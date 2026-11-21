"""Compatibility helpers across OCCT 7.x and OCCT 8.x (OCP) bindings."""

from __future__ import annotations

from OCP.TopoDS import (
    TopoDS,
    TopoDS_Edge,
    TopoDS_Face,
    TopoDS_Shape,
    TopoDS_Shell,
    TopoDS_Solid,
    TopoDS_Wire,
)

try:
    from OCP.collections import (
        HSequence_TopoDS_Shape as ShapeHSequence,
        List_TopoDS_Shape as ShapeList,
        Sequence_TDF_Label as LabelSequence,
    )
except ImportError:
    from OCP.TDF import TDF_LabelSequence as LabelSequence  # type: ignore[no-redef]
    from OCP.TopTools import (  # type: ignore[no-redef]
        TopTools_HSequenceOfShape as ShapeHSequence,
        TopTools_ListOfShape as ShapeList,
    )


def to_solid(shape: TopoDS_Shape) -> TopoDS_Solid:
    fn = getattr(TopoDS, "Solid", None) or getattr(TopoDS, "Solid_s")
    return fn(shape)


def to_shell(shape: TopoDS_Shape) -> TopoDS_Shell:
    fn = getattr(TopoDS, "Shell", None) or getattr(TopoDS, "Shell_s")
    return fn(shape)


def to_face(shape: TopoDS_Shape) -> TopoDS_Face:
    fn = getattr(TopoDS, "Face", None) or getattr(TopoDS, "Face_s")
    return fn(shape)


def to_edge(shape: TopoDS_Shape) -> TopoDS_Edge:
    fn = getattr(TopoDS, "Edge", None) or getattr(TopoDS, "Edge_s")
    return fn(shape)


def to_wire(shape: TopoDS_Shape) -> TopoDS_Wire:
    fn = getattr(TopoDS, "Wire", None) or getattr(TopoDS, "Wire_s")
    return fn(shape)


__all__ = [
    "LabelSequence",
    "ShapeHSequence",
    "ShapeList",
    "to_edge",
    "to_face",
    "to_shell",
    "to_solid",
    "to_wire",
]
