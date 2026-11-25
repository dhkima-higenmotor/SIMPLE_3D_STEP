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
    re_names = {c.name for c in reloaded.components}
    # Flat 1-level STEP export with IDF names prioritized for matched & added parts
    assert "U1_QFP64" in re_names
    assert "J1_HDR8" in re_names
    assert "U2_QFN32" in re_names
    assert "R2_R3216" in re_names
    assert all(c.leaf_name == "" for c in reloaded.components)  # Flat: no nested sub-assemblies


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


def test_pcb_preserve_low_complexity_and_hex_standoff_simplification(tmp_path: Path) -> None:
    """Verify preservation of low-complexity parts (flanged terminal, holed standoff, pin header,
    SMD cap with square base) and hexagonal prism + screw hole simplification for complex hex posts."""
    import math
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
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

    from simple_3d_step.core.pcb_simplifier import _is_cylinder_face_concave, _shape_volume

    def _box(x0: float, y0: float, z0: float, w: float, d: float, h: float):
        return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), w, d, h).Shape()

    def _cyl(cx: float, cy: float, z0: float, r: float, h: float):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, z0), gp_Dir(0.0, 0.0, 1.0)), r, h).Shape()

    def _hex_prism(cx: float, cy: float, z0: float, r_in: float, h: float, phi0: float = 0.0):
        r_hex = r_in / math.cos(math.pi / 6.0)
        poly = BRepBuilderAPI_MakePolygon()
        for k in range(6):
            th = phi0 + k * (math.pi / 3.0)
            poly.Add(gp_Pnt(cx + r_hex * math.cos(th), cy + r_hex * math.sin(th), z0))
        poly.Close()
        face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
        return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, h)).Shape()

    def _count_concave_holes(shape) -> int:
        cnt = 0
        exp = TopExp_Explorer(shape, TopAbs_FACE)
        while exp.More():
            f = to_face(exp.Current())
            surf = BRepAdaptor_Surface(f, False)
            if surf.GetType() == GeomAbs_Cylinder and _is_cylinder_face_concave(f, surf.Cylinder()) is True:
                cnt += 1
            exp.Next()
        return cnt

    # Build synthetic Altium-style STEP containing all 5 targeted component archetypes
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    root = st.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString("PCB_Detail"))
    ident = TopLoc_Location()

    def _add_comp(name: str, items: list[tuple[str, object, tuple[float, float, float]]]) -> None:
        if len(items) == 1:
            _, sh, rgb = items[0]
            pl = st.AddShape(sh, False)
            TDataStd_Name.Set_s(pl, TCollection_ExtendedString(name))
            qc = Quantity_Color(rgb[0], rgb[1], rgb[2], Quantity_TOC_RGB)
            ct.SetColor(pl, qc, XCAFDoc_ColorGen)
            ct.SetColor(pl, qc, XCAFDoc_ColorSurf)
            cl = st.AddComponent(root, pl, ident)
            TDataStd_Name.Set_s(cl, TCollection_ExtendedString(name))
            return
        c_asm = st.NewShape()
        TDataStd_Name.Set_s(c_asm, TCollection_ExtendedString(name))
        for sname, sh, rgb in items:
            sl = st.AddShape(sh, False)
            TDataStd_Name.Set_s(sl, TCollection_ExtendedString(sname))
            qc = Quantity_Color(rgb[0], rgb[1], rgb[2], Quantity_TOC_RGB)
            ct.SetColor(sl, qc, XCAFDoc_ColorGen)
            ct.SetColor(sl, qc, XCAFDoc_ColorSurf)
            scl = st.AddComponent(c_asm, sl, ident)
            TDataStd_Name.Set_s(scl, TCollection_ExtendedString(sname))
        cl = st.AddComponent(root, c_asm, ident)
        TDataStd_Name.Set_s(cl, TCollection_ExtendedString(name))

    # 0. PCB Board (80 x 60 x 1.6)
    _add_comp("PCB_Board", [("Board", _box(0, 0, -1.6, 80, 60, 1.6), (0.0, 0.35, 0.08))])

    # 1. Flanged terminal with blind center hole (like (W)CxS_1KW_Terminal_L10)
    term_body = BRepAlgoAPI_Fuse(_cyl(10, 10, 0.0, 3.5, 1.5), _cyl(10, 10, 1.4, 2.5, 8.2)).Shape()
    term_body = BRepAlgoAPI_Cut(term_body, _cyl(10, 10, 3.6, 1.25, 6.2)).Shape()
    _add_comp("W_TERM", [("Terminal_L10", term_body, (0.78, 0.78, 0.80))])

    # 2. Round standoff with bottom pilot & through-hole (like BH4CxS_1KW_TH-1.6-5.0-M3)
    standoff = BRepAlgoAPI_Fuse(_cyl(25, 10, -1.5, 1.75, 1.6), _cyl(25, 10, 0.0, 2.5, 5.0)).Shape()
    standoff = BRepAlgoAPI_Cut(standoff, _cyl(25, 10, -1.7, 1.25, 7.0)).Shape()
    _add_comp("BH4_ROUND", [("TH_M3", standoff, (0.75, 0.75, 0.78))])

    # 3. 1-body 2-pin header (base + 2 tall pins, like J9CxS_1KW_HWHP-2P)
    hdr = _box(40.0, 10.0, 0.0, 3.2, 5.08, 2.2)
    hdr = BRepAlgoAPI_Fuse(hdr, _box(41.28, 10.95, 2.0, 0.64, 0.64, 6.2)).Shape()
    hdr = BRepAlgoAPI_Fuse(hdr, _box(41.28, 13.49, 2.0, 0.64, 0.64, 6.2)).Shape()
    _add_comp("J9_HDR", [("HWHP_2P", hdr, (0.20, 0.20, 0.22))])

    # 4. 4-body SMD electrolytic cap with 10.5x10.5 square base under 10.2mm can (like C2CxS_1KW)
    c2_base = _box(55.0 - 5.25, 25.0 - 5.25, 0.3, 10.5, 10.5, 1.6)
    c2_pin1 = _box(55.0 - 5.5, 25.0 - 0.65, 0.0, 3.5, 1.3, 0.35)
    c2_pin2 = _box(55.0 + 2.0, 25.0 - 0.65, 0.0, 3.5, 1.3, 0.35)
    c2_can = _cyl(55.0, 25.0, 1.1, 5.1, 9.4)
    _add_comp(
        "C2_SMD",
        [
            ("Base", c2_base, (0.15, 0.15, 0.16)),
            ("Pin1", c2_pin1, (0.80, 0.80, 0.82)),
            ("Pin2", c2_pin2, (0.80, 0.80, 0.82)),
            ("Can", c2_can, (0.78, 0.80, 0.84)),
        ],
    )

    # 5. High-complexity hexagonal standoff (> 60 faces via serration notches) + pilot + through-hole (like BH3)
    hex_post = _hex_prism(20.0, 40.0, 0.0, 1.59, 3.0, phi0=math.radians(30.0))
    hex_post = BRepAlgoAPI_Fuse(hex_post, _cyl(20.0, 40.0, -0.5, 1.20, 0.55)).Shape()
    hex_post = BRepAlgoAPI_Cut(hex_post, _cyl(20.0, 40.0, -0.7, 0.61, 4.0)).Shape()
    # Carve tiny perimeter knurling notches across all 6 hex flats so face count exceeds 60
    r_hex_outer = 1.59 / math.cos(math.pi / 6.0)
    for side in range(6):
        th0 = math.radians(30.0) + side * (math.pi / 3.0)
        th1 = th0 + (math.pi / 3.0)
        p0 = (20.0 + r_hex_outer * math.cos(th0), 40.0 + r_hex_outer * math.sin(th0))
        p1 = (20.0 + r_hex_outer * math.cos(th1), 40.0 + r_hex_outer * math.sin(th1))
        for t in (0.25, 0.50, 0.75):
            nx = p0[0] + (p1[0] - p0[0]) * t
            ny = p0[1] + (p1[1] - p0[1]) * t
            hex_post = BRepAlgoAPI_Cut(hex_post, _cyl(nx, ny, 0.35, 0.08, 0.25)).Shape()
    assert _count_type(hex_post, TopAbs_FACE) > 60
    _add_comp("BH3_HEX", [("SMTSO_M16", hex_post, (0.76, 0.76, 0.78))])

    st.UpdateAssemblies()
    step_path = tmp_path / "pcb_detail_cases.step"
    writer = STEPCAFControl_Writer()
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    writer.Write(str(step_path))

    asm = load_pcb_assembly(step_path)
    res = simplify_pcb_assembly(asm, PcbSimplifyConfig())
    by_ref = {p.ref_des or p.name: p for p in res.parts}

    # 1. W_TERM: preserved with flange & blind center hole
    w_term = by_ref["W_TERM"]
    assert w_term.category == "preserved"
    assert _count_type(w_term.shape, TopAbs_SOLID) == 1
    assert BRepCheck_Analyzer(w_term.shape).IsValid()
    assert _count_concave_holes(w_term.shape) >= 1
    assert _shape_volume(w_term.shape) == pytest.approx(_shape_volume(term_body), rel=0.02)

    # 2. BH4_ROUND: preserved with through-hole (not replaced by solid cylinder!)
    bh4 = by_ref["BH4_ROUND"]
    assert bh4.category == "preserved"
    assert _count_type(bh4.shape, TopAbs_SOLID) == 1
    assert _count_concave_holes(bh4.shape) >= 1

    # 3. J9_HDR: preserved with header base + 2 pins (not collapsed to bloated box!)
    j9 = by_ref["J9_HDR"]
    assert j9.category == "preserved"
    assert _count_type(j9.shape, TopAbs_SOLID) == 1
    assert _shape_volume(j9.shape) < 3.2 * 5.08 * 8.2 * 0.45

    # 4. C2_SMD: preserved & fused into 1 watertight solid keeping square base + pins + can
    c2 = by_ref["C2_SMD"]
    assert c2.category == "preserved"
    assert _count_type(c2.shape, TopAbs_SOLID) == 1
    assert BRepCheck_Analyzer(c2.shape).IsValid()
    assert _has_cylindrical_face(c2.shape)
    assert c2.simplified_faces >= 10  # Can + square base + pins (not a 3-face bare cylinder)

    # 4b. Even with preserve_simple_max_faces=0, cylinder simplifier must STILL keep C2's square base!
    res_no_preserve = simplify_pcb_assembly(asm, PcbSimplifyConfig(preserve_simple_max_faces=0))
    c2_cyl = next(p for p in res_no_preserve.parts if (p.ref_des or p.name) == "C2_SMD")
    assert c2_cyl.category == "cylinder"
    assert _count_type(c2_cyl.shape, TopAbs_SOLID) == 1
    assert c2_cyl.simplified_faces > 3  # Fused cylinder + square base box!

    # 5. BH3_HEX: simplified from >60 faces to clean ~11-face hex prism + pilot + through-hole!
    bh3 = by_ref["BH3_HEX"]
    assert bh3.category == "hex_post"
    assert res.hex_post_count == 1
    assert _count_type(bh3.shape, TopAbs_SOLID) == 1
    assert BRepCheck_Analyzer(bh3.shape).IsValid()
    assert bh3.simplified_faces <= 14
    assert _count_concave_holes(bh3.shape) >= 1
    # Verify 6 vertical planar outer faces exist (hexagonal prism, not 4-sided box)
    vert_planes = 0
    exp = TopExp_Explorer(bh3.shape, TopAbs_FACE)
    while exp.More():
        f = to_face(exp.Current())
        surf = BRepAdaptor_Surface(f, False)
        if surf.GetType() == GeomAbs_Plane and abs(float(surf.Plane().Axis().Direction().Z())) < 0.05:
            vert_planes += 1
        exp.Next()
    assert vert_planes == 6

    # Export & reload roundtrip
    out_step = tmp_path / "pcb_detail_cases_pcb_simplified.step"
    saved = save_colored_assembly_to_step(res.parts, out_step, root_name=res.root_name, preserve_colors=True)
    reloaded = load_pcb_assembly(saved)
    assert len(reloaded.components) == len(res.parts)
    assert all(len(c.bodies) == 1 for c in reloaded.components)


