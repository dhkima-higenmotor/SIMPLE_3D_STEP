"""Bottom Modern Flat status bar, progress indicator, and slate log console."""

from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from simple_3d_step.gui.theme import COLORS, FONTS


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class LogPanel(tk.Frame):
    """Displays live operation status, slim progress bar, cancel button, and log console."""

    def __init__(
        self,
        parent: tk.Misc,
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self._on_cancel = on_cancel
        self._status_var = tk.StringVar(value="준비 완료 — STEP 파일을 열어 시작하세요.")
        self._progress_var = tk.DoubleVar(value=0.0)
        self._busy = False
        self._spin_idx = 0
        self._spin_after_id: Optional[str] = None
        self._base_status = "준비 완료 — STEP 파일을 열어 시작하세요."

        self._build_ui()

    def _build_ui(self) -> None:
        status_row = ttk.Frame(self, style="Subtle.TFrame", padding=(10, 5))
        status_row.pack(fill=tk.X)

        ttk.Label(
            status_row,
            textvariable=self._status_var,
            style="Subtle.TLabel",
            font=FONTS["small_bold"],
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._progress = ttk.Progressbar(
            status_row,
            variable=self._progress_var,
            maximum=100.0,
            length=210,
            mode="determinate",
        )
        self._progress.pack(side=tk.LEFT, padx=(10, 8))

        self._cancel_btn = ttk.Button(
            status_row,
            text="작업 취소",
            style="Small.TButton",
            command=self._handle_cancel,
            state=tk.DISABLED,
            width=9,
        )
        self._cancel_btn.pack(side=tk.LEFT, padx=(0, 4))

        ttk.Button(
            status_row,
            text="로그 지우기",
            style="Small.TButton",
            command=self.clear_log,
            width=10,
        ).pack(side=tk.LEFT)

        # Hairline divider between status strip and console
        tk.Frame(self, height=1, background=COLORS["border"]).pack(fill=tk.X)

        log_frame = tk.Frame(self, background=COLORS["console_bg"], padx=6, pady=4)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._text = tk.Text(
            log_frame,
            height=4,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=FONTS["mono"],
            background=COLORS["console_bg"],
            foreground=COLORS["console_fg"],
            insertbackground="#ffffff",
            selectbackground="#334155",
            relief=tk.FLAT,
            borderwidth=0,
            padx=4,
            pady=2,
        )
        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self._text.yview)
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
        self._text.configure(state=tk.NORMAL)
        self._text.insert(tk.END, f"[{ts}] {message}\n")
        self._text.see(tk.END)
        self._text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
