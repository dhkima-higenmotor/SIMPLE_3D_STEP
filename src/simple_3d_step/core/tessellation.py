"""Fast multi-core OCCT-to-Trimesh tessellation preserving per-part and per-face triangle indices."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import math
from typing import Iterable, Optional

import numpy as np
import trimesh
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape

from simple_3d_step.core.occt_compat import ensure_occt_parallelism_enabled, to_face
from simple_3d_step.models import AssemblyModel, CancelCheck, ProgressCallback


@dataclass
class TessellatedAssembly:
    """Combined triangle mesh of all parts with exact part/face index mappings."""

    vertices: np.ndarray              # (V, 3) float64
    faces: np.ndarray                 # (T, 3) int64
    tri_part_id: np.ndarray           # (T,) int32
    tri_face_id: np.ndarray           # (T,) int32
    tri_areas: np.ndarray             # (T,) float64
    tri_normals: np.ndarray           # (T, 3) float64
    tri_centroids: np.ndarray         # (T, 3) float64
    part_tri_slices: dict[int, tuple[int, int]] = field(default_factory=dict)
    part_face_counts: dict[int, int] = field(default_factory=dict)
    mesh: Optional[trimesh.Trimesh] = None


def extract_shape_triangles(
    shape: TopoDS_Shape,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Extract cached OCCT face triangulations into (vertices, faces, tri_face_ids, face_count).

    Triangle winding respects face orientation so normals point outward.
    """
    verts_list: list[np.ndarray] = []
    faces_list: list[np.ndarray] = []
    face_ids_list: list[np.ndarray] = []
    vert_offset = 0
    face_idx = 0

    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = to_face(exp.Current())
        loc = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, loc)
        if triangulation is not None and triangulation.NbTriangles() > 0:
            nb_nodes = triangulation.NbNodes()
            nb_tris = triangulation.NbTriangles()

            node_fn = triangulation.Node
            pts = np.empty((nb_nodes, 3), dtype=np.float64)
            for i in range(1, nb_nodes + 1):
                p = node_fn(i)
                pts[i - 1, 0] = p.X()
                pts[i - 1, 1] = p.Y()
                pts[i - 1, 2] = p.Z()

            if not loc.IsIdentity():
                trsf = loc.Transformation()
                rot = np.array(
                    [
                        [trsf.Value(1, 1), trsf.Value(1, 2), trsf.Value(1, 3)],
                        [trsf.Value(2, 1), trsf.Value(2, 2), trsf.Value(2, 3)],
                        [trsf.Value(3, 1), trsf.Value(3, 2), trsf.Value(3, 3)],
                    ],
                    dtype=np.float64,
                )
                trans = np.array(
                    [trsf.Value(1, 4), trsf.Value(2, 4), trsf.Value(3, 4)],
                    dtype=np.float64,
                )
                pts = pts @ rot.T + trans

            tri_fn = triangulation.Triangle
            tris = np.empty((nb_tris, 3), dtype=np.int64)
            is_rev = face.Orientation() == TopAbs_REVERSED
            if is_rev:
                for t in range(1, nb_tris + 1):
                    n1, n2, n3 = tri_fn(t).Get()
                    tris[t - 1, 0] = n1
                    tris[t - 1, 1] = n3
                    tris[t - 1, 2] = n2
            else:
                for t in range(1, nb_tris + 1):
                    n1, n2, n3 = tri_fn(t).Get()
                    tris[t - 1, 0] = n1
                    tris[t - 1, 1] = n2
                    tris[t - 1, 2] = n3
            tris += vert_offset - 1

            verts_list.append(pts)
            faces_list.append(tris)
            face_ids_list.append(np.full(nb_tris, face_idx, dtype=np.int32))
            vert_offset += nb_nodes

        face_idx += 1
        exp.Next()

    if not verts_list:
        return (
            np.zeros((0, 3), dtype=np.float64),
            np.zeros((0, 3), dtype=np.int64),
            np.zeros((0,), dtype=np.int32),
            face_idx,
        )

    return (
        np.vstack(verts_list),
        np.vstack(faces_list),
        np.concatenate(face_ids_list),
        face_idx,
    )


