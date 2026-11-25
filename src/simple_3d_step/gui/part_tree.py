"""Modern Flat assembly part table card with search, filter, sort, and manual toggle."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from simple_3d_step.gui.theme import COLORS, FONTS
from simple_3d_step.models import PartNode, PartStatus


class PartTreePanel(tk.Frame):
    """Elevated white flat card displaying all STEP leaf parts with keep/drop overrides."""

    COLUMNS = ("keep", "id", "name", "visibility", "volume", "faces", "status", "path")

    def __init__(
        self,
        parent: tk.Misc,
        on_selection_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            background=COLORS["surface"],
            highlightbackground=COLORS["border"],
            highlightthickness=1,
        )
        self._parts: list[PartNode] = []
        self._part_by_id: dict[int, PartNode] = {}
        self._on_selection_changed = on_selection_changed

        self._search_var = tk.StringVar(value="")
        self._filter_var = tk.StringVar(value="All")
        self._summary_var = tk.StringVar(value="No parts loaded")
        self._sort_col: str = "id"
        self._sort_reverse: bool = False

        self._build_ui()

    def _build_ui(self) -> None:
        # Clean top filter & quick-action header strip
        top_bar = ttk.Frame(self, style="Subtle.TFrame", padding=(8, 4))
        top_bar.pack(fill=tk.X)

        ttk.Label(top_bar, text="Search", style="Subtle.TLabel", font=FONTS["small_bold"]).pack(
            side=tk.LEFT
        )
        search_entry = ttk.Entry(top_bar, textvariable=self._search_var, width=14)
        search_entry.pack(side=tk.LEFT, padx=(6, 10))
        self._search_var.trace_add("write", lambda *_: self.refresh_view())

        ttk.Label(top_bar, text="Filter", style="Subtle.TLabel", font=FONTS["small_bold"]).pack(
            side=tk.LEFT
        )
        filter_combo = ttk.Combobox(
            top_bar,
            textvariable=self._filter_var,
            values=("All", "Keep (External)", "Drop (Internal)", "Manual Override"),
            state="readonly",
            width=15,
        )
        filter_combo.pack(side=tk.LEFT, padx=(6, 10))
        filter_combo.bind("<<ComboboxSelected>>", lambda *_: self.refresh_view())

        ttk.Button(
            top_bar,
            text="Keep Selected",
            style="Small.TButton",
            command=lambda: self._set_selected_keep(True),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            top_bar,
            text="Drop Selected",
            style="Small.TButton",
            command=lambda: self._set_selected_keep(False),
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            top_bar,
            text="Reset Overrides",
            style="Small.TButton",
            command=self._reset_manual_overrides,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Hairline divider
        tk.Frame(self, height=1, background=COLORS["border"]).pack(fill=tk.X)

        # Treeview + flat scrollbars
        tree_frame = tk.Frame(self, background=COLORS["surface"])
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self._tree = ttk.Treeview(
            tree_frame,
            columns=self.COLUMNS,
            show="headings",
            selectmode="extended",
            height=6,
        )

        headings = {
            "keep": ("Keep", 52, tk.CENTER),
            "id": ("#", 36, tk.E),
            "name": ("Part Name", 155, tk.W),
            "visibility": ("Visibility (%)", 114, tk.E),
            "volume": ("Volume (mm³)", 126, tk.E),
            "faces": ("Faces", 62, tk.E),
            "status": ("Status", 120, tk.CENTER),
            "path": ("Assembly Path", 210, tk.W),
        }

        for col, (label, width, anchor) in headings.items():
            self._tree.heading(col, text=label, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=width, minwidth=40, anchor=anchor)

        self._tree.tag_configure("keep_ext", foreground=COLORS["fg"], background=COLORS["bg_dark"])
        self._tree.tag_configure("keep_ext_alt", foreground=COLORS["fg"], background=COLORS["bg_light"])
        self._tree.tag_configure(
            "drop_int", foreground=COLORS["danger_text"], background=COLORS["danger_soft"]
        )
        self._tree.tag_configure(
            "manual_keep", foreground=COLORS["accent_text"], background=COLORS["accent_soft"]
        )
        self._tree.tag_configure(
            "manual_drop", foreground=COLORS["warning_text"], background=COLORS["warning_soft"]
        )

        yscroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        xscroll = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self._tree.grid(row=0, column=0, sticky=tk.NSEW)
        yscroll.grid(row=0, column=1, sticky=tk.NS)
        xscroll.grid(row=1, column=0, sticky=tk.EW)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<space>", self._on_space_toggle)

        # Hairline divider + bottom summary strip
        tk.Frame(self, height=1, background=COLORS["border"]).pack(fill=tk.X)
        summary_frame = ttk.Frame(self, style="Subtle.TFrame", padding=(8, 4))
        summary_frame.pack(fill=tk.X)
        ttk.Label(
            summary_frame,
            textvariable=self._summary_var,
            style="Subtle.TLabel",
            foreground=COLORS["text"],
            font=FONTS["small_bold"],
        ).pack(side=tk.LEFT)
        ttk.Label(
            summary_frame,
            text="Double-click or press Space to toggle Keep/Drop",
            style="Subtle.TLabel",
            foreground=COLORS["text_muted"],
            font=FONTS["small"],
        ).pack(side=tk.RIGHT)

    def set_parts(self, parts: list[PartNode]) -> None:
        self._parts = parts
        self._part_by_id = {p.part_id: p for p in parts}
        self.refresh_view()

    def refresh_view(self) -> None:
        selected_iids = set(self._tree.selection())
        self._tree.delete(*self._tree.get_children())

        query = self._search_var.get().strip().lower()
        filt = self._filter_var.get()

        filtered: list[PartNode] = []
        for p in self._parts:
            if query and query not in p.name.lower() and query not in p.assembly_path.lower():
                continue
            if filt == "Keep (External)" and not p.keep:
                continue
            if filt == "Drop (Internal)" and p.keep:
                continue
            if filt == "Manual Override" and not p.user_overridden:
                continue
            filtered.append(p)

        key_map = {
            "keep": lambda p: (0 if p.keep else 1, p.part_id),
            "id": lambda p: p.part_id,
            "name": lambda p: p.name.lower(),
            "visibility": lambda p: p.visibility_score,
            "volume": lambda p: p.volume,
            "faces": lambda p: p.face_count,
            "status": lambda p: p.effective_status_label,
            "path": lambda p: p.assembly_path.lower(),
        }
        filtered.sort(
            key=key_map.get(self._sort_col, lambda p: p.part_id), reverse=self._sort_reverse
        )

        for row_idx, p in enumerate(filtered):
            iid = str(p.part_id)
            keep_mark = "☑" if p.keep else "☐"
            vis_str = (
                "-"
                if p.auto_status == PartStatus.UNKNOWN
                else f"{p.visibility_score * 100.0:.2f}%"
            )
            vol_str = f"{p.volume:,.1f}"

            if p.user_overridden:
                tag = "manual_keep" if p.keep else "manual_drop"
            elif p.keep:
                tag = "keep_ext_alt" if (row_idx % 2 == 1) else "keep_ext"
            else:
                tag = "drop_int"

            self._tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    keep_mark,
                    p.part_id,
                    p.name,
                    vis_str,
                    vol_str,
                    p.face_count,
                    p.effective_status_label,
                    p.assembly_path,
                ),
                tags=(tag,),
            )

        reselect = [iid for iid in selected_iids if self._tree.exists(iid)]
        if reselect:
            self._tree.selection_set(reselect)

        self._update_summary()

    def _update_summary(self) -> None:
        total = len(self._parts)
        if total == 0:
            self._summary_var.set("No parts loaded")
            return
        kept = [p for p in self._parts if p.keep]
        dropped = total - len(kept)
        kept_vol = sum(max(p.volume, 0.0) for p in kept)
        self._summary_var.set(
            f"Total: {total}  ·  Keep (Outer): {len(kept)}  ·  Drop (Inner): {dropped}  ·  Kept Vol: {kept_vol:,.1f} mm³"
        )

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = col in ("visibility", "volume", "faces")
        self.refresh_view()

    def _on_double_click(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        part = self._part_by_id.get(int(iid))
        if part is not None:
            part.keep = not part.keep
            part.user_overridden = True
            self.refresh_view()
            if self._on_selection_changed:
                self._on_selection_changed()

    def _on_space_toggle(self, _event: tk.Event) -> str:
        selected = self._tree.selection()
        if not selected:
            return "break"
        first = self._part_by_id.get(int(selected[0]))
        if first is None:
            return "break"
        self._set_selected_keep(not first.keep)
        return "break"

    def _set_selected_keep(self, keep: bool) -> None:
        selected = self._tree.selection()
        if not selected:
            return
        for iid in selected:
            part = self._part_by_id.get(int(iid))
            if part is not None:
                part.keep = keep
                part.user_overridden = True
        self.refresh_view()
        if self._on_selection_changed:
            self._on_selection_changed()

    def _reset_manual_overrides(self) -> None:
        for p in self._parts:
            p.user_overridden = False
            if p.auto_status == PartStatus.INTERNAL:
                p.keep = False
            else:
                p.keep = True
        self.refresh_view()
        if self._on_selection_changed:
            self._on_selection_changed()
