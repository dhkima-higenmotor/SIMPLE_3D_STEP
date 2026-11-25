"""STEP file I/O using OpenCASCADE XCAF (preserving assembly hierarchy and part names)."""

from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_ColorRGBA, Quantity_TOC_RGB
from OCP.ShapeAnalysis import ShapeAnalysis_ShapeTolerance
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
from OCP.STEPControl import (
    STEPControl_AsIs,
    STEPControl_ManifoldSolidBrep,
    STEPControl_Writer,
)
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import (
    XCAFDoc_ColorCurv,
    XCAFDoc_ColorGen,
    XCAFDoc_ColorSurf,
    XCAFDoc_ColorTool,
    XCAFDoc_DocumentTool,
    XCAFDoc_ShapeTool,
)

from simple_3d_step.core.occt_compat import (
    LabelSequence as TDF_LabelSequence,
    to_edge,
    to_face,
    to_shell,
    to_solid,
)

from simple_3d_step.models import (
    AssemblyModel,
    CancelCheck,
    PartNode,
    PcbAssemblyModel,
    PcbComponentGroup,
    PcbSimplifiedPart,
    PcbSubBody,
    ProgressCallback,
    RGBColor,
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
    """If path contains non-ASCII chars, copy to a temp ASCII path for OCCT safety."""
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
        raise FileNotFoundError(f"STEP file not found: {source_path}")

    if progress_cb:
        progress_cb(0.05, f"Reading STEP file: {source_path.name}")

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
            raise RuntimeError(f"Failed to parse STEP file (status={status}): {source_path.name}")

        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")

        if progress_cb:
            progress_cb(0.35, "Translating XCAF assembly document...")

        if not reader.Transfer(doc):
            raise RuntimeError(f"Failed to transfer STEP XCAF document: {source_path.name}")
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
        progress_cb(0.55, "Extracting assembly hierarchy and leaf parts...")

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
                raise InterruptedError("Operation cancelled by user.")
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
            raise InterruptedError("Operation cancelled by user.")

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
        progress_cb(0.95, f"Loaded {len(parts)} Solid/Shell parts")

    if parts:
        global_min = np.min(np.vstack([p.bbox_min for p in parts]), axis=0)
        global_max = np.max(np.vstack([p.bbox_max for p in parts]), axis=0)
    else:
        global_min = np.zeros(3, dtype=np.float64)
        global_max = np.zeros(3, dtype=np.float64)

    if progress_cb:
        progress_cb(1.0, f"Loaded ({len(parts)} parts)")

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
    """Write a TopoDS_Shape to a clean cross-kernel STEP file (3D curves, clamped precision).

    Disables explicit OCCT PCURVE export (write.surfacecurve.mode=0) and writes a tight,
    clamped STEP uncertainty [1e-5, 1e-3] derived from the shape's average tolerance so
    both Parasolid (SolidWorks/eDrawings) and older OCCT 7.x viewers (CAD Assistant)
    import solids cleanly without collapsing fine edges or breaking shells.
    """
    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Remove any cached mesh triangulations before STEP export
    BRepTools.Clean_s(shape)

    # Instantiate STEPControl_Writer BEFORE setting Interface_Static parameters so OCCT's
    # STEP controller initialization does not reset our custom export settings in a fresh process.
    writer = STEPControl_Writer()

    Interface_Static.SetCVal_s("write.step.schema", schema)
    Interface_Static.SetCVal_s("write.step.unit", unit)
    # 0 = 3D curves only (matches SolidWorks STEP export; avoids OCCT 8->7 PCURVE issues)
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 0)
    # Compute average shape tolerance and clamp to [1e-5, 1e-3] (matches SolidWorks 1e-5 on
    # clean geometry and prevents mode=1/Max from bloating UNCERTAINTY to 1e-2 and collapsing
    # sub-0.01 mm edges in OCCT 7.x STEP readers). Also cap UNCERTAINTY at <= 0.5 * shortest
    # edge length (when >= 1e-5) so Parasolid/SolidWorks never collapses valid short edges.
    avg_tol = float(ShapeAnalysis_ShapeTolerance().Tolerance(shape, 0))
    clamped_prec = max(1e-5, min(1e-3, avg_tol))
    min_edge_len = float("inf")
    exp_e = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp_e.More():
        ed = to_edge(exp_e.Current())
        if not BRep_Tool.Degenerated_s(ed):
            lp = GProp_GProps()
            BRepGProp.LinearProperties_s(ed, lp)
            elen = abs(float(lp.Mass()))
            if 0.0 < elen < min_edge_len:
                min_edge_len = elen
        exp_e.Next()
    if min_edge_len < float("inf") and 0.5 * min_edge_len >= 1e-5:
        clamped_prec = min(clamped_prec, 0.5 * min_edge_len)
    Interface_Static.SetIVal_s("write.precision.mode", 2)
    Interface_Static.SetRVal_s("write.precision.val", clamped_prec)

    transfer_mode = (
        STEPControl_ManifoldSolidBrep
        if shape.ShapeType() == TopAbs_SOLID
        else STEPControl_AsIs
    )
    writer.Transfer(shape, transfer_mode)

    dest_str = str(dest)
    try:
        dest_str.encode("ascii")
        status = writer.Write(dest_str)
        if status != IFSelect_RetDone:
            raise RuntimeError(f"Failed to write STEP file (status={status}): {dest}")
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.mkdtemp(prefix="s3d_write_"))
        try:
            tmp_file = tmp_dir / "output.step"
            status = writer.Write(str(tmp_file))
            if status != IFSelect_RetDone:
                raise RuntimeError(f"Failed to write STEP file (status={status}): {dest}")
            shutil.copy2(tmp_file, dest)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return dest


