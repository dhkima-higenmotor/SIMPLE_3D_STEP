"""Altium PCB STEP Simplification Engine — Per-Component Single-Solid Reduction.

Unlike the mechanical enclosure pipeline (which drops internal parts and fuses everything
into one body), PCB Mode skips internal-part visibility culling and simplifies every
mounted PCB component into its own lightweight, watertight single solid while preserving:
  - PCB board substrate outline and mounting holes (>= min_board_hole_mm)
  - Cylindrical geometry (electrolytic capacitors, radial cans, round inductors) as cylinders
    (fusing a simple square base box when an SMD can sits on a noticeably wider base flange)
  - IC chips & connectors: main body + heatsink/tab + grouped lead/pin bank boxes fused into 1 solid
  - Removal of tiny engraved/embossed text & logo features
  - Original main-body representative RGB color per component
  - Optional Altium IDF (.bdf/.ldf) supplementation of STEP-missing components as boxes
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Optional

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Defeaturing, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_Transform,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
)
from OCP.GeomAbs import (
    GeomAbs_BSplineSurface,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Plane,
    GeomAbs_Sphere,
)
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from scipy.spatial import ConvexHull

from simple_3d_step.core.idf_parser import (
    IdfAlignment,
    IdfBoardData,
    StepComponentFootprint,
    build_idf_board_shape,
    build_idf_components,
    supplement_missing_idf_parts,
)
from simple_3d_step.core.occt_compat import (
    ShapeIndexedMap,
    ShapeList,
    ensure_occt_parallelism_enabled,
    to_edge,
    to_face,
    to_vertex,
)
from simple_3d_step.models import (
    CancelCheck,
    PcbAssemblyModel,
    PcbComponentGroup,
    PcbSimplifiedPart,
    PcbSimplifyConfig,
    PcbSimplifyResult,
    PcbSubBody,
    ProgressCallback,
    RGBColor,
)


_BOARD_NAME_RE = re.compile(
    r"(?:^|[^A-Za-z])(PCB\w*|BOARD\w*|SUBSTRATE|FR4|MAINBOARD\w*|SUBBOARD\w*|DAUGHTER\w*|PWB\w*)",
    re.IGNORECASE,
)
_HEATSINK_NAME_RE = re.compile(
    r"(?:^|[^A-Za-z])(HEAT[\s_-]?SINK|HEATSINK|COOLER|RADIATOR|THERMAL[\s_-]?(?:PAD|PLATE|BLOCK|SPREADER)?|"
    r"HEAT[\s_-]?(?:SPREADER|PLATE)|COLD[\s_-]?PLATE|FIN[\s_-]?BLOCK|BRK[\s_-])"
    r"|(?:^|[^A-Za-z0-9])(HS|HT)\d+(?:[^A-Za-z0-9]|$)",
    re.IGNORECASE,
)
_ELECTRONIC_REFDES_RE = re.compile(
    r"^(U|IC|Q|J|CN|CON|P|R|C|L|D|Y|X|SW|K|T|F|FB|TP|LED|JP|BH|RT|RV|ZD|TVS|FL)\d+",
    re.IGNORECASE,
)
_DEFAULT_BODY_COLOR: RGBColor = (0.18, 0.18, 0.20)
_DEFAULT_BOARD_COLOR: RGBColor = (0.0, 0.30, 0.10)


# ---------------------------------------------------------------------------
# Basic OCCT Geometry Helpers
# ---------------------------------------------------------------------------

def _count_subshapes(shape: TopoDS_Shape, stype) -> int:
    cnt = 0
    exp = TopExp_Explorer(shape, stype)
    while exp.More():
        cnt += 1
        exp.Next()
    return cnt


def _shape_bbox(shape: TopoDS_Shape) -> tuple[np.ndarray, np.ndarray]:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return np.zeros(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
    pmin, pmax = box.CornerMin(), box.CornerMax()
    return (
        np.array([pmin.X(), pmin.Y(), pmin.Z()], dtype=np.float64),
        np.array([pmax.X(), pmax.Y(), pmax.Z()], dtype=np.float64),
    )


def _shape_volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return abs(float(props.Mass()))


def _collect_shape_points(shape: TopoDS_Shape, max_points: int = 0) -> np.ndarray:
    """Collect ALL unique 3D vertex coordinates (plus curved edge samples) without early truncation.

    Truncating vertices early in topology order causes catastrophic OBB shrinkage on complex
    components such as BGA packages (hundreds of solder balls) or multi-pin connectors.
    """
    vmap = ShapeIndexedMap()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vmap)
    n_verts = vmap.Extent()
    pts: list[tuple[float, float, float]] = []
    # Collect all unique vertices (strided uniformly only if gigantic > 20,000 vertices)
    step = max(1, n_verts // max_points) if (max_points > 0 and n_verts > max_points) else 1
    for i in range(1, n_verts + 1, step):
        p = BRep_Tool.Pnt_s(to_vertex(vmap.FindKey(i)))
        pts.append((float(p.X()), float(p.Y()), float(p.Z())))

    # Also sample curved edges (important for pure cylinders / circles with only 2 seam vertices)
    if len(pts) < 32:
        emap = ShapeIndexedMap()
        TopExp.MapShapes_s(shape, TopAbs_EDGE, emap)
        for i in range(1, emap.Extent() + 1):
            ed = to_edge(emap.FindKey(i))
            if not BRep_Tool.Degenerated_s(ed):
                try:
                    c = BRepAdaptor_Curve(ed)
                    u0, u1 = c.FirstParameter(), c.LastParameter()
                    if math.isfinite(u0) and math.isfinite(u1) and abs(u1 - u0) > 1e-7:
                        for t in (0.25, 0.5, 0.75):
                            p = c.Value(u0 + (u1 - u0) * t)
                            pts.append((float(p.X()), float(p.Y()), float(p.Z())))
                except Exception:
                    pass

    if not pts:
        bmin, bmax = _shape_bbox(shape)
        pts = [
            (bmin[0], bmin[1], bmin[2]),
            (bmax[0], bmin[1], bmin[2]),
            (bmax[0], bmax[1], bmin[2]),
            (bmin[0], bmax[1], bmin[2]),
            (bmin[0], bmin[1], bmax[2]),
            (bmax[0], bmin[1], bmax[2]),
            (bmax[0], bmax[1], bmax[2]),
            (bmin[0], bmax[1], bmax[2]),
        ]
    return np.asarray(pts, dtype=np.float64)


def _min_area_rect_2d(points_xy: np.ndarray) -> tuple[float, float, float, float, float]:
    """Compute exact 2D minimum-area oriented bounding box (cx, cy, w, d, angle_rad)."""
    if len(points_xy) < 3:
        xmin, ymin = float(np.min(points_xy[:, 0])), float(np.min(points_xy[:, 1]))
        xmax, ymax = float(np.max(points_xy[:, 0])), float(np.max(points_xy[:, 1]))
        return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax), max(0.05, xmax - xmin), max(0.05, ymax - ymin), 0.0)

    # Deduplicate quantized points to avoid degenerate ConvexHull
    unique_pts = np.unique(np.round(points_xy, 4), axis=0)
    if len(unique_pts) < 3:
        xmin, ymin = float(np.min(points_xy[:, 0])), float(np.min(points_xy[:, 1]))
        xmax, ymax = float(np.max(points_xy[:, 0])), float(np.max(points_xy[:, 1]))
        return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax), max(0.05, xmax - xmin), max(0.05, ymax - ymin), 0.0)

    try:
        hull = ConvexHull(unique_pts)
        hull_pts = unique_pts[hull.vertices]
    except Exception:
        xmin, ymin = float(np.min(points_xy[:, 0])), float(np.min(points_xy[:, 1]))
        xmax, ymax = float(np.max(points_xy[:, 0])), float(np.max(points_xy[:, 1]))
        return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax), max(0.05, xmax - xmin), max(0.05, ymax - ymin), 0.0)

    # Include 0 deg axis-aligned candidate plus all convex hull edge directions
    edges = np.roll(hull_pts, -1, axis=0) - hull_pts
    angles = list(np.arctan2(edges[:, 1], edges[:, 0]))
    angles.append(0.0)

    best_area = float("inf")
    best_params = (0.0, 0.0, 1.0, 1.0, 0.0)
    aabb_area = float(
        (np.max(hull_pts[:, 0]) - np.min(hull_pts[:, 0]))
        * (np.max(hull_pts[:, 1]) - np.min(hull_pts[:, 1]))
    )

    for ang in angles:
        # Normalize angle to [0, pi/2)
        a = float(ang) % (0.5 * math.pi)
        c, s = math.cos(a), math.sin(a)
        u = hull_pts[:, 0] * c + hull_pts[:, 1] * s
        v = -hull_pts[:, 0] * s + hull_pts[:, 1] * c
        umin, umax = float(np.min(u)), float(np.max(u))
        vmin, vmax = float(np.min(v)), float(np.max(v))
        w = max(0.05, umax - umin)
        d = max(0.05, vmax - vmin)
        area = w * d
        if area < best_area - 1e-7:
            best_area = area
            uc, vc = 0.5 * (umin + umax), 0.5 * (vmin + vmax)
            cx = uc * c - vc * s
            cy = uc * s + vc * c
            best_params = (cx, cy, w, d, a)

    # If axis-aligned box is within 1.5% of minimum OBB area, prefer clean 0 deg alignment
    if aabb_area <= best_area * 1.015:
        xmin, ymin = float(np.min(points_xy[:, 0])), float(np.min(points_xy[:, 1]))
        xmax, ymax = float(np.max(points_xy[:, 0])), float(np.max(points_xy[:, 1]))
        return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax), max(0.05, xmax - xmin), max(0.05, ymax - ymin), 0.0)

    # Snap near-90-deg angles
    cx, cy, w, d, a = best_params
    deg = math.degrees(a)
    if deg < 1.0 or deg > 89.0:
        xmin, ymin = float(np.min(points_xy[:, 0])), float(np.min(points_xy[:, 1]))
        xmax, ymax = float(np.max(points_xy[:, 0])), float(np.max(points_xy[:, 1]))
        return (0.5 * (xmin + xmax), 0.5 * (ymin + ymax), max(0.05, xmax - xmin), max(0.05, ymax - ymin), 0.0)

    return best_params


def _make_local_box(
    u0: float,
    u1: float,
    v0: float,
    v1: float,
    z0: float,
    z1: float,
    origin_xy: tuple[float, float],
    angle_rad: float,
) -> TopoDS_Shape:
    """Create a watertight OCCT box defined in local (u, v, z) frame and transformed to world."""
    w = max(0.05, u1 - u0)
    d = max(0.05, v1 - v0)
    h = max(0.05, z1 - z0)
    box = BRepPrimAPI_MakeBox(gp_Pnt(u0, v0, z0), w, d, h).Shape()

    trsf_rot = gp_Trsf()
    if abs(angle_rad) > 1e-6:
        trsf_rot.SetRotation(gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), angle_rad)
    trsf_t = gp_Trsf()
    trsf_t.SetTranslation(gp_Vec(origin_xy[0], origin_xy[1], 0.0))
    trsf_t.Multiply(trsf_rot)
    return BRepBuilderAPI_Transform(box, trsf_t, True).Shape()


def _world_to_local_bbox(
    shape: TopoDS_Shape,
    origin_xy: tuple[float, float],
    angle_rad: float,
) -> tuple[float, float, float, float, float, float]:
    """Compute tight (u0, u1, v0, v1, z0, z1) bounds of a shape in a local XY-rotated frame."""
    if abs(angle_rad) < 1e-6:
        bmin, bmax = _shape_bbox(shape)
        return (
            float(bmin[0]) - origin_xy[0],
            float(bmax[0]) - origin_xy[0],
            float(bmin[1]) - origin_xy[1],
            float(bmax[1]) - origin_xy[1],
            float(bmin[2]),
            float(bmax[2]),
        )
    pts = _collect_shape_points(shape)
    dx = pts[:, 0] - origin_xy[0]
    dy = pts[:, 1] - origin_xy[1]
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    u = dx * c + dy * s
    v = -dx * s + dy * c
    z = pts[:, 2]
    return (
        float(np.min(u)),
        float(np.max(u)),
        float(np.min(v)),
        float(np.max(v)),
        float(np.min(z)),
        float(np.max(z)),
    )


def _fuse_primitives_to_single_solid(
    shapes: list[TopoDS_Shape],
    fuzzy_values: tuple[float, ...] = (0.0, 1e-3, 5e-3),
) -> Optional[TopoDS_Shape]:
    """Fuse a list of touching/overlapping solids into 1 clean unified solid."""
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]

    for fuzzy in fuzzy_values:
        try:
            fuse = BRepAlgoAPI_Fuse()
            fuse.SetRunParallel(True)
            fuse.SetNonDestructive(True)
            if fuzzy > 0.0:
                fuse.SetFuzzyValue(fuzzy)
            arg_list = ShapeList()
            arg_list.Append(shapes[0])
            tool_list = ShapeList()
            for sh in shapes[1:]:
                tool_list.Append(sh)
            fuse.SetArguments(arg_list)
            fuse.SetTools(tool_list)
            fuse.Build()
            if not fuse.IsDone():
                continue
            res = fuse.Shape()
            if res.IsNull():
                continue
            if _count_subshapes(res, TopAbs_SOLID) == 1:
                try:
                    unify = ShapeUpgrade_UnifySameDomain(res, True, True, False)
                    unify.Build()
                    unified = unify.Shape()
                    if not unified.IsNull() and _count_subshapes(unified, TopAbs_SOLID) == 1:
                        res = unified
                except Exception:
                    pass
                return res
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Color Extraction & Flat-STEP Clustering
# ---------------------------------------------------------------------------

def _ensure_min_brightness(rgb: RGBColor, min_brightness: float) -> RGBColor:
    """Automatically brighten overly dark component colors so shading and edges remain visible.

    Uses ITU-R BT.709 luminance ``L = 0.2126*R + 0.7152*G + 0.0722*B`` (in OCCT linear RGB).
    Pure black / near-black parts (e.g. ``(0.0, 0.0, 0.0)`` housings like ``J1_HDR1x2``) are
    lifted to a readable dark charcoal, while dark tinted parts preserve their hue.
    """
    r = max(0.0, min(1.0, float(rgb[0])))
    g = max(0.0, min(1.0, float(rgb[1])))
    b = max(0.0, min(1.0, float(rgb[2])))
    floor = max(0.0, min(0.85, float(min_brightness)))
    if floor <= 0.0:
        return (r, g, b)

    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    if lum >= floor:
        return (r, g, b)

    # Near-black / achromatic -> clean neutral charcoal at target brightness
    if max(r, g, b) < 1e-3 or lum < 1e-4:
        return (floor, floor, floor)

    # Preserve hue by scaling up; if any channel would clip > 1.0, lift evenly toward floor
    scale = floor / lum
    sr, sg, sb = r * scale, g * scale, b * scale
    if max(sr, sg, sb) <= 1.0:
        return (sr, sg, sb)

    # Smooth additive blend with neutral gray to reach exact luminance floor without clipping
    # Solve (1 - t) * lum + t * 1.0 = floor  ->  t = (floor - lum) / (1.0 - lum)
    t = max(0.0, min(1.0, (floor - lum) / max(1e-6, 1.0 - lum)))
    return (
        min(1.0, r + t * (1.0 - r)),
        min(1.0, g + t * (1.0 - g)),
        min(1.0, b + t * (1.0 - b)),
    )


def _extract_representative_color(
    bodies: list[PcbSubBody],
    default_color: RGBColor = _DEFAULT_BODY_COLOR,
    min_brightness: float = 0.0,
) -> RGBColor:
    """Extract the main body's representative RGB color, ignoring tiny marking faces."""
    picked: RGBColor = default_color
    if bodies:
        # Prefer the largest-volume body (main epoxy/plastic/metal body over leads)
        sorted_bodies = sorted(bodies, key=lambda b: (b.volume, b.surface_area), reverse=True)
        found = False
        for body in sorted_bodies:
            if body.face_colors:
                max_area = max(a for _, a in body.face_colors)
                # Filter out tiny marking/chamfer faces (< 0.5% of largest face)
                significant = [(rgb, a) for rgb, a in body.face_colors if a >= max_area * 0.005]
                if not significant:
                    significant = body.face_colors
                buckets: dict[tuple[int, int, int], list[float]] = {}
                for (r, g, b), area in significant:
                    # Quantize slightly (step 4) to merge near-identical shading tolerances
                    key = (int(round(r * 64)), int(round(g * 64)), int(round(b * 64)))
                    entry = buckets.get(key)
                    if entry is None:
                        buckets[key] = [area, r * area, g * area, b * area]
                    else:
                        entry[0] += area
                        entry[1] += r * area
                        entry[2] += g * area
                        entry[3] += b * area
                best = max(buckets.values(), key=lambda v: v[0])
                if best[0] > 0.0:
                    picked = (best[1] / best[0], best[2] / best[0], best[3] / best[0])
                    found = True
                    break
            if body.color is not None:
                picked = body.color
                found = True
                break
        if not found:
            picked = default_color

    return _ensure_min_brightness(picked, min_brightness) if min_brightness > 0.0 else picked


