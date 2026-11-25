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

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing, BRepAlgoAPI_Fuse, BRepAlgoAPI_Section
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Copy,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepClass3d import BRepClass3d, BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepFeat import BRepFeat_SplitShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeSphere,
)
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pln, gp_Pnt, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance, ShapeAnalysis_Wire
from OCP.ShapeBuild import ShapeBuild_ReShape
from OCP.ShapeFix import (
    ShapeFix_FixSmallFace,
    ShapeFix_Shape,
    ShapeFix_Solid,
    ShapeFix_Wireframe,
)
from OCP.ShapeUpgrade import (
    ShapeUpgrade_RemoveInternalWires,
    ShapeUpgrade_ShapeDivideClosed,
    ShapeUpgrade_UnifySameDomain,
)
from OCP.StdPrs import StdPrs_ShadedShape, StdPrs_ToolTriangulatedShape
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import (
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_FORWARD,
    TopAbs_IN,
    TopAbs_INTERNAL,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import (
    TopoDS_Compound,
    TopoDS_Edge,
    TopoDS_Face,
    TopoDS_Shape,
    TopoDS_Shell,
    TopoDS_Solid,
    TopoDS_Vertex,
    TopoDS_Wire,
)
from scipy import ndimage
from scipy.spatial import cKDTree
from skimage.measure import approximate_polygon, find_contours

from simple_3d_step.core.occt_compat import (
    ShapeIndexedMap,
    ShapeList,
    default_ray_threads,
    ensure_occt_parallelism_enabled,
    make_brep_check_analyzer,
    to_edge,
    to_face,
    to_shell,
    to_solid,
    to_vertex,
    to_wire,
)
from simple_3d_step.core.tessellation import (
    TessellatedAssembly,
    build_subset_trimesh,
    extract_shape_triangles,
    mesh_shapes_in_parallel_buckets,
)
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
        if make_brep_check_analyzer(shape, geom_controls=False, parallel=True).IsValid():
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
                    and make_brep_check_analyzer(
                        res_solids[0], geom_controls=False, parallel=True
                    ).IsValid()
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
                and make_brep_check_analyzer(
                    solids_w[0], geom_controls=False, parallel=True
                ).IsValid()
                and _shape_volume(solids_w[0]) >= _shape_volume(current) * 0.98
            ):
                current = solids_w[0]
        except Exception:
            pass

    return current, removed


def _strip_disconnected_void_components(solid: TopoDS_Solid) -> tuple[TopoDS_Solid, int, float]:
    """Remove any hidden disconnected closed void face-components packed into a single Shell.

    Some OCCT healing/boolean steps can place a closed internal cavity inside the same
    TopoDS_Shell as the outer boundary (so shell_count appears to be 1 while an internal
    hollow shell still exists). BFS over the edge->face adjacency graph detects and strips
    all internal disconnected closed face groups, keeping only the outermost skin.
    """
    fmap = ShapeIndexedMap()
    TopExp.MapShapes_s(solid, TopAbs_FACE, fmap)
    n_faces = fmap.Extent()
    if n_faces == 0:
        return solid, 0, 0.0

    # Map Face -> Edge using ShapeIndexedMap (avoids slow OCP List_TopoDS_Shape iteration)
    emap = ShapeIndexedMap()
    TopExp.MapShapes_s(solid, TopAbs_EDGE, emap)
    edge_faces: list[list[int]] = [[] for _ in range(emap.Extent() + 1)]
    for fid in range(1, n_faces + 1):
        exp_e = TopExp_Explorer(fmap.FindKey(fid), TopAbs_EDGE)
        while exp_e.More():
            eid = emap.FindIndex(exp_e.Current())
            if eid > 0:
                edge_faces[eid].append(fid)
            exp_e.Next()

    adj: list[set[int]] = [set() for _ in range(n_faces + 1)]
    for fids in edge_faces:
        if len(fids) < 2:
            continue
        if len(fids) == 2:
            a, b = fids[0], fids[1]
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
        else:
            for a in fids:
                for b in fids:
                    if a != b:
                        adj[a].add(b)

    visited = [False] * (n_faces + 1)
    components: list[list[int]] = []
    for i in range(1, n_faces + 1):
        if visited[i]:
            continue
        q = deque([i])
        visited[i] = True
        comp: list[int] = []
        while q:
            u = q.popleft()
            comp.append(u)
            for v in adj[u]:
                if not visited[v]:
                    visited[v] = True
                    q.append(v)
        components.append(comp)

    if len(components) <= 1:
        return solid, 0, 0.0

    # Choose the outermost component (largest bounding-box diagonal; tie-break by face count)
    best_comp = None
    best_key = (-1.0, -1)
    for comp in components:
        box = Bnd_Box()
        for fid in comp:
            BRepBndLib.Add_s(fmap.FindKey(fid), box, False)
        diag = 0.0
        if not box.IsVoid():
            pmin, pmax = box.CornerMin(), box.CornerMax()
            diag = math.sqrt(
                (pmax.X() - pmin.X()) ** 2
                + (pmax.Y() - pmin.Y()) ** 2
                + (pmax.Z() - pmin.Z()) ** 2
            )
        key = (round(diag, 4), len(comp))
        if key > best_key:
            best_key = key
            best_comp = comp

    if best_comp is None:
        return solid, 0, 0.0

    orig_vol = abs(_shape_volume(solid))
    try:
        builder = BRep_Builder()
        outer_shell = TopoDS_Shell()
        builder.MakeShell(outer_shell)
        for fid in best_comp:
            builder.Add(outer_shell, fmap.FindKey(fid))
        outer_shell.Closed(True)

        mk = BRepBuilderAPI_MakeSolid(outer_shell)
        if not mk.IsDone():
            return solid, 0, 0.0
        sfix = ShapeFix_Solid(mk.Solid())
        sfix.Perform()
        fixed = to_solid(sfix.Solid())
        BRepLib.OrientClosedSolid_s(fixed)
        new_vol = abs(_shape_volume(fixed))
        # Accept whenever removing inner components keeps or increases enclosed solid volume
        # (or removes strictly-enclosed inner shells regardless of their original face orientation)
        if new_vol > 0.0 and (new_vol >= orig_vol * 0.95):
            return fixed, len(components) - 1, max(0.0, new_vol - orig_vol)
    except Exception:
        pass
    return solid, 0, 0.0


