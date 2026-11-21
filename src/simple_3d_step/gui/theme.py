"""Centralized Modern Flat UI design tokens and ttk style configuration."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


# --- Modern Flat Slate & Cobalt Design Tokens ---
COLORS = {
    "app_bg": "#f1f5f9",          # Cool slate canvas background
    "surface": "#ffffff",         # Elevated crisp white card / toolbar
    "surface_alt": "#f8fafc",     # Subtle secondary strip (headers, footers)
    "surface_hover": "#f1f5f9",   # Soft hover surface
    "border": "#e2e8f0",          # 1px hairline border (slate-200)
    "border_strong": "#cbd5e1",   # Control outline (slate-300)
    "text": "#0f172a",            # Primary ink (slate-900)
    "text_secondary": "#475569",  # Secondary ink (slate-600)
    "text_muted": "#94a3b8",      # Disabled / hint ink (slate-400)
    # Brand accent (Cobalt)
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_soft": "#eff6ff",
    "accent_border": "#93c5fd",
    "accent_text": "#1e40af",
    # Success (Emerald)
    "success": "#059669",
    "success_soft": "#ecfdf5",
    "success_hover": "#d1fae5",
    "success_border": "#34d399",
    "success_text": "#065f46",
    # Danger / Internal (Rose)
    "danger": "#dc2626",
    "danger_hover": "#b91c1c",
    "danger_soft": "#fef2f2",
    "danger_text": "#991b1b",
    # Warning / Manual drop (Amber)
    "warning_soft": "#fffbeb",
    "warning_text": "#92400e",
    # Dark console
    "console_bg": "#0f172a",
    "console_fg": "#e2e8f0",
    "console_muted": "#64748b",
}

# Large high-readability fonts (~2x original 8-10pt scale)
FONTS = {
    "base": ("Segoe UI", 15),
    "base_bold": ("Segoe UI", 15, "bold"),
    "small": ("Segoe UI", 13),
    "small_bold": ("Segoe UI", 13, "bold"),
    "heading": ("Segoe UI", 17, "bold"),
    "mono": ("Consolas", 14),
    "mono_bold": ("Consolas", 14, "bold"),
}

# Sequential workflow button visual states (1px flat border + bold emphasis in all states)
STEP_STYLES = {
    "idle": {
        "bg": "#ffffff",
        "hover_bg": "#f1f5f9",
        "fg": "#1e293b",
        "border": "#cbd5e1",
        "prefix": "",
        "font": FONTS["base_bold"],
    },
    "running": {
        "bg": "#eff6ff",
        "hover_bg": "#dbeafe",
        "fg": "#1d4ed8",
        "border": "#3b82f6",
        "prefix": "● ",
        "font": FONTS["base_bold"],
    },
    "done": {
        "bg": "#ecfdf5",
        "hover_bg": "#d1fae5",
        "fg": "#065f46",
        "border": "#34d399",
        "prefix": "✔ ",
        "font": FONTS["base_bold"],
    },
}


def apply_modern_flat_theme(root: tk.Tk) -> ttk.Style:
    """Configure a sleek, high-readability Modern Flat ttk theme built on 'clam'."""
    root.configure(background=COLORS["app_bg"])

    # Ensure dropdown popups (Combobox listbox, menus) also inherit larger readable font
    root.option_add("*TCombobox*Listbox.font", FONTS["small"])
    root.option_add("*Font", FONTS["base"])

    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    # Global defaults
    style.configure(
        ".",
        background=COLORS["app_bg"],
        foreground=COLORS["text"],
        font=FONTS["base"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface"],
        darkcolor=COLORS["border"],
        focuscolor=COLORS["accent"],
    )

    # Frames
    style.configure("TFrame", background=COLORS["app_bg"])
    style.configure("Card.TFrame", background=COLORS["surface"])
    style.configure("Subtle.TFrame", background=COLORS["surface_alt"])
    style.configure("Toolbar.TFrame", background=COLORS["surface"])

    # Labels
    style.configure("TLabel", background=COLORS["app_bg"], foreground=COLORS["text"], font=FONTS["base"])
    style.configure("Card.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=FONTS["small"])
    style.configure(
        "Muted.TLabel",
        background=COLORS["surface"],
        foreground=COLORS["text_secondary"],
        font=FONTS["small"],
    )
    style.configure(
        "Subtle.TLabel",
        background=COLORS["surface_alt"],
        foreground=COLORS["text_secondary"],
        font=FONTS["small"],
    )
    style.configure(
        "Accent.TLabel",
        background=COLORS["surface"],
        foreground=COLORS["accent_text"],
        font=FONTS["base_bold"],
    )
    style.configure(
        "SectionTitle.TLabel",
        background=COLORS["surface"],
        foreground=COLORS["text"],
        font=FONTS["heading"],
    )

    # Flat LabelFrames
    style.configure(
        "TLabelframe",
        background=COLORS["surface"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface"],
        darkcolor=COLORS["border"],
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "TLabelframe.Label",
        background=COLORS["surface"],
        foreground=COLORS["text_secondary"],
        font=FONTS["small_bold"],
    )

    # Standard Flat Buttons
    style.configure(
        "TButton",
        background=COLORS["surface"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border_strong"],
        lightcolor=COLORS["surface"],
        darkcolor=COLORS["border_strong"],
        borderwidth=1,
        relief="solid",
        padding=(10, 4),
        font=FONTS["small"],
    )
    style.map(
        "TButton",
        background=[
            ("disabled", COLORS["surface_alt"]),
            ("pressed", "#e2e8f0"),
            ("active", COLORS["surface_hover"]),
        ],
        foreground=[
            ("disabled", COLORS["text_muted"]),
            ("active", COLORS["text"]),
        ],
        bordercolor=[
            ("disabled", COLORS["border"]),
            ("active", "#94a3b8"),
        ],
    )

    # Toolbar Flat Buttons
    style.configure(
        "Toolbar.TButton",
        background=COLORS["surface_alt"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface_alt"],
        darkcolor=COLORS["border"],
        borderwidth=1,
        relief="solid",
        padding=(10, 4),
        font=FONTS["small"],
    )
    style.map(
        "Toolbar.TButton",
        background=[
            ("disabled", COLORS["surface"]),
            ("pressed", "#e2e8f0"),
            ("active", "#e2e8f0"),
        ],
        foreground=[
            ("disabled", COLORS["text_muted"]),
            ("active", COLORS["text"]),
        ],
        bordercolor=[
            ("disabled", COLORS["border"]),
            ("active", COLORS["border_strong"]),
        ],
    )

    # Primary Cobalt Accent Button
    style.configure(
        "Primary.TButton",
        background=COLORS["accent"],
        foreground="#ffffff",
        bordercolor=COLORS["accent"],
        lightcolor=COLORS["accent"],
        darkcolor=COLORS["accent_hover"],
        borderwidth=1,
        relief="flat",
        padding=(12, 6),
        font=FONTS["small_bold"],
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", "#cbd5e1"),
            ("pressed", "#1e40af"),
            ("active", COLORS["accent_hover"]),
        ],
        foreground=[("disabled", "#f8fafc")],
        bordercolor=[
            ("disabled", "#cbd5e1"),
            ("active", COLORS["accent_hover"]),
        ],
    )

    # Danger Flat Button (Cancel / Force stop)
    style.configure(
        "Danger.TButton",
        background=COLORS["danger_soft"],
        foreground=COLORS["danger_text"],
        bordercolor="#fca5a5",
        lightcolor=COLORS["danger_soft"],
        darkcolor="#fca5a5",
        borderwidth=1,
        relief="solid",
        padding=(10, 4),
        font=FONTS["small_bold"],
    )
    style.map(
        "Danger.TButton",
        background=[
            ("disabled", COLORS["surface_alt"]),
            ("pressed", "#fecaca"),
            ("active", "#fee2e2"),
        ],
        foreground=[("disabled", COLORS["text_muted"])],
        bordercolor=[("disabled", COLORS["border"])],
    )

    # Compact Small Flat Buttons (Presets, Filter actions)
    style.configure(
        "Small.TButton",
        background=COLORS["surface_alt"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border_strong"],
        lightcolor=COLORS["surface_alt"],
        darkcolor=COLORS["border_strong"],
        borderwidth=1,
        relief="solid",
        padding=(8, 3),
        font=FONTS["small"],
    )
    style.map(
        "Small.TButton",
        background=[
            ("disabled", COLORS["surface_alt"]),
            ("pressed", "#e2e8f0"),
            ("active", "#e2e8f0"),
        ],
        foreground=[("disabled", COLORS["text_muted"])],
    )

    # Modern Flat Toggle Button (for [Parameters] button)
    style.configure(
        "Toolbar.Toolbutton",
        background=COLORS["surface_alt"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface_alt"],
        darkcolor=COLORS["border"],
        borderwidth=1,
        relief="solid",
        padding=(10, 4),
        font=FONTS["small_bold"],
    )
    style.map(
        "Toolbar.Toolbutton",
        background=[
            ("selected", COLORS["accent_soft"]),
            ("active", "#e2e8f0"),
        ],
        foreground=[
            ("selected", COLORS["accent_text"]),
            ("active", COLORS["text"]),
        ],
        bordercolor=[
            ("selected", COLORS["accent"]),
            ("active", COLORS["border_strong"]),
        ],
    )

    # Checkbuttons on white card surface
    style.configure(
        "TCheckbutton",
        background=COLORS["surface"],
        foreground=COLORS["text"],
        indicatorcolor=COLORS["surface"],
        indicatorrelief="solid",
        padding=(2, 2),
        font=FONTS["small"],
    )
    style.map(
        "TCheckbutton",
        background=[("active", COLORS["surface"])],
        indicatorcolor=[
            ("selected", COLORS["accent"]),
            ("active", COLORS["accent_soft"]),
        ],
    )

    # Flat Inputs: Entry, Combobox, Spinbox
    for widget_style in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            widget_style,
            fieldbackground=COLORS["surface"],
            background=COLORS["surface_alt"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border_strong"],
            lightcolor=COLORS["surface"],
            darkcolor=COLORS["border_strong"],
            arrowcolor=COLORS["text_secondary"],
            padding=(6, 4),
            relief="solid",
            borderwidth=1,
            font=FONTS["small"],
        )
        style.map(
            widget_style,
            bordercolor=[("focus", COLORS["accent"]), ("active", "#94a3b8")],
            fieldbackground=[("readonly", COLORS["surface"])],
        )

    # Flat Modern Notebook Tabs
    style.configure(
        "TNotebook",
        background=COLORS["surface"],
        borderwidth=0,
        tabmargins=(0, 0, 0, 0),
    )
    style.configure(
        "TNotebook.Tab",
        background=COLORS["surface_alt"],
        foreground=COLORS["text_secondary"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface_alt"],
        darkcolor=COLORS["border"],
        padding=(10, 6),
        font=FONTS["small"],
    )
    style.map(
        "TNotebook.Tab",
        background=[
            ("selected", COLORS["surface"]),
            ("active", "#e2e8f0"),
        ],
        foreground=[
            ("selected", COLORS["accent"]),
            ("active", COLORS["text"]),
        ],
        font=[("selected", FONTS["small_bold"])],
        expand=[("selected", [0, 0, 0, 1])],
    )

    # Flat Crisp Treeview (scaled rowheight for large 13-15pt UI text)
    style.configure(
        "Treeview",
        background=COLORS["surface"],
        fieldbackground=COLORS["surface"],
        foreground=COLORS["text"],
        borderwidth=0,
        relief="flat",
        rowheight=36,
        font=FONTS["small"],
    )
    style.map(
        "Treeview",
        background=[("selected", "#dbeafe")],
        foreground=[("selected", "#1e3a8a")],
    )
    style.configure(
        "Treeview.Heading",
        background=COLORS["surface_alt"],
        foreground=COLORS["text_secondary"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["surface_alt"],
        darkcolor=COLORS["border"],
        relief="flat",
        padding=(8, 6),
        font=FONTS["small_bold"],
    )
    style.map(
        "Treeview.Heading",
        background=[("active", "#e2e8f0")],
        foreground=[("active", COLORS["text"])],
    )

    # Slim Flat Scrollbar
    style.configure(
        "Vertical.TScrollbar",
        background="#cbd5e1",
        troughcolor=COLORS["surface_alt"],
        bordercolor=COLORS["surface_alt"],
        arrowcolor=COLORS["text_secondary"],
        relief="flat",
        borderwidth=0,
        width=14,
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("active", "#94a3b8"), ("pressed", "#64748b")],
    )
    style.configure(
        "Horizontal.TScrollbar",
        background="#cbd5e1",
        troughcolor=COLORS["surface_alt"],
        bordercolor=COLORS["surface_alt"],
        arrowcolor=COLORS["text_secondary"],
        relief="flat",
        borderwidth=0,
        width=14,
    )
    style.map(
        "Horizontal.TScrollbar",
        background=[("active", "#94a3b8"), ("pressed", "#64748b")],
    )

    # Flat Cobalt Progressbar
    style.configure(
        "Horizontal.TProgressbar",
        background=COLORS["accent"],
        troughcolor="#e2e8f0",
        bordercolor="#e2e8f0",
        lightcolor=COLORS["accent"],
        darkcolor=COLORS["accent"],
        thickness=9,
        relief="flat",
        borderwidth=0,
    )

    # PanedWindow & Separator
    style.configure("TPanedwindow", background=COLORS["app_bg"])
    style.configure("Sash", sashthickness=5, background=COLORS["app_bg"])
    style.configure("TSeparator", background=COLORS["border"])

    return style
