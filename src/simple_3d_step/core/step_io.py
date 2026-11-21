"""STEP file I/O using OpenCASCADE XCAF (preserving assembly hierarchy and part names)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from simple_3d_step.core.occt_compat import (
    LabelSequence as TDF_LabelSequence,
    to_shell,
    to_solid,
)

from simple_3d_step.models import (
    AssemblyModel,
    CancelCheck,
    PartNode,
    ProgressCallback,
)


def _get_label_name(label: TDF_Label) -> str:
    """Extract UTF-8 name attribute from an OCAF label, if present."""
    name_attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr):
        raw = name_attr.Get().ToExtString()
        if raw:
            return str(raw).strip()
    return ""


def _compute_shape_bbox(shape: TopoDS_Shape) -> tuple[np.ndarray, np.ndarray]:
    """Compute axis-aligned bounding box (min, max) of a TopoDS_Shape."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return np.zeros(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
    pmin = box.CornerMin()
    pmax = box.CornerMax()
    return (
        np.array([pmin.X(), pmin.Y(), pmin.Z()], dtype=np.float64),
        np.array([pmax.X(), pmax.Y(), pmax.Z()], dtype=np.float64),
    )


def _compute_shape_props(shape: TopoDS_Shape) -> tuple[float, float, int]:
    """Return (volume, surface_area, face_count) for a shape."""
    vol_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, vol_props)
    volume = abs(float(vol_props.Mass()))

    surf_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surf_props)
    area = abs(float(surf_props.Mass()))

    face_count = 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face_count += 1
        exp.Next()

    return volume, area, face_count


def _extract_solids_and_free_shells(shape: TopoDS_Shape) -> list[TopoDS_Shape]:
    """Extract individual solids (and any free shells not inside solids) from a shape."""
    items: list[TopoDS_Shape] = []
    exp_solid = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp_solid.More():
        items.append(to_solid(exp_solid.Current()))
        exp_solid.Next()

    # Collect free shells not belonging to any solid
    exp_shell = TopExp_Explorer(shape, TopAbs_SHELL, TopAbs_SOLID)
    while exp_shell.More():
        items.append(to_shell(exp_shell.Current()))
        exp_shell.Next()

    return items


def _ensure_ascii_read_path(path: Path) -> tuple[Path, Optional[Path]]:
    """If path contains non-ASCII chars on Windows, copy to a temp ASCII path for OCCT."""
    path_str = str(path.resolve())
    try:
        path_str.encode("ascii")
        return path.resolve(), None
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.mkdtemp(prefix="s3d_read_"))
        tmp_file = tmp_dir / f"input{path.suffix or '.step'}"
        shutil.copy2(path, tmp_file)
        return tmp_file, tmp_dir