def _remove_closed_void_shells(solid: TopoDS_Solid) -> tuple[TopoDS_Solid, int, float]:
    """Strip ALL enclosed internal void shells (both multi-shell and hidden single-shell voids)."""
    current = solid
    total_removed = 0
    total_delta = 0.0

    # Pass 1: Explicit multi-shell internal voids (TopoDS_Shell count > 1)
    n_shells = _count_shells(current)
    if n_shells > 1:
        orig_vol = abs(_shape_volume(current))
        outer_shell: Optional[TopoDS_Shell] = None
        try:
            cand = BRepClass3d.OuterShell_s(current)
            if not cand.IsNull():
                outer_shell = cand
        except Exception:
            outer_shell = None

        # Fallback: pick shell enclosing the largest solid volume
        if outer_shell is None:
            best_v = -1.0
            es = TopExp_Explorer(current, TopAbs_SHELL)
            while es.More():
                shl = to_shell(es.Current())
                try:
                    mk_t = BRepBuilderAPI_MakeSolid(shl)
                    if mk_t.IsDone():
                        st = mk_t.Solid()
                        BRepLib.OrientClosedSolid_s(st)
                        v = abs(_shape_volume(st))
                        if v > best_v:
                            best_v = v
                            outer_shell = shl
                except Exception:
                    pass
                es.Next()

        if outer_shell is not None:
            try:
                mk = BRepBuilderAPI_MakeSolid(outer_shell)
                if mk.IsDone():
                    sfix = ShapeFix_Solid(mk.Solid())
                    sfix.Perform()
                    fixed = to_solid(sfix.Solid())
                    BRepLib.OrientClosedSolid_s(fixed)
                    new_vol = abs(_shape_volume(fixed))
                    if new_vol >= orig_vol * 0.999:
                        total_removed += n_shells - 1
                        total_delta += max(0.0, new_vol - orig_vol)
                        current = fixed
            except Exception:
                pass

    # Pass 2: Hidden disconnected closed face-components inside the remaining shell
    cleaned, n_comp_removed, comp_delta = _strip_disconnected_void_components(current)
    if n_comp_removed > 0:
        current = cleaned
        total_removed += n_comp_removed
        total_delta += comp_delta

    return current, total_removed, total_delta


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
    tessellated: Optional[TessellatedAssembly] = None,
    max_workers: Optional[int] = None,
) -> list[TopoDS_Solid]:
    """Construct clean, strictly-interior analytic B-Rep cylinders & prisms.

    NEVER replaces or touches exterior faces:
      1. Builds an internal distance field inside the gap-bridged envelope.
      2. Enforces strict inward safety inset (>= inset_mm from outer boundary).
      3. Carves out any voxel exposed to external sky (heatsink fin valleys, external
         recesses, counterbores) via multi-threaded 26-direction Embree ray visibility checks.
      4. Converts remaining strictly-interior space into pure analytic B-Rep cylinders
         and simplified planar-faced extruded prisms along the optimal principal axis.
    """
    mesh: Optional[trimesh.Trimesh] = None
    if tessellated is not None:
        mesh = build_subset_trimesh(tessellated, (p.part_id for p in kept_parts))

    if mesh is None:
        shapes = [p.shape for p in kept_parts]
        defls = [max(min(p.bbox_diagonal * 0.01, 0.35), 0.08) for p in kept_parts]
        mesh_shapes_in_parallel_buckets(shapes, defls, cancel_check=cancel_check)

        verts_list, faces_list = [], []
        off = 0
        for p in kept_parts:
            if cancel_check and cancel_check():
                raise InterruptedError("Operation cancelled by user.")
            v, f, _, _ = extract_shape_triangles(p.shape)
            if len(f) > 0:
                verts_list.append(v)
                faces_list.append(f + off)
                off += len(v)

        if not verts_list:
            return []

        mesh = trimesh.Trimesh(
            vertices=np.vstack(verts_list), faces=np.vstack(faces_list), process=False
        )

    if len(mesh.faces) == 0:
        return []

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

    # Pre-warm Embree BVH and trimesh cached properties on main thread for thread-safe queries
    intersector = mesh.ray
    _ = mesh.triangles
    _ = mesh.face_normals
    _ = intersector.intersects_first(
        np.array([0.5 * (bmin + bmax)], dtype=np.float32),
        np.array([[1.0, 0.0, 0.0]], dtype=np.float32),
    )

    n_workers = default_ray_threads(max_workers)

    # Multi-threaded 3-axis Embree surface occupancy sweep
    occ = np.zeros((nx, ny, nz), dtype=bool)
    xs = origin[0] + (np.arange(nx) + 0.5) * pitch
    ys = origin[1] + (np.arange(ny) + 0.5) * pitch
    zs = origin[2] + (np.arange(nz) + 0.5) * pitch

    sweeps = [
        (0, ys, zs, origin[0] - pitch, np.array([1.0, 0.0, 0.0])),
        (1, xs, zs, origin[1] - pitch, np.array([0.0, 1.0, 0.0])),
        (2, xs, ys, origin[2] - pitch, np.array([0.0, 0.0, 1.0])),
    ]
    sweep_jobs: list[tuple[np.ndarray, np.ndarray]] = []
    ray_tile = 4096
    for axis, a1, a2, start_val, direction in sweeps:
        g1, g2 = np.meshgrid(a1, a2, indexing="ij")
        n_rays = g1.size
        origs = np.empty((n_rays, 3), dtype=np.float64)
        if axis == 0:
            origs[:, 0], origs[:, 1], origs[:, 2] = start_val, g1.ravel(), g2.ravel()
        elif axis == 1:
            origs[:, 0], origs[:, 1], origs[:, 2] = g1.ravel(), start_val, g2.ravel()
        else:
            origs[:, 0], origs[:, 1], origs[:, 2] = g1.ravel(), g2.ravel(), start_val
        for r0 in range(0, n_rays, ray_tile):
            r1 = min(r0 + ray_tile, n_rays)
            sweep_jobs.append((origs[r0:r1], np.broadcast_to(direction, (r1 - r0, 3))))

    def _run_sweep_tile(orig_chunk: np.ndarray, dir_chunk: np.ndarray) -> np.ndarray:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        locs, _, _ = intersector.intersects_location(orig_chunk, dir_chunk, multiple_hits=True)
        return locs

    def _mark_occ(locs: np.ndarray) -> None:
        if len(locs) > 0:
            ijk = np.floor((locs - origin) / pitch).astype(int)
            ok = (
                (ijk[:, 0] >= 0) & (ijk[:, 0] < nx)
                & (ijk[:, 1] >= 0) & (ijk[:, 1] < ny)
                & (ijk[:, 2] >= 0) & (ijk[:, 2] < nz)
            )
            ijk = ijk[ok]
            occ[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True

    executor: Optional[ThreadPoolExecutor] = (
        ThreadPoolExecutor(max_workers=n_workers) if n_workers > 1 else None
    )
    try:
        if executor is not None and len(sweep_jobs) > 1:
            futs = [executor.submit(_run_sweep_tile, oc, dc) for oc, dc in sweep_jobs]
            for fut in as_completed(futs):
                if cancel_check and cancel_check():
                    for pending in futs:
                        pending.cancel()
                    raise InterruptedError("Operation cancelled by user.")
                _mark_occ(fut.result())
        else:
            for oc, dc in sweep_jobs:
                _mark_occ(_run_sweep_tile(oc, dc))

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

        # --- Multi-threaded Embree 26-direction Visibility Carving ---
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
            chunk_size = 4000
            exposed_flags = np.zeros(len(pts_u), dtype=bool)
            chunks = [(c0, min(c0 + chunk_size, len(pts_u))) for c0 in range(0, len(pts_u), chunk_size)]

            def _carve_chunk(c0: int, c1: int) -> tuple[int, int, np.ndarray]:
                if cancel_check and cancel_check():
                    raise InterruptedError("Operation cancelled by user.")
                sub_pts = pts_u[c0:c1]
                ray_origs = np.repeat(sub_pts, n_dirs, axis=0)
                ray_dirs = np.tile(dirs26, (len(sub_pts), 1))
                hits = intersector.intersects_any(ray_origins=ray_origs, ray_directions=ray_dirs)
                sky_vis = np.mean(~hits.reshape(len(sub_pts), n_dirs), axis=1)
                return c0, c1, (sky_vis >= 0.15)

            if executor is not None and len(chunks) > 1:
                carve_futs = [executor.submit(_carve_chunk, c0, c1) for c0, c1 in chunks]
                for fut in as_completed(carve_futs):
                    if cancel_check and cancel_check():
                        for pending in carve_futs:
                            pending.cancel()
                        raise InterruptedError("Operation cancelled by user.")
                    c0, c1, flags = fut.result()
                    exposed_flags[c0:c1] = flags
            else:
                for c0, c1 in chunks:
                    _, _, flags = _carve_chunk(c0, c1)
                    exposed_flags[c0:c1] = flags

            if np.any(exposed_flags):
                bad_ijk = uncertain_ijk[exposed_flags]
                exposed_grid = np.zeros_like(safe_core)
                exposed_grid[bad_ijk[:, 0], bad_ijk[:, 1], bad_ijk[:, 2]] = True
                # Dilate carved air slightly (1 voxel) as safety margin from visible recesses
                exposed_grid = ndimage.binary_dilation(exposed_grid, iterations=1)
                carved_voxels = int(np.count_nonzero(safe_core & exposed_grid))
                safe_core &= ~exposed_grid
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

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

    # Collect cylinder specs first so adjacent coaxial slabs coalesce into single exact cylinders
    cyl_specs: list[list[float]] = []  # [cu, cv, radius, w0, w1]
    prism_solids: list[TopoDS_Solid] = []
    wi = w_min_i

    while wi <= w_max_i:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        w_end = min(wi + slab_vox - 1, w_max_i)
        # Intersection across slab ensures 100% containment inside safe_core
        slice_2d = np.all(grid_p[:, :, wi : w_end + 1], axis=2)
        if not slice_2d.any():
            slice_2d = grid_p[:, :, wi]
            w_end = wi
        else:
            # Greedily extend slab while cross-section stays constant (IoU >= 0.94),
            # collapsing long uniform bores/boxes into single exact analytic primitives
            # instead of dozens of redundant 3mm stacked slabs.
            base_union = np.any(grid_p[:, :, wi : w_end + 1], axis=2)
            while w_end < w_max_i:
                nxt_sl = grid_p[:, :, w_end + 1]
                cand_inter = slice_2d & nxt_sl
                cand_union = base_union | nxt_sl
                u_cnt = int(np.count_nonzero(cand_union))
                if u_cnt == 0:
                    break
                i_cnt = int(np.count_nonzero(cand_inter))
                if i_cnt >= 0.94 * u_cnt:
                    slice_2d = cand_inter
                    base_union = cand_union
                    w_end += 1
                else:
                    break

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
                rad = max(r_inscribed - 0.1, 0.5)
                # Try coalescing with an existing coaxial adjacent cylinder (strictly conservative radius)
                merged = False
                for prev in reversed(cyl_specs):
                    if prev[4] < w0 - 0.05:
                        continue
                    shift = math.hypot(cu - prev[0], cv - prev[1])
                    if shift <= 0.25 and abs(rad - prev[2]) <= 0.35:
                        new_r = min(prev[2], rad) - 0.5 * shift
                        if new_r >= max(0.5, 0.92 * min(prev[2], rad)):
                            prev[0] = 0.5 * (prev[0] + cu)
                            prev[1] = 0.5 * (prev[1] + cv)
                            prev[2] = new_r
                            prev[4] = max(prev[4], w1)
                            merged = True
                            break
                if not merged:
                    cyl_specs.append([cu, cv, rad, w0, w1])
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
                            prism_solids.append(sols_p[0])

        wi = w_end + 1

    core_solids: list[TopoDS_Solid] = []
    for cu, cv, rad, cw0, cw1 in cyl_specs:
        ch = cw1 - cw0
        if ch > 1e-3:
            p0 = _to_world_pnt(cu, cv, cw0)
            cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(p0, slice_dir), rad, ch).Solid()
            core_solids.append(cyl)
    core_solids.extend(prism_solids)

    n_cyls = len(cyl_specs)
    n_prisms = len(prism_solids)
    if logs is not None:
        logs.append(
            f"Analytic interior core generated ({axis_names[slice_axis]}-axis aligned: "
            f"{n_cyls} cylinders, {n_prisms} polygonal prisms | protected {carved_voxels:,} exterior-visible voxels)"
        )

    return core_solids


@dataclass
class _ManifoldAudit:
    """Fast O(N) Parasolid / SolidWorks manifold topology & edge-precision summary."""

    nm_edges: list[TopoDS_Edge] = field(default_factory=list)
    nm_verts: list[TopoDS_Vertex] = field(default_factory=list)
    internal_edge_count: int = 0
    free_edge_count: int = 0
    sub_parasolid_count: int = 0  # non-degenerated edges with length < 1e-5 mm
    tiny_edge_count: int = 0      # non-degenerated edges with length < tiny_threshold
    min_edge_len: float = float("inf")
    max_edge_tol: float = 0.0


def _outer_shells_for_audit(shape: TopoDS_Shape) -> TopoDS_Shape:
    """Return a compound of only the outer shell of each solid (ignoring enclosed void shells)."""
    sols = _extract_solids(shape)
    if not sols:
        return shape
    if all(_count_shells(s) <= 1 for s in sols):
        return shape
    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for s in sols:
        if _count_shells(s) > 1:
            try:
                outer = BRepClass3d.OuterShell_s(s)
                if not outer.IsNull():
                    builder.Add(comp, outer)
                    continue
            except Exception:
                pass
        builder.Add(comp, s)
    return comp


def _count_internal_edges(shape: TopoDS_Shape, outer_only: bool = True) -> int:
    """Count TopAbs_INTERNAL edges embedded inside faces (STEP AP214 drops these, causing SI)."""
    target = _outer_shells_for_audit(shape) if outer_only else shape
    cnt = 0
    ef = TopExp_Explorer(target, TopAbs_FACE)
    while ef.More():
        ee = TopExp_Explorer(ef.Current(), TopAbs_EDGE)
        while ee.More():
            if ee.Current().Orientation() == TopAbs_INTERNAL:
                cnt += 1
            ee.Next()
        ef.Next()
    return cnt


