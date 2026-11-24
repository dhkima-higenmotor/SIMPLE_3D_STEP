"""Batch mode panel embedded in the unified SIMPLE 3D STEP notebook GUI.

The panel is intentionally simple: add many ``.step`` / ``.stp`` files, press
[RUN], and every queued file is pushed through the exact same pipeline as the
single-file workflow (load -> visibility analysis -> exact B-Rep
solidification -> save).  All parameters are read from ``config.json`` on every
RUN, and each resulting ``<name>_simplified.step`` is written next to its source
file.

Heavy OCCT/Embree work runs inside a persistent background CAD worker process
(created lazily the first time the tab is opened or RUN is pressed), so the GUI
stays responsive and a batch can be stopped at any time.
"""

from __future__ import annotations

import os
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Iterable, Optional

from simple_3d_step.config import (
    AppConfig,
    get_default_config_path,
    launch_external_viewer,
    load_config,
    resolve_batch_workers,
    threads_per_batch_worker,
)
from simple_3d_step.core.worker import CadWorkerClient
from simple_3d_step.gui.log_panel import LogPanel
from simple_3d_step.gui.theme import COLORS, FONTS
from simple_3d_step.models import SimplifyConfig, VisibilityConfig


STEP_SUFFIXES = {".step", ".stp"}

# Status values shown in the queue table
ST_PENDING = "Pending"
ST_RUNNING = "Running"
ST_DONE = "Done"
ST_FAILED = "Failed"
ST_CANCELLED = "Cancelled"
ST_SKIPPED = "Skipped"


def default_output_path(source: Path) -> Path:
    """Return the simplified STEP path in the *same directory* as the source file."""
    source = Path(source)
    return source.with_name(f"{source.stem}_simplified.step")


def build_visibility_config(cfg: AppConfig) -> VisibilityConfig:
    """Map ``config.json`` values onto the visibility analysis parameters."""
    return VisibilityConfig(
        num_views=max(12, int(cfg.num_views)),
        grid_resolution=max(48, int(cfg.grid_resolution)),
        enable_pass2_verify=bool(cfg.enable_pass2_verify),
        min_visible_ratio=max(0.0, float(cfg.min_visible_ratio_pct) / 100.0),
    )


def build_simplify_config(cfg: AppConfig) -> SimplifyConfig:
    """Map ``config.json`` values onto the exact B-Rep solidification parameters."""
    return SimplifyConfig(
        fill_internal_core=bool(cfg.fill_internal_core),
        core_bridge_gap_mm=max(0.0, float(cfg.core_bridge_gap_mm)),
        core_inset_mm=max(0.5, float(cfg.core_inset_mm)),
        connect_floating_parts=bool(cfg.connect_floating_parts),
        max_island_gap_mm=max(0.0, float(cfg.max_island_gap_mm)),
        remove_small_holes=bool(cfg.remove_small_holes),
        max_hole_diameter=max(0.0, float(cfg.max_hole_diameter)),
        unify_same_domain=bool(cfg.unify_same_domain),
        keep_largest_only=bool(cfg.keep_largest_only),
        step_schema=cfg.step_schema or "AP214",
    )


@dataclass
class BatchItem:
    """One queued STEP file and its processing state."""

    path: Path
    status: str = ST_PENDING
    output_path: Optional[Path] = None


@dataclass
class _WorkerSlot:
    """Tracks one persistent CAD subprocess and its currently assigned batch item."""

    slot_id: int
    client: CadWorkerClient
    item_idx: Optional[int] = None
    cmd_id: Optional[int] = None
    stage: str = ""
    file_progress: float = 0.0


