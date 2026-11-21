"""B-Rep geometric and topological validation for simplified STEP shapes."""

from __future__ import annotations

from OCP.Bnd import Bnd_Box
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import (
    GeomAbs_BSplineSurface,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.GProp import GProp_GProps
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shape

from simple_3d_step.core.occt_compat import to_face
from simple_3d_step.models import ValidationReport


_SURFACE_NAMES = {
    GeomAbs_Plane: "Plane",
    GeomAbs_Cylinder: "Cylinder",
    GeomAbs_Cone: "Cone",
    GeomAbs_Sphere: "Sphere",
    GeomAbs_Torus: "Torus",
    GeomAbs_BSplineSurface: "BSpline",
}


def validate_shape(shape: TopoDS_Shape) -> ValidationReport:
    """Inspect a TopoDS_Shape and report single-solid / watertight / surface-type metrics."""
    warnings: list[str] = []

    if shape is None or shape.IsNull():
        return ValidationReport(
            is_valid=False,
            solid_count=0,
            shell_count=0,
            face_count=0,
            free_edge_count=0,
            is_watertight=False,
            volume=0.0,
            surface_area=0.0,
            bbox_size=(0.0, 0.0, 0.0),
            warnings=["Resulting shape is empty."],
        )

    def _count(shape_type) -> int:
        c = 0
        exp = TopExp_Explorer(shape, shape_type)
        while exp.More():
            c += 1
            exp.Next()
        return c

    solid_count = _count(TopAbs_SOLID)
    shell_count = _count(TopAbs_SHELL)

    face_count = 0
    face_types: dict[str, int] = {}
    exp_f = TopExp_Explorer(shape, TopAbs_FACE)
    while exp_f.More():
        face_count += 1
        try:
            stype = BRepAdaptor_Surface(to_face(exp_f.Current()), False).GetType()
            label = _SURFACE_NAMES.get(stype, "Other")
            face_types[label] = face_types.get(label, 0) + 1
        except Exception:
            pass
        exp_f.Next()

    # Free edges check (0 free edges => watertight closed boundary)
    free_edge_count = 0
    try:
        fb = ShapeAnalysis_FreeBounds(shape)
        for compound in (fb.GetClosedWires(), fb.GetOpenWires()):
            if not compound.IsNull():
                exp_e = TopExp_Explorer(compound, TopAbs_EDGE)
                while exp_e.More():
                    free_edge_count += 1
                    exp_e.Next()
    except Exception as exc:
        warnings.append(f"Free-edge check warning: {exc}")

    vol_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vol_props)
    volume = float(vol_props.Mass())

    surf_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surf_props)
    surface_area = abs(float(surf_props.Mass()))

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        bbox_size = (0.0, 0.0, 0.0)
    else:
        pmin, pmax = box.CornerMin(), box.CornerMax()
        bbox_size = (
            float(pmax.X() - pmin.X()),
            float(pmax.Y() - pmin.Y()),
            float(pmax.Z() - pmin.Z()),
        )

    analyzer = BRepCheck_Analyzer(shape, True)
    brep_valid = bool(analyzer.IsValid())

    if solid_count == 0:
        warnings.append("No solid created (shape is a shell/surface).")
    elif solid_count > 1:
        warnings.append(f"Found {solid_count} separate solids instead of a single body.")

    if solid_count == 1 and shell_count > 1:
        warnings.append(
            f"{shell_count - 1} unremoved internal void shell(s) remain inside the solid."
        )

    if free_edge_count > 0:
        warnings.append(f"Detected {free_edge_count} open free edge(s) (non-watertight).")

    if volume <= 0.0:
        warnings.append(f"Volume is non-positive ({volume:.3f}); orientation may be inverted.")

    if not brep_valid:
        warnings.append("BRepCheck_Analyzer reported topological validity warnings.")

    is_watertight = solid_count >= 1 and free_edge_count == 0 and volume > 0.0

    return ValidationReport(
        is_valid=brep_valid and solid_count == 1 and shell_count == 1 and is_watertight,
        solid_count=solid_count,
        shell_count=shell_count,
        face_count=face_count,
        free_edge_count=free_edge_count,
        is_watertight=is_watertight,
        volume=abs(volume),
        surface_area=surface_area,
        bbox_size=bbox_size,
        face_types=face_types,
        warnings=warnings,
    )