# ---------------------------------------------------------------------------
# PCB Mode XCAF I/O (preserves component grouping, names, and RGB colors)
# ---------------------------------------------------------------------------

_COLOR_TYPES = (XCAFDoc_ColorSurf, XCAFDoc_ColorGen, XCAFDoc_ColorCurv)


def _extract_label_color(label: TDF_Label) -> Optional[RGBColor]:
    """Query XCAF color directly attached to an OCAF label."""
    col = Quantity_Color()
    for ctype in _COLOR_TYPES:
        try:
            if XCAFDoc_ColorTool.GetColor_s(label, ctype, col):
                return (float(col.Red()), float(col.Green()), float(col.Blue()))
        except Exception:
            pass
    col_rgba = Quantity_ColorRGBA()
    for ctype in _COLOR_TYPES:
        try:
            if XCAFDoc_ColorTool.GetColor_s(label, ctype, col_rgba):
                c = col_rgba.GetRGB()
                return (float(c.Red()), float(c.Green()), float(c.Blue()))
        except Exception:
            pass
    return None


def _extract_shape_color(color_tool: XCAFDoc_ColorTool, shape: TopoDS_Shape) -> Optional[RGBColor]:
    """Query XCAF color attached to a TopoDS_Shape (unplaced prototype or placed)."""
    if shape.IsNull():
        return None
    col = Quantity_Color()
    for ctype in _COLOR_TYPES:
        try:
            if color_tool.GetColor(shape, ctype, col):
                return (float(col.Red()), float(col.Green()), float(col.Blue()))
        except Exception:
            pass
    col_rgba = Quantity_ColorRGBA()
    for ctype in _COLOR_TYPES:
        try:
            if color_tool.GetColor(shape, ctype, col_rgba):
                c = col_rgba.GetRGB()
                return (float(c.Red()), float(c.Green()), float(c.Blue()))
        except Exception:
            pass
    return None