def _analyze_slab_topology(
    body: PcbSubBody,
    thin_axis: int,
) -> tuple[float, float, int, int]:
    """Analyze a candidate slab along ``thin_axis`` (0=X, 1=Y, 2=Z).

    Returns ``(bot_plane_area, top_plane_area, cone_count, sphere_bspline_count)``.
    """
    bmin, bmax = body.bbox_min, body.bbox_max
    t_lo, t_hi = float(bmin[thin_axis]), float(bmax[thin_axis])
    tol = max(0.08, 0.06 * max(0.1, t_hi - t_lo))

    bot_area = 0.0
    top_area = 0.0
    cone_cnt = 0
    freeform_cnt = 0

    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        try:
            surf = BRepAdaptor_Surface(face, False)
            st = surf.GetType()
            if st == GeomAbs_Plane:
                d = surf.Plane().Axis().Direction()
                n_comp = abs((float(d.X()), float(d.Y()), float(d.Z()))[thin_axis])
                if n_comp > 0.98:
                    fmin, fmax = _shape_bbox(face)
                    fz = 0.5 * (float(fmin[thin_axis]) + float(fmax[thin_axis]))
                    if abs(fz - t_lo) <= tol:
                        props = GProp_GProps()
                        BRepGProp.SurfaceProperties_s(face, props)
                        bot_area += abs(float(props.Mass()))
                    elif abs(fz - t_hi) <= tol:
                        props = GProp_GProps()
                        BRepGProp.SurfaceProperties_s(face, props)
                        top_area += abs(float(props.Mass()))
            elif st == GeomAbs_Cone:
                cone_cnt += 1
            elif st in (GeomAbs_Sphere, GeomAbs_BSplineSurface):
                freeform_cnt += 1
        except Exception:
            pass
        exp.Next()

    return bot_area, top_area, cone_cnt, freeform_cnt


def _is_electronic_refdes(comp: PcbComponentGroup) -> bool:
    ref = (comp.ref_des or "").strip()
    if ref and _ELECTRONIC_REFDES_RE.match(ref):
        return True
    return False


def _component_own_name(comp: PcbComponentGroup) -> str:
    """Return the component's own RefDes/leaf name without any ancestor PCB sub-assembly prefix."""
    own = f"{comp.ref_des} {comp.leaf_name}".strip()
    return own if own else comp.name


def _detect_board_indices(components: list[PcbComponentGroup]) -> set[int]:
    """Identify 1, 2, or more PCB substrate boards by flat double-sided slab geometry & naming.

    Handles:
      - Multi-PCB assemblies (mainboard + smaller daughterboards / sub-PCBs)
      - Horizontal (XY) and vertical/riser (XZ / YZ) PCBs
      - Distinguishes flat PCB substrates from machined heatsink/bracket plates (many cones/steps)
        and from mounted electronic ICs/connectors.
    """
    if not components:
        return set()

    board_indices: set[int] = set()
    best_fallback_idx: Optional[int] = None
    best_fallback_score = -1.0

    for idx, comp in enumerate(components):
        if not comp.bodies:
            continue
        if _is_electronic_refdes(comp):
            continue

        own_name = _component_own_name(comp)
        has_board_name = _BOARD_NAME_RE.search(own_name) is not None
        has_heatsink_name = _HEATSINK_NAME_RE.search(own_name) is not None
        if has_heatsink_name and not has_board_name:
            continue

        main_body = max(comp.bodies, key=lambda b: (b.volume, b.surface_area))
        # Guard against ever classifying an unflattened multi-part assembly as a single PCB slab
        if len(comp.bodies) > 25 and main_body.volume < 0.50 * max(comp.total_volume, 1e-6):
            continue
        sz = main_body.bbox_size
        order = np.argsort(sz)
        thin_axis = int(order[0])
        t_min = float(sz[order[0]])
        d_mid = float(sz[order[1]])
        d_max = float(sz[order[2]])

        # PCB substrates are thin planar slabs (0.25 mm to 6.5 mm thick)
        if not (0.25 <= t_min <= 6.5):
            continue
        if d_mid < 10.0 or d_max < 12.0:
            continue
        aspect_mid = d_mid / max(t_min, 1e-3)
        aspect_max = d_max / max(t_min, 1e-3)
        if aspect_mid < 3.5 or aspect_max < 4.5:
            continue

        bot_area, top_area, cone_cnt, freeform_cnt = _analyze_slab_topology(main_body, thin_axis)
        face_area_ref = max(1e-6, d_mid * d_max)
        cov_both = (bot_area + top_area) / (2.0 * face_area_ref)
        balance = min(bot_area, top_area) / max(bot_area, top_area, 1e-6)

        # Machined plates / brackets with many countersunk cones or heavy freeform BGA/chip faces
        # are NOT PCB substrates (unless explicitly named PCB/Board).
        is_flat_double_sided = (
            cov_both >= (0.30 if has_board_name else 0.48)
            and balance >= (0.40 if has_board_name else 0.50)
            and freeform_cnt <= 8
        )

        if has_board_name and (is_flat_double_sided or (aspect_max >= 5.0 and cone_cnt <= 8)):
            board_indices.add(idx)
            continue

        # Unnamed / generic PCB substrate (supports multi-board assemblies of different sizes!)
        if (
            is_flat_double_sided
            and 0.30 <= t_min <= 4.5
            and d_mid >= 18.0
            and d_max >= 20.0
            and aspect_max >= 6.0
            and cone_cnt <= 4
        ):
            board_indices.add(idx)
            continue

        # Track largest plausible thin-slab fallback if no explicit/clean board was found
        if aspect_max >= 5.0 and freeform_cnt <= 12 and cone_cnt <= 6:
            score = d_mid * d_max + (5000.0 if has_board_name else 0.0)
            if score > best_fallback_score:
                best_fallback_score = score
                best_fallback_idx = idx

    if not board_indices and best_fallback_idx is not None:
        board_indices.add(best_fallback_idx)
    return board_indices


def _has_parallel_fin_array(body: PcbSubBody) -> bool:
    """Return True if a body exhibits an extruded finned heatsink profile (>= 6 parallel fin walls)."""
    total_area = float(body.surface_area)
    if total_area <= 1e-6:
        return False

    # Group planar faces by unsigned normal direction (n and -n share the same fin pitch axis)
    dir_groups: list[dict[str, Any]] = []
    min_wall_area = max(1.5, 0.012 * total_area)

    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        try:
            surf = BRepAdaptor_Surface(face, False)
            if surf.GetType() == GeomAbs_Plane:
                props = GProp_GProps()
                BRepGProp.SurfaceProperties_s(face, props)
                area = abs(float(props.Mass()))
                if area >= min_wall_area:
                    pl = surf.Plane()
                    d = pl.Axis().Direction()
                    n = np.array([float(d.X()), float(d.Y()), float(d.Z())], dtype=np.float64)
                    nlen = float(np.linalg.norm(n))
                    if nlen > 1e-12:
                        n /= nlen
                        # Canonicalize sign so largest component is positive
                        dom = int(np.argmax(np.abs(n)))
                        if n[dom] < 0.0:
                            n = -n
                        loc = pl.Location()
                        offset = float(n[0] * loc.X() + n[1] * loc.Y() + n[2] * loc.Z())
                        placed = False
                        for grp in dir_groups:
                            if abs(float(np.dot(grp["n"], n))) > 0.995:
                                grp["planes"].append((offset, area))
                                placed = True
                                break
                        if not placed:
                            dir_groups.append({"n": n, "planes": [(offset, area)]})
        except Exception:
            pass
        exp.Next()

    for grp in dir_groups:
        # Merge coplanar faces at the same offset (within 0.15 mm)
        planes = sorted(grp["planes"], key=lambda x: x[0])
        merged: list[tuple[float, float]] = []
        for off, area in planes:
            if merged and abs(off - merged[-1][0]) <= 0.15:
                merged[-1] = (merged[-1][0], merged[-1][1] + area)
            else:
                merged.append((off, area))
        # >= 6 distinct parallel fin walls (>= 3 extruded fins) carrying >= 25% of surface area
        if len(merged) >= 6:
            fin_area = sum(a for _, a in merged)
            if fin_area >= 0.25 * total_area:
                return True
    return False


def _has_pin_fin_array(body: PcbSubBody) -> bool:
    """Return True if a body is a pin-fin heatsink (flat base + >= 9 similar vertical pin posts)."""
    bsz = body.bbox_size
    if float(np.min(bsz)) < 3.5 or float(np.max(bsz)) < 12.0:
        return False

    cyl_posts: list[tuple[float, float, float]] = []  # (radius, axis_x_or_y_hash, height)
    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        try:
            surf = BRepAdaptor_Surface(face, False)
            if surf.GetType() == GeomAbs_Cylinder:
                cyl = surf.Cylinder()
                d = cyl.Axis().Direction()
                if abs(float(d.Z())) > 0.98 and _is_cylinder_face_concave(face, cyl) is False:
                    r = float(cyl.Radius())
                    if 0.30 <= r <= 3.0:
                        fmin, fmax = _shape_bbox(face)
                        h = float(fmax[2] - fmin[2])
                        if h >= 2.0:
                            loc = cyl.Location()
                            cyl_posts.append((round(r, 1), round(float(loc.X()), 1) * 1000.0 + round(float(loc.Y()), 1), h))
        except Exception:
            pass
        exp.Next()

    if len(cyl_posts) < 9:
        return False
    # Count unique pin centers sharing the dominant pin radius
    by_r: dict[float, set[float]] = {}
    for r_key, pos_key, _h in cyl_posts:
        by_r.setdefault(r_key, set()).add(pos_key)
    return any(len(centers) >= 9 for centers in by_r.values())


