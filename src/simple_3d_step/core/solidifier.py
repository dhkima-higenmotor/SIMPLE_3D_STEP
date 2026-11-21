"""100% Exterior-Preserving Pure Exact B-Rep Solidification Engine.

Keeps ALL original exterior B-Rep surfaces (Planes, Cylinders, Cones, Toruses, NURBS)
completely intact without any voxel/triangle surface approximation.
Internal cavities and hidden internal features are eliminated by:
  1. Constructing a strictly-interior analytic B-Rep core (cylinders & planar prisms)
     safely inset behind the outer walls and visibility-carved away from external
     recesses/fins so it never alters exterior geometry.
  2. Performing precision-first exact B-Rep Boolean Fuse (starting at fuzzy=0.0 with
     strict volume preservation verification).
  3. Stripping all closed internal void shells via BRepClass3d.OuterShell_s + MakeSolid.
  4. Connecting any remaining clearance-separated external parts via hidden internal
     B-Rep bridges.
  5. Optimizing coplanar/co-cylindrical faces via ShapeUpgrade_UnifySameDomain.
"""

from __future__ import annotations

import math
import time
from typing import Optional

import numpy as np
import trimesh
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass3d import BRepClass3d, BRepClass3d_SolidClassifier
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.GeomAbs import GeomAbs_Cylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Solid
from OCP.ShapeUpgrade import (
    ShapeUpgrade_RemoveInternalWires,
    ShapeUpgrade_UnifySameDomain,
)
from OCP.TopAbs import (
    TopAbs_FACE,
    TopAbs_IN,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
)
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Compound, TopoDS_Face, TopoDS_Shape, TopoDS_Solid
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import approximate_polygon, find_contours

from simple_3d_step.core.occt_compat import (
    ShapeList,
    to_face,
    to_shell,
    to_solid,
)
from simple_3d_step.core.tessellation import tessellate_shape_triangles
from simple_3d_step.core.validator import validate_shape
from simple_3d_step.models import (
    AssemblyModel,
    CancelCheck,
    PartNode,
    ProgressCallback,
    SimplifyConfig,
    SimplifyResult,
)


def _shape_volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return float(props.Mass())


def _shape_bbox(shape: TopoDS_Shape) -> tuple[np.ndarray, np.ndarray]:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return np.zeros(3), np.zeros(3)
    pmin, pmax = box.CornerMin(), box.CornerMax()
    return (
        np.array([pmin.X(), pmin.Y(), pmin.Z()], dtype=np.float64),
        np.array([pmax.X(), pmax.Y(), pmax.Z()], dtype=np.float64),
    )


def _extract_solids(shape: TopoDS_Shape) -> list[TopoDS_Solid]:
    solids: list[TopoDS_Solid] = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        solids.append(to_solid(exp.Current()))
        exp.Next()
    return solids


def _count_shells(solid: TopoDS_Solid) -> int:
    cnt = 0
    exp = TopExp_Explorer(solid, TopAbs_SHELL)
    while exp.More():
        cnt += 1
        exp.Next()
    return cnt


def _heal_solid(shape: TopoDS_Shape) -> Optional[TopoDS_Solid]:
    """Ensure a TopoDS_Shape is a valid TopoDS_Solid."""
    if shape is None or shape.IsNull():
        return None
    if shape.ShapeType() == TopAbs_SOLID:
        if BRepCheck_Analyzer(shape, False).IsValid():
            return to_solid(shape)
        fixer = ShapeFix_Shape(shape)
        fixer.Perform()
        solids = _extract_solids(fixer.Shape())
        return solids[0] if solids else to_solid(shape)

    if shape.ShapeType() == TopAbs_SHELL:
        mk = BRepBuilderAPI_MakeSolid(to_shell(shape))
        if mk.IsDone():
            sfix = ShapeFix_Solid(mk.Solid())
            sfix.Perform()
            return sfix.Solid()
    return None


def _is_concave_small_cylindrical_hole(face: TopoDS_Face, max_radius: float) -> bool:
    """Return True if `face` is an internal (concave) cylindrical hole with radius <= max_radius."""
    try:
        surf = BRepAdaptor_Surface(face, True)
        if surf.GetType() != GeomAbs_Cylinder:
            return False
        cyl = surf.Cylinder()
        radius = cyl.Radius()
        if radius <= 1e-6 or radius > max_radius:
            return False

        u_mid = 0.5 * (surf.FirstUParameter() + surf.LastUParameter())
        v_mid = 0.5 * (surf.FirstVParameter() + surf.LastVParameter())
        props = BRepLProp_SLProps(surf, u_mid, v_mid, 1, 1e-6)
        if not props.IsNormalDefined():
            return False

        pt = props.Value()
        normal = props.Normal()
        if face.Orientation() == TopAbs_REVERSED:
            normal.Reverse()

        axis = cyl.Axis()
        axis_loc = axis.Location()
        axis_dir = axis.Direction()
        to_pt = gp_Vec(axis_loc, pt)
        proj = to_pt.Dot(gp_Vec(axis_dir))
        axis_closest = gp_Pnt(
            axis_loc.X() + proj * axis_dir.X(),
            axis_loc.Y() + proj * axis_dir.Y(),
            axis_loc.Z() + proj * axis_dir.Z(),
        )
        to_axis = gp_Vec(pt, axis_closest)
        return to_axis.Dot(gp_Vec(normal)) > 0.0
    except Exception:
        return False


