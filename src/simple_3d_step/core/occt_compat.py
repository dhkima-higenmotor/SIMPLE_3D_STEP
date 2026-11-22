"""Compatibility helpers and multi-core runtime configuration across OCCT 7.x and 8.x (OCP)."""

from __future__ import annotations

import io
import os
from typing import Optional

from OCP.BinTools import BinTools
from OCP.BOPAlgo import BOPAlgo_Options
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.OSD import OSD_ThreadPool
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
        IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeIndexedDataMapOfList,
        IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeIndexedMap,
        List_TopoDS_Shape as ShapeList,
        Sequence_TDF_Label as LabelSequence,
    )
except ImportError:
    from OCP.TDF import TDF_LabelSequence as LabelSequence  # type: ignore[no-redef]
    from OCP.TopTools import (  # type: ignore[no-redef]
        TopTools_HSequenceOfShape as ShapeHSequence,
        TopTools_IndexedDataMapOfShapeListOfShape as ShapeIndexedDataMapOfList,
        TopTools_IndexedMapOfShape as ShapeIndexedMap,
        TopTools_ListOfShape as ShapeList,
    )


_CONFIGURED_MAX_THREADS: Optional[int] = None
_PARALLEL_DEFAULTS_ENABLED: bool = False


def ensure_occt_parallelism_enabled() -> None:
    """Enable OCCT global C++ parallel defaults once without altering thread pool size."""
    global _PARALLEL_DEFAULTS_ENABLED
    if _PARALLEL_DEFAULTS_ENABLED:
        return
    try:
        BOPAlgo_Options.SetParallelMode_s(True)
    except Exception:
        pass
    try:
        BRepMesh_IncrementalMesh.SetParallelDefault_s(True)
    except Exception:
        pass
    _PARALLEL_DEFAULTS_ENABLED = True


def configure_occt_parallelism(max_threads: Optional[int] = None) -> int:
    """Enable OCCT native C++ parallel defaults and optionally cap OSD_ThreadPool size.

    Call once per process (especially when spawning multiple batch workers) so each
    process stays within its CPU core budget without thread oversubscription.
    """
    global _CONFIGURED_MAX_THREADS
    cpu_cnt = max(1, os.cpu_count() or 1)
    target = cpu_cnt if max_threads is None else max(1, min(int(max_threads), cpu_cnt))
    _CONFIGURED_MAX_THREADS = target
    ensure_occt_parallelism_enabled()

    if max_threads is not None:
        try:
            pool = OSD_ThreadPool.DefaultPool_s()
            if pool is not None:
                pool.Init(target)
                if hasattr(pool, "SetNbDefaultThreadsToLaunch"):
                    pool.SetNbDefaultThreadsToLaunch(target)
        except Exception:
            pass
    return target


def default_ray_threads(max_threads: Optional[int] = None) -> int:
    """Return optimal worker thread count for nogil Embree ray-casting queries.

    Caps at 12 threads per process because shared-BVH memory bandwidth saturates
    around 8-12 threads while keeping thread-pool overhead negligible.
    """
    cpu_cnt = max(1, os.cpu_count() or 1)
    budget = _CONFIGURED_MAX_THREADS if _CONFIGURED_MAX_THREADS is not None else cpu_cnt
    if max_threads is not None:
        budget = min(budget, max(1, int(max_threads)))
    return max(1, min(budget, 12))


def make_brep_check_analyzer(
    shape: TopoDS_Shape,
    geom_controls: bool = True,
    parallel: bool = True,
) -> BRepCheck_Analyzer:
    """Construct a BRepCheck_Analyzer with C++ multithreading enabled when supported."""
    if parallel:
        try:
            return BRepCheck_Analyzer(shape, geom_controls, True)
        except TypeError:
            pass
    return BRepCheck_Analyzer(shape, geom_controls)


def shape_to_bytes(shape: TopoDS_Shape) -> bytes:
    """Serialize a TopoDS_Shape losslessly to OCCT binary B-Rep bytes (for IPC)."""
    buf = io.BytesIO()
    BinTools.Write_s(shape, buf)
    return buf.getvalue()


def bytes_to_shape(data: bytes) -> TopoDS_Shape:
    """Deserialize OCCT binary B-Rep bytes back into a TopoDS_Shape."""
    shape = TopoDS_Shape()
    BinTools.Read_s(shape, io.BytesIO(data))
    return shape


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
    "ShapeIndexedDataMapOfList",
    "ShapeIndexedMap",
    "ShapeList",
    "bytes_to_shape",
    "configure_occt_parallelism",
    "default_ray_threads",
    "ensure_occt_parallelism_enabled",
    "make_brep_check_analyzer",
    "shape_to_bytes",
    "to_edge",
    "to_face",
    "to_shell",
    "to_solid",
    "to_wire",
]