def _detect_heatsink_indices(
    components: list[PcbComponentGroup],
    board_indices: set[int],
) -> set[int]:
    """Identify heatsink / thermal spreader / machined heat-plate components to preserve intact."""
    heatsink_indices: set[int] = set()
    board_areas = [
        float(np.sort(components[i].bbox_size)[1] * np.sort(components[i].bbox_size)[2])
        for i in board_indices
        if components[i].bodies
    ]
    max_board_area = max(board_areas) if board_areas else 0.0
    board_boxes = [
        (components[i].bbox_min, components[i].bbox_max)
        for i in board_indices
        if components[i].bodies
    ]

    for idx, comp in enumerate(components):
        if idx in board_indices or not comp.bodies:
            continue

        own_name = _component_own_name(comp)
        sz = comp.bbox_size
        s_sorted = np.sort(sz)
        t_min, d_mid, d_max = float(s_sorted[0]), float(s_sorted[1]), float(s_sorted[2])

        # 1. Explicit heatsink / thermal / bracket naming (check component's own name, not parent PCB prefix)
        if _HEATSINK_NAME_RE.search(own_name) and d_max >= 8.0 and len(comp.bodies) <= 8:
            heatsink_indices.add(idx)
            continue

        # Never misclassify standard mounted electronic components (connectors/ICs/passives/standoffs)
        if _is_electronic_refdes(comp) or len(comp.bodies) > 6:
            continue

        main_body = max(comp.bodies, key=lambda b: (b.volume, b.surface_area))
        bbox_vol = max(1e-6, float(sz[0] * sz[1] * sz[2]))
        fill_ratio = comp.total_volume / bbox_vol

        # 2. Geometric extruded finned heatsink (>= 6 parallel fin walls with air gaps)
        if d_max >= 12.0 and d_mid >= 8.0 and t_min >= 3.0 and fill_ratio < 0.80:
            if _has_parallel_fin_array(main_body) or _has_pin_fin_array(main_body):
                heatsink_indices.add(idx)
                continue

        # 3. Large machined thermal baseplate / heat spreader sitting against a PCB
        #    (e.g. aluminum heatsink plate with countersunk mounting holes under a power PCB)
        if (
            len(comp.bodies) <= 4
            and d_mid >= 25.0
            and d_max >= 35.0
            and (max_board_area <= 0.0 or (d_mid * d_max) >= 0.25 * max_board_area)
        ):
            overlaps_board = False
            for bmin, bmax in board_boxes:
                ox = min(float(comp.bbox_max[0]), float(bmax[0])) - max(float(comp.bbox_min[0]), float(bmin[0]))
                oy = min(float(comp.bbox_max[1]), float(bmax[1])) - max(float(comp.bbox_min[1]), float(bmin[1]))
                if ox > 5.0 and oy > 5.0:
                    overlaps_board = True
                    break
            if overlaps_board:
                heatsink_indices.add(idx)
                continue

    return heatsink_indices


def _cluster_flat_components(
    components: list[PcbComponentGroup],
    board_indices: set[int],
    gap_mm: float = 0.25,
) -> list[PcbComponentGroup]:
    """Group spatially touching/overlapping bodies when a STEP file had no sub-assemblies."""
    boards = [c for i, c in enumerate(components) if i in board_indices]
    others = [c for i, c in enumerate(components) if i not in board_indices and c.bodies]
    if len(others) <= 1:
        return boards + others

    n = len(others)
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    mins = np.array([c.bbox_min - gap_mm for c in others], dtype=np.float64)
    maxs = np.array([c.bbox_max + gap_mm for c in others], dtype=np.float64)

    for i in range(n):
        for j in range(i + 1, n):
            if (
                mins[i, 0] <= maxs[j, 0]
                and maxs[i, 0] >= mins[j, 0]
                and mins[i, 1] <= maxs[j, 1]
                and maxs[i, 1] >= mins[j, 1]
                and mins[i, 2] <= maxs[j, 2]
                and maxs[i, 2] >= mins[j, 2]
            ):
                _union(i, j)

    groups: dict[int, list[PcbComponentGroup]] = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(others[i])

    merged: list[PcbComponentGroup] = list(boards)
    for grp in groups.values():
        all_bodies = [b for c in grp for b in c.bodies]
        main = max(grp, key=lambda c: c.total_volume)
        merged.append(
            PcbComponentGroup(
                comp_id=len(merged),
                name=main.name,
                assembly_path=main.assembly_path,
                bodies=all_bodies,
                rotation_xy_deg=main.rotation_xy_deg,
            )
        )
    return merged


# ---------------------------------------------------------------------------
# PCB Board Cleanup (keep outline & mounting holes >= min_board_hole_mm)
# ---------------------------------------------------------------------------

def _simplify_board_component(
    comp: PcbComponentGroup,
    cfg: PcbSimplifyConfig,
) -> PcbSimplifiedPart:
    """Clean PCB board substrate: only apply hole deletion (< min_board_hole_mm), preserve all other geometry."""
    main_body = max(comp.bodies, key=lambda b: (b.volume, b.surface_area))
    board_color = _extract_representative_color(
        [main_body],
        default_color=_DEFAULT_BOARD_COLOR,
        min_brightness=cfg.min_color_brightness,
    )
    shape = main_body.shape
    orig_faces = comp.total_faces
    orig_vol = max(main_body.volume, 1e-6)

    # Identify board normal axis (thin-slab axis: Z for horizontal PCBs, X/Y for vertical daughterboards)
    thin_axis = int(np.argmin(main_body.bbox_size))
    u_axis, v_axis = ((1, 2), (2, 0), (0, 1))[thin_axis]

    # Plug small cylindrical via holes (< min_board_hole_mm) aligned with board normal while keeping all else
    if cfg.keep_board_holes and cfg.min_board_hole_mm > 0.0:
        min_dia = float(cfg.min_board_hole_mm)
        small_hole_faces: list[TopoDS_Shape] = []
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        while exp.More():
            face = to_face(exp.Current())
            try:
                surf = BRepAdaptor_Surface(face, False)
                if surf.GetType() == GeomAbs_Cylinder:
                    cyl = surf.Cylinder()
                    axis_dir = cyl.Axis().Direction()
                    dir_comp = abs((float(axis_dir.X()), float(axis_dir.Y()), float(axis_dir.Z()))[thin_axis])
                    if dir_comp > 0.95:
                        dia = 2.0 * float(cyl.Radius())
                        if 0.0 < dia < min_dia:
                            # Ensure this is an internal hole (strictly inside board outline in plane)
                            fmin, fmax = _shape_bbox(face)
                            margin = 0.2
                            if (
                                fmin[u_axis] > main_body.bbox_min[u_axis] + margin
                                and fmax[u_axis] < main_body.bbox_max[u_axis] - margin
                                and fmin[v_axis] > main_body.bbox_min[v_axis] + margin
                                and fmax[v_axis] < main_body.bbox_max[v_axis] - margin
                            ):
                                small_hole_faces.append(face)
            except Exception:
                pass
            exp.Next()

        if small_hole_faces:
            try:
                defeat = BRepAlgoAPI_Defeaturing()
                defeat.SetRunParallel(True)
                defeat.SetShape(shape)
                for f in small_hole_faces:
                    defeat.AddFaceToRemove(f)
                defeat.Build()
                if defeat.IsDone():
                    cand = defeat.Shape()
                    if (
                        not cand.IsNull()
                        and _count_subshapes(cand, TopAbs_SOLID) == 1
                        and _shape_volume(cand) >= orig_vol * 0.98
                    ):
                        shape = cand
            except Exception:
                pass

    # Merge coplanar / co-cylindrical faces on the board without altering its geometry
    try:
        unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
        unify.Build()
        unified = unify.Shape()
        if (
            not unified.IsNull()
            and _count_subshapes(unified, TopAbs_SOLID) == 1
            and _shape_volume(unified) >= orig_vol * 0.98
        ):
            shape = unified
    except Exception:
        pass

    return PcbSimplifiedPart(
        name=comp.name or "PCB_Board",
        shape=shape,
        color=board_color,
        category="board",
        ref_des=comp.ref_des or "Board",
        leaf_name=comp.leaf_name or "",
        original_bodies=len(comp.bodies),
        original_faces=orig_faces,
        simplified_faces=_count_subshapes(shape, TopAbs_FACE),
        solid_count=max(1, _count_subshapes(shape, TopAbs_SOLID)),
    )


def _simplify_heatsink_component(
    comp: PcbComponentGroup,
    cfg: PcbSimplifyConfig,
) -> PcbSimplifiedPart:
    """Preserve heatsink / thermal spreader original geometry intact (no hole deletion or boxing)."""
    bodies = list(comp.bodies)
    main_body = max(bodies, key=lambda b: (b.volume, b.surface_area))
    hs_color = (
        _extract_representative_color(
            bodies,
            default_color=(0.60, 0.62, 0.65),
            min_brightness=cfg.min_color_brightness,
        )
        if cfg.preserve_colors
        else None
    )
    orig_vol = max(main_body.volume, 1e-6)

    if len(bodies) == 1:
        shape = main_body.shape
        try:
            unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
            unify.Build()
            unified = unify.Shape()
            if (
                not unified.IsNull()
                and _count_subshapes(unified, TopAbs_SOLID) == 1
                and BRepCheck_Analyzer(unified).IsValid()
            ):
                u_vol = _shape_volume(unified)
                if 0.99 * orig_vol <= u_vol <= 1.01 * orig_vol:
                    shape = unified
        except Exception:
            pass
    else:
        fused = _fuse_primitives_to_single_solid(
            [b.shape for b in bodies],
            fuzzy_values=(0.0, 1e-3, 5e-3, 2e-2),
        )
        if fused is not None and BRepCheck_Analyzer(fused).IsValid():
            shape = fused
        else:
            shape = main_body.shape

    return PcbSimplifiedPart(
        name=comp.name or "Heatsink",
        shape=shape,
        color=hs_color,
        category="heatsink",
        ref_des=comp.ref_des,
        leaf_name=comp.leaf_name,
        original_bodies=len(comp.bodies),
        original_faces=comp.total_faces,
        simplified_faces=_count_subshapes(shape, TopAbs_FACE),
        solid_count=max(1, _count_subshapes(shape, TopAbs_SOLID)),
    )


# ---------------------------------------------------------------------------
# Text / Logo Feature Removal (Requirement 7)
# ---------------------------------------------------------------------------

def _filter_text_bodies(
    bodies: list[PcbSubBody],
) -> tuple[list[PcbSubBody], int]:
    """Drop tiny engraved/embossed text & logo solids sitting on top/bottom of the main body."""
    if len(bodies) <= 1:
        return bodies, 0

    main = max(bodies, key=lambda b: (b.volume, b.surface_area))
    main_vol = max(main.volume, 1e-9)
    mmin, mmax = main.bbox_min, main.bbox_max

    kept: list[PcbSubBody] = []
    removed = 0
    for b in bodies:
        if b is main:
            kept.append(b)
            continue
        bsz = b.bbox_size
        min_dim = float(np.min(bsz))
        # Check if body is strictly inside main body's XY footprint (not peripheral leads!)
        inside_xy = (
            b.bbox_min[0] >= mmin[0] - 0.05
            and b.bbox_max[0] <= mmax[0] + 0.05
            and b.bbox_min[1] >= mmin[1] - 0.05
            and b.bbox_max[1] <= mmax[1] + 0.05
        )
        near_top_or_bot = (
            abs(float(b.bbox_min[2]) - float(mmax[2])) < 0.25
            or abs(float(b.bbox_max[2]) - float(mmax[2])) < 0.25
            or abs(float(b.bbox_max[2]) - float(mmin[2])) < 0.25
            or abs(float(b.bbox_min[2]) - float(mmin[2])) < 0.25
        )
        is_tiny_text = (
            inside_xy
            and near_top_or_bot
            and b.volume < main_vol * 0.008
            and min_dim < 0.25
        )
        if is_tiny_text:
            removed += 1
        else:
            kept.append(b)

    return (kept if kept else [main]), removed


# ---------------------------------------------------------------------------
# Surface Normal, Hole Detection & Low-Complexity Preservation Helpers
# ---------------------------------------------------------------------------

def _face_outward_normal_at_mid(face: TopoDS_Shape) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """Return (midpoint_3d, outward_unit_normal) accounting for topological face orientation."""
    try:
        bf = BRepGProp_Face(face)
        u1, u2, v1, v2 = bf.Bounds()
        if not (math.isfinite(u1) and math.isfinite(u2) and math.isfinite(v1) and math.isfinite(v2)):
            return None
        p = gp_Pnt()
        vnorm = gp_Vec()
        bf.Normal(0.5 * (u1 + u2), 0.5 * (v1 + v2), p, vnorm)
        n = np.array([float(vnorm.X()), float(vnorm.Y()), float(vnorm.Z())], dtype=np.float64)
        nlen = float(np.linalg.norm(n))
        if nlen < 1e-12:
            return None
        return np.array([float(p.X()), float(p.Y()), float(p.Z())], dtype=np.float64), n / nlen
    except Exception:
        return None


