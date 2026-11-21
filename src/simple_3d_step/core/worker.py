"""Dedicated background CAD worker process to prevent Python GIL / OCCT GUI freezes.

OpenCASCADE C++ calls via OCP (STEP ReadFile, Boolean Fuse, UnifySameDomain, STEP Write)
hold the Python GIL for seconds at a time. Running all OCCT/Embree operations inside a
dedicated persistent subprocess guarantees the Tkinter GUI process remains 100% responsive
at all times with zero 'Not Responding' freezes.
"""

from __future__ import annotations

import copy
import multiprocessing
import queue
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from simple_3d_step.models import (
    AssemblyModel,
    PartNode,
    SimplifyConfig,
    SimplifyResult,
    VisibilityConfig,
)


def _strip_part_shape(part: PartNode) -> PartNode:
    """Create a lightweight picklable copy of PartNode with shape=None for IPC."""
    return PartNode(
        part_id=part.part_id,
        name=part.name,
        assembly_path=part.assembly_path,
        shape=None,
        solid_count=part.solid_count,
        face_count=part.face_count,
        volume=part.volume,
        surface_area=part.surface_area,
        bbox_min=part.bbox_min.copy(),
        bbox_max=part.bbox_max.copy(),
        ray_hits=part.ray_hits,
        visible_face_count=part.visible_face_count,
        visible_area_ratio=part.visible_area_ratio,
        escape_ratio=part.escape_ratio,
        visibility_score=part.visibility_score,
        auto_status=part.auto_status,
        keep=part.keep,
        user_overridden=part.user_overridden,
    )


def _strip_simplify_result(res: SimplifyResult) -> SimplifyResult:
    """Create a picklable SimplifyResult summary with shape=None for IPC."""
    return SimplifyResult(
        shape=None,
        mode_used=res.mode_used,
        validation=copy.deepcopy(res.validation),
        kept_part_count=res.kept_part_count,
        removed_part_count=res.removed_part_count,
        original_volume=res.original_volume,
        filled_volume_delta=res.filled_volume_delta,
        elapsed_seconds=res.elapsed_seconds,
        logs=list(res.logs),
        output_path=res.output_path,
    )


def _apply_overrides(
    assembly: AssemblyModel,
    overrides: Optional[dict[int, tuple[bool, bool]]],
) -> None:
    if not overrides:
        return
    for p in assembly.parts:
        ov = overrides.get(p.part_id)
        if ov is not None:
            p.keep, p.user_overridden = ov