def _extract_face_colors(
    color_tool: XCAFDoc_ColorTool,
    raw_shape: TopoDS_Shape,
    fallback: Optional[RGBColor] = None,
    max_faces_for_exact_area: int = 80,
) -> list[tuple[RGBColor, float]]:
    """Collect area-weighted (or bbox-area-weighted for large solids) RGB colors across faces."""
    faces_with_col: list[tuple[TopoDS_Shape, RGBColor]] = []
    distinct_keys: set[tuple[int, int, int]] = set()
    exp = TopExp_Explorer(raw_shape, TopAbs_FACE)
    total_faces = 0
    while exp.More():
        face = to_face(exp.Current())
        total_faces += 1
        rgb = _extract_shape_color(color_tool, face) or fallback
        if rgb is not None:
            faces_with_col.append((face, rgb))
            distinct_keys.add((int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255)))
        exp.Next()

    if not faces_with_col:
        return []
    # Fast path: single uniform color across all faces -> skip per-face GProp integration
    if len(distinct_keys) == 1:
        return [(faces_with_col[0][1], 1.0)]

    result: list[tuple[RGBColor, float]] = []
    use_exact = total_faces <= max_faces_for_exact_area
    for face, rgb in faces_with_col:
        if use_exact:
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            area = abs(float(props.Mass()))
        else:
            bmin, bmax = _compute_shape_bbox(face)
            sz = np.sort(bmax - bmin)
            area = float(sz[1] * sz[2])
        if area > 1e-9:
            result.append((rgb, area))
    return result


def _dominant_rgb_from_faces(
    face_colors: list[tuple[RGBColor, float]],
    fallback: Optional[RGBColor] = None,
) -> Optional[RGBColor]:
    """Return the area-weighted dominant RGB color (quantized to 8-bit buckets)."""
    if not face_colors:
        return fallback
    buckets: dict[tuple[int, int, int], list[float]] = {}
    for (r, g, b), area in face_colors:
        key = (
            max(0, min(255, int(round(r * 255)))),
            max(0, min(255, int(round(g * 255)))),
            max(0, min(255, int(round(b * 255)))),
        )
        entry = buckets.get(key)
        if entry is None:
            buckets[key] = [area, r * area, g * area, b * area]
        else:
            entry[0] += area
            entry[1] += r * area
            entry[2] += g * area
            entry[3] += b * area
    best = max(buckets.values(), key=lambda v: v[0])
    total_a = best[0]
    if total_a <= 0.0:
        return fallback
    return (best[1] / total_a, best[2] / total_a, best[3] / total_a)


def _extract_xy_rotation_deg(loc: TopLoc_Location) -> Optional[float]:
    """Extract in-plane Z rotation angle (degrees) if the transform preserves board Z axis."""
    if loc.IsIdentity():
        return 0.0
    try:
        trsf = loc.Transformation()
        # Local +Z axis in world coordinates
        z_world = (trsf.Value(1, 3), trsf.Value(2, 3), trsf.Value(3, 3))
        if abs(z_world[2]) < 0.98:
            return None
        # Local +X axis projected to world XY
        angle = math.degrees(math.atan2(trsf.Value(2, 1), trsf.Value(1, 1)))
        return float(angle % 360.0)
    except Exception:
        return None


def _is_meaningful_step_name(name: str) -> bool:
    """Return True if a STEP XCAF label name is a real part/model name (not auto-ID boilerplate)."""
    s = (name or "").strip()
    if not s:
        return False
    if s.startswith("=>["):
        return False
    low = s.lower()
    if low.startswith("open cascade step translator"):
        return False
    return True