def _inspect_manifold_topology(
    shape: TopoDS_Shape,
    tiny_threshold: float = 5e-5,
    outer_only: bool = False,
) -> _ManifoldAudit:
    """Inspect non-manifold edges/vertices, TopAbs_INTERNAL edges, and sub-Parasolid edges."""
    target = _outer_shells_for_audit(shape) if outer_only else shape
    fmap = ShapeIndexedMap()
    TopExp.MapShapes_s(target, TopAbs_FACE, fmap)
    emap = ShapeIndexedMap()
    TopExp.MapShapes_s(target, TopAbs_EDGE, emap)
    vmap = ShapeIndexedMap()
    TopExp.MapShapes_s(target, TopAbs_VERTEX, vmap)

    n_faces = fmap.Extent()
    n_edges = emap.Extent()
    n_verts = vmap.Extent()
    if n_faces == 0 or n_edges == 0:
        return _ManifoldAudit()

    edge_faces: list[list[int]] = [[] for _ in range(n_edges + 1)]
    internal_edge_count = 0
    for fid in range(1, n_faces + 1):
        ee = TopExp_Explorer(fmap.FindKey(fid), TopAbs_EDGE)
        while ee.More():
            ed = to_edge(ee.Current())
            if ed.Orientation() == TopAbs_INTERNAL:
                internal_edge_count += 1
            if not BRep_Tool.Degenerated_s(ed):
                eid = emap.FindIndex(ed)
                if eid > 0:
                    edge_faces[eid].append(fid)
            ee.Next()

    nm_edges: list[TopoDS_Edge] = []
    free_edge_count = 0
    sub_parasolid_count = 0
    tiny_edge_count = 0
    min_edge_len = float("inf")
    max_edge_tol = 0.0

    for eid in range(1, n_edges + 1):
        e = to_edge(emap.FindKey(eid))
        tol = float(BRep_Tool.Tolerance_s(e))
        if tol > max_edge_tol:
            max_edge_tol = tol
        if BRep_Tool.Degenerated_s(e):
            continue
        inc = edge_faces[eid]
        if len(inc) == 1:
            free_edge_count += 1
        elif len(inc) > 2:
            nm_edges.append(e)
        lp = GProp_GProps()
        BRepGProp.LinearProperties_s(e, lp)
        elen = abs(float(lp.Mass()))
        if elen < min_edge_len:
            min_edge_len = elen
        if 0.0 < elen < 1e-5:
            sub_parasolid_count += 1
        if 0.0 < elen < tiny_threshold:
            tiny_edge_count += 1

    # Detect pinched non-manifold vertices (incident faces form > 1 edge-connected fan)
    v_faces: list[set[int]] = [set() for _ in range(n_verts + 1)]
    v_edges: list[set[int]] = [set() for _ in range(n_verts + 1)]
    for eid in range(1, n_edges + 1):
        e = to_edge(emap.FindKey(eid))
        if BRep_Tool.Degenerated_s(e):
            continue
        v1 = vmap.FindIndex(TopExp.FirstVertex_s(e, False))
        v2 = vmap.FindIndex(TopExp.LastVertex_s(e, False))
        if v1 > 0:
            v_edges[v1].add(eid)
        if v2 > 0:
            v_edges[v2].add(eid)
        for fid in edge_faces[eid]:
            if v1 > 0:
                v_faces[v1].add(fid)
            if v2 > 0:
                v_faces[v2].add(fid)

    nm_edge_vids: set[int] = set()
    for e in nm_edges:
        nm_edge_vids.add(vmap.FindIndex(TopExp.FirstVertex_s(e, False)))
        nm_edge_vids.add(vmap.FindIndex(TopExp.LastVertex_s(e, False)))

    nm_verts: list[TopoDS_Vertex] = []
    for vid in range(1, n_verts + 1):
        if vid in nm_edge_vids:
            continue
        fs = v_faces[vid]
        if len(fs) < 2:
            continue
        adj: dict[int, set[int]] = {f: set() for f in fs}
        for eid in v_edges[vid]:
            inc = [f for f in edge_faces[eid] if f in fs]
            for a in inc:
                for b in inc:
                    if a != b:
                        adj[a].add(b)
        visited: set[int] = set()
        comps = 0
        for f in fs:
            if f not in visited:
                comps += 1
                stack = [f]
                visited.add(f)
                while stack:
                    u = stack.pop()
                    for w in adj[u]:
                        if w not in visited:
                            visited.add(w)
                            stack.append(w)
        if comps > 1:
            nm_verts.append(to_vertex(vmap.FindKey(vid)))

    return _ManifoldAudit(
        nm_edges=nm_edges,
        nm_verts=nm_verts,
        internal_edge_count=internal_edge_count,
        free_edge_count=free_edge_count,
        sub_parasolid_count=sub_parasolid_count,
        tiny_edge_count=tiny_edge_count,
        min_edge_len=min_edge_len,
        max_edge_tol=max_edge_tol,
    )


def _fuse_two_safe(
    s1: TopoDS_Shape,
    s2: TopoDS_Shape,
    fuzzy_schedule: tuple[float, ...] = (0.0, 5e-5, 1e-4, 1e-3, 5e-3),
) -> Optional[TopoDS_Shape]:
    """Precision-first B-Rep Fuse with strict volume-preservation and internal-edge guards.

    Always tries exact fuzzy=0.0 first so fine CAD fillets/tangent edges are never
    corrupted, and rejects degenerate results that silently drop either operand. If
    fuzzy=0.0 embeds TopAbs_INTERNAL edges (from nanometer-scale NURBS/cylinder tangency
    gaps that STEP AP214 drops into self-intersections), retries at tight fuzzy (5e-5, 1e-4)
    and keeps the cleaner result only when validity and volume are preserved.
    """
    v1 = abs(_shape_volume(s1))
    v2 = abs(_shape_volume(s2))
    min_expected = max(v1, v2) - max(0.5, min(v1, v2) * 0.01)
    max_expected = (v1 + v2) * 1.01 + 1.0

    best_res: Optional[TopoDS_Shape] = None
    best_int = 0
    best_valid = False

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
                    if fz == 0.0:
                        int_cnt = _count_internal_edges(res, outer_only=True)
                        if int_cnt == 0:
                            return res
                        best_res = res
                        best_int = int_cnt
                        best_valid = bool(
                            make_brep_check_analyzer(res, geom_controls=False, parallel=True).IsValid()
                        )
                        continue
                    if best_res is not None:
                        if fz > 1e-4:
                            return best_res
                        int_cnt = _count_internal_edges(res, outer_only=True)
                        cand_valid = bool(
                            make_brep_check_analyzer(res, geom_controls=False, parallel=True).IsValid()
                        )
                        if int_cnt < best_int and not (best_valid and not cand_valid):
                            best_res = res
                            best_int = int_cnt
                            best_valid = cand_valid
                            if best_int == 0:
                                return best_res
                        continue
                    return res
        except Exception:
            continue
    return best_res


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

    def _run_batch(fz: float) -> Optional[TopoDS_Shape]:
        args = ShapeList()
        args.Append(shapes[0])
        tools = ShapeList()
        for s in shapes[1:]:
            tools.Append(s)
        fuse = BRepAlgoAPI_Fuse()
        fuse.SetArguments(args)
        fuse.SetTools(tools)
        fuse.SetRunParallel(True)
        fuse.SetNonDestructive(True)
        if fz > 0.0:
            fuse.SetFuzzyValue(float(fz))
        fuse.Build()
        if fuse.IsDone() and not fuse.Shape().IsNull():
            res = fuse.Shape()
            sols = _extract_solids(res)
            vr = abs(_shape_volume(res))
            if sols and vr >= max_v * 0.99 and vr <= sum_v * 1.01 + 1.0:
                return res
        return None

    # Attempt fast single-pass multi-tool batch fuse at exact fuzzy=0.0 first.
    # Never blindly degrade the batch to fuzzy>0; however, if fuzzy=0.0 leaves
    # TopAbs_INTERNAL edges on outer shells (from nanometer NURBS/cylinder tangency gaps
    # that STEP AP214 drops into self-intersections) or sub-Parasolid (< 1e-5 mm) micro-edges,
    # test ultra-tight fuzzy (5e-5, 1e-4) and upgrade ONLY if validity is preserved and the
    # outer-shell defect tuple strictly improves.
    if cancel_check and cancel_check():
        raise InterruptedError("Operation cancelled by user.")
    try:
        res0 = _run_batch(0.0)
        if res0 is not None:
            sols0 = _extract_solids(res0)
            m0 = _inspect_manifold_topology(res0, outer_only=True)
            best_res = res0
            best_fz = 0.0
            best_score = (m0.internal_edge_count, m0.sub_parasolid_count, len(m0.nm_edges))

            if m0.internal_edge_count > 0 or m0.sub_parasolid_count > 0:
                brep_ok0 = bool(
                    make_brep_check_analyzer(res0, geom_controls=False, parallel=True).IsValid()
                )
                for fz in (5e-5, 1e-4):
                    if cancel_check and cancel_check():
                        raise InterruptedError("Operation cancelled by user.")
                    cand = _run_batch(fz)
                    if cand is None:
                        continue
                    brep_ok_c = bool(
                        make_brep_check_analyzer(cand, geom_controls=False, parallel=True).IsValid()
                    )
                    if brep_ok0 and not brep_ok_c:
                        continue
                    mc = _inspect_manifold_topology(cand, outer_only=True)
                    cand_score = (mc.internal_edge_count, mc.sub_parasolid_count, len(mc.nm_edges))
                    if cand_score < best_score:
                        best_res = cand
                        best_fz = fz
                        best_score = cand_score
                        if best_score[0] == 0 and best_score[1] == 0:
                            break

            best_sols = _extract_solids(best_res)
            if best_fz > 0.0:
                logs.append(
                    f"{label} batch fuse succeeded ({len(shapes)} -> {len(best_sols)} solid(s), "
                    f"fuzzy={best_fz:g}; cleared internal/sub-precision edges "
                    f"{m0.internal_edge_count}/{m0.sub_parasolid_count} -> {best_score[0]}/{best_score[1]})"
                )
            else:
                logs.append(
                    f"{label} batch fuse succeeded ({len(shapes)} -> {len(sols0)} solid(s), fuzzy=0.0)"
                )
            return best_res
    except InterruptedError:
        raise
    except Exception:
        pass

    # Spatial-proximity balanced binary-tree fallback (O(N log N) face work vs O(N^2) linear chain)
    def _pair_by_proximity(layer: list[TopoDS_Shape]) -> list[tuple[TopoDS_Shape, Optional[TopoDS_Shape]]]:
        n = len(layer)
        if n <= 2:
            return [(layer[0], layer[1] if n == 2 else None)]
        centers = []
        for s in layer:
            bmin_s, bmax_s = _shape_bbox(s)
            centers.append(0.5 * (bmin_s + bmax_s))
        pts = np.vstack(centers)
        used = [False] * n
        pairs: list[tuple[TopoDS_Shape, Optional[TopoDS_Shape]]] = []
        for i in range(n):
            if used[i]:
                continue
            used[i] = True
            best_j = -1
            best_d2 = float("inf")
            for j in range(i + 1, n):
                if used[j]:
                    continue
                diff = pts[j] - pts[i]
                d2 = float(diff[0] * diff[0] + diff[1] * diff[1] + diff[2] * diff[2])
                if d2 < best_d2:
                    best_d2 = d2
                    best_j = j
            if best_j >= 0:
                used[best_j] = True
                pairs.append((layer[i], layer[best_j]))
            else:
                pairs.append((layer[i], None))
        return pairs

    layer = list(shapes)
    total_merges = len(shapes) - 1
    merges_done = 0
    failed = 0
    p0, p1 = prog_range

    while len(layer) > 1:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        pairs = _pair_by_proximity(layer)
        next_layer: list[TopoDS_Shape] = []
        round_merged = 0
        for s_a, s_b in pairs:
            if cancel_check and cancel_check():
                raise InterruptedError("Operation cancelled by user.")
            if s_b is None:
                next_layer.append(s_a)
                continue
            merged = _fuse_two_safe(s_a, s_b)
            if merged is not None:
                next_layer.append(merged)
                round_merged += 1
                merges_done += 1
                if progress_cb:
                    progress_cb(
                        p0 + (p1 - p0) * min(1.0, merges_done / max(total_merges, 1)),
                        f"{label} tree fuse ({merges_done}/{total_merges})...",
                    )
            else:
                next_layer.append(s_a)
                next_layer.append(s_b)

        if round_merged == 0:
            # Final sequential rescue pass for any stubborn pairs that couldn't pair by proximity
            acc = next_layer[0]
            for nxt in next_layer[1:]:
                if cancel_check and cancel_check():
                    raise InterruptedError("Operation cancelled by user.")
                merged = _fuse_two_safe(acc, nxt)
                if merged is not None:
                    acc = merged
                    merges_done += 1
                else:
                    failed += 1
            layer = [acc]
            break
        layer = next_layer

    acc = layer[0]
    if failed > 0:
        logs.append(f"{label} tree fuse completed ({total_merges + 1 - failed}/{total_merges + 1} succeeded, {failed} skipped)")
    else:
        logs.append(f"{label} tree fuse completed ({total_merges + 1}/{total_merges + 1} succeeded)")
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

    # Batch-mesh main_solid + all islands together across all CPU cores
    all_bridge_shapes: list[TopoDS_Shape] = [main_solid] + list(islands)
    mesh_shapes_in_parallel_buckets(
        all_bridge_shapes, [0.25] * len(all_bridge_shapes), cancel_check=cancel_check
    )

    # Build fast KDTree on main_solid surface vertices
    mv, _, _, _ = extract_shape_triangles(main_solid)
    if len(mv) == 0:
        return main_solid, islands
    main_tree = cKDTree(mv)

    remaining: list[TopoDS_Solid] = []
    connected = 0

    for isl in islands:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        if _is_solid_inside(isl, main_solid):
            continue

        iv, _, _, _ = extract_shape_triangles(isl)
        if len(iv) == 0:
            continue
        dists, idxs = main_tree.query(iv, k=1, workers=-1)
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
        logs.append(f"Bridged {connected} clearance-separated outer part(s) via hidden internal B-Rep pins")

    return main_solid, remaining


