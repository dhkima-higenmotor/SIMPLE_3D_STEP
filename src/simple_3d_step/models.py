"""Data models for STEP assembly parsing, visibility analysis, and exact B-Rep simplification."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np


class PartStatus(str, Enum):
    """Classification status of a part in the assembly."""

    UNKNOWN = "Unanalyzed"
    EXTERNAL = "External (Keep)"
    INTERNAL = "Internal (Drop)"


ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]


@dataclass
class PartNode:
    """Represents a single leaf solid/shell instance extracted from a STEP assembly."""

    part_id: int
    name: str
    assembly_path: str
    shape: Any  # OCP.TopoDS.TopoDS_Shape (with global placement applied)
    solid_count: int = 1
    face_count: int = 0
    volume: float = 0.0
    surface_area: float = 0.0
    bbox_min: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    bbox_max: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))

    # Visibility analysis metrics
    ray_hits: int = 0
    visible_face_count: int = 0
    visible_area_ratio: float = 0.0
    escape_ratio: float = 0.0
    visibility_score: float = 0.0  # 0.0 ~ 1.0

    # Classification & user override
    auto_status: PartStatus = PartStatus.UNKNOWN
    keep: bool = True
    user_overridden: bool = False

    @property
    def bbox_size(self) -> np.ndarray:
        return self.bbox_max - self.bbox_min

    @property
    def bbox_diagonal(self) -> float:
        return float(np.linalg.norm(self.bbox_size))

    @property
    def effective_status_label(self) -> str:
        if self.user_overridden:
            return "Keep (Manual)" if self.keep else "Drop (Manual)"
        if self.auto_status == PartStatus.UNKNOWN:
            return "Keep" if self.keep else "Drop"
        return self.auto_status.value


@dataclass
class AssemblyModel:
    """Loaded STEP assembly containing normalized leaf parts and global metadata."""

    source_path: Path
    parts: list[PartNode] = field(default_factory=list)
    unit: str = "MM"
    bbox_min: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    bbox_max: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    raw_shape: Any = None  # Full TopoDS_Shape

    @property
    def bbox_diagonal(self) -> float:
        return float(np.linalg.norm(self.bbox_max - self.bbox_min))

    @property
    def total_volume(self) -> float:
        return float(sum(max(p.volume, 0.0) for p in self.parts))


@dataclass
class VisibilityConfig:
    """Parameters controlling 2-Pass Embree ray-casting visibility analysis."""

    num_views: int = 64                 # Fibonacci sphere viewpoints (Fast:26, Standard:64, Fine:162)
    grid_resolution: int = 160          # Orthographic ray grid resolution per viewpoint (R x R)
    linear_deflection_ratio: float = 0.0025  # Tessellation linear deflection ratio vs bbox diagonal
    angular_deflection: float = 0.5     # Tessellation angular deflection (rad)
    enable_pass2_verify: bool = True    # Reverse inside-out hemisphere verification for borderline/small parts
    pass2_samples_per_part: int = 48    # Pass 2 surface sample points per part
    pass2_rays_per_sample: int = 32     # Pass 2 hemisphere rays per sample point
    min_visible_ratio: float = 0.005    # Minimum exterior visibility threshold (0.005 = 0.5%)
    min_part_volume_mm3: float = 0.0    # Auto-drop parts below this volume in mm^3 (0 = disabled)


@dataclass
class SimplifyConfig:
    """Parameters controlling 100% exterior-preserving exact B-Rep solidification."""

    fill_internal_core: bool = True     # Enable exact analytic B-Rep internal core filling
    core_bridge_gap_mm: float = 3.5     # Bridge gap tolerance between outer shells/covers/rotors (mm)
    core_inset_mm: float = 2.0          # Minimum inward safety inset protecting exterior B-Rep surfaces (mm)
    core_grid_resolution: int = 120     # Auxiliary voxel grid resolution for cavity probing (never exported)
    connect_floating_parts: bool = True # Connect tolerance-gapped exterior parts via hidden internal pins
    max_island_gap_mm: float = 3.0      # Maximum gap for internal pin bridges (mm)
    remove_small_holes: bool = False    # Plug small cylindrical through-holes (OFF by default to preserve exterior)
    max_hole_diameter: float = 10.0     # Maximum hole diameter when remove_small_holes is enabled (mm)
    unify_same_domain: bool = True      # Merge coplanar/co-surface faces (ShapeUpgrade_UnifySameDomain)
    keep_largest_only: bool = True      # Keep only the largest solid if tiny unconnectable islands remain
    step_schema: str = "AP214"          # Output STEP schema (AP214 / AP242 / AP203)


@dataclass
class ValidationReport:
    """Geometric and topological validation report of a simplified B-Rep shape."""

    is_valid: bool
    solid_count: int
    shell_count: int
    face_count: int
    free_edge_count: int
    is_watertight: bool
    volume: float
    surface_area: float
    bbox_size: tuple[float, float, float]
    face_types: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SimplifyResult:
    """Result returned after running the exact B-Rep simplification pipeline."""

    shape: Any  # Final OCP TopoDS_Shape
    mode_used: str
    validation: ValidationReport
    kept_part_count: int
    removed_part_count: int
    original_volume: float
    filled_volume_delta: float
    elapsed_seconds: float
    logs: list[str] = field(default_factory=list)
    output_path: Optional[Path] = None