def _extract_component_refdes_and_leaf(comp_label: TDF_Label, fallback: str) -> tuple[str, str, str]:
    """Extract (display_name, ref_des, leaf_part_name) from an Altium/CAD XCAF component node.

    Altium STEP structure is typically:
      occurrence_ref ('856') -> referred_asm ('U1') -> model_ref ('855') -> model_leaf ('User Library-CSG324')
    or nested sub-assemblies like:
      occurrence_ref ('646') -> referred_asm ('J2') -> ... -> leaf ('TF31-20S-0_5SH_PRT-215090.PRT').
    Preserves both the component designator (RefDes) and the deepest leaf part name.
    """
    inst_name = _get_label_name(comp_label)
    ref_label = comp_label
    ref_name = ""
    if XCAFDoc_ShapeTool.IsReference_s(comp_label):
        r = TDF_Label()
        if XCAFDoc_ShapeTool.GetReferredShape_s(comp_label, r):
            ref_label = r
            ref_name = _get_label_name(r)

    # Prefer referred product name (real Altium RefDes like 'U1', 'J2', 'Board') over numeric occurrence ID
    if _is_meaningful_step_name(ref_name):
        ref_des = ref_name
    elif _is_meaningful_step_name(inst_name):
        ref_des = inst_name
    else:
        ref_des = fallback

    # Walk down single-child assembly/reference chains under ref_label to find the bottom-most leaf part name
    leaf_candidates: list[str] = []
    cur = ref_label
    visited: set[str] = set()
    for _ in range(16):
        s_entry = TCollection_AsciiString()
        TDF_Tool.Entry_s(cur, s_entry)
        ent = s_entry.ToCString()
        if ent in visited:
            break
        visited.add(ent)

        if XCAFDoc_ShapeTool.IsReference_s(cur):
            r2 = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(cur, r2):
                rn = _get_label_name(r2)
                if _is_meaningful_step_name(rn) and rn != ref_des:
                    leaf_candidates.append(rn)
                cur = r2
                continue
            break

        if XCAFDoc_ShapeTool.IsAssembly_s(cur):
            children = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(cur, children)
            if children.Length() == 1:
                cur = children.Value(1)
                continue
            # Multi-body model assembly (e.g. FL1 -> 8354540016 -> [solidBody, solidLead, ...]):
            # keep the model assembly name already collected; if none yet, use first meaningful child name
            if not leaf_candidates and children.Length() > 1:
                for ci in range(1, children.Length() + 1):
                    ch = children.Value(ci)
                    chn = _get_label_name(ch)
                    if XCAFDoc_ShapeTool.IsReference_s(ch):
                        r3 = TDF_Label()
                        if XCAFDoc_ShapeTool.GetReferredShape_s(ch, r3):
                            chn = _get_label_name(r3) or chn
                    if _is_meaningful_step_name(chn) and chn != ref_des:
                        leaf_candidates.append(chn)
                        break
            break

        # Leaf simple shape
        ln = _get_label_name(cur)
        if _is_meaningful_step_name(ln) and ln != ref_des:
            leaf_candidates.append(ln)
        break

    leaf_name = leaf_candidates[-1] if leaf_candidates else ""

    if leaf_name and leaf_name.lower() != ref_des.lower():
        # Avoid redundant concatenation if ref_des already includes leaf_name or vice versa
        if ref_des.lower() in leaf_name.lower():
            display_name = leaf_name
        elif leaf_name.lower() in ref_des.lower():
            display_name = ref_des
        else:
            display_name = f"{ref_des}_{leaf_name}"
    else:
        display_name = ref_des

    return display_name, ref_des, leaf_name


