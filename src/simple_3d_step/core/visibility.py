"""2-Pass external visibility analysis using Intel Embree BVH ray casting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from simple_3d_step.core.tessellation import TessellatedAssembly, tessellate_assembly
from simple_3d_step.models import (
    AssemblyModel,
    CancelCheck,
    PartStatus,
    ProgressCallback,
    VisibilityConfig,
)


@dataclass
class VisibilityResult:
    """Detailed output of 2-Pass visibility analysis."""

    tessellated: TessellatedAssembly
    part_visible_faces: dict[int, set[int]] = field(default_factory=dict)
    tri_hit_counts: np.ndarray = field(default_factory=lambda: np.zeros((0,), dtype=np.int32))
    external_count: int = 0
    internal_count: int = 0


def _fibonacci_sphere(n: int) -> np.ndarray:
    """Generate n unit vectors uniformly distributed on the unit sphere."""
    n = max(6, int(n))
    idx = np.arange(n, dtype=np.float64) + 0.5
    phi = np.arccos(1.0 - 2.0 * idx / n)
    theta = np.pi * (1.0 + 5.0 ** 0.5) * idx
    x = np.sin(phi) * np.cos(theta)
    y = np.sin(phi) * np.sin(theta)
    z = np.cos(phi)
    vecs = np.column_stack([x, y, z])
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.maximum(norms, 1e-12)


def _fibonacci_hemisphere(k: int) -> np.ndarray:
    """Generate k unit vectors uniformly distributed on the +Z hemisphere."""
    k = max(8, int(k))
    idx = np.arange(k, dtype=np.float64) + 0.5
    z = idx / k  # (0, 1) strictly above tangent plane
    r = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    theta = np.pi * (1.0 + 5.0 ** 0.5) * idx
    return np.column_stack([r * np.cos(theta), r * np.sin(theta), z])


def _orthonormal_basis(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return two unit vectors (u, v) orthogonal to unit vector `axis`."""
    up = np.array([0.0, 0.0, 1.0]) if abs(axis[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(up, axis)
    u /= max(float(np.linalg.norm(u)), 1e-12)
    v = np.cross(axis, u)
    v /= max(float(np.linalg.norm(v)), 1e-12)
    return u, v


def _align_hemisphere_to_normals(
    normals: np.ndarray, hemi_local: np.ndarray
) -> np.ndarray:
    """Rotate local +Z hemisphere directions (K, 3) to each normal (M, 3) -> (M, K, 3)."""
    m = len(normals)
    up = np.where(
        np.abs(normals[:, 2:3]) < 0.9,
        np.array([0.0, 0.0, 1.0]),
        np.array([1.0, 0.0, 0.0]),
    )
    u = np.cross(up, normals)
    u /= np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-12)
    v = np.cross(normals, u)
    v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

    # (M, K, 3) = u[:, None, :] * hx + v[:, None, :] * hy + n[:, None, :] * hz
    hx = hemi_local[None, :, 0:1]
    hy = hemi_local[None, :, 1:2]
    hz = hemi_local[None, :, 2:3]
    dirs = u[:, None, :] * hx + v[:, None, :] * hy + normals[:, None, :] * hz
    return dirs.reshape(m * len(hemi_local), 3)


def analyze_visibility(
    assembly: AssemblyModel,
    config: VisibilityConfig,
    tessellated: Optional[TessellatedAssembly] = None,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> VisibilityResult:
    """Run 2-Pass Embree visibility analysis to identify internal (non-visible) parts.

    Pass 1: Multi-view outside-in orthographic ray grids from Fibonacci sphere.
    Pass 2: Inside-out hemisphere escape check on borderline/low-hit parts.
    Updates each PartNode's visibility metrics and auto_status/keep in-place.
    """
    if not assembly.parts:
        return VisibilityResult(
            tessellated=tessellated or tessellate_assembly(assembly),
        )

    if tessellated is None:
        tessellated = tessellate_assembly(
            assembly,
            linear_deflection_ratio=config.linear_deflection_ratio,
            angular_deflection=config.angular_deflection,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
        )

    mesh = tessellated.mesh
    n_tris = len(tessellated.faces)
    if mesh is None or n_tris == 0:
        return VisibilityResult(tessellated=tessellated)

    if progress_cb:
        progress_cb(0.38, f"Embree BVH 가속 구조 구축 중 (삼각형 {n_tris:,}개)...")

    intersector = mesh.ray
    center = 0.5 * (assembly.bbox_min + assembly.bbox_max)
    diag = max(assembly.bbox_diagonal, 1e-3)
    radius = 0.5 * diag * 1.05

    # --- Pass 1: Outside-In Fibonacci Sphere Orthographic Ray Casting ---
    view_dirs = _fibonacci_sphere(config.num_views)
    res = max(32, int(config.grid_resolution))
    lin = np.linspace(-radius, radius, res, dtype=np.float64)
    gu_base, gv_base = np.meshgrid(lin, lin, indexing="xy")
    gu_flat = gu_base.ravel()
    gv_flat = gv_base.ravel()
    # Filter to circular disk covering the bounding sphere (saves ~21% wasted corner rays)
    disk_mask = (gu_flat * gu_flat + gv_flat * gv_flat) <= (radius * radius * 1.04)
    gu_disk = gu_flat[disk_mask]
    gv_disk = gv_flat[disk_mask]
    n_rays_per_view = len(gu_disk)
    step = (2.0 * radius) / max(res - 1, 1)

    tri_hit_counts = np.zeros(n_tris, dtype=np.int32)
    n_views = len(view_dirs)

    for v_idx, v_pos in enumerate(view_dirs):
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")

        if progress_cb and (v_idx % max(1, n_views // 20) == 0 or v_idx == n_views - 1):
            frac = 0.40 + 0.40 * ((v_idx + 1) / n_views)
            progress_cb(frac, f"Pass 1 외부 Ray Casting ({v_idx + 1}/{n_views} 시점)...")

        ray_dir = -v_pos
        u, v = _orthonormal_basis(ray_dir)
        # Golden-ratio sub-pixel jitter per view avoids aliasing thin features
        jx = (((v_idx * 0.61803398875) % 1.0) - 0.5) * step
        jy = (((v_idx * 0.38196601125) % 1.0) - 0.5) * step

        origin_center = center + v_pos * (radius * 1.5)
        origins = (
            origin_center[None, :]
            + (gu_disk + jx)[:, None] * u[None, :]
            + (gv_disk + jy)[:, None] * v[None, :]
        )
        directions = np.broadcast_to(ray_dir, (n_rays_per_view, 3))

        hit_tris, _ = intersector.intersects_id(
            ray_origins=origins,
            ray_directions=directions,
            multiple_hits=False,
            return_locations=False,
        )
        if len(hit_tris) > 0:
            np.add.at(tri_hit_counts, hit_tris, 1)

    # Aggregate Pass 1 results per part and per B-Rep face
    part_visible_faces: dict[int, set[int]] = {}
    borderline_parts: list[int] = []
    pass2_threshold = max(config.min_visible_ratio * 2.5, 0.025)

    for part in assembly.parts:
        s, e = tessellated.part_tri_slices.get(part.part_id, (0, 0))
        if e <= s:
            part.ray_hits = 0
            part.visible_face_count = 0
            part.visible_area_ratio = 0.0
            part.escape_ratio = 0.0
            part_visible_faces[part.part_id] = set()
            continue

        p_hits = tri_hit_counts[s:e]
        p_areas = tessellated.tri_areas[s:e]
        p_fids = tessellated.tri_face_id[s:e]

        hit_mask = p_hits > 0
        total_area = float(np.sum(p_areas))
        hit_tri_area = float(np.sum(p_areas[hit_mask]))

        part_hits = int(np.sum(p_hits))
        part.ray_hits = part_hits
        # Cauchy integral geometry estimate of true exposed surface area (invariant to large triangles)
        cauchy_vis_area = 4.0 * part_hits * (step * step) / max(n_views, 1)
        est_vis_area = min(hit_tri_area, cauchy_vis_area)
        part.visible_area_ratio = (
            min(1.0, est_vis_area / total_area) if total_area > 1e-12 else 0.0
        )
        vis_fids = set(int(fid) for fid in np.unique(p_fids[hit_mask]))
        part_visible_faces[part.part_id] = vis_fids
        part.visible_face_count = len(vis_fids)
        part.escape_ratio = 0.0

        if config.enable_pass2_verify and part.visible_area_ratio < pass2_threshold:
            borderline_parts.append(part.part_id)

    # --- Pass 2: Inside-Out Hemisphere Escape Check for Borderline / Small Parts ---
    if config.enable_pass2_verify and borderline_parts:
        hemi_local = _fibonacci_hemisphere(config.pass2_rays_per_sample)
        eps = max(diag * 2e-4, 1e-2)
        rng = np.random.default_rng(42)
        n_border = len(borderline_parts)
        part_map = {p.part_id: p for p in assembly.parts}

        for b_idx, pid in enumerate(borderline_parts):
            if cancel_check and cancel_check():
                raise InterruptedError("사용자가 작업을 취소했습니다.")

            if progress_cb and (b_idx % max(1, n_border // 15) == 0 or b_idx == n_border - 1):
                frac = 0.80 + 0.17 * ((b_idx + 1) / n_border)
                progress_cb(
                    frac, f"Pass 2 경계/소형 부품 역방향 검증 ({b_idx + 1}/{n_border})..."
                )

            part = part_map[pid]
            s, e = tessellated.part_tri_slices[pid]
            p_areas = tessellated.tri_areas[s:e]
            total_area = float(np.sum(p_areas))
            if total_area <= 1e-12:
                continue

            probs = p_areas / total_area
            n_samples = min(config.pass2_samples_per_part, max(12, e - s))
            sampled_local = rng.choice(e - s, size=n_samples, replace=(n_samples > (e - s)), p=probs)
            sampled_global = s + sampled_local

            centroids = tessellated.tri_centroids[sampled_global]
            normals = tessellated.tri_normals[sampled_global]
            sample_origins = centroids + normals * eps

            dirs = _align_hemisphere_to_normals(normals, hemi_local)
            origins = np.repeat(sample_origins, len(hemi_local), axis=0)

            hit_any = intersector.intersects_any(ray_origins=origins, ray_directions=dirs)
            escaped = int(np.count_nonzero(~hit_any))
            part.escape_ratio = escaped / max(len(dirs), 1)

            # Record faces whose outward sample rays escaped as visible faces
            if escaped > 0:
                escaped_per_sample = (~hit_any).reshape(n_samples, len(hemi_local)).any(axis=1)
                escaped_fids = tessellated.tri_face_id[sampled_global[escaped_per_sample]]
                part_visible_faces[pid].update(int(fid) for fid in np.unique(escaped_fids))
                part.visible_face_count = len(part_visible_faces[pid])

    # --- Final Classification ---
    ext_count = 0
    int_count = 0
    for part in assembly.parts:
        part.visibility_score = max(part.visible_area_ratio, part.escape_ratio)

        is_too_small = (
            config.min_part_volume_mm3 > 0.0 and part.volume < config.min_part_volume_mm3
        )
        if part.visibility_score >= config.min_visible_ratio and not is_too_small:
            part.auto_status = PartStatus.EXTERNAL
            if not part.user_overridden:
                part.keep = True
            ext_count += 1
        else:
            part.auto_status = PartStatus.INTERNAL
            if not part.user_overridden:
                part.keep = False
            int_count += 1

    if progress_cb:
        progress_cb(
            1.0,
            f"가시성 분석 완료: 외부 유지 {ext_count}개 / 내부 삭제 {int_count}개",
        )

    return VisibilityResult(
        tessellated=tessellated,
        part_visible_faces=part_visible_faces,
        tri_hit_counts=tri_hit_counts,
        external_count=ext_count,
        internal_count=int_count,
    )
