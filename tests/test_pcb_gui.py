"""GUI & worker integration tests for the PCB Mode tab."""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from simple_3d_step.config import AppConfig
from simple_3d_step.gui.app import Simple3DStepApp
from simple_3d_step.gui.batch_panel import ST_DONE, ST_PENDING
from simple_3d_step.gui.pcb_panel import (
    PcbPanel,
    build_pcb_simplify_config,
    default_pcb_output_path,
)
from simple_3d_step.gui.theme import apply_modern_flat_theme
from tests.generate_sample_pcb import generate_sample_pcb_step_and_idf


@pytest.fixture(autouse=True)
def _isolate_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg_path = tmp_path / "pcb_test_config.json"
    monkeypatch.setattr(
        "simple_3d_step.gui.app.get_default_config_path",
        lambda: cfg_path,
    )
    return cfg_path


@pytest.fixture
def pcb_panel(tmp_path: Path):
    root = tk.Tk()
    root.geometry("1100x700")
    apply_modern_flat_theme(root)
    panel = PcbPanel(root, config_path=tmp_path / "pcb_panel_config.json")
    panel.pack(fill=tk.BOTH, expand=True)
    root.update()
    try:
        yield panel
    finally:
        panel.shutdown()
        root.destroy()


def test_default_pcb_output_path_and_config_mapping(tmp_path: Path) -> None:
    src = tmp_path / "boards" / "main_pcb.stp"
    assert default_pcb_output_path(src) == tmp_path / "boards" / "main_pcb_pcb_simplified.step"

    cfg = AppConfig(
        pcb_simplify_leads=False,
        pcb_detect_cylinders=True,
        pcb_cylinder_circularity_min=0.85,
        pcb_remove_text_features=False,
        pcb_small_part_box_mm=3.5,
        pcb_preserve_colors=True,
        pcb_use_idf_supplement=False,
        pcb_keep_board_holes=True,
        pcb_min_board_hole_mm=1.0,
        pcb_exclude_small_part_mm=2.5,
        pcb_min_color_brightness=0.15,
        pcb_idf_default_color=[0.5, 0.4, 0.3],
        step_schema="AP242",
    )
    pcb_cfg = build_pcb_simplify_config(cfg)
    assert pcb_cfg.simplify_leads is False
    assert pcb_cfg.cylinder_circularity_min == pytest.approx(0.85)
    assert pcb_cfg.remove_text_features is False
    assert pcb_cfg.small_part_box_mm == pytest.approx(3.5)
    assert pcb_cfg.use_idf_supplement is False
    assert pcb_cfg.min_board_hole_mm == pytest.approx(1.0)
    assert pcb_cfg.exclude_small_part_mm == pytest.approx(2.5)
    assert pcb_cfg.min_color_brightness == pytest.approx(0.15)
    assert pcb_cfg.idf_default_color == pytest.approx((0.5, 0.4, 0.3))
    assert pcb_cfg.step_schema == "AP242"


def test_unified_app_has_pcb_mode_tab_and_lazy_worker() -> None:
    app = Simple3DStepApp()
    try:
        app.update()
        assert len(app._notebook.tabs()) == 3
        assert "PCB Mode" in app._notebook.tab(2, "text")
        assert app._pcb_panel._worker is None

        app._select_mode("pcb")
        app.update()
        assert app._current_mode() == "pcb"
        assert app._pcb_panel._worker is not None
    finally:
        app._on_close()


def test_pcb_panel_auto_and_manual_idf_attachment(pcb_panel: PcbPanel, tmp_path: Path) -> None:
    step_a = tmp_path / "board1.step"
    bdf_a = tmp_path / "board1.bdf"
    ldf_a = tmp_path / "board1.ldf"
    step_b = tmp_path / "board2.step"
    alt_bdf = tmp_path / "custom.bdf"
    alt_ldf = tmp_path / "custom.ldf"
    for p in (step_a, bdf_a, ldf_a, step_b, alt_bdf, alt_ldf):
        p.write_text("dummy", encoding="utf-8")

    pcb_panel.queue_files([step_a, step_b])
    assert len(pcb_panel._items) == 2
    assert pcb_panel._items[0].bdf_path == bdf_a.resolve()
    assert pcb_panel._items[0].ldf_path == ldf_a.resolve()
    assert pcb_panel._items[0].idf_label == "Auto (bdf+ldf)"
    assert pcb_panel._items[1].bdf_path is None
    assert pcb_panel._items[1].idf_label == "—"

    # Manually attach custom.bdf (should auto-discover sibling custom.ldf)
    pcb_panel.attach_idf_files(1, alt_bdf)
    assert pcb_panel._items[1].bdf_path == alt_bdf.resolve()
    assert pcb_panel._items[1].ldf_path == alt_ldf.resolve()
    assert pcb_panel._items[1].idf_label == "Manual (bdf+ldf)"

    # Clear manual IDF attachment
    pcb_panel.attach_idf_files(1, None)
    assert pcb_panel._items[1].idf_label == "—"


