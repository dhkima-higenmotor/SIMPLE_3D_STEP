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
        pcb_detect_hex_posts=False,
        pcb_cylinder_circularity_min=0.85,
        pcb_remove_text_features=False,
        pcb_small_part_box_mm=3.5,
        pcb_preserve_simple_max_faces=45,
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
    assert pcb_cfg.detect_hex_posts is False
    assert pcb_cfg.cylinder_circularity_min == pytest.approx(0.85)
    assert pcb_cfg.remove_text_features is False
    assert pcb_cfg.small_part_box_mm == pytest.approx(3.5)
    assert pcb_cfg.preserve_simple_max_faces == 45
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
    pcb_panel._keep_simple_var.set("40")
    pcb_panel._on_save_params()

    assert pcb_panel._config.pcb_min_board_hole_mm == pytest.approx(1.2)
    assert pcb_panel._config.pcb_exclude_small_part_mm == pytest.approx(2.5)
    assert pcb_panel._config.pcb_min_color_brightness == pytest.approx(0.16)
    assert pcb_panel._config.pcb_preserve_simple_max_faces == 40
    assert "<1.2 mm" in pcb_panel._param_var1.get()
    assert "≤2.5 mm" in pcb_panel._param_var1.get()
    assert "0.16" in pcb_panel._param_var1.get()
    assert "≤40f" in pcb_panel._param_var2.get()

    cfg = build_pcb_simplify_config(pcb_panel._config)
    assert cfg.min_board_hole_mm == pytest.approx(1.2)
    assert cfg.exclude_small_part_mm == pytest.approx(2.5)
    assert cfg.min_color_brightness == pytest.approx(0.16)
    assert cfg.preserve_simple_max_faces == 40

    # Toggle [Parameters] OFF -> hides right-hand drawer cleanly
    pcb_panel._params_visible_var.set(False)
    pcb_panel._toggle_params_panel()
    pcb_panel.update()
    assert len(pcb_panel._main_pane.panes()) == 1


