"""Integration tests for the PCB Mode per-component single-solid simplification engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Cylinder
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from simple_3d_step.core.idf_parser import load_idf_pair
from simple_3d_step.core.occt_compat import to_face
from simple_3d_step.core.pcb_simplifier import simplify_pcb_assembly
from simple_3d_step.core.step_io import load_pcb_assembly, save_colored_assembly_to_step
from simple_3d_step.models import PcbSimplifyConfig
from tests.generate_sample_pcb import generate_sample_pcb_step_and_idf


def _count_type(shape, stype) -> int:
    c = 0
    exp = TopExp_Explorer(shape, stype)
    while exp.More():
        c += 1
        exp.Next()
    return c


def _has_cylindrical_face(shape) -> bool:
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        if BRepAdaptor_Surface(to_face(exp.Current()), False).GetType() == GeomAbs_Cylinder:
            return True
        exp.Next()
    return False


def test_pcb_simplification_full_pipeline_and_color_roundtrip(tmp_path: Path) -> None:
    step_path, bdf_path, ldf_path = generate_sample_pcb_step_and_idf(tmp_path)

    asm = load_pcb_assembly(step_path)
    assert len(asm.components) == 12  # Board + U1 + J1 + C1 + C2 + R1 + C10..C15
    assert asm.total_bodies > 35

    idf = load_idf_pair(bdf_path, ldf_path)
    assert idf is not None

    cfg = PcbSimplifyConfig(min_board_hole_mm=0.8)
    res = simplify_pcb_assembly(asm, cfg, idf_data=idf)

    assert res.board_count == 1
    assert res.component_count == 11
    assert res.cylinder_count == 2  # C1 (SMD cap + base) and C2 (radial cap)
    assert res.fused_count >= 2     # U1 (QFP + heatsink + leads) and J1 (housing + pins)
    assert res.removed_text_bodies == 2  # Text1 & Text2 dropped from U1
    assert res.idf_added_count == 2  # U2 (TOP) & R2 (BOTTOM); M1 zero-height HOLE skipped
    assert res.simplified_faces < res.original_faces

    by_ref = {p.ref_des or p.name: p for p in res.parts}

    # Board: preserves 3.2mm mounting holes (2 cylindrical faces), plugs 0.4mm vias
    board = by_ref["PCB_Board"]
    assert board.category == "board"
    assert _count_type(board.shape, TopAbs_SOLID) == 1
    board_cyl_faces = 0
    exp = TopExp_Explorer(board.shape, TopAbs_FACE)
    while exp.More():
        if BRepAdaptor_Surface(to_face(exp.Current()), False).GetType() == GeomAbs_Cylinder:
            board_cyl_faces += 1
        exp.Next()
    assert board_cyl_faces == 2  # 2 mounting holes kept, 2 tiny vias plugged
    assert board.color is not None and board.color[1] > board.color[0]  # Green

    # U1: QFP fused into 1 watertight solid, black body color preserved, vastly fewer faces
    u1 = by_ref["U1"]
    assert u1.category == "ic_fused"
    assert _count_type(u1.shape, TopAbs_SOLID) == 1
    assert u1.simplified_faces < u1.original_faces
    assert u1.color is not None and max(u1.color) < 0.20

    # J1: Connector housing + pin bank fused into 1 solid, white color preserved
    j1 = by_ref["J1"]
    assert _count_type(j1.shape, TopAbs_SOLID) == 1
    assert j1.color is not None and min(j1.color) > 0.80

    # C1 (SMD electrolytic cap with wide base) & C2 (pure radial cap): cylindrical, 1 solid each
    c1 = by_ref["C1"]
    c2 = by_ref["C2"]
    assert c1.category == "cylinder"
    assert c2.category == "cylinder"
    assert _count_type(c1.shape, TopAbs_SOLID) == 1
    assert _count_type(c2.shape, TopAbs_SOLID) == 1
    assert _has_cylindrical_face(c1.shape)
    assert _has_cylindrical_face(c2.shape)
    assert c2.simplified_faces == 3  # Pure cylinder: 1 lateral + 2 planar caps
    assert c2.color is not None and c2.color[2] > 0.5  # Blue

    # R1: 45-degree rotated SMD box retains tight volume (2.0 * 1.25 * 0.6 = 1.5 mm^3, NOT bloated AABB)
    r1 = by_ref["R1"]
    assert _count_type(r1.shape, TopAbs_SOLID) == 1
    assert r1.simplified_faces == 6

    # Export colored XCAF STEP and verify clean roundtrip (all 1-solid + all colored + RefDes & leaf names!)
    out_step = tmp_path / f"{step_path.stem}_pcb_simplified.step"
    saved = save_colored_assembly_to_step(
        res.parts,
        out_step,
        root_name=res.root_name,
        schema=cfg.step_schema,
        unit=res.unit,
        preserve_colors=True,
    )
    assert saved.is_file()
    assert saved.stat().st_size < step_path.stat().st_size

    reloaded = load_pcb_assembly(saved)
    assert len(reloaded.components) == len(res.parts)
    assert all(len(c.bodies) == 1 for c in reloaded.components)
    colored_reloaded = sum(1 for c in reloaded.components if c.bodies[0].color is not None)
    assert colored_reloaded == len(reloaded.components)
    re_by_ref = {c.ref_des: c for c in reloaded.components}
    assert "U1" in re_by_ref and re_by_ref["U1"].leaf_name != ""
    assert "J1" in re_by_ref
    assert "U2" in re_by_ref and re_by_ref["U2"].leaf_name == "QFN32"
    assert "R2" in re_by_ref and re_by_ref["R2"].leaf_name == "R3216"


def test_pcb_exclude_small_parts_and_min_brightness_lift(tmp_path: Path) -> None:
    from simple_3d_step.core.pcb_simplifier import _ensure_min_brightness

    # 1. Brightness lift: pitch black (0,0,0) -> readable dark charcoal, bright green untouched
    lifted_black = _ensure_min_brightness((0.0, 0.0, 0.0), 0.12)
    assert lifted_black == pytest.approx((0.12, 0.12, 0.12))
    lifted_dark_blue = _ensure_min_brightness((0.01, 0.02, 0.08), 0.12)
    lum = 0.2126 * lifted_dark_blue[0] + 0.7152 * lifted_dark_blue[1] + 0.0722 * lifted_dark_blue[2]
    assert lum == pytest.approx(0.12, abs=1e-3)
    assert lifted_dark_blue[2] > lifted_dark_blue[0]  # Hue preserved
    green = _ensure_min_brightness((0.0, 0.22, 0.0), 0.12)
    assert green == pytest.approx((0.0, 0.22, 0.0))  # Already bright enough

    # 2. Exclude small parts <= 2.5 mm drops R1 (2.0mm) and C10..C15 (2.0mm), keeps large parts & board
    step_path, bdf_path, ldf_path = generate_sample_pcb_step_and_idf(tmp_path, stem="small_filter")
    asm = load_pcb_assembly(step_path)
    idf = load_idf_pair(bdf_path, ldf_path)
    cfg = PcbSimplifyConfig(
        min_board_hole_mm=0.8,
        exclude_small_part_mm=2.5,
        min_color_brightness=0.14,
    )
    res = simplify_pcb_assembly(asm, cfg, idf_data=idf)

    # 7 small STEP parts excluded (R1 + C10..C15); IDF must NOT re-add R1!
    assert res.excluded_small_count == 7
    refs = {p.ref_des for p in res.parts}
    assert "PCB_Board" in refs
    assert "U1" in refs
    assert "J1" in refs
    assert "C1" in refs
    assert "C2" in refs
    assert "R1" not in refs
    assert "C10" not in refs

    # Verify U1's very dark (0.08, 0.08, 0.09) color was lifted to >= 0.14 luminance
    u1 = next(p for p in res.parts if p.ref_des == "U1")
    assert u1.color is not None
    u1_lum = 0.2126 * u1.color[0] + 0.7152 * u1.color[1] + 0.0722 * u1.color[2]
    assert u1_lum >= 0.139
