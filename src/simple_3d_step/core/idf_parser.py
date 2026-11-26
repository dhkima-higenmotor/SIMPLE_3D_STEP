"""Altium / standard IDF 2.0 & 3.0 (.bdf/.ldf) parser and STEP missing-component supplementer."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.GC import GC_MakeArcOfCircle
from OCP.gp import gp_Ax1, gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Shape, ShapeFix_Wire
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape, TopoDS_Wire

from simple_3d_step.core.occt_compat import ShapeList
from simple_3d_step.models import PcbSimplifiedPart, RGBColor


# Extension pairs in priority order: Altium (.bdf/.ldf) first, then standard IDF pairs
IDF_BOARD_EXTS = (".bdf", ".brd", ".emn")
IDF_LIB_EXTS = (".ldf", ".pro", ".emp")
IDF_EXT_PAIRS = (
    (".bdf", ".ldf"),
    (".brd", ".pro"),
    (".emn", ".emp"),
)


@dataclass
class IdfFootprint:
    """Component 2D outline bounds and height parsed from an IDF library (.ldf) file."""

    geometry_name: str
    part_number: str
    height_mm: float
    xmin_mm: float
    ymin_mm: float
    xmax_mm: float
    ymax_mm: float
    is_mechanical: bool = False
    is_circle: bool = False
    circle_cx_mm: float = 0.0
    circle_cy_mm: float = 0.0
    circle_radius_mm: float = 0.0

    @property
    def width_mm(self) -> float:
        return max(0.0, self.xmax_mm - self.xmin_mm)

    @property
    def depth_mm(self) -> float:
        return max(0.0, self.ymax_mm - self.ymin_mm)


@dataclass
class IdfDrilledHole:
    """Single drilled hole record parsed from an IDF .DRILLED_HOLES section."""

    diameter_mm: float
    x_mm: float
    y_mm: float
    plating: str = "PTH"
    associated_part: str = "BOARD"
    hole_type: str = "PIN"


@dataclass
class IdfPlacement:
    """Single component placement record parsed from an IDF board (.bdf) file."""

    package_name: str
    part_number: str
    ref_des: str
    x_mm: float
    y_mm: float
    z_offset_mm: float
    rotation_deg: float
    side: str  # "TOP" | "BOTTOM"
    status: str = "PLACED"  # "PLACED" | "UNPLACED" | "MCAD" | "ECAD"

    @property
    def is_placed(self) -> bool:
        return self.status.upper() != "UNPLACED"


@dataclass
class IdfBoardData:
    """Complete parsed IDF board (.bdf) + library (.ldf) dataset in millimeters."""

    bdf_path: Optional[Path] = None
    ldf_path: Optional[Path] = None
    idf_version: str = "3.0"
    board_name: str = ""
    board_thickness_mm: float = 1.6
    board_bbox_xy: Optional[tuple[float, float, float, float]] = None  # (xmin, ymin, xmax, ymax)
    board_outline_loops: dict[int, list[tuple[float, float, float]]] = field(default_factory=dict)
    drilled_holes: list[IdfDrilledHole] = field(default_factory=list)
    placements: list[IdfPlacement] = field(default_factory=list)
    footprints: dict[tuple[str, str], IdfFootprint] = field(default_factory=dict)
    footprints_by_geom: dict[str, IdfFootprint] = field(default_factory=dict)

    def lookup_footprint(self, package_name: str, part_number: str) -> Optional[IdfFootprint]:
        key = (package_name.strip().upper(), part_number.strip().upper())
        fp = self.footprints.get(key)
        if fp is not None:
            return fp
        return self.footprints_by_geom.get(package_name.strip().upper())


@dataclass
class IdfAlignment:
    """Rigid 2D XY + Z elevation alignment mapping IDF board coordinates into STEP world space."""

    dx: float = 0.0
    dy: float = 0.0
    rot_deg: float = 0.0  # Rotation about Z applied around IDF board center
    idf_center_x: float = 0.0
    idf_center_y: float = 0.0
    board_z_bottom: float = 0.0
    board_z_top: float = 1.6

    def transform_xy(self, x: float, y: float) -> tuple[float, float]:
        if abs(self.rot_deg) < 1e-6:
            return (x + self.dx, y + self.dy)
        rad = math.radians(self.rot_deg)
        c, s = math.cos(rad), math.sin(rad)
        rx = x - self.idf_center_x
        ry = y - self.idf_center_y
        wx = self.idf_center_x + (rx * c - ry * s) + self.dx
        wy = self.idf_center_y + (rx * s + ry * c) + self.dy
        return (wx, wy)


@dataclass
class StepComponentFootprint:
    """Lightweight descriptor of an existing STEP component for IDF deduplication."""

    name: str
    bbox_min: np.ndarray
    bbox_max: np.ndarray
    ref_des: str = ""

    @property
    def center(self) -> np.ndarray:
        return 0.5 * (self.bbox_min + self.bbox_max)


# ---------------------------------------------------------------------------
# IDF File Discovery
# ---------------------------------------------------------------------------

def find_matching_idf_files(step_path: str | Path) -> tuple[Optional[Path], Optional[Path]]:
    """Auto-discover matching IDF board/library files in the same directory as a STEP file.

    Prioritizes Altium ``.bdf`` + ``.ldf`` pairs sharing the STEP file stem (case-insensitive),
    then falls back to standard IDF pairs (``.brd``/``.pro``, ``.emn``/``.emp``).
    """
    src = Path(step_path)
    parent = src.parent
    if not parent.is_dir():
        return None, None

    stem_lower = src.stem.lower()
    by_ext: dict[str, Path] = {}
    try:
        for child in parent.iterdir():
            if not child.is_file():
                continue
            if child.stem.lower() == stem_lower:
                ext = child.suffix.lower()
                if ext in IDF_BOARD_EXTS or ext in IDF_LIB_EXTS:
                    by_ext[ext] = child.resolve()
    except OSError:
        return None, None

    for b_ext, l_ext in IDF_EXT_PAIRS:
        b = by_ext.get(b_ext)
        l = by_ext.get(l_ext)
        if b is not None or l is not None:
            return b, l
    return None, None


# ---------------------------------------------------------------------------
# IDF Tokenizer & Unit Helpers
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')


def _tokenize_idf_line(line: str) -> list[str]:
    """Split an IDF record line, respecting double-quoted strings and '#' comments."""
    # Strip comments outside quotes
    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#"):
        return []
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(cleaned):
        if m.group(1) is not None:
            tokens.append(m.group(1))
        else:
            tok = m.group(2)
            if tok.startswith("#"):
                break
            tokens.append(tok)
    return tokens


def _unit_scale_to_mm(unit_token: str) -> float:
    u = unit_token.strip().upper()
    if u in ("THOU", "MILS", "MIL"):
        return 0.0254
    if u in ("INCH", "IN", "INCHES"):
        return 25.4
    return 1.0


def _try_float(tok: str) -> Optional[float]:
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def _outline_points_to_bbox(
    records: list[tuple[int, float, float, float]],
    scale: float,
    outer_loop_only: bool = False,
) -> Optional[tuple[float, float, float, float]]:
    """Compute 2D XY bounding box (mm) from IDF outline loop records (loop, x, y, angle)."""
    if not records:
        return None

    loops: dict[int, list[tuple[float, float, float]]] = {}
    for loop_id, x, y, ang in records:
        loops.setdefault(loop_id, []).append((x * scale, y * scale, ang))

    if outer_loop_only and 0 in loops:
        selected_loops = [loops[0]]
    else:
        selected_loops = list(loops.values())

    xs: list[float] = []
    ys: list[float] = []

    for pts in selected_loops:
        n = len(pts)
        for i, (x, y, ang) in enumerate(pts):
            # IDF circle encoding: two points where 2nd has angle == 360 (1st=center, 2nd=point on circle)
            if abs(abs(ang) - 360.0) < 1e-3 and i > 0:
                cx, cy, _ = pts[i - 1]
                r = math.hypot(x - cx, y - cy)
                xs.extend([cx - r, cx + r])
                ys.extend([cy - r, cy + r])
                continue
            xs.append(x)
            ys.append(y)
            # Non-zero included arc between previous point and current point
            if abs(ang) > 1e-3 and abs(ang) < 359.999 and i > 0:
                x0, y0, _ = pts[i - 1]
                _append_arc_samples(x0, y0, x, y, ang, xs, ys)

    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _append_arc_samples(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    included_deg: float,
    xs: list[float],
    ys: list[float],
) -> None:
    """Sample intermediate points along an IDF circular arc so the bbox captures bulges."""
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord < 1e-9:
        return
    rad = math.radians(included_deg)
    sin_half = math.sin(abs(rad) * 0.5)
    if sin_half < 1e-9:
        return
    radius = chord / (2.0 * sin_half)
    # Center lies perpendicular to chord
    mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    dx, dy = (x1 - x0) / chord, (y1 - y0) / chord
    # Left normal (-dy, dx) for positive CCW arc; flip if |angle| > 180 or negative
    h_sq = max(0.0, radius * radius - (0.5 * chord) ** 2)
    h = math.sqrt(h_sq)
    sign = 1.0 if included_deg > 0 else -1.0
    if abs(included_deg) > 180.0:
        sign = -sign
    cx = mx - sign * dy * h
    cy = my + sign * dx * h

    a0 = math.atan2(y0 - cy, x0 - cx)
    steps = max(4, int(math.ceil(abs(included_deg) / 22.5)))
    for k in range(1, steps):
        a = a0 + rad * (k / steps)
        xs.append(cx + radius * math.cos(a))
        ys.append(cy + radius * math.sin(a))


# ---------------------------------------------------------------------------
# .bdf & .ldf Parsers
# ---------------------------------------------------------------------------

def parse_idf_ldf(ldf_path: str | Path) -> tuple[dict[tuple[str, str], IdfFootprint], dict[str, IdfFootprint]]:
    """Parse an Altium/standard IDF library file (.ldf / .pro / .emp)."""
    path = Path(ldf_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    by_pair: dict[tuple[str, str], IdfFootprint] = {}
    by_geom: dict[str, IdfFootprint] = {}

    section: Optional[str] = None
    cur_header: Optional[tuple[str, str, float, float, bool]] = None  # geom, pn, scale, height_mm, is_mech
    cur_points: list[tuple[int, float, float, float]] = []

    def _flush_component() -> None:
        nonlocal cur_header, cur_points
        if cur_header is None:
            return
        geom, pn, scale, height_mm, is_mech = cur_header
        bbox = _outline_points_to_bbox(cur_points, scale, outer_loop_only=False)
        if bbox is not None:
            is_circle = False
            ccx, ccy, crad = 0.0, 0.0, 0.0
            if (
                len(cur_points) == 2
                and cur_points[0][0] == cur_points[1][0]
                and abs(abs(cur_points[1][3]) - 360.0) < 1e-3
            ):
                ccx = cur_points[0][1] * scale
                ccy = cur_points[0][2] * scale
                crad = math.hypot(
                    cur_points[1][1] * scale - ccx,
                    cur_points[1][2] * scale - ccy,
                )
                if crad > 1e-4:
                    is_circle = True
            fp = IdfFootprint(
                geometry_name=geom,
                part_number=pn,
                height_mm=max(0.0, height_mm),
                xmin_mm=bbox[0],
                ymin_mm=bbox[1],
                xmax_mm=bbox[2],
                ymax_mm=bbox[3],
                is_mechanical=is_mech,
                is_circle=is_circle,
                circle_cx_mm=ccx,
                circle_cy_mm=ccy,
                circle_radius_mm=crad,
            )
            by_pair[(geom.upper(), pn.upper())] = fp
            by_geom.setdefault(geom.upper(), fp)
        cur_header = None
        cur_points = []

    for raw in lines:
        toks = _tokenize_idf_line(raw)
        if not toks:
            continue
        head = toks[0].upper()

        if head in (".ELECTRICAL", ".MECHANICAL"):
            _flush_component()
            section = head
            cur_header = None
            cur_points = []
            continue

        if head in (".END_ELECTRICAL", ".END_MECHANICAL"):
            _flush_component()
            section = None
            continue

        if head.startswith("."):
            section = None
            continue

        if section in (".ELECTRICAL", ".MECHANICAL"):
            if cur_header is None:
                # Record 1: <geometry_name> <part_number> <units> <height>
                if len(toks) >= 4:
                    geom = toks[0]
                    pn = toks[1]
                    scale = _unit_scale_to_mm(toks[2])
                    h_val = _try_float(toks[3])
                    height_mm = (h_val * scale) if h_val is not None else 0.0
                    cur_header = (geom, pn, scale, height_mm, section == ".MECHANICAL")
                elif len(toks) == 3:
                    geom = toks[0]
                    pn = ""
                    scale = _unit_scale_to_mm(toks[1])
                    h_val = _try_float(toks[2])
                    height_mm = (h_val * scale) if h_val is not None else 0.0
                    cur_header = (geom, pn, scale, height_mm, section == ".MECHANICAL")
            else:
                # Outline point record: <loop_label> <x> <y> <included_angle>
                if len(toks) >= 4:
                    lid = _try_float(toks[0])
                    x = _try_float(toks[1])
                    y = _try_float(toks[2])
                    ang = _try_float(toks[3])
                    if lid is not None and x is not None and y is not None and ang is not None:
                        cur_points.append((int(lid), x, y, ang))

    _flush_component()
    return by_pair, by_geom


def parse_idf_bdf(bdf_path: str | Path) -> IdfBoardData:
    """Parse an Altium/standard IDF board file (.bdf / .brd / .emn)."""
    path = Path(bdf_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    data = IdfBoardData(bdf_path=path.resolve())
    board_scale = 1.0

    section: Optional[str] = None
    header_line_idx = 0
    outline_thickness_read = False
    outline_points: list[tuple[int, float, float, float]] = []
    pending_placement: Optional[tuple[str, str, str]] = None

    for raw in lines:
        toks = _tokenize_idf_line(raw)
        if not toks:
            continue
        head = toks[0].upper()

        if head == ".HEADER":
            section = ".HEADER"
            header_line_idx = 0
            continue
        if head == ".END_HEADER":
            section = None
            continue
        if head == ".BOARD_OUTLINE":
            section = ".BOARD_OUTLINE"
            outline_thickness_read = False
            continue
        if head == ".END_BOARD_OUTLINE":
            section = None
            continue
        if head == ".DRILLED_HOLES":
            section = ".DRILLED_HOLES"
            continue
        if head == ".END_DRILLED_HOLES":
            section = None
            continue
        if head == ".PLACEMENT":
            section = ".PLACEMENT"
            pending_placement = None
            continue
        if head == ".END_PLACEMENT":
            section = None
            pending_placement = None
            continue
        if head.startswith("."):
            section = None
            continue

        if section == ".HEADER":
            header_line_idx += 1
            if header_line_idx == 1 and len(toks) >= 2:
                data.idf_version = toks[1]
            elif header_line_idx == 2:
                data.board_name = toks[0]
                if len(toks) >= 2:
                    board_scale = _unit_scale_to_mm(toks[1])

        elif section == ".BOARD_OUTLINE":
            if not outline_thickness_read:
                # IDF 3.0 may have an owner line (ECAD / MCAD / UNOWNED) before thickness
                if len(toks) == 1:
                    val = _try_float(toks[0])
                    if val is not None:
                        data.board_thickness_mm = abs(val) * board_scale
                        outline_thickness_read = True
                    # else owner keyword, keep waiting for thickness line
                elif len(toks) >= 2:
                    val = _try_float(toks[0])
                    if val is not None:
                        data.board_thickness_mm = abs(val) * board_scale
                        outline_thickness_read = True
                    else:
                        val2 = _try_float(toks[1])
                        if val2 is not None:
                            data.board_thickness_mm = abs(val2) * board_scale
                            outline_thickness_read = True
            else:
                if len(toks) >= 4:
                    lid = _try_float(toks[0])
                    x = _try_float(toks[1])
                    y = _try_float(toks[2])
                    ang = _try_float(toks[3])
                    if lid is not None and x is not None and y is not None and ang is not None:
                        outline_points.append((int(lid), x, y, ang))

        elif section == ".DRILLED_HOLES":
            if len(toks) >= 3:
                dia = _try_float(toks[0])
                hx = _try_float(toks[1])
                hy = _try_float(toks[2])
                if dia is not None and dia > 0.0 and hx is not None and hy is not None:
                    if len(toks) >= 6:
                        plating = toks[3].upper()
                        assoc = toks[4]
                        htype = toks[5].upper()
                    elif len(toks) >= 4:
                        plating = "PTH"
                        assoc = toks[3]
                        htype = "PIN"
                    else:
                        plating = "PTH"
                        assoc = "BOARD"
                        htype = "PIN"
                    data.drilled_holes.append(
                        IdfDrilledHole(
                            diameter_mm=abs(dia) * board_scale,
                            x_mm=hx * board_scale,
                            y_mm=hy * board_scale,
                            plating=plating,
                            associated_part=assoc,
                            hole_type=htype,
                        )
                    )

        elif section == ".PLACEMENT":
            if pending_placement is None:
                if len(toks) >= 3:
                    pending_placement = (toks[0], toks[1], toks[2])
                elif len(toks) == 2:
                    pending_placement = (toks[0], "", toks[1])
            else:
                pkg, pn, ref = pending_placement
                pending_placement = None
                placement = _parse_placement_coords(pkg, pn, ref, toks, board_scale)
                if placement is not None:
                    data.placements.append(placement)

    if outline_points:
        for loop_id, ox, oy, oang in outline_points:
            data.board_outline_loops.setdefault(loop_id, []).append(
                (ox * board_scale, oy * board_scale, oang)
            )
        data.board_bbox_xy = _outline_points_to_bbox(outline_points, board_scale, outer_loop_only=True)
    elif data.placements:
        xs = [p.x_mm for p in data.placements]
        ys = [p.y_mm for p in data.placements]
        data.board_bbox_xy = (min(xs), min(ys), max(xs), max(ys))

    return data


def _parse_placement_coords(
    pkg: str,
    pn: str,
    ref: str,
    toks: list[str],
    scale: float,
) -> Optional[IdfPlacement]:
    """Parse line 2 of an IDF 2.0 or 3.0 .PLACEMENT entry."""
    if len(toks) < 4:
        return None
    x = _try_float(toks[0])
    y = _try_float(toks[1])
    if x is None or y is None:
        return None

    # Find TOP/BOTTOM side token to cleanly distinguish IDF 3.0 (has z_offset) vs IDF 2.0
    side_idx = -1
    for i in range(2, len(toks)):
        if toks[i].upper() in ("TOP", "BOTTOM"):
            side_idx = i
            break

    if side_idx == 4:
        # IDF 3.0: x y z_offset rotation side [status]
        z_off = _try_float(toks[2]) or 0.0
        rot = _try_float(toks[3]) or 0.0
        side = toks[4].upper()
        status = toks[5].upper() if len(toks) >= 6 else "PLACED"
    elif side_idx == 3:
        # IDF 2.0: x y rotation side [status]
        z_off = 0.0
        rot = _try_float(toks[2]) or 0.0
        side = toks[3].upper()
        status = toks[4].upper() if len(toks) >= 5 else "PLACED"
    else:
        # Best-effort fallback
        z_off = _try_float(toks[2]) or 0.0
        rot = _try_float(toks[3]) or 0.0
        side = "TOP"
        status = "PLACED"

    return IdfPlacement(
        package_name=pkg,
        part_number=pn,
        ref_des=ref,
        x_mm=x * scale,
        y_mm=y * scale,
        z_offset_mm=z_off * scale,
        rotation_deg=rot,
        side=side,
        status=status,
    )


def load_idf_pair(
    bdf_path: Optional[str | Path],
    ldf_path: Optional[str | Path] = None,
) -> Optional[IdfBoardData]:
    """Load an IDF board file (.bdf) and optional library file (.ldf) together."""
    if not bdf_path:
        return None
    bp = Path(bdf_path)
    if not bp.is_file():
        return None
    data = parse_idf_bdf(bp)
    if ldf_path:
        lp = Path(ldf_path)
        if lp.is_file():
            data.ldf_path = lp.resolve()
            data.footprints, data.footprints_by_geom = parse_idf_ldf(lp)
    return data


# ---------------------------------------------------------------------------
# Coordinate Alignment & Missing Component Supplementation
# ---------------------------------------------------------------------------

def align_idf_to_step_board(
    idf: IdfBoardData,
    board_bbox_min: np.ndarray,
    board_bbox_max: np.ndarray,
    step_components: list[StepComponentFootprint],
) -> IdfAlignment:
    """Align IDF board XY/Z coordinates to the detected STEP PCB board geometry."""
    bx0, by0, bz0 = float(board_bbox_min[0]), float(board_bbox_min[1]), float(board_bbox_min[2])
    bx1, by1, bz1 = float(board_bbox_max[0]), float(board_bbox_max[1]), float(board_bbox_max[2])
    board_cx = 0.5 * (bx0 + bx1)
    board_cy = 0.5 * (by0 + by1)
    board_w = bx1 - bx0
    board_h = by1 - by0

    if idf.board_bbox_xy is not None:
        ix0, iy0, ix1, iy1 = idf.board_bbox_xy
    else:
        return IdfAlignment(board_z_bottom=bz0, board_z_top=bz1)

    idf_cx = 0.5 * (ix0 + ix1)
    idf_cy = 0.5 * (iy0 + iy1)
    idf_w = ix1 - ix0
    idf_h = iy1 - iy0

    # Candidate rotations that preserve board aspect ratio
    candidates: list[float] = [0.0, 180.0]
    if abs(idf_w - board_h) < max(3.0, 0.15 * max(board_w, board_h)) and abs(
        idf_h - board_w
    ) < max(3.0, 0.15 * max(board_w, board_h)):
        candidates.extend([90.0, 270.0])

    # Evaluate alignment candidates against STEP component centroids (prefer 0 deg on ties)
    placed = [p for p in idf.placements if p.is_placed]
    step_pts = np.array([[c.center[0], c.center[1]] for c in step_components], dtype=np.float64) if step_components else None

    best_align = IdfAlignment(
        dx=board_cx - idf_cx,
        dy=board_cy - idf_cy,
        rot_deg=0.0,
        idf_center_x=idf_cx,
        idf_center_y=idf_cy,
        board_z_bottom=bz0,
        board_z_top=bz1,
    )

    if step_pts is None or len(step_pts) == 0 or not placed:
        return best_align

    best_score = -1.0
    for rot in candidates:
        cand = IdfAlignment(
            dx=board_cx - idf_cx,
            dy=board_cy - idf_cy,
            rot_deg=rot,
            idf_center_x=idf_cx,
            idf_center_y=idf_cy,
            board_z_bottom=bz0,
            board_z_top=bz1,
        )
        # Count how many IDF components land within 2.5 mm of an existing STEP component
        hits = 0.0
        for pl in placed:
            wx, wy = cand.transform_xy(pl.x_mm, pl.y_mm)
            dists = np.hypot(step_pts[:, 0] - wx, step_pts[:, 1] - wy)
            dmin = float(np.min(dists))
            if dmin <= 2.5:
                hits += 1.0 + max(0.0, (2.5 - dmin) * 0.05)
        # Slight tie-breaker favoring 0 degrees (standard Altium export)
        if rot == 0.0:
            hits += 0.25
        if hits > best_score:
            best_score = hits
            best_align = cand

    # Refine small translation offset using median residual of matched STEP components
    # (eliminates sub-mm arc-sampling drift between IDF outline and STEP board bbox)
    res_dx: list[float] = []
    res_dy: list[float] = []
    for pl in placed:
        wx, wy = best_align.transform_xy(pl.x_mm, pl.y_mm)
        dists = np.hypot(step_pts[:, 0] - wx, step_pts[:, 1] - wy)
        idx = int(np.argmin(dists))
        if float(dists[idx]) <= 1.2:
            res_dx.append(float(step_pts[idx, 0]) - wx)
            res_dy.append(float(step_pts[idx, 1]) - wy)
    if len(res_dx) >= 3:
        best_align.dx += float(np.median(res_dx))
        best_align.dy += float(np.median(res_dy))
        # Snap near-zero residual to exact 0.0 (same-origin Altium STEP + IDF export)
        if abs(best_align.dx) < 0.05 and abs(best_align.dy) < 0.05 and best_align.rot_deg == 0.0:
            best_align.dx = 0.0
            best_align.dy = 0.0

    return best_align


_NON_PART_FOOTPRINT_RE = re.compile(
    r"\b(HOLE|FIDUCIAL|FID|MOUNTING[_\s-]?HOLE|NPTH|PTH[_\s-]?HOLE)\b",
    re.IGNORECASE,
)


def _is_zero_height_non_component(pl: IdfPlacement, fp: Optional[IdfFootprint]) -> bool:
    """Skip zero-height mounting holes / fiducials that are not physical components."""
    height = fp.height_mm if fp is not None else 0.0
    if height > 0.05:
        return False
    combined = f"{pl.package_name} {pl.part_number} {pl.ref_des}"
    return _NON_PART_FOOTPRINT_RE.search(combined) is not None


def _refdes_matches_name(ref_des: str, step_name: str) -> bool:
    """Check if an IDF RefDes (e.g. 'U1', 'R12') appears as a distinct token in a STEP name."""
    ref = ref_des.strip()
    if not ref or ref.upper() in ("NOREFDES", "BOARD", "PCB"):
        return False
    # Match whole token separated by start/end or non-alphanumeric delimiters
    pattern = r"(?<![A-Za-z0-9])" + re.escape(ref) + r"(?![A-Za-z0-9])"
    return re.search(pattern, step_name, flags=re.IGNORECASE) is not None


def _compute_idf_world_footprint(
    placement: IdfPlacement,
    fp: Optional[IdfFootprint],
    alignment: IdfAlignment,
    default_size_mm: float = 2.0,
    default_height_mm: float = 1.0,
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return (wx_center, wy_center, local_w, local_d, total_rot_deg, z_min, z_max, height)."""
    if fp is not None and fp.width_mm > 1e-4 and fp.depth_mm > 1e-4:
        u0, u1 = fp.xmin_mm, fp.xmax_mm
        v0, v1 = fp.ymin_mm, fp.ymax_mm
        height = fp.height_mm if fp.height_mm > 0.05 else default_height_mm
    else:
        half = 0.5 * default_size_mm
        u0, u1 = -half, half
        v0, v1 = -half, half
        height = default_height_mm

    # Per IDF 3.0 spec: BOTTOM side mirrors footprint X before rotation
    if placement.side == "BOTTOM":
        u0, u1 = -u1, -u0

    local_cx = 0.5 * (u0 + u1)
    local_cy = 0.5 * (v0 + v1)
    local_w = max(0.2, u1 - u0)
    local_d = max(0.2, v1 - v0)

    # Rotate local footprint center into IDF board space
    rad = math.radians(placement.rotation_deg)
    c, s = math.cos(rad), math.sin(rad)
    idf_cx = placement.x_mm + (local_cx * c - local_cy * s)
    idf_cy = placement.y_mm + (local_cx * s + local_cy * c)

    wx, wy = alignment.transform_xy(idf_cx, idf_cy)
    total_rot = (placement.rotation_deg + alignment.rot_deg) % 360.0

    if placement.side == "BOTTOM":
        z_max = alignment.board_z_bottom - placement.z_offset_mm
        z_min = z_max - height
    else:
        z_min = alignment.board_z_top + placement.z_offset_mm
        z_max = z_min + height

    return (wx, wy, local_w, local_d, total_rot, z_min, z_max, height)


