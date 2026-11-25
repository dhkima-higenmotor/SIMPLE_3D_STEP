"""Main Modern Flat Tkinter / ttk window backed by a separate CAD worker process."""

from __future__ import annotations

import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

import numpy as np

from simple_3d_step.config import (
    AppConfig,
    default_viewer_label,
    ensure_config_file,
    get_default_config_path,
    launch_external_viewer,
    load_config,
    remember_last_open_dir,
    remember_last_save_dir,
    resolve_last_open_dir,
    resolve_last_save_dir,
    save_config,
)
from simple_3d_step.core.worker import CadWorkerClient
from simple_3d_step.gui.batch_panel import BatchPanel
from simple_3d_step.gui.busy_banner import BusyBanner
from simple_3d_step.gui.control_panel import ControlPanel
from simple_3d_step.gui.log_panel import LogPanel
from simple_3d_step.gui.part_tree import PartTreePanel
from simple_3d_step.gui.pcb_panel import PcbPanel
from simple_3d_step.gui.settings_dialog import SettingsDialog
from simple_3d_step.gui.theme import (
    COLORS,
    FONTS,
    STEP_STYLES,
    apply_modern_flat_theme,
    resolve_ui_font_family,
    update_ui_font,
)
from simple_3d_step.models import AssemblyModel, SimplifyResult


BASE_TITLE = "SIMPLE 3D STEP — Internal Part Removal & Exact B-Rep Solidifier"
STEP_FILETYPES = [
    ("STEP Files", ("*.step", "*.stp", "*.STEP", "*.STP")),
    ("All Files", "*.*" if os.name == "nt" else "*"),
]