def load_step_assembly(
    filepath: str | Path,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> AssemblyModel:
    """Load a STEP file via XCAF and normalize all leaf solids/shells into PartNodes."""
    source_path = Path(filepath).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"STEP 파일을 찾을 수 없습니다: {source_path}")

    if progress_cb:
        progress_cb(0.05, f"STEP 파일 읽는 중: {source_path.name}")

    read_path, tmp_dir = _ensure_ascii_read_path(source_path)
    try:
        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        app.InitDocument(doc)

        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(False)
        reader.SetLayerMode(False)
        reader.SetPropsMode(False)

        status = reader.ReadFile(str(read_path))
        if status != IFSelect_RetDone:
            raise RuntimeError(f"STEP 파일 파싱 실패 (status={status}): {source_path.name}")

        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")

        if progress_cb:
            progress_cb(0.35, "XCAF 어셈블리 데이터 변환 중...")

        if not reader.Transfer(doc):
            raise RuntimeError(f"STEP XCAF 문서 변환 실패: {source_path.name}")
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Detect length unit (default MM)
    unit = "MM"
    try:
        u = Interface_Static.CVal_s("xstep.cascade.unit")
        if u:
            unit = str(u).strip().upper()
    except Exception:
        pass

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    root_labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(root_labels)

    if progress_cb:
        progress_cb(0.55, "어셈블리 계층 및 부품 목록 추출 중...")

    parts: list[PartNode] = []
    compound_builder = BRep_Builder()
    full_compound = TopoDS_Compound()
    compound_builder.MakeCompound(full_compound)

    def _add_leaf_shapes(base_name: str, path_str: str, shape: TopoDS_Shape) -> None:
        if shape.IsNull():
            return
        sub_shapes = _extract_solids_and_free_shells(shape)
        if not sub_shapes:
            return
        total_sub = len(sub_shapes)
        for idx, sub in enumerate(sub_shapes, start=1):
            if cancel_check and cancel_check():
                raise InterruptedError("사용자가 작업을 취소했습니다.")
            part_name = base_name if total_sub == 1 else f"{base_name} [Body {idx}]"
            full_path = path_str if total_sub == 1 else f"{path_str} [Body {idx}]"
            vol, area, face_cnt = _compute_shape_props(sub)
            bmin, bmax = _compute_shape_bbox(sub)
            if face_cnt == 0:
                continue
            part_id = len(parts)
            is_solid = sub.ShapeType() == TopAbs_SOLID
            parts.append(
                PartNode(
                    part_id=part_id,
                    name=part_name,
                    assembly_path=full_path,
                    shape=sub,
                    solid_count=1 if is_solid else 0,
                    face_count=face_cnt,
                    volume=vol,
                    surface_area=area,
                    bbox_min=bmin,
                    bbox_max=bmax,
                )
            )
            compound_builder.Add(full_compound, sub)

    def _traverse(label: TDF_Label, parent_loc: TopLoc_Location, path_parts: list[str]) -> None:
        if cancel_check and cancel_check():
            raise InterruptedError("사용자가 작업을 취소했습니다.")

        label_name = _get_label_name(label)
        if XCAFDoc_ShapeTool.IsReference_s(label):
            ref_label = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(label, ref_label):
                inst_loc = XCAFDoc_ShapeTool.GetLocation_s(label)
                combined_loc = parent_loc.Multiplied(inst_loc)
                ref_name = _get_label_name(ref_label)
                node_name = label_name or ref_name or f"Component_{label.Tag()}"
                _traverse(ref_label, combined_loc, path_parts + [node_name])
                return

        node_name = label_name or f"Part_{label.Tag()}"
        current_path = path_parts if (path_parts and path_parts[-1] == node_name) else path_parts + [node_name]

        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                comp_label = comps.Value(i)
                _traverse(comp_label, parent_loc, current_path)
            return

        # Leaf simple shape or compound part
        shape = XCAFDoc_ShapeTool.GetShape_s(label)
        if not shape.IsNull():
            # Apply accumulated assembly transform
            placed = shape.Located(parent_loc.Multiplied(shape.Location()))
            _add_leaf_shapes(node_name, " / ".join(current_path), placed)

    identity_loc = TopLoc_Location()
    for i in range(1, root_labels.Length() + 1):
        root_label = root_labels.Value(i)
        _traverse(root_label, identity_loc, [])

    if progress_cb:
        progress_cb(0.95, f"총 {len(parts)}개 Solid/Shell 부품 로드 완료")

    if parts:
        global_min = np.min(np.vstack([p.bbox_min for p in parts]), axis=0)
        global_max = np.max(np.vstack([p.bbox_max for p in parts]), axis=0)
    else:
        global_min = np.zeros(3, dtype=np.float64)
        global_max = np.zeros(3, dtype=np.float64)

    if progress_cb:
        progress_cb(1.0, f"로드 완료 ({len(parts)}개 부품)")

    return AssemblyModel(
        source_path=source_path,
        parts=parts,
        unit=unit,
        bbox_min=global_min,
        bbox_max=global_max,
        raw_shape=full_compound,
    )


def make_compound_from_parts(parts: list[PartNode]) -> TopoDS_Compound:
    """Combine a list of PartNode shapes into a single TopoDS_Compound."""
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for part in parts:
        if part.shape is not None and not part.shape.IsNull():
            builder.Add(compound, part.shape)
    return compound


def save_shape_to_step(
    shape: TopoDS_Shape,
    output_path: str | Path,
    schema: str = "AP214",
    unit: str = "MM",
) -> Path:
    """Write a TopoDS_Shape to a STEP file, handling non-ASCII Windows paths safely."""
    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    Interface_Static.SetCVal_s("write.step.schema", schema)
    Interface_Static.SetCVal_s("write.step.unit", unit)

    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)

    dest_str = str(dest)
    try:
        dest_str.encode("ascii")
        status = writer.Write(dest_str)
        if status != IFSelect_RetDone:
            raise RuntimeError(f"STEP 파일 저장 실패 (status={status}): {dest}")
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.mkdtemp(prefix="s3d_write_"))
        try:
            tmp_file = tmp_dir / "output.step"
            status = writer.Write(str(tmp_file))
            if status != IFSelect_RetDone:
                raise RuntimeError(f"STEP 파일 저장 실패 (status={status}): {dest}")
            shutil.copy2(tmp_file, dest)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return dest
