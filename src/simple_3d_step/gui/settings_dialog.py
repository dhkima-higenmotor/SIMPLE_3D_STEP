"""Modern Flat settings dialog for configuring external 3D viewer, GUI font, and config.json."""

from __future__ import annotations

import os
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from simple_3d_step.config import AppConfig, resolve_viewer_executable, save_config
from simple_3d_step.gui.theme import (
    COLORS,
    DEFAULT_UI_FONT_SIZE,
    FONTS,
    MAX_UI_FONT_SIZE,
    MIN_UI_FONT_SIZE,
    has_xft_font_support,
    list_available_font_families,
    normalize_ui_font_size,
    resolve_ui_font_family,
)


AUTO_FONT_LABEL = "Auto (System Default)"


class SettingsDialog(tk.Toplevel):
    """Modal Modern Flat dialog to configure external 3D viewer and GUI font preferences."""

    def __init__(
        self,
        parent: tk.Misc,
        config: AppConfig,
        config_path: Path,
        on_saved: Optional[Callable[[AppConfig], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.title("External 3D Viewer & Preferences")
        self.configure(background=COLORS["bg_light"])
        self.transient(parent)
        self.grab_set()
        self.resizable(True, False)
        self.minsize(580, 410)

        self._config = config
        self._config_path = config_path
        self._on_saved = on_saved

        self._viewer_var = tk.StringVar(value=config.viewer_executable)
        self._args_var = tk.StringVar(value=config.viewer_args or "{file}")

        initial_family = (config.ui_font_family or "").strip() or AUTO_FONT_LABEL
        self._font_family_var = tk.StringVar(value=initial_family)
        self._font_size_var = tk.StringVar(value=str(normalize_ui_font_size(config.ui_font_size)))
        self._preview_font = tkfont.Font(
            root=self,
            family=resolve_ui_font_family(self, preferred=config.ui_font_family),
            size=normalize_ui_font_size(config.ui_font_size),
        )

        self._build_ui()
        self._update_font_preview()
        self._center_on_parent(parent)

    def _build_ui(self) -> None:
        container = ttk.Frame(self, style="Card.TFrame", padding=18)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)

        # --- Section 1: External 3D Viewer ---
        ttk.Label(
            container,
            text="External 3D Viewer Executable",
            style="SectionTitle.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

        ttk.Label(container, text="Executable Path:", style="Card.TLabel").grid(
            row=1, column=0, sticky=tk.W, pady=(0, 6)
        )

        path_frame = ttk.Frame(container, style="Card.TFrame")
        path_frame.grid(row=1, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 6))
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
            row=2, column=0, sticky=tk.W, pady=(0, 10)
        )

        ttk.Entry(container, textvariable=self._args_var).grid(
            row=2, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 10)
        )

        ttk.Separator(container, orient=tk.HORIZONTAL).grid(
            row=3, column=0, columnspan=2, sticky=tk.EW, pady=(2, 10)
        )

        # --- Section 2: GUI Font (Appearance) ---
        ttk.Label(
            container,
            text="GUI Font (Applied Immediately)",
            style="SectionTitle.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

        ttk.Label(container, text="Font Family:", style="Card.TLabel").grid(
            row=5, column=0, sticky=tk.W, pady=(0, 6)
        )

        font_row = ttk.Frame(container, style="Card.TFrame")
        font_row.grid(row=5, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 6))
        font_row.columnconfigure(0, weight=1)

        families = [AUTO_FONT_LABEL] + list_available_font_families(self)
        self._font_combo = ttk.Combobox(
            font_row,
            textvariable=self._font_family_var,
            values=families,
        )
        self._font_combo.grid(row=0, column=0, sticky=tk.EW)

        ttk.Label(font_row, text="Size (pt):", style="Card.TLabel").grid(
            row=0, column=1, sticky=tk.W, padx=(10, 4)
        )
        self._font_size_spin = ttk.Spinbox(
            font_row,
            from_=MIN_UI_FONT_SIZE,
            to=MAX_UI_FONT_SIZE,
            increment=1,
            textvariable=self._font_size_var,
            width=5,
            command=self._update_font_preview,
        )
        self._font_size_spin.grid(row=0, column=2, sticky=tk.W)

        ttk.Button(
            font_row,
            text="Auto",
            style="Small.TButton",
            command=self._reset_font_defaults,
        ).grid(row=0, column=3, padx=(6, 0))

        self._font_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_font_preview())
        self._font_family_var.trace_add("write", lambda *_: self._update_font_preview())
        self._font_size_var.trace_add("write", lambda *_: self._update_font_preview())

        ttk.Label(container, text="Preview:", style="Card.TLabel").grid(
            row=6, column=0, sticky=tk.W, pady=(0, 8)
        )
        preview_box = tk.Frame(
            container,
            background=COLORS["bg_dark"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=8,
            pady=6,
        )
        preview_box.grid(row=6, column=1, sticky=tk.EW, padx=(10, 0), pady=(0, 8))

        self._preview_label = tk.Label(
            preview_box,
            text="Open STEP · 1. Analyze Parts · Solid 1 / Shell 1 · 한글 123.4 mm³",
            background=COLORS["bg_dark"],
            foreground=COLORS["fg"],
            font=self._preview_font,
            anchor=tk.W,
            justify=tk.LEFT,
        )
        self._preview_label.pack(fill=tk.X)

        next_row = 7
        if not has_xft_font_support(self):
            warn_box = tk.Frame(
                container,
                background=COLORS["warning_soft"],
                highlightbackground=COLORS["peach"],
                highlightthickness=1,
                padx=10,
                pady=6,
            )
            warn_box.grid(row=next_row, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
            tk.Label(
                warn_box,
                text=(
                    "Note: Current Python Tk build has no Xft support (bitmap 'fixed' font only). "
                    "Recreate .venv with an Xft-enabled system Python (e.g. `uv sync --python /usr/bin/python3`) "
                    "to enable anti-aliased system fonts."
                ),
                background=COLORS["warning_soft"],
                foreground=COLORS["warning_text"],
                font=FONTS["small"],
                justify=tk.LEFT,
                anchor=tk.W,
                wraplength=520,
            ).pack(fill=tk.X)
            next_row += 1

        info_box = tk.Frame(
            container,
            background=COLORS["bg_dark"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        info_box.grid(row=next_row, column=0, columnspan=2, sticky=tk.EW, pady=(2, 14))

        default_opener = (
            "Windows default application"
            if os.name == "nt"
            else ("macOS default application (open)" if sys.platform == "darwin" else "system default application (xdg-open)")
        )
        help_text = (
            f"• Leave the executable path empty to open STEP files with the {default_opener}.\n"
            "• You may enter either a full executable path or a command available on PATH (e.g. freecad).\n"
            "• {file} in the argument template is replaced with the STEP file path (e.g. {file} or --open {file}).\n"
            "• Font changes apply immediately to the GUI and are saved to config.json.\n"
            f"• Config file path: {self._config_path}"
        )
        tk.Label(
            info_box,
            text=help_text,
            background=COLORS["bg_dark"],
            foreground=COLORS["fg_sub"],
            font=FONTS["small"],
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=520,
        ).pack(fill=tk.X)

        btn_frame = ttk.Frame(container, style="Card.TFrame")
        btn_frame.grid(row=next_row + 1, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(
            btn_frame, text="Save", style="Primary.TButton", command=self._save, width=10
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Cancel", command=self.destroy, width=10).pack(
            side=tk.LEFT
        )

    def _selected_font_family(self) -> str:
        raw = self._font_family_var.get().strip()
        if not raw or raw == AUTO_FONT_LABEL:
            return ""
        return raw

    def _reset_font_defaults(self) -> None:
        self._font_family_var.set(AUTO_FONT_LABEL)
        self._font_size_var.set(str(DEFAULT_UI_FONT_SIZE))
        self._update_font_preview()

    def _update_font_preview(self) -> None:
        family = resolve_ui_font_family(self, preferred=self._selected_font_family())
        size = normalize_ui_font_size(self._font_size_var.get())
        try:
            self._preview_font.configure(family=family, size=size)
        except tk.TclError:
            pass

    def _browse_exe(self) -> None:
        current_exe = self._viewer_var.get().strip()
        resolved = resolve_viewer_executable(current_exe) if current_exe else None
        if resolved is not None:
            initial = resolved.parent
        elif current_exe:
            initial = Path(current_exe).expanduser().parent
        elif os.name == "nt":
            initial = Path("C:/Program Files")
        elif sys.platform == "darwin":
            initial = Path("/Applications")
        else:
            initial = Path("/usr/bin")

        if not initial.exists():
            initial = Path.home()

        if os.name == "nt":
            filetypes = [("Executables", ("*.exe", "*.bat", "*.cmd")), ("All Files", "*.*")]
        else:
            filetypes = [
                ("All Files", "*"),
                ("AppImage / Scripts", ("*.AppImage", "*.appimage", "*.sh")),
            ]

        selected = filedialog.askopenfilename(
            parent=self,
            title="Select External 3D Viewer Executable",
            initialdir=str(initial),
            filetypes=filetypes,
        )
        if selected:
            self._viewer_var.set(selected)

    def _save(self) -> None:
        exe = self._viewer_var.get().strip()
        if exe and resolve_viewer_executable(exe) is None:
            if not messagebox.askyesno(
                "Confirm Path",
                f"The specified executable does not currently exist (or is not on PATH):\n{exe}\nSave anyway?",
                parent=self,
            ):
                return

        self._config.viewer_executable = exe
        self._config.viewer_args = self._args_var.get().strip() or "{file}"
        self._config.ui_font_family = self._selected_font_family()
        self._config.ui_font_size = normalize_ui_font_size(self._font_size_var.get())
        save_config(self._config, self._config_path)
        if self._on_saved:
            self._on_saved(self._config)
        self.destroy()

    def _center_on_parent(self, parent: tk.Misc) -> None:
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(px, 0)}+{max(py, 0)}")