def _rotated_rect_aabb(
    cx: float, cy: float, w: float, d: float, rot_deg: float
) -> tuple[float, float, float, float]:
    rad = math.radians(rot_deg)
    ac, as_ = abs(math.cos(rad)), abs(math.sin(rad))
    hw = 0.5 * (w * ac + d * as_)
    hd = 0.5 * (w * as_ + d * ac)
    return (cx - hw, cy - hd, cx + hw, cy + hd)


def _xy_overlap_ratio(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0.0:
        return 0.0
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def build_idf_box_shape(
    cx: float,
    cy: float,
    w: float,
    d: float,
    rot_deg: float,
    z_min: float,
    z_max: float,
) -> TopoDS_Shape:
    """Create a watertight OCCT rectangular box centered at (cx, cy), rotated by rot_deg, spanning [z_min, z_max]."""
    proto, trsf = build_idf_box_proto_and_trsf(cx, cy, w, d, rot_deg, z_min, z_max)
    return proto.Located(TopLoc_Location(trsf))


def build_idf_box_proto_and_trsf(
    cx: float,
    cy: float,
    w: float,
    d: float,
    rot_deg: float,
    z_min: float,
    z_max: float,
) -> tuple[TopoDS_Shape, gp_Trsf]:
    """Return canonical origin-centered box prototype + rigid gp_Trsf for XCAF instancing."""
    h = max(0.1, z_max - z_min)
    proto = BRepPrimAPI_MakeBox(gp_Pnt(-0.5 * w, -0.5 * d, 0.0), w, d, h).Shape()

    trsf_rot = gp_Trsf()
    if abs(rot_deg) > 1e-6:
        trsf_rot.SetRotation(gp_Ax1(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), math.radians(rot_deg))
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(cx, cy, z_min))
    trsf.Multiply(trsf_rot)
    return proto, trsf


