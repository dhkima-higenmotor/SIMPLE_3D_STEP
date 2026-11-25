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
