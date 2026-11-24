"""Animated Modern Flat busy banner widget for background CAD operations."""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from simple_3d_step.gui.theme import COLORS, FONTS


class BusyBanner(tk.Frame):
    """Prominent Slate + Indigo status banner displayed while the CAD subprocess is busy."""

    BG = "#eef2ff"                   # Indigo-50
    BORDER = COLORS["accent_border"] # Indigo-200 (#c7d2fe)
    FG_TITLE = COLORS["accent_text"] # Indigo-800 (#3730a3)
    FG_DETAIL = COLORS["fg_sub"]     # Slate-500 (#64748b)
    BAR_BG = COLORS["accent_soft"]   # Indigo-100 (#e0e7ff)
    BAR_FILL = COLORS["accent"]      # Indigo-500 (#6366f1)
    BAR_PULSE = COLORS["accent_hover"]  # Indigo-400 (#818cf8)

    def __init__(
        self,
        parent: tk.Misc,
        on_cancel: Callable[[], None],
        on_force_cancel: Callable[[], None],
    ) -> None:
        super().__init__(
            parent,
            background=self.BG,
            highlightbackground=self.BORDER,
            highlightthickness=1,
            padx=10,
            pady=5,
        )
        self._on_cancel = on_cancel
        self._on_force_cancel = on_force_cancel

        self._active = False
        self._cancel_requested = False
        self._start_time = 0.0
        self._fraction = 0.0
        self._angle = 0.0
        self._pulse_pos = 0.0
        self._anim_after_id: Optional[str] = None

        self._title_var = tk.StringVar(value="")
        self._detail_var = tk.StringVar(value="")
        self._timer_var = tk.StringVar(value="00:00.0")
        self._pct_var = tk.StringVar(value="0%")

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)

        # Left: crisp rotating circular arc spinner
        self._spinner = tk.Canvas(
            self,
            width=26,
            height=26,
            background=self.BG,
            highlightthickness=0,
        )
        self._spinner.grid(row=0, column=0, rowspan=2, padx=(0, 10), sticky=tk.W)
        self._spinner.create_oval(3, 3, 23, 23, outline=COLORS["accent_border"], width=2.5)
        self._arc_id = self._spinner.create_arc(
            3, 3, 23, 23,
            start=0,
            extent=105,
            style=tk.ARC,
            outline=COLORS["accent"],
            width=2.5,
        )

        # Top middle: task title + percentage + elapsed timer pill
        top_row = tk.Frame(self, background=self.BG)
        top_row.grid(row=0, column=1, sticky=tk.EW)
        top_row.columnconfigure(0, weight=1)

        tk.Label(
            top_row,
            textvariable=self._title_var,
            background=self.BG,
            foreground=self.FG_TITLE,
            font=FONTS["base_bold"],
            anchor=tk.W,
        ).grid(row=0, column=0, sticky=tk.W)

        tk.Label(
            top_row,
            textvariable=self._pct_var,
            background=COLORS["accent_soft"],
            foreground=self.FG_TITLE,
            font=FONTS["mono_bold"],
            padx=6,
            pady=1,
        ).grid(row=0, column=1, padx=(8, 6))

        tk.Label(
            top_row,
            textvariable=self._timer_var,
            background=self.BG,
            foreground=self.FG_DETAIL,
            font=FONTS["mono"],
        ).grid(row=0, column=2, padx=(0, 8))

        # Bottom middle: detail step text + slim animated progress/pulse track
        bot_row = tk.Frame(self, background=self.BG)
        bot_row.grid(row=1, column=1, sticky=tk.EW, pady=(3, 0))
        bot_row.columnconfigure(0, weight=1)

        tk.Label(
            bot_row,
            textvariable=self._detail_var,
            background=self.BG,
            foreground=self.FG_DETAIL,
            font=FONTS["small"],
            anchor=tk.W,
        ).grid(row=0, column=0, sticky=tk.W)

        self._bar = tk.Canvas(
            bot_row,
            height=6,
            background=self.BAR_BG,
            highlightthickness=0,
        )
        self._bar.grid(row=1, column=0, sticky=tk.EW, pady=(4, 0))
        self._fill_rect = self._bar.create_rectangle(0, 0, 0, 6, fill=self.BAR_FILL, width=0)
        self._pulse_rect = self._bar.create_rectangle(0, 0, 0, 6, fill=self.BAR_PULSE, width=0)

        # Right: Flat Danger Cancel / Force-Stop button
        self._cancel_btn = ttk.Button(
            self,
            text="Cancel",
            style="Danger.TButton",
            command=self._handle_cancel_click,
            width=11,
        )
        self._cancel_btn.grid(row=0, column=2, rowspan=2, padx=(12, 0), sticky=tk.E)

    def start(self, task_name: str, detail: str = "Processing in background CAD engine...") -> None:
        self._active = True
        self._cancel_requested = False
        self._start_time = time.perf_counter()
        self._fraction = 0.0
        self._title_var.set(f"Working · {task_name}")
        self._detail_var.set(detail)
        self._pct_var.set("0%")
        self._timer_var.set("Elapsed 00:00.0")
        self._cancel_btn.configure(text="Cancel", state=tk.NORMAL)

        if self._anim_after_id is None:
            self._tick_animation()

    def update_progress(self, fraction: float, detail: str) -> None:
        self._fraction = max(0.0, min(1.0, float(fraction)))
        self._pct_var.set(f"{int(round(self._fraction * 100))}%")
        if detail:
            self._detail_var.set(detail)

    def mark_cancelling(self) -> None:
        self._cancel_requested = True
        self._detail_var.set("Cancellation requested — waiting for current C++ step (click 'Force Stop' to abort immediately)")
        self._cancel_btn.configure(text="Force Stop")

    def stop(self) -> None:
        self._active = False
        self._cancel_requested = False
        if self._anim_after_id is not None:
            try:
                self.after_cancel(self._anim_after_id)
            except Exception:
                pass
            self._anim_after_id = None

    @property
    def elapsed_formatted(self) -> str:
        if not self._active:
            return "00:00"
        elapsed = max(0.0, time.perf_counter() - self._start_time)
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        return f"{mins:02d}:{secs:02d}"

    def _handle_cancel_click(self) -> None:
        if not self._cancel_requested:
            self._on_cancel()
        else:
            self._on_force_cancel()

    def _tick_animation(self) -> None:
        if not self._active:
            self._anim_after_id = None
            return

        self._angle = (self._angle - 18.0) % 360.0
        self._spinner.itemconfigure(self._arc_id, start=self._angle)

        elapsed = max(0.0, time.perf_counter() - self._start_time)
        mins = int(elapsed // 60)
        secs = elapsed % 60.0
        self._timer_var.set(f"Elapsed {mins:02d}:{secs:04.1f}")

        w = max(self._bar.winfo_width(), 120)
        fill_w = w * self._fraction
        self._bar.coords(self._fill_rect, 0, 0, fill_w, 6)

        pulse_w = max(48.0, w * 0.18)
        self._pulse_pos = (self._pulse_pos + w * 0.028) % (w + pulse_w)
        px0 = self._pulse_pos - pulse_w
        px1 = self._pulse_pos
        self._bar.coords(self._pulse_rect, px0, 0, px1, 6)

        self._anim_after_id = self.after(50, self._tick_animation)
