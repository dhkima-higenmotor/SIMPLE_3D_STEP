"""Automated tests for 100% exterior-preserving exact B-Rep solidification."""

from __future__ import annotations

import math
import re
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shell

from simple_3d_step.core.solidifier import (
    _audit_prs_closed_meshing,
    _count_lacking_wires,
    _extract_solids,
    _purge_final_closed_void_shells,
    _remove_closed_void_shells,
    _split_tangent_pinch_wire_faces,
    simplify_assembly_to_single_solid,
)
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
    # Pure exact cylinder split cleanly into 2 180-deg halves + 2 end caps (zero seam defects)
    assert res.validation.face_count <= 8
    assert res.validation.face_types.get("Cylinder", 0) >= 1
    assert _count_lacking_wires(res.shape) == 0

    expected_vol = math.pi * (50.0 ** 2) * 100.0
    assert abs(res.validation.volume - expected_vol) / expected_vol < 0.005

    # Round-trip STEP export and cross-kernel compatibility validation (CAD Assistant / OCCT 7.x)
    out_step = save_shape_to_step(res.shape, tmp_path / "closed_simplified.step")
    step_txt = out_step.read_text(encoding="latin-1", errors="ignore")
    assert "MANIFOLD_SOLID_BREP" in step_txt
    assert "CLOSED_SHELL" in step_txt
    assert "PCURVE(" not in step_txt
    unc_match = re.search(r"UNCERTAINTY_MEASURE_WITH_UNIT\s*\(\s*LENGTH_MEASURE\s*\(\s*([0-9.eE+-]+)\s*\)", step_txt)
    assert unc_match is not None
    unc_val = float(unc_match.group(1))
    assert 1e-5 <= unc_val <= 1e-3

    reloaded = load_step_assembly(out_step)
    assert len(reloaded.parts) == 1
    rep2 = validate_shape(reloaded.parts[0].shape)
    assert rep2.solid_count == 1
    assert rep2.shell_count == 1
    assert rep2.is_valid is True
    assert _count_lacking_wires(reloaded.parts[0].shape) == 0
    failed_profiles, failing_faces = _audit_prs_closed_meshing(reloaded.parts[0].shape)
    assert failed_profiles == 0
    assert not failing_faces
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


def test_purge_internal_closed_void_shells_multi_and_single_shell() -> None:
    """Post-processing removes both explicit multi-shell voids and hidden single-shell voids."""
    outer = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 40.0, 40.0, 40.0).Solid()
    inner1 = BRepPrimAPI_MakeBox(gp_Pnt(5.0, 5.0, 5.0), 10.0, 10.0, 10.0).Solid()
    inner2 = BRepPrimAPI_MakeBox(gp_Pnt(22.0, 22.0, 22.0), 10.0, 10.0, 10.0).Solid()

    # Case 1: Explicit multi-shell solid with 2 internal closed cavities (3 shells total)
    hollow = BRepAlgoAPI_Cut(BRepAlgoAPI_Cut(outer, inner1).Shape(), inner2).Shape()
    hollow_solid = _extract_solids(hollow)[0]
    rep_before = validate_shape(hollow_solid)
    assert rep_before.shell_count == 3
    assert abs(rep_before.volume - (64_000.0 - 2_000.0)) < 1.0

    logs: list[str] = []
    purged = _purge_final_closed_void_shells(hollow_solid, logs)
    rep_after = validate_shape(purged)
    assert rep_after.solid_count == 1
    assert rep_after.shell_count == 1
    assert rep_after.is_valid is True
    assert abs(rep_after.volume - 64_000.0) < 1.0

    # Case 2: Hidden disconnected inward-facing void faces packed into a single TopoDS_Shell
    builder = BRep_Builder()
    merged_shell = TopoDS_Shell()
    builder.MakeShell(merged_shell)
    ef_out = TopExp_Explorer(outer, TopAbs_FACE)
    while ef_out.More():
        builder.Add(merged_shell, ef_out.Current())
        ef_out.Next()
    ef_in = TopExp_Explorer(inner1, TopAbs_FACE)
    while ef_in.More():
        builder.Add(merged_shell, ef_in.Current().Reversed())
        ef_in.Next()
    merged_shell.Closed(True)
    hidden_solid = BRepBuilderAPI_MakeSolid(merged_shell).Solid()

    cleaned_solid, n_removed, _ = _remove_closed_void_shells(hidden_solid)
    assert n_removed == 1
    rep_hidden = validate_shape(cleaned_solid)
    assert rep_hidden.solid_count == 1
    assert rep_hidden.shell_count == 1
    assert rep_hidden.face_count == 6
    assert abs(rep_hidden.volume - 64_000.0) < 1.0


def test_tangent_pinch_wire_hole_healed_for_cad_assistant() -> None:
    """Planar face with an internally-tangent circular hole splits cleanly with 0 unmeshed faces."""
    outer = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0, 0, 1)), 30.3, 10.0).Solid()
    # Hole of R=2.3 centered at X=28.0 -> 28.0 + 2.3 = 30.3 (exact internal tangency with outer rim)
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(28.0, 0.0, -1.0), gp_Dir(0, 0, 1)), 2.3, 12.0).Solid()
    cut_shape = BRepAlgoAPI_Cut(outer, hole).Shape()
    solid = _extract_solids(cut_shape)[0]
    rep_orig = validate_shape(solid)
    assert rep_orig.solid_count == 1

    # Pass all face indices to _split_tangent_pinch_wire_faces (non-pinched faces are skipped)
    all_indices = set(range(rep_orig.face_count))
    repaired, pinch_holes = _split_tangent_pinch_wire_faces(solid, all_indices)
    assert repaired is not None
    assert pinch_holes >= 2  # Top & bottom planar caps each have 1 tangent pinch-wire hole

    rep_new = validate_shape(repaired)
    assert rep_new.solid_count == 1
    assert rep_new.shell_count == 1
    assert rep_new.is_watertight is True
    assert rep_new.is_valid is True
    assert abs(rep_new.volume - rep_orig.volume) / rep_orig.volume < 1e-4

    failed_profiles, failing_faces = _audit_prs_closed_meshing(repaired, fast=False)
    assert failed_profiles == 0
    assert not failing_faces
