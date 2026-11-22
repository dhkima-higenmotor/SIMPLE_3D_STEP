"""Automated tests for multi-core visibility, tessellation, OCCT parallelism, and parallel batch."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from simple_3d_step.config import (
    AppConfig,
    load_config,
    resolve_batch_workers,
    save_config,
    threads_per_batch_worker,
)
from simple_3d_step.core.occt_compat import (
    bytes_to_shape,
    configure_occt_parallelism,
    make_brep_check_analyzer,
    shape_to_bytes,
)
from simple_3d_step.core.solidifier import _batch_fuse_safe
from simple_3d_step.core.step_io import load_step_assembly
from simple_3d_step.core.tessellation import build_subset_trimesh, tessellate_assembly
from simple_3d_step.core.validator import validate_shape
from simple_3d_step.core.visibility import analyze_visibility
from simple_3d_step.main import main
from simple_3d_step.models import VisibilityConfig
from tests.generate_sample_step import (
    generate_closed_enclosure_step,
    generate_holed_box_step,
)


def test_visibility_single_vs_multithread_exact_match(tmp_path: Path) -> None:
    """Multi-threaded Embree Pass 1 + Pass 2 must match single-threaded results exactly."""
    step_file = generate_holed_box_step(tmp_path / "holed_det.step")
    asm1 = load_step_assembly(step_file)
    asm2 = load_step_assembly(step_file)

    cfg = VisibilityConfig(num_views=42, grid_resolution=120, enable_pass2_verify=True)
    res1 = analyze_visibility(asm1, cfg, max_workers=1)
    res8 = analyze_visibility(asm2, cfg, max_workers=8)

    assert res1.external_count == res8.external_count
    assert res1.internal_count == res8.internal_count
    assert np.array_equal(res1.tri_hit_counts, res8.tri_hit_counts)

    for p1, p2 in zip(asm1.parts, asm2.parts):
        assert p1.ray_hits == p2.ray_hits
        assert p1.visible_face_count == p2.visible_face_count
        assert p1.visible_area_ratio == pytest.approx(p2.visible_area_ratio)
        assert p1.escape_ratio == pytest.approx(p2.escape_ratio)
        assert p1.auto_status == p2.auto_status
        assert p1.keep == p2.keep


def test_bucketed_tessellation_and_subset_trimesh(tmp_path: Path) -> None:
    """Deflection-bucketed compound meshing and kept-part subset extraction preserve indices."""
    step_file = generate_closed_enclosure_step(tmp_path / "closed_tess.step")
    asm = load_step_assembly(step_file)
    tess = tessellate_assembly(asm)

    assert len(tess.faces) > 0
    assert len(tess.part_tri_slices) == len(asm.parts)

    ext_ids = [p.part_id for p in asm.parts if p.name.startswith("EXT_")]
    sub = build_subset_trimesh(tess, ext_ids)
    assert sub is not None
    expected_tris = sum(
        tess.part_tri_slices[pid][1] - tess.part_tri_slices[pid][0] for pid in ext_ids
    )
    assert len(sub.faces) == expected_tris


def test_occt_parallelism_and_bintools_roundtrip() -> None:
    """Verify OCCT parallel config, parallel BRepCheck, and lossless BinTools serialization."""
    applied = configure_occt_parallelism()
    assert applied >= 1

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 30.0, 20.0, 10.0).Solid()
    assert make_brep_check_analyzer(box, geom_controls=True, parallel=True).IsValid()

    raw = shape_to_bytes(box)
    assert len(raw) > 50
    restored = bytes_to_shape(raw)
    assert not restored.IsNull()
    rep = validate_shape(restored)
    assert rep.is_valid is True
    assert rep.volume == pytest.approx(6000.0, rel=1e-4)


def test_batch_worker_resolution_and_config_roundtrip(tmp_path: Path) -> None:
    assert resolve_batch_workers(0, task_count=1) == 1
    assert resolve_batch_workers(1, task_count=8) == 1
    assert resolve_batch_workers(4, task_count=2) == 2
    assert threads_per_batch_worker(4) >= 1

    cfg_path = tmp_path / "cfg.json"
    save_config(AppConfig(batch_workers=3), cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.batch_workers == 3


def test_batch_fuse_tree_and_multi_tool_safety() -> None:
    """Verify _batch_fuse_safe merges overlapping multi-part bodies cleanly."""
    parts = [BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 60.0, 60.0, 10.0).Solid()]
    for ix in range(3):
        for iy in range(3):
            parts.append(
                BRepPrimAPI_MakeCylinder(
                    gp_Ax2(gp_Pnt(10.0 + ix * 20.0, 10.0 + iy * 20.0, 5.0), gp_Dir(0, 0, 1)),
                    4.0,
                    15.0,
                ).Solid()
            )
    logs: list[str] = []
    fused = _batch_fuse_safe(parts, logs, label="Test fuse")
    rep = validate_shape(fused)
    assert rep.solid_count == 1
    assert rep.is_watertight is True
    assert rep.volume > 36_000.0


def test_cli_parallel_batch_multi_file(tmp_path: Path) -> None:
    """CLI --cli with multiple STEP files and -j 2 processes all files in parallel."""
    src1 = generate_closed_enclosure_step(tmp_path / "m1.step")
    src2 = generate_closed_enclosure_step(tmp_path / "m2.step")
    rc = main(["--cli", str(src1), str(src2), "-j", "2", "--views", "26", "--grid", "80"])
    assert rc == 0

    for src in (src1, src2):
        out = src.with_name(f"{src.stem}_simplified.step")
        assert out.exists() and out.stat().st_size > 1000
        reloaded = load_step_assembly(out)
        assert len(reloaded.parts) == 1
        assert validate_shape(reloaded.parts[0].shape).is_valid is True