def _count_lacking_wires(shape: TopoDS_Shape) -> int:
    """Count faces with 2D seam/pcurve closure gaps (CheckLacking) that break OCCT 7.x viewers."""
    lacking = 0
    ef = TopExp_Explorer(shape, TopAbs_FACE)
    while ef.More():
        f = to_face(ef.Current())
        ew = TopExp_Explorer(f, TopAbs_WIRE)
        while ew.More():
            try:
                if ShapeAnalysis_Wire(to_wire(ew.Current()), f, 1e-4).CheckLacking():
                    lacking += 1
            except Exception:
                pass
            ew.Next()
        ef.Next()
    return lacking


def _split_edge_exact_subedges(edge: TopoDS_Edge, n: int = 2) -> Optional[TopoDS_Wire]:
    """Split a 3D curve edge into n exact sub-edges in a TopoDS_Wire (0.0 geometric error)."""
    try:
        ac = BRepAdaptor_Curve(edge)
        f_par = ac.FirstParameter()
        l_par = ac.LastParameter()
        if (l_par - f_par) <= 1e-9:
            return None
        loc = TopLoc_Location()
        c3d = BRep_Tool.Curve_s(edge, loc, f_par, l_par)
        if c3d is None:
            return None

        v_first = TopExp.FirstVertex_s(edge, False)
        v_last = TopExp.LastVertex_s(edge, False)
        trsf = loc.Transformation()
        params = [f_par + (l_par - f_par) * k / n for k in range(n + 1)]
        verts = (
            [v_first]
            + [BRepBuilderAPI_MakeVertex(c3d.Value(pv).Transformed(trsf)).Vertex() for pv in params[1:-1]]
            + [v_last]
        )

        builder = BRep_Builder()
        wire = TopoDS_Wire()
        builder.MakeWire(wire)
        c_world = c3d.Transformed(trsf) if not loc.IsIdentity() else c3d
        tol = BRep_Tool.Tolerance_s(edge)
        for k in range(n):
            me = BRepBuilderAPI_MakeEdge(c_world, verts[k], verts[k + 1], params[k], params[k + 1])
            if not me.IsDone():
                return None
            sub_e = me.Edge()
            builder.UpdateEdge(sub_e, tol)
            builder.Add(wire, sub_e)
        return wire
    except Exception:
        return None


def _cad_assistant_deflection_suite(shape: TopoDS_Shape) -> list[tuple[str, float, bool, float]]:
    """Return OCCT/CAD Assistant Prs3d deflection profiles (both relative and absolute)."""
    bmin, bmax = _shape_bbox(shape)
    max_dim = max(float(np.max(bmax - bmin)), 1.0)
    return [
        ("rel0.001_0.50", 0.001, True, 0.50),
        ("rel0.001_0.35", 0.001, True, 0.35),
        ("abs_fine_0.35", max(1e-3, max_dim * 0.001), False, 0.35),
        ("abs_med_0.35", max(1e-3, max_dim * 0.002), False, 0.35),
        ("abs_prs3d_0.35", max(1e-3, max_dim * 0.004), False, 0.35),
    ]


def _audit_prs_closed_meshing(
    shape: TopoDS_Shape,
    fast: bool = False,
) -> tuple[int, set[int]]:
    """Audit BRepMesh + StdPrs_ShadedShape::ExploreSolids across CAD Assistant deflections.

    Returns (failed_profile_count, union_of_failing_face_indices).
    If even 1 face returns NULL triangulation, OCCT's StdPrs_ShadedShape::ExploreSolids
    reclassifies the ENTIRE solid as Opened, disabling Clipping Plane Capping in CAD Assistant.
    When fast=True, tests the two bounding profiles (rel0.001_0.50 and abs_prs3d_0.35).
    """
    suite = _cad_assistant_deflection_suite(shape)
    if fast:
        suite = [suite[0], suite[-1]]

    failing_faces: set[int] = set()
    failed_profiles = 0
    for _, lin, rel, ang in suite:
        BRepTools.Clean_s(shape)
        BRepMesh_IncrementalMesh(shape, lin, rel, ang, True).Perform()

        profile_bad = False
        ef = TopExp_Explorer(shape, TopAbs_FACE)
        idx = 0
        while ef.More():
            tri = BRep_Tool.Triangulation_s(to_face(ef.Current()), TopLoc_Location())
            if tri is None or tri.NbTriangles() == 0:
                failing_faces.add(idx)
                profile_bad = True
            idx += 1
            ef.Next()

        if not profile_bad:
            try:
                if not StdPrs_ToolTriangulatedShape.IsTriangulated_s(shape):
                    profile_bad = True
                else:
                    c_cl = TopoDS_Compound()
                    c_op = TopoDS_Compound()
                    b = BRep_Builder()
                    b.MakeCompound(c_cl)
                    b.MakeCompound(c_op)
                    StdPrs_ShadedShape.ExploreSolids_s(shape, b, c_cl, c_op, True)
                    if TopExp_Explorer(c_op, TopAbs_FACE).More():
                        profile_bad = True
            except Exception:
                pass

        if profile_bad:
            failed_profiles += 1

    BRepTools.Clean_s(shape)
    return failed_profiles, failing_faces


def _normalize_via_step_3d_curves(shape: TopoDS_Shape) -> TopoDS_Shape:
    """Round-trip through clean 3D-curve STEP so stale in-memory 2D pcurves are rebuilt cleanly."""
    from simple_3d_step.core.step_io import save_shape_to_step

    tmp_dir = Path(tempfile.mkdtemp(prefix="s3d_norm_"))
    try:
        tmp_step = tmp_dir / "norm.step"
        save_shape_to_step(shape, tmp_step)
        rdr = STEPControl_Reader()
        if rdr.ReadFile(str(tmp_step)) == IFSelect_RetDone:
            rdr.TransferRoots()
            reloaded = rdr.OneShape()
            if not reloaded.IsNull():
                sols = _extract_solids(reloaded)
                if len(sols) == 1 and shape.ShapeType() == TopAbs_SOLID:
                    BRepLib.OrientClosedSolid_s(sols[0])
                    BRepTools.Clean_s(sols[0])
                    return sols[0]
                BRepTools.Clean_s(reloaded)
                return reloaded
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return shape


def _unique_wire_vertices(wire: TopoDS_Wire) -> list:
    verts = []
    ve = TopExp_Explorer(wire, TopAbs_VERTEX)
    while ve.More():
        v = to_vertex(ve.Current())
        if not any(v.IsSame(u) for u in verts):
            verts.append(v)
        ve.Next()
    return verts