def tessellate_shape_triangles(
    shape: TopoDS_Shape,
    linear_deflection: float,
    angular_deflection: float = 0.5,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Tessellate a single TopoDS_Shape into (vertices, faces, tri_face_ids, face_count).

    Triangle winding respects face orientation so normals point outward.
    """
    mesher = BRepMesh_IncrementalMesh(
        shape, float(linear_deflection), False, float(angular_deflection), True
    )
    mesher.Perform()
    return extract_shape_triangles(shape)


def mesh_shapes_in_parallel_buckets(
    shapes: list[TopoDS_Shape],
    deflections: list[float],
    angular_deflection: float = 0.5,
    cancel_check: Optional[CancelCheck] = None,
) -> None:
    """Batch-mesh multiple shapes in deflection buckets so OCCT OSD_Parallel saturates all cores.

    Grouping shapes into logarithmic deflection buckets (using each bucket's minimum deflection
    so accuracy is strictly >= requested) lets a single BRepMesh_IncrementalMesh call distribute
    all faces across all C++ threads rather than serializing part-by-part behind the Python GIL.
    """
    if not shapes:
        return

    log_step = math.log(1.5)
    buckets: dict[int, list[tuple[TopoDS_Shape, float]]] = defaultdict(list)
    for shp, defl in zip(shapes, deflections):
        if shp is None or shp.IsNull():
            continue
        d = max(float(defl), 1e-4)
        bkey = math.floor(math.log(d) / log_step)
        buckets[bkey].append((shp, d))

    # Mesh finest buckets first so shared TShapes get the finest needed deflection
    for bkey in sorted(buckets.keys()):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")
        items = buckets[bkey]
        bucket_defl = min(d for _, d in items)
        if len(items) == 1:
            target = items[0][0]
        else:
            builder = BRep_Builder()
            comp = TopoDS_Compound()
            builder.MakeCompound(comp)
            for shp, _ in items:
                builder.Add(comp, shp)
            target = comp

        mesher = BRepMesh_IncrementalMesh(
            target, float(bucket_defl), False, float(angular_deflection), True
        )
        mesher.Perform()


def build_subset_trimesh(
    tessellated: TessellatedAssembly,
    part_ids: Iterable[int],
) -> Optional[trimesh.Trimesh]:
    """Extract a Trimesh containing only the triangles belonging to `part_ids` (zero re-meshing)."""
    if tessellated is None or len(tessellated.faces) == 0:
        return None
    face_chunks: list[np.ndarray] = []
    for pid in part_ids:
        s, e = tessellated.part_tri_slices.get(pid, (0, 0))
        if e > s:
            face_chunks.append(tessellated.faces[s:e])
    if not face_chunks:
        return None
    sub_faces = np.vstack(face_chunks)
    # Compact vertex array to referenced vertices so mesh.bounds reflects ONLY kept_parts
    used_verts, inv = np.unique(sub_faces.ravel(), return_inverse=True)
    compact_verts = tessellated.vertices[used_verts]
    compact_faces = inv.reshape(-1, 3)
    return trimesh.Trimesh(vertices=compact_verts, faces=compact_faces, process=False)


def tessellate_assembly(
    assembly: AssemblyModel,
    linear_deflection_ratio: float = 0.0025,
    angular_deflection: float = 0.5,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> TessellatedAssembly:
    """Tessellate all parts in an AssemblyModel into one unified Embree-ready mesh."""
    ensure_occt_parallelism_enabled()
    diag = max(assembly.bbox_diagonal, 1e-3)
    global_deflection = max(diag * linear_deflection_ratio, 0.05)

    n_parts = len(assembly.parts)
    if progress_cb and n_parts > 0:
        progress_cb(0.05, f"Batch-meshing {n_parts} parts in parallel across CPU cores...")

    # Phase A: Multi-core C++ compound meshing grouped by deflection buckets
    shapes = [p.shape for p in assembly.parts]
    deflections = [
        min(global_deflection, max(max(p.bbox_diagonal, 1e-3) * 0.02, 0.02))
        for p in assembly.parts
    ]
    mesh_shapes_in_parallel_buckets(
        shapes,
        deflections,
        angular_deflection=angular_deflection,
        cancel_check=cancel_check,
    )

    # Phase B: Extract triangulations per part preserving exact part/face index mappings
    all_verts: list[np.ndarray] = []
    all_faces: list[np.ndarray] = []
    all_part_ids: list[np.ndarray] = []
    all_face_ids: list[np.ndarray] = []
    part_tri_slices: dict[int, tuple[int, int]] = {}
    part_face_counts: dict[int, int] = {}

    vert_offset = 0
    tri_offset = 0

    for idx, part in enumerate(assembly.parts):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")

        if progress_cb and (idx % max(1, n_parts // 20) == 0 or idx == n_parts - 1):
            frac = 0.15 + 0.20 * ((idx + 1) / max(n_parts, 1))
            progress_cb(frac, f"Extracting mesh ({idx + 1}/{n_parts}): {part.name}")

        v, f, fids, face_cnt = extract_shape_triangles(part.shape)
        part_face_counts[part.part_id] = face_cnt

        if len(f) > 0:
            all_verts.append(v)
            all_faces.append(f + vert_offset)
            all_part_ids.append(np.full(len(f), part.part_id, dtype=np.int32))
            all_face_ids.append(fids)
            part_tri_slices[part.part_id] = (tri_offset, tri_offset + len(f))
            vert_offset += len(v)
            tri_offset += len(f)
        else:
            part_tri_slices[part.part_id] = (tri_offset, tri_offset)

    if not all_verts:
        empty_v = np.zeros((0, 3), dtype=np.float64)
        empty_f = np.zeros((0, 3), dtype=np.int64)
        return TessellatedAssembly(
            vertices=empty_v,
            faces=empty_f,
            tri_part_id=np.zeros((0,), dtype=np.int32),
            tri_face_id=np.zeros((0,), dtype=np.int32),
            tri_areas=np.zeros((0,), dtype=np.float64),
            tri_normals=np.zeros((0, 3), dtype=np.float64),
            tri_centroids=np.zeros((0, 3), dtype=np.float64),
            part_tri_slices=part_tri_slices,
            part_face_counts=part_face_counts,
            mesh=trimesh.Trimesh(vertices=empty_v, faces=empty_f, process=False),
        )

    vertices = np.vstack(all_verts)
    faces = np.vstack(all_faces)
    tri_part_id = np.concatenate(all_part_ids)
    tri_face_id = np.concatenate(all_face_ids)

    # Precompute triangle geometry (areas, unit normals, centroids)
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    norms = np.linalg.norm(cross, axis=1)
    tri_areas = 0.5 * norms
    safe_norms = np.where(norms > 1e-12, norms, 1.0)[:, None]
    tri_normals = cross / safe_norms
    tri_centroids = (v0 + v1 + v2) / 3.0

    # Keep process=False so triangle indices remain 1:1 with tri_part_id & tri_face_id
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    return TessellatedAssembly(
        vertices=vertices,
        faces=faces,
        tri_part_id=tri_part_id,
        tri_face_id=tri_face_id,
        tri_areas=tri_areas,
        tri_normals=tri_normals,
        tri_centroids=tri_centroids,
        part_tri_slices=part_tri_slices,
        part_face_counts=part_face_counts,
        mesh=mesh,
    )