def test_pcb_panel_end_to_end_run_and_double_click_viewer(
    pcb_panel: PcbPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step_path, _bdf, _ldf = generate_sample_pcb_step_and_idf(tmp_path, stem="gui_pcb")
    pcb_panel.queue_files([step_path])
    assert pcb_panel._items[0].idf_label == "Auto (bdf+ldf)"
    assert pcb_panel._items[0].status == ST_PENDING

    pcb_panel.run()
    deadline = time.monotonic() + 30.0
    while pcb_panel.running and time.monotonic() < deadline:
        pcb_panel.update()
        time.sleep(0.05)

    assert not pcb_panel.running
    assert pcb_panel._items[0].status == ST_DONE
    out_file = pcb_panel._items[0].output_path
    assert out_file is not None and out_file.is_file()
    assert out_file.name == "gui_pcb_pcb_simplified.step"

    # Double-click opens simplified output in external viewer
    opened: list[Path] = []
    monkeypatch.setattr(
        "simple_3d_step.gui.pcb_panel.launch_external_viewer",
        lambda p, _cfg: (opened.append(Path(p)), "Launched")[1],
    )
    pcb_panel.update()
    bbox = pcb_panel._tree.bbox("0")
    assert bbox
    pcb_panel._on_tree_double_click(SimpleNamespace(y=bbox[1] + bbox[3] // 2))
    assert opened == [out_file]


def test_pcb_panel_gui_hole_and_small_part_controls_sync_and_persist(
    pcb_panel: PcbPanel,
) -> None:
    # Right-hand PCB Parameters panel is hidden by default (matches Single Mode)
    assert pcb_panel._params_visible_var.get() is False
    assert len(pcb_panel._main_pane.panes()) == 1

    # Toggle [Parameters] ON -> shows right-hand drawer without clipping LogPanel
    pcb_panel._params_visible_var.set(True)
    pcb_panel._toggle_params_panel()
    pcb_panel.update()
    assert len(pcb_panel._main_pane.panes()) == 2
    assert pcb_panel._params_panel.winfo_width() >= 200
    assert pcb_panel._log_panel.winfo_y() + pcb_panel._log_panel.winfo_height() <= pcb_panel.winfo_height()

    # Edit spinbox controls, save to config.json, and verify live summary & persistence
    pcb_panel._hole_limit_var.set("1.2")
    pcb_panel._exclude_small_var.set("2.5")
    pcb_panel._min_bright_var.set("0.16")
    pcb_panel._on_save_params()

    assert pcb_panel._config.pcb_min_board_hole_mm == pytest.approx(1.2)
    assert pcb_panel._config.pcb_exclude_small_part_mm == pytest.approx(2.5)
    assert pcb_panel._config.pcb_min_color_brightness == pytest.approx(0.16)
    assert "<1.2 mm" in pcb_panel._param_var1.get()
    assert "≤2.5 mm" in pcb_panel._param_var1.get()
    assert "0.16" in pcb_panel._param_var1.get()

    cfg = build_pcb_simplify_config(pcb_panel._config)
    assert cfg.min_board_hole_mm == pytest.approx(1.2)
    assert cfg.exclude_small_part_mm == pytest.approx(2.5)
    assert cfg.min_color_brightness == pytest.approx(0.16)

    # Toggle [Parameters] OFF -> hides right-hand drawer cleanly
    pcb_panel._params_visible_var.set(False)
    pcb_panel._toggle_params_panel()
    pcb_panel.update()
    assert len(pcb_panel._main_pane.panes()) == 1
