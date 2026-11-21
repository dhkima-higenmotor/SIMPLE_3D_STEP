"""Modern Flat settings dialog for configuring external 3D viewer path and config.json."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from simple_3d_step.config import AppConfig, save_config
from simple_3d_step.gui.theme import COLORS, FONTS


class SettingsDialog(tk.Toplevel):
    """Modal Modern Flat dialog to configure external 3D viewer executable and arguments."""

    def __init__(
        self,
        parent: tk.Misc,
        config: AppConfig,
        config_path: Path,
        on_saved: Optional[Callable[[AppConfig], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.title("External 3D Viewer & Preferences")
        self.configure(background=COLORS["surface"])
        self.transient(parent)
        self.grab_set()
        self.resizable(True, False)
        self.minsize(720, 340)

        self._config = config
        self._config_path = config_path
        self._on_saved = on_saved

        self._viewer_var = tk.StringVar(value=config.viewer_executable)
        self._args_var = tk.StringVar(value=config.viewer_args or "{file}")

        self._build_ui()
        self._center_on_parent(parent)

    def _build_ui(self) -> None:
        container = ttk.Frame(self, style="Card.TFrame", padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)

        ttk.Label(
            container,
            text="External 3D Viewer Executable",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        ttk.Label(container, text="Executable Path:", style="Card.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(0, 8)
        )

        path_frame = ttk.Frame(container, style="Card.TFrame")
        path_frame.grid(row=1, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 8))
        path_frame.columnconfigure(0, weight=1)

        ttk.Entry(path_frame, textvariable=self._viewer_var).grid(
            row=0, column=0, sticky=tk.EW
        )
        ttk.Button(
            path_frame, text="Browse...", style="Small.TButton", command=self._browse_exe
        ).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(
            path_frame,
            text="Clear",
            style="Small.TButton",
            command=lambda: self._viewer_var.set(""),
        ).grid(row=0, column=2, padx=(4, 0))

        ttk.Label(container, text="Argument Template:", style="Card.TLabel").grid(
            row=2, column=0, sticky=tk.W, pady=(0, 8)
        )

        ttk.Entry(container, textvariable=self._args_var).grid(
            row=2, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 8)
        )

        info_box = tk.Frame(
            container,
            background=COLORS["surface_alt"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        info_box.grid(row=3, column=0, columnspan=2, sticky=tk.EW, pady=(4, 14))

        help_text = (
            "• Leave the executable path empty to open STEP files with the Windows default application.\n"
            "• {file} in the argument template is replaced with the STEP file path (e.g. {file} or --open {file}).\n"
            f"• Config file path: {self._config_path}"
        )
        tk.Label(
            info_box,
            text=help_text,
            background=COLORS["surface_alt"],
            foreground=COLORS["text_secondary"],
            font=FONTS["small"],
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=660,
        ).pack(fill=tk.X)

        btn_frame = ttk.Frame(container, style="Card.TFrame")
        btn_frame.grid(row=4, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(
            btn_frame, text="Save", style="Primary.TButton", command=self._save, width=10
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy, width=10).pack(
            side=tk.LEFT
        )

    def _browse_exe(self) -> None:
        initial = (
            Path(self._viewer_var.get()).parent
            if self._viewer_var.get()
            else Path("C:/Program Files")
        )
        if not initial.exists():
            initial = Path.home()
        selected = filedialog.askopenfilename(
            parent=self,
            title="Select External 3D Viewer Executable",
            initialdir=str(initial),
            filetypes=[("Executables", "*.exe;*.bat;*.cmd"), ("All Files", "*.*")],
        )
        if selected:
            self._viewer_var.set(selected)

    def _save(self) -> None:
        exe = self._viewer_var.get().strip()
        if exe and not Path(exe).exists():
            if not messagebox.askyesno(
                "Confirm Path",
                f"The specified executable does not currently exist:\n{exe}\nSave anyway?",
                parent=self,
            ):
                return

        self._config.viewer_executable = exe
        self._config.viewer_args = self._args_var.get().strip() or "{file}"
        save_config(self._config, self._config_path)
        if self._on_saved:
            self._on_saved(self._config)
        self.destroy()

    def _center_on_parent(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")