def _format_idf_part_name(ref_des: str, package_name: str, fallback: str = "") -> str:
    """Format a clean component name from IDF RefDes and Package name without duplication."""
    ref = (ref_des or "").strip()
    pkg = (package_name or "").strip()
    if ref.upper() == "NOREFDES":
        ref = ""
    if ref and pkg:
        if ref.lower() == pkg.lower() or _refdes_matches_name(ref, pkg):
            return pkg
        if _refdes_matches_name(pkg, ref):
            return ref
        return f"{ref}_{pkg}"
    return ref or pkg or fallback


def supplement_missing_idf_parts(
    idf: IdfBoardData,
    board_bbox_min: np.ndarray,
    board_bbox_max: np.ndarray,
    existing_step_parts: list[StepComponentFootprint],
    default_color: RGBColor = (0.35, 0.35, 0.38),
    exclude_small_part_mm: float = 0.0,
) -> tuple[list[PcbSimplifiedPart], int, int, dict[int, str]]:
    """Identify IDF-placed parts missing from the STEP model and generate box solids for them.

    Returns ``(added_parts, matched_count, added_count, step_idx_to_refdes_name)``.
    """
    alignment = align_idf_to_step_board(idf, board_bbox_min, board_bbox_max, existing_step_parts)
    board_z_mid = 0.5 * (alignment.board_z_bottom + alignment.board_z_top)

    added: list[PcbSimplifiedPart] = []
    matched_count = 0
    matched_names: dict[int, str] = {}

    for pl in idf.placements:
        if not pl.is_placed:
            continue
        fp = idf.lookup_footprint(pl.package_name, pl.part_number)
        wx, wy, w, d, rot, z_min, z_max, _h = _compute_idf_world_footprint(pl, fp, alignment)
        idf_aabb = _rotated_rect_aabb(wx, wy, w, d, rot)

        # Check if already present in STEP:
        # 1st priority: exact RefDes match against STEP component designator (100% definitive)
        # 2nd priority: RefDes token in STEP component name
        # 3rd priority: same-side XY spatial overlap / centroid proximity
        matched_idx: Optional[int] = None
        pl_ref_up = pl.ref_des.strip().upper()
        if pl_ref_up and pl_ref_up not in ("NOREFDES", "BOARD", "PCB"):
            for sc_idx, sc in enumerate(existing_step_parts):
                if sc.ref_des and sc.ref_des.strip().upper() == pl_ref_up:
                    matched_idx = sc_idx
                    break

        if matched_idx is None:
            best_dist = float("inf")
            for sc_idx, sc in enumerate(existing_step_parts):
                if _refdes_matches_name(pl.ref_des, sc.name):
                    matched_idx = sc_idx
                    break
                sc_center = sc.center
                sc_is_top = float(sc_center[2]) >= board_z_mid
                if sc_is_top != (pl.side != "BOTTOM"):
                    continue
                sc_aabb = (
                    float(sc.bbox_min[0]),
                    float(sc.bbox_min[1]),
                    float(sc.bbox_max[0]),
                    float(sc.bbox_max[1]),
                )
                ov = _xy_overlap_ratio(idf_aabb, sc_aabb)
                dist = math.hypot(float(sc_center[0]) - wx, float(sc_center[1]) - wy)
                if ov >= 0.30 or dist <= max(0.8, 0.35 * min(w, d)):
                    if dist < best_dist:
                        best_dist = dist
                        matched_idx = sc_idx

        if matched_idx is not None:
            matched_count += 1
            idf_name = _format_idf_part_name(pl.ref_des, pl.package_name)
            if idf_name and matched_idx not in matched_names:
                matched_names[matched_idx] = idf_name
            continue

        if _is_zero_height_non_component(pl, fp):
            continue

        ph = max(0.1, z_max - z_min)
        if exclude_small_part_mm > 0.0 and max(w, d, ph) <= exclude_small_part_mm:
            continue

        # Canonicalize (w >= d) by adjusting rotation by 90 deg so rotated identical parts share 1 prototype
        pw, pd, prot = w, d, rot
        if pd > pw:
            pw, pd = pd, pw
            prot = (prot + 90.0) % 360.0
        proto_box, trsf = build_idf_box_proto_and_trsf(wx, wy, pw, pd, prot, z_min, z_max)
        shape = proto_box.Located(TopLoc_Location(trsf))
        col_key = tuple(int(round(c * 255)) for c in default_color)
        proto_key = ("idf_box", round(pw, 2), round(pd, 2), round(ph, 2), col_key)

        ref = pl.ref_des.strip()
        pkg = pl.package_name.strip()
        comp_name = _format_idf_part_name(ref, pkg, fallback=f"IDF_Part_{len(added) + 1}")

        added.append(
            PcbSimplifiedPart(
                name=comp_name,
                shape=shape,
                color=default_color,
                category="idf_box",
                ref_des=ref if ref.upper() != "NOREFDES" else "",
                leaf_name=pkg or "IDF_BOX",
                original_bodies=0,
                original_faces=0,
                simplified_faces=6,
                solid_count=1,
                proto_key=proto_key,
                proto_shape=proto_box,
                placement_trsf=trsf,
            )
        )

    return added, matched_count, len(added), matched_names


