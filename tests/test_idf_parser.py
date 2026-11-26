"""Unit tests for Altium / standard IDF (.bdf/.ldf) parsing, discovery, and alignment."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simple_3d_step.core.idf_parser import (
    StepComponentFootprint,
    align_idf_to_step_board,
    find_matching_idf_files,
    load_idf_pair,
    supplement_missing_idf_parts,
)


def test_find_matching_idf_files_prefers_bdf_ldf_and_supports_fallbacks(tmp_path: Path) -> None:
    step1 = tmp_path / "board_a.step"
    bdf1 = tmp_path / "board_a.BDF"
    ldf1 = tmp_path / "board_a.LDF"
    for p in (step1, bdf1, ldf1):
        p.write_text("dummy", encoding="utf-8")

    b, l = find_matching_idf_files(step1)
    assert b == bdf1.resolve()
    assert l == ldf1.resolve()

    # Standard .emn/.emp fallback
    step2 = tmp_path / "board_b.stp"
    emn2 = tmp_path / "board_b.emn"
    emp2 = tmp_path / "board_b.emp"
    for p in (step2, emn2, emp2):
        p.write_text("dummy", encoding="utf-8")

    b2, l2 = find_matching_idf_files(step2)
    assert b2 == emn2.resolve()
    assert l2 == emp2.resolve()


def test_parse_idf_thou_units_and_idf2_format(tmp_path: Path) -> None:
    bdf = tmp_path / "imperial.bdf"
    ldf = tmp_path / "imperial.ldf"
    bdf.write_text(
        """.HEADER
BOARD_FILE 2.0 "Altium" 2026-01-01 1
IMP_BOARD THOU
.END_HEADER
.BOARD_OUTLINE
62.9921
0 0 0 0
0 2000 0 0
0 2000 1000 0
0 0 1000 0
0 0 0 0
.END_BOARD_OUTLINE
.PLACEMENT
"PKG A" PART1 U1
1000 500 90 TOP PLACED
"PKG A" PART1 U2
500 500 0 BOTTOM UNPLACED
.END_PLACEMENT
""",
        encoding="utf-8",
    )
    ldf.write_text(
        """.HEADER
LIBRARY_FILE 2.0 "Altium" 2026-01-01 1
.END_HEADER
.ELECTRICAL
"PKG A" PART1 THOU 100
0 -100 -50 0
0 100 -50 0
0 100 50 0
0 -100 50 0
0 -100 -50 0
.END_ELECTRICAL
""",
        encoding="utf-8",
    )

    data = load_idf_pair(bdf, ldf)
    assert data is not None
    assert data.board_thickness_mm == pytest.approx(62.9921 * 0.0254, rel=1e-3)
    assert data.board_bbox_xy is not None
    assert data.board_bbox_xy[2] == pytest.approx(2000 * 0.0254, rel=1e-3)
    assert len(data.placements) == 2
    assert data.placements[0].is_placed is True
    assert data.placements[0].side == "TOP"
    assert data.placements[0].rotation_deg == pytest.approx(90.0)
    assert data.placements[1].is_placed is False

    fp = data.lookup_footprint("PKG A", "PART1")
    assert fp is not None
    assert fp.width_mm == pytest.approx(200 * 0.0254, rel=1e-3)
    assert fp.height_mm == pytest.approx(100 * 0.0254, rel=1e-3)


def test_align_and_supplement_missing_idf_parts(tmp_path: Path) -> None:
    bdf = tmp_path / "b.bdf"
    ldf = tmp_path / "b.ldf"
    bdf.write_text(
        """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-25 1
TEST MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.6
0 0 0 0
0 50 0 0
0 50 30 0
0 0 30 0
0 0 0 0
.END_BOARD_OUTLINE
.PLACEMENT
IC8 P1 U1
10 15 0 0 TOP PLACED
IC8 P1 U2
35 15 0 0 TOP PLACED
R1206 P2 R9
25 15 0 90 BOTTOM PLACED
"HOLE 3MM" H1 M1
5 5 0 0 TOP PLACED
.END_PLACEMENT
""",
        encoding="utf-8",
    )
    ldf.write_text(
        """.HEADER