def test_pcb_flat_export_multi_board_heatsink_and_idf_naming(tmp_path: Path) -> None:
    """Verify:
    1. Flat 1-level STEP export (zero nested sub-assemblies in XCAF tree)
    2. Multiple PCBs (mainboard + smaller sub-PCB) each detected as board with hole deletion only
    3. Extruded finned heatsink detected geometrically & preserved intact (no boxing/hole deletion)
    4. Flat part names reflect sub-assembly + child part name, with IDF names taking priority
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import (
        XCAFDoc_ColorGen,
        XCAFDoc_ColorSurf,
        XCAFDoc_DocumentTool,
        XCAFDoc_ShapeTool,
    )

    from simple_3d_step.core.occt_compat import LabelSequence
    from simple_3d_step.core.pcb_simplifier import _shape_volume

    def _box(x0: float, y0: float, z0: float, w: float, d: float, h: float):
        return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), w, d, h).Shape()

    def _cyl(cx: float, cy: float, z0: float, r: float, h: float):
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, z0), gp_Dir(0.0, 0.0, 1.0)), r, h).Shape()

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    root = st.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString("MultiPcbSystem"))
    ident = TopLoc_Location()

    def _add_sub(sub_name: str, part_name: str, shape, rgb: tuple[float, float, float]) -> None:
        pl = st.AddShape(shape, False)
        TDataStd_Name.Set_s(pl, TCollection_ExtendedString(part_name))
        qc = Quantity_Color(*rgb, Quantity_TOC_RGB)
        ct.SetColor(pl, qc, XCAFDoc_ColorGen)
        ct.SetColor(pl, qc, XCAFDoc_ColorSurf)
        sub_asm = st.NewShape()
        TDataStd_Name.Set_s(sub_asm, TCollection_ExtendedString(sub_name))
        occ_leaf = st.AddComponent(sub_asm, pl, ident)
        TDataStd_Name.Set_s(occ_leaf, TCollection_ExtendedString(part_name))
        occ_top = st.AddComponent(root, sub_asm, ident)
        TDataStd_Name.Set_s(occ_top, TCollection_ExtendedString(sub_name))

    # 1. Main PCB (80 x 60 x 1.6) with 3.2mm mounting hole + 0.4mm via hole
    main_pcb = _box(0, 0, -1.6, 80, 60, 1.6)
    main_pcb = BRepAlgoAPI_Cut(main_pcb, _cyl(5, 5, -2.0, 1.6, 2.4)).Shape()
    main_pcb = BRepAlgoAPI_Cut(main_pcb, _cyl(40, 30, -2.0, 0.2, 2.4)).Shape()
    _add_sub("MainPCB", "FR4_Main", main_pcb, (0.0, 0.35, 0.10))

    # 2. Secondary smaller PCB (26 x 20 x 1.2) sitting above mainboard (z in [12.0, 13.2])
    #    with 2.5mm mounting hole + 0.4mm via hole (and generic non-board name to test geometry detection!)
    sub_pcb = _box(50, 35, 12.0, 26, 20, 1.2)
    sub_pcb = BRepAlgoAPI_Cut(sub_pcb, _cyl(54, 39, 11.5, 1.25, 2.2)).Shape()
    sub_pcb = BRepAlgoAPI_Cut(sub_pcb, _cyl(63, 45, 11.5, 0.2, 2.2)).Shape()
    _add_sub("SubModule", "Plate_2", sub_pcb, (0.0, 0.25, 0.45))

    # 3. Extruded finned heatsink (24 x 20 x 10, baseplate + 6 parallel fins + small 0.6mm pin hole)
    #    Named generically to verify geometric fin-array detection (NOT name-only!)
    hs = _box(10, 10, 0.0, 24, 20, 2.0)
    for k in range(6):
        fx = 10.0 + k * 4.4
        hs = BRepAlgoAPI_Fuse(hs, _box(fx, 10.0, 1.8, 1.6, 20.0, 8.2)).Shape()
    hs_orig_vol = _shape_volume(hs)
    hs_orig_faces = _count_type(hs, TopAbs_FACE)
    _add_sub("MetalBlock", "Extrusion_Al", hs, (0.65, 0.68, 0.72))

    # 4. Mounted IC U1 (matched by IDF -> IDF name must override STEP name!)
    _add_sub("U1", "Vendor_IC_Body", _box(45, 15, 0.0, 6.0, 6.0, 1.4), (0.12, 0.12, 0.14))

    # 5. Mounted unmatched part J10 (not in IDF -> keeps STEP SubAsm_Part name!)
    _add_sub("J10", "PinHeader_2P", _box(65, 15, 0.0, 3.0, 5.0, 4.0), (0.20, 0.20, 0.22))

    st.UpdateAssemblies()
    step_path = tmp_path / "multi_pcb_heatsink.step"
    writer = STEPCAFControl_Writer()
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    writer.Write(str(step_path))

    # Matching IDF file with custom name for U1 + missing part U2
    bdf_path = tmp_path / "multi_pcb_heatsink.bdf"
    ldf_path = tmp_path / "multi_pcb_heatsink.ldf"
    bdf_path.write_text(
        """.HEADER