def _remove_small_holes_from_solid(
    solid: TopoDS_Solid,
    max_hole_diameter: float,
    max_faces_per_solid: int = 250,
    max_hole_faces: int = 16,
) -> tuple[TopoDS_Solid, int]:
    """Optionally plug small concave cylindrical holes on simple/moderate solids."""
    if max_hole_diameter <= 0.0:
        return solid, 0

    max_radius = 0.5 * max_hole_diameter
    hole_faces: list[TopoDS_Face] = []
    total_faces = 0
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        total_faces += 1
        face = to_face(exp.Current())
        if len(hole_faces) <= max_hole_faces and _is_concave_small_cylindrical_hole(
            face, max_radius
        ):
            hole_faces.append(face)
        exp.Next()

    current = solid
    removed = 0

    if hole_faces and total_faces <= max_faces_per_solid and len(hole_faces) <= max_hole_faces:
        try:
            defeat = BRepAlgoAPI_Defeaturing()
            defeat.SetShape(current)
            defeat.SetRunParallel(True)
            for f in hole_faces:
                defeat.AddFaceToRemove(f)
            defeat.Build()
            if defeat.IsDone():
                res_solids = _extract_solids(defeat.Shape())
                if (
                    len(res_solids) == 1
                    and BRepCheck_Analyzer(res_solids[0], False).IsValid()
                    and _shape_volume(res_solids[0]) >= _shape_volume(current) * 0.98
                ):
                    current = res_solids[0]
                    removed = len(hole_faces)
        except Exception:
            pass

    if total_faces <= max_faces_per_solid:
        try:
            max_area = math.pi * (max_radius ** 2) * 1.1
            rem_wires = ShapeUpgrade_RemoveInternalWires(current)
            rem_wires.SetMinArea(max_area)
            rem_wires.Perform()
            solids_w = _extract_solids(rem_wires.GetResult())
            if (
                len(solids_w) == 1
                and BRepCheck_Analyzer(solids_w[0], False).IsValid()
                and _shape_volume(solids_w[0]) >= _shape_volume(current) * 0.98
            ):
                current = solids_w[0]
        except Exception:
            pass

    return current, removed


def _remove_closed_void_shells(solid: TopoDS_Solid) -> tuple[TopoDS_Solid, int, float]:
    """Strip all enclosed internal void shells from a solid, keeping exact OuterShell."""
    n_shells = _count_shells(solid)
    if n_shells <= 1:
        return solid, 0, 0.0

    orig_vol = abs(_shape_volume(solid))
    try:
        outer_shell = BRepClass3d.OuterShell_s(solid)
        if outer_shell.IsNull():
            return solid, 0, 0.0
        mk = BRepBuilderAPI_MakeSolid(outer_shell)
        if not mk.IsDone():
            return solid, 0, 0.0
        sfix = ShapeFix_Solid(mk.Solid())
        sfix.Perform()
        fixed = sfix.Solid()
        new_vol = abs(_shape_volume(fixed))
        if new_vol >= orig_vol * 0.999:
            return fixed, n_shells - 1, max(0.0, new_vol - orig_vol)
    except Exception:
        pass
    return solid, 0, 0.0


def _26_unit_directions() -> np.ndarray:
    """Return the 26 unit grid directions (6 axis + 12 edge + 8 corner) for sky visibility."""
    dirs = []
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dy == 0 and dz == 0:
                    continue
                v = np.array([dx, dy, dz], dtype=np.float64)
                dirs.append(v / np.linalg.norm(v))
    return np.vstack(dirs)


def _choose_best_slice_axis(safe_core: np.ndarray) -> int:
    """Pick X(0), Y(1), or Z(2) axis giving highest average cross-section circularity."""
    best_axis = 2
    best_score = -1.0
    for axis in (0, 1, 2):
        n_slices = safe_core.shape[axis]
        if n_slices < 3:
            continue
        sample_idxs = np.linspace(0, n_slices - 1, min(12, n_slices), dtype=int)
        scores = []
        for idx in sample_idxs:
            sl = np.take(safe_core, int(idx), axis=axis)
            area = int(np.count_nonzero(sl))
            if area < 16:
                continue
            dt = ndimage.distance_transform_edt(sl)
            r_max = float(np.max(dt))
            circ = (math.pi * r_max * r_max) / area
            scores.append(min(1.0, circ))
        mean_score = float(np.mean(scores)) if scores else 0.0
        if mean_score > best_score:
            best_score = mean_score
            best_axis = axis
    return best_axis


