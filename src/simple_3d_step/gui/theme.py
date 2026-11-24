"""Centralized Slate + Indigo (Premium Light) design tokens and ttk style configuration.

Visually unified with the AUTO_RENDER GUI (Slate-50/100/200 surfaces, Indigo-500 accent,
Emerald-600 primary actions, Rose-500 danger actions, and crisp DPI-aware Segoe UI 9-10pt).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def enable_dpi_awareness() -> None:
    """Make the process DPI-aware on Windows to render sharp system fonts (matching AUTO_RENDER)."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


# Match AUTO_RENDER_GUI.py: activate DPI awareness at module import before Tk() is created
enable_dpi_awareness()


# ═══════════════════════════════════════════════════════════════════
# Centralized System Font Definitions (Identical to AUTO_RENDER_GUI)
# ═══════════════════════════════════════════════════════════════════
SYSTEM_FONT = ("Segoe UI", 9)
SYSTEM_FONT_BOLD = ("Segoe UI", 9, "bold")
SYSTEM_FONT_LARGE_BOLD = ("Segoe UI", 10, "bold")

FONTS = {
    "base": SYSTEM_FONT,
    "base_bold": SYSTEM_FONT_BOLD,
    "small": SYSTEM_FONT,
    "small_bold": SYSTEM_FONT_BOLD,
    "heading": SYSTEM_FONT_LARGE_BOLD,
    "mono": SYSTEM_FONT,
    "mono_bold": SYSTEM_FONT_BOLD,
}


# ═══════════════════════════════════════════════════════════════════
# Color Palette — Slate + Indigo (Premium Light, Identical to AUTO_RENDER)
# ═══════════════════════════════════════════════════════════════════
COLORS = {
    # Core AUTO_RENDER palette tokens
    "bg": "#f8fafc",            # Slate-50    — root background & frame background
    "bg_light": "#f8fafc",      # Slate-50    — unified card background with root bg
    "bg_dark": "#f1f5f9",       # Slate-100   — text inputs / log output / treeview
    "fg": "#0f172a",            # Slate-900   — primary text
    "fg_dim": "#94a3b8",        # Slate-400   — disabled text
    "fg_sub": "#64748b",        # Slate-500   — secondary text
    "accent": "#6366f1",        # Indigo-500  — primary accent / selection bg
    "accent_hover": "#818cf8",  # Indigo-400  — Info button hover
    "accent_soft": "#e0e7ff",   # Indigo-100  — selection tint / soft highlight
    "accent_border": "#c7d2fe", # Indigo-200  — subtle indigo border
    "accent_text": "#3730a3",   # Indigo-800  — selected / strong indigo ink
    "green": "#34d399",         # Emerald-400 — success hover
    "green_dim": "#059669",     # Emerald-600 — success button bg
    "red": "#f43f5e",           # Rose-500    — error / danger
    "peach": "#f59e0b",         # Amber-500   — warning
    "border": "#e2e8f0",        # Slate-200   — borders & flat button fill
    "surface2": "#f1f5f9",      # Slate-100   — hover / selection
    # Semantic aliases mapped directly onto the AUTO_RENDER palette
    "app_bg": "#f8fafc",        # == bg
    "surface": "#f8fafc",       # == bg_light
    "surface_alt": "#f1f5f9",   # == bg_dark / surface2
    "surface_hover": "#f1f5f9", # == surface2
    "border_strong": "#e2e8f0", # == border
    "text": "#0f172a",          # == fg
    "text_secondary": "#64748b",# == fg_sub
    "text_muted": "#94a3b8",    # == fg_dim
    # Success (Emerald)
    "success": "#059669",       # == green_dim
    "success_soft": "#ecfdf5",  # Emerald-50
    "success_hover": "#d1fae5", # Emerald-100
    "success_border": "#34d399",# == green
    "success_text": "#059669",  # == green_dim
    # Danger / Internal (Rose)
    "danger": "#f43f5e",        # == red
    "danger_hover": "#e11d48",  # Rose-600 (AUTO_RENDER Danger active fg)
    "danger_soft": "#fff1f2",   # Rose-50  (AUTO_RENDER Danger active bg)
    "danger_text": "#e11d48",   # Rose-600
    # Warning / Manual drop (Amber)
    "warning_soft": "#fffbeb",  # Amber-50
    "warning_text": "#b45309",  # Amber-700
    # Light Output Log (matches AUTO_RENDER Output Log)
    "console_bg": "#f1f5f9",    # == bg_dark
    "console_fg": "#0f172a",    # == fg
    "console_muted": "#64748b", # == fg_sub
}