def load_pcb_assembly(
    filepath: str | Path,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_check: Optional[CancelCheck] = None,
) -> PcbAssemblyModel:
    """Load an Altium/CAD PCB STEP file via XCAF, preserving component groups and RGB colors."""
    source_path = Path(filepath).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"STEP file not found: {source_path}")

    if progress_cb:
        progress_cb(0.05, f"Reading PCB STEP (with colors): {source_path.name}")

    read_path, tmp_dir = _ensure_ascii_read_path(source_path)
    try:
        app = XCAFApp_Application.GetApplication_s()
        doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        app.InitDocument(doc)

        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        reader.SetLayerMode(False)
        reader.SetPropsMode(False)

        status = reader.ReadFile(str(read_path))
        if status != IFSelect_RetDone:
            raise RuntimeError(f"Failed to parse STEP file (status={status}): {source_path.name}")

        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")

        if progress_cb:
            progress_cb(0.35, "Translating XCAF assembly & color table...")

        if not reader.Transfer(doc):
            raise RuntimeError(f"Failed to transfer STEP XCAF document: {source_path.name}")
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    unit = "MM"
    try:
        u = Interface_Static.CVal_s("xstep.cascade.unit")
        if u:
            unit = str(u).strip().upper()
    except Exception:
        pass

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    root_labels = TDF_LabelSequence()
    shape_tool.GetFreeShapes(root_labels)

    if progress_cb:
        progress_cb(0.55, "Extracting PCB component hierarchy and body colors...")

    # Cache invariant per-prototype-label body properties & colors (avoids recomputing BRepGProp
    # hundreds of times for repeated SMD components like 170 identical C2012 capacitors).
    proto_body_cache: dict[
        tuple[str, Optional[RGBColor]],
        list[tuple[TopoDS_Shape, bool, float, float, int, Optional[RGBColor], list[tuple[RGBColor, float]], str]],
    ] = {}

    def _label_entry(lbl: TDF_Label) -> str:
        s = TCollection_AsciiString()
        TDF_Tool.Entry_s(lbl, s)
        return s.ToCString()

    def _collect_bodies(
        label: TDF_Label,
        parent_loc: TopLoc_Location,
        inherited_color: Optional[RGBColor],
        out_bodies: list[PcbSubBody],
        base_prefix: str,
    ) -> None:
        if cancel_check and cancel_check():
            raise InterruptedError("Operation cancelled by user.")

        lbl_color = _extract_label_color(label) or inherited_color
        label_name = _get_label_name(label)

        if XCAFDoc_ShapeTool.IsReference_s(label):
            ref_label = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(label, ref_label):
                inst_loc = XCAFDoc_ShapeTool.GetLocation_s(label)
                combined_loc = parent_loc.Multiplied(inst_loc)
                ref_color = _extract_label_color(ref_label) or lbl_color
                ref_name = _get_label_name(ref_label)
                sub_name = label_name or ref_name or base_prefix
                _collect_bodies(ref_label, combined_loc, ref_color, out_bodies, sub_name)
                return

        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                _collect_bodies(comps.Value(i), parent_loc, lbl_color, out_bodies, label_name or base_prefix)
            return

        cache_key = (_label_entry(label), lbl_color)
        cached = proto_body_cache.get(cache_key)
        if cached is None:
            raw_shape = XCAFDoc_ShapeTool.GetShape_s(label)
            if raw_shape.IsNull():
                proto_body_cache[cache_key] = []
                return

            shape_color = _extract_shape_color(color_tool, raw_shape) or lbl_color
            raw_subs = _extract_solids_and_free_shells(raw_shape)
            node_name = label_name or base_prefix or f"Body_{label.Tag()}"
            cached = []
            for idx, raw_sub in enumerate(raw_subs, start=1):
                if cancel_check and cancel_check():
                    raise InterruptedError("Operation cancelled by user.")
                sub_color = _extract_shape_color(color_tool, raw_sub) or shape_color
                face_colors = _extract_face_colors(color_tool, raw_sub, fallback=sub_color)
                body_color = _dominant_rgb_from_faces(face_colors, fallback=sub_color)
                vol, area, face_cnt = _compute_shape_props(raw_sub)
                if face_cnt == 0:
                    continue
                bname = node_name if len(raw_subs) == 1 else f"{node_name} [Body {idx}]"
                cached.append(
                    (
                        raw_sub,
                        raw_sub.ShapeType() == TopAbs_SOLID,
                        vol,
                        area,
                        face_cnt,
                        body_color,
                        face_colors,
                        bname,
                    )
                )
            proto_body_cache[cache_key] = cached

        for raw_sub, is_solid, vol, area, face_cnt, body_color, face_colors, bname in cached:
            placed = raw_sub.Located(parent_loc.Multiplied(raw_sub.Location()))
            bmin, bmax = _compute_shape_bbox(placed)
            out_bodies.append(
                PcbSubBody(
                    name=bname,
                    shape=placed,
                    is_solid=is_solid,
                    face_count=face_cnt,
                    volume=vol,
                    surface_area=area,
                    bbox_min=bmin,
                    bbox_max=bmax,
                    color=body_color,
                    face_colors=face_colors,
                )
            )

    # Unwrap top-level assembly wrapper(s) to find the PCB component list
    root_name = source_path.stem
    component_entries: list[tuple[str, TDF_Label, TopLoc_Location, Optional[RGBColor]]] = []
    was_flat = False

    def _resolve_root_entries(
        lbl: TDF_Label, loc: TopLoc_Location, inh_color: Optional[RGBColor]
    ) -> list[tuple[str, TDF_Label, TopLoc_Location, Optional[RGBColor]]]:
        nonlocal root_name
        c_col = _extract_label_color(lbl) or inh_color
        if XCAFDoc_ShapeTool.IsReference_s(lbl):
            ref = TDF_Label()
            if XCAFDoc_ShapeTool.GetReferredShape_s(lbl, ref):
                inst_loc = XCAFDoc_ShapeTool.GetLocation_s(lbl)
                return _resolve_root_entries(ref, loc.Multiplied(inst_loc), c_col)

        if XCAFDoc_ShapeTool.IsAssembly_s(lbl):
            nm = _get_label_name(lbl)
            if nm:
                root_name = nm
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(lbl, comps)
            if comps.Length() == 1:
                # Check if single child is itself a wrapper assembly (common in Altium exports)
                child = comps.Value(1)
                child_ref = child
                child_loc = loc
                child_col = _extract_label_color(child) or c_col
                if XCAFDoc_ShapeTool.IsReference_s(child):
                    ref = TDF_Label()
                    if XCAFDoc_ShapeTool.GetReferredShape_s(child, ref):
                        child_ref = ref
                        child_loc = loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(child))
                        child_col = _extract_label_color(ref) or child_col
                if XCAFDoc_ShapeTool.IsAssembly_s(child_ref):
                    return _resolve_root_entries(child_ref, child_loc, child_col)

            entries: list[tuple[str, TDF_Label, TopLoc_Location, Optional[RGBColor]]] = []
            for i in range(1, comps.Length() + 1):
                comp = comps.Value(i)
                display_name, _ref_des, _leaf_name = _extract_component_refdes_and_leaf(
                    comp, f"Component_{i}"
                )
                entries.append((display_name, comp, loc, c_col))
            return entries

        nm = _get_label_name(lbl) or root_name
        return [(nm, lbl, loc, c_col)]

    identity = TopLoc_Location()
    if root_labels.Length() == 1:
        root_lbl = root_labels.Value(1)
        if not XCAFDoc_ShapeTool.IsAssembly_s(root_lbl) and not XCAFDoc_ShapeTool.IsReference_s(root_lbl):
            was_flat = True
        component_entries = _resolve_root_entries(root_lbl, identity, None)
    else:
        for i in range(1, root_labels.Length() + 1):
            rl = root_labels.Value(i)
            rname = _get_label_name(rl) or f"Component_{i}"
            component_entries.append((rname, rl, identity, None))

    components: list[PcbComponentGroup] = []
    if was_flat and len(component_entries) == 1:
        # Flat STEP with no sub-assemblies: expose each leaf solid as a temporary group
        # so the PCB simplifier can cluster nearby bodies spatially.
        cname, clbl, cloc, ccol = component_entries[0]
        bodies: list[PcbSubBody] = []
        _collect_bodies(clbl, cloc, ccol, bodies, cname)
        for idx, body in enumerate(bodies):
            components.append(
                PcbComponentGroup(
                    comp_id=idx,
                    name=body.name,
                    assembly_path=f"{root_name} / {body.name}",
                    bodies=[body],
                    rotation_xy_deg=0.0,
                    color=body.color,
                )
            )
    else:
        for idx, (cname, clbl, cloc, ccol) in enumerate(component_entries):
            if cancel_check and cancel_check():
                raise InterruptedError("Operation cancelled by user.")
            comp_loc = cloc
            if XCAFDoc_ShapeTool.IsReference_s(clbl):
                comp_loc = cloc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(clbl))
            rot_deg = _extract_xy_rotation_deg(comp_loc)
            display_name, ref_des, leaf_name = _extract_component_refdes_and_leaf(clbl, cname)
            bodies = []
            _collect_bodies(clbl, cloc, ccol, bodies, display_name)
            if not bodies:
                continue
            components.append(
                PcbComponentGroup(
                    comp_id=len(components),
                    name=display_name,
                    assembly_path=f"{root_name} / {display_name}",
                    bodies=bodies,
                    ref_des=ref_des,
                    leaf_name=leaf_name,
                    rotation_xy_deg=rot_deg,
                )
            )
        if len(components) == 1 and len(components[0].bodies) > 1:
            # Single compound part holding all PCB bodies -> split so spatial clustering can group them
            was_flat = True
            flat_bodies = components[0].bodies
            components = [
                PcbComponentGroup(
                    comp_id=i,
                    name=b.name,
                    assembly_path=f"{root_name} / {b.name}",
                    bodies=[b],
                    rotation_xy_deg=0.0,
                    color=b.color,
                )
                for i, b in enumerate(flat_bodies)
            ]

    all_mins = [c.bbox_min for c in components if c.bodies]
    all_maxs = [c.bbox_max for c in components if c.bodies]
    if all_mins:
        global_min = np.min(np.vstack(all_mins), axis=0)
        global_max = np.max(np.vstack(all_maxs), axis=0)
    else:
        global_min = np.zeros(3, dtype=np.float64)
        global_max = np.zeros(3, dtype=np.float64)

    if progress_cb:
        progress_cb(
            1.0,
            f"Loaded PCB STEP: {len(components)} component group(s), "
            f"{sum(len(c.bodies) for c in components)} bodies",
        )

    return PcbAssemblyModel(
        source_path=source_path,
        root_name=root_name,
        components=components,
        unit=unit,
        bbox_min=global_min,
        bbox_max=global_max,
        was_flat=was_flat,
    )