def cad_worker_main(
    cmd_queue: multiprocessing.Queue,
    evt_queue: multiprocessing.Queue,
    cancel_event: Any,
) -> None:
    """Persistent CAD worker process loop holding OCCT shapes in local RAM."""
    # Import heavy CAD modules only inside the worker process
    from simple_3d_step.core.solidifier import simplify_assembly_to_single_solid
    from simple_3d_step.core.step_io import (
        load_step_assembly,
        make_compound_from_parts,
        save_shape_to_step,
    )
    from simple_3d_step.core.visibility import analyze_visibility

    assembly: Optional[AssemblyModel] = None
    tessellated = None
    result: Optional[SimplifyResult] = None

    evt_queue.put(("worker_ready", None))

    while True:
        try:
            msg = cmd_queue.get()
        except (EOFError, KeyboardInterrupt):
            break

        if msg is None:
            break

        cmd_id, cmd_name, payload = msg
        if cmd_name == "shutdown":
            break

        cancel_event.clear()

        def _progress(frac: float, text: str) -> None:
            evt_queue.put(("progress", (cmd_id, float(frac), str(text))))

        def _is_cancelled() -> bool:
            return bool(cancel_event.is_set())

        try:
            if cmd_name == "load_step":
                path = Path(payload["path"])
                assembly = load_step_assembly(
                    path, progress_cb=_progress, cancel_check=_is_cancelled
                )
                tessellated = None
                result = None
                evt_queue.put(
                    (
                        "done",
                        (
                            cmd_id,
                            "load_step",
                            {
                                "source_path": str(assembly.source_path),
                                "unit": assembly.unit,
                                "bbox_min": assembly.bbox_min.copy(),
                                "bbox_max": assembly.bbox_max.copy(),
                                "total_volume": assembly.total_volume,
                                "parts": [_strip_part_shape(p) for p in assembly.parts],
                            },
                        ),
                    )
                )

            elif cmd_name == "analyze_visibility":
                if assembly is None:
                    raise RuntimeError("Please open a STEP file first.")
                _apply_overrides(assembly, payload.get("overrides"))
                vis_cfg: VisibilityConfig = payload["config"]
                vis_res = analyze_visibility(
                    assembly,
                    vis_cfg,
                    tessellated=tessellated,
                    progress_cb=_progress,
                    cancel_check=_is_cancelled,
                )
                tessellated = vis_res.tessellated
                evt_queue.put(
                    (
                        "done",
                        (
                            cmd_id,
                            "analyze_visibility",
                            {
                                "parts": [_strip_part_shape(p) for p in assembly.parts],
                                "external_count": vis_res.external_count,
                                "internal_count": vis_res.internal_count,
                                "tri_count": len(vis_res.tessellated.faces),
                            },
                        ),
                    )
                )

            elif cmd_name == "simplify":
                if assembly is None:
                    raise RuntimeError("Please open a STEP file first.")
                _apply_overrides(assembly, payload.get("overrides"))
                simp_cfg: SimplifyConfig = payload["config"]
                result = simplify_assembly_to_single_solid(
                    assembly,
                    simp_cfg,
                    progress_cb=_progress,
                    cancel_check=_is_cancelled,
                )
                evt_queue.put(
                    (
                        "done",
                        (cmd_id, "simplify", _strip_simplify_result(result)),
                    )
                )

            elif cmd_name == "save_result":
                if result is None or result.shape is None or assembly is None:
                    raise RuntimeError("No simplification result available to save.")
                out_path = Path(payload["output_path"])
                schema = str(payload.get("schema", "AP214"))
                _progress(0.25, f"Saving STEP file: {out_path.name}")
                saved = save_shape_to_step(
                    result.shape, out_path, schema=schema, unit=assembly.unit
                )
                result.output_path = saved
                _progress(1.0, "Save complete")
                evt_queue.put(
                    (
                        "done",
                        (
                            cmd_id,
                            "save_result",
                            {
                                "output_path": str(saved),
                                "size_bytes": saved.stat().st_size,
                            },
                        ),
                    )
                )

            elif cmd_name == "export_kept_preview":
                if assembly is None:
                    raise RuntimeError("No STEP file is loaded.")
                _apply_overrides(assembly, payload.get("overrides"))
                kept = [p for p in assembly.parts if p.keep]
                if not kept:
                    raise ValueError("No kept parts are selected.")
                out_path = Path(payload["output_path"])
                schema = str(payload.get("schema", "AP214"))
                _progress(0.3, f"Exporting temporary preview STEP for {len(kept)} kept parts...")
                comp = make_compound_from_parts(kept)
                saved = save_shape_to_step(comp, out_path, schema=schema, unit=assembly.unit)
                _progress(1.0, "Temporary preview STEP ready")
                evt_queue.put(
                    (
                        "done",
                        (cmd_id, "export_kept_preview", {"output_path": str(saved)}),
                    )
                )

            elif cmd_name == "export_result_preview":
                if result is None or result.shape is None or assembly is None:
                    raise RuntimeError("No simplification result available to preview.")
                if result.output_path and result.output_path.exists():
                    saved = result.output_path
                else:
                    out_path = Path(payload["output_path"])
                    schema = str(payload.get("schema", "AP214"))
                    _progress(0.35, "Exporting temporary result preview STEP...")
                    saved = save_shape_to_step(
                        result.shape, out_path, schema=schema, unit=assembly.unit
                    )
                _progress(1.0, "Temporary preview STEP ready")
                evt_queue.put(
                    (
                        "done",
                        (cmd_id, "export_result_preview", {"output_path": str(saved)}),
                    )
                )

            else:
                raise ValueError(f"Unknown worker command: {cmd_name}")

        except InterruptedError as exc:
            evt_queue.put(("cancelled", (cmd_id, cmd_name, str(exc))))
        except Exception as exc:
            detail = f"{exc}\n\n{traceback.format_exc(limit=4)}"
            evt_queue.put(("error", (cmd_id, cmd_name, str(exc), detail)))


