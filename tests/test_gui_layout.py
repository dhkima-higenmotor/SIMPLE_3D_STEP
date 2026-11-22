"""GUI layout, zero-clipping, parameter toggle, and save-directory tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from simple_3d_step.gui.app import Simple3DStepApp
from simple_3d_step.gui.theme import STEP_STYLES
from simple_3d_step.models import AssemblyModel, SimplifyResult, ValidationReport


@pytest.fixture(autouse=True)
def _isolate_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent GUI tests from overwriting the user's real config.json."""
    monkeypatch.setattr(
        "simple_3d_step.gui.app.get_default_config_path",
        lambda: tmp_path / "test_config.json",
    )


def test_packaged_window_icon_is_available() -> None:
    """The app icon ships with the package and is a valid multi-size .ico."""
    import simple_3d_step.gui.app as app_module

    icon = Path(app_module.__file__).with_name("assets") / "app.ico"
    assert icon.is_file()
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_gui_default_layout_no_menu_no_clipping_and_params_toggle() -> None:
    app = Simple3DStepApp()
    try:
        app.update()
        ww = app.winfo_width()
        wh = app.winfo_height()
        # 1. Menu bar removed & compact default window size
        assert app.cget("menu") == ""
        assert ww <= 1160 and wh <= 780
        # 2. Both toolbar rows fit horizontally inside compact window with large fonts
        assert app._toolbar_row1.winfo_reqwidth() <= ww
        assert app._toolbar_row2.winfo_reqwidth() <= ww
        # 3. Parameter panel hidden by default
        assert len(app._main_pane.panes()) == 1
        assert app._params_visible_var.get() is False
        # 4. Bottom log panel completely inside window (no clipping)
        lp_y = app._log_panel.winfo_y()
        lp_h = app._log_panel.winfo_height()
        lp_req = app._log_panel.winfo_reqheight()
        assert lp_y + lp_h <= wh
        assert lp_h >= lp_req - 2

        # 5. Toggle parameters ON + show BusyBanner simultaneously (worst-case height)
        app._params_visible_var.set(True)
        app._toggle_params_panel()
        app._show_busy_banner("Test Task")
        app.update()

        assert len(app._main_pane.panes()) == 2
        lp_y2 = app._log_panel.winfo_y()
        lp_h2 = app._log_panel.winfo_height()
        assert lp_y2 + lp_h2 <= app.winfo_height()
        assert lp_h2 >= lp_req - 2
        assert app._main_pane.winfo_height() >= 220
        assert app._control_panel.winfo_width() >= 280
        assert app._control_panel._canvas.bbox("all") is not None

        # 5. Toggle parameters OFF again
        app._params_visible_var.set(False)
        app._toggle_params_panel()
        app._hide_busy_banner()
        app.update()
        assert len(app._main_pane.panes()) == 1
    finally:
        app._on_close()


def test_save_dialog_defaults_to_source_step_directory(tmp_path: Path) -> None:
    src_dir = tmp_path / "my_cad_folder"
    src_dir.mkdir()
    src_step = src_dir / "assembly_01.step"
    src_step.write_text("ISO-10303-21;", encoding="utf-8")

    app = Simple3DStepApp()
    try:
        # Even if last_save_dir points elsewhere, save dialog must propose src_step.parent
        app._config.last_save_dir = str(tmp_path / "other_folder")
        app._assembly = AssemblyModel(source_path=src_step, parts=[])
        app._result = SimplifyResult(
            shape=None,
            mode_used="exact",
            validation=ValidationReport(
                is_valid=True,
                solid_count=1,
                shell_count=1,
                face_count=6,
                free_edge_count=0,
                is_watertight=True,
                volume=100.0,
                surface_area=100.0,
                bbox_size=(10.0, 10.0, 10.0),
            ),
            kept_part_count=1,
            removed_part_count=0,
            original_volume=100.0,
            filled_volume_delta=0.0,
            elapsed_seconds=0.1,
        )

        captured: dict[str, object] = {}

        def _fake_saveas(**kwargs):
            captured.update(kwargs)
            return ""  # Cancel save

        with patch("simple_3d_step.gui.app.filedialog.asksaveasfilename", side_effect=_fake_saveas):
            app._on_save_result()

        assert captured["initialdir"] == str(src_dir)
        assert captured["initialfile"] == "assembly_01_simplified.step"
    finally:
        app._on_close()


def test_workflow_step_buttons_visual_completion_states() -> None:
    app = Simple3DStepApp()
    try:
        app.update()
        # Verify Row-1 primary workflow button label is English
        assert app._tb_open.cget("text") == "Open STEP"

        # Initially all 4 workflow buttons are idle and bold
        for key in ("open", "analyze", "simplify", "save"):
            assert app._step_states[key] == "idle"
            btn, label = app._step_buttons[key]
            assert btn.cget("text") == label
            assert btn.cget("background") == STEP_STYLES["idle"]["bg"]
            assert "bold" in str(btn.cget("font")).lower()

        # Verify running state preserves bold font
        app._apply_step_style("analyze", "running")
        assert "bold" in str(app._step_buttons["analyze"][0].cget("font")).lower()

        # Mark steps done sequentially and verify bold font persists
        app._apply_step_style("open", "done")
        app._apply_step_style("analyze", "done")
        app._apply_step_style("simplify", "done")
        app._apply_step_style("save", "done")
        for key in ("open", "analyze", "simplify", "save"):
            btn, label = app._step_buttons[key]
            assert btn.cget("text") == f"✔ {label}"
            assert btn.cget("background") == STEP_STYLES["done"]["bg"]
            assert btn.cget("foreground") == STEP_STYLES["done"]["fg"]
            assert "bold" in str(btn.cget("font")).lower()

        # Manual part selection modification resets downstream simplify & save states (still bold)
        app._on_part_selection_modified()
        assert app._step_states["open"] == "done"
        assert app._step_states["analyze"] == "done"
        assert app._step_states["simplify"] == "idle"
        assert app._step_states["save"] == "idle"
        for key in ("simplify", "save"):
            assert "bold" in str(app._step_buttons[key][0].cget("font")).lower()
    finally:
        app._on_close()