def _wire_centroid_on_plane(wire: TopoDS_Wire, pln: gp_Pln) -> Optional[gp_Pnt]:
    """Compute a robust interior centroid of a closed wire projected onto ``pln``."""
    sx = sy = sz = 0.0
    cnt = 0
    ee = TopExp_Explorer(wire, TopAbs_EDGE)
    while ee.More():
        ed = to_edge(ee.Current())
        if not BRep_Tool.Degenerated_s(ed):
            try:
                ac = BRepAdaptor_Curve(ed)
                u0, u1 = ac.FirstParameter(), ac.LastParameter()
                for k in range(8):
                    p = ac.Value(u0 + (u1 - u0) * (k / 8.0))
                    sx += p.X()
                    sy += p.Y()
                    sz += p.Z()
                    cnt += 1
            except Exception:
                pass
        ee.Next()
    if cnt == 0:
        return None
    raw = gp_Pnt(sx / cnt, sy / cnt, sz / cnt)
    loc = pln.Location()
    n = pln.Axis().Direction()
    dist = (
        (raw.X() - loc.X()) * n.X()
        + (raw.Y() - loc.Y()) * n.Y()
        + (raw.Z() - loc.Z()) * n.Z()
    )
    return gp_Pnt(raw.X() - dist * n.X(), raw.Y() - dist * n.Y(), raw.Z() - dist * n.Z())


def _split_tangent_pinch_wire_faces(
    shape: TopoDS_Shape,
    bad_faces: set[int],
) -> tuple[Optional[TopoDS_Shape], int]:
    """Split planar mesh-failing faces where an inner hole wire is tangent to the outer wire.

    In OCCT's BRepMesh, an inner hole wire touching the outer boundary wire at a single tangent
    pinch-point triggers ``IMeshData_SelfIntersectingWire | IMeshData_Failure`` (0 triangles)
    after STEP import tolerance clamping, causing CAD Assistant to drop the entire face and
    downgrade the solid to an opened shell.

    Splitting the face with local exact chords passing through the tangent hole's centroid
    parallel to the tangent line separates the pinch-point into clean single-wire sub-faces
    with 0.0 mm geometric distortion.
    """
    cand = BRepBuilderAPI_Copy(shape).Shape()

    # Pass 1: Unify any coincident unmerged vertices between a tangent inner wire and the
    # outer wire on failing planar faces so BRepFeat_SplitShape splits the pinch cleanly into
    # two non-pinched crescent faces rather than leaving a figure-8 bow-tie face.
    vertex_reshape = ShapeBuild_ReShape()
    merged_verts = 0
    ef = TopExp_Explorer(cand, TopAbs_FACE)
    idx = 0
    while ef.More():
        if idx in bad_faces:
            f = to_face(ef.Current())
            if BRepAdaptor_Surface(f, False).GetType() == GeomAbs_Plane:
                outer_w = BRepTools.OuterWire_s(f)
                outer_verts = _unique_wire_vertices(outer_w)
                we = TopExp_Explorer(f, TopAbs_WIRE)
                while we.More():
                    iw = to_wire(we.Current())
                    if not iw.IsSame(outer_w):
                        for vi in _unique_wire_vertices(iw):
                            pi = BRep_Tool.Pnt_s(vi)
                            for vo in outer_verts:
                                if not vi.IsSame(vo) and pi.Distance(BRep_Tool.Pnt_s(vo)) < 1e-3:
                                    vertex_reshape.Replace(
                                        vi.Oriented(TopAbs_FORWARD),
                                        vo.Oriented(TopAbs_FORWARD),
                                    )
                                    merged_verts += 1
                                    break
                    we.Next()
        idx += 1
        ef.Next()

    if merged_verts > 0:
        cand = vertex_reshape.Apply(cand)

    # Pass 2: Collect exact local chord edges cutting each tangent inner wire on failing faces.
    split_ops: list[tuple[TopoDS_Edge, TopoDS_Face]] = []
    pinch_holes = 0
    ef2 = TopExp_Explorer(cand, TopAbs_FACE)
    idx = 0
    while ef2.More():
        if idx in bad_faces:
            f = to_face(ef2.Current())
            surf = BRepAdaptor_Surface(f, False)
            if surf.GetType() == GeomAbs_Plane:
                face_pln = surf.Plane()
                fn = face_pln.Axis().Direction()
                box = Bnd_Box()
                BRepBndLib.Add_s(f, box, False)
                if not box.IsVoid():
                    pmin, pmax = box.CornerMin(), box.CornerMax()
                    span = max(pmin.Distance(pmax) * 1.5, 20.0)
                else:
                    span = 100.0

                outer_w = BRepTools.OuterWire_s(f)
                we = TopExp_Explorer(f, TopAbs_WIRE)
                while we.More():
                    iw = to_wire(we.Current())
                    if not iw.IsSame(outer_w):
                        ext = BRepExtrema_DistShapeShape(iw, outer_w)
                        if ext.IsDone() and ext.NbSolution() > 0 and ext.Value() < 1e-2:
                            p_pinch = ext.PointOnShape1(1)
                            c_hole = _wire_centroid_on_plane(iw, face_pln)
                            if c_hole is not None:
                                v_raw = gp_Vec(c_hole, p_pinch)
                                dot_n = v_raw.X() * fn.X() + v_raw.Y() * fn.Y() + v_raw.Z() * fn.Z()
                                v_in_pln = gp_Vec(
                                    v_raw.X() - dot_n * fn.X(),
                                    v_raw.Y() - dot_n * fn.Y(),
                                    v_raw.Z() - dot_n * fn.Z(),
                                )
                                if v_in_pln.Magnitude() > 1e-6:
                                    cut_pln = gp_Pln(c_hole, gp_Dir(v_in_pln))
                                    cut_face = BRepBuilderAPI_MakeFace(
                                        cut_pln, -span, span, -span, span
                                    ).Face()
                                    sec = BRepAlgoAPI_Section(f, cut_face, False)
                                    sec.ComputePCurveOn1(True)
                                    sec.Approximation(True)
                                    sec.Build()
                                    if sec.IsDone():
                                        added_here = 0
                                        se = TopExp_Explorer(sec.Shape(), TopAbs_EDGE)
                                        while se.More():
                                            ed = to_edge(se.Current())
                                            if not BRep_Tool.Degenerated_s(ed):
                                                v1 = TopExp.FirstVertex_s(ed, False)
                                                v2 = TopExp.LastVertex_s(ed, False)
                                                d1 = BRepExtrema_DistShapeShape(v1, iw)
                                                d2 = BRepExtrema_DistShapeShape(v2, iw)
                                                dist1 = d1.Value() if (d1.IsDone() and d1.NbSolution() > 0) else 1e9
                                                dist2 = d2.Value() if (d2.IsDone() and d2.NbSolution() > 0) else 1e9
                                                if min(dist1, dist2) < 0.05:
                                                    split_ops.append((ed, f))
                                                    added_here += 1
                                            se.Next()
                                        if added_here > 0:
                                            pinch_holes += 1
                    we.Next()
        idx += 1
        ef2.Next()

    if not split_ops:
        return None, 0

    feat = BRepFeat_SplitShape(cand)
    for ed, f in split_ops:
        feat.Add(ed, f)
    feat.Build()
    if not feat.IsDone():
        return None, 0

    sf = ShapeFix_Shape(feat.Shape())
    sf.SetPrecision(1e-4)
    sf.SetMaxTolerance(1e-3)
    sf.Perform()
    sols = _extract_solids(sf.Shape())
    repaired: TopoDS_Shape = sols[0] if len(sols) == 1 else sf.Shape()
    for s in sols:
        BRepLib.OrientClosedSolid_s(s)
    BRepTools.Clean_s(repaired)
    repaired = _normalize_via_step_3d_curves(repaired)
    return repaired, pinch_holes


def _build_non_manifold_bead(edge: TopoDS_Edge, radius: float = 0.05) -> Optional[TopoDS_Shape]:
    """Build a microscopic analytic capsule/pipe bead along a non-manifold edge.

    Eliminates zero-thickness (4-face checkerboard) non-manifold contacts that Parasolid
    (SolidWorks) strictly forbids in a MANIFOLD_SOLID_BREP solid body, while keeping geometric
    deviation microscopic (radius ~0.05 mm) and strictly preserving single-solid connectivity.
    """
    try:
        p1 = BRep_Tool.Pnt_s(TopExp.FirstVertex_s(edge, False))
        p2 = BRep_Tool.Pnt_s(TopExp.LastVertex_s(edge, False))
        ac = BRepAdaptor_Curve(edge)
        parts: list[TopoDS_Shape] = []

        if ac.GetType() == GeomAbs_Line:
            vec = gp_Vec(p1, p2)
            length = vec.Magnitude()
            if length > 1e-6:
                parts.append(
                    BRepPrimAPI_MakeCylinder(gp_Ax2(p1, gp_Dir(vec)), radius, length).Solid()
                )
            parts.append(BRepPrimAPI_MakeSphere(p1, radius).Solid())
            if p1.Distance(p2) > 1e-6:
                parts.append(BRepPrimAPI_MakeSphere(p2, radius).Solid())
        else:
            piped = False
            try:
                u0 = ac.FirstParameter()
                pt0 = gp_Pnt()
                tan0 = gp_Vec()
                ac.D1(u0, pt0, tan0)
                if tan0.Magnitude() > 1e-9:
                    circ = gp_Circ(gp_Ax2(pt0, gp_Dir(tan0)), radius)
                    prof_w = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire()
                    prof_f = BRepBuilderAPI_MakeFace(prof_w, True).Face()
                    spine = BRepBuilderAPI_MakeWire(edge).Wire()
                    pipe = BRepOffsetAPI_MakePipe(spine, prof_f).Shape()
                    if not pipe.IsNull() and _extract_solids(pipe):
                        parts.append(pipe)
                        piped = True
            except Exception:
                piped = False

            if not piped:
                # Fallback: overlapping spheres along the curve
                u0, u1 = ac.FirstParameter(), ac.LastParameter()
                lp = GProp_GProps()
                BRepGProp.LinearProperties_s(edge, lp)
                n_pts = max(3, min(32, int(math.ceil(abs(float(lp.Mass())) / max(radius, 1e-3))) + 1))
                for k in range(n_pts):
                    u = u0 + (u1 - u0) * (k / max(n_pts - 1, 1))
                    parts.append(BRepPrimAPI_MakeSphere(ac.Value(u), radius).Solid())
            else:
                parts.append(BRepPrimAPI_MakeSphere(p1, radius).Solid())
                if p1.Distance(p2) > 1e-6:
                    parts.append(BRepPrimAPI_MakeSphere(p2, radius).Solid())

        if not parts:
            return None
        acc = parts[0]
        for nxt in parts[1:]:
            fu = BRepAlgoAPI_Fuse(acc, nxt)
            fu.SetRunParallel(True)
            fu.SetNonDestructive(True)
            fu.Build()
            if fu.IsDone() and not fu.Shape().IsNull():
                acc = fu.Shape()
        return acc
    except Exception:
        return None


