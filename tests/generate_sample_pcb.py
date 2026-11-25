"""Synthetic Altium-style PCB STEP + IDF (.bdf/.ldf) generator for automated tests."""

from __future__ import annotations

import math
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_DocumentTool


def _make_box(x0: float, y0: float, z0: float, w: float, d: float, h: float):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), w, d, h).Shape()


def _make_cyl(cx: float, cy: float, z0: float, r: float, h: float):
    ax = gp_Ax2(gp_Pnt(cx, cy, z0), gp_Dir(0.0, 0.0, 1.0))
    return BRepPrimAPI_MakeCylinder(ax, r, h).Shape()


def generate_sample_pcb_step_and_idf(
    out_dir: Path,
    stem: str = "sample_altium_pcb",
) -> tuple[Path, Path, Path]:
    """Generate a colored Altium-like PCB STEP assembly and matching .bdf / .ldf IDF files.

    Board: 60 x 40 x 1.6 mm (z in [-1.6, 0.0]), green solder mask, 2 mounting holes (3.2 mm),
           2 tiny via holes (0.4 mm).
    Components in STEP:
      - PCB_Board (green)
      - U1: QFP IC (10x10x1.5 black body + top heatsink + 16 leads + 2 tiny engraved text solids)
      - J1: Pin header connector (12x4x6 white housing + 8 gold pins)
      - C1: SMD electrolytic capacitor (r=3.5, h=8.0 aluminum can + 8.5x8.5x0.8 black square base)
      - C2: Radial electrolytic capacitor (pure cylinder r=3.0, h=9.0 blue can)
      - R1: 45-degree rotated small SMD resistor (2.0x1.25x0.6 brown body + 2 silver caps)
      - R10..R15: 6 identical SMD capacitors (tests prototype instancing compression)
    IDF (.bdf + .ldf) includes all STEP components PLUS:
      - U2 (TOP, 6x6x1.5 mm) — missing from STEP -> should be supplemented as box
      - R2 (BOTTOM, 3.2x1.6x0.8 mm) — missing from STEP -> should be supplemented as box
      - M1 (HOLE 3.2MM, height 0.0 mm) — zero-height mounting hole -> should NOT be added
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    step_path = out_dir / f"{stem}.step"
    bdf_path = out_dir / f"{stem}.bdf"
    ldf_path = out_dir / f"{stem}.ldf"

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    root = st.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString("PCB"))
    ident = TopLoc_Location()

    def _set_col(lbl, rgb: tuple[float, float, float]) -> None:
        qc = Quantity_Color(rgb[0], rgb[1], rgb[2], Quantity_TOC_RGB)
        ct.SetColor(lbl, qc, XCAFDoc_ColorGen)
        ct.SetColor(lbl, qc, XCAFDoc_ColorSurf)

    def _add_component(name: str, sub_items: list[tuple[str, object, tuple[float, float, float]]]) -> None:
        if len(sub_items) == 1:
            _, sh, rgb = sub_items[0]
            pl = st.AddShape(sh, False)
            TDataStd_Name.Set_s(pl, TCollection_ExtendedString(name))
            _set_col(pl, rgb)
            cl = st.AddComponent(root, pl, ident)
            TDataStd_Name.Set_s(cl, TCollection_ExtendedString(name))
            return
        comp_asm = st.NewShape()
        TDataStd_Name.Set_s(comp_asm, TCollection_ExtendedString(name))
        for sub_name, sh, rgb in sub_items:
            sl = st.AddShape(sh, False)
            TDataStd_Name.Set_s(sl, TCollection_ExtendedString(sub_name))
            _set_col(sl, rgb)
            scl = st.AddComponent(comp_asm, sl, ident)
            TDataStd_Name.Set_s(scl, TCollection_ExtendedString(sub_name))
        cl = st.AddComponent(root, comp_asm, ident)
        TDataStd_Name.Set_s(cl, TCollection_ExtendedString(name))

    # 1. Green PCB board: [0..60] x [0..40] x [-1.6..0.0] with 2 mounting holes (3.2mm) & 2 vias (0.4mm)
    board = _make_box(0.0, 0.0, -1.6, 60.0, 40.0, 1.6)
    for hx, hy, dia in [(4.0, 4.0, 3.2), (56.0, 36.0, 3.2), (25.0, 20.0, 0.4), (27.0, 20.0, 0.4)]:
        hole = _make_cyl(hx, hy, -2.0, 0.5 * dia, 2.4)
        board = BRepAlgoAPI_Cut(board, hole).Shape()
    _add_component("PCB_Board", [("Board", board, (0.0, 0.35, 0.08))])

    # 2. U1: QFP IC centered at (15, 20), TOP (z >= 0)
    u1_parts: list[tuple[str, object, tuple[float, float, float]]] = [
        ("Body", _make_box(10.0, 15.0, 0.2, 10.0, 10.0, 1.5), (0.08, 0.08, 0.09)),
        ("Heatsink", _make_box(12.0, 17.0, 1.7, 6.0, 6.0, 0.8), (0.75, 0.75, 0.78)),
        # Two tiny embossed text solids on top of body
        ("Text1", _make_box(11.0, 16.0, 1.7, 0.6, 0.4, 0.05), (0.9, 0.9, 0.9)),
        ("Text2", _make_box(11.8, 16.0, 1.7, 0.6, 0.4, 0.05), (0.9, 0.9, 0.9)),
    ]
    # 4 leads on each of 4 sides (16 leads total)
    for k in range(4):
        offset = -3.0 + k * 2.0
        u1_parts.append((f"LeadL{k}", _make_box(8.8, 20.0 + offset - 0.2, 0.0, 1.2, 0.4, 0.5), (0.8, 0.8, 0.85)))
        u1_parts.append((f"LeadR{k}", _make_box(20.0, 20.0 + offset - 0.2, 0.0, 1.2, 0.4, 0.5), (0.8, 0.8, 0.85)))
        u1_parts.append((f"LeadF{k}", _make_box(15.0 + offset - 0.2, 13.8, 0.0, 0.4, 1.2, 0.5), (0.8, 0.8, 0.85)))
        u1_parts.append((f"LeadB{k}", _make_box(15.0 + offset - 0.2, 25.0, 0.0, 0.4, 1.2, 0.5), (0.8, 0.8, 0.85)))
    _add_component("U1", u1_parts)

    # 3. J1: Connector centered at (42, 32), TOP
    j1_parts: list[tuple[str, object, tuple[float, float, float]]] = [
        ("Housing", _make_box(36.0, 30.0, 0.0, 12.0, 4.0, 6.0), (0.92, 0.92, 0.88)),
    ]
    for k in range(8):
        px = 37.0 + k * 1.4
        j1_parts.append((f"Pin{k}", _make_box(px, 31.7, -2.0, 0.5, 0.5, 2.2), (0.92, 0.72, 0.15)))
    _add_component("J1", j1_parts)

    # 4. C1: SMD electrolytic capacitor with wide square base centered at (35, 14), TOP
    c1_parts = [
        ("Can", _make_cyl(35.0, 14.0, 0.8, 3.5, 8.0), (0.78, 0.80, 0.84)),
        ("Base", _make_box(35.0 - 4.25, 14.0 - 4.25, 0.0, 8.5, 8.5, 0.8), (0.08, 0.08, 0.08)),
    ]
    _add_component("C1", c1_parts)

    # 5. C2: Radial electrolytic capacitor (pure cylinder) at (48, 14), TOP
    _add_component("C2", [("Can", _make_cyl(48.0, 14.0, 0.0, 3.0, 9.0), (0.10, 0.25, 0.65))])

    # 6. R1: 45-degree rotated SMD resistor at (25, 8), TOP
    rot45 = gp_Trsf()
    rot45.SetRotation(gp_Ax1(gp_Pnt(25.0, 8.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), math.radians(45.0))
    r1_body = BRepBuilderAPI_Transform(_make_box(24.0, 7.375, 0.0, 2.0, 1.25, 0.6), rot45, True).Shape()
    _add_component("R1", [("Body", r1_body, (0.35, 0.15, 0.05))])

    # 7. Six identical C2012 capacitors (tests XCAF prototype instancing)
    for k in range(6):
        cx = 10.0 + k * 3.0
        _add_component(
            f"C{10 + k}",
            [
                ("Ceramic", _make_box(cx - 0.6, 4.4, 0.0, 1.2, 1.2, 1.0), (0.40, 0.18, 0.04)),
                ("CapL", _make_box(cx - 1.0, 4.4, 0.0, 0.4, 1.2, 1.0), (0.75, 0.78, 0.82)),
                ("CapR", _make_box(cx + 0.6, 4.4, 0.0, 0.4, 1.2, 1.0), (0.75, 0.78, 0.82)),
            ],
        )

    st.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    writer.Write(str(step_path))

    # Write matching Altium IDF 3.0 .bdf & .ldf
    bdf_text = """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-25.12:00:00 1