# ---------------------------------------------------------------------------
# Standalone IDF-to-B-Rep Board & Component Builders (IDF-Only Mode)
# ---------------------------------------------------------------------------

def _count_solids(shape: TopoDS_Shape) -> int:
    cnt = 0
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        cnt += 1
        exp.Next()
    return cnt


def _arc_midpoint_2d(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    included_deg: float,
) -> Optional[tuple[float, float]]:
    """Return a point midway along an IDF circular arc from (x0, y0) to (x1, y1)."""
    chord = math.hypot(x1 - x0, y1 - y0)
    if chord < 1e-7:
        return None
    rad = math.radians(included_deg)
    sin_half = math.sin(abs(rad) * 0.5)
    if sin_half < 1e-7:
        return None
    radius = chord / (2.0 * sin_half)
    mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    dx, dy = (x1 - x0) / chord, (y1 - y0) / chord
    h = math.sqrt(max(0.0, radius * radius - (0.5 * chord) ** 2))
    sign = 1.0 if included_deg > 0 else -1.0
    if abs(included_deg) > 180.0:
        sign = -sign
    cx = mx - sign * dy * h
    cy = my + sign * dx * h
    a0 = math.atan2(y0 - cy, x0 - cx)
    a_mid = a0 + 0.5 * rad
    return (cx + radius * math.cos(a_mid), cy + radius * math.sin(a_mid))