@dataclass
class WorkerEvent:
    """Message received from the background CAD worker process."""

    kind: str  # "worker_ready" | "progress" | "done" | "cancelled" | "error" | "crashed"
    payload: Any


class CadWorkerClient:
    """Manages lifecycle, IPC queues, and cancellation of the CAD subprocess."""

    def __init__(self) -> None:
        self._ctx = multiprocessing.get_context("spawn")
        self._cmd_queue: Optional[multiprocessing.Queue] = None
        self._evt_queue: Optional[multiprocessing.Queue] = None
        self._cancel_event: Any = None
        self._proc: Optional[multiprocessing.Process] = None
        self._cmd_seq: int = 0
        self._active_cmd_id: Optional[int] = None
        self._active_cmd_name: str = ""
        self._ready: bool = False
        self.start()

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    @property
    def is_ready(self) -> bool:
        return self._ready and self.is_alive

    def start(self) -> None:
        """Spawn a fresh persistent CAD worker process."""
        if self.is_alive:
            return
        self._cmd_queue = self._ctx.Queue()
        self._evt_queue = self._ctx.Queue()
        self._cancel_event = self._ctx.Event()
        self._ready = False
        self._proc = self._ctx.Process(
            target=cad_worker_main,
            args=(self._cmd_queue, self._evt_queue, self._cancel_event),
            name="Simple3DStepCadWorker",
            daemon=True,
        )
        self._proc.start()

    def submit(self, cmd_name: str, payload: dict[str, Any]) -> int:
        """Send a command to the background CAD worker process."""
        if not self.is_alive:
            self.start()
        assert self._cmd_queue is not None
        self._cmd_seq += 1
        cmd_id = self._cmd_seq
        self._active_cmd_id = cmd_id
        self._active_cmd_name = cmd_name
        self._cmd_queue.put((cmd_id, cmd_name, payload))
        return cmd_id

    def request_cancel(self) -> None:
        """Request graceful cancellation of the currently running task."""
        if self._cancel_event is not None:
            self._cancel_event.set()

    def force_terminate_and_restart(self) -> None:
        """Immediately kill a C++-blocked worker process and spawn a clean replacement."""
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=3.0)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=2.0)
        self._proc = None
        self._active_cmd_id = None
        self._active_cmd_name = ""
        self._ready = False
        self.start()

    def poll_events(self) -> list[WorkerEvent]:
        """Drain all pending events from the worker process without blocking."""
        events: list[WorkerEvent] = []
        if self._evt_queue is not None:
            while True:
                try:
                    kind, payload = self._evt_queue.get_nowait()
                except (queue.Empty, EOFError, OSError):
                    break
                if kind == "worker_ready":
                    self._ready = True
                elif kind in ("done", "cancelled", "error"):
                    if payload and payload[0] == self._active_cmd_id:
                        self._active_cmd_id = None
                        self._active_cmd_name = ""
                events.append(WorkerEvent(kind=kind, payload=payload))

        # Detect unexpected worker crash while a command was active
        if self._active_cmd_id is not None and not self.is_alive:
            crashed_id = self._active_cmd_id
            crashed_name = self._active_cmd_name
            self._active_cmd_id = None
            self._active_cmd_name = ""
            events.append(
                WorkerEvent(
                    kind="crashed",
                    payload=(crashed_id, crashed_name, "Background CAD worker terminated unexpectedly."),
                )
            )
            self.start()

        return events

    def shutdown(self) -> None:
        """Cleanly terminate the background worker process on app exit."""
        if self._proc is None:
            return
        try:
            if self._cancel_event is not None:
                self._cancel_event.set()
            if self._cmd_queue is not None and self._proc.is_alive():
                self._cmd_queue.put((0, "shutdown", {}))
            self._proc.join(timeout=1.5)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=1.5)
        except Exception:
            pass
        finally:
            self._proc = None