class BatchPanel(tk.Frame):
    """Multi-worker parallel multi-file STEP simplifier driven by ``config.json``."""

    # Relative progress weight of every pipeline stage within a single file
    _STAGE_RANGES = {
        "load_step": (0.00, 0.12),
        "analyze_visibility": (0.12, 0.42),
        "simplify": (0.42, 0.90),
        "save_result": (0.90, 1.00),
    }
    _STAGE_LABELS = {
        "load_step": "Loading",
        "analyze_visibility": "Analyzing internal parts",
        "simplify": "Converting to solid",
        "save_result": "Saving",
    }

    def __init__(
        self,
        parent: tk.Misc,
        config_path: Optional[Path] = None,
        initial_files: Optional[Iterable[Path]] = None,
    ) -> None:
        super().__init__(parent, background=COLORS["app_bg"])

        # config.json is re-read on every RUN; the batch panel never writes it
        self._config_path = Path(config_path) if config_path else get_default_config_path()
        self._config: AppConfig = load_config(self._config_path)

        self._items: list[BatchItem] = []
        self._slots: list[_WorkerSlot] = []
        self._worker: Optional[CadWorkerClient] = None  # Primary worker slot (slots[0])
        self._poll_after_id: Optional[str] = None

        # Batch orchestration state
        self._running = False
        self._stop_requested = False
        self._queue: list[int] = []
        self._current_idx: Optional[int] = None
        self._active_cmd_id: Optional[int] = None
        self._stage = ""
        self._batch_concurrency = 1
        self._started_count = 0
        self._total = 0
        self._done_count = 0
        self._ok_count = 0
        self._failed_count = 0

        self._vis_cfg = build_visibility_config(self._config)
        self._simp_cfg = build_simplify_config(self._config)

        self._build_layout()
        self._refresh_config_summary()
        self._refresh_tree()
        self._update_controls()

        if initial_files:
            self.queue_files([Path(p) for p in initial_files])

        self._log_panel.set_progress(0.0, "Ready — add STEP files and press RUN.")
        self._log_panel.append_log(f"Loaded config: {self._config_path}")
        self._log_panel.append_log(
            "Batch mode: parameters are read from config.json every time RUN is pressed."
        )

    # ------------------------------------------------------------------
    # Public state used by the main window (tab label / title sync)
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def completed(self) -> int:
        return self._done_count

    @property
    def total(self) -> int:
        return self._total

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        header = tk.Frame(self, background=COLORS["surface"])
        header.pack(side=tk.TOP, fill=tk.X)

        title_row = tk.Frame(header, background=COLORS["surface"])
        title_row.pack(fill=tk.X, padx=8, pady=(5, 0))
        tk.Label(
            title_row,
            text="Batch STEP Simplifier",
            background=COLORS["surface"],
            foreground=COLORS["accent"],
            font=FONTS["heading"],
        ).pack(side=tk.LEFT)

        self._summary_var = tk.StringVar(value="No STEP files queued.")
        tk.Label(
            title_row,
            textvariable=self._summary_var,
            background=COLORS["surface"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small_bold"],
        ).pack(side=tk.RIGHT)

        tk.Label(
            header,
            text=(
                "Add .step / .stp files and press [RUN] — each file is simplified using config.json (<name>_simplified.step).\n"
                "Tip: highlight rows to run a subset (none = all). Double-click a completed row to open it in the 3D viewer."
            ),
            background=COLORS["surface"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=8, pady=(2, 4))

        toolbar = tk.Frame(header, background=COLORS["surface"])
        toolbar.pack(fill=tk.X, padx=8, pady=(0, 5))

        self._btn_add = ttk.Button(
            toolbar,
            text="Add STEP Files...",
            style="Info.TButton",
            command=self.add_files_dialog,
        )
        self._btn_add.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_remove = ttk.Button(
            toolbar,
            text="Remove Selected",
            style="Toolbar.TButton",
            command=self._on_remove_selected,
        )
        self._btn_remove.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_clear = ttk.Button(
            toolbar, text="Clear List", style="Toolbar.TButton", command=self._on_clear_list
        )
        self._btn_clear.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_reload = ttk.Button(
            toolbar,
            text="Reload config.json",
            style="Toolbar.TButton",
            command=self.reload_config,
        )
        self._btn_reload.pack(side=tk.LEFT)

        # Primary actions on the right: [Force Stop] [Stop] [RUN]
        self._btn_run = ttk.Button(
            toolbar, text="RUN", style="Info.TButton", command=self.run, width=10
        )
        self._btn_run.pack(side=tk.RIGHT, padx=(4, 0))

        self._btn_stop = ttk.Button(
            toolbar, text="Stop", style="Danger.TButton", command=self.stop, width=7
        )
        self._btn_stop.pack(side=tk.RIGHT, padx=(4, 0))

        self._btn_force_stop = ttk.Button(
            toolbar,
            text="Force Stop",
            style="Danger.TButton",
            command=self._on_force_stop,
        )
        self._btn_force_stop.pack(side=tk.RIGHT)

        tk.Frame(header, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # Parameter strip — read-only view of the values that RUN will use
        params_strip = tk.Frame(self, background=COLORS["surface_alt"], padx=8, pady=3)
        params_strip.pack(side=tk.TOP, fill=tk.X)

        self._param_var1 = tk.StringVar(value="")
        self._param_var2 = tk.StringVar(value="")
        tk.Label(
            params_strip,
            textvariable=self._param_var1,
            background=COLORS["surface_alt"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
        ).pack(fill=tk.X)
        tk.Label(
            params_strip,
            textvariable=self._param_var2,
            background=COLORS["surface_alt"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
        ).pack(fill=tk.X)

        tk.Frame(self, height=1, background=COLORS["border"]).pack(side=tk.TOP, fill=tk.X)

        # Bottom log drawer is packed BEFORE the queue table so it is never clipped
        self._log_panel = LogPanel(self, on_cancel=self.stop)
        self._log_panel.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(3, 6))

        self._files_box = ttk.LabelFrame(self, text="Queued STEP Files", padding=(6, 4))
        self._files_box.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(5, 3))

        columns = ("name", "folder", "status", "output")
        headings = {
            "name": ("STEP File", 170, False),
            "folder": ("Source Folder", 310, True),
            "status": ("Status", 75, False),
            "output": ("Output File", 185, True),
        }
        self._tree = ttk.Treeview(
            self._files_box, columns=columns, show="headings", selectmode="extended", height=5
        )
        for col, (text, width, stretch) in headings.items():
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, minwidth=70, stretch=stretch, anchor=tk.W)

        yscroll = ttk.Scrollbar(self._files_box, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=yscroll.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self._files_box.rowconfigure(0, weight=1)
        self._files_box.columnconfigure(0, weight=1)

        self._tree.tag_configure(
            "done", background=COLORS["success_soft"], foreground=COLORS["success_text"]
        )
        self._tree.tag_configure(
            "failed", background=COLORS["danger_soft"], foreground=COLORS["danger_text"]
        )
        self._tree.tag_configure(
            "running", background=COLORS["accent_soft"], foreground=COLORS["accent_text"]
        )
        self._tree.tag_configure(
            "skipped", background=COLORS["warning_soft"], foreground=COLORS["warning_text"]
        )

        # Double-click a row whose output exists -> open it in the configured STEP viewer
        self._tree.bind("<Double-1>", self._on_tree_double_click)

    # ------------------------------------------------------------------
    # Queue table helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_tag(status: str) -> str:
        return {
            ST_DONE: "done",
            ST_FAILED: "failed",
            ST_CANCELLED: "failed",
            ST_RUNNING: "running",
            ST_SKIPPED: "skipped",
        }.get(status, "pending")

    def _insert_row(self, idx: int, item: BatchItem) -> None:
        self._tree.insert(
            "",
            tk.END,
            iid=str(idx),
            values=(
                item.path.name,
                str(item.path.parent),
                item.status,
                item.output_path.name if item.output_path else "",
            ),
            tags=(self._row_tag(item.status),),
        )

    def _refresh_tree(self, selected: Optional[Iterable[int]] = None) -> None:
        self._tree.delete(*self._tree.get_children())
        for idx, item in enumerate(self._items):
            self._insert_row(idx, item)
        if selected is not None:
            iids = [str(i) for i in selected if 0 <= i < len(self._items)]
            if iids:
                self._tree.selection_set(iids)
        self._files_box.configure(text=f"Queued STEP Files — {len(self._items)}")
        self._update_summary()

    def _update_row(self, idx: int) -> None:
        if not (0 <= idx < len(self._items)):
            return
        item = self._items[idx]
        iid = str(idx)
        if self._tree.exists(iid):
            self._tree.item(
                iid,
                values=(
                    item.path.name,
                    str(item.path.parent),
                    item.status,
                    item.output_path.name if item.output_path else "",
                ),
                tags=(self._row_tag(item.status),),
            )
        self._update_summary()

    def _update_summary(self) -> None:
        if not self._items:
            self._summary_var.set("No STEP files queued.")
            return
        done = sum(1 for it in self._items if it.status == ST_DONE)
        failed = sum(1 for it in self._items if it.status == ST_FAILED)
        pending = sum(1 for it in self._items if it.status == ST_PENDING)
        bits = [f"{len(self._items)} queued"]
        if done:
            bits.append(f"{done} done")
        if failed:
            bits.append(f"{failed} failed")
        if pending:
            bits.append(f"{pending} pending")
        self._summary_var.set(" · ".join(bits))

    def _update_controls(self) -> None:
        idle = not self._running
        has_items = bool(self._items)
        self._btn_add.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self._btn_remove.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_clear.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_reload.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self._btn_run.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_stop.configure(
            state=tk.NORMAL if (self._running and not self._stop_requested) else tk.DISABLED
        )
        self._btn_force_stop.configure(state=tk.NORMAL if self._running else tk.DISABLED)

    def _refresh_config_summary(self) -> None:
        c = self._config
        resolved_w = resolve_batch_workers(c.batch_workers)
        worker_lbl = f"auto ({resolved_w})" if int(c.batch_workers) <= 0 else str(resolved_w)
        self._param_var1.set(
            f"Parameters from config.json:   workers={worker_lbl}  ·  views={c.num_views}  ·  "
            f"grid={c.grid_resolution}  ·  min visible={c.min_visible_ratio_pct:g}%  ·  "
            f"pass2={'on' if c.enable_pass2_verify else 'off'}  ·  schema={c.step_schema or 'AP214'}"
        )
        self._param_var2.set(
            f"Core fill={'on' if c.fill_internal_core else 'off'} "
            f"(gap {c.core_bridge_gap_mm:g} mm · inset {c.core_inset_mm:g} mm)  ·  "
            f"island bridge={'on' if c.connect_floating_parts else 'off'} "
            f"({c.max_island_gap_mm:g} mm)  ·  "
            f"small holes={'on' if c.remove_small_holes else 'off'}  ·  "
            f"unify={'on' if c.unify_same_domain else 'off'}  ·  "
            f"largest only={'on' if c.keep_largest_only else 'off'}"
        )

    # ------------------------------------------------------------------
    # Queue management actions
    # ------------------------------------------------------------------

    def queue_files(self, paths: Iterable[Path]) -> None:
        """Append STEP files (drag & drop, startup files, or programmatic use)."""
        if self._running:
            return

        known = {os.path.normcase(str(item.path)) for item in self._items}
        added = 0
        ignored = 0
        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
            except OSError:
                ignored += 1
                continue
            if not path.is_file() or path.suffix.lower() not in STEP_SUFFIXES:
                ignored += 1
                continue
            key = os.path.normcase(str(path))
            if key in known:
                ignored += 1
                continue
            known.add(key)
            self._items.append(BatchItem(path=path))
            added += 1

        if added:
            start = len(self._items) - added
            self._refresh_tree(selected=range(start, len(self._items)))
            self._log_panel.append_log(
                f"Added {added} STEP file(s) — {len(self._items)} file(s) queued."
            )
        if ignored:
            self._log_panel.append_log(
                f"Ignored {ignored} item(s): not a .step/.stp file, missing, or already queued."
            )
        self._update_controls()

    def add_files_dialog(self) -> None:
        if self._running:
            return
        init_dir = self._config.last_open_dir
        if not init_dir or not Path(init_dir).is_dir():
            init_dir = str(Path.cwd())
        selected = filedialog.askopenfilenames(
            parent=self,
            title="Select STEP Files",
            initialdir=init_dir,
            filetypes=[
                ("STEP Files", ("*.step", "*.stp", "*.STEP", "*.STP")),
                ("All Files", "*.*" if os.name == "nt" else "*"),
            ],
        )
        if selected:
            self.queue_files([Path(p) for p in selected])

    def _on_remove_selected(self) -> None:
        if self._running:
            return
        selected = sorted((int(iid) for iid in self._tree.selection()), reverse=True)
        if not selected:
            self._log_panel.append_log("No rows selected to remove.")
            return
        for idx in selected:
            if 0 <= idx < len(self._items):
                del self._items[idx]
        self._refresh_tree()
        self._update_controls()
        self._log_panel.append_log(
            f"Removed {len(selected)} file(s) — {len(self._items)} file(s) queued."
        )

    def _on_clear_list(self) -> None:
        if self._running or not self._items:
            return
        count = len(self._items)
        self._items.clear()
        self._refresh_tree()
        self._update_controls()
        self._log_panel.append_log(f"Cleared {count} file(s) from the queue.")

    # ------------------------------------------------------------------
    # Output viewer integration
    # ------------------------------------------------------------------

    def _on_tree_double_click(self, event: tk.Event) -> None:
        """Open the row's simplified STEP file in the configured viewer, if it exists."""
        iid = self._tree.identify_row(int(event.y))
        if not iid:
            return
        try:
            idx = int(iid)
        except ValueError:
            return
        self.open_output_file(idx)

    def open_output_file(self, idx: int) -> None:
        """Launch the configured external STEP viewer for the item's saved output."""
        if not (0 <= idx < len(self._items)):
            return
        item = self._items[idx]
        output = item.output_path
        if output is None or not Path(output).is_file():
            self._log_panel.append_log(
                f"No simplified STEP file is available yet for {item.path.name}."
            )
            return
        try:
            message = launch_external_viewer(Path(output), self._config)
        except Exception as exc:
            self._log_panel.append_log(f"Could not open {Path(output).name}: {exc}")
            messagebox.showerror("STEP Viewer Error", str(exc), parent=self)
            return
        self._log_panel.append_log(message)

    def reload_config(self) -> None:
        if self._running:
            return
        self._config = load_config(self._config_path)
        self._vis_cfg = build_visibility_config(self._config)
        self._simp_cfg = build_simplify_config(self._config)
        self._refresh_config_summary()
        self._log_panel.append_log(f"Reloaded config.json: {self._config_path}")

    # ------------------------------------------------------------------
    # Batch orchestration
    # ------------------------------------------------------------------

    def run(self) -> None:
        if self._running:
            return
        if not self._items:
            self._log_panel.set_progress(0.0, "Nothing to do — add at least one STEP file first.")
            self._log_panel.append_log("RUN ignored — no STEP files are queued.")
            return

        # Selected rows are processed; with no selection the whole queue runs.
        selected = sorted(int(iid) for iid in self._tree.selection())
        targets = [i for i in selected if 0 <= i < len(self._items)]
        if not targets:
            targets = list(range(len(self._items)))
        targets = [i for i in targets if self._items[i].path.is_file()]
        if not targets:
            self._log_panel.set_progress(0.0, "Selected files are missing on disk.")
            self._log_panel.append_log("RUN ignored — the selected file(s) no longer exist.")
            return

        # config.json is the single source of truth for every batch run
        self._config = load_config(self._config_path)
        self._vis_cfg = build_visibility_config(self._config)
        self._simp_cfg = build_simplify_config(self._config)
        self._refresh_config_summary()

        for idx in targets:
            self._items[idx].status = ST_PENDING
            self._items[idx].output_path = None

        self._queue = list(targets)
        self._total = len(targets)
        self._started_count = 0
        self._done_count = 0
        self._ok_count = 0
        self._failed_count = 0
        self._current_idx = None
        self._active_cmd_id = None
        self._batch_concurrency = resolve_batch_workers(self._config.batch_workers, self._total)
        self._stop_requested = False
        self._running = True

        self._refresh_tree(selected=targets)
        self._update_controls()
        self._log_panel.set_busy(True)
        self._log_panel.set_progress(
            0.0,
            f"Starting batch of {self._total} file(s) ({self._batch_concurrency} worker(s))...",
        )
        self._log_panel.append_log(
            f"RUN — {self._total} file(s) · workers={self._batch_concurrency} "
            f"(threads/worker={threads_per_batch_worker(self._batch_concurrency)}) · "
            f"schema={self._simp_cfg.step_schema} · views={self._vis_cfg.num_views} · "
            f"grid={self._vis_cfg.grid_resolution} (params from {self._config_path.name})"
        )
        self._advance()

    def _active_slots(self) -> list[_WorkerSlot]:
        return [s for s in self._slots if s.item_idx is not None]

    def _clear_slot(self, slot: _WorkerSlot) -> None:
        slot.item_idx = None
        slot.cmd_id = None
        slot.stage = ""
        slot.file_progress = 0.0
        active = self._active_slots()
        if active:
            self._current_idx = active[0].item_idx
            self._active_cmd_id = active[0].cmd_id
            self._stage = active[0].stage
        else:
            self._current_idx = None
            self._active_cmd_id = None
            self._stage = ""

    def _advance(self) -> None:
        if self._stop_requested:
            if not self._active_slots():
                self._finish_batch(stopped=True)
            return

        if not self._queue:
            if not self._active_slots():
                self._finish_batch(stopped=False)
            return

        pool_size = max(1, min(self._batch_concurrency, len(self._queue) + len(self._active_slots())))
        self._ensure_worker_pool(pool_size)

        for slot in self._slots[:pool_size]:
            if self._stop_requested or not self._queue:
                break
            if slot.item_idx is not None:
                continue
            idx = self._queue.pop(0)
            self._started_count += 1
            self._current_idx = idx
            item = self._items[idx]
            item.status = ST_RUNNING
            self._update_row(idx)
            worker_tag = f" [W{slot.slot_id + 1}]" if pool_size > 1 else ""
            self._log_panel.append_log(
                f"[{self._started_count}/{self._total}] {item.path.name}{worker_tag}  —  {item.path.parent}"
            )
            self._submit_slot(slot, idx, "load_step", {"path": str(item.path)})

    def ensure_worker(self) -> CadWorkerClient:
        """Create the primary persistent CAD worker process on first use (lazy start)."""
        if not self._slots:
            self._log_panel.append_log("Starting background CAD worker process...")
            client = CadWorkerClient()
            self._slots.append(_WorkerSlot(slot_id=0, client=client))
            self._worker = client
            if self._poll_after_id is None:
                self._poll_after_id = self.after(40, self._poll_worker)
        return self._slots[0].client

    def _ensure_worker_pool(self, count: int) -> None:
        """Ensure `count` persistent CAD workers are running with balanced thread budgets."""
        count = max(1, int(count))
        budget = threads_per_batch_worker(count)
        self.ensure_worker()
        # Update thread budget on any idle existing slots
        for slot in self._slots[:count]:
            if slot.item_idx is None:
                slot.client.set_max_threads(budget)
        while len(self._slots) < count:
            sid = len(self._slots)
            self._log_panel.append_log(
                f"Starting parallel CAD worker #{sid + 1} (thread budget={budget})..."
            )
            client = CadWorkerClient(max_threads=budget)
            self._slots.append(_WorkerSlot(slot_id=sid, client=client))
        if self._poll_after_id is None:
            self._poll_after_id = self.after(40, self._poll_worker)

    def _submit_slot(
        self, slot: _WorkerSlot, item_idx: int, cmd_name: str, payload: dict[str, Any]
    ) -> None:
        slot.item_idx = item_idx
        slot.stage = cmd_name
        lo, _ = self._STAGE_RANGES.get(cmd_name, (0.0, 1.0))
        slot.file_progress = max(slot.file_progress, lo)
        slot.cmd_id = slot.client.submit(cmd_name, payload)
        self._stage = cmd_name
        self._active_cmd_id = slot.cmd_id
        self._current_idx = item_idx

    def _submit(self, cmd_name: str, payload: dict[str, Any]) -> None:
        self.ensure_worker()
        slot = self._slots[0]
        idx = slot.item_idx if slot.item_idx is not None else self._current_idx
        if idx is not None:
            self._submit_slot(slot, idx, cmd_name, payload)
        else:
            self._stage = cmd_name
            self._active_cmd_id = slot.client.submit(cmd_name, payload)

    def _on_progress(
        self, fraction: float, message: str, slot: Optional[_WorkerSlot] = None
    ) -> None:
        if slot is not None:
            lo, hi = self._STAGE_RANGES.get(slot.stage, (0.0, 1.0))
            slot.file_progress = lo + (hi - lo) * max(0.0, min(1.0, float(fraction)))
            active = self._active_slots()
            in_flight = sum(s.file_progress for s in active)
            overall = (self._done_count + in_flight) / max(1, self._total)
            idx = slot.item_idx
            name = self._items[idx].path.name if idx is not None else ""
            label = self._STAGE_LABELS.get(slot.stage, "Working")
            if len(active) > 1:
                prefix = f"[{self._done_count}/{self._total} done · {len(active)} active]"
            else:
                prefix = f"[{min(self._done_count + 1, self._total)}/{self._total}]"
            self._log_panel.set_progress(overall, f"{prefix} {label}: {name} — {message}")
            return

        lo, hi = self._STAGE_RANGES.get(self._stage, (0.0, 1.0))
        local = lo + (hi - lo) * max(0.0, min(1.0, float(fraction)))
        overall = (self._done_count + local) / max(1, self._total)
        idx = self._current_idx
        name = self._items[idx].path.name if idx is not None else ""
        label = self._STAGE_LABELS.get(self._stage, "Working")
        self._log_panel.set_progress(
            overall, f"[{self._done_count + 1}/{self._total}] {label}: {name} — {message}"
        )

    def stop(self) -> None:
        if not self._running or self._stop_requested:
            return
        self._stop_requested = True
        for slot in self._slots:
            if slot.item_idx is not None:
                slot.client.request_cancel()
        self._update_controls()
        self._log_panel.append_log(
            "Stop requested — aborting active file(s); remaining files will be skipped."
        )

    def _on_force_stop(self) -> None:
        if not self._running:
            return
        self._stop_requested = True
        active = self._active_slots()
        if active:
            for slot in active:
                idx = slot.item_idx
                slot.client.force_terminate_and_restart()
                self._clear_slot(slot)
                if idx is not None and 0 <= idx < len(self._items):
                    item = self._items[idx]
                    item.status = ST_CANCELLED
                    item.output_path = None
                    self._done_count += 1
                    self._failed_count += 1
                    self._update_row(idx)
                    self._log_panel.append_log(
                        f"■ Force stopped: {item.path.name} (background CAD worker restarted)"
                    )
        else:
            idx = self._current_idx
            if self._worker is not None:
                self._worker.force_terminate_and_restart()
            self._active_cmd_id = None
            if idx is not None and 0 <= idx < len(self._items):
                item = self._items[idx]
                item.status = ST_CANCELLED
                item.output_path = None
                self._done_count += 1
                self._failed_count += 1
                self._update_row(idx)
                self._log_panel.append_log(
                    f"■ Force stopped: {item.path.name} (background CAD worker restarted)"
                )
        self._finish_batch(stopped=True)

    def _finish_batch(self, stopped: bool) -> None:
        skipped = 0
        if stopped:
            for idx in self._queue:
                if 0 <= idx < len(self._items) and self._items[idx].status == ST_PENDING:
                    self._items[idx].status = ST_SKIPPED
                    skipped += 1
            self._queue.clear()

        for slot in self._slots:
            slot.item_idx = None
            slot.cmd_id = None
            slot.stage = ""
            slot.file_progress = 0.0

        # Safety net: an interrupted stage must never leave a row "Running"
        for item in self._items:
            if item.status == ST_RUNNING:
                item.status = ST_CANCELLED

        self._current_idx = None
        self._stage = ""
        self._active_cmd_id = None
        self._running = False
        self._stop_requested = False

        self._refresh_tree()
        self._update_controls()
        self._log_panel.set_busy(False)

        completed_fraction = self._done_count / max(1, self._total)
        bits = [f"{self._ok_count} succeeded", f"{self._failed_count} failed"]
        if skipped:
            bits.append(f"{skipped} skipped")
        summary = (
            f"Batch {'stopped' if stopped else 'finished'} — " + ", ".join(bits) + "."
        )
        self._log_panel.set_progress(completed_fraction, summary)
        self._log_panel.append_log(summary)

    # ------------------------------------------------------------------
    # Worker event loop
    # ------------------------------------------------------------------

    def _poll_worker(self) -> None:
        self._poll_after_id = None
        if not self._slots:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        for slot in list(self._slots):
            for ev in slot.client.poll_events():
                if ev.kind == "worker_ready":
                    tag = f" #{slot.slot_id + 1}" if slot.slot_id > 0 else ""
                    self._log_panel.append_log(f"✔ Background CAD worker{tag} ready")
                elif ev.kind == "progress":
                    cmd_id, frac, msg = ev.payload
                    if cmd_id == slot.cmd_id and self._running:
                        self._on_progress(frac, msg, slot=slot)
                elif ev.kind == "done":
                    cmd_id, cmd_name, res = ev.payload
                    if cmd_name == "configure_threads":
                        continue
                    if cmd_id != slot.cmd_id or not self._running:
                        continue
                    self._handle_stage_done(cmd_name, res, slot=slot)
                elif ev.kind == "cancelled":
                    cmd_id, _cmd_name, msg = ev.payload
                    if cmd_id != slot.cmd_id or not self._running:
                        continue
                    self._handle_cancelled(msg, slot=slot)
                elif ev.kind in ("error", "crashed"):
                    cmd_id = ev.payload[0]
                    if cmd_id != slot.cmd_id or not self._running:
                        continue
                    self._handle_failure(
                        ev.payload[2], crashed=(ev.kind == "crashed"), slot=slot
                    )

        if self._slots:
            self._poll_after_id = self.after(40, self._poll_worker)

    def _handle_stage_done(
        self, cmd_name: str, res: Any, slot: Optional[_WorkerSlot] = None
    ) -> None:
        if slot is not None:
            slot.cmd_id = None
            idx = slot.item_idx
        else:
            self._active_cmd_id = None
            idx = self._current_idx

        if idx is None or not (0 <= idx < len(self._items)):
            if slot is not None:
                self._clear_slot(slot)
            if not self._active_slots():
                self._finish_batch(stopped=True)
            return
        item = self._items[idx]
        prefix = f"    [{item.path.name}] " if self._batch_concurrency > 1 else "    "

        # Stop clicked while this stage was finishing: abort at the stage boundary.
        # (The worker clears its cancel event when a new command starts, so the batch
        # must not submit another stage once a stop has been requested.)
        if self._stop_requested and cmd_name != "save_result":
            item.status = ST_CANCELLED
            item.output_path = None
            stage_label = self._STAGE_LABELS.get(cmd_name, cmd_name)
            self._update_row(idx)
            self._log_panel.append_log(f"    Stopped after {stage_label}: {item.path.name}")
            if slot is not None:
                self._clear_slot(slot)
            if not self._active_slots():
                self._finish_batch(stopped=True)
            return

        if cmd_name == "load_step":
            self._log_panel.append_log(
                f"{prefix}Loaded {len(res['parts'])} part(s) · "
                f"{res['total_volume']:,.0f} mm³ · unit {res['unit']}"
            )
            if slot is not None:
                self._submit_slot(
                    slot, idx, "analyze_visibility", {"config": self._vis_cfg, "overrides": {}}
                )
            else:
                self._submit("analyze_visibility", {"config": self._vis_cfg, "overrides": {}})
            return

        if cmd_name == "analyze_visibility":
            self._log_panel.append_log(
                f"{prefix}Visibility: keep {res['external_count']} external / "
                f"drop {res['internal_count']} internal"
            )
            if slot is not None:
                self._submit_slot(
                    slot, idx, "simplify", {"config": self._simp_cfg, "overrides": {}}
                )
            else:
                self._submit("simplify", {"config": self._simp_cfg, "overrides": {}})
            return

        if cmd_name == "simplify":
            v = res.validation
            self._log_panel.append_log(
                f"{prefix}Solidified: solid={v.solid_count} · shell={v.shell_count} · "
                f"faces={v.face_count:,} · fill +{res.filled_volume_delta:,.0f} mm³"
            )
            out_path = default_output_path(item.path)
            item.output_path = out_path
            self._update_row(idx)
            save_payload = {"output_path": str(out_path), "schema": self._simp_cfg.step_schema}
            if slot is not None:
                self._submit_slot(slot, idx, "save_result", save_payload)
            else:
                self._submit("save_result", save_payload)
            return

        if cmd_name == "save_result":
            saved = Path(res["output_path"])
            size_mb = res["size_bytes"] / (1024 * 1024)
            item.output_path = saved
            item.status = ST_DONE
            self._done_count += 1
            self._ok_count += 1
            self._update_row(idx)
            self._log_panel.append_log(f"{prefix}Saved: {saved} ({size_mb:.2f} MB)")
            if slot is not None:
                self._clear_slot(slot)
            if self._stop_requested:
                if not self._active_slots():
                    self._finish_batch(stopped=True)
            else:
                self._advance()

    def _handle_cancelled(self, message: str, slot: Optional[_WorkerSlot] = None) -> None:
        if slot is not None:
            idx = slot.item_idx
            self._clear_slot(slot)
        else:
            self._active_cmd_id = None
            idx = self._current_idx
        if idx is not None and 0 <= idx < len(self._items):
            self._items[idx].status = ST_CANCELLED
            self._update_row(idx)
        self._log_panel.append_log(f"■ Cancelled: {message}")
        if not self._active_slots():
            self._finish_batch(stopped=True)

    def _handle_failure(
        self, message: str, crashed: bool = False, slot: Optional[_WorkerSlot] = None
    ) -> None:
        if slot is not None:
            idx = slot.item_idx
            self._clear_slot(slot)
        else:
            self._active_cmd_id = None
            idx = self._current_idx
        if idx is not None and 0 <= idx < len(self._items):
            item = self._items[idx]
            item.status = ST_FAILED
            item.output_path = None
            self._done_count += 1
            self._failed_count += 1
            self._update_row(idx)
            self._log_panel.append_log(f"✖ Failed: {item.path.name} — {message}")
        if crashed:
            self._log_panel.append_log(
                "    Background CAD worker crashed and was restarted — continuing with the next file."
            )
        if self._stop_requested:
            if not self._active_slots():
                self._finish_batch(stopped=True)
        else:
            self._advance()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop polling and cleanly terminate all batch CAD worker processes."""
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        for slot in self._slots:
            slot.client.shutdown()
        self._slots.clear()
        self._worker = None
