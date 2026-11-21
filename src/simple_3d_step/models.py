"""Data models for STEP assembly parsing, visibility analysis, and exact B-Rep simplification."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np


class PartStatus(str, Enum):
    """Classification status of a part in the assembly."""

    UNKNOWN = "미분석"
    EXTERNAL = "외부(유지)"
    INTERNAL = "내부(삭제)"


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
            return "유지(수동)" if self.keep else "삭제(수동)"
        if self.auto_status == PartStatus.UNKNOWN:
            return "유지" if self.keep else "삭제"
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

    num_views: int = 64                 # Fibonacci 구면 시점 수 (빠름:26, 표준:64, 정밀:162)
    grid_resolution: int = 160          # 시점당 직교 광선 격자 해상도 (R x R)
    linear_deflection_ratio: float = 0.0025  # 전체 대각선 대비 테셀레이션 편차 비율
    angular_deflection: float = 0.5     # 테셀레이션 각도 편차 (rad)
    enable_pass2_verify: bool = True    # 경계/소형 부품 내부→외부 역방향 검증
    pass2_samples_per_part: int = 48    # Pass 2 부품당 표면 샘플 수
    pass2_rays_per_sample: int = 32     # Pass 2 샘플당 반구 광선 수
    min_visible_ratio: float = 0.005    # 최소 가시 비율 임계값 (0.005 = 0.5%)
    min_part_volume_mm3: float = 0.0    # 이 부피 이하 소형 부품 자동 삭제 (0 = 비활성)


@dataclass
class SimplifyConfig:
    """Parameters controlling 100% exterior-preserving exact B-Rep solidification."""

    fill_internal_core: bool = True     # 외벽 안쪽 해석적 B-Rep 내부 코어 채움 활성화
    core_bridge_gap_mm: float = 3.5     # 회전부·커버 등 외관 부품 간 내부 브리지 허용 간극 (mm)
    core_inset_mm: float = 2.0          # 외관 표면 보호 최소 안전 여백 (코어는 외피에서 이 거리 안쪽에만 생성)
    core_grid_resolution: int = 120     # 내부 공동 탐색용 보조 격자 해상도 (외관 형상에는 미사용)
    connect_floating_parts: bool = True # 미세 공차로 떠 있는 외관 부품(커넥터/볼트 등) 내부 핀 연결
    max_island_gap_mm: float = 3.0      # 분리 외관 부품 내부 연결 허용 최대 간격 (mm)
    remove_small_holes: bool = False    # 소구경 원통형 관통홀 자동 막기 (기본 OFF: 외관 홀 원본 보존)
    max_hole_diameter: float = 10.0     # 소구경 홀 막기 기준 직경 (mm)
    unify_same_domain: bool = True      # 동일 평면/곡면 통합 (ShapeUpgrade_UnifySameDomain)
    keep_largest_only: bool = True      # 연결 불가한 극소 분리체 잔존 시 최대 단일 본체만 유지
    step_schema: str = "AP214"          # 출력 STEP 스키마 (AP214 / AP242 / AP203)


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