def _is_cylinder_face_concave(face: TopoDS_Shape, cyl: Any) -> Optional[bool]:
    """Return True if cylindrical face normal points inward toward axis (internal bore/hole)."""
    res = _face_outward_normal_at_mid(face)
    if res is None:
        return None
    pt, n = res
    loc = cyl.Location()
    d = cyl.Axis().Direction()
    ax_orig = np.array([float(loc.X()), float(loc.Y()), float(loc.Z())], dtype=np.float64)
    ax_dir = np.array([float(d.X()), float(d.Y()), float(d.Z())], dtype=np.float64)
    alen = float(np.linalg.norm(ax_dir))
    if alen < 1e-12:
        return None
    ax_dir /= alen
    rel = pt - ax_orig
    radial = rel - float(np.dot(rel, ax_dir)) * ax_dir
    rlen = float(np.linalg.norm(radial))
    if rlen < 1e-9:
        return None
    dot = float(np.dot(n, radial / rlen))
    if dot < -0.2:
        return True
    if dot > 0.2:
        return False
    return None


def _collect_coaxial_cylinders(
    shapes: list[TopoDS_Shape],
    axis_pt: np.ndarray,
    axis_dir: np.ndarray,
    axis_tol: float,
) -> tuple[list[tuple[float, float, float, float]], list[tuple[float, float, float, float]]]:
    """Collect coaxial outer (convex) and inner (concave hole) cylindrical features.

    Returns ``(outer_cyls, inner_holes)`` where each item is ``(radius, total_area, t_min, t_max)``
    projected along ``axis_dir`` relative to ``axis_pt``, sorted by area descending.
    """
    outer_buckets: dict[int, list[float]] = {}
    inner_buckets: dict[int, list[float]] = {}

    for sh in shapes:
        exp = TopExp_Explorer(sh, TopAbs_FACE)
        while exp.More():
            face = to_face(exp.Current())
            try:
                surf = BRepAdaptor_Surface(face, False)
                if surf.GetType() != GeomAbs_Cylinder:
                    exp.Next()
                    continue
                cyl = surf.Cylinder()
                d = cyl.Axis().Direction()
                cdir = np.array([float(d.X()), float(d.Y()), float(d.Z())], dtype=np.float64)
                clen = float(np.linalg.norm(cdir))
                if clen < 1e-12 or abs(float(np.dot(cdir / clen, axis_dir))) < 0.97:
                    exp.Next()
                    continue
                loc = cyl.Location()
                cloc = np.array([float(loc.X()), float(loc.Y()), float(loc.Z())], dtype=np.float64)
                off = cloc - axis_pt
                perp_dist = float(np.linalg.norm(off - float(np.dot(off, axis_dir)) * axis_dir))
                if perp_dist > axis_tol:
                    exp.Next()
                    continue

                props = GProp_GProps()
                BRepGProp.SurfaceProperties_s(face, props)
                area = abs(float(props.Mass()))
                if area < 0.05:
                    exp.Next()
                    continue

                is_concave = _is_cylinder_face_concave(face, cyl)
                if is_concave is None:
                    exp.Next()
                    continue

                # Project face bbox corners onto axis_dir for reliable axial span
                fmin, fmax = _shape_bbox(face)
                corners = np.array(
                    [
                        [fmin[0], fmin[1], fmin[2]],
                        [fmax[0], fmin[1], fmin[2]],
                        [fmin[0], fmax[1], fmin[2]],
                        [fmax[0], fmax[1], fmin[2]],
                        [fmin[0], fmin[1], fmax[2]],
                        [fmax[0], fmin[1], fmax[2]],
                        [fmin[0], fmax[1], fmax[2]],
                        [fmax[0], fmax[1], fmax[2]],
                    ],
                    dtype=np.float64,
                )
                ts = (corners - axis_pt) @ axis_dir
                t0, t1 = float(np.min(ts)), float(np.max(ts))

                r = float(cyl.Radius())
                rkey = int(round(r * 25.0))  # 0.04 mm radius clustering bucket
                target = inner_buckets if is_concave else outer_buckets
                entry = target.get(rkey)
                if entry is None:
                    target[rkey] = [r * area, area, t0, t1]
                else:
                    entry[0] += r * area
                    entry[1] += area
                    entry[2] = min(entry[2], t0)
                    entry[3] = max(entry[3], t1)
            except Exception:
                pass
            exp.Next()

    def _finalize(buckets: dict[int, list[float]]) -> list[tuple[float, float, float, float]]:
        items = [
            (v[0] / v[1], v[1], v[2], v[3])
            for v in buckets.values()
            if v[1] > 0.0
        ]
        items.sort(key=lambda x: x[1], reverse=True)
        return items

    return _finalize(outer_buckets), _finalize(inner_buckets)


def _count_freeform_faces(bodies: list[PcbSubBody]) -> int:
    """Count BSpline and spherical faces (typically heavy filleted SMD chips or BGA balls)."""
    cnt = 0
    for b in bodies:
        exp = TopExp_Explorer(b.shape, TopAbs_FACE)
        while exp.More():
            try:
                st = BRepAdaptor_Surface(to_face(exp.Current()), False).GetType()
                if st in (GeomAbs_BSplineSurface, GeomAbs_Sphere):
                    cnt += 1
            except Exception:
                pass
            exp.Next()
    return cnt


def _bodies_have_significant_hole(bodies: list[PcbSubBody]) -> bool:
    """Return True if any body contains a significant concave cylindrical bore/hole."""
    for b in bodies:
        exp = TopExp_Explorer(b.shape, TopAbs_FACE)
        while exp.More():
            face = to_face(exp.Current())
            try:
                surf = BRepAdaptor_Surface(face, False)
                if surf.GetType() == GeomAbs_Cylinder:
                    cyl = surf.Cylinder()
                    if float(cyl.Radius()) >= 0.20:
                        props = GProp_GProps()
                        BRepGProp.SurfaceProperties_s(face, props)
                        if abs(float(props.Mass())) >= 1.0 and _is_cylinder_face_concave(face, cyl) is True:
                            return True
            except Exception:
                pass
            exp.Next()
    return False


def _try_preserve_simple_component(
    comp: PcbComponentGroup,
    bodies: list[PcbSubBody],
    obb_size: tuple[float, float, float],
    cfg: PcbSimplifyConfig,
    color: Optional[RGBColor],
) -> Optional[PcbSimplifiedPart]:
    """Preserve & unify/fuse low-complexity non-box components without coarse box/cylinder loss.

    Examples:
      - Flanged terminals with center blind holes (e.g. Terminal_L10, 15 -> 9 faces)
      - Round mounting standoffs with center M3 holes (e.g. TH-1.6-5.0-M3, 15 -> 9 faces)
      - Low-face pin headers with housing + pins (e.g. HWHP-2P, 32 faces)
      - Low-face SMD electrolytic caps with chamfered square base & pins (e.g. 4 bodies, 35 -> 34 faces)
      - Bridge jumpers & raised power shunts (28..52 faces)

    Compact brick-like SMD chips (<= small_part_box_mm with high OBB fill >= 0.82 and no hole)
    and pure primitive cylinders are deliberately skipped so they still benefit from crisp
    primitive replacement and XCAF prototype instancing.
    """
    max_faces = int(cfg.preserve_simple_max_faces)
    if max_faces <= 0 or not bodies:
        return None

    total_faces = sum(b.face_count for b in bodies)
    if total_faces > max_faces or len(bodies) > 6:
        return None

    # Heavy BSpline/Sphere filleted SMD chips (e.g. 0402/0603/1206/ferrite beads) should simplify to boxes
    if _count_freeform_faces(bodies) > 6:
        return None

    obb_w, obb_d, obb_h = obb_size
    max_dim = max(obb_w, obb_d, obb_h)
    obb_vol = max(1e-6, obb_w * obb_d * obb_h)
    total_vol = sum(max(0.0, b.volume) for b in bodies)
    obb_fill = total_vol / obb_vol

    has_hole: Optional[bool] = None

    def _get_has_hole() -> bool:
        nonlocal has_hole
        if has_hole is None:
            has_hole = _bodies_have_significant_hole(bodies)
        return has_hole

    # Compact solid brick-like SMD chip (no hole, >= 82% of OBB filled) -> collapse to 6-face OBB box
    if max_dim <= cfg.small_part_box_mm and obb_fill >= 0.82 and not _get_has_hole():
        return None

    # Let pure primitive cylinders (without holes/chamfered bases) go to _try_simplify_as_cylinder
    # so they receive category="cylinder" and shared XCAF prototype instancing
    if cfg.detect_cylinders:
        if len(bodies) == 1 and total_faces <= 6 and not _get_has_hole():
            if _analyze_cylinder_candidate(bodies[0], cfg.cylinder_circularity_min) is not None:
                return None
        elif len(bodies) == 2 and total_faces <= 12 and not _get_has_hole():
            f_sorted = sorted(bodies, key=lambda b: b.face_count)
            if (
                f_sorted[0].face_count <= 6
                and f_sorted[1].face_count == 6
                and _analyze_cylinder_candidate(f_sorted[0], cfg.cylinder_circularity_min) is not None
            ):
                return None

    # Build clean 1-solid preserved shape
    if len(bodies) == 1:
        orig_shape = bodies[0].shape
        if _count_subshapes(orig_shape, TopAbs_SOLID) != 1:
            return None
        orig_vol = max(bodies[0].volume, 1e-6)
        final_shape = orig_shape
        try:
            unify = ShapeUpgrade_UnifySameDomain(orig_shape, True, True, False)
            unify.Build()
            unified = unify.Shape()
            if (
                not unified.IsNull()
                and _count_subshapes(unified, TopAbs_SOLID) == 1
                and BRepCheck_Analyzer(unified).IsValid()
            ):
                u_vol = _shape_volume(unified)
                if 0.98 * orig_vol <= u_vol <= 1.02 * orig_vol:
                    final_shape = unified
        except Exception:
            pass
    else:
        fused = _fuse_primitives_to_single_solid(
            [b.shape for b in bodies],
            fuzzy_values=(0.0, 1e-3, 5e-3, 2e-2),
        )
        if fused is None or _count_subshapes(fused, TopAbs_SOLID) != 1:
            return None
        if not BRepCheck_Analyzer(fused).IsValid():
            return None
        main_vol = max(b.volume for b in bodies)
        fused_vol = _shape_volume(fused)
        if fused_vol < 0.90 * main_vol or fused_vol > max(1.10 * total_vol, main_vol * 1.10):
            return None
        final_shape = fused

    return PcbSimplifiedPart(
        name=comp.name,
        shape=final_shape,
        color=color if cfg.preserve_colors else None,
        category="preserved",
        ref_des=comp.ref_des,
        leaf_name=comp.leaf_name,
        original_bodies=len(comp.bodies),
        original_faces=comp.total_faces,
        simplified_faces=_count_subshapes(final_shape, TopAbs_FACE),
        solid_count=1,
    )


# ---------------------------------------------------------------------------
# Hexagonal Standoff / Post Detection & Simplification
# ---------------------------------------------------------------------------