LIBRARY_FILE 3.0 "Altium DXP" 2026-11-25 1
.END_HEADER
.ELECTRICAL
IC8 P1 MM 2.0
0 -2 -2 0
0 2 -2 0
0 2 2 0
0 -2 2 0
0 -2 -2 0
.END_ELECTRICAL
.ELECTRICAL
R1206 P2 MM 1.0
0 -1.6 -0.8 0
0 1.6 -0.8 0
0 1.6 0.8 0
0 -1.6 0.8 0
0 -1.6 -0.8 0
.END_ELECTRICAL
.ELECTRICAL
"HOLE 3MM" H1 MM 0.0
0 -2 -2 0
0 2 -2 0
0 2 2 0
0 -2 2 0
0 -2 -2 0
.END_ELECTRICAL
""",
        encoding="utf-8",
    )

    idf = load_idf_pair(bdf, ldf)
    assert idf is not None

    # STEP board is shifted by (+100, +200) and z in [-1.6, 0.0]; only U1 exists in STEP (named "999")
    board_min = np.array([100.0, 200.0, -1.6])
    board_max = np.array([150.0, 230.0, 0.0])
    existing = [
        StepComponentFootprint(
            name="999",
            bbox_min=np.array([108.0, 213.0, 0.0]),
            bbox_max=np.array([112.0, 217.0, 2.0]),
        )
    ]

    al = align_idf_to_step_board(idf, board_min, board_max, existing)
    assert al.dx == pytest.approx(100.0, abs=0.1)
    assert al.dy == pytest.approx(200.0, abs=0.1)

    added, matched, added_cnt, renamed = supplement_missing_idf_parts(
        idf, board_min, board_max, existing
    )
    assert matched == 1
    assert added_cnt == 2  # U2 (TOP) and R9 (BOTTOM); M1 zero-height HOLE is skipped
    assert renamed[0].startswith("U1")
    names = {p.name for p in added}
    assert any(n.startswith("U2") for n in names)
    assert any(n.startswith("R9") for n in names)
    assert all(p.solid_count == 1 and p.simplified_faces == 6 for p in added)


def test_idf_only_board_holes_arcs_and_pcb_step_generation(tmp_path: Path) -> None:
    """Verify pure IDF-only PCB generation (without any STEP file):
    - Rounded-corner board outline with internal cutout and .DRILLED_HOLES (hole size filtering)
    - Circular footprint -> 3-face cylinder; rectangular footprints -> 6-face boxes (TOP + BOTTOM)
    - Clean colored flat XCAF STEP export and reload roundtrip
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    from simple_3d_step.core.occt_compat import to_face
    from simple_3d_step.core.pcb_simplifier import simplify_pcb_from_idf
    from simple_3d_step.core.step_io import load_pcb_assembly, save_colored_assembly_to_step
    from simple_3d_step.models import PcbSimplifyConfig

    bdf = tmp_path / "idf_only_board.bdf"
    ldf = tmp_path / "idf_only_board.ldf"
    # 60 x 40 mm board with one 5 mm rounded arc corner, one 6x4 mm internal cutout,
    # two 3.2 mm mounting holes (kept) and two 0.4 mm via holes (filtered out by min_board_hole_mm=0.8)
    bdf.write_text(
        """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-26 1
PURE_IDF_PCB MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.6
0 0.0 0.0 0.0
0 55.0 0.0 0.0
0 60.0 5.0 90.0
0 60.0 40.0 0.0
0 0.0 40.0 0.0
0 0.0 0.0 0.0
1 25.0 18.0 0.0
1 31.0 18.0 0.0
1 31.0 22.0 0.0
1 25.0 22.0 0.0
1 25.0 18.0 0.0
.END_BOARD_OUTLINE
.DRILLED_HOLES
3.2 4.0 4.0 NPTH BOARD MTG ECAD
3.2 56.0 36.0 NPTH BOARD MTG ECAD
0.4 15.0 15.0 PTH BOARD VIA ECAD
0.4 18.0 15.0 PTH BOARD VIA ECAD
.END_DRILLED_HOLES
.PLACEMENT
QFP32 MCU U1
15.0 25.0 0.0 0.0 TOP PLACED
CAP_RAD 100uF C1
45.0 20.0 0.0 0.0 TOP PLACED
R1206 10K R1
40.0 10.0 0.0 90.0 BOTTOM PLACED
R0402 TINY R2
10.0 10.0 0.0 0.0 TOP PLACED
"HOLE 3.2MM" MTG M1
4.0 4.0 0.0 0.0 TOP PLACED
QFP32 UNPL U9
20.0 30.0 0.0 0.0 TOP UNPLACED
.END_PLACEMENT
""",
        encoding="utf-8",
    )
    ldf.write_text(
        """.HEADER
LIBRARY_FILE 3.0 "Altium DXP" 2026-11-26 1
.END_HEADER
.ELECTRICAL
QFP32 MCU MM 2.0
0 -4.0 -4.0 0.0
0 4.0 -4.0 0.0
0 4.0 4.0 0.0
0 -4.0 4.0 0.0
0 -4.0 -4.0 0.0
.END_ELECTRICAL
.ELECTRICAL
CAP_RAD 100uF MM 8.5
0 0.0 0.0 0.0
0 3.0 0.0 360.0
.END_ELECTRICAL
.ELECTRICAL
R1206 10K MM 0.8
0 -1.6 -0.8 0.0
0 1.6 -0.8 0.0
0 1.6 0.8 0.0
0 -1.6 0.8 0.0
0 -1.6 -0.8 0.0
.END_ELECTRICAL
.ELECTRICAL
R0402 TINY MM 0.4
0 -0.5 -0.25 0.0
0 0.5 -0.25 0.0
0 0.5 0.25 0.0
0 -0.5 0.25 0.0
0 -0.5 -0.25 0.0
.END_ELECTRICAL
.ELECTRICAL
"HOLE 3.2MM" MTG MM 0.0
0 -1.6 -1.6 0.0
0 1.6 -1.6 0.0
0 1.6 1.6 0.0
0 -1.6 1.6 0.0
0 -1.6 -1.6 0.0
.END_ELECTRICAL
""",
        encoding="utf-8",
    )

    idf = load_idf_pair(bdf, ldf)
    assert idf is not None
    assert len(idf.drilled_holes) == 4
    assert 0 in idf.board_outline_loops and 1 in idf.board_outline_loops
    cap_fp = idf.lookup_footprint("CAP_RAD", "100uF")
    assert cap_fp is not None and cap_fp.is_circle and cap_fp.circle_radius_mm == pytest.approx(3.0)

    cfg = PcbSimplifyConfig(min_board_hole_mm=0.8, exclude_small_part_mm=1.2)
    res = simplify_pcb_from_idf(idf, cfg)

    assert res.board_count == 1
    assert res.idf_added_count == 3  # U1, C1, R1 (R2 excluded <= 1.2mm, M1 zero-height hole, U9 unplaced)
    assert res.cylinder_count == 1   # C1 circular footprint -> cylinder
    assert res.excluded_small_count == 1

    by_name = {p.name: p for p in res.parts}
    assert "PCB_Board" in by_name
    assert "U1_QFP32" in by_name
    assert "C1_CAP_RAD" in by_name
    assert "R1_R1206" in by_name

    # Validate board solid: watertight, valid, keeps 1 rounded corner + 2 mounting holes (3 cyl faces), skips 0.4mm vias
    board = by_name["PCB_Board"]
    assert BRepCheck_Analyzer(board.shape).IsValid()
    cyl_faces = 0
    exp = TopExp_Explorer(board.shape, TopAbs_FACE)
    while exp.More():
        if BRepAdaptor_Surface(to_face(exp.Current()), False).GetType() == GeomAbs_Cylinder:
            cyl_faces += 1
        exp.Next()
    assert cyl_faces == 3  # 1 arc corner + 2 mounting holes (0.4mm vias filtered out)

    # Validate C1 cylinder (3 faces)
    assert by_name["C1_CAP_RAD"].category == "cylinder"
    assert by_name["C1_CAP_RAD"].simplified_faces == 3

    # Export to STEP and reload roundtrip
    out_step = tmp_path / "idf_only_board_pcb_simplified.step"
    saved = save_colored_assembly_to_step(
        res.parts, out_step, root_name=res.root_name, schema=cfg.step_schema, preserve_colors=True
    )
    assert saved.is_file()
    reloaded = load_pcb_assembly(saved)
    assert len(reloaded.components) == 4  # PCB_Board + U1 + C1 + R1
    assert all(len(c.bodies) == 1 for c in reloaded.components)
    re_names = {c.name for c in reloaded.components}
    assert {"PCB_Board", "U1_QFP32", "C1_CAP_RAD", "R1_R1206"}.issubset(re_names)