def save_colored_assembly_to_step(
    parts: list[PcbSimplifiedPart],
    output_path: str | Path,
    *,
    root_name: str = "PCB_Simplified",
    schema: str = "AP214",
    unit: str = "MM",
    preserve_colors: bool = True,
) -> Path:
    """Write simplified PCB components to a clean colored XCAF STEP assembly (AP214/AP242)."""
    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    effective_schema = (schema or "AP214").upper()
    if preserve_colors and effective_schema == "AP203":
        effective_schema = "AP214"

    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    root_label = shape_tool.NewShape()
    TDataStd_Name.Set_s(root_label, TCollection_ExtendedString(root_name or "PCB_Simplified"))

    min_edge_len = float("inf")
    max_avg_tol = 1e-5
    identity = TopLoc_Location()
    proto_cache: dict[tuple, TDF_Label] = {}

    def _inspect_shape_precision(sh: TopoDS_Shape) -> None:
        nonlocal min_edge_len, max_avg_tol
        BRepTools.Clean_s(sh)
        try:
            avg_tol = float(ShapeAnalysis_ShapeTolerance().Tolerance(sh, 0))
            if avg_tol > max_avg_tol:
                max_avg_tol = avg_tol
        except Exception:
            pass
        exp_e = TopExp_Explorer(sh, TopAbs_EDGE)
        while exp_e.More():
            ed = to_edge(exp_e.Current())
            if not BRep_Tool.Degenerated_s(ed):
                lp = GProp_GProps()
                BRepGProp.LinearProperties_s(ed, lp)
                elen = abs(float(lp.Mass()))
                if 0.0 < elen < min_edge_len:
                    min_edge_len = elen
            exp_e.Next()

    def _unwrap_single_solid(sh: TopoDS_Shape) -> TopoDS_Shape:
        """Unwrap a 1-solid Compound (from Fuse / UnifySameDomain) to a plain TopoDS_Solid for clean XCAF coloring."""
        if sh.ShapeType() == TopAbs_SOLID:
            return sh
        solids: list[TopoDS_Shape] = []
        exp_s = TopExp_Explorer(sh, TopAbs_SOLID)
        while exp_s.More():
            solids.append(to_solid(exp_s.Current()))
            exp_s.Next()
        if len(solids) == 1:
            return solids[0]
        return sh

    def _apply_color(lbl: TDF_Label, rgb: Optional[RGBColor]) -> None:
        if preserve_colors and rgb is not None:
            r, g, b = rgb
            qcol = Quantity_Color(
                max(0.0, min(1.0, float(r))),
                max(0.0, min(1.0, float(g))),
                max(0.0, min(1.0, float(b))),
                Quantity_TOC_RGB,
            )
            color_tool.SetColor(lbl, qcol, XCAFDoc_ColorGen)
            color_tool.SetColor(lbl, qcol, XCAFDoc_ColorSurf)

    for idx, part in enumerate(parts, start=1):
        clean_name = (part.name or f"Part_{idx}").strip()
        ref_title = (part.ref_des or "").strip() or clean_name
        leaf_title = (part.leaf_name or "").strip()
        use_two_level = bool(leaf_title) and (leaf_title.lower() != ref_title.lower())

        # Resolve (proto_label, comp_loc) either from shared prototype cache or unique solid
        if (
            part.proto_key is not None
            and part.proto_shape is not None
            and not part.proto_shape.IsNull()
            and part.placement_trsf is not None
        ):
            proto_label = proto_cache.get(part.proto_key)
            if proto_label is None:
                proto_sh = _unwrap_single_solid(part.proto_shape)
                _inspect_shape_precision(proto_sh)
                proto_label = shape_tool.AddShape(proto_sh, False)
                proto_name = leaf_title or clean_name or f"{part.proto_key[0]}_{len(proto_cache) + 1}"
                TDataStd_Name.Set_s(proto_label, TCollection_ExtendedString(proto_name))
                _apply_color(proto_label, part.color)
                proto_cache[part.proto_key] = proto_label
            comp_loc = TopLoc_Location(part.placement_trsf)
        else:
            sh = part.shape
            if sh is None or sh.IsNull():
                continue
            sh = _unwrap_single_solid(sh)
            sh_loc = sh.Location()
            if not sh_loc.IsIdentity():
                sh_proto = sh.Located(identity)
                comp_loc = sh_loc
            else:
                sh_proto = sh
                comp_loc = identity

            _inspect_shape_precision(sh_proto)
            proto_label = shape_tool.AddShape(sh_proto, False)
            proto_name = leaf_title if use_two_level else ref_title
            TDataStd_Name.Set_s(proto_label, TCollection_ExtendedString(proto_name))
            _apply_color(proto_label, part.color)

        if use_two_level:
            # Altium-native 2-level hierarchy: Root -> RefDes (component designator) -> Leaf Part Name
            comp_asm = shape_tool.NewShape()
            TDataStd_Name.Set_s(comp_asm, TCollection_ExtendedString(ref_title))
            leaf_occ = shape_tool.AddComponent(comp_asm, proto_label, comp_loc)
            TDataStd_Name.Set_s(leaf_occ, TCollection_ExtendedString(leaf_title))
            comp_occ = shape_tool.AddComponent(root_label, comp_asm, identity)
            TDataStd_Name.Set_s(comp_occ, TCollection_ExtendedString(ref_title))
        else:
            comp_occ = shape_tool.AddComponent(root_label, proto_label, comp_loc)
            TDataStd_Name.Set_s(comp_occ, TCollection_ExtendedString(ref_title))

    shape_tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    Interface_Static.SetCVal_s("write.step.schema", effective_schema)
    Interface_Static.SetCVal_s("write.step.unit", unit)
    Interface_Static.SetIVal_s("write.surfacecurve.mode", 0)

    clamped_prec = max(1e-5, min(1e-3, max_avg_tol))
    if min_edge_len < float("inf") and 0.5 * min_edge_len >= 1e-5:
        clamped_prec = min(clamped_prec, 0.5 * min_edge_len)
    Interface_Static.SetIVal_s("write.precision.mode", 2)
    Interface_Static.SetRVal_s("write.precision.val", clamped_prec)

    writer.SetNameMode(True)
    writer.SetColorMode(bool(preserve_colors))

    if not writer.Transfer(doc, STEPControl_AsIs):
        raise RuntimeError(f"Failed to transfer colored PCB assembly to STEP: {dest}")

    dest_str = str(dest)
    try:
        dest_str.encode("ascii")
        status = writer.Write(dest_str)
        if status != IFSelect_RetDone:
            raise RuntimeError(f"Failed to write PCB STEP file (status={status}): {dest}")
    except UnicodeEncodeError:
        tmp_dir = Path(tempfile.mkdtemp(prefix="s3d_pcb_write_"))
        try:
            tmp_file = tmp_dir / "output.step"
            status = writer.Write(str(tmp_file))
            if status != IFSelect_RetDone:
                raise RuntimeError(f"Failed to write PCB STEP file (status={status}): {dest}")
            shutil.copy2(tmp_file, dest)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return dest
