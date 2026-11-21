"""Fast OCCT-to-Trimesh tessellation preserving per-part and per-face triangle indices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import trimesh
from OCP.BRep import BRep_Tool
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape

from simple_3d_step.core.occt_compat import to_face
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
            trsf = loc.Transformation()
            has_trsf = not loc.IsIdentity()

            pts = np.empty((nb_nodes, 3), dtype=np.float64)
            for i in range(1, nb_nodes + 1):
                p = triangulation.Node(i)
                if has_trsf:
                    p = p.Transformed(trsf)
                pts[i - 1, 0] = p.X()
                pts[i - 1, 1] = p.Y()
                pts[i - 1, 2] = p.Z()

            tris = np.empty((nb_tris, 3), dtype=np.int64)
            is_rev = face.Orientation() == TopAbs_REVERSED
            for t in range(1, nb_tris + 1):
                tri = triangulation.Triangle(t)
                n1, n2, n3 = tri.Get()
                if is_rev:
                    tris[t - 1, 0] = n1 - 1 + vert_offset
                    tris[t - 1, 1] = n3 - 1 + vert_offset
                    tris[t - 1, 2] = n2 - 1 + vert_offset
                else:
                    tris[t - 1, 0] = n1 - 1 + vert_offset
                    tris[t - 1, 1] = n2 - 1 + vert_offset
                    tris[t - 1, 2] = n3 - 1 + vert_offset

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


def tessellate_assembly(
    assembly: AssemblyModel,
    linear_deflection_ratio: float = 0.0025,
    angular_deflection: float = 0.5,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> TessellatedAssembly:
    """Tessellate all parts in an AssemblyModel into one unified Embree-ready mesh."""
    diag = max(assembly.bbox_diagonal, 1e-3)
    global_deflection = max(diag * linear_deflection_ratio, 0.05)

    all_verts: list[np.ndarray] = []
    all_faces: list[np.ndarray] = []
    all_part_ids: list[np.ndarray] = []
    all_face_ids: list[np.ndarray] = []
    part_tri_slices: dict[int, tuple[int, int]] = {}
    part_face_counts: dict[int, int] = {}

    vert_offset = 0
    tri_offset = 0
    n_parts = len(assembly.parts)

    for idx, part in enumerate(assembly.parts):
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")

        if progress_cb and (idx % max(1, n_parts // 25) == 0 or idx == n_parts - 1):
            frac = (idx + 1) / max(n_parts, 1)
            progress_cb(frac * 0.35, f"Tessellating ({idx + 1}/{n_parts}): {part.name}")

        # Use finer deflection for very small parts so they don't collapse
        part_diag = max(part.bbox_diagonal, 1e-3)
        defl = min(global_deflection, max(part_diag * 0.02, 0.02))

        v, f, fids, face_cnt = tessellate_shape_triangles(
            part.shape, linear_deflection=defl, angular_deflection=angular_deflection
        )
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