def _detect_hex_prism(body: PcbSubBody) -> Optional[dict[str, Any]]:
    """Detect a regular hexagonal prism aligned with X, Y, or Z in a single face pass."""
    bsz = body.bbox_size
    if float(np.min(bsz)) < 0.70 or body.face_count < 8:
        return None
    bbox_vol = max(1e-6, float(bsz[0] * bsz[1] * bsz[2]))
    # A solid hexagon fills at most ~86.6% of its AABB (much less with a screw hole);
    # skip dense rectangular bricks (>= 90% AABB fill) immediately.
    if body.volume / bbox_vol > 0.90:
        return None

    total_area = float(body.surface_area)
    if total_area <= 1e-6:
        return None

    # Collect planar faces perpendicular to Z (2), X (0), or Y (1) in ONE pass
    perp_by_axis: dict[int, list[tuple[float, float, float, float, float, float, float]]] = {
        2: [],
        0: [],
        1: [],
    }
    uv_map = {0: (1, 2), 1: (2, 0), 2: (0, 1)}

    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        try:
            surf = BRepAdaptor_Surface(face, False)
            if surf.GetType() == GeomAbs_Plane:
                res = _face_outward_normal_at_mid(face)
                if res is not None:
                    pt, n = res
                    # Check which principal axis (if any) this plane is parallel to (|n[axis]| < 0.05)
                    matched_axes = [ax for ax in (2, 0, 1) if abs(float(n[ax])) < 0.05]
                    if matched_axes:
                        props = GProp_GProps()
                        BRepGProp.SurfaceProperties_s(face, props)
                        area = abs(float(props.Mass()))
                        if area >= 0.05:
                            fmin, fmax = _shape_bbox(face)
                            for ax in matched_axes:
                                u_idx, v_idx = uv_map[ax]
                                nu, nv = float(n[u_idx]), float(n[v_idx])
                                plen = math.hypot(nu, nv)
                                if plen > 0.95:
                                    nu /= plen
                                    nv /= plen
                                    ang = math.atan2(nv, nu) % (2.0 * math.pi)
                                    dist = nu * float(pt[u_idx]) + nv * float(pt[v_idx])
                                    perp_by_axis[ax].append(
                                        (ang, nu, nv, dist, area, float(fmin[ax]), float(fmax[ax]))
                                    )
        except Exception:
            pass
        exp.Next()

    pts_cache: Optional[np.ndarray] = None
    best_hex: Optional[dict[str, Any]] = None

    for axis_idx in (2, 0, 1):
        perp_planes = perp_by_axis[axis_idx]
        if len(perp_planes) < 6:
            continue

        # Cluster coplanar side faces (same outward normal angle within 3.5 deg & offset within 0.08 mm)
        clusters: list[dict[str, float]] = []
        for ang, nu, nv, dist, area, t0, t1 in sorted(perp_planes, key=lambda x: x[4], reverse=True):
            placed = False
            for cl in clusters:
                da = abs(((ang - cl["ang"] + math.pi) % (2.0 * math.pi)) - math.pi)
                if da < math.radians(3.5) and abs(dist - cl["dist"]) < 0.08:
                    tot = cl["area"] + area
                    cl["dist"] = (cl["dist"] * cl["area"] + dist * area) / tot
                    cl["area"] = tot
                    cl["t0"] = min(cl["t0"], t0)
                    cl["t1"] = max(cl["t1"], t1)
                    placed = True
                    break
            if not placed:
                clusters.append(
                    {"ang": ang, "nu": nu, "nv": nv, "dist": dist, "area": area, "t0": t0, "t1": t1}
                )

        if len(clusters) < 6:
            continue

        clusters.sort(key=lambda c: c["area"], reverse=True)
        top6 = clusters[:6]
        sum6_area = sum(c["area"] for c in top6)
        all_perp_area = sum(c["area"] for c in clusters)
        min6_area = min(c["area"] for c in top6)
        max6_area = max(c["area"] for c in top6)

        if max6_area <= 1e-6 or (min6_area / max6_area) < 0.40:
            continue
        if (sum6_area / total_area) < 0.25 or (sum6_area / max(1e-6, all_perp_area)) < 0.55:
            continue

        # Check 60-degree regular hexagonal angular spacing
        top6.sort(key=lambda c: c["ang"])
        angs_deg = [math.degrees(c["ang"]) for c in top6]
        diffs = [(angs_deg[(i + 1) % 6] - angs_deg[i]) % 360.0 for i in range(6)]
        if any(abs(d - 60.0) > 5.0 for d in diffs):
            continue

        # Least-squares fit for center (uc, vc) and inradius r_in: nu*uc + nv*vc + r_in = dist
        A = np.array([[c["nu"], c["nv"], 1.0] for c in top6], dtype=np.float64)
        rhs = np.array([c["dist"] for c in top6], dtype=np.float64)
        sol, _, _, _ = np.linalg.lstsq(A, rhs, rcond=None)
        uc, vc, r_in = float(sol[0]), float(sol[1]), float(sol[2])
        if r_in < 0.50:
            continue
        max_err = float(np.max(np.abs(A @ sol - rhs)))
        if max_err > max(0.08, 0.05 * r_in):
            continue

        r_hex = r_in / math.cos(math.pi / 6.0)
        t_hex0 = min(c["t0"] for c in top6)
        t_hex1 = max(c["t1"] for c in top6)
        if (t_hex1 - t_hex0) < 0.40:
            continue

        # Verify body vertices within the hex axial span stay inside ~1.22 * R_hex
        u_idx, v_idx = uv_map[axis_idx]
        if pts_cache is None:
            pts_cache = _collect_shape_points(body.shape)
        t_pts = pts_cache[:, axis_idx]
        in_span = (t_pts >= t_hex0 - 0.05) & (t_pts <= t_hex1 + 0.05)
        if np.any(in_span):
            du = pts_cache[in_span, u_idx] - uc
            dv = pts_cache[in_span, v_idx] - vc
            max_rad = float(np.max(np.hypot(du, dv)))
            if max_rad > r_hex * 1.22:
                continue

        axis_vec = np.zeros(3, dtype=np.float64)
        axis_vec[axis_idx] = 1.0
        cand = {
            "axis_idx": axis_idx,
            "axis_vec": axis_vec,
            "u_idx": u_idx,
            "v_idx": v_idx,
            "uc": uc,
            "vc": vc,
            "r_in": r_in,
            "r_hex": r_hex,
            "phi0": top6[0]["ang"] + math.pi / 6.0,
            "t_hex0": t_hex0,
            "t_hex1": t_hex1,
            "score": sum6_area / total_area,
        }
        if best_hex is None or cand["score"] > best_hex["score"]:
            best_hex = cand
            if axis_idx == 2:
                break

    return best_hex


def _try_simplify_as_hex_post(
    comp: PcbComponentGroup,
    bodies: list[PcbSubBody],
    cfg: PcbSimplifyConfig,
    color: RGBColor,
) -> Optional[PcbSimplifiedPart]:
    """Simplify hexagonal standoffs/posts into a clean hex prism (+ coaxial pilot & center hole)."""
    if not cfg.detect_hex_posts or not bodies:
        return None

    main_body = max(bodies, key=lambda b: (b.volume, b.surface_area))
    best_hex = _detect_hex_prism(main_body)
    if best_hex is None:
        return None

    axis_idx: int = best_hex["axis_idx"]
    axis_vec: np.ndarray = best_hex["axis_vec"]
    u_idx: int = best_hex["u_idx"]
    v_idx: int = best_hex["v_idx"]
    uc: float = best_hex["uc"]
    vc: float = best_hex["vc"]
    r_in: float = best_hex["r_in"]
    r_hex: float = best_hex["r_hex"]
    phi0: float = best_hex["phi0"]
    t_hex0: float = best_hex["t_hex0"]
    t_hex1: float = best_hex["t_hex1"]

    part_t0 = float(min(b.bbox_min[axis_idx] for b in bodies))
    part_t1 = float(max(b.bbox_max[axis_idx] for b in bodies))
    total_h = max(0.2, part_t1 - part_t0)

    # Local frame origin at (uc, vc, part_t0) along (u_dir, v_dir, axis_vec)
    axis_pt = np.zeros(3, dtype=np.float64)
    axis_pt[u_idx] = uc
    axis_pt[v_idx] = vc
    axis_pt[axis_idx] = 0.0

    outer_cyls, inner_holes = _collect_coaxial_cylinders(
        [b.shape for b in bodies],
        axis_pt,
        axis_vec,
        axis_tol=max(0.15, 0.12 * r_in),
    )

    def _local_pt(u: float, v: float, t_rel: float) -> gp_Pnt:
        if axis_idx == 2:
            return gp_Pnt(u, v, t_rel)
        coords = [0.0, 0.0, 0.0]
        coords[u_idx] = uc + u
        coords[v_idx] = vc + v
        coords[axis_idx] = part_t0 + t_rel
        return gp_Pnt(coords[0], coords[1], coords[2])

    def _local_dir() -> gp_Dir:
        return gp_Dir(float(axis_vec[0]), float(axis_vec[1]), float(axis_vec[2]))

    # 1. Build regular hexagonal prism
    poly = BRepBuilderAPI_MakePolygon()
    hex_base_rel = t_hex0 - part_t0
    for k in range(6):
        th = phi0 + k * (math.pi / 3.0)
        poly.Add(_local_pt(r_hex * math.cos(th), r_hex * math.sin(th), hex_base_rel))
    poly.Close()
    hex_face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    hex_vec = gp_Vec(
        float(axis_vec[0] * (t_hex1 - t_hex0)),
        float(axis_vec[1] * (t_hex1 - t_hex0)),
        float(axis_vec[2] * (t_hex1 - t_hex0)),
    )
    solid = BRepPrimAPI_MakePrism(hex_face, hex_vec).Shape()

    # 2. Fuse significant coaxial cylindrical pilot / boss / flange steps extending past hex ends
    pilot_sig: list[tuple[float, float, float]] = []
    for pr, _parea, pt0, pt1 in outer_cyls:
        if pr < 0.25 or pr > r_hex * 1.60:
            continue
        if pt0 < t_hex0 - 0.10:
            p_bot = part_t0 if (pt0 - part_t0) <= max(0.45, 0.55 * pr) else pt0
            p_top = t_hex0 + 0.05
            if p_top - p_bot >= 0.15:
                cyl = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(_local_pt(0.0, 0.0, p_bot - part_t0), _local_dir()),
                    float(pr),
                    float(p_top - p_bot),
                ).Shape()
                fused = _fuse_primitives_to_single_solid([solid, cyl])
                if fused is not None:
                    solid = fused
                    pilot_sig.append((round(pr, 2), round(p_bot - part_t0, 2), round(p_top - part_t0, 2)))
        elif pt1 > t_hex1 + 0.10:
            p_bot = t_hex1 - 0.05
            p_top = part_t1 if (part_t1 - pt1) <= max(0.45, 0.55 * pr) else pt1
            if p_top - p_bot >= 0.15:
                cyl = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(_local_pt(0.0, 0.0, p_bot - part_t0), _local_dir()),
                    float(pr),
                    float(p_top - p_bot),
                ).Shape()
                fused = _fuse_primitives_to_single_solid([solid, cyl])
                if fused is not None:
                    solid = fused
                    pilot_sig.append((round(pr, 2), round(p_bot - part_t0, 2), round(p_top - part_t0, 2)))

    # 3. Cut main coaxial central threaded/screw hole if present
    hole_sig: Optional[tuple[float, float, float]] = None
    if inner_holes:
        hr, _harea, hz0, hz1 = inner_holes[0]
        if 0.20 <= hr < r_in * 0.92 and (hz1 - hz0) >= 0.25 * total_h:
            cut_t0 = (part_t0 - 0.20) if (hz0 - part_t0) <= max(0.45, 0.85 * hr) else hz0
            cut_t1 = (part_t1 + 0.20) if (part_t1 - hz1) <= max(0.45, 0.85 * hr) else hz1
            hole_cyl = BRepPrimAPI_MakeCylinder(
                gp_Ax2(_local_pt(0.0, 0.0, cut_t0 - part_t0), _local_dir()),
                float(hr),
                float(cut_t1 - cut_t0),
            ).Shape()
            try:
                cut = BRepAlgoAPI_Cut(solid, hole_cyl)
                cut.SetRunParallel(True)
                cut.Build()
                if cut.IsDone():
                    cand = cut.Shape()
                    if not cand.IsNull() and _count_subshapes(cand, TopAbs_SOLID) == 1:
                        solid = cand
                        hole_sig = (round(hr, 2), round(cut_t0 - part_t0, 2), round(cut_t1 - part_t0, 2))
            except Exception:
                pass

    try:
        unify = ShapeUpgrade_UnifySameDomain(solid, True, True, False)
        unify.Build()
        unified = unify.Shape()
        if not unified.IsNull() and _count_subshapes(unified, TopAbs_SOLID) == 1:
            solid = unified
    except Exception:
        pass

    if _count_subshapes(solid, TopAbs_SOLID) != 1 or not BRepCheck_Analyzer(solid).IsValid():
        return None

    # Use shared XCAF prototype instancing for canonical vertical Z-axis hex posts
    proto_key: Optional[tuple] = None
    proto_shape: Any = None
    placement_trsf: Any = None
    if axis_idx == 2:
        proto_shape = solid
        placement_trsf = gp_Trsf()
        placement_trsf.SetTranslation(gp_Vec(uc, vc, part_t0))
        final_shape = proto_shape.Located(TopLoc_Location(placement_trsf))
        col_key = tuple(int(round(c * 255)) for c in color) if cfg.preserve_colors else None
        phi_mod60 = round(math.degrees(phi0) % 60.0, 1)
        proto_key = (
            "hex",
            comp.leaf_name or "",
            round(r_in, 2),
            phi_mod60,
            round(t_hex0 - part_t0, 2),
            round(t_hex1 - part_t0, 2),
            tuple(pilot_sig),
            hole_sig,
            col_key,
        )
    else:
        final_shape = solid

    return PcbSimplifiedPart(
        name=comp.name,
        shape=final_shape,
        color=color if cfg.preserve_colors else None,
        category="hex_post",
        ref_des=comp.ref_des,
        leaf_name=comp.leaf_name,
        original_bodies=len(comp.bodies),
        original_faces=comp.total_faces,
        simplified_faces=_count_subshapes(final_shape, TopAbs_FACE),
        solid_count=1,
        proto_key=proto_key,
        proto_shape=proto_shape,
        placement_trsf=placement_trsf,
    )


# ---------------------------------------------------------------------------
# Cylindrical Component Detection & Simplification (Requirement 6)
# ---------------------------------------------------------------------------