BOARD_FILE 3.0 "Altium DXP" 2026-11-25 1
MULTI_PCB MM
.END_HEADER
.BOARD_OUTLINE UNOWNED
1.6
0 0 0 0
0 80 0 0
0 80 60 0
0 0 60 0
0 0 0 0
.END_BOARD_OUTLINE
.PLACEMENT
QFP48_IDF MCU_CHIP U1
48.0 18.0 0.0 0.0 TOP PLACED
QFN16_IDF SENSOR U2
20.0 45.0 0.0 0.0 TOP PLACED
.END_PLACEMENT
""",
        encoding="utf-8",
    )
    ldf_path.write_text(
        """.HEADER
LIBRARY_FILE 3.0 "Altium DXP" 2026-11-25 1
.END_HEADER
.ELECTRICAL
QFP48_IDF MCU_CHIP MM 1.4
0 -3 -3 0
0 3 -3 0
0 3 3 0
0 -3 3 0
0 -3 -3 0
.END_ELECTRICAL
.ELECTRICAL
QFN16_IDF SENSOR MM 1.2
0 -2 -2 0
0 2 -2 0
0 2 2 0
0 -2 2 0
0 -2 -2 0
.END_ELECTRICAL
""",
        encoding="utf-8",
    )

    asm = load_pcb_assembly(step_path)
    idf = load_idf_pair(bdf_path, ldf_path)
    assert idf is not None

    cfg = PcbSimplifyConfig(min_board_hole_mm=0.8)
    res = simplify_pcb_assembly(asm, cfg, idf_data=idf)

    # Req 2: BOTH PCBs detected as board, each keeps mounting hole and plugs 0.4mm via!
    assert res.board_count == 2
    boards = [p for p in res.parts if p.category == "board"]
    assert len(boards) == 2
    for bd in boards:
        assert _count_type(bd.shape, TopAbs_SOLID) == 1
        cyl_cnt = 0
        exp = TopExp_Explorer(bd.shape, TopAbs_FACE)
        while exp.More():
            if BRepAdaptor_Surface(to_face(exp.Current()), False).GetType() == GeomAbs_Cylinder:
                cyl_cnt += 1
            exp.Next()
        assert cyl_cnt == 1  # Mounting hole kept, 0.4mm via plugged!

    # Req 3: Finned heatsink detected & exact original fin geometry preserved intact!
    assert res.heatsink_count == 1
    hs_part = next(p for p in res.parts if p.category == "heatsink")
    assert _count_type(hs_part.shape, TopAbs_SOLID) == 1
    assert BRepCheck_Analyzer(hs_part.shape).IsValid()
    assert _shape_volume(hs_part.shape) == pytest.approx(hs_orig_vol, rel=0.01)
    assert hs_part.simplified_faces >= 20  # All fins preserved (not turned into a 6-face box!)

    # Req 1 & 4: Export flat STEP (ZERO sub-assemblies!) and verify combined + IDF-prioritized names
    out_step = tmp_path / "multi_pcb_heatsink_pcb_simplified.step"
    saved = save_colored_assembly_to_step(
        res.parts, out_step, root_name=res.root_name, preserve_colors=True
    )

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    doc_re = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc_re)
    assert reader.ReadFile(str(saved)) == 1
    assert reader.Transfer(doc_re)
    st_re = XCAFDoc_DocumentTool.ShapeTool_s(doc_re.Main())
    roots = LabelSequence()
    st_re.GetFreeShapes(roots)
    assert roots.Length() == 1
    comps = LabelSequence()
    XCAFDoc_ShapeTool.GetComponents_s(roots.Value(1), comps)
    assert comps.Length() == len(res.parts)

    # Verify ZERO sub-assemblies exist directly under root
    for i in range(1, comps.Length() + 1):
        cl = comps.Value(i)
        rl = TDF_Label()
        assert XCAFDoc_ShapeTool.GetReferredShape_s(cl, rl)
        assert not XCAFDoc_ShapeTool.IsAssembly_s(rl), "Exported STEP must be strictly flat (no sub-assemblies)"

    reloaded = load_pcb_assembly(saved)
    re_names = {c.name for c in reloaded.components}
    # IDF names take priority for U1 (matched) and U2 (added)
    assert "U1_QFP48_IDF" in re_names
    assert "U2_QFN16_IDF" in re_names
    # Non-IDF STEP parts reflect SubAssembly_PartName
    assert "MainPCB_FR4_Main" in re_names
    assert "SubModule_Plate_2" in re_names
    assert "MetalBlock_Extrusion_Al" in re_names
    assert "J10_PinHeader_2P" in re_names


def test_pcb_deeply_nested_free_models_stacked_boards_preserved(tmp_path: Path) -> None:
    """Verify Altium-style stacked PCBs nested inside single-child 'Free-Models' wrappers
    (Board1 -> Free-Models -> Board2 -> Free-Models -> Board3 -> Free-Models -> Heatsink)
    are completely unwrapped without losing any PCB or mounted component."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
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

    def _box(x0: float, y0: float, z0: float, w: float, d: float, h: float):
        return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), w, d, h).Shape()

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    ident = TopLoc_Location()

    def _leaf_part(name: str, shape, rgb: tuple[float, float, float]):
        pl = st.AddShape(shape, False)
        TDataStd_Name.Set_s(pl, TCollection_ExtendedString(name))
        qc = Quantity_Color(*rgb, Quantity_TOC_RGB)
        ct.SetColor(pl, qc, XCAFDoc_ColorGen)
        ct.SetColor(pl, qc, XCAFDoc_ColorSurf)
        return pl

    def _wrap(asm_name: str, children: list[tuple[str, object]]):
        a = st.NewShape()
        TDataStd_Name.Set_s(a, TCollection_ExtendedString(asm_name))
        for cname, child_lbl in children:
            occ = st.AddComponent(a, child_lbl, ident)
            TDataStd_Name.Set_s(occ, TCollection_ExtendedString(cname))
        return a

    # Level 3 (bottom): Power_PCB (Board + Q1 + Free-Models -> Finned Heatsink)
    hs_shape = _box(0, 0, -25.0, 50, 50, 2.0)
    for k in range(6):
        hs_shape = BRepAlgoAPI_Fuse(hs_shape, _box(k * 9.0, 0, -30.0, 2.5, 50, 5.2)).Shape()
    hs_leaf = _leaf_part("BRK_Heatsink", hs_shape, (0.6, 0.62, 0.65))
    fm_pwr = _wrap("Free-Models", [("BRK_Heatsink", hs_leaf)])
    pwr_asm = _wrap(
        "Power_PCB",
        [
            ("Board", _wrap("Board", [("Board", _leaf_part("Board", _box(0, 0, -22.0, 50, 50, 1.6), (0.0, 0.3, 0.1)))])),
            ("Q1", _wrap("Q1", [("MOSFET", _leaf_part("MOSFET", _box(10, 10, -20.4, 5, 5, 1.0), (0.15, 0.15, 0.15)))])),
            ("Free-Models", fm_pwr),
        ],
    )

    # Level 2 (middle): GateDriver_PCB (Board + U1 + single-child Free-Models -> Power_PCB)
    fm_gate = _wrap("Free-Models", [("Power_PCB", pwr_asm)])
    gate_asm = _wrap(
        "GateDriver_PCB",
        [
            ("Board", _wrap("Board", [("Board", _leaf_part("Board", _box(0, 0, -12.0, 50, 50, 1.6), (0.0, 0.3, 0.1)))])),
            ("U1", _wrap("U1", [("DriverIC", _leaf_part("DriverIC", _box(20, 20, -10.4, 6, 6, 1.2), (0.15, 0.15, 0.15)))])),
            ("Free-Models", fm_gate),
        ],
    )

    # Level 1 (top): Interface_PCB (Board + R1 + single-child Free-Models -> GateDriver_PCB)
    fm_top = _wrap("Free-Models", [("GateDriver_PCB", gate_asm)])
    _wrap(
        "Interface_PCB",
        [
            ("Board", _wrap("Board", [("Board", _leaf_part("Board", _box(0, 0, -1.6, 50, 50, 1.6), (0.0, 0.3, 0.1)))])),
            ("R1", _wrap("R1", [("R1206", _leaf_part("R1206", _box(5, 5, 0.0, 3.2, 1.6, 0.8), (0.2, 0.2, 0.2)))])),
            ("Free-Models", fm_top),
        ],
    )

    st.UpdateAssemblies()
    step_path = tmp_path / "stacked_free_models.step"
    writer = STEPCAFControl_Writer()
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    writer.Write(str(step_path))

    asm = load_pcb_assembly(step_path)
    # All 3 boards + 3 mounted components + 1 heatsink = 7 components (nothing lost!)
    assert len(asm.components) == 7

    res = simplify_pcb_assembly(asm, PcbSimplifyConfig())
    assert res.board_count == 3
    assert res.heatsink_count == 1
    assert res.component_count == 3
    names = {p.name for p in res.parts}
    assert "Board" in names
    assert "GateDriver_PCB_Board" in names
    assert "Power_PCB_Board" in names
    assert "GateDriver_PCB_U1_DriverIC" in names
    assert "Power_PCB_Q1_MOSFET" in names
    assert any("BRK_Heatsink" in n and "Free-Models" not in n for n in names)