def _heal_non_manifold_topology(
    shape: TopoDS_Shape,
    logs: list[str],
    radii: tuple[float, ...] = (0.05, 0.10),
    max_entities: int = 64,
) -> TopoDS_Shape:
    """Resolve non-manifold edges (>2 incident faces) and pinched non-manifold vertices.

    Parasolid (SolidWorks) strictly requires every edge in a MANIFOLD_SOLID_BREP to have
    exactly 2 incident faces and every vertex to have a single edge-connected face fan.
    Fusing a microscopic analytic capsule/sphere (r=0.05 mm) over each zero-thickness pinch
    converts it into clean 2-manifold geometry with negligible volume change (< 0.01%).
    """
    current = shape
    m_base = _inspect_manifold_topology(current)
    if not m_base.nm_edges and not m_base.nm_verts:
        return current

    rep_base = validate_shape(current)
    orig_nm_e = len(m_base.nm_edges)
    orig_nm_v = len(m_base.nm_verts)

    for radius in radii:
        m_cur = _inspect_manifold_topology(current)
        if not m_cur.nm_edges and not m_cur.nm_verts:
            break

        beads: list[TopoDS_Shape] = []
        for ed in m_cur.nm_edges[:max_entities]:
            b = _build_non_manifold_bead(ed, radius=radius)
            if b is not None:
                beads.append(b)
        rem_slots = max(0, max_entities - len(beads))
        for v in m_cur.nm_verts[:rem_slots]:
            try:
                beads.append(BRepPrimAPI_MakeSphere(BRep_Tool.Pnt_s(v), radius).Solid())
            except Exception:
                pass

        if not beads:
            break

        cand: Optional[TopoDS_Shape] = None
        # Attempt single batch fuse of all beads first
        try:
            args = ShapeList()
            args.Append(current)
            tools = ShapeList()
            for b in beads:
                tools.Append(b)
            fu = BRepAlgoAPI_Fuse()
            fu.SetArguments(args)
            fu.SetTools(tools)
            fu.SetRunParallel(True)
            fu.SetNonDestructive(True)
            fu.Build()
            if fu.IsDone() and not fu.Shape().IsNull():
                sols = _extract_solids(fu.Shape())
                if len(sols) == rep_base.solid_count:
                    cleaned_sols = []
                    for s in sols:
                        sf, _, _ = _remove_closed_void_shells(s)
                        BRepLib.OrientClosedSolid_s(sf)
                        cleaned_sols.append(sf)
                    cand = cleaned_sols[0] if len(cleaned_sols) == 1 else fu.Shape()
        except Exception:
            cand = None

        # Sequential fallback if batch fuse failed on multiple beads
        if cand is None and len(beads) > 1:
            acc = current
            for b in beads:
                try:
                    fu = BRepAlgoAPI_Fuse(acc, b)
                    fu.SetRunParallel(True)
                    fu.SetNonDestructive(True)
                    fu.Build()
                    if fu.IsDone() and not fu.Shape().IsNull():
                        sols = _extract_solids(fu.Shape())
                        if len(sols) == rep_base.solid_count:
                            sf, _, _ = _remove_closed_void_shells(sols[0])
                            BRepLib.OrientClosedSolid_s(sf)
                            acc = sf if len(sols) == 1 else fu.Shape()
                except Exception:
                    continue
            if not acc.IsSame(current):
                cand = acc

        if cand is None:
            continue

        BRepTools.Clean_s(cand)
        rep_c = validate_shape(cand)
        m_c = _inspect_manifold_topology(cand)
        if (
            rep_c.solid_count == rep_base.solid_count
            and rep_c.shell_count <= rep_base.shell_count
            and rep_c.is_watertight
            and not (rep_base.is_valid and not rep_c.is_valid)
            and abs(rep_c.volume - rep_base.volume) / max(rep_base.volume, 1.0) < 0.01
            and (len(m_c.nm_edges), len(m_c.nm_verts)) < (len(m_cur.nm_edges), len(m_cur.nm_verts))
        ):
            current = cand
            rep_base = rep_c

    m_fin = _inspect_manifold_topology(current)
    if (len(m_fin.nm_edges), len(m_fin.nm_verts)) < (orig_nm_e, orig_nm_v):
        logs.append(
            f"Healed non-manifold zero-thickness topology for SolidWorks/Parasolid compatibility "
            f"(non-manifold edges {orig_nm_e} -> {len(m_fin.nm_edges)}, "
            f"vertices {orig_nm_v} -> {len(m_fin.nm_verts)})"
        )
    return current


def _collapse_sub_precision_edges(
    shape: TopoDS_Shape,
    logs: list[str],
    min_length: float = 5e-5,
) -> TopoDS_Shape:
    """Eliminate edges shorter than Parasolid's linear precision (1e-5 mm, with 5x margin).

    Microscopic edges (e.g. 6.3e-6 mm from nanometer-level mating misalignments) collapse to
    degenerate zero-length edges in Parasolid/SolidWorks STEP import and break solid knitting.
    """
    m_base = _inspect_manifold_topology(shape, tiny_threshold=min_length)
    if m_base.tiny_edge_count == 0:
        return shape

    rep_base = validate_shape(shape)
    lacking_base = _count_lacking_wires(shape)
    try:
        sfw = ShapeFix_Wireframe(shape)
        sfw.SetPrecision(min_length)
        sfw.SetMaxTolerance(max(1e-4, 2.0 * min_length))
        sfw.ModeDropSmallEdges = True
        sfw.FixSmallEdges()
        fixed_wf = sfw.Shape()
        if fixed_wf.IsNull():
            return shape

        sf = ShapeFix_Shape(fixed_wf)
        sf.SetPrecision(1e-4)
        sf.SetMaxTolerance(1e-3)
        sf.Perform()
        sols = _extract_solids(sf.Shape())
        cand: TopoDS_Shape = sols[0] if len(sols) == 1 else sf.Shape()
        for s in sols:
            BRepLib.OrientClosedSolid_s(s)
        BRepTools.Clean_s(cand)
        cand = _normalize_via_step_3d_curves(cand)

        rep_c = validate_shape(cand)
        lacking_c = _count_lacking_wires(cand)
        m_c = _inspect_manifold_topology(cand, tiny_threshold=min_length)
        max_tol_limit = max(m_base.max_edge_tol * 1.25, 1e-3)
        if (
            rep_c.solid_count == rep_base.solid_count
            and rep_c.shell_count <= rep_base.shell_count
            and rep_c.is_watertight
            and not (rep_base.is_valid and not rep_c.is_valid)
            and abs(rep_c.volume - rep_base.volume) / max(rep_base.volume, 1.0) < 0.01
            and lacking_c <= lacking_base
            and m_c.max_edge_tol <= max_tol_limit
            and len(m_c.nm_edges) <= len(m_base.nm_edges)
            and len(m_c.nm_verts) <= len(m_base.nm_verts)
            and m_c.tiny_edge_count < m_base.tiny_edge_count
        ):
            logs.append(
                f"Collapsed sub-precision micro-edges (< {min_length:.0e} mm) for SolidWorks/Parasolid "
                f"compatibility ({m_base.tiny_edge_count} -> {m_c.tiny_edge_count})"
            )
            return cand
    except Exception:
        pass
    return shape