class Simple3DStepApp(tk.Tk):
    """Desktop GUI for removing internal STEP parts and creating a solid outer body."""

    def __init__(
        self,
        initial_step: Optional[Path] = None,
        initial_files: Optional[list[Path]] = None,
        start_mode: str = "single",
    ) -> None:
        super().__init__()
        self._set_window_icon()
        self.title(BASE_TITLE)

        self._config_path = get_default_config_path()
        bootstrapped_from = ensure_config_file(self._config_path)
        self._config: AppConfig = load_config(self._config_path)

        self._start_mode = start_mode if start_mode in ("single", "batch", "pcb") else "single"
        self._initial_files: list[Path] = [Path(p) for p in (initial_files or [])]

        # Metadata-only assembly/result in GUI process; real OCCT shapes live in worker process
        self._assembly: Optional[AssemblyModel] = None
        self._result: Optional[SimplifyResult] = None
        self._temp_preview_dir = Path(tempfile.mkdtemp(prefix="simple_3d_step_preview_"))

        # Separate OS process for GIL-free OCCT / Embree execution
        self._worker = CadWorkerClient()
        self._busy = False
        self._active_cmd_id: Optional[int] = None
        self._active_task_name: str = ""
        self._active_on_success: Optional[Callable[[Any], None]] = None
        self._pending_initial_step: Optional[Path] = (
            initial_step
            if (self._start_mode == "single" and initial_step and initial_step.exists())
            else None
        )

        # Right-hand parameter panel is hidden by default for a clean beginner-friendly UI
        self._params_visible_var = tk.BooleanVar(value=False)

        # Workflow step completion state ("idle" | "running" | "done")
        self._step_states: dict[str, str] = {
            "open": "idle",
            "analyze": "idle",
            "simplify": "idle",
            "save": "idle",
        }
        self._step_buttons: dict[str, tuple[tk.Button, str]] = {}
        self._step_borders: dict[str, tk.Frame] = {}
        self._active_step_key: Optional[str] = None
        self._active_step_prev_state: str = "idle"

        apply_modern_flat_theme(
            self,
            font_family=self._config.ui_font_family,
            font_size=self._config.ui_font_size,
        )
        self._build_layout()
        self._apply_optimal_window_geometry()
        self._bind_shortcuts()
        self._update_ui_state()

        # Pre-fill the active multi-file queue with any files passed on the command line
        # (run_simple_3d_step.bat / .sh drag & drop or CLI args support several files at once)
        if self._start_mode == "pcb":
            self._pcb_panel.queue_files(self._initial_files)
        else:
            self._batch_panel.queue_files(self._initial_files)
        self._select_mode(self._start_mode)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_after_id: Optional[str] = self.after(40, self._poll_worker)

        viewer_info = self._config.viewer_executable or default_viewer_label()
        active_font = resolve_ui_font_family(self, preferred=self._config.ui_font_family)
        if bootstrapped_from is not None:
            self._log_panel.append_log(
                f"Created {self._config_path.name} from {bootstrapped_from.name}"
            )
        self._log_panel.append_log(f"Loaded config: {self._config_path}")
        self._log_panel.append_log(
            f"External 3D viewer: {viewer_info} · UI Font: {active_font} {self._config.ui_font_size}pt"
        )
        self._log_panel.append_log("Starting background CAD worker process (freeze-free mode)...")

    def _apply_optimal_window_geometry(self, expand_only: bool = False) -> None:
        """Size the window cleanly to fit all 3 tabs and toolbars without clipping at any UI font size."""
        self.update_idletasks()
        scr_w = max(800, self.winfo_screenwidth())
        scr_h = max(600, self.winfo_screenheight())
        max_w = min(1140, max(820, scr_w - 80))
        max_h = min(740, max(580, scr_h - 100))

        req_w = max(
            self.winfo_reqwidth(),
            self._batch_panel.winfo_reqwidth(),
            self._pcb_panel.winfo_reqwidth(),
        )
        req_h = max(
            self.winfo_reqheight(),
            self._batch_panel.winfo_reqheight(),
            self._pcb_panel.winfo_reqheight(),
        )

        target_w = min(max_w, max(1020, req_w + 32))
        target_h = min(max_h, max(680, req_h + 24))

        if expand_only:
            cur_w = self.winfo_width()
            cur_h = self.winfo_height()
            target_w = max(cur_w, target_w)
            target_h = max(cur_h, target_h)

        self.geometry(f"{target_w}x{target_h}")
        self.minsize(min(900, target_w), min(580, target_h))

    def _set_window_icon(self) -> None:
        """Apply the packaged app icon across Windows (.ico) and Linux/macOS (.png)."""
        assets_dir = Path(__file__).with_name("assets")
        ico_icon = assets_dir / "app.ico"
        png_icon = assets_dir / "app.png"

        if sys.platform == "win32" and ico_icon.is_file():
            try:
                # Rounded tiles look best as the window/taskbar icon on every toplevel
                self.iconbitmap(str(ico_icon))
                self.iconbitmap(default=str(ico_icon))
                return
            except tk.TclError:
                pass

        if png_icon.is_file():
            try:
                # Keep a strong reference so Tkinter PhotoImage is not garbage-collected
                self._icon_photo = tk.PhotoImage(file=str(png_icon))
                self.iconphoto(True, self._icon_photo)
                return
            except tk.TclError:
                pass

        if ico_icon.is_file():
            try:
                self.iconbitmap(str(ico_icon))
            except tk.TclError:
                pass

    def _bind_shortcuts(self) -> None:
        # Shortcuts are routed to whichever mode tab is currently active
        self.bind_all("<Control-o>", lambda _e: self._on_shortcut_open())
        self.bind_all("<Control-s>", lambda _e: self._on_shortcut_save())
        self.bind_all("<F5>", lambda _e: self._on_shortcut_run())
        self.bind_all("<Escape>", lambda _e: self._on_shortcut_stop())

    # --- Mode tabs (Single / Batch / PCB) ---

    def _current_mode(self) -> str:
        """Return the active mode tab key: ``"single"``, ``"batch"``, or ``"pcb"``."""
        try:
            current = self._notebook.select()
        except tk.TclError:
            return "single"
        if str(current) == str(self._batch_panel):
            return "batch"
        if str(current) == str(self._pcb_panel):
            return "pcb"
        return "single"

    def _select_mode(self, mode: str) -> None:
        """Switch to the Single, Batch, or PCB mode tab."""
        if mode == "batch":
            page = self._batch_panel
        elif mode == "pcb":
            page = self._pcb_panel
        else:
            page = self._single_tab
        self._notebook.select(page)
        self._on_mode_changed()

    def _on_mode_changed(self, _event: Optional[tk.Event] = None) -> None:
        # Warm up the multi-file CAD worker lazily as soon as its tab is opened
        mode = self._current_mode()
        if mode == "batch":
            self._batch_panel.ensure_worker()
        elif mode == "pcb":
            self._pcb_panel.ensure_worker()
        self._sync_mode_tabs()

    def _sync_mode_tabs(self) -> None:
        """Reflect busy / running state in the notebook tab labels and window title."""
        single_text = "  ● Single Mode  " if self._busy else "  Single Mode  "
        batch = self._batch_panel
        batch_text = (
            f"  ● Batch Mode ({batch.completed}/{batch.total})  "
            if batch.running
            else "  Batch Mode  "
        )
        pcb = self._pcb_panel
        pcb_text = (
            f"  ● PCB Mode ({pcb.completed}/{pcb.total})  "
            if pcb.running
            else "  PCB Mode  "
        )
        for idx, text in enumerate((single_text, batch_text, pcb_text)):
            if self._notebook.tab(idx, "text") != text:
                self._notebook.tab(idx, text=text)

        if self._busy:
            return
        mode = self._current_mode()
        if batch.running and mode == "batch":
            self.title(f"[{batch.completed + 1}/{batch.total}] Batch processing — SIMPLE 3D STEP")
        elif pcb.running and mode == "pcb":
            self.title(f"[{pcb.completed + 1}/{pcb.total}] PCB processing — SIMPLE 3D STEP")
        else:
            self.title(BASE_TITLE)

    def _on_shortcut_open(self) -> None:
        mode = self._current_mode()
        if mode == "batch":
            self._batch_panel.add_files_dialog()
        elif mode == "pcb":
            self._pcb_panel.add_files_dialog()
        else:
            self._on_open_step()

    def _on_shortcut_save(self) -> None:
        if self._current_mode() == "single":
            self._on_save_result()

    def _on_shortcut_run(self) -> None:
        mode = self._current_mode()
        if mode == "batch":
            self._batch_panel.run()
        elif mode == "pcb":
            self._pcb_panel.run()

    def _on_shortcut_stop(self) -> None:
        mode = self._current_mode()
        if mode == "batch":
            self._batch_panel.stop()
        elif mode == "pcb":
            self._pcb_panel.stop()

    def _make_step_button(
        self,
        parent: tk.Misc,
        step_key: str,
        label: str,
        command: Callable[[], None],
    ) -> tuple[tk.Frame, tk.Button]:
        """Create a crisp 1px-bordered Modern Flat workflow step button with bold typography."""
        border_wrap = tk.Frame(parent, background=STEP_STYLES["idle"]["border"], padx=1, pady=1)
        btn = tk.Button(
            border_wrap,
            text=label,
            command=command,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=4,
            cursor="hand2",
            disabledforeground=COLORS["text_muted"],
            font=FONTS["base_bold"],
        )
        btn.pack(fill=tk.BOTH, expand=True)

        def _on_enter(_e: tk.Event) -> None:
            if str(btn.cget("state")) == tk.NORMAL:
                spec = STEP_STYLES[self._step_states[step_key]]
                btn.configure(background=spec["hover_bg"])

        def _on_leave(_e: tk.Event) -> None:
            spec = STEP_STYLES[self._step_states[step_key]]
            btn.configure(background=spec["bg"])

        btn.bind("<Enter>", _on_enter)
        btn.bind("<Leave>", _on_leave)

        self._step_buttons[step_key] = (btn, label)
        self._step_borders[step_key] = border_wrap
        self._apply_step_style(step_key, "idle")
        return border_wrap, btn

    def _apply_step_style(self, step_key: str, state: str) -> None:
        self._step_states[step_key] = state
        entry = self._step_buttons.get(step_key)
        if entry is None:
            return
        btn, base_label = entry
        spec = STEP_STYLES[state]
        btn.configure(
            text=f"{spec['prefix']}{base_label}",
            background=spec["bg"],
            foreground=spec["fg"],
            disabledforeground=spec.get("disabled_fg", COLORS["text_muted"]),
            activebackground=spec["hover_bg"],
            activeforeground=spec["fg"],
            font=spec["font"],
        )
        border_wrap = self._step_borders.get(step_key)
        if border_wrap is not None:
            border_wrap.configure(background=spec["border"])

    def _reset_steps_from(self, step_keys: tuple[str, ...]) -> None:
        for key in step_keys:
            self._apply_step_style(key, "idle")

    def _build_layout(self) -> None:
        # Mode tabs: [Single Mode] / [Batch Mode] / [PCB Mode]
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill=tk.BOTH, expand=True)

        self._single_tab = tk.Frame(self._notebook, background=COLORS["app_bg"])
        self._batch_panel = BatchPanel(self._notebook, config_path=self._config_path)
        self._pcb_panel = PcbPanel(self._notebook, config_path=self._config_path)

        self._notebook.add(self._single_tab, text="  Single Mode  ")
        self._notebook.add(self._batch_panel, text="  Batch Mode  ")
        self._notebook.add(self._pcb_panel, text="  PCB Mode  ")
        self._notebook.bind("<<NotebookTabChanged>>", self._on_mode_changed)

        # 1. Elevated white top header card split cleanly into 2 compact rows for large fonts
        header_card = tk.Frame(self._single_tab, background=COLORS["surface"])
        header_card.pack(side=tk.TOP, fill=tk.X)

        # Row 1: Primary 4-step workflow buttons
        self._toolbar_row1 = tk.Frame(header_card, background=COLORS["surface"])
        self._toolbar_row1.pack(fill=tk.X, padx=8, pady=(5, 3))

        def _add_chevron() -> None:
            tk.Label(
                self._toolbar_row1,
                text="›",
                background=COLORS["surface"],
                foreground=COLORS["text_muted"],
                font=FONTS["chevron"],
                padx=4,
            ).pack(side=tk.LEFT)

        w_open, self._tb_open = self._make_step_button(
            self._toolbar_row1, "open", "Open STEP", self._on_open_step
        )
        w_open.pack(side=tk.LEFT)
        _add_chevron()

        w_analyze, self._tb_analyze = self._make_step_button(
            self._toolbar_row1, "analyze", "1. Analyze Parts", self._on_analyze
        )
        w_analyze.pack(side=tk.LEFT)
        _add_chevron()

        w_simplify, self._tb_simplify = self._make_step_button(
            self._toolbar_row1, "simplify", "2. Convert to Solid", self._on_simplify
        )
        w_simplify.pack(side=tk.LEFT)
        _add_chevron()

        w_save, self._tb_save = self._make_step_button(
            self._toolbar_row1, "save", "Save Result", self._on_save_result
        )
        w_save.pack(side=tk.LEFT)

        # Row 2: External 3D viewer actions (left) + [Parameters] toggle & [Viewer Settings] (right)
        self._toolbar_row2 = tk.Frame(header_card, background=COLORS["surface"])
        self._toolbar_row2.pack(fill=tk.X, padx=8, pady=(1, 5))

        self._tb_view_orig = ttk.Button(
            self._toolbar_row2, text="Open Original", style="Toolbar.TButton", command=self._on_view_original
        )
        self._tb_view_orig.pack(side=tk.LEFT, padx=(0, 6))

        self._tb_view_kept = ttk.Button(
            self._toolbar_row2, text="Preview Kept Parts", style="Toolbar.TButton", command=self._on_view_kept_parts
        )
        self._tb_view_kept.pack(side=tk.LEFT, padx=(0, 6))

        self._tb_view_res = ttk.Button(
            self._toolbar_row2, text="Open Result", style="Toolbar.TButton", command=self._on_view_result
        )
        self._tb_view_res.pack(side=tk.LEFT)

        # Right side: [Parameters] toggle button directly to the left of [Settings]
        ttk.Button(
            self._toolbar_row2, text="Settings", style="Toolbar.TButton", command=self._on_open_settings
        ).pack(side=tk.RIGHT)

        self._tb_params = ttk.Checkbutton(
            self._toolbar_row2,
            text="Parameters",
            style="Toolbar.Toolbutton",
            variable=self._params_visible_var,
            command=self._toggle_params_panel,
        )
        self._tb_params.pack(side=tk.RIGHT, padx=(0, 6))

        tk.Frame(header_card, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # 2. Clean file & result summary status strip
        self._file_var = tk.StringVar(value="No file loaded — click [Open STEP] above to select a file.")
        self._result_badge_var = tk.StringVar(value="")

        self._info_bar = tk.Frame(self._single_tab, background=COLORS["surface_alt"], padx=8, pady=3)
        self._info_bar.pack(side=tk.TOP, fill=tk.X)
        tk.Label(
            self._info_bar,
            textvariable=self._file_var,
            background=COLORS["surface_alt"],
            foreground=COLORS["accent"],
            font=FONTS["small_bold"],
        ).pack(side=tk.LEFT)

        self._result_badge = tk.Label(
            self._info_bar,
            textvariable=self._result_badge_var,
            background=COLORS["surface_alt"],
            foreground=COLORS["success_text"],
            font=FONTS["small_bold"],
            padx=8,
            pady=1,
        )
        self._result_badge.pack(side=tk.RIGHT)

        tk.Frame(self._single_tab, height=1, background=COLORS["border"]).pack(side=tk.TOP, fill=tk.X)

        # 3. Bottom Log & Progress drawer — packed BEFORE main_pane so it is NEVER clipped
        self._log_panel = LogPanel(self._single_tab, on_cancel=self._on_cancel_task)
        self._log_panel.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(3, 6))

        # Dynamic animated busy banner (packed above main_pane only while busy)
        self._busy_banner = BusyBanner(
            self._single_tab,
            on_cancel=self._on_cancel_task,
            on_force_cancel=self._on_force_cancel_task,
        )

        # 4. Main PanedWindow packed LAST with expand=True so it absorbs all remaining space
        self._main_pane = ttk.PanedWindow(self._single_tab, orient=tk.HORIZONTAL)
        self._main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(5, 3))

        self._part_tree = PartTreePanel(
            self._main_pane, on_selection_changed=self._on_part_selection_modified
        )
        self._control_panel = ControlPanel(
            self._main_pane,
            app_config=self._config,
            on_analyze=self._on_analyze,
            on_simplify=self._on_simplify,
            on_save_result=self._on_save_result,
            on_open_result_viewer=self._on_view_result,
        )

        # Only show PartTree by default; ControlPanel is toggled via [Parameters] button
        self._main_pane.add(self._part_tree, weight=3)

    def _toggle_params_panel(self) -> None:
        show = bool(self._params_visible_var.get())
        panes = self._main_pane.panes()
        ctrl_id = str(self._control_panel)
        if show and ctrl_id not in panes:
            self._main_pane.add(self._control_panel, weight=1)
            self.update_idletasks()
            pane_w = self._main_pane.winfo_width()
            if pane_w > 640:
                self._main_pane.sashpos(0, max(380, pane_w - 405))
        elif not show and ctrl_id in panes:
            self._main_pane.forget(self._control_panel)

    def _set_result_badge(self, text: str, is_ok: bool = True) -> None:
        self._result_badge_var.set(text)
        if not text:
            self._result_badge.configure(
                background=COLORS["surface_alt"], foreground=COLORS["success_text"]
            )
        elif is_ok:
            self._result_badge.configure(
                background=COLORS["success_soft"], foreground=COLORS["success_text"]
            )
        else:
            self._result_badge.configure(
                background=COLORS["warning_soft"], foreground=COLORS["warning_text"]
            )

    def _update_ui_state(self) -> None:
        has_asm = self._assembly is not None and len(self._assembly.parts) > 0
        has_res = self._result is not None
        busy = self._busy

        self._tb_open.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._tb_analyze.configure(state=tk.NORMAL if (has_asm and not busy) else tk.DISABLED)
        self._tb_simplify.configure(state=tk.NORMAL if (has_asm and not busy) else tk.DISABLED)
        self._tb_save.configure(state=tk.NORMAL if (has_res and not busy) else tk.DISABLED)
        self._tb_view_orig.configure(state=tk.NORMAL if (has_asm and not busy) else tk.DISABLED)
        self._tb_view_kept.configure(state=tk.NORMAL if (has_asm and not busy) else tk.DISABLED)
        self._tb_view_res.configure(state=tk.NORMAL if (has_res and not busy) else tk.DISABLED)

        self._control_panel.set_controls_state(has_asm, has_res, busy)
        self._log_panel.set_busy(busy)

    def _show_busy_banner(self, task_name: str) -> None:
        self._busy_banner.start(task_name)
        if not self._busy_banner.winfo_ismapped():
            self._busy_banner.pack(
                side=tk.TOP, fill=tk.X, padx=8, pady=(5, 0), before=self._main_pane
            )

    def _hide_busy_banner(self) -> None:
        self._busy_banner.stop()
        if self._busy_banner.winfo_ismapped():
            self._busy_banner.pack_forget()
        self.title(BASE_TITLE)

    # --- Subprocess Background Orchestration ---

    def _collect_overrides(self) -> dict[int, tuple[bool, bool]]:
        if self._assembly is None:
            return {}
        return {p.part_id: (p.keep, p.user_overridden) for p in self._assembly.parts}

    def _on_part_selection_modified(self) -> None:
        if self._step_states["simplify"] == "done" or self._step_states["save"] == "done":
            self._reset_steps_from(("simplify", "save"))

    def _submit_worker_task(
        self,
        task_name: str,
        cmd_name: str,
        payload: dict[str, Any],
        on_success: Callable[[Any], None],
        step_key: Optional[str] = None,
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._active_task_name = task_name
        self._active_on_success = on_success
        self._active_step_key = step_key
        if step_key is not None:
            self._active_step_prev_state = self._step_states.get(step_key, "idle")
            self._apply_step_style(step_key, "running")
        self._active_cmd_id = self._worker.submit(cmd_name, payload)

        self._show_busy_banner(task_name)
        self._log_panel.set_progress(0.0, f"{task_name} in progress...")
        self._log_panel.append_log(f"▶ Started: {task_name} (background CAD process)")
        self._update_ui_state()

    def _on_cancel_task(self) -> None:
        if not self._busy:
            return
        self._worker.request_cancel()
        self._busy_banner.mark_cancelling()
        self._log_panel.append_log(
            "Cancel requested — waiting for current C++ step (click [Force Stop] above to abort immediately)"
        )

    def _on_force_cancel_task(self) -> None:
        if not self._busy:
            return
        task_name = self._active_task_name
        self._worker.force_terminate_and_restart()
        self._busy = False
        self._active_cmd_id = None
        self._active_on_success = None
        self._active_step_key = None
        self._hide_busy_banner()

        self._assembly = None
        self._result = None
        self._part_tree.set_parts([])
        self._control_panel.update_result(None)
        self._set_result_badge("")
        self._reset_steps_from(("open", "analyze", "simplify", "save"))
        self._file_var.set("CAD engine restarted after force stop — please open the STEP file again.")
        self._log_panel.set_progress(0.0, f"{task_name} force stopped")
        self._log_panel.append_log(
            f"■ Force stopped: {task_name} (background CAD worker restarted)"
        )
        self._update_ui_state()

    def _poll_worker(self) -> None:
        self._poll_after_id = None
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._sync_mode_tabs()
        if self._busy:
            self.title(
                f"[Working {self._busy_banner.elapsed_formatted}] {self._active_task_name} — SIMPLE 3D STEP"
            )

        for ev in self._worker.poll_events():
            if ev.kind == "worker_ready":
                self._log_panel.append_log("✔ Background CAD worker ready")
                if self._pending_initial_step is not None and not self._busy:
                    path = self._pending_initial_step
                    self._pending_initial_step = None
                    self._load_step_file(path)

            elif ev.kind == "progress":
                cmd_id, frac, msg = ev.payload
                if cmd_id == self._active_cmd_id:
                    self._busy_banner.update_progress(frac, msg)
                    self._log_panel.set_progress(frac, msg)

            elif ev.kind == "done":
                cmd_id, _cmd_name, res = ev.payload
                if cmd_id != self._active_cmd_id:
                    continue
                task_name = self._active_task_name
                cb = self._active_on_success
                step_key = self._active_step_key
                self._busy = False
                self._active_cmd_id = None
                self._active_on_success = None
                self._active_step_key = None
                self._hide_busy_banner()
                self._log_panel.set_progress(1.0, f"{task_name} completed")
                self._log_panel.append_log(f"✔ Completed: {task_name}")
                if step_key is not None:
                    self._apply_step_style(step_key, "done")
                if cb:
                    cb(res)
                self._update_ui_state()

            elif ev.kind == "cancelled":
                cmd_id, _cmd_name, msg = ev.payload
                if cmd_id != self._active_cmd_id:
                    continue
                task_name = self._active_task_name
                step_key = self._active_step_key
                prev_state = self._active_step_prev_state
                self._busy = False
                self._active_cmd_id = None
                self._active_on_success = None
                self._active_step_key = None
                self._hide_busy_banner()
                if step_key is not None:
                    self._apply_step_style(step_key, prev_state)
                self._log_panel.set_progress(0.0, f"{task_name} cancelled")
                self._log_panel.append_log(f"■ Cancelled: {task_name} — {msg}")
                self._update_ui_state()

            elif ev.kind in ("error", "crashed"):
                cmd_id = ev.payload[0]
                if cmd_id != self._active_cmd_id:
                    continue
                task_name = self._active_task_name
                err_msg = ev.payload[2]
                step_key = self._active_step_key
                prev_state = self._active_step_prev_state
                self._busy = False
                self._active_cmd_id = None
                self._active_on_success = None
                self._active_step_key = None
                self._hide_busy_banner()
                if ev.kind == "crashed":
                    self._reset_steps_from(("open", "analyze", "simplify", "save"))
                elif step_key is not None:
                    self._apply_step_style(step_key, prev_state)
                self._log_panel.set_progress(0.0, f"{task_name} failed")
                self._log_panel.append_log(f"✖ Failed: {task_name} — {err_msg}")
                if ev.kind == "crashed":
                    self._assembly = None
                    self._result = None
                    self._part_tree.set_parts([])
                    self._control_panel.update_result(None)
                    self._set_result_badge("")
                self._update_ui_state()
                messagebox.showerror(f"{task_name} Error", err_msg, parent=self)

        self._poll_after_id = self.after(40, self._poll_worker)

    # --- Actions ---

    def _on_open_step(self) -> None:
        if self._busy:
            return
        init_dir = resolve_last_open_dir(self._config_path, fallback=self._config.last_open_dir)
        selected = filedialog.askopenfilename(
            parent=self,
            title="Open 3D STEP File",
            initialdir=init_dir,
            filetypes=STEP_FILETYPES,
        )
        if selected:
            path = Path(selected)
            self._load_step_file(path)

    def _load_step_file(self, path: Path) -> None:
        self._config.last_open_dir = remember_last_open_dir(path, self._config_path)

        def _done(data: dict[str, Any]) -> None:
            src = Path(data["source_path"])
            asm = AssemblyModel(
                source_path=src,
                parts=data["parts"],
                unit=data["unit"],
                bbox_min=np.asarray(data["bbox_min"], dtype=np.float64),
                bbox_max=np.asarray(data["bbox_max"], dtype=np.float64),
                raw_shape=None,
            )
            self._assembly = asm
            self._result = None
            self._part_tree.set_parts(asm.parts)
            self._control_panel.update_result(None)
            self._set_result_badge("")
            self._reset_steps_from(("analyze", "simplify", "save"))
            sz = asm.bbox_max - asm.bbox_min
            size_mb = src.stat().st_size / (1024 * 1024)
            self._file_var.set(
                f"File: {src.name} ({size_mb:.2f} MB)  ·  {len(asm.parts)} parts  ·  "
                f"{sz[0]:.1f} × {sz[1]:.1f} × {sz[2]:.1f} {asm.unit}"
            )
            self._log_panel.append_log(
                f"Loaded STEP: {src.name} — {len(asm.parts)} parts, total volume {data['total_volume']:,.1f} mm³"
            )

        self._submit_worker_task(
            f"Load STEP ({path.name})",
            "load_step",
            {"path": str(path)},
            _done,
            step_key="open",
        )

    def _persist_config_preserving_shared(self) -> None:
        """Persist Single Mode settings without clobbering last_open_dir, last_save_dir,
        or PCB/Batch settings updated by other tabs during the session."""
        if self._config_path.is_file():
            disk_cfg = load_config(self._config_path)
            if disk_cfg.last_open_dir:
                self._config.last_open_dir = disk_cfg.last_open_dir
            if disk_cfg.last_save_dir:
                self._config.last_save_dir = disk_cfg.last_save_dir
            # Preserve any PCB Mode parameters saved via PcbPanel
            for fld in AppConfig.__dataclass_fields__:
                if fld.startswith("pcb_"):
                    setattr(self._config, fld, getattr(disk_cfg, fld))
        self._control_panel.sync_to_app_config(self._config)
        save_config(self._config, self._config_path)

    def _on_analyze(self) -> None:
        if self._assembly is None or self._busy:
            return
        vis_cfg = self._control_panel.build_visibility_config()
        self._persist_config_preserving_shared()

        def _done(data: dict[str, Any]) -> None:
            if self._assembly is not None:
                self._assembly.parts = data["parts"]
                self._part_tree.set_parts(self._assembly.parts)
            self._reset_steps_from(("simplify", "save"))
            self._log_panel.append_log(
                f"Visibility analysis: keep {data['external_count']} external / "
                f"drop {data['internal_count']} internal "
                f"({data['tri_count']:,} triangles)"
            )

        self._submit_worker_task(
            "Internal Part Visibility Analysis",
            "analyze_visibility",
            {"config": vis_cfg, "overrides": self._collect_overrides()},
            _done,
            step_key="analyze",
        )

    def _on_simplify(self) -> None:
        if self._assembly is None or self._busy:
            return
        kept_cnt = sum(1 for p in self._assembly.parts if p.keep)
        if kept_cnt == 0:
            messagebox.showwarning("No Parts Selected", "Please select at least one outer part to keep.", parent=self)
            return

        simp_cfg = self._control_panel.build_simplify_config()
        self._persist_config_preserving_shared()

        def _done(res: SimplifyResult) -> None:
            self._result = res
            self._control_panel.update_result(res)
            self._reset_steps_from(("save",))
            v = res.validation
            status_mark = "✔" if v.is_valid else "⚠"
            self._set_result_badge(
                f"{status_mark} Solid {v.solid_count} / Shell {v.shell_count} · "
                f"{v.face_count:,} faces · Fill +{res.filled_volume_delta:,.0f} mm³",
                is_ok=v.is_valid,
            )
            for line in res.logs:
                self._log_panel.append_log(f"  · {line}")
            self._log_panel.append_log(
                f"Validation summary: Solid={v.solid_count}, Shell={v.shell_count}, "
                f"Watertight={v.is_watertight}, Volume={v.volume:,.1f} mm³ "
                f"(Internal fill +{res.filled_volume_delta:,.1f} mm³)"
            )

        self._submit_worker_task(
            "Exact B-Rep Single Solid Conversion",
            "simplify",
            {"config": simp_cfg, "overrides": self._collect_overrides()},
            _done,
            step_key="simplify",
        )

    def _on_save_result(self) -> None:
        if self._result is None or self._busy or self._assembly is None:
            return
        init_dir = resolve_last_save_dir(
            self._config_path, fallback=str(self._assembly.source_path.parent)
        )
        default_name = f"{self._assembly.source_path.stem}_simplified.step"
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="Save Simplified STEP File",
            initialdir=init_dir,
            initialfile=default_name,
            defaultextension=".step",
            filetypes=STEP_FILETYPES,
        )
        if not selected:
            return

        out_path = Path(selected)
        self._config.last_save_dir = remember_last_save_dir(out_path, self._config_path)
        schema = self._control_panel.build_simplify_config().step_schema

        def _done(data: dict[str, Any]) -> None:
            saved_path = Path(data["output_path"])
            if self._result is not None:
                self._result.output_path = saved_path
            size_mb = data["size_bytes"] / (1024 * 1024)
            self._log_panel.append_log(f"Saved STEP: {saved_path} ({size_mb:.2f} MB)")
            messagebox.showinfo(
                "Saved",
                f"Simplified STEP file saved successfully.\n\n{saved_path}\n({size_mb:.2f} MB)",
                parent=self,
            )

        self._submit_worker_task(
            f"Save STEP ({out_path.name})",
            "save_result",
            {"output_path": str(out_path), "schema": schema},
            _done,
            step_key="save",
        )

    # --- External 3D Viewer Actions ---

    def _launch_viewer_safe(self, step_path: Path) -> None:
        try:
            msg = launch_external_viewer(step_path, self._config)
            self._log_panel.append_log(msg)
        except Exception as exc:
            messagebox.showerror("External 3D Viewer Error", str(exc), parent=self)

    def _on_view_original(self) -> None:
        if self._assembly is None:
            return
        self._launch_viewer_safe(self._assembly.source_path)

    def _on_view_kept_parts(self) -> None:
        if self._assembly is None or self._busy:
            return
        kept = [p for p in self._assembly.parts if p.keep]
        if not kept:
            messagebox.showwarning("Preview Unavailable", "No kept parts are selected.", parent=self)
            return

        preview_path = (
            self._temp_preview_dir / f"{self._assembly.source_path.stem}_kept_preview.step"
        )
        schema = self._control_panel.build_simplify_config().step_schema

        def _done(data: dict[str, Any]) -> None:
            self._launch_viewer_safe(Path(data["output_path"]))

        self._submit_worker_task(
            "Prepare Kept Parts Preview",
            "export_kept_preview",
            {
                "output_path": str(preview_path),
                "schema": schema,
                "overrides": self._collect_overrides(),
            },
            _done,
        )

    def _on_view_result(self) -> None:
        if self._result is None or self._busy or self._assembly is None:
            return
        if self._result.output_path and self._result.output_path.exists():
            self._launch_viewer_safe(self._result.output_path)
            return

        preview_path = (
            self._temp_preview_dir / f"{self._assembly.source_path.stem}_result_preview.step"
        )
        schema = self._control_panel.build_simplify_config().step_schema

        def _done(data: dict[str, Any]) -> None:
            self._launch_viewer_safe(Path(data["output_path"]))

        self._submit_worker_task(
            "Prepare Result Viewer Preview",
            "export_result_preview",
            {"output_path": str(preview_path), "schema": schema},
            _done,
        )

    def _on_open_settings(self) -> None:
        def _saved(updated: AppConfig) -> None:
            self._config = updated
            applied_family, applied_size = update_ui_font(
                self, updated.ui_font_family, updated.ui_font_size
            )
            self._apply_optimal_window_geometry(expand_only=True)
            self._batch_panel.reload_config()
            self._pcb_panel.reload_config()
            viewer = updated.viewer_executable or default_viewer_label()
            self._log_panel.append_log(
                f"Settings saved — Viewer: {viewer} · UI Font: {applied_family} {applied_size}pt"
            )

        SettingsDialog(self, self._config, self._config_path, on_saved=_saved)

    def _on_close(self) -> None:
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self._busy_banner.stop()
        self._persist_config_preserving_shared()
        self._worker.shutdown()
        self._batch_panel.shutdown()
        self._pcb_panel.shutdown()
        self.destroy()
