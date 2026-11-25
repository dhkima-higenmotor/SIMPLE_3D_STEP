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
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.GeomAbs import GeomAbs_BSplineSurface, GeomAbs_Cylinder, GeomAbs_Plane
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from scipy.spatial import ConvexHull

from simple_3d_step.core.idf_parser import (
    IdfBoardData,
    StepComponentFootprint,
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


_BOARD_NAME_RE = re.compile(r"\b(PCB|BOARD|SUBSTRATE|FR4|MAINBOARD|PWB)\b", re.IGNORECASE)
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


def _fuse_primitives_to_single_solid(shapes: list[TopoDS_Shape]) -> Optional[TopoDS_Shape]:
    """Fuse a small list of overlapping analytic primitives into 1 clean unified solid."""
    if not shapes:
        return None
    if len(shapes) == 1:
        return shapes[0]

    for fuzzy in (0.0, 1e-3, 5e-3):
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


def _detect_board_indices(components: list[PcbComponentGroup]) -> set[int]:
    """Identify the PCB substrate component(s) by thin-slab geometry and naming."""
    if not components:
        return set()

    xy_areas = [
        max(0.0, float(c.bbox_size[0]) * float(c.bbox_size[1])) if c.bodies else 0.0
        for c in components
    ]
    max_area = max(xy_areas) if xy_areas else 0.0
    if max_area <= 0.0:
        return set()

    board_indices: set[int] = set()
    best_idx: Optional[int] = None
    best_score = -1.0

    for idx, comp in enumerate(components):
        if not comp.bodies:
            continue
        sz = comp.bbox_size
        dx, dy, dz = float(sz[0]), float(sz[1]), float(sz[2])
        area_xy = dx * dy
        if area_xy < 0.35 * max_area:
            continue
        # PCB boards are thin planar slabs (typically 0.3 mm to 6.5 mm thick)
        if not (0.25 <= dz <= 6.5):
            continue
        aspect = max(dx, dy) / max(dz, 1e-3)
        if aspect < 5.0:
            continue
        score = area_xy / max_area
        if _BOARD_NAME_RE.search(comp.name):
            score += 0.5
        if score > best_score:
            best_score = score
            best_idx = idx
        # Also include multi-panel boards with nearly equal area and thickness
        if area_xy >= 0.5 * max_area and aspect >= 8.0:
            board_indices.add(idx)

    if best_idx is not None:
        board_indices.add(best_idx)
    return board_indices


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
    """Clean PCB board substrate: keep outline & mounting holes, drop tiny via holes & thin copper."""
    main_body = max(comp.bodies, key=lambda b: (b.volume, b.surface_area))
    board_color = _extract_representative_color(
        [main_body],
        default_color=_DEFAULT_BOARD_COLOR,
        min_brightness=cfg.min_color_brightness,
    )
    shape = main_body.shape
    orig_faces = comp.total_faces
    orig_vol = max(main_body.volume, 1e-6)

    # Plug small vertical cylindrical via holes (< min_board_hole_mm) while keeping mounting holes
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
                    if abs(float(axis_dir.Z())) > 0.95:
                        dia = 2.0 * float(cyl.Radius())
                        if 0.0 < dia < min_dia:
                            # Ensure this is an internal hole (radius small vs board, bbox inside board)
                            fmin, fmax = _shape_bbox(face)
                            margin = 0.2
                            if (
                                fmin[0] > main_body.bbox_min[0] + margin
                                and fmax[0] < main_body.bbox_max[0] - margin
                                and fmin[1] > main_body.bbox_min[1] + margin
                                and fmax[1] < main_body.bbox_max[1] - margin
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

    # Merge coplanar / co-cylindrical faces on the board
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
        leaf_name=comp.leaf_name or "Board",
        original_bodies=len(comp.bodies),
        original_faces=orig_faces,
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

    # Gather cylindrical faces weighted by approximate lateral area
    cyl_faces: list[tuple[np.ndarray, np.ndarray, float, float]] = []  # (origin, dir, radius, area)
    total_area = 0.0
    exp = TopExp_Explorer(body.shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = abs(float(props.Mass()))
        total_area += area
        try:
            surf = BRepAdaptor_Surface(face, False)
            if surf.GetType() == GeomAbs_Cylinder and area > 1e-6:
                cyl = surf.Cylinder()
                r = float(cyl.Radius())
                if r >= 0.25 * min(float(bsz[0]), float(bsz[1]), float(bsz[2])) or r >= 0.18 * max_dim:
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

    if not cyl_faces or total_area <= 1e-6:
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
    for orig, d, r, area in cyl_faces:
        if abs(float(np.dot(d, dom_dir))) > 0.98 and abs(r - dom_r) <= max(0.15, 0.12 * dom_r):
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

    # Check radial bounding box perpendicular to axis is circular (aspect ~ 1.0)
    perp = rel - np.outer(t_vals, dom_dir)
    radii = np.linalg.norm(perp, axis=1)
    max_r = float(np.max(radii))
    if max_r > radius * 1.28:
        # Significant non-cylindrical protrusion (e.g. square IC with a central round mark)
        return None

    # Expected lateral surface area of a full cylinder of (radius, height)
    full_cyl_lat = 2.0 * math.pi * radius * height
    full_cyl_vol = math.pi * radius * radius * height
    lat_coverage = aligned_area / max(1e-6, full_cyl_lat)
    cyl_area_ratio = aligned_area / total_area
    vol_ratio = body.volume / max(1e-6, full_cyl_vol)

    # Must genuinely be a cylindrical body (not a square box with rounded corners/fillets!)
    min_lat_cov = max(0.50, circularity_min - 0.18)
    if lat_coverage >= min_lat_cov and cyl_area_ratio >= 0.30 and 0.55 <= vol_ratio <= 1.20:
        return (axis_pt, dom_dir, radius, t_min, t_max)

    return None


def _try_simplify_as_cylinder(
    comp: PcbComponentGroup,
    bodies: list[PcbSubBody],
    cfg: PcbSimplifyConfig,
    color: RGBColor,
) -> Optional[PcbSimplifiedPart]:
    """Attempt to simplify a cylindrical component (electrolytic cap, can, radial inductor, etc.)."""
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

    # Check if there is a noticeably wider flat square base plate (e.g., SMD electrolytic capacitor)
    base_bodies: list[PcbSubBody] = []
    if abs(float(axis_dir[2])) > 0.95 and len(bodies) > 1:
        comp_z_span = float(comp.bbox_max[2] - comp.bbox_min[2])
        for b in bodies:
            if b is cyl_body:
                continue
            bsz = b.bbox_size
            # Flat base sitting near bottom of vertical can and noticeably wider than can diameter
            is_wider = max(float(bsz[0]), float(bsz[1])) > (2.0 * radius * 1.08)
            is_flat_base = float(bsz[2]) <= max(1.5, 0.40 * comp_z_span)
            near_bottom = float(b.bbox_min[2]) <= float(cyl_body.bbox_min[2]) + 0.5
            if is_wider and is_flat_base and near_bottom:
                base_bodies.append(b)

    # Build the cylinder solid (using prototype instancing when pure vertical cylinder)
    start_pt = axis_pt + axis_dir * t_min
    cyl_height = cyl_h
    proto_key: Optional[tuple] = None
    proto_shape: Any = None
    placement_trsf: Any = None

    if base_bodies:
        # Compute local OBB box for the square base and fuse with cylinder (with 0.05 mm overlap)
        base_pts = np.vstack([_collect_shape_points(b.shape) for b in base_bodies])
        bcx, bcy, bw, bd, bang = _min_area_rect_2d(base_pts[:, :2])
        bz0 = float(np.min([b.bbox_min[2] for b in base_bodies]))
        bz1 = float(np.max([b.bbox_max[2] for b in base_bodies]))
        base_box = _make_local_box(
            -0.5 * bw, 0.5 * bw, -0.5 * bd, 0.5 * bd, bz0, bz1, (bcx, bcy), bang
        )

        # Extend cylinder downward slightly into base_box for guaranteed solid fusion
        cyl_z_bot = min(float(start_pt[2]), bz1 - 0.05)
        cyl_z_top = max(float(start_pt[2]) + cyl_h, bz1 + 0.1)
        start_pt = np.array([axis_pt[0], axis_pt[1], cyl_z_bot], dtype=np.float64)
        cyl_height = cyl_z_top - cyl_z_bot
        ax2 = gp_Ax2(
            gp_Pnt(float(start_pt[0]), float(start_pt[1]), float(start_pt[2])),
            gp_Dir(float(axis_dir[0]), float(axis_dir[1]), float(axis_dir[2])),
        )
        cyl_shape = BRepPrimAPI_MakeCylinder(ax2, float(radius), float(cyl_height)).Shape()
        fused = _fuse_primitives_to_single_solid([base_box, cyl_shape])
        final_shape = fused if fused is not None else cyl_shape
    elif abs(float(axis_dir[2])) > 0.995:
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
        ax2 = gp_Ax2(
            gp_Pnt(float(start_pt[0]), float(start_pt[1]), float(start_pt[2])),
            gp_Dir(float(axis_dir[0]), float(axis_dir[1]), float(axis_dir[2])),
        )
        final_shape = BRepPrimAPI_MakeCylinder(ax2, float(radius), float(cyl_height)).Shape()

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

    # 1. Try cylindrical simplification first (electrolytic capacitors, round cans, standoffs)
    cyl_part = _try_simplify_as_cylinder(comp, bodies, cfg, color or _DEFAULT_BODY_COLOR)
    if cyl_part is not None:
        if not cfg.preserve_colors:
            cyl_part.color = None
        return cyl_part, removed_text

    # 2. Compute component's tight 2D local OBB frame across ALL vertices (never truncated!)
    all_pts = np.vstack([_collect_shape_points(b.shape) for b in bodies])
    cx, cy, w, d, angle_rad = _min_area_rect_2d(all_pts[:, :2])
    z_min = float(np.min([b.bbox_min[2] for b in bodies]))
    z_max = float(np.max([b.bbox_max[2] for b in bodies]))
    max_dim = max(w, d, z_max - z_min)

    # 3. For larger / multi-pin IC & connector components, fuse body + heatsink + lead-bank boxes
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

    # 4. Compact SMD parts or guaranteed single-solid fallback -> tight local OBB rectangular box
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

    for idx in board_indices:
        components[idx].is_board = True

    board_comps = [c for c in components if c.is_board]
    part_comps = [c for c in components if not c.is_board]
    logs.append(
        f"Detected {len(board_comps)} PCB board(s) and {len(part_comps)} mounted component(s) "
        f"({orig_bodies} bodies, {orig_faces:,} faces)."
    )

    output_parts: list[PcbSimplifiedPart] = []
    board_count = 0
    cylinder_count = 0
    fused_count = 0
    box_count = 0
    excluded_small_count = 0
    removed_text_total = 0

    # 1. Clean PCB board substrate(s)
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

    # 2. Simplify each mounted component into 1 watertight solid
    step_footprints: list[StepComponentFootprint] = []
    total_parts = max(1, len(part_comps))
    exclude_thr = max(0.0, float(cfg.exclude_small_part_mm))

    for i, comp in enumerate(part_comps):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if progress_cb and (i % 5 == 0 or i == total_parts - 1):
            frac = 0.12 + 0.78 * ((i + 1) / total_parts)
            progress_cb(frac, f"Simplifying component {i + 1}/{len(part_comps)}: {comp.name}")

        # Always register original STEP footprint so IDF never re-adds an intentionally excluded part
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
        if simp_part.category == "cylinder":
            cylinder_count += 1
        elif simp_part.category in ("ic_fused", "connector_fused"):
            fused_count += 1
        else:
            box_count += 1

        output_parts.append(simp_part)

    # 3. Optional IDF (.bdf/.ldf) missing component supplementation (NEVER replaces existing STEP parts)
    idf_matched = 0
    idf_added = 0
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
        added_parts, idf_matched, idf_added, _matched_names = supplement_missing_idf_parts(
            idf_data,
            bmin,
            bmax,
            step_footprints,
            default_color=idf_color,
            exclude_small_part_mm=exclude_thr,
        )
        if not cfg.preserve_colors:
            for ap in added_parts:
                ap.color = None
        output_parts.extend(added_parts)
        logs.append(
            f"IDF ({idf_data.bdf_path.name if idf_data.bdf_path else 'IDF'}): "
            f"{idf_matched} matched in STEP, {idf_added} missing component(s) added as boxes."
        )

    simp_faces = sum(p.simplified_faces for p in output_parts)
    elapsed = time.perf_counter() - t0
    face_pct = (1.0 - simp_faces / max(1, orig_faces)) * 100.0
    kept_step_comps = len(part_comps) - excluded_small_count
    logs.append(
        f"Simplified {kept_step_comps}/{len(part_comps)} STEP component(s) -> "
        f"{cylinder_count} cylinder(s), {fused_count} fused IC/connector(s), {box_count} box(es)"
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
        component_count=kept_step_comps,
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
