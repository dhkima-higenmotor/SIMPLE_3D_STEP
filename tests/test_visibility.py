"""Automated tests for 2-Pass Embree visibility-based internal part detection."""

from __future__ import annotations

from pathlib import Path

from simple_3d_step.core.step_io import load_step_assembly
from simple_3d_step.core.visibility import analyze_visibility
from simple_3d_step.models import PartStatus, VisibilityConfig
from tests.generate_sample_step import (
    generate_closed_enclosure_step,
    generate_holed_box_step,
)


def test_closed_enclosure_visibility(tmp_path: Path) -> None:
    step_file = generate_closed_enclosure_step(tmp_path / "closed_enclosure.step")
    asm = load_step_assembly(step_file)
    assert len(asm.parts) == 6

    cfg = VisibilityConfig(num_views=42, grid_resolution=120, min_visible_ratio=0.005)
    vis = analyze_visibility(asm, cfg)

    assert vis.external_count == 3
    assert vis.internal_count == 3

    by_name = {p.name: p for p in asm.parts}
    for ext_name in ("EXT_HOUSING", "EXT_BOTTOM_CAP", "EXT_TOP_CAP"):
        assert by_name[ext_name].auto_status == PartStatus.EXTERNAL
        assert by_name[ext_name].keep is True

    for int_name in ("INT_ROTOR_SHAFT", "INT_GEAR", "INT_PCB"):
        assert by_name[int_name].auto_status == PartStatus.INTERNAL
        assert by_name[int_name].keep is False
        assert by_name[int_name].visibility_score == 0.0


def test_holed_box_visibility_filters_peeking_internal_part(tmp_path: Path) -> None:
    step_file = generate_holed_box_step(tmp_path / "holed_box.step")
    asm = load_step_assembly(step_file)
    assert len(asm.parts) == 3

    cfg = VisibilityConfig(num_views=42, grid_resolution=128, min_visible_ratio=0.01)
    vis = analyze_visibility(asm, cfg)

    by_name = {p.name: p for p in asm.parts}
    assert by_name["EXT_HOLED_HOUSING"].keep is True
    assert by_name["EXT_SIDE_BOSS"].keep is True
    assert by_name["INT_CORE"].keep is False
    assert vis.external_count == 2
    assert vis.internal_count == 1
