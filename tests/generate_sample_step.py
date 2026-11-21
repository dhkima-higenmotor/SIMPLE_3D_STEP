"""Synthetic STEP assembly generator for automated testing of visibility & solidification."""

from __future__ import annotations

from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopoDS import TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool


def _translate(shape: TopoDS_Shape, dx: float, dy: float, dz: float) -> TopoDS_Shape:
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(dx, dy, dz))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _write_named_assembly(named_shapes: list[tuple[str, TopoDS_Shape]], out_path: Path) -> Path:
    """Write a multi-part STEP assembly with preserved XCAF part names."""
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    root = shape_tool.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString(out_path.stem))

    for name, shape in named_shapes:
        part_label = shape_tool.AddShape(shape, False)
        TDataStd_Name.Set_s(part_label, TCollection_ExtendedString(name))
        comp_label = shape_tool.AddComponent(root, part_label, shape.Location())
        TDataStd_Name.Set_s(comp_label, TCollection_ExtendedString(name))

    shape_tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    status = writer.Write(str(out_path))
    if status != IFSelect_RetDone:
        raise RuntimeError(f"Failed to write synthetic STEP: {out_path}")
    return out_path


def generate_closed_enclosure_step(out_path: Path) -> Path:
    """Scenario A: Multi-part closed cylindrical housing (3 external + 3 internal parts).

    Outer envelope: Cylinder R=50, H=100 (Z=0..100). Expected filled volume = pi * 50^2 * 100 ≈ 785,398.16
    """
    # Hollow cylinder wall (Z=10..90, R_out=50, R_in=40)
    outer_wall = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 10), gp_Dir(0, 0, 1)), 50.0, 80.0).Shape()
    inner_bore = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 10), gp_Dir(0, 0, 1)), 40.0, 80.0).Shape()
    housing = BRepAlgoAPI_Cut(outer_wall, inner_bore).Shape()

    # Bottom cap (Z=0..10, R=50) & Top cap (Z=90..100, R=50)
    bottom_cap = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 50.0, 10.0).Shape()
    top_cap = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 90), gp_Dir(0, 0, 1)), 50.0, 10.0).Shape()

    # 3 completely hidden internal parts inside Z=15..85, R<35
    rotor_shaft = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 20), gp_Dir(0, 0, 1)), 10.0, 60.0).Shape()
    inner_gear = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 35), gp_Dir(0, 0, 1)), 28.0, 20.0).Shape()
    inner_pcb = _translate(BRepPrimAPI_MakeBox(24.0, 24.0, 5.0).Shape(), -12.0, -12.0, 70.0)

    return _write_named_assembly(
        [
            ("EXT_HOUSING", housing),
            ("EXT_BOTTOM_CAP", bottom_cap),
            ("EXT_TOP_CAP", top_cap),
            ("INT_ROTOR_SHAFT", rotor_shaft),
            ("INT_GEAR", inner_gear),
            ("INT_PCB", inner_pcb),
        ],
        out_path,
    )


def generate_holed_box_step(out_path: Path) -> Path:
    """Scenario B: Hollow box 100x100x100 with a 6mm top hole + hidden internal block.

    Expected outer filled volume = 100 * 100 * 100 = 1,000,000.
    """
    outer_box = BRepPrimAPI_MakeBox(100.0, 100.0, 100.0).Shape()
    inner_void = _translate(BRepPrimAPI_MakeBox(80.0, 80.0, 80.0).Shape(), 10.0, 10.0, 10.0)
    hollow = BRepAlgoAPI_Cut(outer_box, inner_void).Shape()

    # 6mm diameter through-hole on top face connecting to internal cavity
    hole_cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 50, 85), gp_Dir(0, 0, 1)), 3.0, 20.0).Shape()
    holed_housing = BRepAlgoAPI_Cut(hollow, hole_cyl).Shape()

    # Small external boss on the side (must NOT be deleted!)
    ext_boss = _translate(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), 100.0, 45.0, 45.0)

    # Internal hidden core
    int_core = _translate(BRepPrimAPI_MakeBox(40.0, 40.0, 40.0).Shape(), 30.0, 30.0, 20.0)

    return _write_named_assembly(
        [
            ("EXT_HOLED_HOUSING", holed_housing),
            ("EXT_SIDE_BOSS", ext_boss),
            ("INT_CORE", int_core),
        ],
        out_path,
    )


def generate_open_cavity_box_step(out_path: Path) -> Path:
    """Scenario C: Hollow box 100x100x100 with a 30x30 open window + internal block."""
    outer_box = BRepPrimAPI_MakeBox(100.0, 100.0, 100.0).Shape()
    inner_void = _translate(BRepPrimAPI_MakeBox(80.0, 80.0, 80.0).Shape(), 10.0, 10.0, 10.0)
    hollow = BRepAlgoAPI_Cut(outer_box, inner_void).Shape()
    # 30x30 window on +Z top face
    window = _translate(BRepPrimAPI_MakeBox(30.0, 30.0, 20.0).Shape(), 35.0, 35.0, 85.0)
    open_housing = BRepAlgoAPI_Cut(hollow, window).Shape()

    # Internal block tucked off to the side inside the cavity
    int_block = _translate(BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape(), 12.0, 12.0, 12.0)

    return _write_named_assembly(
        [
            ("EXT_OPEN_HOUSING", open_housing),
            ("INT_SIDE_BLOCK", int_block),
        ],
        out_path,
    )