SAMPLE_PCB MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.6000
0 0.0000 0.0000 0.0000
0 60.0000 0.0000 0.0000
0 60.0000 40.0000 0.0000
0 0.0000 40.0000 0.0000
0 0.0000 0.0000 0.0000
.END_BOARD_OUTLINE
.PLACEMENT
QFP64 IC_MAIN U1
15.0000 20.0000 0.0000 0.0000 TOP PLACED
HDR8 CON_8P J1
42.0000 32.0000 0.0000 0.0000 TOP PLACED
CAP_SMD 100uF C1
35.0000 14.0000 0.0000 0.0000 TOP PLACED
CAP_RAD 47uF C2
48.0000 14.0000 0.0000 0.0000 TOP PLACED
R2012 10K R1
25.0000 8.0000 0.0000 45.0000 TOP PLACED
QFN32 IC_MISSING U2
50.0000 26.0000 0.0000 30.0000 TOP PLACED
R3216 SHUNT_BOT R2
30.0000 28.0000 0.0000 90.0000 BOTTOM PLACED
"HOLE 3.2MM" MNT_HOLE M1
4.0000 4.0000 0.0000 0.0000 TOP PLACED
.END_PLACEMENT
"""
    ldf_text = """.HEADER
LIBRARY_FILE 3.0 "Altium DXP" 2026-11-25.12:00:00 1
.END_HEADER
.ELECTRICAL
QFP64 IC_MAIN MM 2.5000
0 -6.2 -6.2 0.0
0 6.2 -6.2 0.0
0 6.2 6.2 0.0
0 -6.2 6.2 0.0
0 -6.2 -6.2 0.0
.END_ELECTRICAL
.ELECTRICAL
HDR8 CON_8P MM 6.0000
0 -6.0 -2.0 0.0
0 6.0 -2.0 0.0
0 6.0 2.0 0.0
0 -6.0 2.0 0.0
0 -6.0 -2.0 0.0
.END_ELECTRICAL
.ELECTRICAL
CAP_SMD 100uF MM 8.8000
0 -4.25 -4.25 0.0
0 4.25 -4.25 0.0
0 4.25 4.25 0.0
0 -4.25 4.25 0.0
0 -4.25 -4.25 0.0
.END_ELECTRICAL
.ELECTRICAL
CAP_RAD 47uF MM 9.0000
0 0.0 0.0 0.0
0 3.0 0.0 360.0
.END_ELECTRICAL
.ELECTRICAL
R2012 10K MM 0.6000
0 -1.0 -0.625 0.0
0 1.0 -0.625 0.0
0 1.0 0.625 0.0
0 -1.0 0.625 0.0
0 -1.0 -0.625 0.0
.END_ELECTRICAL
.ELECTRICAL
QFN32 IC_MISSING MM 1.5000
0 -3.0 -3.0 0.0
0 3.0 -3.0 0.0
0 3.0 3.0 0.0
0 -3.0 3.0 0.0
0 -3.0 -3.0 0.0
.END_ELECTRICAL
.ELECTRICAL
R3216 SHUNT_BOT MM 0.8000
0 -1.6 -0.8 0.0
0 1.6 -0.8 0.0
0 1.6 0.8 0.0
0 -1.6 0.8 0.0
0 -1.6 -0.8 0.0
.END_ELECTRICAL
.ELECTRICAL
"HOLE 3.2MM" MNT_HOLE MM 0.0000
0 -2.0 -2.0 0.0
0 2.0 -2.0 0.0
0 2.0 2.0 0.0
0 -2.0 2.0 0.0
0 -2.0 -2.0 0.0
.END_ELECTRICAL
"""
    bdf_path.write_text(bdf_text, encoding="utf-8")
    ldf_path.write_text(ldf_text, encoding="utf-8")
    return step_path, bdf_path, ldf_path