def _build_analytic_interior_core(
    kept_parts: list[PartNode],
    bridge_gap_mm: float = 3.5,
    inset_mm: float = 2.0,
    grid_res: int = 120,
    logs: Optional[list[str]] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> list[TopoDS_Solid]:
    """Construct clean, strictly-interior analytic B-Rep cylinders & prisms.

    NEVER replaces or touches exterior faces:
      1. Builds an internal distance field inside the gap-bridged envelope.
      2. Enforces strict inward safety inset (>= inset_mm from outer boundary).
      3. Carves out any voxel exposed to external sky (heatsink fin valleys, external
         recesses, counterbores) via 26-direction Embree ray visibility checks.
      4. Converts remaining strictly-interior space into pure analytic B-Rep cylinders
         and simplified planar-faced extruded prisms along the optimal principal axis.
    """
    verts_list, faces_list = [], []
    off = 0
    for p in kept_parts:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        defl = max(min(p.bbox_diagonal * 0.01, 0.35), 0.08)
        v, f, _, _ = tessellate_shape_triangles(p.shape, linear_deflection=defl)
        if len(f) > 0:
            verts_list.append(v)
            faces_list.append(f + off)
            off += len(v)

    if not verts_list:
        return []

    mesh = trimesh.Trimesh(
        vertices=np.vstack(verts_list), faces=np.vstack(faces_list), process=False
    )
    bmin = np.asarray(mesh.bounds[0], dtype=np.float64)
    bmax = np.asarray(mesh.bounds[1], dtype=np.float64)
    extents = bmax - bmin
    max_ext = float(np.max(extents))
    if max_ext <= 1e-3:
        return []

    res = max(64, min(int(grid_res), 180))
    pitch = max_ext / res
    r_close = max(0.0, float(bridge_gap_mm)) / pitch
    pad = max(4, int(math.ceil(r_close)) + 3)
    origin = bmin - pad * pitch
    dims = np.ceil(extents / pitch).astype(int) + 2 * pad + 1
    nx, ny, nz = int(dims[0]), int(dims[1]), int(dims[2])

    # 3-axis Embree surface occupancy sweep
    occ = np.zeros((nx, ny, nz), dtype=bool)
    intersector = mesh.ray
    xs = origin[0] + (np.arange(nx) + 0.5) * pitch
    ys = origin[1] + (np.arange(ny) + 0.5) * pitch
    zs = origin[2] + (np.arange(nz) + 0.5) * pitch

    sweeps = [
        (0, ys, zs, origin[0] - pitch, np.array([1.0, 0.0, 0.0])),
        (1, xs, zs, origin[1] - pitch, np.array([0.0, 1.0, 0.0])),
        (2, xs, ys, origin[2] - pitch, np.array([0.0, 0.0, 1.0])),
    ]
    for axis, a1, a2, start_val, direction in sweeps:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        g1, g2 = np.meshgrid(a1, a2, indexing="ij")
        n_rays = g1.size
        origs = np.empty((n_rays, 3), dtype=np.float64)
        if axis == 0:
            origs[:, 0], origs[:, 1], origs[:, 2] = start_val, g1.ravel(), g2.ravel()
        elif axis == 1:
            origs[:, 0], origs[:, 1], origs[:, 2] = g1.ravel(), start_val, g2.ravel()
        else:
            origs[:, 0], origs[:, 1], origs[:, 2] = g1.ravel(), g2.ravel(), start_val
        dirs = np.broadcast_to(direction, (n_rays, 3))
        locs, _, _ = intersector.intersects_location(origs, dirs, multiple_hits=True)
        if len(locs) > 0:
            ijk = np.floor((locs - origin) / pitch).astype(int)
            ok = (
                (ijk[:, 0] >= 0) & (ijk[:, 0] < nx)
                & (ijk[:, 1] >= 0) & (ijk[:, 1] < ny)
                & (ijk[:, 2] >= 0) & (ijk[:, 2] < nz)
            )
            ijk = ijk[ok]
            occ[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True

    # Unclosed flood-fill: regions already 100% sealed by existing walls
    air_raw, _ = ndimage.label(~occ)
    unclosed_inside = air_raw != int(air_raw[0, 0, 0])

    # EDT gap-bridged envelope (bridges bearing/cover running clearances up to ~2*bridge_gap_mm)
    if r_close >= 0.75:
        dilated = ndimage.distance_transform_edt(~occ) <= r_close
        air_dil, _ = ndimage.label(~dilated)
        filled_dil = air_dil != int(air_dil[0, 0, 0])
        envelope = (ndimage.distance_transform_edt(filled_dil) > r_close) | occ
        air2, _ = ndimage.label(~envelope)
        envelope = air2 != int(air2[0, 0, 0])
    else:
        envelope = unclosed_inside

    # Strict inward safety inset: core stays >= inset_mm inside the outer envelope
    effective_inset = max(float(inset_mm), pitch * 1.6)
    dist_to_outside = ndimage.distance_transform_edt(envelope) * pitch
    safe_core = dist_to_outside >= effective_inset

    if not safe_core.any():
        return []

    # --- Embree 26-direction Visibility Carving ---
    # Any candidate core voxel that wasn't strictly sealed in `unclosed_inside` AND has wide
    # open line-of-sight to the sky (e.g. between heatsink cooling fins or in an open external
    # recessed pocket) is carved away so the core NEVER intrudes into exterior features!
    uncertain_mask = safe_core & (~unclosed_inside)
    uncertain_ijk = np.argwhere(uncertain_mask)
    carved_voxels = 0
    if len(uncertain_ijk) > 0:
        dirs26 = _26_unit_directions()
        n_dirs = len(dirs26)
        pts_u = origin + (uncertain_ijk + 0.5) * pitch
        # Process in chunks to keep memory modest
        chunk_size = 4000
        exposed_flags = np.zeros(len(pts_u), dtype=bool)
        for c0 in range(0, len(pts_u), chunk_size):
            if cancel_check and cancel_check():
                raise InterruptedError("사용자가 작업을 취소했습니다.")
            c1 = min(c0 + chunk_size, len(pts_u))
            sub_pts = pts_u[c0:c1]
            ray_origs = np.repeat(sub_pts, n_dirs, axis=0)
            ray_dirs = np.tile(dirs26, (len(sub_pts), 1))
            hits = intersector.intersects_any(ray_origins=ray_origs, ray_directions=ray_dirs)
            sky_vis = np.mean(~hits.reshape(len(sub_pts), n_dirs), axis=1)
            # >= 15% unobstructed sky directions => external recess / fin valley, NOT internal cavity
            exposed_flags[c0:c1] = sky_vis >= 0.15

        if np.any(exposed_flags):
            bad_ijk = uncertain_ijk[exposed_flags]
            exposed_grid = np.zeros_like(safe_core)
            exposed_grid[bad_ijk[:, 0], bad_ijk[:, 1], bad_ijk[:, 2]] = True
            # Dilate carved air slightly (1 voxel) as safety margin from visible recesses
            exposed_grid = ndimage.binary_dilation(exposed_grid, iterations=1)
            carved_voxels = int(np.count_nonzero(safe_core & exposed_grid))
            safe_core &= ~exposed_grid

    if not safe_core.any():
        return []

    # Choose optimal principal slicing axis (X, Y, or Z)
    slice_axis = _choose_best_slice_axis(safe_core)
    axis_names = ("X", "Y", "Z")
    # Permute grid so slice_axis becomes axis 2 (local Z)
    perm = [a for a in (0, 1, 2) if a != slice_axis] + [slice_axis]
    grid_p = np.transpose(safe_core, perm)
    origin_p = origin[perm]

    def _to_world_pnt(u: float, v: float, w: float) -> gp_Pnt:
        coords = [0.0, 0.0, 0.0]
        coords[perm[0]] = u
        coords[perm[1]] = v
        coords[perm[2]] = w
        return gp_Pnt(coords[0], coords[1], coords[2])

    axis_vec = [0.0, 0.0, 0.0]
    axis_vec[slice_axis] = 1.0
    slice_dir = gp_Dir(axis_vec[0], axis_vec[1], axis_vec[2])

    # Build clean analytic cylinders / planar prisms per slab
    slab_vox = max(2, int(round(3.0 / pitch)))
    w_indices = np.where(grid_p.any(axis=(0, 1)))[0]
    if len(w_indices) == 0:
        return []

    w_min_i, w_max_i = int(w_indices[0]), int(w_indices[-1])
    w_overlap = max(0.25, pitch * 0.45)

    core_solids: list[TopoDS_Solid] = []
    n_cyls = 0
    n_prisms = 0
    wi = w_min_i

    while wi <= w_max_i:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        w_end = min(wi + slab_vox - 1, w_max_i)
        # Intersection across slab ensures 100% containment inside safe_core
        slice_2d = np.all(grid_p[:, :, wi : w_end + 1], axis=2)
        if not slice_2d.any():
            slice_2d = grid_p[:, :, wi]
            w_end = wi

        slice_2d = ndimage.binary_fill_holes(slice_2d)
        lbl_2d, n2 = ndimage.label(slice_2d)

        w0 = origin_p[2] + wi * pitch
        w1 = origin_p[2] + (w_end + 1) * pitch
        if wi > w_min_i:
            w0 -= w_overlap
        if w_end < w_max_i:
            w1 += w_overlap
        height = w1 - w0
        if height <= 1e-3:
            wi = w_end + 1
            continue

        for comp_id in range(1, n2 + 1):
            comp = lbl_2d == comp_id
            area_mm2 = float(np.count_nonzero(comp)) * (pitch ** 2)
            if area_mm2 < 20.0:
                continue

            dt2 = ndimage.distance_transform_edt(comp)
            max_idx = np.unravel_index(int(np.argmax(dt2)), dt2.shape)
            r_inscribed = float(dt2[max_idx]) * pitch
            circ_area = math.pi * (r_inscribed ** 2)

            if r_inscribed >= 2.5 and (circ_area / area_mm2) >= 0.84:
                cu = origin_p[0] + (max_idx[0] + 0.5) * pitch
                cv = origin_p[1] + (max_idx[1] + 0.5) * pitch
                p0 = _to_world_pnt(cu, cv, w0)
                cyl = BRepPrimAPI_MakeCylinder(
                    gp_Ax2(p0, slice_dir), max(r_inscribed - 0.1, 0.5), height
                ).Solid()
                core_solids.append(cyl)
                n_cyls += 1
            else:
                eroded_2d = ndimage.binary_erosion(comp, iterations=1)
                if not eroded_2d.any():
                    continue
                contours = find_contours(eroded_2d.astype(np.float32), 0.5)
                if not contours:
                    continue
                contour = max(contours, key=len)
                approx = approximate_polygon(contour, tolerance=max(0.75, 0.55 / pitch))
                if len(approx) < 4:
                    continue
                poly = BRepBuilderAPI_MakePolygon()
                for pt in approx[:-1]:
                    wu = origin_p[0] + (float(pt[0]) + 0.5) * pitch
                    wv = origin_p[1] + (float(pt[1]) + 0.5) * pitch
                    poly.Add(_to_world_pnt(wu, wv, w0))
                poly.Close()
                if poly.IsDone():
                    mk_f = BRepBuilderAPI_MakeFace(poly.Wire(), True)
                    if mk_f.IsDone():
                        extrude_vec = gp_Vec(
                            axis_vec[0] * height,
                            axis_vec[1] * height,
                            axis_vec[2] * height,
                        )
                        prism = BRepPrimAPI_MakePrism(mk_f.Face(), extrude_vec).Shape()
                        sols_p = _extract_solids(prism)
                        if sols_p:
                            core_solids.append(sols_p[0])
                            n_prisms += 1

        wi = w_end + 1

    if logs is not None:
        logs.append(
            f"해석적 내부 코어 생성 완료 ({axis_names[slice_axis]}축 정렬: "
            f"원통 {n_cyls}개, 다각 프리즘 {n_prisms}개 | 외관 노출 영역 {carved_voxels:,} 복셀 보호 제외)"
        )

    return core_solids


def _fuse_two_safe(
    s1: TopoDS_Shape,
    s2: TopoDS_Shape,
    fuzzy_schedule: tuple[float, ...] = (0.0, 1e-5, 1e-4, 1e-3, 5e-3),
) -> Optional[TopoDS_Shape]:
    """Precision-first B-Rep Fuse with strict volume-preservation guard.

    Always tries exact fuzzy=0.0 first so fine CAD fillets/tangent edges are never
    corrupted, and rejects degenerate results that silently drop either operand.
    """
    v1 = abs(_shape_volume(s1))
    v2 = abs(_shape_volume(s2))
    min_expected = max(v1, v2) - max(0.5, min(v1, v2) * 0.01)
    max_expected = (v1 + v2) * 1.01 + 1.0

    for fz in fuzzy_schedule:
        try:
            fuse = BRepAlgoAPI_Fuse(s1, s2)
            fuse.SetRunParallel(True)
            if fz > 0.0:
                fuse.SetFuzzyValue(float(fz))
            fuse.SetNonDestructive(True)
            fuse.Build()
            if fuse.IsDone() and not fuse.Shape().IsNull():
                res = fuse.Shape()
                if not _extract_solids(res):
                    continue
                vr = abs(_shape_volume(res))
                if min_expected <= vr <= max_expected:
                    return res
        except Exception:
            continue
    return None


def _batch_fuse_safe(
    shapes: list[TopoDS_Shape],
    logs: list[str],
    label: str = "Boolean Fuse",
    progress_cb: Optional[ProgressCallback] = None,
    prog_range: tuple[float, float] = (0.35, 0.70),
    cancel_check: Optional[CancelCheck] = None,
) -> TopoDS_Shape:
    """Fast multi-tool exact B-Rep Fuse with safe progressive fallback."""
    if len(shapes) == 1:
        return shapes[0]

    max_v = max(abs(_shape_volume(s)) for s in shapes)
    sum_v = sum(abs(_shape_volume(s)) for s in shapes)

    # Attempt fast single-pass multi-tool batch fuse at exact fuzzy=0.0 (then 1e-4)
    for fz in (0.0, 1e-4):
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        try:
            args = ShapeList()
            args.Append(shapes[0])
            tools = ShapeList()
            for s in shapes[1:]:
                tools.Append(s)
            fuse = BRepAlgoAPI_Fuse()
            fuse.SetArguments(args)
            fuse.SetTools(tools)
            fuse.SetRunParallel(True)
            if fz > 0.0:
                fuse.SetFuzzyValue(fz)
            fuse.SetNonDestructive(True)
            fuse.Build()
            if fuse.IsDone() and not fuse.Shape().IsNull():
                res = fuse.Shape()
                sols = _extract_solids(res)
                vr = abs(_shape_volume(res))
                if sols and vr >= max_v * 0.99 and vr <= sum_v * 1.01 + 1.0:
                    logs.append(
                        f"{label} 배치 결합 성공 ({len(shapes)}개 → {len(sols)}개 Solid, fuzzy={fz})"
                    )
                    return res
        except Exception:
            pass

    # Progressive pairwise fallback (strictly volume-checked so no part is silently lost)
    acc = shapes[0]
    failed = 0
    total = len(shapes) - 1
    p0, p1 = prog_range

    for idx, nxt in enumerate(shapes[1:], start=1):
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        if progress_cb and (idx % max(1, total // 10) == 0 or idx == total):
            progress_cb(p0 + (p1 - p0) * (idx / max(total, 1)), f"{label} ({idx}/{total})...")
        merged = _fuse_two_safe(acc, nxt)
        if merged is not None:
            acc = merged
        else:
            failed += 1

    if failed > 0:
        logs.append(f"{label} 순차 결합 완료 ({total + 1 - failed}/{total + 1}개 성공, {failed}개 생략)")
    else:
        logs.append(f"{label} 순차 결합 완료 ({total + 1}개 전체 성공)")
    return acc


def _is_solid_inside(inner: TopoDS_Solid, outer: TopoDS_Solid) -> bool:
    """Return True if `inner` solid is strictly inside `outer` solid."""
    in_min, in_max = _shape_bbox(inner)
    out_min, out_max = _shape_bbox(outer)
    if np.any(in_min < out_min - 1e-2) or np.any(in_max > out_max + 1e-2):
        return False
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(inner, props)
    cog = props.CentreOfMass()
    classifier = BRepClass3d_SolidClassifier(outer, cog, 1e-3)
    return classifier.State() == TopAbs_IN


def _connect_floating_islands(
    main_solid: TopoDS_Solid,
    islands: list[TopoDS_Solid],
    max_gap_mm: float,
    logs: list[str],
    cancel_check: Optional[CancelCheck] = None,
) -> tuple[TopoDS_Solid, list[TopoDS_Solid]]:
    """Connect nearby clearance-separated external solids to main_solid via hidden B-Rep pins."""
    if not islands or max_gap_mm <= 0.0:
        return main_solid, islands

    # Build fast KDTree on main_solid surface vertices
    mv, _, _, _ = tessellate_shape_triangles(main_solid, 0.25)
    if len(mv) == 0:
        return main_solid, islands
    main_tree = cKDTree(mv)

    remaining: list[TopoDS_Solid] = []
    connected = 0

    for isl in islands:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        if _is_solid_inside(isl, main_solid):
            continue

        iv, _, _, _ = tessellate_shape_triangles(isl, 0.25)
        if len(iv) == 0:
            continue
        dists, idxs = main_tree.query(iv, k=1)
        best_i = int(np.argmin(dists))
        min_dist = float(dists[best_i])
        if min_dist > max_gap_mm:
            remaining.append(isl)
            continue

        p_isl = iv[best_i]
        p_main = mv[int(idxs[best_i])]
        vec = p_main - p_isl
        dist_len = float(np.linalg.norm(vec))
        if dist_len < 1e-4:
            # Already touching — direct exact fuse
            fused = _fuse_two_safe(main_solid, isl)
            if fused is not None:
                sols = _extract_solids(fused)
                if len(sols) == 1:
                    main_solid, _, _ = _remove_closed_void_shells(sols[0])
                    connected += 1
                    continue
            remaining.append(isl)
            continue

        unit_v = vec / dist_len
        bmin, bmax = _shape_bbox(isl)
        min_dim = float(np.min(bmax - bmin))
        pin_r = max(0.3, min(min_dim * 0.28, 1.5))
        pen = min(1.5, max(min_dim * 0.35, 0.5))

        start = p_isl - unit_v * pen
        total_len = dist_len + 2.0 * pen
        pin = BRepPrimAPI_MakeCylinder(
            gp_Ax2(
                gp_Pnt(float(start[0]), float(start[1]), float(start[2])),
                gp_Dir(float(unit_v[0]), float(unit_v[1]), float(unit_v[2])),
            ),
            pin_r,
            total_len,
        ).Solid()

        step1 = _fuse_two_safe(main_solid, pin)
        if step1 is not None:
            step2 = _fuse_two_safe(step1, isl)
            if step2 is not None:
                sols = _extract_solids(step2)
                if len(sols) == 1:
                    main_solid, _, _ = _remove_closed_void_shells(sols[0])
                    connected += 1
                    continue

        remaining.append(isl)

    if connected > 0:
        logs.append(f"미세 공차 분리 외관 부품 {connected}개를 내부 B-Rep 핀 브리지로 단일화 연결")

    return main_solid, remaining


def simplify_assembly_to_single_solid(
    assembly: AssemblyModel,
    config: SimplifyConfig,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> SimplifyResult:
    """Remove internal parts and build a 100% exterior-preserving single exact B-Rep Solid."""
    t0 = time.perf_counter()
    logs: list[str] = []

    kept_parts = [p for p in assembly.parts if p.keep]
    removed_count = len(assembly.parts) - len(kept_parts)
    if not kept_parts:
        raise ValueError("유지할 외관 부품이 1개도 선택되지 않았습니다.")

    kept_parts_sorted = sorted(kept_parts, key=lambda p: -max(p.volume, 0.0))
    orig_vol = sum(max(p.volume, 0.0) for p in kept_parts_sorted)
    logs.append(
        f"정밀 B-Rep 단순화 시작: 유지 부품 {len(kept_parts_sorted)}개 / 삭제 부품 {removed_count}개 "
        f"(유지 부품 합산 부피: {orig_vol:,.1f} mm³)"
    )

    # --- Stage 1: Heal kept B-Rep shapes ---
    if progress_cb:
        progress_cb(0.06, f"외관 부품 B-Rep 형상 검증 및 보정 중 ({len(kept_parts_sorted)}개)...")

    solids: list[TopoDS_Solid] = []
    valid_parts: list[PartNode] = []
    for p in kept_parts_sorted:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")
        healed = _heal_solid(p.shape)
        if healed is not None:
            solids.append(healed)
            valid_parts.append(p)

    if not solids:
        raise RuntimeError("유효한 외관 B-Rep Solid를 추출하지 못했습니다.")

    # --- Stage 2: Optional Small Hole Defeaturing (OFF by default to keep exact exterior) ---
    if config.remove_small_holes and config.max_hole_diameter > 0.0:
        if progress_cb:
            progress_cb(
                0.12,
                f"소구경 관통홀 막기 실행 중 (직경 ≤ {config.max_hole_diameter:.1f} mm)...",
            )
        total_holes = 0
        cleaned: list[TopoDS_Solid] = []
        for idx, s in enumerate(solids):
            if cancel_check and cancel_check():
                raise InterruptedError("사용자가 작업을 취소했습니다.")
            cs, cnt = _remove_small_holes_from_solid(s, config.max_hole_diameter)
            cleaned.append(cs)
            total_holes += cnt
        solids = cleaned
        logs.append(f"소구경 홀 막기 완료: {total_holes}개 원통형 홀 면 제거")

    # --- Stage 3: Strictly-Interior Analytic B-Rep Core Generation ---
    core_shape: Optional[TopoDS_Shape] = None
    if config.fill_internal_core:
        if progress_cb:
            progress_cb(0.20, "외벽 안쪽 해석적 B-Rep 내부 코어 계산 중 (외관 변형 없음)...")
        core_primitives = _build_analytic_interior_core(
            valid_parts,
            bridge_gap_mm=config.core_bridge_gap_mm,
            inset_mm=config.core_inset_mm,
            grid_res=config.core_grid_resolution,
            logs=logs,
            cancel_check=cancel_check,
        )
        if core_primitives:
            if progress_cb:
                progress_cb(0.32, f"내부 코어 프리미티브 결합 중 ({len(core_primitives)}개)...")
            core_shape = _batch_fuse_safe(
                core_primitives,
                logs=logs,
                label="내부 코어 결합",
                cancel_check=cancel_check,
            )

    # --- Stage 4: Two-Tier Exact B-Rep Boolean Fuse (fuzzy=0.0 first!) ---
    # Tier 1: Fuse major structural housings + analytic interior core FIRST, then immediately
    # strip closed internal void shells so internal features (e.g. gear teeth, inner pockets)
    # disappear before fusing small fasteners/connectors.
    vol_thresh = orig_vol * 0.015
    major_solids = [s for s, p in zip(solids, valid_parts) if p.volume >= vol_thresh]
    minor_solids = [s for s, p in zip(solids, valid_parts) if p.volume < vol_thresh]
    if not major_solids:
        major_solids = solids[: min(6, len(solids))]
        minor_solids = solids[min(6, len(solids)) :]

    if progress_cb:
        progress_cb(0.40, f"주요 외관 하우징 + 내부 코어 정밀 B-Rep 결합 중 ({len(major_solids)}개)...")

    tier1_inputs: list[TopoDS_Shape] = (
        ([core_shape] if core_shape is not None else []) + list(major_solids)
    )
    tier1_fused = _batch_fuse_safe(
        tier1_inputs,
        logs=logs,
        label="주요 외관+내부코어 B-Rep Fuse",
        progress_cb=progress_cb,
        prog_range=(0.40, 0.62),
        cancel_check=cancel_check,
    )

    # Immediately purge closed void shells inside Tier-1 result
    if progress_cb:
        progress_cb(0.64, "1차 밀폐 내부 공동(Void Shell) 제거 중...")
    tier1_solids = _extract_solids(tier1_fused)
    tier1_cleaned: list[TopoDS_Solid] = []
    total_voids = 0
    total_void_vol = 0.0
    for s in tier1_solids:
        sf, nv, dv = _remove_closed_void_shells(s)
        total_voids += nv
        total_void_vol += dv
        tier1_cleaned.append(sf)

    # Tier 2: Fuse remaining smaller external parts (connectors, screws, brackets)
    if minor_solids:
        if progress_cb:
            progress_cb(0.68, f"세부 외관 부품(커넥터·볼트 등) 결합 중 ({len(minor_solids)}개)...")
        tier2_inputs: list[TopoDS_Shape] = list(tier1_cleaned) + list(minor_solids)
        combined = _batch_fuse_safe(
            tier2_inputs,
            logs=logs,
            label="세부 외관 부품 B-Rep Fuse",
            progress_cb=progress_cb,
            prog_range=(0.68, 0.80),
            cancel_check=cancel_check,
        )
    else:
        combined = tier1_cleaned[0] if len(tier1_cleaned) == 1 else tier1_fused

    # Final closed-void shell elimination on every solid
    if progress_cb:
        progress_cb(0.82, "최종 내부 공동(Void Shell) 완전 제거 중...")

    final_solids: list[TopoDS_Solid] = []
    for s in _extract_solids(combined):
        sf, nv, dv = _remove_closed_void_shells(s)
        total_voids += nv
        total_void_vol += dv
        final_solids.append(sf)

    if total_voids > 0:
        logs.append(
            f"밀폐 내부 공동(Void Shell) 총 {total_voids}개 제거 (추가 솔리드 채움 +{total_void_vol:,.1f} mm³)"
        )

    final_solids.sort(key=lambda s: -abs(_shape_volume(s)))

    # Prune any solids strictly swallowed inside a larger filled solid
    surviving: list[TopoDS_Solid] = []
    swallowed = 0
    for s in final_solids:
        if any(_is_solid_inside(s, big) for big in surviving):
            swallowed += 1
        else:
            surviving.append(s)
    if swallowed > 0:
        logs.append(f"채워진 외피 내부에 완전히 포함된 잔여 Solid {swallowed}개 제거")

    # Stage 5: Bridge any remaining nearby floating external islands via hidden internal B-Rep pins
    if len(surviving) > 1 and config.connect_floating_parts and config.max_island_gap_mm > 0.0:
        if progress_cb:
            progress_cb(0.86, f"분리 외관 부품({len(surviving) - 1}개) 내부 브리지 연결 중...")
        main_s, leftovers = _connect_floating_islands(
            surviving[0],
            surviving[1:],
            max_gap_mm=config.max_island_gap_mm,
            logs=logs,
            cancel_check=cancel_check,
        )
        surviving = [main_s] + leftovers

    if not surviving:
        raise RuntimeError("최종 정밀 B-Rep Solid를 생성하지 못했습니다.")

    if len(surviving) == 1 or config.keep_largest_only:
        final_shape: TopoDS_Shape = surviving[0]
        if len(surviving) > 1:
            logs.append(
                f"단일 Body 유지 옵션에 따라 최대 본체 1개 채택 (연결 불가 분리체 {len(surviving) - 1}개 제외)"
            )
    else:
        builder = BRep_Builder()
        comp = TopoDS_Compound()
        builder.MakeCompound(comp)
        for s in surviving:
            builder.Add(comp, s)
        final_shape = comp

    # --- Stage 6: UnifySameDomain for Clean, Compact Exact B-Rep ---
    if config.unify_same_domain:
        if progress_cb:
            progress_cb(0.91, "동일 평면/곡면 통합(UnifySameDomain) 최적화 중...")
        try:
            rep_before = validate_shape(final_shape)
            unifier = ShapeUpgrade_UnifySameDomain(final_shape, True, True, False)
            unifier.Build()
            unified = unifier.Shape()
            if not unified.IsNull():
                rep_u = validate_shape(unified)
                if (
                    rep_u.solid_count == rep_before.solid_count
                    and rep_u.shell_count <= rep_before.shell_count
                    and rep_u.is_watertight
                    and abs(rep_u.volume - rep_before.volume) / max(rep_before.volume, 1.0) < 0.01
                ):
                    final_shape = unified
                    logs.append(
                        f"동일 곡면 통합 완료: {rep_before.face_count:,}면 → {rep_u.face_count:,}면"
                    )
        except Exception as exc:
            logs.append(f"UnifySameDomain 경고(원본 유지): {exc}")

    if progress_cb:
        progress_cb(0.97, "최종 단일 B-Rep Solid 품질 검증 중...")

    report = validate_shape(final_shape)
    if report.face_types:
        ft_summary = ", ".join(f"{k}:{v}" for k, v in sorted(report.face_types.items(), key=lambda kv: -kv[1]))
        logs.append(f"최종 B-Rep 곡면 구성 (삼각 복셀 없음): {ft_summary}")

    if progress_cb:
        progress_cb(1.0, "정밀 B-Rep 단일 Solid Body 변환 완료")

    return SimplifyResult(
        shape=final_shape,
        mode_used="정밀 B-Rep (외관 원본 보존 + 내부 코어 솔리드화)",
        validation=report,
        kept_part_count=len(kept_parts_sorted),
        removed_part_count=removed_count,
        original_volume=orig_vol,
        filled_volume_delta=max(0.0, report.volume - orig_vol),
        elapsed_seconds=time.perf_counter() - t0,
        logs=logs,
    )