def _analyze_cylinder_candidate(
    body: PcbSubBody,
    circularity_min: float,
) -> Optional[tuple[np.ndarray, np.ndarray, float, float, float]]:
    """If body is predominantly cylindrical, return (axis_origin, axis_dir, radius, t_min, t_max)."""
    bsz = body.bbox_size
    max_dim = float(np.max(bsz))
    if max_dim < 0.5:
        return None

    # Gather OUTER (convex) cylindrical faces weighted by lateral area (ignore internal bores).
    # Reuse cached body.surface_area instead of integrating every planar/BSpline/sphere face.
    total_area = float(body.surface_area)
    if total_area <= 1e-6:
        return None
    cyl_faces: list[tuple[np.ndarray, np.ndarray, float, float]] = []  # (origin, dir, radius, area)
    min_dim = min(float(bsz[0]), float(bsz[1]), float(bsz[2]))
    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        try:
            surf = BRepAdaptor_Surface(face, False)
            if surf.GetType() == GeomAbs_Cylinder:
                cyl = surf.Cylinder()
                r = float(cyl.Radius())
                if r >= 0.25 * min_dim or r >= 0.18 * max_dim:
                    if _is_cylinder_face_concave(face, cyl) is True:
                        exp.Next()
                        continue
                    props = GProp_GProps()
                    BRepGProp.SurfaceProperties_s(face, props)
                    area = abs(float(props.Mass()))
                    if area > 1e-6:
                        loc = cyl.Location()
                        d = cyl.Axis().Direction()
                        cyl_faces.append(
                            (
                                np.array([loc.X(), loc.Y(), loc.Z()], dtype=np.float64),
                                np.array([d.X(), d.Y(), d.Z()], dtype=np.float64),
                                r,
                                area,
                            )
                        )
        except Exception:
            pass
        exp.Next()

    if not cyl_faces:
        return None

    # Find dominant cylinder radius & axis direction (grouping coaxial cylinders)
    cyl_faces.sort(key=lambda x: x[3], reverse=True)
    dom_orig, dom_dir, dom_r, _ = cyl_faces[0]
    # Canonicalize axis direction so largest component is positive
    dom_dir = dom_dir / max(1e-12, float(np.linalg.norm(dom_dir)))
    if dom_dir[int(np.argmax(np.abs(dom_dir)))] < 0:
        dom_dir = -dom_dir

    # Sum areas of faces aligned with dominant axis and similar radius (outer cylindrical wall)
    aligned_area = 0.0
    weighted_r = 0.0
    weighted_orig = np.zeros(3, dtype=np.float64)
    max_coaxial_r = dom_r
    for orig, d, r, area in cyl_faces:
        if abs(float(np.dot(d, dom_dir))) > 0.98:
            off = orig - dom_orig
            perp_off = float(np.linalg.norm(off - float(np.dot(off, dom_dir)) * dom_dir))
            if perp_off <= max(0.15, 0.10 * dom_r):
                max_coaxial_r = max(max_coaxial_r, r)
            if abs(r - dom_r) <= max(0.15, 0.12 * dom_r):
                aligned_area += area
                weighted_r += r * area
                weighted_orig += orig * area

    if aligned_area <= 0.0:
        return None
    radius = weighted_r / aligned_area
    axis_pt = weighted_orig / aligned_area

    # Project body points onto the dominant axis to get exact axial height & radial spread
    pts = _collect_shape_points(body.shape)
    rel = pts - axis_pt
    t_vals = rel @ dom_dir
    t_min, t_max = float(np.min(t_vals)), float(np.max(t_vals))
    height = t_max - t_min
    if height < 0.25 or radius < 0.25:
        return None

    # Check radial bounding box perpendicular to axis is circular (allow coaxial cylindrical flange)
    perp = rel - np.outer(t_vals, dom_dir)
    radii = np.linalg.norm(perp, axis=1)
    max_r = float(np.max(radii))
    allowed_r = max(radius, max_coaxial_r) * 1.28
    if max_r > allowed_r:
        # Significant non-cylindrical protrusion (e.g. square IC with a central round mark)
        return None

    # Expected lateral surface area of a full cylinder of (radius, height)
    full_cyl_lat = 2.0 * math.pi * radius * height
    full_cyl_vol = math.pi * radius * radius * height
    lat_coverage = aligned_area / max(1e-6, full_cyl_lat)
    cyl_area_ratio = aligned_area / total_area
    vol_ratio = body.volume / max(1e-6, full_cyl_vol)

    # Must genuinely be a cylindrical body (not a square box with rounded corners/fillets!)
    min_lat_cov = max(0.45, circularity_min - 0.22)
    if lat_coverage >= min_lat_cov and cyl_area_ratio >= 0.28 and 0.40 <= vol_ratio <= 1.35:
        return (axis_pt, dom_dir, radius, t_min, t_max)

    return None


def _try_simplify_as_cylinder(
    comp: PcbComponentGroup,
    bodies: list[PcbSubBody],
    cfg: PcbSimplifyConfig,
    color: RGBColor,
) -> Optional[PcbSimplifiedPart]:
    """Attempt to simplify a cylindrical component (electrolytic cap, can, radial inductor, turned post)."""
    if not cfg.detect_cylinders or not bodies:
        return None

    # Find the main cylindrical body (largest-volume body, or tallest body if SMD cap base is heavy)
    candidates = sorted(bodies, key=lambda b: (b.volume, b.bbox_size[2]), reverse=True)[:2]
    cyl_info = None
    cyl_body = None
    for cand in candidates:
        info = _analyze_cylinder_candidate(cand, cfg.cylinder_circularity_min)
        if info is not None:
            cyl_info = info
            cyl_body = cand
            break

    if cyl_info is None or cyl_body is None:
        return None

    axis_pt, axis_dir, radius, t_min, t_max = cyl_info
    cyl_h = max(0.1, t_max - t_min)

    # Check coaxial outer flange/pilot steps and internal center hole on the cylindrical body
    outer_cyls, inner_holes = _collect_coaxial_cylinders(
        [cyl_body.shape],
        axis_pt,
        axis_dir,
        axis_tol=max(0.15, 0.12 * radius),
    )
    extra_steps = [
        (sr, sarea, st0, st1)
        for (sr, sarea, st0, st1) in outer_cyls
        if abs(sr - radius) > max(0.20, 0.12 * radius) and sr >= 0.30 and (st1 - st0) >= 0.25
    ]
    main_hole: Optional[tuple[float, float, float]] = None
    if inner_holes:
        hr, _harea, ht0, ht1 = inner_holes[0]
        if 0.25 <= hr < radius * 0.90 and (ht1 - ht0) >= 0.25 * cyl_h:
            main_hole = (hr, ht0, ht1)

    # Check if there is a flat square/rectangular base plate under a vertical can (SMD electrolytic cap)
    base_bodies: list[PcbSubBody] = []
    if abs(float(axis_dir[2])) > 0.95 and len(bodies) > 1:
        comp_z_span = float(comp.bbox_max[2] - comp.bbox_min[2])
        can_area = math.pi * radius * radius
        for b in bodies:
            if b is cyl_body:
                continue
            bsz = b.bbox_size
            is_flat_base = float(bsz[2]) <= max(2.5, 0.40 * comp_z_span)
            near_bottom = float(b.bbox_min[2]) <= float(cyl_body.bbox_min[2]) + 0.5
            if not (is_flat_base and near_bottom):
                continue
            # Square SMD base corners protrude radially beyond the circular can even when width ~= 2*r
            b_pts = _collect_shape_points(b.shape)
            max_rad = float(np.max(np.hypot(b_pts[:, 0] - axis_pt[0], b_pts[:, 1] - axis_pt[1])))
            base_xy_area = float(bsz[0]) * float(bsz[1])
            is_wider = max(float(bsz[0]), float(bsz[1])) > (2.0 * radius * 1.08)
            is_square_base = (max_rad > radius * 1.04) and (base_xy_area >= 0.50 * can_area)
            if is_wider or is_square_base:
                base_bodies.append(b)

    # Build the cylinder solid (using prototype instancing when pure vertical cylinder)
    start_pt = axis_pt + axis_dir * t_min
    cyl_height = cyl_h
    proto_key: Optional[tuple] = None
    proto_shape: Any = None
    placement_trsf: Any = None

    has_extra_features = bool(base_bodies or extra_steps or main_hole)

    if not has_extra_features and abs(float(axis_dir[2])) > 0.995:
        # Canonical vertical cylinder at origin + translation -> shared XCAF prototype
        proto_shape = BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)),
            float(radius),
            float(cyl_height),
        ).Shape()
        placement_trsf = gp_Trsf()
        placement_trsf.SetTranslation(gp_Vec(float(start_pt[0]), float(start_pt[1]), float(start_pt[2])))
        final_shape = proto_shape.Located(TopLoc_Location(placement_trsf))
        col_key = tuple(int(round(c * 255)) for c in color) if cfg.preserve_colors else None
        proto_key = ("cyl", comp.leaf_name or "", round(float(radius), 2), round(float(cyl_height), 2), col_key)
    else:
        primitives: list[TopoDS_Shape] = []
        if base_bodies:
            # Compute local OBB box for the square base and fuse with cylinder (with 0.05 mm overlap)
            base_pts = np.vstack([_collect_shape_points(b.shape) for b in base_bodies])
            bcx, bcy, bw, bd, bang = _min_area_rect_2d(base_pts[:, :2])
            bz0 = float(np.min([b.bbox_min[2] for b in base_bodies]))
            bz1 = float(np.max([b.bbox_max[2] for b in base_bodies]))
            base_box = _make_local_box(
                -0.5 * bw, 0.5 * bw, -0.5 * bd, 0.5 * bd, bz0, bz1, (bcx, bcy), bang
            )
            primitives.append(base_box)
            cyl_z_bot = min(float(start_pt[2]), bz1 - 0.05)
            cyl_z_top = max(float(start_pt[2]) + cyl_h, bz1 + 0.1)
            start_pt = np.array([axis_pt[0], axis_pt[1], cyl_z_bot], dtype=np.float64)
            cyl_height = cyl_z_top - cyl_z_bot

        # When stepped coaxial flange/pilot exists, use dominant outer wall axial span for main cylinder
        main_t0, main_t1 = t_min, t_max
        if extra_steps and outer_cyls:
            dom_outer = [
                (sr, st0, st1)
                for (sr, _sa, st0, st1) in outer_cyls
                if abs(sr - radius) <= max(0.20, 0.12 * radius)
            ]
            if dom_outer:
                main_t0 = min(x[1] for x in dom_outer)
                main_t1 = max(x[2] for x in dom_outer)
                # Snap to ends if within chamfer distance
                if (main_t0 - t_min) <= max(0.35, 0.25 * radius):
                    main_t0 = t_min
                if (t_max - main_t1) <= max(0.35, 0.25 * radius):
                    main_t1 = t_max
                start_pt = axis_pt + axis_dir * main_t0
                cyl_height = max(0.1, main_t1 - main_t0)

        ax2 = gp_Ax2(
            gp_Pnt(float(start_pt[0]), float(start_pt[1]), float(start_pt[2])),
            gp_Dir(float(axis_dir[0]), float(axis_dir[1]), float(axis_dir[2])),
        )
        cyl_shape = BRepPrimAPI_MakeCylinder(ax2, float(radius), float(cyl_height)).Shape()
        primitives.append(cyl_shape)

        for sr, _sarea, st0, st1 in extra_steps:
            # Snap ends & add slight overlap into main cylinder for guaranteed fusion
            s_bot = t_min if (st0 - t_min) <= max(0.35, 0.25 * sr) else st0
            s_top = t_max if (t_max - st1) <= max(0.35, 0.25 * sr) else st1
            if s_top <= main_t0 + 0.05:
                s_top = min(main_t1, main_t0 + 0.08)
            elif s_bot >= main_t1 - 0.05:
                s_bot = max(main_t0, main_t1 - 0.08)
            if s_top - s_bot >= 0.15:
                sp = axis_pt + axis_dir * s_bot
                step_cyl = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(
                        gp_Pnt(float(sp[0]), float(sp[1]), float(sp[2])),
                        gp_Dir(float(axis_dir[0]), float(axis_dir[1]), float(axis_dir[2])),
                    ),
                    float(sr),
                    float(s_top - s_bot),
                ).Shape()
                primitives.append(step_cyl)

        fused = _fuse_primitives_to_single_solid(primitives) if len(primitives) > 1 else primitives[0]
        final_shape = fused if fused is not None else cyl_shape

        # Preserve significant coaxial central hole on hollow standoffs / terminals
        if main_hole is not None:
            hr, ht0, ht1 = main_hole
            cut_t0 = (t_min - 0.20) if (ht0 - t_min) <= max(0.45, 0.85 * hr) else ht0
            cut_t1 = (t_max + 0.20) if (t_max - ht1) <= max(0.45, 0.85 * hr) else ht1
            hp = axis_pt + axis_dir * cut_t0
            hole_cyl = BRepPrimAPI_MakeCylinder(
                gp_Ax2(
                    gp_Pnt(float(hp[0]), float(hp[1]), float(hp[2])),
                    gp_Dir(float(axis_dir[0]), float(axis_dir[1]), float(axis_dir[2])),
                ),
                float(hr),
                float(cut_t1 - cut_t0),
            ).Shape()
            try:
                cut = BRepAlgoAPI_Cut(final_shape, hole_cyl)
                cut.SetRunParallel(True)
                cut.Build()
                if cut.IsDone():
                    cand = cut.Shape()
                    if not cand.IsNull() and _count_subshapes(cand, TopAbs_SOLID) == 1:
                        final_shape = cand
            except Exception:
                pass

    return PcbSimplifiedPart(
        name=comp.name,
        shape=final_shape,
        color=color,
        category="cylinder",
        ref_des=comp.ref_des,
        leaf_name=comp.leaf_name,
        original_bodies=len(comp.bodies),
        original_faces=comp.total_faces,
        simplified_faces=_count_subshapes(final_shape, TopAbs_FACE),
        solid_count=max(1, _count_subshapes(final_shape, TopAbs_SOLID)),
        proto_key=proto_key,
        proto_shape=proto_shape,
        placement_trsf=placement_trsf,
    )


# ---------------------------------------------------------------------------
# Multi-Body IC / Connector Simplification (Requirements 3, 4, 5)
# ---------------------------------------------------------------------------