def test_last_open_dir_remembered_and_shared_across_all_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening/adding a STEP file in any tab persists its folder to config.json
    and immediately becomes the initialdir for the next open dialog in every tab."""
    from simple_3d_step.config import load_config

    dir_pcb = tmp_path / "proj_pcb"
    dir_batch = tmp_path / "proj_batch"
    dir_single = tmp_path / "proj_single"
    for d in (dir_pcb, dir_batch, dir_single):
        d.mkdir()
    pcb_step = dir_pcb / "board_a.step"
    batch_step = dir_batch / "housing_b.step"
    single_step = dir_single / "cover_c.step"
    for f in (pcb_step, batch_step, single_step):
        f.write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")

    app = Simple3DStepApp()
    try:
        app.update()
        seen_init_dirs: list[tuple[str, str]] = []

        # 1. Add STEP file in PCB Mode -> saves dir_pcb to config.json
        def _fake_pcb_open(**kwargs):
            seen_init_dirs.append(("pcb", str(kwargs.get("initialdir", ""))))
            return (str(pcb_step),)

        monkeypatch.setattr("simple_3d_step.gui.pcb_panel.filedialog.askopenfilenames", _fake_pcb_open)
        app._pcb_panel.add_files_dialog()
        assert load_config(app._config_path).last_open_dir == str(dir_pcb.resolve())

        # 2. Next open in Batch Mode starts in dir_pcb, and selecting batch_step updates config to dir_batch
        def _fake_batch_open(**kwargs):
            seen_init_dirs.append(("batch", str(kwargs.get("initialdir", ""))))
            return (str(batch_step),)

        monkeypatch.setattr("simple_3d_step.gui.batch_panel.filedialog.askopenfilenames", _fake_batch_open)
        app._batch_panel.add_files_dialog()
        assert Path(seen_init_dirs[-1][1]).resolve() == dir_pcb.resolve()
        assert load_config(app._config_path).last_open_dir == str(dir_batch.resolve())

        # 3. Next open in Single Mode starts in dir_batch, and selecting single_step updates config to dir_single
        loaded_paths: list[Path] = []
        monkeypatch.setattr(app, "_submit_worker_task", lambda *_a, **_kw: None)

        def _fake_single_open(**kwargs):
            seen_init_dirs.append(("single", str(kwargs.get("initialdir", ""))))
            return str(single_step)

        monkeypatch.setattr("simple_3d_step.gui.app.filedialog.askopenfilename", _fake_single_open)
        app._on_open_step()
        assert Path(seen_init_dirs[-1][1]).resolve() == dir_batch.resolve()
        assert load_config(app._config_path).last_open_dir == str(dir_single.resolve())

        # 4. Next open back in PCB Mode starts in dir_single
        monkeypatch.setattr(
            "simple_3d_step.gui.pcb_panel.filedialog.askopenfilenames",
            lambda **kw: (seen_init_dirs.append(("pcb2", str(kw.get("initialdir", "")))), ())[1],
        )
        app._pcb_panel.add_files_dialog()
        assert Path(seen_init_dirs[-1][1]).resolve() == dir_single.resolve()
    finally:
        app._on_close()


def test_open_and_save_dirs_survive_app_close_and_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the app preserves the latest open & save directories across all tabs
    and guides both open and save dialogs to those directories on the next launch."""
    from simple_3d_step.config import load_config
    from simple_3d_step.models import AssemblyModel, SimplifyResult, ValidationReport

    open_dir = tmp_path / "recent_open"
    save_dir = tmp_path / "recent_save"
    other_src_dir = tmp_path / "another_src"
    for d in (open_dir, save_dir, other_src_dir):
        d.mkdir()
    pcb_step = open_dir / "board.step"
    pcb_step.write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")
    out_step = save_dir / "board_simplified.step"
    other_step = other_src_dir / "next_part.step"
    other_step.write_text("ISO-10303-21;END-ISO-10303-21;", encoding="utf-8")

    # --- Session 1: open in PCB Mode, save in Single Mode, then close app ---
    app1 = Simple3DStepApp()
    cfg_path = app1._config_path
    try:
        app1.update()
        # Open in PCB Mode (updates config.json via PcbPanel, NOT app1._config directly!)
        monkeypatch.setattr(
            "simple_3d_step.gui.pcb_panel.filedialog.askopenfilenames",
            lambda **_kw: (str(pcb_step),),
        )
        app1._pcb_panel.add_files_dialog()

        # Save in Single Mode into save_dir
        app1._assembly = AssemblyModel(source_path=pcb_step, parts=[])
        app1._result = SimplifyResult(
            shape=None,
            mode_used="exact",
            validation=ValidationReport(
                is_valid=True,
                solid_count=1,
                shell_count=1,
                face_count=6,
                free_edge_count=0,
                is_watertight=True,
                volume=10.0,
                surface_area=30.0,
                bbox_size=(2.0, 2.0, 2.5),
            ),
            kept_part_count=1,
            removed_part_count=0,
            original_volume=10.0,
            filled_volume_delta=0.0,
            elapsed_seconds=0.1,
        )
        monkeypatch.setattr(app1, "_submit_worker_task", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            "simple_3d_step.gui.app.filedialog.asksaveasfilename",
            lambda **_kw: str(out_step),
        )
        app1._on_save_result()
    finally:
        # Closing must NOT clobber open_dir (written by PcbPanel) or save_dir!
        app1._on_close()

    persisted = load_config(cfg_path)
    assert Path(persisted.last_open_dir).resolve() == open_dir.resolve()
    assert Path(persisted.last_save_dir).resolve() == save_dir.resolve()

    # --- Session 2: relaunch app -> open & save dialogs start in remembered directories ---
    app2 = Simple3DStepApp()
    try:
        app2.update()
        captured: dict[str, str] = {}

        monkeypatch.setattr(
            "simple_3d_step.gui.app.filedialog.askopenfilename",
            lambda **kw: (captured.__setitem__("open_single", str(kw.get("initialdir", ""))), "")[1],
        )
        app2._on_open_step()

        monkeypatch.setattr(
            "simple_3d_step.gui.pcb_panel.filedialog.askopenfilenames",
            lambda **kw: (captured.__setitem__("open_pcb", str(kw.get("initialdir", ""))), ())[1],
        )
        app2._pcb_panel.add_files_dialog()

        app2._assembly = AssemblyModel(source_path=other_step, parts=[])
        app2._result = SimplifyResult(
            shape=None,
            mode_used="exact",
            validation=ValidationReport(
                is_valid=True,
                solid_count=1,
                shell_count=1,
                face_count=6,
                free_edge_count=0,
                is_watertight=True,
                volume=10.0,
                surface_area=30.0,
                bbox_size=(2.0, 2.0, 2.5),
            ),
            kept_part_count=1,
            removed_part_count=0,
            original_volume=10.0,
            filled_volume_delta=0.0,
            elapsed_seconds=0.1,
        )
        monkeypatch.setattr(
            "simple_3d_step.gui.app.filedialog.asksaveasfilename",
            lambda **kw: (captured.__setitem__("save_single", str(kw.get("initialdir", ""))), "")[1],
        )
        app2._on_save_result()

        assert Path(captured["open_single"]).resolve() == open_dir.resolve()
        assert Path(captured["open_pcb"]).resolve() == open_dir.resolve()
        assert Path(captured["save_single"]).resolve() == save_dir.resolve()
    finally:
        app2._on_close()
