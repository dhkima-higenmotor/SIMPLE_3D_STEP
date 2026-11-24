"""Modern Flat collapsible right-hand parameter configuration & result summary card."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from simple_3d_step.config import AppConfig
from simple_3d_step.gui.theme import COLORS, FONTS
from simple_3d_step.models import (
    SimplifyConfig,
    SimplifyResult,
    VisibilityConfig,
)


class ControlPanel(tk.Frame):
    """Elevated white flat card with scroll-safe parameter controls and result summary."""

    def __init__(
        self,
        parent: tk.Misc,
        app_config: AppConfig,
        on_analyze: Callable[[], None],
        on_simplify: Callable[[], None],
        on_save_result: Callable[[], None],
        on_open_result_viewer: Callable[[], None],
    ) -> None:
        super().__init__(
            parent,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self._on_analyze = on_analyze
        self._on_simplify = on_simplify
        self._on_save_result = on_save_result
        self._on_open_result_viewer = on_open_result_viewer

        # Visibility variables
        self._num_views_var = tk.IntVar(value=app_config.num_views)
        self._grid_res_var = tk.IntVar(value=app_config.grid_resolution)
        self._min_vis_pct_var = tk.DoubleVar(value=app_config.min_visible_ratio_pct)
        self._min_vol_var = tk.DoubleVar(value=0.0)
        self._pass2_var = tk.BooleanVar(value=app_config.enable_pass2_verify)

        # Pure Exact B-Rep Solidification variables
        self._fill_core_var = tk.BooleanVar(value=app_config.fill_internal_core)
        self._bridge_gap_var = tk.DoubleVar(value=app_config.core_bridge_gap_mm)
        self._core_inset_var = tk.DoubleVar(value=app_config.core_inset_mm)
        self._connect_islands_var = tk.BooleanVar(value=app_config.connect_floating_parts)
        self._island_gap_var = tk.DoubleVar(value=app_config.max_island_gap_mm)
        self._remove_holes_var = tk.BooleanVar(value=app_config.remove_small_holes)
        self._max_hole_dia_var = tk.DoubleVar(value=app_config.max_hole_diameter)
        self._unify_var = tk.BooleanVar(value=app_config.unify_same_domain)
        self._largest_only_var = tk.BooleanVar(value=app_config.keep_largest_only)
        self._schema_var = tk.StringVar(value=app_config.step_schema or "AP214")

        self._result_var = tk.StringVar(value="Simplification has not been run yet.")

        self._build_ui()

    def _build_ui(self) -> None:
        # Card header strip
        header = ttk.Frame(self, style="Subtle.TFrame", padding=(10, 6))
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="Advanced Parameters",
            style="SectionTitle.TLabel",
        ).pack(side=tk.LEFT)
        tk.Frame(self, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # Vertically scrollable canvas prevents clipping when window is short
        scroll_wrap = tk.Frame(self, background=COLORS["surface"])
        scroll_wrap.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(
            scroll_wrap,
            background=COLORS["surface"],
            highlightthickness=0,
            borderwidth=0,
            width=390,
        )
        self._vscroll = ttk.Scrollbar(scroll_wrap, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vscroll.set)

        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._vscroll.pack(side=tk.RIGHT, fill=tk.Y)

        body = ttk.Frame(self._canvas, style="Card.TFrame", padding=10)
        self._body_win = self._canvas.create_window((0, 0), window=body, anchor=tk.NW)

        def _on_body_configure(_event=None) -> None:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        def _on_canvas_configure(event) -> None:
            self._canvas.itemconfigure(self._body_win, width=event.width)

        body.bind("<Configure>", _on_body_configure)
        self._canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event) -> None:
            if self._canvas.bbox("all") and self._canvas.bbox("all")[3] > self._canvas.winfo_height():
                self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._canvas.bind("<Enter>", lambda _e: self._canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self._canvas.bind("<Leave>", lambda _e: self._canvas.unbind_all("<MouseWheel>"))

        nb = ttk.Notebook(body)
        nb.pack(fill=tk.X, expand=False)

        # --- Tab 1: Visibility Analysis ---
        tab1 = ttk.Frame(nb, style="Card.TFrame", padding=10)
        nb.add(tab1, text="1. Internal Parts")
        tab1.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(tab1, text="View Preset", style="Card.TLabel").grid(row=row, column=0, sticky=tk.W, pady=3)
        preset_frame = ttk.Frame(tab1, style="Card.TFrame")
        preset_frame.grid(row=row, column=1, sticky=tk.EW, pady=3)
        ttk.Button(
            preset_frame, text="Fast(26)", style="Small.TButton", width=7,
            command=lambda: self._apply_vis_preset(26, 112),
        ).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(
            preset_frame, text="Std(64)", style="Small.TButton", width=7,
            command=lambda: self._apply_vis_preset(64, 160),
        ).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(
            preset_frame, text="Fine(162)", style="Small.TButton", width=8,
            command=lambda: self._apply_vis_preset(162, 200),
        ).pack(side=tk.LEFT)

        row += 1
        ttk.Label(tab1, text="Sphere Views", style="Card.TLabel").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Spinbox(tab1, from_=12, to=300, textvariable=self._num_views_var, width=10).grid(
            row=row, column=1, sticky=tk.EW, pady=3
        )

        row += 1
        ttk.Label(tab1, text="Ray Grid Resolution", style="Card.TLabel").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Spinbox(tab1, from_=48, to=320, increment=16, textvariable=self._grid_res_var, width=10).grid(
            row=row, column=1, sticky=tk.EW, pady=3
        )

        row += 1
        ttk.Label(tab1, text="Min Visibility (%)", style="Card.TLabel").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Spinbox(
            tab1, from_=0.0, to=20.0, increment=0.1, format="%.2f",
            textvariable=self._min_vis_pct_var, width=10,
        ).grid(row=row, column=1, sticky=tk.EW, pady=3)

        row += 1
        ttk.Label(tab1, text="Min Part Volume (mm³)", style="Card.TLabel").grid(row=row, column=0, sticky=tk.W, pady=3)
        ttk.Spinbox(
            tab1, from_=0.0, to=100000.0, increment=50.0, format="%.1f",
            textvariable=self._min_vol_var, width=10,
        ).grid(row=row, column=1, sticky=tk.EW, pady=3)

        row += 1
        ttk.Checkbutton(
            tab1,
            text="Enable Pass 2 reverse check (borderline/small parts)",
            variable=self._pass2_var,
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(6, 4))

        row += 1
        self._btn_analyze = ttk.Button(
            tab1,
            text="Step 1: Run Internal Part Detection",
            style="Primary.TButton",
            command=self._on_analyze,
            state=tk.DISABLED,
        )
        self._btn_analyze.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))

        # --- Tab 2: Pure Exact B-Rep Solidification ---
        tab2 = ttk.Frame(nb, style="Card.TFrame", padding=10)
        nb.add(tab2, text="2. Exact B-Rep Solid")
        tab2.columnconfigure(1, weight=1)

        r = 0
        ttk.Label(
            tab2,
            text="Preserves all original exterior B-Rep surfaces and fills only internal voids.",
            style="Muted.TLabel",
            foreground=COLORS["accent"],
            wraplength=340,
        ).grid(row=r, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))

        r += 1
        core_box = ttk.LabelFrame(tab2, text="Internal Solid Fill (Analytic B-Rep Core)", padding=7)
        core_box.grid(row=r, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        core_box.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            core_box,
            text="Auto-generate & fuse interior analytic core",
            variable=self._fill_core_var,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 3))

        ttk.Label(core_box, text="Internal Bridge Gap (mm)", style="Card.TLabel").grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(
            core_box, from_=0.0, to=20.0, increment=0.5, format="%.1f",
            textvariable=self._bridge_gap_var, width=10,
        ).grid(row=1, column=1, sticky=tk.EW, pady=2)

        ttk.Label(core_box, text="Exterior Safety Inset (mm)", style="Card.TLabel").grid(row=2, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(
            core_box, from_=0.5, to=15.0, increment=0.5, format="%.1f",
            textvariable=self._core_inset_var, width=10,
        ).grid(row=2, column=1, sticky=tk.EW, pady=2)

        r += 1
        ttk.Checkbutton(
            tab2,
            text="Bridge clearance-separated outer parts internally",
            variable=self._connect_islands_var,
        ).grid(row=r, column=0, columnspan=2, sticky=tk.W, pady=(2, 1))

        r += 1
        ttk.Label(tab2, text="  Max Bridge Gap (mm)", style="Card.TLabel").grid(row=r, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(
            tab2, from_=0.0, to=15.0, increment=0.5, format="%.1f",
            textvariable=self._island_gap_var, width=10,
        ).grid(row=r, column=1, sticky=tk.EW, pady=2)

        r += 1
        ttk.Checkbutton(
            tab2,
            text="Plug small outer through-holes (optional, off by default)",
            variable=self._remove_holes_var,
        ).grid(row=r, column=0, columnspan=2, sticky=tk.W, pady=(3, 1))

        r += 1
        ttk.Label(tab2, text="  Max Hole Diameter (mm)", style="Card.TLabel").grid(row=r, column=0, sticky=tk.W, pady=2)
        ttk.Spinbox(
            tab2, from_=0.0, to=100.0, increment=1.0, format="%.1f",
            textvariable=self._max_hole_dia_var, width=10,
        ).grid(row=r, column=1, sticky=tk.EW, pady=2)

        r += 1
        ttk.Checkbutton(
            tab2,
            text="Merge coplanar/co-cylindrical faces (UnifySameDomain)",
            variable=self._unify_var,
        ).grid(row=r, column=0, columnspan=2, sticky=tk.W, pady=(3, 1))

        r += 1
        ttk.Checkbutton(
            tab2,
            text="Keep largest single body if unbridgeable islands remain",
            variable=self._largest_only_var,
        ).grid(row=r, column=0, columnspan=2, sticky=tk.W, pady=1)

        r += 1
        ttk.Label(tab2, text="Output STEP Schema", style="Card.TLabel").grid(row=r, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(
            tab2, textvariable=self._schema_var, values=("AP214", "AP242", "AP203"), state="readonly"
        ).grid(row=r, column=1, sticky=tk.EW, pady=3)

        r += 1
        self._btn_simplify = ttk.Button(
            tab2,
            text="Step 2: Convert to Single B-Rep Solid",
            style="Primary.TButton",
            command=self._on_simplify,
            state=tk.DISABLED,
        )
        self._btn_simplify.grid(row=r, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))

        # --- Validation & Result Summary Card ---
        res_card = ttk.LabelFrame(body, text="Result & B-Rep Validation Summary", padding=8)
        res_card.pack(fill=tk.X, expand=False, pady=(10, 0))

        ttk.Label(
            res_card,
            textvariable=self._result_var,
            style="Card.TLabel",
            justify=tk.LEFT,
            wraplength=340,
        ).pack(fill=tk.X, anchor=tk.NW)

        btn_row = ttk.Frame(res_card, style="Card.TFrame", padding=(0, 8, 0, 0))
        btn_row.pack(fill=tk.X)
        self._btn_save = ttk.Button(
            btn_row, text="Save Result STEP...", style="Small.TButton",
            command=self._on_save_result, state=tk.DISABLED,
        )
        self._btn_save.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._btn_view_res = ttk.Button(
            btn_row, text="Open in 3D Viewer", style="Small.TButton",
            command=self._on_open_result_viewer, state=tk.DISABLED,
        )
        self._btn_view_res.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _apply_vis_preset(self, views: int, grid: int) -> None:
        self._num_views_var.set(views)
        self._grid_res_var.set(grid)

    def build_visibility_config(self) -> VisibilityConfig:
        return VisibilityConfig(
            num_views=max(12, int(self._num_views_var.get())),
            grid_resolution=max(48, int(self._grid_res_var.get())),
            enable_pass2_verify=bool(self._pass2_var.get()),
            min_visible_ratio=max(0.0, float(self._min_vis_pct_var.get()) / 100.0),
            min_part_volume_mm3=max(0.0, float(self._min_vol_var.get())),
        )

    def build_simplify_config(self) -> SimplifyConfig:
        return SimplifyConfig(
            fill_internal_core=bool(self._fill_core_var.get()),
            core_bridge_gap_mm=max(0.0, float(self._bridge_gap_var.get())),
            core_inset_mm=max(0.5, float(self._core_inset_var.get())),
            connect_floating_parts=bool(self._connect_islands_var.get()),
            max_island_gap_mm=max(0.0, float(self._island_gap_var.get())),
            remove_small_holes=bool(self._remove_holes_var.get()),
            max_hole_diameter=max(0.0, float(self._max_hole_dia_var.get())),
            unify_same_domain=bool(self._unify_var.get()),
            keep_largest_only=bool(self._largest_only_var.get()),
            step_schema=self._schema_var.get() or "AP214",
        )

    def sync_to_app_config(self, cfg: AppConfig) -> None:
        cfg.num_views = int(self._num_views_var.get())
        cfg.grid_resolution = int(self._grid_res_var.get())
        cfg.min_visible_ratio_pct = float(self._min_vis_pct_var.get())
        cfg.enable_pass2_verify = bool(self._pass2_var.get())
        cfg.fill_internal_core = bool(self._fill_core_var.get())
        cfg.core_bridge_gap_mm = float(self._bridge_gap_var.get())
        cfg.core_inset_mm = float(self._core_inset_var.get())
        cfg.connect_floating_parts = bool(self._connect_islands_var.get())
        cfg.max_island_gap_mm = float(self._island_gap_var.get())
        cfg.remove_small_holes = bool(self._remove_holes_var.get())
        cfg.max_hole_diameter = float(self._max_hole_dia_var.get())
        cfg.unify_same_domain = bool(self._unify_var.get())
        cfg.keep_largest_only = bool(self._largest_only_var.get())
        cfg.step_schema = self._schema_var.get() or "AP214"

    def set_controls_state(self, has_assembly: bool, has_result: bool, busy: bool) -> None:
        self._btn_analyze.configure(
            state=tk.NORMAL if (has_assembly and not busy) else tk.DISABLED
        )
        self._btn_simplify.configure(
            state=tk.NORMAL if (has_assembly and not busy) else tk.DISABLED
        )
        self._btn_save.configure(
            state=tk.NORMAL if (has_result and not busy) else tk.DISABLED
        )
        self._btn_view_res.configure(
            state=tk.NORMAL if (has_result and not busy) else tk.DISABLED
        )

    def update_result(self, result: Optional[SimplifyResult]) -> None:
        if result is None:
            self._result_var.set("Simplification has not been run yet.")
            return
        v = result.validation
        valid_txt = "Valid (Single Watertight B-Rep Solid)" if v.is_valid else "Warnings present"
        wt_txt = "Watertight" if v.is_watertight else f"{v.free_edge_count} free edges"
        ft_txt = ", ".join(
            f"{k}:{cnt}" for k, cnt in sorted(v.face_types.items(), key=lambda kv: -kv[1])[:5]
        )
        lines = [
            f"• Validation: {valid_txt}",
            f"• Solids / Shells: {v.solid_count} / {v.shell_count}",
            f"• Closure: {wt_txt}",
            f"• Final Faces: {v.face_count:,} ({ft_txt})",
            f"• Final Volume: {v.volume:,.1f} mm³ (Fill +{result.filled_volume_delta:,.1f})",
            f"• Bounding Box: {v.bbox_size[0]:.2f} × {v.bbox_size[1]:.2f} × {v.bbox_size[2]:.2f} mm",
            f"• Elapsed: {result.elapsed_seconds:.2f}s",
        ]
        if v.warnings:
            lines.append("• Warnings: " + " / ".join(v.warnings))
        self._result_var.set("\n".join(lines))
