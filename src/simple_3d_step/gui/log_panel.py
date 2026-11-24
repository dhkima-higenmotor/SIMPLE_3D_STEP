"""Bottom status bar, progress indicator, and light Output Log matching AUTO_RENDER."""

from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from simple_3d_step.gui.theme import COLORS, FONTS


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class LogPanel(tk.Frame):
    """Displays live operation status, slim progress bar, cancel button, and Output Log."""

    def __init__(
        self,
        parent: tk.Misc,
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            background=COLORS["bg_light"],
        )
        self._on_cancel = on_cancel
        self._status_var = tk.StringVar(value="Ready — click [Open STEP] to load an assembly.")
        self._progress_var = tk.DoubleVar(value=0.0)
        self._busy = False
        self._spin_idx = 0
        self._spin_after_id: Optional[str] = None
        self._base_status = "Ready — click [Open STEP] to load an assembly."

        self._build_ui()

    def _build_ui(self) -> None:
        frame_log = ttk.LabelFrame(self, text="Output Log", padding=(6, 3))
        frame_log.pack(fill=tk.BOTH, expand=True)

        status_row = ttk.Frame(frame_log, style="Card.TFrame")
        status_row.pack(fill=tk.X, pady=(0, 3))

        ttk.Label(
            status_row,
            textvariable=self._status_var,
            style="CardBold.TLabel",
            foreground=COLORS["fg_sub"],
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._progress = ttk.Progressbar(
            status_row,
            variable=self._progress_var,
            maximum=100.0,
            length=160,
            mode="determinate",
        )
        self._progress.pack(side=tk.LEFT, padx=(8, 6))

        self._cancel_btn = ttk.Button(
            status_row,
            text="Cancel",
            style="Danger.TButton",
            command=self._handle_cancel,
            state=tk.DISABLED,
            width=8,
        )
        self._cancel_btn.pack(side=tk.LEFT, padx=(0, 4))

        ttk.Button(
            status_row,
            text="Clear Log",
            style="Small.TButton",
            command=self.clear_log,
            width=9,
        ).pack(side=tk.LEFT)

        log_inner = ttk.Frame(frame_log, style="Card.TFrame")
        log_inner.pack(fill=tk.BOTH, expand=True)

        self._text = tk.Text(
            log_inner,
            height=3,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=FONTS["mono"],
            background=COLORS["bg_dark"],
            foreground=COLORS["fg"],
            insertbackground=COLORS["fg"],
            selectbackground=COLORS["accent"],
            selectforeground="#ffffff",
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            padx=4,
            pady=2,
        )
        self._text.tag_config("error", foreground=COLORS["red"])
        self._text.tag_config("ok", foreground=COLORS["green_dim"])
        self._text.tag_config("accent", foreground=COLORS["accent"])

        scroll = ttk.Scrollbar(log_inner, orient=tk.VERTICAL, command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)

        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _handle_cancel(self) -> None:
        if self._on_cancel:
            self._on_cancel()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._cancel_btn.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self._progress.configure(mode="determinate")
            if self._spin_after_id is None:
                self._tick_spinner()
        else:
            if self._spin_after_id is not None:
                try:
                    self.after_cancel(self._spin_after_id)
                except Exception:
                    pass
                self._spin_after_id = None
            self._progress_var.set(0.0)
            self._status_var.set(self._base_status)

    def _tick_spinner(self) -> None:
        if not self._busy:
            self._spin_after_id = None
            return
        frame = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
        self._spin_idx += 1
        self._status_var.set(f"{frame}  {self._base_status}")
        self._spin_after_id = self.after(100, self._tick_spinner)

    def set_progress(self, fraction: float, message: str) -> None:
        clamped = max(0.0, min(1.0, float(fraction))) * 100.0
        self._progress_var.set(clamped)
        self._base_status = message
        if not self._busy:
            self._status_var.set(message)

    def append_log(self, message: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        tag = None
        stripped = message.lstrip()
        if stripped.startswith(("✖", "■")):
            tag = "error"
        elif stripped.startswith("✔"):
            tag = "ok"
        elif stripped.startswith("▶"):
            tag = "accent"

        self._text.configure(state=tk.NORMAL)
        if tag:
            self._text.insert(tk.END, f"[{ts}] {message}\n", tag)
        else:
            self._text.insert(tk.END, f"[{ts}] {message}\n")
        self._text.see(tk.END)
        self._text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