def _split_1d_clusters(
    items: list[tuple[float, tuple[float, float, float, float, float, float]]],
    gap_threshold: float,
) -> list[list[tuple[float, float, float, float, float, float]]]:
    """Split a list of (coord, box) entries along a bank axis whenever a wide empty gap occurs."""
    if not items:
        return []
    items_sorted = sorted(items, key=lambda x: x[0])
    clusters: list[list[tuple[float, float, float, float, float, float]]] = [[items_sorted[0][1]]]
    prev_c = items_sorted[0][0]
    for coord, box in items_sorted[1:]:
        if coord - prev_c > gap_threshold:
            clusters.append([box])
        else:
            clusters[-1].append(box)
        prev_c = coord
    return clusters


def _simplify_multi_body_ic_or_connector(
    comp: PcbComponentGroup,
    bodies: list[PcbSubBody],
    origin_xy: tuple[float, float],
    angle_rad: float,
    cfg: PcbSimplifyConfig,
    color: RGBColor,
) -> Optional[PcbSimplifiedPart]:
    """Merge body + heatsink/tab + grouped lead/pin bank boxes into 1 watertight solid."""
    if len(bodies) < 2 or not cfg.simplify_leads:
        return None

    main = max(bodies, key=lambda b: (b.volume, b.surface_area))
    main_vol = max(main.volume, 1e-9)
    mu0, mu1, mv0, mv1, mz0, mz1 = _world_to_local_bbox(main.shape, origin_xy, angle_rad)
    mw = max(0.1, mu1 - mu0)
    md = max(0.1, mv1 - mv0)
    mh = max(0.1, mz1 - mz0)

    # Separate other bodies into secondary major blocks (heatsink/tab/shroud) vs leads/pins
    major_boxes: list[tuple[float, float, float, float, float, float]] = []
    sector_leads: dict[str, list[tuple[float, tuple[float, float, float, float, float, float]]]] = {
        "left": [],
        "right": [],
        "front": [],
        "back": [],
        "bottom": [],
        "top": [],
    }

    overlap_eps = 0.06  # Small overlap into main body guarantees clean Boolean Fuse

    for b in bodies:
        if b is main:
            continue
        bu0, bu1, bv0, bv1, bz0, bz1 = _world_to_local_bbox(b.shape, origin_xy, angle_rad)
        # Skip sub-bodies completely enclosed inside main body
        if (
            bu0 >= mu0 - 0.03
            and bu1 <= mu1 + 0.03
            and bv0 >= mv0 - 0.03
            and bv1 <= mv1 + 0.03
            and bz0 >= mz0 - 0.03
            and bz1 <= mz1 + 0.03
        ):
            continue

        bw, bd, bh = bu1 - bu0, bv1 - bv0, bz1 - bz0
        is_major = (b.volume >= main_vol * 0.12) and (max(bw, bd, bh) >= 0.45 * min(mw, md))
        if is_major:
            # Clamp inward slightly so adjacent heatsink/tab cleanly overlaps main body
            major_boxes.append((bu0, bu1, bv0, bv1, bz0, bz1))
            continue

        # Assign peripheral lead/pin to the appropriate side bank of main body
        bc_u = 0.5 * (bu0 + bu1)
        bc_v = 0.5 * (bv0 + bv1)
        bc_z = 0.5 * (bz0 + bz1)
        box_tuple = (bu0, bu1, bv0, bv1, bz0, bz1)

        # Measure how far outside each face of main body the lead extends
        out_left = mu0 - bu0
        out_right = bu1 - mu1
        out_front = mv0 - bv0
        out_back = bv1 - mv1
        out_bot = mz0 - bz0
        out_top = bz1 - mz1

        best_side = max(
            ("left", out_left),
            ("right", out_right),
            ("front", out_front),
            ("back", out_back),
            ("bottom", out_bot),
            ("top", out_top),
            key=lambda x: x[1],
        )
        side_name, protrusion = best_side
        if protrusion <= 0.02:
            continue
        axis_coord = bc_v if side_name in ("left", "right") else bc_u
        sector_leads[side_name].append((axis_coord, box_tuple))

    # Build overlapping primitive boxes: main body + major blocks + grouped lead-bank boxes
    primitive_boxes: list[tuple[float, float, float, float, float, float]] = [
        (mu0, mu1, mv0, mv1, mz0, mz1)
    ]

    for (u0, u1, v0, v1, z0, z1) in major_boxes:
        # Ensure slight overlap with main body if touching/nearly touching
        if u1 <= mu0 + overlap_eps and mu0 - u1 <= 0.5:
            u1 = mu0 + overlap_eps
        elif u0 >= mu1 - overlap_eps and u0 - mu1 <= 0.5:
            u0 = mu1 - overlap_eps
        if v1 <= mv0 + overlap_eps and mv0 - v1 <= 0.5:
            v1 = mv0 + overlap_eps
        elif v0 >= mv1 - overlap_eps and v0 - mv1 <= 0.5:
            v0 = mv1 - overlap_eps
        if z1 <= mz0 + overlap_eps and mz0 - z1 <= 0.5:
            z1 = mz0 + overlap_eps
        elif z0 >= mz1 - overlap_eps and z0 - mz1 <= 0.5:
            z0 = mz1 - overlap_eps
        primitive_boxes.append((u0, u1, v0, v1, z0, z1))

    lead_bank_count = 0
    for side_name, items in sector_leads.items():
        if not items:
            continue
        span = md if side_name in ("left", "right") else mw
        gap_thr = max(1.5, 0.45 * span)
        clusters = _split_1d_clusters(items, gap_thr)
        for cl in clusters:
            cu0 = min(x[0] for x in cl)
            cu1 = max(x[1] for x in cl)
            cv0 = min(x[2] for x in cl)
            cv1 = max(x[3] for x in cl)
            cz0 = min(x[4] for x in cl)
            cz1 = max(x[5] for x in cl)

            # Pull inner boundary into main body by overlap_eps for rock-solid Boolean Fuse
            if side_name == "left":
                cu1 = max(cu1, mu0 + overlap_eps)
            elif side_name == "right":
                cu0 = min(cu0, mu1 - overlap_eps)
            elif side_name == "front":
                cv1 = max(cv1, mv0 + overlap_eps)
            elif side_name == "back":
                cv0 = min(cv0, mv1 - overlap_eps)
            elif side_name == "bottom":
                cz1 = max(cz1, mz0 + overlap_eps)
            elif side_name == "top":
                cz0 = min(cz0, mz1 - overlap_eps)

            # Ensure Z overlap with main body for side leads so they never float detached
            if side_name in ("left", "right", "front", "back"):
                if cz1 < mz0 + overlap_eps:
                    cz1 = mz0 + overlap_eps
                elif cz0 > mz1 - overlap_eps:
                    cz0 = mz1 - overlap_eps

            primitive_boxes.append((cu0, cu1, cv0, cv1, cz0, cz1))
            lead_bank_count += 1

    if len(primitive_boxes) <= 1:
        return None

    shapes = [
        _make_local_box(u0, u1, v0, v1, z0, z1, origin_xy, angle_rad)
        for (u0, u1, v0, v1, z0, z1) in primitive_boxes
    ]
    fused = _fuse_primitives_to_single_solid(shapes)
    if fused is None:
        return None

    return PcbSimplifiedPart(
        name=comp.name,
        shape=fused,
        color=color,
        category="ic_fused" if lead_bank_count > 0 else "connector_fused",
        ref_des=comp.ref_des,
        leaf_name=comp.leaf_name,
        original_bodies=len(comp.bodies),
        original_faces=comp.total_faces,
        simplified_faces=_count_subshapes(fused, TopAbs_FACE),
        solid_count=1,
    )


# ---------------------------------------------------------------------------
# Per-Component Dispatcher
# ---------------------------------------------------------------------------

def simplify_pcb_component(
    comp: PcbComponentGroup,
    cfg: PcbSimplifyConfig,
) -> tuple[PcbSimplifiedPart, int]:
    """Simplify one mounted PCB component into a single watertight solid with preserved color.

    Returns ``(simplified_part, removed_text_body_count)``.
    """
    bodies = list(comp.bodies)
    removed_text = 0
    if cfg.remove_text_features and len(bodies) > 1:
        bodies, removed_text = _filter_text_bodies(bodies)

    color = (
        _extract_representative_color(
            bodies,
            default_color=_DEFAULT_BODY_COLOR,
            min_brightness=cfg.min_color_brightness,
        )
        if cfg.preserve_colors
        else None
    )

    # 1. Compute component's tight 2D local OBB frame across ALL vertices (never truncated!)
    all_pts = np.vstack([_collect_shape_points(b.shape) for b in bodies])
    cx, cy, w, d, angle_rad = _min_area_rect_2d(all_pts[:, :2])
    z_min = float(np.min([b.bbox_min[2] for b in bodies]))
    z_max = float(np.max([b.bbox_max[2] for b in bodies]))
    h_obb = max(0.05, z_max - z_min)
    max_dim = max(w, d, h_obb)

    # 2. Preserve & fuse low-complexity non-box components (headers, flanged terminals,
    #    holed standoffs, low-face SMD caps with square base, jumpers, shunts)
    preserved_part = _try_preserve_simple_component(
        comp, bodies, (w, d, h_obb), cfg, color
    )
    if preserved_part is not None:
        return preserved_part, removed_text

    # 3. Simplify hexagonal standoffs/posts into clean hex prisms (+ pilot & center screw hole)
    hex_part = _try_simplify_as_hex_post(comp, bodies, cfg, color or _DEFAULT_BODY_COLOR)
    if hex_part is not None:
        if not cfg.preserve_colors:
            hex_part.color = None
        return hex_part, removed_text

    # 4. Try cylindrical simplification (electrolytic capacitors, round cans, turned standoffs)
    cyl_part = _try_simplify_as_cylinder(comp, bodies, cfg, color or _DEFAULT_BODY_COLOR)
    if cyl_part is not None:
        if not cfg.preserve_colors:
            cyl_part.color = None
        return cyl_part, removed_text

    # 5. For larger / multi-pin IC & connector components, fuse body + heatsink + lead-bank boxes
    is_multi_pin_candidate = (
        len(bodies) >= 2
        and (max_dim > cfg.small_part_box_mm or (len(bodies) >= 5 and max_dim >= 2.4))
    )
    if is_multi_pin_candidate:
        fused_part = _simplify_multi_body_ic_or_connector(
            comp, bodies, (cx, cy), angle_rad, cfg, color or _DEFAULT_BODY_COLOR
        )
        if fused_part is not None:
            if not cfg.preserve_colors:
                fused_part.color = None
            return fused_part, removed_text

    # 6. Compact SMD parts or guaranteed single-solid fallback -> tight local OBB rectangular box
    # Canonicalize (pw >= pd) so 0/90/180/270-deg rotated identical SMD parts share 1 XCAF prototype!
    pw, pd, pang = max(0.05, w), max(0.05, d), angle_rad
    if pd > pw:
        pw, pd = pd, pw
        pang = pang + 0.5 * math.pi
    ph = max(0.05, z_max - z_min)
    proto_box = BRepPrimAPI_MakeBox(gp_Pnt(-0.5 * pw, -0.5 * pd, 0.0), pw, pd, ph).Shape()
    trsf_rot = gp_Trsf()
    if abs(pang) > 1e-6:
        trsf_rot.SetRotation(gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), pang)
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(cx, cy, z_min))
    trsf.Multiply(trsf_rot)
    box_shape = proto_box.Located(TopLoc_Location(trsf))
    col_key = tuple(int(round(c * 255)) for c in color) if color is not None else None
    proto_key = ("box", comp.leaf_name or "", round(pw, 2), round(pd, 2), round(ph, 2), col_key)
    return (
        PcbSimplifiedPart(
            name=comp.name,
            shape=box_shape,
            color=color,
            category="box",
            ref_des=comp.ref_des,
            leaf_name=comp.leaf_name,
            original_bodies=len(comp.bodies),
            original_faces=comp.total_faces,
            simplified_faces=6,
            solid_count=1,
            proto_key=proto_key,
            proto_shape=proto_box,
            placement_trsf=trsf,
        ),
        removed_text,
    )


# ---------------------------------------------------------------------------
# Full PCB Assembly Pipeline
# ---------------------------------------------------------------------------