# Sequential workflow button visual states (Indigo blue primary + Emerald done)
STEP_STYLES = {
    "idle": {
        "bg": COLORS["accent"],          # #6366f1 (Indigo-500 blue emphasis)
        "hover_bg": COLORS["accent_hover"],  # #818cf8 (Indigo-400)
        "fg": "#ffffff",
        "disabled_fg": "#c7d2fe",        # Indigo-200 on Indigo-500 when disabled
        "border": COLORS["accent"],      # #6366f1
        "prefix": "",
        "font": SYSTEM_FONT_BOLD,
    },
    "running": {
        "bg": COLORS["accent_soft"],     # #e0e7ff (Indigo-100)
        "hover_bg": COLORS["accent_border"],
        "fg": COLORS["accent_text"],     # #3730a3 (Indigo-800)
        "disabled_fg": COLORS["accent"],
        "border": COLORS["accent"],      # #6366f1 (Indigo-500)
        "prefix": "● ",
        "font": SYSTEM_FONT_BOLD,
    },
    "done": {
        "bg": COLORS["success_soft"],    # #ecfdf5 (Emerald-50)
        "hover_bg": COLORS["success_hover"],
        "fg": COLORS["green_dim"],       # #059669 (Emerald-600)
        "disabled_fg": COLORS["green"],
        "border": COLORS["green"],       # #34d399 (Emerald-400)
        "prefix": "✔ ",
        "font": SYSTEM_FONT_BOLD,
    },
}