def _heal_mesh_blocking_faces(
    shape: TopoDS_Shape,
    logs: list[str],
) -> TopoDS_Shape:
    """Eliminate faces that fail BRepMesh so CAD Assistant Clipping Capping stays enabled.

    Audits first and returns immediately if all faces already mesh cleanly (never needlessly
    collapsing thin valid CAD features or bloating edge tolerances). When unmeshed faces exist:
    Step A: Progressive tight ShapeFix_FixSmallFace (1e-3 -> 5e-3 -> 1.5e-2 mm) guarded
            against tolerance bloat and non-manifold regressions.
    Step B: Targeted exact midpoint splitting of long curved edges ONLY on remaining
            mesh-failing faces (0 extra faces, 0.0 mm geometric distortion).
    Step C: Exact local chord splitting of planar faces whose inner hole wire touches the outer
            boundary wire at a tangent pinch-point (0.0 mm geometric distortion, resolves OCCT
            IMeshData_SelfIntersectingWire on tangent holes).
    """
    current = shape

    # Scan key CAD Assistant deflections FIRST — never touch faces when already 100% clean
    fails_before, bad_faces = _audit_prs_closed_meshing(current, fast=True)
    if fails_before == 0 and not bad_faces:
        logs.append("CAD Assistant closed-mesh capping audit passed (0 unmeshed faces)")
        return current

    rep_base = validate_shape(current)
    lacking_base = _count_lacking_wires(current)
    m_base = _inspect_manifold_topology(current)

    # Step A: Progressive tight sliver collapse — only accept if unmeshed faces strictly improve
    # and edge tolerances do NOT bloat (prevents deleting real 0.01 mm features and creating
    # multi-micron 3D curve-to-surface gaps that break SolidWorks solid knitting).
    for prec in (1e-3, 5e-3, 1.5e-2):
        if fails_before == 0 and not bad_faces:
            break
        try:
            sfsf = ShapeFix_FixSmallFace()
            sfsf.Init(current)
            sfsf.SetPrecision(prec)
            sfsf.SetMaxTolerance(prec)
            sfsf.Perform()
            fixed_small = sfsf.Shape()

            if fixed_small.IsSame(current):
                continue

            sf = ShapeFix_Shape(fixed_small)
            sf.SetPrecision(1e-4)
            sf.SetMaxTolerance(1e-3)
            sf.Perform()

            sols = _extract_solids(sf.Shape())
            cand_a: TopoDS_Shape = sols[0] if len(sols) == 1 else sf.Shape()
            for s in sols:
                BRepLib.OrientClosedSolid_s(s)
            BRepTools.Clean_s(cand_a)
            cand_a = _normalize_via_step_3d_curves(cand_a)

            rep_a = validate_shape(cand_a)
            lacking_a = _count_lacking_wires(cand_a)
            m_a = _inspect_manifold_topology(cand_a)
            max_tol_limit = max(m_base.max_edge_tol * 1.25, 1e-3)
            if (
                rep_a.solid_count == rep_base.solid_count
                and rep_a.shell_count <= rep_base.shell_count
                and rep_a.is_watertight
                and not (rep_base.is_valid and not rep_a.is_valid)
                and abs(rep_a.volume - rep_base.volume) / max(rep_base.volume, 1.0) < 0.01
                and lacking_a <= lacking_base
                and m_a.max_edge_tol <= max_tol_limit
                and len(m_a.nm_edges) <= len(m_base.nm_edges)
                and len(m_a.nm_verts) <= len(m_base.nm_verts)
                and m_a.sub_parasolid_count <= m_base.sub_parasolid_count
            ):
                fails_a, bad_a = _audit_prs_closed_meshing(cand_a, fast=True)
                if fails_a < fails_before or len(bad_a) < len(bad_faces):
                    removed_slivers = max(0, rep_base.face_count - rep_a.face_count)
                    if removed_slivers > 0:
                        logs.append(
                            f"Cleaned {removed_slivers} microscopic sliver face(s) at {prec:g} mm "
                            f"({rep_a.face_count:,} faces remaining, unmeshed {len(bad_faces)} -> {len(bad_a)})"
                        )
                    current = cand_a
                    rep_base = rep_a
                    lacking_base = lacking_a
                    m_base = m_a
                    fails_before = fails_a
                    bad_faces = bad_a
        except Exception:
            pass

    if fails_before == 0 and not bad_faces:
        logs.append("CAD Assistant closed-mesh capping audit passed (0 unmeshed faces)")
        return current

    # Step B: Targeted exact midpoint split of curved edges ONLY on remaining mesh-failing faces
    if bad_faces:
        try:
            edges_to_split = ShapeIndexedMap()
            ef = TopExp_Explorer(current, TopAbs_FACE)
            idx = 0
            while ef.More():
                f = to_face(ef.Current())
                if idx in bad_faces:
                    sp = GProp_GProps()
                    BRepGProp.SurfaceProperties_s(f, sp)
                    min_edge_len = 10.0 if abs(sp.Mass()) >= 0.01 else 0.5
                    ee = TopExp_Explorer(f, TopAbs_EDGE)
                    while ee.More():
                        ed = to_edge(ee.Current())
                        if not BRep_Tool.Degenerated_s(ed):
                            ac = BRepAdaptor_Curve(ed)
                            gp = GProp_GProps()
                            BRepGProp.LinearProperties_s(ed, gp)
                            if gp.Mass() > min_edge_len and ac.GetType() != GeomAbs_Line:
                                edges_to_split.Add(ed)
                        ee.Next()
                idx += 1
                ef.Next()

            if edges_to_split.Extent() > 0:
                reshape = ShapeBuild_ReShape()
                split_cnt = 0
                for i in range(1, edges_to_split.Extent() + 1):
                    ed_fwd = to_edge(edges_to_split.FindKey(i).Oriented(TopAbs_FORWARD))
                    w_rep = _split_edge_exact_subedges(ed_fwd, n=2)
                    if w_rep is not None:
                        reshape.Replace(ed_fwd, w_rep)
                        split_cnt += 1

                if split_cnt > 0:
                    sf = ShapeFix_Shape(reshape.Apply(current))
                    sf.SetPrecision(1e-4)
                    sf.SetMaxTolerance(1e-3)
                    sf.Perform()
                    sols = _extract_solids(sf.Shape())
                    cand_b: TopoDS_Shape = sols[0] if len(sols) == 1 else sf.Shape()
                    for s in sols:
                        BRepLib.OrientClosedSolid_s(s)
                    BRepTools.Clean_s(cand_b)
                    cand_b = _normalize_via_step_3d_curves(cand_b)

                    rep_b = validate_shape(cand_b)
                    lacking_b = _count_lacking_wires(cand_b)
                    m_b = _inspect_manifold_topology(cand_b)
                    if (
                        rep_b.solid_count == rep_base.solid_count
                        and rep_b.shell_count <= rep_base.shell_count
                        and rep_b.is_watertight
                        and not (rep_base.is_valid and not rep_b.is_valid)
                        and abs(rep_b.volume - rep_base.volume) / max(rep_base.volume, 1.0) < 0.01
                        and lacking_b <= lacking_base
                        and len(m_b.nm_edges) <= len(m_base.nm_edges)
                        and len(m_b.nm_verts) <= len(m_base.nm_verts)
                    ):
                        fails_b, bad_b = _audit_prs_closed_meshing(cand_b, fast=True)
                        if fails_b < fails_before or len(bad_b) < len(bad_faces):
                            logs.append(
                                f"Split {split_cnt} curved edge(s) for CAD Assistant capping compatibility "
                                f"(unmeshed faces {len(bad_faces)} -> {len(bad_b)}, 0.0 mm geometric error)"
                            )
                            current = cand_b
                            rep_base = rep_b
                            lacking_base = lacking_b
                            m_base = m_b
                            fails_before = fails_b
                            bad_faces = bad_b
        except Exception:
            pass

    # Step C: Split planar faces where an inner hole wire is tangent to the outer boundary wire
    # (pinch-point wire tangency triggers IMeshData_SelfIntersectingWire in OCCT / CAD Assistant)
    if bad_faces:
        try:
            cand_c, pinch_holes = _split_tangent_pinch_wire_faces(current, bad_faces)
            if cand_c is not None and pinch_holes > 0:
                rep_c = validate_shape(cand_c)
                lacking_c = _count_lacking_wires(cand_c)
                m_c = _inspect_manifold_topology(cand_c)
                if (
                    rep_c.solid_count == rep_base.solid_count
                    and rep_c.shell_count <= rep_base.shell_count
                    and rep_c.is_watertight
                    and not (rep_base.is_valid and not rep_c.is_valid)
                    and abs(rep_c.volume - rep_base.volume) / max(rep_base.volume, 1.0) < 0.01
                    and lacking_c <= lacking_base
                    and len(m_c.nm_edges) <= len(m_base.nm_edges)
                    and len(m_c.nm_verts) <= len(m_base.nm_verts)
                ):
                    fails_c, bad_c = _audit_prs_closed_meshing(cand_c, fast=False)
                    if fails_c == 0 or len(bad_c) < len(bad_faces) or fails_c < fails_before:
                        logs.append(
                            f"Resolved {pinch_holes} tangent pinch-wire hole(s) for CAD Assistant "
                            f"solid compatibility (unmeshed faces {len(bad_faces)} -> {len(bad_c)}, "
                            f"0.0 mm geometric error)"
                        )
                        current = cand_c
        except Exception:
            pass

    BRepTools.Clean_s(current)
    return current


def _sanitize_for_step_compat(
    shape: TopoDS_Shape,
    logs: list[str],
) -> TopoDS_Shape:
    """Ensure strict 2-manifold Parasolid/SolidWorks solid validity and OCCT 7.x closed meshing.

    1. Heals non-manifold edges (>2 incident faces) & pinched vertices via microscopic analytic
       beads BEFORE periodic splitting so SolidWorks/Parasolid imports a true MANIFOLD_SOLID_BREP.
    2. Splits 360-deg closed periodic surfaces into 180-deg halves and heals wires/tolerances.
    3. Normalizes via 3D-curve-only STEP so in-memory pcurves match external viewers.
    4. Collapses sub-Parasolid-precision micro-edges (< 5e-5 mm) without tolerance bloat.
    5. Eliminates CAD Assistant mesh-blocking sliver / curved / tangent pinch-wire faces.
    """
    current = _heal_non_manifold_topology(shape, logs)

    rep_orig = validate_shape(current)
    lacking_before = _count_lacking_wires(current)
    m_orig = _inspect_manifold_topology(current)

    try:
        # 1. Split 360-deg closed periodic faces into two clean 180-deg non-periodic halves
        divider = ShapeUpgrade_ShapeDivideClosed(current)
        divider.SetNbSplitPoints(1)
        divider.Perform()
        divided = divider.Result()

        # 2. Heal wires, seams, and tolerances (keep MaxTolerance tight at 1e-3 so fine edges
        #    are never collapsed and STEP UNCERTAINTY is never bloated to 1e-2)
        fixer = ShapeFix_Shape(divided)
        fixer.SetPrecision(1e-4)
        fixer.SetMaxTolerance(1e-3)
        fixer.Perform()
        healed = fixer.Shape()

        # 3. Ensure outward closed-solid orientation & clean stale triangulations
        healed_solids = _extract_solids(healed)
        if len(healed_solids) == 1:
            sol = healed_solids[0]
            BRepLib.OrientClosedSolid_s(sol)
            BRepTools.Clean_s(sol)
            candidate: TopoDS_Shape = sol
        else:
            for sol in healed_solids:
                BRepLib.OrientClosedSolid_s(sol)
            BRepTools.Clean_s(healed)
            candidate = healed

        rep_new = validate_shape(candidate)
        lacking_after = _count_lacking_wires(candidate)
        m_new = _inspect_manifold_topology(candidate)

        if (
            rep_new.solid_count == rep_orig.solid_count
            and rep_new.shell_count <= rep_orig.shell_count
            and rep_new.is_watertight
            and not (rep_orig.is_valid and not rep_new.is_valid)
            and abs(rep_new.volume - rep_orig.volume) / max(rep_orig.volume, 1.0) < 0.01
            and lacking_after <= lacking_before
            and len(m_new.nm_edges) <= len(m_orig.nm_edges)
            and len(m_new.nm_verts) <= len(m_orig.nm_verts)
        ):
            logs.append(
                f"STEP viewer periodic surface split & wire healing complete "
                f"(2D seam defects {lacking_before} -> {lacking_after}, {rep_new.face_count:,} faces)"
            )
            current = candidate
    except Exception as exc:
        logs.append(f"STEP compatibility healing warning (kept original): {exc}")

    # 4. Normalize via 3D-curve-only STEP so stale in-memory pcurves match actual viewer import
    current = _normalize_via_step_3d_curves(current)

    # 5. Collapse any remaining sub-Parasolid-precision edges (< 5e-5 mm)
    current = _collapse_sub_precision_edges(current, logs)

    # 6. Eliminate sliver / coarse-deflection / pinch-wire mesh-blocking faces for CAD Assistant
    current = _heal_mesh_blocking_faces(current, logs)

    # 7. Final safety pass if Step 6 introduced any sub-precision edge or non-manifold pinch
    m_post = _inspect_manifold_topology(current)
    if m_post.nm_edges or m_post.nm_verts:
        healed_nm = _heal_non_manifold_topology(current, logs)
        if not healed_nm.IsSame(current):
            try:
                divider2 = ShapeUpgrade_ShapeDivideClosed(healed_nm)
                divider2.SetNbSplitPoints(1)
                divider2.Perform()
                sf2 = ShapeFix_Shape(divider2.Result())
                sf2.SetPrecision(1e-4)
                sf2.SetMaxTolerance(1e-3)
                sf2.Perform()
                sols2 = _extract_solids(sf2.Shape())
                if len(sols2) == 1:
                    BRepLib.OrientClosedSolid_s(sols2[0])
                    healed_nm = sols2[0]
            except Exception:
                pass
            current = _normalize_via_step_3d_curves(healed_nm)
            m_post = _inspect_manifold_topology(current)
    if m_post.tiny_edge_count > 0:
        current = _collapse_sub_precision_edges(current, logs)

    return current