def _clean_idf_outline_loop(
    pts: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Remove Altium/ECAD IDF outline artifacts that cause wire self-intersections:
    1. 180-degree backtracking hairline spikes between consecutive straight segments
    2. Degenerate sub-10-micron straight micro-slivers
    """
    if len(pts) < 3:
        return list(pts)
    if abs(abs(pts[1][2]) - 360.0) < 1e-3:
        return list(pts)

    segs: list[list[float]] = []
    for i in range(1, len(pts)):
        x0, y0, _ = pts[i - 1]
        x1, y1, ang = pts[i]
        if math.hypot(x1 - x0, y1 - y0) < 1e-5:
            continue
        segs.append([x0, y0, x1, y1, ang])

    if math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) > 1e-4:
        segs.append([pts[-1][0], pts[-1][1], pts[0][0], pts[0][1], 0.0])

    if len(segs) < 3:
        return list(pts)

    # 1. Collapse 180-degree backtracking spikes (and exact collinear reversals)
    changed = True
    while changed and len(segs) >= 3:
        changed = False
        n = len(segs)
        for i in range(n):
            a = segs[i]
            b = segs[(i + 1) % n]
            if abs(a[4]) <= 1e-3 and abs(b[4]) <= 1e-3:
                v1x, v1y = a[2] - a[0], a[3] - a[1]
                v2x, v2y = b[2] - b[0], b[3] - b[1]
                l1, l2 = math.hypot(v1x, v1y), math.hypot(v2x, v2y)
                if l1 > 1e-7 and l2 > 1e-7:
                    cos_t = (v1x * v2x + v1y * v2y) / (l1 * l2)
                    if cos_t < -0.95:
                        merged = [a[0], a[1], b[2], b[3], 0.0]
                        merged_len = math.hypot(merged[2] - merged[0], merged[3] - merged[1])
                        if i + 1 < n:
                            if merged_len >= 1e-5:
                                segs[i : i + 2] = [merged]
                            else:
                                del segs[i : i + 2]
                        else:
                            if merged_len >= 1e-5:
                                segs[0] = merged
                                segs.pop()
                            else:
                                segs.pop()
                                segs.pop(0)
                        changed = True
                        break

    # 2. Collapse ultra-tiny straight micro-edges (< 0.01 mm) while preserving arc endpoints
    changed = True
    while changed and len(segs) >= 3:
        changed = False
        n = len(segs)
        for i in range(n):
            a = segs[i]
            if abs(a[4]) <= 1e-3 and math.hypot(a[2] - a[0], a[3] - a[1]) < 0.01:
                prev_s = segs[(i - 1) % n]
                next_s = segs[(i + 1) % n]
                if abs(prev_s[4]) > 1e-3 and abs(next_s[4]) <= 1e-3:
                    next_s[0], next_s[1] = prev_s[2], prev_s[3]
                else:
                    prev_s[2], prev_s[3] = next_s[0], next_s[1]
                segs.pop(i)
                changed = True
                break

    if len(segs) < 2:
        return list(pts)

    cleaned: list[tuple[float, float, float]] = [(segs[0][0], segs[0][1], 0.0)]
    for s in segs:
        cleaned.append((s[2], s[3], s[4]))
    return cleaned


def _build_idf_wire_2d(
    pts: list[tuple[float, float, float]],
    z: float = 0.0,
) -> Optional[TopoDS_Wire]:
    """Convert an IDF 2D outline loop (in mm) into a closed OCCT TopoDS_Wire at elevation z."""
    if len(pts) < 2:
        return None

    # Full-circle IDF encoding: 2 points where 2nd has |angle| == 360 (1st=center, 2nd=point on rim)
    if abs(abs(pts[1][2]) - 360.0) < 1e-3:
        cx, cy, _ = pts[0]
        px, py, _ = pts[1]
        r = math.hypot(px - cx, py - cy)
        if r <= 1e-4:
            return None
        try:
            circ = gp_Circ(gp_Ax2(gp_Pnt(cx, cy, z), gp_Dir(0.0, 0.0, 1.0)), r)
            edge = BRepBuilderAPI_MakeEdge(circ).Edge()
            wb = BRepBuilderAPI_MakeWire(edge)
            if wb.IsDone():
                return wb.Wire()
        except Exception:
            return None
        return None

    pts = _clean_idf_outline_loop(pts)

    segments: list[tuple[float, float, float, float, float]] = []
    for i in range(1, len(pts)):
        x0, y0, _ = pts[i - 1]
        x1, y1, ang = pts[i]
        if math.hypot(x1 - x0, y1 - y0) < 1e-5:
            continue
        segments.append((x0, y0, x1, y1, ang))

    # Close loop automatically if final point does not repeat starting vertex
    if math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) > 1e-4:
        segments.append((pts[-1][0], pts[-1][1], pts[0][0], pts[0][1], 0.0))

    if len(segments) < 2:
        return None

    wb = BRepBuilderAPI_MakeWire()
    added_edges = 0
    for x0, y0, x1, y1, ang in segments:
        p0 = gp_Pnt(x0, y0, z)
        p1 = gp_Pnt(x1, y1, z)
        edge = None
        if 1e-3 < abs(ang) < 359.999:
            mid = _arc_midpoint_2d(x0, y0, x1, y1, ang)
            if mid is not None:
                try:
                    arc = GC_MakeArcOfCircle(p0, gp_Pnt(mid[0], mid[1], z), p1)
                    if arc.IsDone():
                        edge = BRepBuilderAPI_MakeEdge(arc.Value()).Edge()
                except Exception:
                    edge = None
        if edge is None:
            try:
                mk = BRepBuilderAPI_MakeEdge(p0, p1)
                if mk.IsDone():
                    edge = mk.Edge()
            except Exception:
                edge = None
        if edge is not None:
            wb.Add(edge)
            added_edges += 1

    if added_edges < 2 or not wb.IsDone():
        return None
    return wb.Wire()


def _extrude_idf_loop_to_solid(
    pts: list[tuple[float, float, float]],
    z_bottom: float,
    height: float,
) -> Optional[TopoDS_Shape]:
    """Build a positively oriented watertight OCCT prism solid from a 2D IDF outline loop."""
    wire = _build_idf_wire_2d(pts, z=z_bottom)
    if wire is None:
        return None
    try:
        face_mk = BRepBuilderAPI_MakeFace(wire, True)
        if not face_mk.IsDone():
            return None
        face = face_mk.Face()
        fixer = ShapeFix_Face(face)
        fixer.Perform()
        fixed_face = fixer.Face()
        if not fixed_face.IsNull():
            face = fixed_face

        # If the wire still contains subtle self-intersections, repair it via ShapeFix_Wire
        if not BRepCheck_Analyzer(face).IsValid():
            try:
                wfix = ShapeFix_Wire(wire, face, 1e-3)
                wfix.ModifyTopologyMode = True
                wfix.FixSelfIntersectionMode = True
                wfix.FixSmallMode = True
                wfix.FixConnected()
                wfix.FixClosed()
                wfix.Perform()
                fw = wfix.Wire()
                if not fw.IsNull():
                    f2 = BRepBuilderAPI_MakeFace(fw, True)
                    if f2.IsDone():
                        sf2 = ShapeFix_Face(f2.Face())
                        sf2.Perform()
                        if not sf2.Face().IsNull() and BRepCheck_Analyzer(sf2.Face()).IsValid():
                            face = sf2.Face()
            except Exception:
                pass

        if not BRepCheck_Analyzer(face).IsValid():
            return None

        prism = BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, height)).Shape()
        if prism.IsNull() or _count_solids(prism) < 1:
            return None
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(prism, props)
        vol = float(props.Mass())
        if abs(vol) <= 1e-6:
            return None
        if vol < 0.0:
            prism = prism.Reversed()
            sfix = ShapeFix_Shape(prism)
            sfix.Perform()
            if not sfix.Shape().IsNull():
                prism = sfix.Shape()
        if not BRepCheck_Analyzer(prism).IsValid():
            return None
        return prism
    except Exception:
        return None


def _loop_bbox_xy(pts: list[tuple[float, float, float]]) -> Optional[tuple[float, float, float, float]]:
    records = [(0, x, y, ang) for x, y, ang in pts]
    return _outline_points_to_bbox(records, 1.0, outer_loop_only=False)


def build_idf_board_shape(
    idf: IdfBoardData,
    *,
    z_bottom: float = 0.0,
    keep_board_holes: bool = True,
    min_board_hole_mm: float = 0.8,
) -> tuple[Optional[TopoDS_Shape], float, float, int, int]:
    """Construct a watertight PCB substrate B-Rep solid directly from IDF board outline & drilled holes.

    Returns ``(board_shape, z_bottom, z_top, kept_holes_count, skipped_holes_count)``.
    """
    thickness = idf.board_thickness_mm if idf.board_thickness_mm > 0.05 else 1.6
    z_top = z_bottom + thickness

    outer_loop_id: Optional[int] = None
    board_solid: Optional[TopoDS_Shape] = None

    if idf.board_outline_loops:
        if 0 in idf.board_outline_loops:
            outer_loop_id = 0
        else:
            best_area = -1.0
            for lid, pts in idf.board_outline_loops.items():
                bb = _loop_bbox_xy(pts)
                if bb is not None:
                    area = (bb[2] - bb[0]) * (bb[3] - bb[1])
                    if area > best_area:
                        best_area = area
                        outer_loop_id = lid
        if outer_loop_id is not None:
            board_solid = _extrude_idf_loop_to_solid(
                idf.board_outline_loops[outer_loop_id], z_bottom, thickness
            )

    if board_solid is None and idf.board_bbox_xy is not None:
        x0, y0, x1, y1 = idf.board_bbox_xy
        pad = 2.0 if not idf.board_outline_loops else 0.0
        w = max(1.0, (x1 - x0) + 2.0 * pad)
        d = max(1.0, (y1 - y0) + 2.0 * pad)
        board_solid = BRepPrimAPI_MakeBox(gp_Pnt(x0 - pad, y0 - pad, z_bottom), w, d, thickness).Shape()

    if board_solid is None:
        return None, z_bottom, z_top, 0, 0

    kept_holes = 0
    skipped_holes = 0
    cut_tools: list[TopoDS_Shape] = []
    cut_z0 = z_bottom - 0.5
    cut_h = thickness + 1.0
    hole_thr = max(0.0, float(min_board_hole_mm))

    # 1. Internal cutout loops in .BOARD_OUTLINE (loop_id != outer_loop_id)
    for lid, pts in idf.board_outline_loops.items():
        if lid == outer_loop_id:
            continue
        if not keep_board_holes:
            skipped_holes += 1
            continue
        bb = _loop_bbox_xy(pts)
        if bb is not None and hole_thr > 0.0:
            cw = bb[2] - bb[0]
            cd = bb[3] - bb[1]
            if max(cw, cd) < hole_thr:
                skipped_holes += 1
                continue
        cutout = _extrude_idf_loop_to_solid(pts, cut_z0, cut_h)
        if cutout is not None:
            cut_tools.append(cutout)
            kept_holes += 1

    # 2. Drilled holes from .DRILLED_HOLES
    for hole in idf.drilled_holes:
        if not keep_board_holes:
            skipped_holes += 1
            continue
        if hole_thr > 0.0 and hole.diameter_mm < hole_thr:
            skipped_holes += 1
            continue
        r = 0.5 * hole.diameter_mm
        if r <= 0.02:
            skipped_holes += 1
            continue
        try:
            cyl = BRepPrimAPI_MakeCylinder(
                gp_Ax2(gp_Pnt(hole.x_mm, hole.y_mm, cut_z0), gp_Dir(0.0, 0.0, 1.0)),
                r,
                cut_h,
            ).Shape()
            cut_tools.append(cyl)
            kept_holes += 1
        except Exception:
            skipped_holes += 1

    if cut_tools:
        batch_ok = False
        try:
            cutter = BRepAlgoAPI_Cut()
            cutter.SetRunParallel(True)
            cutter.SetNonDestructive(True)
            args = ShapeList()
            args.Append(board_solid)
            tools = ShapeList()
            for t_sh in cut_tools:
                tools.Append(t_sh)
            cutter.SetArguments(args)
            cutter.SetTools(tools)
            cutter.Build()
            if cutter.IsDone():
                res = cutter.Shape()
                if not res.IsNull() and _count_solids(res) >= 1 and BRepCheck_Analyzer(res).IsValid():
                    board_solid = res
                    batch_ok = True
        except Exception:
            batch_ok = False

        if not batch_ok:
            applied = 0
            for t_sh in cut_tools:
                try:
                    c_one = BRepAlgoAPI_Cut(board_solid, t_sh)
                    c_one.Build()
                    if c_one.IsDone():
                        res = c_one.Shape()
                        if not res.IsNull() and _count_solids(res) >= 1 and BRepCheck_Analyzer(res).IsValid():
                            board_solid = res
                            applied += 1
                except Exception:
                    continue
            skipped_holes += max(0, kept_holes - applied)
            kept_holes = applied

    return board_solid, z_bottom, z_top, kept_holes, skipped_holes


def build_idf_cylinder_proto_and_trsf(
    cx: float,
    cy: float,
    radius: float,
    z_min: float,
    z_max: float,
) -> tuple[TopoDS_Shape, gp_Trsf]:
    """Return canonical origin-centered cylinder prototype + rigid gp_Trsf for XCAF instancing."""
    r = max(0.1, float(radius))
    h = max(0.1, float(z_max - z_min))
    proto = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), r, h
    ).Shape()
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(cx, cy, z_min))
    return proto, trsf


def build_idf_components(
    idf: IdfBoardData,
    alignment: IdfAlignment,
    *,
    default_color: Optional[RGBColor] = (0.35, 0.35, 0.38),
    exclude_small_part_mm: float = 0.0,
    detect_cylinders: bool = True,
) -> tuple[list[PcbSimplifiedPart], int, int]:
    """Generate standalone simplified 3D solids for all placed components in an IDF dataset.

    Returns ``(parts, cylinder_count, excluded_small_count)``.
    """
    parts: list[PcbSimplifiedPart] = []
    cylinder_count = 0
    excluded_small_count = 0
    exclude_thr = max(0.0, float(exclude_small_part_mm))
    col_key = tuple(int(round(c * 255)) for c in default_color) if default_color is not None else None

    for pl in idf.placements:
        if not pl.is_placed:
            continue
        fp = idf.lookup_footprint(pl.package_name, pl.part_number)
        if _is_zero_height_non_component(pl, fp):
            continue

        wx, wy, w, d, rot, z_min, z_max, _h = _compute_idf_world_footprint(pl, fp, alignment)
        ph = max(0.1, z_max - z_min)
        if exclude_thr > 0.0 and max(w, d, ph) <= exclude_thr + 1e-6:
            excluded_small_count += 1
            continue

        ref = pl.ref_des.strip()
        pkg = pl.package_name.strip()
        comp_name = _format_idf_part_name(ref, pkg, fallback=f"IDF_Part_{len(parts) + 1}")

        if detect_cylinders and fp is not None and fp.is_circle and fp.circle_radius_mm > 0.05:
            r = fp.circle_radius_mm
            proto_cyl, trsf = build_idf_cylinder_proto_and_trsf(wx, wy, r, z_min, z_max)
            shape = proto_cyl.Located(TopLoc_Location(trsf))
            proto_key = ("idf_cyl", round(r, 2), round(ph, 2), col_key)
            cylinder_count += 1
            parts.append(
                PcbSimplifiedPart(
                    name=comp_name,
                    shape=shape,
                    color=default_color,
                    category="cylinder",
                    ref_des=ref if ref.upper() != "NOREFDES" else "",
                    leaf_name=pkg or "IDF_CYL",
                    original_bodies=0,
                    original_faces=0,
                    simplified_faces=3,
                    solid_count=1,
                    proto_key=proto_key,
                    proto_shape=proto_cyl,
                    placement_trsf=trsf,
                )
            )
            continue

        pw, pd, prot = w, d, rot
        if pd > pw:
            pw, pd = pd, pw
            prot = (prot + 90.0) % 360.0
        proto_box, trsf = build_idf_box_proto_and_trsf(wx, wy, pw, pd, prot, z_min, z_max)
        shape = proto_box.Located(TopLoc_Location(trsf))
        proto_key = ("idf_box", round(pw, 2), round(pd, 2), round(ph, 2), col_key)
        parts.append(
            PcbSimplifiedPart(
                name=comp_name,
                shape=shape,
                color=default_color,
                category="idf_box",
                ref_des=ref if ref.upper() != "NOREFDES" else "",
                leaf_name=pkg or "IDF_BOX",
                original_bodies=0,
                original_faces=0,
                simplified_faces=6,
                solid_count=1,
                proto_key=proto_key,
                proto_shape=proto_box,
                placement_trsf=trsf,
            )
        )

    return parts, cylinder_count, excluded_small_count