def test_idf_only_bdf_without_ldf_still_generates_step(tmp_path: Path) -> None:
    """Even when only a .bdf file is provided (no .ldf library), a valid PCB STEP is generated."""
    from simple_3d_step.core.pcb_simplifier import simplify_pcb_from_idf
    from simple_3d_step.core.step_io import load_pcb_assembly, save_colored_assembly_to_step
    from simple_3d_step.models import PcbSimplifyConfig

    bdf = tmp_path / "board_only.bdf"
    bdf.write_text(
        """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-26 1
BDF_ONLY MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.6
0 0 0 0
0 40 0 0
0 40 25 0
0 0 25 0
0 0 0 0
.END_BOARD_OUTLINE
.PLACEMENT
PKG1 P1 U1
20 12 0 0 TOP PLACED
.END_PLACEMENT
""",
        encoding="utf-8",
    )
    idf = load_idf_pair(bdf, None)
    assert idf is not None and idf.ldf_path is None

    res = simplify_pcb_from_idf(idf, PcbSimplifyConfig())
    assert res.board_count == 1
    assert res.idf_added_count == 1
    saved = save_colored_assembly_to_step(
        res.parts, tmp_path / "board_only_pcb_simplified.step", root_name=res.root_name
    )
    reloaded = load_pcb_assembly(saved)
    assert len(reloaded.components) == 2