def apply_modern_flat_theme(root: tk.Tk) -> ttk.Style:
    """Configure all ttk widget styles using the AUTO_RENDER Slate + Indigo palette."""
    C = COLORS
    root.configure(background=C["bg"])

    # Centralized system font uniformity (matching AUTO_RENDER_GUI)
    root.option_add("*Font", SYSTEM_FONT)
    root.option_add("*TCombobox*Listbox.font", SYSTEM_FONT)
    root.option_add("*TCombobox*Listbox.background", C["bg_dark"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["accent_text"])

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    # Global defaults
    style.configure(
        ".",
        background=C["bg"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
        bordercolor=C["border"],
        lightcolor=C["bg_light"],
        darkcolor=C["bg_light"],
        focuscolor=C["accent"],
    )

    # ── Frame ──
    style.configure("TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["bg_light"])
    style.configure("Subtle.TFrame", background=C["bg_light"])
    style.configure("Toolbar.TFrame", background=C["bg_light"])

    # ── LabelFrame (signature Indigo-500 bold header from AUTO_RENDER) ──
    style.configure(
        "TLabelframe",
        background=C["bg_light"],
        bordercolor=C["border"],
        lightcolor=C["bg_light"],
        darkcolor=C["bg_light"],
    )
    style.configure(
        "TLabelframe.Label",
        background=C["bg_light"],
        foreground=C["accent"],
        font=SYSTEM_FONT_LARGE_BOLD,
    )

    # ── Label ──
    style.configure(
        "TLabel",
        background=C["bg"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
    )
    style.configure(
        "Card.TLabel",
        background=C["bg_light"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
    )
    style.configure(
        "Path.TLabel",
        background=C["bg_light"],
        foreground=C["accent"],
        font=SYSTEM_FONT,
    )
    style.configure(
        "Bold.TLabel",
        background=C["bg"],
        foreground=C["fg"],
        font=SYSTEM_FONT_BOLD,
    )
    style.configure(
        "CardBold.TLabel",
        background=C["bg_light"],
        foreground=C["fg"],
        font=SYSTEM_FONT_BOLD,
    )
    style.configure(
        "Muted.TLabel",
        background=C["bg_light"],
        foreground=C["fg_sub"],
        font=SYSTEM_FONT,
    )
    style.configure(
        "Subtle.TLabel",
        background=C["bg_light"],
        foreground=C["fg_sub"],
        font=SYSTEM_FONT,
    )
    style.configure(
        "Accent.TLabel",
        background=C["bg_light"],
        foreground=C["accent"],
        font=SYSTEM_FONT_BOLD,
    )
    style.configure(
        "SectionTitle.TLabel",
        background=C["bg_light"],
        foreground=C["accent"],
        font=SYSTEM_FONT_LARGE_BOLD,
    )

    # ── Flat Buttons Configuration (Identical to AUTO_RENDER_GUI) ──
    # Default Button (flat Slate-200)
    style.configure(
        "TButton",
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        font=SYSTEM_FONT,
        padding=(6, 4),
    )
    style.map(
        "TButton",
        background=[("active", C["surface2"]), ("pressed", C["border"])],
        foreground=[("disabled", C["fg_dim"])],
        bordercolor=[("active", C["border"])],
    )

    # Toolbar Button (matches AUTO_RENDER default TButton)
    style.configure(
        "Toolbar.TButton",
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        font=SYSTEM_FONT,
        padding=(8, 4),
    )
    style.map(
        "Toolbar.TButton",
        background=[("active", C["surface2"]), ("pressed", C["border"])],
        foreground=[("disabled", C["fg_dim"])],
        bordercolor=[("active", C["border"])],
    )

    # Compact Small Flat Button
    style.configure(
        "Small.TButton",
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        font=SYSTEM_FONT,
        padding=(6, 3),
    )
    style.map(
        "Small.TButton",
        background=[("active", C["surface2"]), ("pressed", C["border"])],
        foreground=[("disabled", C["fg_dim"])],
        bordercolor=[("active", C["border"])],
    )

    # Accent / Primary Button (Emerald — exact AUTO_RENDER Accent.TButton)
    for btn_style in ("Accent.TButton", "Primary.TButton"):
        style.configure(
            btn_style,
            background=C["green_dim"],
            foreground="#ffffff",
            bordercolor=C["green_dim"],
            lightcolor=C["green_dim"],
            darkcolor=C["green_dim"],
            font=SYSTEM_FONT_BOLD,
            padding=(6, 4),
        )
        style.map(
            btn_style,
            background=[
                ("disabled", C["border"]),
                ("active", C["green"]),
                ("pressed", C["green_dim"]),
            ],
            foreground=[("disabled", C["fg_dim"]), ("active", "#ffffff")],
            bordercolor=[("disabled", C["border"]), ("active", C["green"])],
            lightcolor=[("disabled", C["border"]), ("active", C["green"])],
            darkcolor=[("disabled", C["border"]), ("active", C["green"])],
        )

    # Info / Blue Emphasized Button (Indigo — matches AUTO_RENDER Info.TButton)
    style.configure(
        "Info.TButton",
        background=C["accent"],
        foreground="#ffffff",
        bordercolor=C["accent"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        font=SYSTEM_FONT_BOLD,
        padding=(8, 4),
    )
    style.map(
        "Info.TButton",
        background=[
            ("disabled", C["accent_soft"]),
            ("active", C["accent_hover"]),
            ("pressed", "#4f46e5"),
        ],
        foreground=[("disabled", C["accent_hover"]), ("active", "#ffffff")],
        bordercolor=[("disabled", C["accent_border"]), ("active", C["accent_hover"])],
        lightcolor=[("disabled", C["accent_soft"]), ("active", C["accent_hover"])],
        darkcolor=[("disabled", C["accent_border"]), ("active", C["accent_hover"])],
    )

    # Danger / Red Button (Solid Rose-500 fill with crisp white text)
    style.configure(
        "Danger.TButton",
        background=C["red"],
        foreground="#ffffff",
        bordercolor=C["red"],
        lightcolor=C["red"],
        darkcolor=C["red"],
        font=SYSTEM_FONT_BOLD,
        padding=(8, 4),
    )
    style.map(
        "Danger.TButton",
        background=[
            ("disabled", "#ffe4e6"),
            ("active", C["danger_hover"]),
            ("pressed", "#be123c"),
        ],
        foreground=[("disabled", "#fb7185"), ("active", "#ffffff")],
        bordercolor=[("disabled", "#fecdd3"), ("active", C["danger_hover"])],
        lightcolor=[("disabled", "#ffe4e6"), ("active", C["danger_hover"])],
        darkcolor=[("disabled", "#fecdd3"), ("active", C["danger_hover"])],
    )

    # Flat Toggle Button (for [Parameters] toggle — highlights in Indigo like Info.TButton)
    style.configure(
        "Toolbar.Toolbutton",
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        font=SYSTEM_FONT_BOLD,
        padding=(8, 4),
    )
    style.map(
        "Toolbar.Toolbutton",
        background=[
            ("selected", C["accent"]),
            ("active", C["surface2"]),
        ],
        foreground=[
            ("selected", "#ffffff"),
            ("active", C["fg"]),
        ],
        bordercolor=[
            ("selected", C["accent"]),
            ("active", C["border"]),
        ],
        lightcolor=[
            ("selected", C["accent"]),
            ("active", C["surface2"]),
        ],
        darkcolor=[
            ("selected", C["accent"]),
            ("active", C["surface2"]),
        ],
    )

    # ── Radiobutton & Checkbutton ──
    style.configure(
        "TRadiobutton",
        background=C["bg"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
        indicatorcolor=C["border"],
    )
    style.map(
        "TRadiobutton",
        background=[("active", C["bg"])],
        indicatorcolor=[("selected", C["accent"])],
    )

    style.configure(
        "Card.TRadiobutton",
        background=C["bg_light"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
        indicatorcolor=C["border"],
    )
    style.map(
        "Card.TRadiobutton",
        background=[("active", C["bg_light"])],
        indicatorcolor=[("selected", C["accent"])],
    )

    style.configure(
        "TCheckbutton",
        background=C["bg_light"],
        foreground=C["fg"],
        font=SYSTEM_FONT,
        indicatorcolor=C["border"],
        padding=(2, 2),
    )
    style.map(
        "TCheckbutton",
        background=[("active", C["bg_light"])],
        indicatorcolor=[("selected", C["accent"])],
    )

    # ── Entry / Spinbox / Combobox (Slate-100 field, Slate-200 border, Indigo focus) ──
    style.configure(
        "TEntry",
        fieldbackground=C["bg_dark"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        insertcolor=C["fg"],
        font=SYSTEM_FONT,
        padding=(4, 2),
    )
    style.map("TEntry", bordercolor=[("focus", C["accent"])])

    style.configure(
        "TSpinbox",
        fieldbackground=C["bg_dark"],
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        arrowcolor=C["fg"],
        insertcolor=C["fg"],
        font=SYSTEM_FONT,
        padding=(4, 2),
    )
    style.map("TSpinbox", bordercolor=[("focus", C["accent"])])

    style.configure(
        "TCombobox",
        fieldbackground=C["bg_dark"],
        background=C["border"],
        foreground=C["fg"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        arrowcolor=C["fg"],
        font=SYSTEM_FONT,
        padding=(4, 2),
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", C["accent"])],
        fieldbackground=[("readonly", C["bg_dark"])],
        selectbackground=[("readonly", C["bg_dark"])],
        selectforeground=[("readonly", C["fg"])],
    )

    # ── Notebook (Slate + Indigo tabs) ──
    style.configure(
        "TNotebook",
        background=C["bg"],
        bordercolor=C["border"],
        lightcolor=C["bg"],
        darkcolor=C["border"],
        borderwidth=0,
        tabmargins=(8, 6, 0, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background=C["border"],
        foreground=C["fg_sub"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        padding=(12, 5),
        font=SYSTEM_FONT_BOLD,
    )
    style.map(
        "TNotebook.Tab",
        background=[
            ("selected", C["bg_light"]),
            ("active", C["surface2"]),
        ],
        foreground=[
            ("selected", C["accent"]),
            ("active", C["fg"]),
        ],
        bordercolor=[
            ("selected", C["border"]),
            ("active", C["border"]),
        ],
        lightcolor=[
            ("selected", C["bg_light"]),
            ("active", C["surface2"]),
        ],
        expand=[("selected", [0, 0, 0, 1])],
    )

    # ── Treeview (Identical to AUTO_RENDER_GUI) ──
    style.configure(
        "Treeview",
        background=C["bg_dark"],
        foreground=C["fg"],
        fieldbackground=C["bg_dark"],
        bordercolor=C["border"],
        font=SYSTEM_FONT,
        rowheight=26,
    )
    style.configure(
        "Treeview.Heading",
        background=C["border"],
        foreground=C["fg_sub"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        font=SYSTEM_FONT_BOLD,
        padding=(8, 4),
    )
    style.map(
        "Treeview",
        background=[("selected", C["accent_soft"])],
        foreground=[("selected", C["accent_text"])],
    )
    style.map(
        "Treeview.Heading",
        background=[("active", C["surface2"])],
        foreground=[("active", C["fg"])],
    )

    # ── Scrollbar (Identical to AUTO_RENDER_GUI) ──
    for sb_style in ("TScrollbar", "Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(
            sb_style,
            background=C["border"],
            troughcolor=C["bg_dark"],
            bordercolor=C["bg_dark"],
            arrowcolor=C["fg_dim"],
        )
        style.map(
            sb_style,
            background=[("active", C["fg_dim"]), ("pressed", C["fg_sub"])],
        )

    # ── Progressbar (Indigo-500 fill on Slate-200 trough) ──
    style.configure(
        "Horizontal.TProgressbar",
        background=C["accent"],
        troughcolor=C["border"],
        bordercolor=C["border"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        thickness=8,
        relief="flat",
        borderwidth=0,
    )

    # ── PanedWindow & Separator ──
    style.configure("TPanedwindow", background=C["bg"])
    style.configure("Sash", sashthickness=5, background=C["bg"])
    style.configure("TSeparator", background=C["border"])

    return style
