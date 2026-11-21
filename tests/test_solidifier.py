"""Automated tests for 100% exterior-preserving exact B-Rep solidification."""

from __future__ import annotations

import math
from pathlib import Path

from simple_3d_step.core.solidifier import simplify_assembly_to_single_solid
from simple_3d_step.core.step_io import load_step_assembly, save_shape_to_step
from simple_3d_step.core.validator import validate_shape
from simple_3d_step.core.visibility import analyze_visibility
from simple_3d_step.models import SimplifyConfig, VisibilityConfig
from tests.generate_sample_step import (
    generate_closed_enclosure_step,
    generate_holed_box_step,
    generate_open_cavity_box_step,
)


def test_closed_enclosure_brep_exact_fill(tmp_path: Path) -> None:
    """Multi-part closed enclosure -> fused into 1 exact B-Rep Solid with 1 Shell and filled cavity."""
    step_file = generate_closed_enclosure_step(tmp_path / "closed.step")
    asm = load_step_assembly(step_file)
    analyze_visibility(asm, VisibilityConfig(num_views=32, grid_resolution=96))

    cfg = SimplifyConfig(
        fill_internal_core=True,
        core_bridge_gap_mm=2.0,
        core_inset_mm=2.0,
        remove_small_holes=False,
        unify_same_domain=True,
    )
    res = simplify_assembly_to_single_solid(asm, cfg)

    assert res.validation.solid_count == 1
    assert res.validation.shell_count == 1
    assert res.validation.is_watertight is True
    assert res.validation.is_valid is True
    # Pure exact cylinder: only 3 faces (1 Cylinder + 2 Planes)!
    assert res.validation.face_count <= 6
    assert res.validation.face_types.get("Cylinder", 0) >= 1

    expected_vol = math.pi * (50.0 ** 2) * 100.0
    assert abs(res.validation.volume - expected_vol) / expected_vol < 0.005

    # Round-trip STEP export and re-load validation
    out_step = save_shape_to_step(res.shape, tmp_path / "closed_simplified.step")
    reloaded = load_step_assembly(out_step)
    assert len(reloaded.parts) == 1
    rep2 = validate_shape(reloaded.parts[0].shape)
    assert rep2.solid_count == 1
    assert rep2.shell_count == 1
    assert abs(rep2.volume - expected_vol) / expected_vol < 0.005


def test_holed_box_small_hole_removal_and_fill(tmp_path: Path) -> None:
    """Hollow box with 6mm hole + side boss -> exact B-Rep outer walls preserved & interior filled."""
    step_file = generate_holed_box_step(tmp_path / "holed.step")
    asm = load_step_assembly(step_file)
    analyze_visibility(asm, VisibilityConfig(num_views=42, grid_resolution=120, min_visible_ratio=0.01))

    cfg = SimplifyConfig(
        fill_internal_core=True,
        core_bridge_gap_mm=4.0,
        core_inset_mm=2.0,
        remove_small_holes=True,
        max_hole_diameter=10.0,
        unify_same_domain=True,
    )
    res = simplify_assembly_to_single_solid(asm, cfg)

    assert res.validation.solid_count == 1
    assert res.validation.shell_count == 1
    assert res.validation.is_watertight is True
    assert res.validation.is_valid is True

    # 100^3 box + 10^3 side boss = 1,001,000
    expected_vol = 1_000_000.0 + 1_000.0
    assert abs(res.validation.volume - expected_vol) / expected_vol < 0.01


def test_open_cavity_box_preserves_exact_outer_planes(tmp_path: Path) -> None:
    """Open hollow box -> analytic core fills interior while outer 100x100x100 faces stay exact planes."""
    step_file = generate_open_cavity_box_step(tmp_path / "open.step")
    asm = load_step_assembly(step_file)
    analyze_visibility(asm, VisibilityConfig(num_views=42, grid_resolution=120, min_visible_ratio=0.02))

    cfg = SimplifyConfig(
        fill_internal_core=True,
        core_bridge_gap_mm=18.0,
        core_inset_mm=3.0,
        unify_same_domain=True,
    )
    res = simplify_assembly_to_single_solid(asm, cfg)

    assert res.validation.solid_count == 1
    assert res.validation.shell_count == 1
    assert res.validation.is_watertight is True
    assert res.validation.is_valid is True
    # Exact 100x100x100 outer bounding box preserved (zero voxel shrinkage/distortion!)
    bx, by, bz = res.validation.bbox_size
    assert abs(bx - 100.0) < 0.01
    assert abs(by - 100.0) < 0.01
    assert abs(bz - 100.0) < 0.01
    # Interior filled dramatically above hollow volume (~479,000)
    assert res.validation.volume > 850_000.0