def _purge_final_closed_void_shells(
    shape: TopoDS_Shape,
    logs: list[str],
) -> TopoDS_Shape:
    """Final mandatory post-processing pass ensuring ZERO internal closed void shells remain."""
    sols = _extract_solids(shape)
    if not sols:
        return shape

    cleaned_sols: list[TopoDS_Solid] = []
    purged_count = 0
    purged_vol = 0.0
    for s in sols:
        sf, nv, dv = _remove_closed_void_shells(s)
        BRepLib.OrientClosedSolid_s(sf)
        BRepTools.Clean_s(sf)
        cleaned_sols.append(sf)
        purged_count += nv
        purged_vol += dv

    if purged_count > 0:
        logs.append(
            f"Final post-processing purged {purged_count} remaining internal closed void shell(s) "
            f"(+{purged_vol:,.1f} mm³ solid fill)"
        )
    else:
        logs.append("Final post-processing void check passed: 0 internal closed shells remaining")

    if len(cleaned_sols) == 1 and shape.ShapeType() == TopAbs_SOLID:
        return cleaned_sols[0]
    if len(cleaned_sols) == 1:
        return cleaned_sols[0]
    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for s in cleaned_sols:
        builder.Add(comp, s)
    return comp


def simplify_assembly_to_single_solid(
    assembly: AssemblyModel,
    config: SimplifyConfig,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
    tessellated: Optional[TessellatedAssembly] = None,
    max_workers: Optional[int] = None,
) -> SimplifyResult:
    """Remove internal parts and build a 100% exterior-preserving single exact B-Rep Solid."""
    ensure_occt_parallelism_enabled()
    t0 = time.perf_counter()
    logs: list[str] = []

    kept_parts = [p for p in assembly.parts if p.keep]
    removed_count = len(assembly.parts) - len(kept_parts)
    if not kept_parts:
        raise ValueError("No outer parts selected to keep.")

    kept_parts_sorted = sorted(kept_parts, key=lambda p: -max(p.volume, 0.0))
    orig_vol = sum(max(p.volume, 0.0) for p in kept_parts_sorted)
    logs.append(
        f"Starting exact B-Rep simplification: keep {len(kept_parts_sorted)} / drop {removed_count} parts "
        f"(kept total volume: {orig_vol:,.1f} mm³)"
    )

    # --- Stage 1: Heal kept B-Rep shapes ---
    if progress_cb:
        progress_cb(0.06, f"Validating & healing outer B-Rep parts ({len(kept_parts_sorted)})...")

    solids: list[TopoDS_Solid] = []
    valid_parts: list[PartNode] = []
    for p in kept_parts_sorted:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        healed = _heal_solid(p.shape)
        if healed is not None:
            solids.append(healed)
            valid_parts.append(p)

    if not solids:
        raise RuntimeError("Failed to extract any valid outer B-Rep solids.")

    # --- Stage 2: Optional Small Hole Defeaturing (OFF by default to keep exact exterior) ---
    if config.remove_small_holes and config.max_hole_diameter > 0.0:
        if progress_cb:
            progress_cb(
                0.12,
                f"Plugging small outer through-holes (diameter <= {config.max_hole_diameter:.1f} mm)...",
            )
        total_holes = 0
        cleaned: list[TopoDS_Solid] = []
        for idx, s in enumerate(solids):
            if cancel_check and cancel_check():
                raise InterruptedError("Operation cancelled by user.")
            cs, cnt = _remove_small_holes_from_solid(s, config.max_hole_diameter)
            cleaned.append(cs)
            total_holes += cnt
        solids = cleaned
        logs.append(f"Small hole plugging complete: removed {total_holes} cylindrical hole face(s)")

    # --- Stage 3: Strictly-Interior Analytic B-Rep Core Generation ---
    core_shape: Optional[TopoDS_Shape] = None
    if config.fill_internal_core:
        if progress_cb:
            progress_cb(0.20, "Computing strictly-interior analytic B-Rep core (exterior untouched)...")
        core_primitives = _build_analytic_interior_core(
            valid_parts,
            bridge_gap_mm=config.core_bridge_gap_mm,
            inset_mm=config.core_inset_mm,
            grid_res=config.core_grid_resolution,
            logs=logs,
            cancel_check=cancel_check,
            tessellated=tessellated,
            max_workers=max_workers,
        )
        if core_primitives:
            if progress_cb:
                progress_cb(0.32, f"Fusing interior core primitives ({len(core_primitives)})...")
            core_shape = _batch_fuse_safe(
                core_primitives,
                logs=logs,
                label="Interior core",
                cancel_check=cancel_check,
            )

    # --- Stage 4: Two-Tier Exact B-Rep Boolean Fuse (fuzzy=0.0 first!) ---
    # Tier 1: Fuse major structural housings + analytic interior core FIRST, then immediately
    # strip closed internal void shells so internal features (e.g. gear teeth, inner pockets)
    # disappear before fusing small fasteners/connectors/PCBs.
    vol_thresh = orig_vol * 0.015
    major_solids = [s for s, p in zip(solids, valid_parts) if p.volume >= vol_thresh]
    minor_solids = [s for s, p in zip(solids, valid_parts) if p.volume < vol_thresh]
    if not major_solids:
        major_solids = solids[: min(6, len(solids))]
        minor_solids = solids[min(6, len(solids)) :]

    if progress_cb:
        progress_cb(0.40, f"Fusing major outer housings + interior core ({len(major_solids)})...")

    tier1_inputs: list[TopoDS_Shape] = (
        ([core_shape] if core_shape is not None else []) + list(major_solids)
    )
    tier1_fused = _batch_fuse_safe(
        tier1_inputs,
        logs=logs,
        label="Tier-1 major housings + core",
        progress_cb=progress_cb,
        prog_range=(0.40, 0.62),
        cancel_check=cancel_check,
    )

    # Immediately purge closed void shells inside Tier-1 result
    if progress_cb:
        progress_cb(0.64, "Pass 1: removing enclosed internal void shells...")
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
            progress_cb(0.68, f"Fusing detail outer parts (connectors, fasteners: {len(minor_solids)})...")
        tier2_inputs: list[TopoDS_Shape] = list(tier1_cleaned) + list(minor_solids)
        combined = _batch_fuse_safe(
            tier2_inputs,
            logs=logs,
            label="Tier-2 detail outer parts",
            progress_cb=progress_cb,
            prog_range=(0.68, 0.80),
            cancel_check=cancel_check,
        )
    else:
        combined = tier1_cleaned[0] if len(tier1_cleaned) == 1 else tier1_fused

    # Final closed-void shell elimination on every solid
    if progress_cb:
        progress_cb(0.82, "Eliminating enclosed internal void shells...")

    final_solids: list[TopoDS_Solid] = []
    for s in _extract_solids(combined):
        sf, nv, dv = _remove_closed_void_shells(s)
        total_voids += nv
        total_void_vol += dv
        final_solids.append(sf)

    if total_voids > 0:
        logs.append(
            f"Removed {total_voids} enclosed internal void shell(s) (+{total_void_vol:,.1f} mm³ solid fill)"
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
        logs.append(f"Removed {swallowed} residual solid(s) fully enclosed inside the filled body")

    # Stage 5: Bridge any remaining nearby floating external islands via hidden internal B-Rep pins
    if len(surviving) > 1 and config.connect_floating_parts and config.max_island_gap_mm > 0.0:
        if progress_cb:
            progress_cb(0.86, f"Bridging {len(surviving) - 1} clearance-separated outer part(s) internally...")
        main_s, leftovers = _connect_floating_islands(
            surviving[0],
            surviving[1:],
            max_gap_mm=config.max_island_gap_mm,
            logs=logs,
            cancel_check=cancel_check,
        )
        surviving = [main_s] + leftovers

    if not surviving:
        raise RuntimeError("Failed to generate final exact B-Rep solid.")

    if len(surviving) == 1 or config.keep_largest_only:
        final_shape: TopoDS_Shape = surviving[0]
        if len(surviving) > 1:
            logs.append(
                f"Kept largest single body per single-body option (excluded {len(surviving) - 1} unbridgeable island(s))"
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
            progress_cb(0.91, "Merging coplanar/co-cylindrical faces (UnifySameDomain)...")
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
                        f"Unified same-domain faces: {rep_before.face_count:,} -> {rep_u.face_count:,} faces"
                    )
        except Exception as exc:
            logs.append(f"UnifySameDomain warning (kept original): {exc}")

    # --- Stage 7: Cross-Kernel / OCCT 7.x STEP Compatibility & Closed-Mesh Sanitization ---
    if progress_cb:
        progress_cb(0.94, "Healing STEP viewer compatibility (periodic seams, slivers & closed mesh)...")
    final_shape = _sanitize_for_step_compat(final_shape, logs)

    # --- Stage 8: Mandatory Final Closed Void Shell Post-Processing Purge ---
    if progress_cb:
        progress_cb(0.97, "Final post-processing: purging any remaining internal closed shells...")
    final_shape = _purge_final_closed_void_shells(final_shape, logs)

    if progress_cb:
        progress_cb(0.99, "Validating final single B-Rep solid...")

    report = validate_shape(final_shape)
    if report.face_types:
        ft_summary = ", ".join(f"{k}:{v}" for k, v in sorted(report.face_types.items(), key=lambda kv: -kv[1]))
        logs.append(f"Final B-Rep surface breakdown (exact CAD, no voxels): {ft_summary}")

    if progress_cb:
        progress_cb(1.0, "Exact B-Rep single solid conversion complete")

    return SimplifyResult(
        shape=final_shape,
        mode_used="Exact B-Rep (Exterior Preserved + Analytic Interior Core)",
        validation=report,
        kept_part_count=len(kept_parts_sorted),
        removed_part_count=removed_count,
        original_volume=orig_vol,
        filled_volume_delta=max(0.0, report.volume - orig_vol),
        elapsed_seconds=time.perf_counter() - t0,
        logs=logs,
    )