def test_idf_board_outline_with_altium_backtracking_spikes_keeps_valid_top_bottom_faces(
    tmp_path: Path,
) -> None:
    """Regression test: Altium IDF exports sometimes emit 180-deg backtracking micro-spikes
    (~0.06 mm) and sub-micron slivers along curved mounting-tab outlines.
    Without outline cleanup, the planar wire self-intersects and the board loses its top/bottom
    triangulated faces and hole cuts."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location

    from simple_3d_step.core.idf_parser import build_idf_board_shape
    from simple_3d_step.core.occt_compat import to_face
    from simple_3d_step.core.pcb_simplifier import simplify_pcb_from_idf
    from simple_3d_step.core.step_io import load_pcb_assembly, save_colored_assembly_to_step
    from simple_3d_step.models import PcbSimplifyConfig

    bdf = tmp_path / "altium_spikes.bdf"
    bdf.write_text(
        """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-26 1
SPIKE_BOARD MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.5800
0 0.0000 0.0000 0.0000
0 20.0000 0.0000 0.0000
0 20.0006 -0.0004 0.0000
0 30.0000 0.0000 0.0000
0 30.0000 10.0000 0.0000
0 30.0000 13.4000 -180.0000
0 30.9000 13.4000 0.0000
0 30.8400 13.4000 0.0000
0 30.8400 25.0000 0.0000
0 0.0000 25.0000 0.0000
0 0.0000 0.0000 0.0000
.END_BOARD_OUTLINE
.DRILLED_HOLES
3.2000 5.0000 5.0000 NPTH BOARD MTG ECAD
0.3000 15.0000 12.0000 PTH BOARD VIA ECAD
.END_DRILLED_HOLES
""",
        encoding="utf-8",
    )

    idf = load_idf_pair(bdf, None)
    assert idf is not None

    board_sh, _zb, _zt, kept_holes, skipped_holes = build_idf_board_shape(
        idf, min_board_hole_mm=0.8
    )
    assert board_sh is not None
    assert kept_holes == 1
    assert skipped_holes == 1
    assert BRepCheck_Analyzer(board_sh).IsValid()

    res = simplify_pcb_from_idf(idf, PcbSimplifyConfig(min_board_hole_mm=0.8))
    saved = save_colored_assembly_to_step(
        res.parts, tmp_path / "altium_spikes_pcb_simplified.step", root_name=res.root_name
    )
    reloaded = load_pcb_assembly(saved)
    assert len(reloaded.components) == 1
    sh = reloaded.components[0].bodies[0].shape
    assert BRepCheck_Analyzer(sh).IsValid()

    # Every single face (including top & bottom planar faces) MUST triangulate cleanly!
    BRepMesh_IncrementalMesh(sh, 0.1, False, 0.5, True)
    exp = TopExp_Explorer(sh, TopAbs_FACE)
    total_faces = 0
    unmeshed = 0
    while exp.More():
        total_faces += 1
        loc = TopLoc_Location()
        if BRep_Tool.Triangulation_s(to_face(exp.Current()), loc) is None:
            unmeshed += 1
        exp.Next()
    assert total_faces >= 9
    assert unmeshed == 0
