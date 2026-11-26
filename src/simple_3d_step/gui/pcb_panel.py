"""PCB Mode panel embedded in the unified SIMPLE 3D STEP notebook GUI.

Mirrors the familiar Batch Mode multi-file queue layout while running the dedicated
Altium PCB simplification pipeline:
  - Skips internal-part visibility culling
  - Simplifies each PCB component into 1 watertight solid (cylinders for electrolytic
    caps/cans, fused body+heatsink+grouped-lead boxes for ICs & connectors, local OBB
    boxes for compact SMDs, text/logo feature removal)
  - Preserves each component's representative main-body RGB color in AP214/AP242 XCAF STEP
  - Auto-pairs matching Altium IDF (.bdf / .ldf, plus standard fallback pairs) or lets the
    user manually attach IDF files to supplement STEP-missing parts as rectangular boxes
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
    remember_last_open_dir,
    remember_last_save_dir,
    resolve_batch_workers,
    resolve_last_open_dir,
    save_config,
    threads_per_batch_worker,
)
from simple_3d_step.core.idf_parser import (
    IDF_BOARD_EXTS,
    IDF_EXT_PAIRS,
    IDF_LIB_EXTS,
    find_matching_idf_files,
)
from simple_3d_step.core.worker import CadWorkerClient
from simple_3d_step.gui.batch_panel import (
    ST_CANCELLED,
    ST_DONE,
    ST_FAILED,
    ST_PENDING,
    ST_RUNNING,
    ST_SKIPPED,
    STEP_SUFFIXES,
)
from simple_3d_step.gui.log_panel import LogPanel
from simple_3d_step.gui.theme import COLORS, FONTS
from simple_3d_step.models import PcbSimplifyConfig, RGBColor


def default_pcb_output_path(source: Path) -> Path:
    """Return the simplified PCB STEP path in the *same directory* as the source file."""
    source = Path(source)
    return source.with_name(f"{source.stem}_pcb_simplified.step")


def build_pcb_simplify_config(cfg: AppConfig) -> PcbSimplifyConfig:
    """Map ``config.json`` values onto ``PcbSimplifyConfig``."""
    raw_col = getattr(cfg, "pcb_idf_default_color", None) or [0.35, 0.35, 0.38]
    if isinstance(raw_col, (list, tuple)) and len(raw_col) >= 3:
        idf_color: RGBColor = (
            max(0.0, min(1.0, float(raw_col[0]))),
            max(0.0, min(1.0, float(raw_col[1]))),
            max(0.0, min(1.0, float(raw_col[2]))),
        )
    else:
        idf_color = (0.35, 0.35, 0.38)

    return PcbSimplifyConfig(
        simplify_leads=bool(cfg.pcb_simplify_leads),
        detect_cylinders=bool(cfg.pcb_detect_cylinders),
        detect_hex_posts=bool(cfg.pcb_detect_hex_posts),
        cylinder_circularity_min=max(0.50, min(0.98, float(cfg.pcb_cylinder_circularity_min))),
        remove_text_features=bool(cfg.pcb_remove_text_features),
        small_part_box_mm=max(0.0, float(cfg.pcb_small_part_box_mm)),
        preserve_simple_max_faces=max(0, int(cfg.pcb_preserve_simple_max_faces)),
        preserve_colors=bool(cfg.pcb_preserve_colors),
        use_idf_supplement=bool(cfg.pcb_use_idf_supplement),
        keep_board_holes=bool(cfg.pcb_keep_board_holes),
        min_board_hole_mm=max(0.0, float(cfg.pcb_min_board_hole_mm)),
        exclude_small_part_mm=max(0.0, float(cfg.pcb_exclude_small_part_mm)),
        min_color_brightness=max(0.0, min(0.85, float(cfg.pcb_min_color_brightness))),
        idf_default_color=idf_color,
        step_schema=cfg.step_schema or "AP214",
    )


@dataclass
class PcbBatchItem:
    """One queued PCB STEP file (or standalone IDF board file), paired IDF (.bdf/.ldf), and state."""

    path: Optional[Path] = None
    bdf_path: Optional[Path] = None
    ldf_path: Optional[Path] = None
    idf_source: str = "none"  # "auto" | "manual" | "only" | "none"
    status: str = ST_PENDING
    output_path: Optional[Path] = None

    @property
    def is_idf_only(self) -> bool:
        return self.path is None and self.bdf_path is not None

    @property
    def source_path(self) -> Path:
        if self.path is not None:
            return self.path
        assert self.bdf_path is not None
        return self.bdf_path

    @property
    def display_name(self) -> str:
        if self.path is not None:
            return self.path.name
        if self.bdf_path is not None:
            return self.bdf_path.name
        return "(Unknown)"

    @property
    def idf_label(self) -> str:
        if self.bdf_path is None:
            return "—"
        b_ext = self.bdf_path.suffix.lstrip(".").lower()
        if self.ldf_path is not None:
            l_ext = self.ldf_path.suffix.lstrip(".").lower()
            pair = f"{b_ext}+{l_ext}"
        else:
            pair = f"{b_ext} only"
        if self.is_idf_only or self.idf_source == "only":
            return f"IDF Only ({pair})"
        prefix = "Manual" if self.idf_source == "manual" else "Auto"
        return f"{prefix} ({pair})"


@dataclass
class _PcbWorkerSlot:
    """Tracks one persistent CAD subprocess and its currently assigned PCB item."""

    slot_id: int
    client: CadWorkerClient
    item_idx: Optional[int] = None
    cmd_id: Optional[int] = None
    file_progress: float = 0.0


class PcbPanel(tk.Frame):
    """Multi-worker parallel Altium PCB STEP + IDF simplifier driven by ``config.json``."""

    def __init__(
        self,
        parent: tk.Misc,
        config_path: Optional[Path] = None,
        initial_files: Optional[Iterable[Path]] = None,
    ) -> None:
        super().__init__(parent, background=COLORS["app_bg"])

        self._config_path = Path(config_path) if config_path else get_default_config_path()
        self._config: AppConfig = load_config(self._config_path)
        self._pcb_cfg: PcbSimplifyConfig = build_pcb_simplify_config(self._config)

        # Right-hand PCB parameter panel is hidden by default (matches Single Mode [Parameters] toggle)
        self._params_visible_var = tk.BooleanVar(value=False)

        # User-editable PCB filter controls (synced with config.json on Save / RUN / Reload)
        self._hole_limit_var = tk.StringVar(value=f"{self._config.pcb_min_board_hole_mm:g}")
        self._exclude_small_var = tk.StringVar(value=f"{self._config.pcb_exclude_small_part_mm:g}")
        self._min_bright_var = tk.StringVar(value=f"{self._config.pcb_min_color_brightness:.2f}")
        self._keep_simple_var = tk.StringVar(
            value=f"{max(0, int(self._config.pcb_preserve_simple_max_faces))}"
        )

        self._items: list[PcbBatchItem] = []
        self._slots: list[_PcbWorkerSlot] = []
        self._worker: Optional[CadWorkerClient] = None
        self._poll_after_id: Optional[str] = None

        # Batch orchestration state
        self._running = False
        self._stop_requested = False
        self._queue: list[int] = []
        self._batch_concurrency = 1
        self._started_count = 0
        self._total = 0
        self._done_count = 0
        self._ok_count = 0
        self._failed_count = 0

        self._build_layout()
        self._refresh_config_summary()
        self._refresh_tree()
        self._update_controls()

        if initial_files:
            self.queue_files([Path(p) for p in initial_files])

        self._log_panel.set_progress(
            0.0,
            "Ready — add Altium PCB STEP or IDF (.bdf/.ldf) files and press RUN.",
        )
        self._log_panel.append_log(
            "PCB mode: per-component 1-solid simplification with color preservation, IDF supplementation, and standalone IDF-only STEP generation."
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
            text="PCB STEP Simplifier (Altium STEP + IDF)",
            background=COLORS["surface"],
            foreground=COLORS["accent"],
            font=FONTS["heading"],
        ).pack(side=tk.LEFT)

        # Collapsible [Parameters] toggle button matching Single Mode
        self._tb_params = ttk.Checkbutton(
            title_row,
            text="Parameters",
            style="Toolbar.Toolbutton",
            variable=self._params_visible_var,
            command=self._toggle_params_panel,
        )
        self._tb_params.pack(side=tk.RIGHT, padx=(8, 0))

        self._summary_var = tk.StringVar(value="No PCB files queued.")
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
                "Add PCB .step / .stp files or IDF (.bdf/.ldf) files and press [RUN] — generates <name>_pcb_simplified.step.\n"
                "Sibling .bdf/.ldf IDF files auto-link to STEP files, or use [Add IDF...] for IDF-only models or manual linking. Double-click a completed row to view."
            ),
            background=COLORS["surface"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=8, pady=(2, 4))

        self._toolbar = tk.Frame(header, background=COLORS["surface"])
        self._toolbar.pack(fill=tk.X, padx=8, pady=(0, 5))

        self._btn_add = ttk.Button(
            self._toolbar,
            text="Add STEP Files...",
            style="Info.TButton",
            command=self.add_files_dialog,
        )
        self._btn_add.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_attach_idf = ttk.Button(
            self._toolbar,
            text="Add IDF...",
            style="Toolbar.TButton",
            command=self.add_idf_dialog,
        )
        self._btn_attach_idf.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_add_idf = self._btn_attach_idf

        self._btn_remove = ttk.Button(
            self._toolbar,
            text="Remove",
            style="Toolbar.TButton",
            command=self._on_remove_selected,
        )
        self._btn_remove.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_clear = ttk.Button(
            self._toolbar, text="Clear", style="Toolbar.TButton", command=self._on_clear_list
        )
        self._btn_clear.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_reload = ttk.Button(
            self._toolbar,
            text="Reload config.json",
            style="Toolbar.TButton",
            command=self.reload_config,
        )
        self._btn_reload.pack(side=tk.LEFT)

        # Primary actions on the right: [Force Stop] [Stop] [RUN]
        self._btn_run = ttk.Button(
            self._toolbar, text="RUN", style="Info.TButton", command=self.run, width=10
        )
        self._btn_run.pack(side=tk.RIGHT, padx=(4, 0))

        self._btn_stop = ttk.Button(
            self._toolbar, text="Stop", style="Danger.TButton", command=self.stop, width=7
        )
        self._btn_stop.pack(side=tk.RIGHT, padx=(4, 0))

        self._btn_force_stop = ttk.Button(
            self._toolbar,
            text="Force Stop",
            style="Danger.TButton",
            command=self._on_force_stop,
        )
        self._btn_force_stop.pack(side=tk.RIGHT)

        tk.Frame(header, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # Compact read-only parameter summary strip (matches Batch Mode; edit via [Parameters] drawer)
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

        # Bottom log drawer is packed BEFORE the main pane so it is never clipped
        self._log_panel = LogPanel(self, on_cancel=self.stop)
        self._log_panel.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(3, 6))

        # Main PanedWindow: Left = Queued PCB STEP Files, Right = Collapsible PCB Parameters card
        self._main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self._main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(5, 3))

        self._files_box = ttk.LabelFrame(
            self._main_pane, text="Queued PCB STEP / IDF Files", padding=(6, 4)
        )
        self._params_panel = self._build_params_panel(self._main_pane)

        # Show only the file queue by default; right-hand PCB Parameters card toggles via [Parameters]
        self._main_pane.add(self._files_box, weight=3)

        columns = ("name", "idf", "folder", "status", "output")
        headings = {
            "name": ("STEP / IDF File", 160, False),
            "idf": ("IDF (.bdf/.ldf)", 125, False),
            "folder": ("Source Folder", 230, True),
            "status": ("Status", 75, False),
            "output": ("Output File", 175, True),
        }
        self._tree = ttk.Treeview(
            self._files_box, columns=columns, show="headings", selectmode="extended", height=5
        )
        for col, (text, width, stretch) in headings.items():
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width, minwidth=65, stretch=stretch, anchor=tk.W)

        yscroll = ttk.Scrollbar(self._files_box, orient=tk.VERTICAL, command=self._tree.yview)
        xscroll = ttk.Scrollbar(self._files_box, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
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

        self._tree.bind("<Double-1>", self._on_tree_double_click)
        self._tree.bind("<Button-3>", self._on_tree_right_click)

        # Context menu for convenient IDF linking / viewer actions
        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(
            label="Attach / Change IDF (.bdf/.ldf)...",
            command=lambda: self.attach_idf_dialog(force_attach=True),
        )
        self._ctx_menu.add_command(label="Clear IDF Attachment", command=self._clear_selected_idf)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="Open Original STEP", command=self._open_selected_original)
        self._ctx_menu.add_command(label="Open Simplified STEP", command=self._open_selected_output)

    def _build_params_panel(self, parent: tk.Misc) -> tk.Frame:
        """Build the collapsible right-hand PCB Parameters card (mirrors Single Mode ControlPanel)."""
        card = tk.Frame(
            parent,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )

        # Card header strip
        hdr = ttk.Frame(card, style="Subtle.TFrame", padding=(10, 6))
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="PCB Parameters", style="SectionTitle.TLabel").pack(side=tk.LEFT)
        tk.Frame(card, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # Scroll-safe canvas so controls never clip on compact window heights
        scroll_wrap = tk.Frame(card, background=COLORS["surface"])
        scroll_wrap.pack(fill=tk.BOTH, expand=True)

        self._params_canvas = tk.Canvas(
            scroll_wrap,
            background=COLORS["surface"],
            highlightthickness=0,
            borderwidth=0,
            width=290,
        )
        vscroll = ttk.Scrollbar(scroll_wrap, orient=tk.VERTICAL, command=self._params_canvas.yview)
        self._params_canvas.configure(yscrollcommand=vscroll.set)
        self._params_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        body = ttk.Frame(self._params_canvas, style="Card.TFrame", padding=8)
        body_win = self._params_canvas.create_window((0, 0), window=body, anchor=tk.NW)

        def _on_body_cfg(_event=None) -> None:
            self._params_canvas.configure(scrollregion=self._params_canvas.bbox("all"))

        def _on_canvas_cfg(event) -> None:
            self._params_canvas.itemconfigure(body_win, width=event.width)

        body.bind("<Configure>", _on_body_cfg)
        self._params_canvas.bind("<Configure>", _on_canvas_cfg)

        # Section: Filtering & Color Visibility
        sec = ttk.LabelFrame(body, text="Board & Component Filters", padding=(8, 5))
        sec.pack(fill=tk.X, pady=(0, 6))
        sec.columnconfigure(1, weight=1)

        ttk.Label(sec, text="Delete Board Holes < (mm):", style="Card.TLabel").grid(
            row=0, column=0, sticky=tk.W, pady=(0, 1)
        )
        self._spin_hole_limit = ttk.Spinbox(
            sec,
            from_=0.0,
            to=20.0,
            increment=0.1,
            textvariable=self._hole_limit_var,
            width=6,
            command=self._on_param_input_changed,
        )
        self._spin_hole_limit.grid(row=0, column=1, sticky=tk.E, pady=(0, 1))
        tk.Label(
            sec,
            text="Plug vias smaller than this (0 = keep all).",
            background=COLORS["surface"],
            foreground=COLORS["text_muted"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=255,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(0, 5))

        ttk.Label(sec, text="Exclude Parts ≤ (mm):", style="Card.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=(0, 1)
        )
        self._spin_exclude_small = ttk.Spinbox(
            sec,
            from_=0.0,
            to=50.0,
            increment=0.5,
            textvariable=self._exclude_small_var,
            width=6,
            command=self._on_param_input_changed,
        )
        self._spin_exclude_small.grid(row=2, column=1, sticky=tk.E, pady=(0, 1))
        tk.Label(
            sec,
            text="Skip small parts ≤ this (0 = keep all).",
            background=COLORS["surface"],
            foreground=COLORS["text_muted"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=255,
        ).grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=(0, 5))

        ttk.Label(sec, text="Min Color Brightness:", style="Card.TLabel").grid(
            row=4, column=0, sticky=tk.W, pady=(0, 1)
        )
        self._spin_min_bright = ttk.Spinbox(
            sec,
            from_=0.0,
            to=0.50,
            increment=0.02,
            textvariable=self._min_bright_var,
            width=6,
            command=self._on_param_input_changed,
        )
        self._spin_min_bright.grid(row=4, column=1, sticky=tk.E, pady=(0, 1))
        tk.Label(
            sec,
            text="Lift dark/black part colors (0 = off).",
            background=COLORS["surface"],
            foreground=COLORS["text_muted"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=255,
        ).grid(row=5, column=0, columnspan=2, sticky=tk.EW, pady=(0, 5))

        ttk.Label(sec, text="Keep Simple ≤ (faces):", style="Card.TLabel").grid(
            row=6, column=0, sticky=tk.W, pady=(0, 1)
        )
        self._spin_keep_simple = ttk.Spinbox(
            sec,
            from_=0,
            to=200,
            increment=5,
            textvariable=self._keep_simple_var,
            width=6,
            command=self._on_param_input_changed,
        )
        self._spin_keep_simple.grid(row=6, column=1, sticky=tk.E, pady=(0, 1))
        tk.Label(
            sec,
            text="Keep & fuse low-face non-box parts as-is (0 = off).",
            background=COLORS["surface"],
            foreground=COLORS["text_muted"],
            font=FONTS["small"],
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=255,
        ).grid(row=7, column=0, columnspan=2, sticky=tk.EW, pady=(0, 2))

        for spin in (
            self._spin_hole_limit,
            self._spin_exclude_small,
            self._spin_min_bright,
            self._spin_keep_simple,
        ):
            spin.bind("<FocusOut>", lambda _e: self._on_param_input_changed())
            spin.bind("<Return>", lambda _e: self._on_param_input_changed())

        self._btn_save_params = ttk.Button(
            body,
            text="Save to config.json",
            style="Toolbar.TButton",
            command=self._on_save_params,
        )
        self._btn_save_params.pack(fill=tk.X)

        return card

    def _toggle_params_panel(self) -> None:
        """Show or hide the right-hand PCB Parameters card (same pattern as Single Mode)."""
        show = bool(self._params_visible_var.get())
        panes = self._main_pane.panes()
        panel_id = str(self._params_panel)
        if show and panel_id not in panes:
            self._main_pane.add(self._params_panel, weight=1)
            self.update_idletasks()
            pane_w = self._main_pane.winfo_width()
            if pane_w > 560:
                self._main_pane.sashpos(0, max(380, pane_w - 305))
        elif not show and panel_id in panes:
            self._main_pane.forget(self._params_panel)

    def _on_param_input_changed(self) -> None:
        """Reflect spinbox edits in the read-only summary strip immediately."""
        self._sync_controls_to_config()
        self._pcb_cfg = build_pcb_simplify_config(self._config)
        self._refresh_config_summary()

    def _on_save_params(self) -> None:
        if self._running:
            return
        self._config = load_config(self._config_path)
        self._sync_controls_to_config()
        save_config(self._config, self._config_path)
        self._pcb_cfg = build_pcb_simplify_config(self._config)
        self._refresh_config_summary()
        self._log_panel.append_log(
            f"Saved PCB parameters to {self._config_path.name} "
            f"(hole<{self._pcb_cfg.min_board_hole_mm:g}mm · "
            f"exclude<={self._pcb_cfg.exclude_small_part_mm:g}mm · "
            f"minL={self._pcb_cfg.min_color_brightness:.2f} · "
            f"keepSimple<={self._pcb_cfg.preserve_simple_max_faces}f)"
        )

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

    def _insert_row(self, idx: int, item: PcbBatchItem) -> None:
        self._tree.insert(
            "",
            tk.END,
            iid=str(idx),
            values=(
                item.display_name,
                item.idf_label,
                str(item.source_path.parent),
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
        self._files_box.configure(text=f"Queued PCB STEP / IDF Files — {len(self._items)}")
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
                    item.display_name,
                    item.idf_label,
                    str(item.source_path.parent),
                    item.status,
                    item.output_path.name if item.output_path else "",
                ),
                tags=(self._row_tag(item.status),),
            )
        self._update_summary()

    def _update_summary(self) -> None:
        if not self._items:
            self._summary_var.set("No PCB files queued.")
            return
        done = sum(1 for it in self._items if it.status == ST_DONE)
        failed = sum(1 for it in self._items if it.status == ST_FAILED)
        pending = sum(1 for it in self._items if it.status == ST_PENDING)
        idf_only = sum(1 for it in self._items if it.is_idf_only)
        with_idf = sum(1 for it in self._items if not it.is_idf_only and it.bdf_path is not None)
        bits = [f"{len(self._items)} queued"]
        if idf_only:
            bits.append(f"{idf_only} IDF-only")
        if with_idf:
            bits.append(f"{with_idf} with IDF")
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
        self._btn_attach_idf.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self._btn_remove.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_clear.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_reload.configure(state=tk.NORMAL if idle else tk.DISABLED)
        self._btn_run.configure(state=tk.NORMAL if idle and has_items else tk.DISABLED)
        self._btn_stop.configure(
            state=tk.NORMAL if (self._running and not self._stop_requested) else tk.DISABLED
        )
        self._btn_force_stop.configure(state=tk.NORMAL if self._running else tk.DISABLED)
        spin_state = tk.NORMAL if idle else tk.DISABLED
        self._spin_hole_limit.configure(state=spin_state)
        self._spin_exclude_small.configure(state=spin_state)
        self._spin_min_bright.configure(state=spin_state)
        self._spin_keep_simple.configure(state=spin_state)
        self._btn_save_params.configure(state=spin_state)

    @staticmethod
    def _parse_nonneg_float(raw: str, default: float, upper: float) -> float:
        try:
            val = float(str(raw).strip())
        except (TypeError, ValueError):
            return default
        if val < 0.0:
            return 0.0
        return min(upper, val)

    @staticmethod
    def _parse_nonneg_int(raw: str, default: int, upper: int) -> int:
        try:
            val = int(round(float(str(raw).strip())))
        except (TypeError, ValueError):
            return default
        if val < 0:
            return 0
        return min(upper, val)

    def _sync_controls_to_config(self) -> None:
        """Read GUI PCB spinbox inputs into self._config and normalize displayed values."""
        hole = self._parse_nonneg_float(
            self._hole_limit_var.get(), self._config.pcb_min_board_hole_mm, 50.0
        )
        excl = self._parse_nonneg_float(
            self._exclude_small_var.get(), self._config.pcb_exclude_small_part_mm, 200.0
        )
        bright = self._parse_nonneg_float(
            self._min_bright_var.get(), self._config.pcb_min_color_brightness, 0.85
        )
        keep_simple = self._parse_nonneg_int(
            self._keep_simple_var.get(), self._config.pcb_preserve_simple_max_faces, 500
        )
        self._config.pcb_min_board_hole_mm = hole
        self._config.pcb_exclude_small_part_mm = excl
        self._config.pcb_min_color_brightness = bright
        self._config.pcb_preserve_simple_max_faces = keep_simple
        self._hole_limit_var.set(f"{hole:g}")
        self._exclude_small_var.set(f"{excl:g}")
        self._min_bright_var.set(f"{bright:.2f}")
        self._keep_simple_var.set(f"{keep_simple}")

    def _sync_config_to_controls(self) -> None:
        """Push self._config values into the GUI PCB spinboxes."""
        self._hole_limit_var.set(f"{max(0.0, float(self._config.pcb_min_board_hole_mm)):g}")
        self._exclude_small_var.set(f"{max(0.0, float(self._config.pcb_exclude_small_part_mm)):g}")
        self._min_bright_var.set(
            f"{max(0.0, min(0.85, float(self._config.pcb_min_color_brightness))):.2f}"
        )
        self._keep_simple_var.set(
            f"{max(0, int(self._config.pcb_preserve_simple_max_faces))}"
        )

    def _refresh_config_summary(self) -> None:
        c = self._config
        resolved_w = resolve_batch_workers(c.batch_workers)
        worker_lbl = f"auto ({resolved_w})" if int(c.batch_workers) <= 0 else str(resolved_w)
        schema = c.step_schema or "AP214"
        if c.pcb_preserve_colors and schema.upper() == "AP203":
            schema = "AP214 (color)"
        excl_lbl = f"≤{c.pcb_exclude_small_part_mm:g} mm" if c.pcb_exclude_small_part_mm > 0 else "off"
        hole_lbl = f"<{c.pcb_min_board_hole_mm:g} mm" if c.pcb_min_board_hole_mm > 0 else "keep all"
        bright_lbl = f"{c.pcb_min_color_brightness:.2f}" if c.pcb_min_color_brightness > 0 else "off"
        keep_lbl = f"≤{int(c.pcb_preserve_simple_max_faces)}f" if int(c.pcb_preserve_simple_max_faces) > 0 else "off"
        self._param_var1.set(
            f"Parameters:  workers={worker_lbl}  ·  schema={schema}  ·  "
            f"delete holes {hole_lbl}  ·  exclude parts {excl_lbl}  ·  min brightness={bright_lbl}"
        )
        self._param_var2.set(
            f"Colors={'keep' if c.pcb_preserve_colors else 'off'}  ·  "
            f"keep simple={keep_lbl}  ·  "
            f"hex posts={'on' if c.pcb_detect_hex_posts else 'off'}  ·  "
            f"cylinders={'on' if c.pcb_detect_cylinders else 'off'}  ·  "
            f"lead boxes={'on' if c.pcb_simplify_leads else 'off'}  ·  "
            f"IDF={'on' if c.pcb_use_idf_supplement else 'off'}"
        )

    # ------------------------------------------------------------------
    # Queue & IDF Attachment Management
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_sibling_ldf(bdf_path: Path) -> Optional[Path]:
        """Auto-discover a matching IDF library file (.ldf/.pro/.emp) next to a board file."""
        for b_ext, l_ext in IDF_EXT_PAIRS:
            if bdf_path.suffix.lower() == b_ext:
                cand = bdf_path.with_suffix(l_ext)
                if cand.is_file():
                    return cand.resolve()
                try:
                    for sib in bdf_path.parent.iterdir():
                        if (
                            sib.is_file()
                            and sib.stem.lower() == bdf_path.stem.lower()
                            and sib.suffix.lower() == l_ext
                        ):
                            return sib.resolve()
                except OSError:
                    pass
                break
        return None

    @staticmethod
    def _resolve_sibling_bdf(ldf_path: Path) -> Optional[Path]:
        """Auto-discover a matching IDF board file (.bdf/.brd/.emn) next to a library file."""
        for b_ext, l_ext in IDF_EXT_PAIRS:
            if ldf_path.suffix.lower() == l_ext:
                cand = ldf_path.with_suffix(b_ext)
                if cand.is_file():
                    return cand.resolve()
                try:
                    for sib in ldf_path.parent.iterdir():
                        if (
                            sib.is_file()
                            and sib.stem.lower() == ldf_path.stem.lower()
                            and sib.suffix.lower() == b_ext
                        ):
                            return sib.resolve()
                except OSError:
                    pass
                break
        return None

    def queue_files(self, paths: Iterable[Path]) -> None:
        """Append PCB STEP files (auto-linking sibling IDF) and/or standalone IDF files."""
        if self._running:
            return

        known_steps = {
            os.path.normcase(str(item.path))
            for item in self._items
            if item.path is not None
        }
        known_idf_only = {
            os.path.normcase(str(item.bdf_path))
            for item in self._items
            if item.is_idf_only and item.bdf_path is not None
        }

        step_paths: list[Path] = []
        idf_board_paths: list[Path] = []
        idf_lib_paths: list[Path] = []
        ignored = 0

        for raw in paths:
            try:
                path = Path(raw).expanduser().resolve()
            except OSError:
                ignored += 1
                continue
            if not path.is_file():
                ignored += 1
                continue
            ext = path.suffix.lower()
            if ext in STEP_SUFFIXES:
                step_paths.append(path)
            elif ext in IDF_BOARD_EXTS:
                idf_board_paths.append(path)
            elif ext in IDF_LIB_EXTS:
                idf_lib_paths.append(path)
            else:
                ignored += 1

        added_step = 0
        added_idf_only = 0
        idf_linked = 0
        linked_bdfs_this_call: set[str] = set()

        for path in step_paths:
            key = os.path.normcase(str(path))
            if key in known_steps:
                ignored += 1
                continue
            known_steps.add(key)
            bdf, ldf = find_matching_idf_files(path)
            idf_src = "auto" if bdf is not None else "none"
            if bdf is not None:
                idf_linked += 1
                linked_bdfs_this_call.add(os.path.normcase(str(bdf)))
            self._items.append(
                PcbBatchItem(
                    path=path,
                    bdf_path=bdf,
                    ldf_path=ldf,
                    idf_source=idf_src,
                )
            )
            added_step += 1

        # Group standalone IDF inputs (excluding any .bdf already auto-linked to a STEP file above)
        lib_by_stem: dict[tuple[str, str], Path] = {
            (os.path.normcase(str(lp.parent)), lp.stem.lower()): lp
            for lp in idf_lib_paths
        }
        standalone_pairs: list[tuple[Path, Optional[Path]]] = []
        seen_bdfs: set[str] = set()
        for bp in idf_board_paths:
            bkey = os.path.normcase(str(bp))
            if bkey in linked_bdfs_this_call or bkey in seen_bdfs:
                continue
            seen_bdfs.add(bkey)
            lp = lib_by_stem.get((os.path.normcase(str(bp.parent)), bp.stem.lower()))
            if lp is None:
                lp = self._resolve_sibling_ldf(bp)
            standalone_pairs.append((bp, lp))

        # If only library files were passed, resolve their sibling board files
        for lp in idf_lib_paths:
            bp = self._resolve_sibling_bdf(lp)
            if bp is None:
                if not step_paths and not idf_board_paths:
                    ignored += 1
                continue
            bkey = os.path.normcase(str(bp))
            if bkey in linked_bdfs_this_call or bkey in seen_bdfs:
                continue
            seen_bdfs.add(bkey)
            standalone_pairs.append((bp, lp))

        for bp, lp in standalone_pairs:
            bkey = os.path.normcase(str(bp))
            if bkey in known_idf_only:
                ignored += 1
                continue
            known_idf_only.add(bkey)
            self._items.append(
                PcbBatchItem(
                    path=None,
                    bdf_path=bp,
                    ldf_path=lp,
                    idf_source="only",
                )
            )
            added_idf_only += 1

        total_added = added_step + added_idf_only
        if total_added:
            self._config.last_open_dir = remember_last_open_dir(
                self._items[-1].source_path, self._config_path
            )
            start = len(self._items) - total_added
            self._refresh_tree(selected=range(start, len(self._items)))
            msg_parts: list[str] = []
            if added_step:
                idf_note = f" ({idf_linked} auto-linked with IDF)" if idf_linked else ""
                msg_parts.append(f"{added_step} PCB STEP file(s){idf_note}")
            if added_idf_only:
                msg_parts.append(f"{added_idf_only} IDF-only PCB job(s)")
            self._log_panel.append_log(
                f"Added {' + '.join(msg_parts)} — {len(self._items)} item(s) queued."
            )
        if ignored:
            self._log_panel.append_log(
                f"Ignored {ignored} item(s): unsupported type, missing, or already queued."
            )
        self._update_controls()

    def queue_idf_pairs(self, pairs: Iterable[tuple[Path, Optional[Path]]]) -> int:
        """Append one or more standalone IDF-only PCB items (without a STEP file)."""
        if self._running:
            return 0

        known_idf_only = {
            os.path.normcase(str(item.bdf_path))
            for item in self._items
            if item.is_idf_only and item.bdf_path is not None
        }
        added = 0
        ignored = 0

        for raw_bdf, raw_ldf in pairs:
            try:
                bp = Path(raw_bdf).expanduser().resolve()
            except OSError:
                ignored += 1
                continue
            if not bp.is_file() or bp.suffix.lower() not in IDF_BOARD_EXTS:
                ignored += 1
                continue
            bkey = os.path.normcase(str(bp))
            if bkey in known_idf_only:
                ignored += 1
                continue
            lp: Optional[Path] = None
            if raw_ldf is not None:
                try:
                    cand_lp = Path(raw_ldf).expanduser().resolve()
                    if cand_lp.is_file():
                        lp = cand_lp
                except OSError:
                    lp = None
            if lp is None:
                lp = self._resolve_sibling_ldf(bp)

            known_idf_only.add(bkey)
            self._items.append(
                PcbBatchItem(
                    path=None,
                    bdf_path=bp,
                    ldf_path=lp,
                    idf_source="only",
                )
            )
            added += 1

        if added:
            self._config.last_open_dir = remember_last_open_dir(
                self._items[-1].source_path, self._config_path
            )
            start = len(self._items) - added
            self._refresh_tree(selected=range(start, len(self._items)))
            if added == 1:
                it = self._items[-1]
                lib_note = f" + {it.ldf_path.name}" if it.ldf_path else " (no library file)"
                self._log_panel.append_log(
                    f"Added IDF-only PCB: {it.bdf_path.name}{lib_note} — {len(self._items)} item(s) queued."
                )
            else:
                self._log_panel.append_log(
                    f"Added {added} IDF-only PCB job(s) — {len(self._items)} item(s) queued."
                )
        if ignored:
            self._log_panel.append_log(
                f"Ignored {ignored} IDF item(s): missing, invalid extension, or already queued."
            )
        self._update_controls()
        return added

    def add_files_dialog(self) -> None:
        if self._running:
            return
        init_dir = resolve_last_open_dir(self._config_path, fallback=self._config.last_open_dir)
        selected = filedialog.askopenfilenames(
            parent=self,
            title="Select PCB STEP Files",
            initialdir=init_dir,
            filetypes=[
                ("STEP Files", ("*.step", "*.stp", "*.STEP", "*.STP")),
                ("All Files", "*.*" if os.name == "nt" else "*"),
            ],
        )
        if selected:
            self.queue_files([Path(p) for p in selected])

    def attach_idf_files(
        self,
        idx: int,
        bdf_path: Optional[Path],
        ldf_path: Optional[Path] = None,
    ) -> None:
        """Manually attach (or clear) IDF .bdf/.ldf files on a queued row."""
        if not (0 <= idx < len(self._items)) or self._running:
            return
        item = self._items[idx]
        if bdf_path is None:
            if item.is_idf_only:
                return
            item.bdf_path = None
            item.ldf_path = None
            item.idf_source = "none"
        else:
            bp = Path(bdf_path).expanduser().resolve()
            lp = Path(ldf_path).expanduser().resolve() if ldf_path else None
            # Auto-find matching library file with same stem if only board file was chosen
            if lp is None:
                lp = self._resolve_sibling_ldf(bp)
            item.bdf_path = bp
            item.ldf_path = lp
            item.idf_source = "only" if item.is_idf_only else "manual"
        self._update_row(idx)

    @classmethod
    def _parse_selected_idf_pairs(cls, selected: Iterable[str | Path]) -> list[tuple[Path, Optional[Path]]]:
        """Group user-selected IDF board (.bdf/.brd/.emn) & library (.ldf/.pro/.emp) files into pairs."""
        bdfs: list[Path] = []
        ldfs: list[Path] = []
        for raw in selected:
            p = Path(raw)
            ext = p.suffix.lower()
            if ext in IDF_BOARD_EXTS:
                bdfs.append(p)
            elif ext in IDF_LIB_EXTS:
                ldfs.append(p)

        pairs: list[tuple[Path, Optional[Path]]] = []
        if len(bdfs) == 1 and len(ldfs) <= 1:
            pairs.append((bdfs[0], ldfs[0] if ldfs else None))
        elif len(bdfs) >= 1:
            lib_by_stem: dict[tuple[str, str], Path] = {
                (os.path.normcase(str(lp.parent)), lp.stem.lower()): lp for lp in ldfs
            }
            for bp in bdfs:
                lp = lib_by_stem.get((os.path.normcase(str(bp.parent)), bp.stem.lower()))
                pairs.append((bp, lp))
        elif ldfs:
            seen: set[str] = set()
            for lp in ldfs:
                bp = cls._resolve_sibling_bdf(lp)
                if bp is not None:
                    bkey = os.path.normcase(str(bp))
                    if bkey not in seen:
                        seen.add(bkey)
                        pairs.append((bp, lp))
        return pairs

    def add_idf_dialog(self) -> None:
        """Open file dialog to add standalone IDF-only PCB model(s) or attach IDF to an unlinked STEP row."""
        self.attach_idf_dialog(force_attach=False)

    def attach_idf_dialog(self, *, force_attach: bool = False) -> None:
        """Select IDF (.bdf/.ldf) files to attach to a selected row or queue as standalone IDF-only PCB(s)."""
        if self._running:
            return
        sel = [int(iid) for iid in self._tree.selection() if 0 <= int(iid) < len(self._items)]
        attach_idx: Optional[int] = None
        if len(sel) == 1:
            cand_item = self._items[sel[0]]
            if force_attach:
                attach_idx = sel[0]
            elif cand_item.path is not None and cand_item.bdf_path is None:
                attach_idx = sel[0]

        if attach_idx is not None and self._items[attach_idx].source_path.parent.is_dir():
            init_dir = str(self._items[attach_idx].source_path.parent)
            dlg_title = f"Select IDF Files (.bdf / .ldf) for {self._items[attach_idx].display_name}"
        else:
            init_dir = resolve_last_open_dir(self._config_path, fallback=self._config.last_open_dir)
            dlg_title = "Select IDF Files (.bdf / .ldf)"

        selected = filedialog.askopenfilenames(
            parent=self,
            title=dlg_title,
            initialdir=init_dir,
            filetypes=[
                ("IDF Files", ("*.bdf", "*.ldf", "*.brd", "*.pro", "*.emn", "*.emp")),
                ("IDF Board Files", ("*.bdf", "*.brd", "*.emn")),
                ("IDF Library Files", ("*.ldf", "*.pro", "*.emp")),
                ("All Files", "*.*" if os.name == "nt" else "*"),
            ],
        )
        if not selected:
            return

        pairs = self._parse_selected_idf_pairs(selected)
        if not pairs:
            messagebox.showwarning(
                "IDF Board File Required",
                "Please select an IDF board file (.bdf, .brd, or .emn), optionally together with its .ldf library file.",
                parent=self,
            )
            return

        if attach_idx is not None and len(pairs) == 1:
            bdf, ldf = pairs[0]
            item = self._items[attach_idx]
            self.attach_idf_files(attach_idx, bdf, ldf)
            self._config.last_open_dir = remember_last_open_dir(bdf, self._config_path)
            action = "Updated IDF on" if item.is_idf_only else "Attached IDF to"
            self._log_panel.append_log(
                f"{action} {item.display_name}: {item.bdf_path.name}"
                + (f" + {item.ldf_path.name}" if item.ldf_path else " (no library file)")
            )
            return

        self.queue_idf_pairs(pairs)

    def _clear_selected_idf(self) -> None:
        if self._running:
            return
        skipped_only = 0
        for iid in self._tree.selection():
            idx = int(iid)
            if 0 <= idx < len(self._items):
                if self._items[idx].is_idf_only:
                    skipped_only += 1
                    continue
                self.attach_idf_files(idx, None, None)
        if skipped_only:
            self._log_panel.append_log(
                "IDF-only row(s) cannot clear their IDF file — use Remove to delete the row."
            )

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
        self._log_panel.append_log(f"Cleared {count} file(s) from the PCB queue.")

    # ------------------------------------------------------------------
    # Viewer & Context Menu
    # ------------------------------------------------------------------

    def _on_tree_right_click(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(int(event.y))
        if iid:
            if iid not in self._tree.selection():
                self._tree.selection_set((iid,))
            try:
                self._ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._ctx_menu.grab_release()

    def _on_tree_double_click(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(int(event.y))
        if not iid:
            return
        try:
            idx = int(iid)
        except ValueError:
            return
        self.open_output_file(idx)

    def _open_selected_original(self) -> None:
        sel = [int(iid) for iid in self._tree.selection()]
        if not sel or not (0 <= sel[0] < len(self._items)):
            return
        item = self._items[sel[0]]
        src = item.path
        if src is None:
            self._log_panel.append_log(
                f"No original STEP file for IDF-only item {item.display_name}."
            )
            return
        if not src.is_file():
            return
        try:
            self._log_panel.append_log(launch_external_viewer(src, self._config))
        except Exception as exc:
            messagebox.showerror("STEP Viewer Error", str(exc), parent=self)

    def _open_selected_output(self) -> None:
        sel = [int(iid) for iid in self._tree.selection()]
        if sel:
            self.open_output_file(sel[0])

    def open_output_file(self, idx: int) -> None:
        if not (0 <= idx < len(self._items)):
            return
        item = self._items[idx]
        output = item.output_path
        if output is None or not Path(output).is_file():
            self._log_panel.append_log(
                f"No simplified PCB STEP file is available yet for {item.display_name}."
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
        self._sync_config_to_controls()
        self._pcb_cfg = build_pcb_simplify_config(self._config)
        self._refresh_config_summary()
        self._log_panel.append_log(f"Reloaded config.json: {self._config_path}")

    # ------------------------------------------------------------------
    # Multi-Worker PCB Batch Orchestration
    # ------------------------------------------------------------------

    def run(self) -> None:
        if self._running:
            return
        if not self._items:
            self._log_panel.set_progress(
                0.0, "Nothing to do — add at least one PCB STEP or IDF file first."
            )
            self._log_panel.append_log("RUN ignored — no PCB STEP or IDF files are queued.")
            return

        selected = sorted(int(iid) for iid in self._tree.selection())
        targets = [i for i in selected if 0 <= i < len(self._items)]
        if not targets:
            targets = list(range(len(self._items)))
        targets = [i for i in targets if self._items[i].source_path.is_file()]
        if not targets:
            self._log_panel.set_progress(0.0, "Selected files are missing on disk.")
            self._log_panel.append_log("RUN ignored — the selected file(s) no longer exist.")
            return

        # Reload base config.json, then apply live GUI PCB spinbox inputs and persist them
        self._config = load_config(self._config_path)
        self._sync_controls_to_config()
        save_config(self._config, self._config_path)
        self._pcb_cfg = build_pcb_simplify_config(self._config)
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
        self._batch_concurrency = resolve_batch_workers(self._config.batch_workers, self._total)
        self._stop_requested = False
        self._running = True

        self._refresh_tree(selected=targets)
        self._update_controls()
        self._log_panel.set_busy(True)
        self._log_panel.set_progress(
            0.0,
            f"Starting PCB batch of {self._total} file(s) ({self._batch_concurrency} worker(s))...",
        )
        self._log_panel.append_log(
            f"PCB RUN — {self._total} file(s) · workers={self._batch_concurrency} "
            f"(threads/worker={threads_per_batch_worker(self._batch_concurrency)}) · "
            f"hole<{self._pcb_cfg.min_board_hole_mm:g}mm · "
            f"exclude<={self._pcb_cfg.exclude_small_part_mm:g}mm · "
            f"minL={self._pcb_cfg.min_color_brightness:.2f} · "
            f"schema={self._pcb_cfg.step_schema}"
        )
        self._advance()

    def _active_slots(self) -> list[_PcbWorkerSlot]:
        return [s for s in self._slots if s.item_idx is not None]

    def _clear_slot(self, slot: _PcbWorkerSlot) -> None:
        slot.item_idx = None
        slot.cmd_id = None
        slot.file_progress = 0.0

    def ensure_worker(self) -> CadWorkerClient:
        """Create the primary persistent CAD worker process lazily on first tab visit or RUN."""
        if not self._slots:
            self._log_panel.append_log("Starting background CAD worker process (PCB Mode)...")
            client = CadWorkerClient()
            self._slots.append(_PcbWorkerSlot(slot_id=0, client=client))
            self._worker = client
            if self._poll_after_id is None:
                self._poll_after_id = self.after(40, self._poll_worker)
        return self._slots[0].client

    def _ensure_worker_pool(self, count: int) -> None:
        count = max(1, int(count))
        budget = threads_per_batch_worker(count)
        self.ensure_worker()
        for slot in self._slots[:count]:
            if slot.item_idx is None:
                slot.client.set_max_threads(budget)
        while len(self._slots) < count:
            sid = len(self._slots)
            self._log_panel.append_log(
                f"Starting parallel PCB CAD worker #{sid + 1} (thread budget={budget})..."
            )
            client = CadWorkerClient(max_threads=budget)
            self._slots.append(_PcbWorkerSlot(slot_id=sid, client=client))
        if self._poll_after_id is None:
            self._poll_after_id = self.after(40, self._poll_worker)

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
            item = self._items[idx]
            item.status = ST_RUNNING
            out_path = default_pcb_output_path(item.source_path)
            item.output_path = out_path
            self._update_row(idx)
            worker_tag = f" [W{slot.slot_id + 1}]" if pool_size > 1 else ""
            if item.is_idf_only:
                self._log_panel.append_log(
                    f"[{self._started_count}/{self._total}] {item.display_name} ({item.idf_label}){worker_tag}"
                )
            else:
                idf_note = f" + IDF ({item.idf_label})" if item.bdf_path else ""
                self._log_panel.append_log(
                    f"[{self._started_count}/{self._total}] {item.display_name}{idf_note}{worker_tag}"
                )
            slot.item_idx = idx
            slot.file_progress = 0.0
            slot.cmd_id = slot.client.submit(
                "pcb_simplify",
                {
                    "step_path": str(item.path) if item.path is not None else None,
                    "output_path": str(out_path),
                    "bdf_path": str(item.bdf_path) if item.bdf_path else None,
                    "ldf_path": str(item.ldf_path) if item.ldf_path else None,
                    "config": self._pcb_cfg,
                },
            )

    def _on_progress(self, fraction: float, message: str, slot: _PcbWorkerSlot) -> None:
        slot.file_progress = max(0.0, min(1.0, float(fraction)))
        active = self._active_slots()
        in_flight = sum(s.file_progress for s in active)
        overall = (self._done_count + in_flight) / max(1, self._total)
        idx = slot.item_idx
        name = self._items[idx].display_name if idx is not None else ""
        if len(active) > 1:
            prefix = f"[{self._done_count}/{self._total} done · {len(active)} active]"
        else:
            prefix = f"[{min(self._done_count + 1, self._total)}/{self._total}]"
        self._log_panel.set_progress(overall, f"{prefix} {name} — {message}")

    def stop(self) -> None:
        if not self._running or self._stop_requested:
            return
        self._stop_requested = True
        for slot in self._slots:
            if slot.item_idx is not None:
                slot.client.request_cancel()
        self._update_controls()
        self._log_panel.append_log(
            "Stop requested — aborting active PCB file(s); remaining files will be skipped."
        )

    def _on_force_stop(self) -> None:
        if not self._running:
            return
        self._stop_requested = True
        for slot in self._active_slots():
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
                    f"■ Force stopped: {item.display_name} (background CAD worker restarted)"
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
            self._clear_slot(slot)

        for item in self._items:
            if item.status == ST_RUNNING:
                item.status = ST_CANCELLED

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
            f"PCB batch {'stopped' if stopped else 'finished'} — " + ", ".join(bits) + "."
        )
        self._log_panel.set_progress(completed_fraction, summary)
        self._log_panel.append_log(summary)

    # ------------------------------------------------------------------
    # Worker Event Loop
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
                    self._log_panel.append_log(f"✔ Background PCB CAD worker{tag} ready")
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
                    self._handle_done(res, slot=slot)
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

    def _handle_done(self, res: dict[str, Any], slot: _PcbWorkerSlot) -> None:
        idx = slot.item_idx
        self._clear_slot(slot)
        if idx is None or not (0 <= idx < len(self._items)):
            if not self._active_slots():
                self._finish_batch(stopped=self._stop_requested)
            return

        item = self._items[idx]
        saved = Path(res["output_path"])
        self._config.last_save_dir = remember_last_save_dir(saved, self._config_path)
        orig_mb = res.get("orig_size_bytes", 0) / (1024 * 1024)
        new_mb = res.get("size_bytes", 0) / (1024 * 1024)
        size_pct = (1.0 - new_mb / max(1e-9, orig_mb)) * 100.0 if orig_mb > 0 else 0.0

        item.output_path = saved
        item.status = ST_DONE
        self._done_count += 1
        self._ok_count += 1
        self._update_row(idx)

        prefix = f"    [{item.display_name}] " if self._batch_concurrency > 1 else "    "
        for line in res.get("logs", []):
            self._log_panel.append_log(f"{prefix}{line}")
        if res.get("idf_only") or item.is_idf_only:
            size_bytes = res.get("size_bytes", 0)
            size_lbl = (
                f"{new_mb:.2f} MB" if new_mb >= 0.1 else f"{size_bytes / 1024:.1f} KB"
            )
            self._log_panel.append_log(f"{prefix}Saved (from IDF): {saved.name} ({size_lbl})")
        else:
            self._log_panel.append_log(
                f"{prefix}Saved: {saved.name} ({orig_mb:.2f} MB -> {new_mb:.2f} MB, -{size_pct:.1f}%)"
            )

        if self._stop_requested:
            if not self._active_slots():
                self._finish_batch(stopped=True)
        else:
            self._advance()

    def _handle_cancelled(self, message: str, slot: _PcbWorkerSlot) -> None:
        idx = slot.item_idx
        self._clear_slot(slot)
        if idx is not None and 0 <= idx < len(self._items):
            self._items[idx].status = ST_CANCELLED
            self._items[idx].output_path = None
            self._update_row(idx)
        self._log_panel.append_log(f"■ Cancelled: {message}")
        if not self._active_slots():
            self._finish_batch(stopped=True)

    def _handle_failure(
        self, message: str, crashed: bool, slot: _PcbWorkerSlot
    ) -> None:
        idx = slot.item_idx
        self._clear_slot(slot)
        if idx is not None and 0 <= idx < len(self._items):
            item = self._items[idx]
            item.status = ST_FAILED
            item.output_path = None
            self._done_count += 1
            self._failed_count += 1
            self._update_row(idx)
            self._log_panel.append_log(f"✖ Failed: {item.display_name} — {message}")
        if crashed:
            self._log_panel.append_log(
                "    Background PCB CAD worker crashed and was restarted — continuing with the next file."
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
        """Stop polling and cleanly terminate all PCB CAD worker processes."""
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