def simplify_pcb_assembly(
    assembly: PcbAssemblyModel,
    cfg: PcbSimplifyConfig,
    *,
    idf_data: Optional[IdfBoardData] = None,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> PcbSimplifyResult:
    """Simplify an entire Altium PCB STEP assembly (plus optional IDF supplementation)."""
    ensure_occt_parallelism_enabled()
    t0 = time.perf_counter()
    logs: list[str] = []

    components = list(assembly.components)
    orig_bodies = assembly.total_bodies
    orig_faces = assembly.total_faces

    if not components:
        raise ValueError("No valid 3D bodies found in PCB STEP assembly.")

    if progress_cb:
        progress_cb(0.05, "Detecting PCB board substrate and component groups...")

    board_indices = _detect_board_indices(components)
    if assembly.was_flat and len(components) > len(board_indices) + 1:
        components = _cluster_flat_components(components, board_indices)
        board_indices = _detect_board_indices(components)
        logs.append(
            f"Flat STEP detected: clustered into {len(components)} logical component group(s)."
        )

    heatsink_indices = _detect_heatsink_indices(components, board_indices)

    for idx in board_indices:
        components[idx].is_board = True
    for idx in heatsink_indices:
        components[idx].is_heatsink = True

    board_comps = [c for c in components if c.is_board]
    heatsink_comps = [c for c in components if c.is_heatsink]
    part_comps = [c for c in components if not c.is_board and not c.is_heatsink]
    logs.append(
        f"Detected {len(board_comps)} PCB board(s)"
        + (f", {len(heatsink_comps)} heatsink(s)" if heatsink_comps else "")
        + f", and {len(part_comps)} mounted component(s) "
        f"({orig_bodies} bodies, {orig_faces:,} faces)."
    )

    output_parts: list[PcbSimplifiedPart] = []
    board_count = 0
    heatsink_count = 0
    preserved_count = 0
    hex_post_count = 0
    cylinder_count = 0
    fused_count = 0
    box_count = 0
    excluded_small_count = 0
    removed_text_total = 0

    # 1. Clean PCB board substrate(s) — only apply hole deletion (< min_board_hole_mm)
    for b_idx, b_comp in enumerate(board_comps):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if progress_cb:
            progress_cb(0.10, f"Cleaning PCB board: {b_comp.name}")
        b_part = _simplify_board_component(b_comp, cfg)
        if not cfg.preserve_colors:
            b_part.color = None
        output_parts.append(b_part)
        board_count += 1

    # 1b. Preserve heatsink / thermal spreader component(s) intact
    step_footprints: list[StepComponentFootprint] = []
    step_idx_to_part: dict[int, PcbSimplifiedPart] = {}

    for h_comp in heatsink_comps:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if progress_cb:
            progress_cb(0.11, f"Preserving heatsink: {h_comp.name}")
        h_part = _simplify_heatsink_component(h_comp, cfg)
        if not cfg.preserve_colors:
            h_part.color = None
        fp_idx = len(step_footprints)
        step_footprints.append(
            StepComponentFootprint(
                name=h_comp.name,
                bbox_min=h_comp.bbox_min.copy(),
                bbox_max=h_comp.bbox_max.copy(),
                ref_des=h_comp.ref_des,
            )
        )
        step_idx_to_part[fp_idx] = h_part
        output_parts.append(h_part)
        heatsink_count += 1

    # 2. Simplify each mounted component into 1 watertight solid
    total_parts = max(1, len(part_comps))
    exclude_thr = max(0.0, float(cfg.exclude_small_part_mm))

    for i, comp in enumerate(part_comps):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if progress_cb and (i % 5 == 0 or i == total_parts - 1):
            frac = 0.12 + 0.78 * ((i + 1) / total_parts)
            progress_cb(frac, f"Simplifying component {i + 1}/{len(part_comps)}: {comp.name}")

        # Always register original STEP footprint so IDF never re-adds an intentionally excluded part
        fp_idx = len(step_footprints)
        step_footprints.append(
            StepComponentFootprint(
                name=comp.name,
                bbox_min=comp.bbox_min.copy(),
                bbox_max=comp.bbox_max.copy(),
                ref_des=comp.ref_des,
            )
        )

        # Exclude small components whose max dimension is <= exclude_small_part_mm
        if exclude_thr > 0.0:
            # Quick bbox pre-check, then exact OBB max dimension check for rotated parts
            if float(np.max(comp.bbox_size)) <= exclude_thr * 1.45:
                pts_chk = np.vstack([_collect_shape_points(b.shape) for b in comp.bodies])
                _cx, _cy, obb_w, obb_d, _ang = _min_area_rect_2d(pts_chk[:, :2])
                obb_h = float(comp.bbox_size[2])
                if max(obb_w, obb_d, obb_h) <= exclude_thr + 1e-6:
                    excluded_small_count += 1
                    continue

        simp_part, rm_txt = simplify_pcb_component(comp, cfg)
        removed_text_total += rm_txt
        if simp_part.category == "preserved":
            preserved_count += 1
        elif simp_part.category == "hex_post":
            hex_post_count += 1
        elif simp_part.category == "cylinder":
            cylinder_count += 1
        elif simp_part.category in ("ic_fused", "connector_fused"):
            fused_count += 1
        else:
            box_count += 1

        step_idx_to_part[fp_idx] = simp_part
        output_parts.append(simp_part)

    # 3. Optional IDF (.bdf/.ldf) missing component supplementation (NEVER replaces existing STEP parts,
    #    but IDF part names take priority over STEP names for matched parts!)
    idf_matched = 0
    idf_added = 0
    idf_renamed = 0
    if cfg.use_idf_supplement and idf_data is not None:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if progress_cb:
            progress_cb(0.92, "Matching IDF (.bdf/.ldf) and supplementing missing components...")

        if board_comps:
            bmin = np.min(np.vstack([b.bbox_min for b in board_comps]), axis=0)
            bmax = np.max(np.vstack([b.bbox_max for b in board_comps]), axis=0)
        else:
            bmin, bmax = assembly.bbox_min, assembly.bbox_max

        raw_idf_color: RGBColor = (
            tuple(float(x) for x in cfg.idf_default_color[:3])  # type: ignore[assignment]
            if len(cfg.idf_default_color) >= 3
            else (0.35, 0.35, 0.38)
        )
        idf_color = _ensure_min_brightness(raw_idf_color, cfg.min_color_brightness)
        added_parts, idf_matched, idf_added, matched_names = supplement_missing_idf_parts(
            idf_data,
            bmin,
            bmax,
            step_footprints,
            default_color=idf_color,
            exclude_small_part_mm=exclude_thr,
        )
        # Prioritize IDF component names on STEP components matched in IDF
        for fp_idx, idf_name in matched_names.items():
            part_obj = step_idx_to_part.get(fp_idx)
            if part_obj is not None and idf_name:
                part_obj.name = idf_name
                part_obj.leaf_name = ""
                idf_renamed += 1
        if not cfg.preserve_colors:
            for ap in added_parts:
                ap.color = None
        output_parts.extend(added_parts)
        logs.append(
            f"IDF ({idf_data.bdf_path.name if idf_data.bdf_path else 'IDF'}): "
            f"{idf_matched} matched in STEP ({idf_renamed} named from IDF), "
            f"{idf_added} missing component(s) added as boxes."
        )

    simp_faces = sum(p.simplified_faces for p in output_parts)
    elapsed = time.perf_counter() - t0
    face_pct = (1.0 - simp_faces / max(1, orig_faces)) * 100.0
    kept_step_comps = len(part_comps) - excluded_small_count
    cat_bits: list[str] = []
    if heatsink_count:
        cat_bits.append(f"{heatsink_count} heatsink(s)")
    if preserved_count:
        cat_bits.append(f"{preserved_count} preserved")
    if hex_post_count:
        cat_bits.append(f"{hex_post_count} hex post(s)")
    cat_bits.append(f"{cylinder_count} cylinder(s)")
    cat_bits.append(f"{fused_count} fused IC/connector(s)")
    cat_bits.append(f"{box_count} box(es)")
    logs.append(
        f"Simplified {kept_step_comps}/{len(part_comps)} STEP component(s) -> "
        + ", ".join(cat_bits)
        + (f", excluded {excluded_small_count} small part(s) <= {exclude_thr:g} mm" if excluded_small_count else "")
        + (f", +{idf_added} IDF box(es)" if idf_added else "")
        + (f", dropped {removed_text_total} text/marking body(ies)" if removed_text_total else "")
        + f" | Faces: {orig_faces:,} -> {simp_faces:,} (-{face_pct:.1f}%) in {elapsed:.2f}s"
    )

    if progress_cb:
        progress_cb(1.0, f"PCB simplification complete ({len(output_parts)} solids)")

    return PcbSimplifyResult(
        parts=output_parts,
        root_name=assembly.root_name or assembly.source_path.stem,
        unit=assembly.unit,
        board_count=board_count,
        heatsink_count=heatsink_count,
        component_count=kept_step_comps,
        preserved_count=preserved_count,
        hex_post_count=hex_post_count,
        cylinder_count=cylinder_count,
        fused_count=fused_count,
        box_count=box_count,
        idf_matched_count=idf_matched,
        idf_added_count=idf_added,
        excluded_small_count=excluded_small_count,
        removed_text_bodies=removed_text_total,
        original_bodies=orig_bodies,
        original_faces=orig_faces,
        simplified_faces=simp_faces,
        elapsed_seconds=elapsed,
        logs=logs,
    )


def simplify_pcb_from_idf(
    idf_data: IdfBoardData,
    cfg: PcbSimplifyConfig,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> PcbSimplifyResult:
    """Generate a simplified colored PCB STEP assembly purely from IDF (.bdf/.ldf) without a STEP file."""
    ensure_occt_parallelism_enabled()
    t0 = time.perf_counter()
    logs: list[str] = []

    if idf_data is None:
        raise ValueError("IDF board data is required to generate an IDF-only PCB model.")

    if cancel_check and cancel_check():
        raise InterruptedError("Operation cancelled by user.")

    if progress_cb:
        progress_cb(0.10, "Building PCB board solid from IDF outline...")

    output_parts: list[PcbSimplifiedPart] = []
    board_count = 0

    board_shape, z_bot, z_top, kept_holes, skipped_holes = build_idf_board_shape(
        idf_data,
        z_bottom=0.0,
        keep_board_holes=cfg.keep_board_holes,
        min_board_hole_mm=cfg.min_board_hole_mm,
    )
    if board_shape is not None:
        board_color: Optional[RGBColor] = (
            _ensure_min_brightness(_DEFAULT_BOARD_COLOR, cfg.min_color_brightness)
            if cfg.preserve_colors
            else None
        )
        board_faces = _count_subshapes(board_shape, TopAbs_FACE)
        board_solids = max(1, _count_subshapes(board_shape, TopAbs_SOLID))
        output_parts.append(
            PcbSimplifiedPart(
                name="PCB_Board",
                shape=board_shape,
                color=board_color,
                category="board",
                ref_des="PCB_Board",
                leaf_name="",
                original_bodies=0,
                original_faces=0,
                simplified_faces=board_faces,
                solid_count=board_solids,
            )
        )
        board_count = 1
        hole_note = ""
        if kept_holes or skipped_holes:
            hole_note = f", {kept_holes} hole(s)/cutout(s) kept"
            if skipped_holes:
                hole_note += f", {skipped_holes} skipped"
        logs.append(
            f"Built PCB board from IDF ({idf_data.bdf_path.name if idf_data.bdf_path else 'IDF'}): "
            f"thickness={z_top - z_bot:.2f} mm{hole_note}."
        )

    if cancel_check and cancel_check():
        raise InterruptedError("Operation cancelled by user.")

    if progress_cb:
        progress_cb(0.45, "Generating PCB components from IDF placements...")

    raw_idf_color: RGBColor = (
        tuple(float(x) for x in cfg.idf_default_color[:3])  # type: ignore[assignment]
        if len(cfg.idf_default_color) >= 3
        else (0.35, 0.35, 0.38)
    )
    comp_color: Optional[RGBColor] = (
        _ensure_min_brightness(raw_idf_color, cfg.min_color_brightness)
        if cfg.preserve_colors
        else None
    )

    alignment = IdfAlignment(
        dx=0.0,
        dy=0.0,
        rot_deg=0.0,
        board_z_bottom=z_bot,
        board_z_top=z_top,
    )
    exclude_thr = max(0.0, float(cfg.exclude_small_part_mm))
    comp_parts, cyl_count, excluded_small = build_idf_components(
        idf_data,
        alignment,
        default_color=comp_color,
        exclude_small_part_mm=exclude_thr,
        detect_cylinders=cfg.detect_cylinders,
    )
    output_parts.extend(comp_parts)

    if not output_parts:
        raise ValueError("IDF file contains neither a valid board outline nor placed components.")

    idf_box_count = len(comp_parts) - cyl_count
    simp_faces = sum(p.simplified_faces for p in output_parts)
    elapsed = time.perf_counter() - t0
    if idf_data.ldf_path is None:
        logs.append("Note: No IDF library file (.ldf/.pro/.emp) linked — using default component box sizes.")
    logs.append(
        f"Generated PCB from IDF-only: {board_count} board(s), "
        f"{len(comp_parts)} component(s) ({cyl_count} cylinder(s), {idf_box_count} box(es))"
        + (f", excluded {excluded_small} small part(s) <= {exclude_thr:g} mm" if excluded_small else "")
        + f" | Total faces: {simp_faces:,} in {elapsed:.2f}s"
    )

    if progress_cb:
        progress_cb(1.0, f"IDF-only PCB generation complete ({len(output_parts)} solids)")

    root_name = (
        (idf_data.board_name or "").strip()
        or (idf_data.bdf_path.stem if idf_data.bdf_path else "PCB_IDF")
    )
    return PcbSimplifyResult(
        parts=output_parts,
        root_name=root_name,
        unit="MM",
        board_count=board_count,
        heatsink_count=0,
        component_count=0,
        preserved_count=0,
        hex_post_count=0,
        cylinder_count=cyl_count,
        fused_count=0,
        box_count=0,
        idf_matched_count=0,
        idf_added_count=len(comp_parts),
        excluded_small_count=excluded_small,
        removed_text_bodies=0,
        original_bodies=0,
        original_faces=0,
        simplified_faces=simp_faces,
        elapsed_seconds=elapsed,
        logs=logs,
    )
