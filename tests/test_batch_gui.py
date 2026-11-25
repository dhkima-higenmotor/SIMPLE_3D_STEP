"""Tests for the unified Single/Batch mode GUI and the sequential batch pipeline."""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

from simple_3d_step.config import AppConfig, save_config
from simple_3d_step.gui.app import Simple3DStepApp
from simple_3d_step.gui.batch_panel import (
    ST_CANCELLED,
    ST_DONE,
    ST_PENDING,
    BatchPanel,
    build_simplify_config,
    build_visibility_config,
    default_output_path,
)
from simple_3d_step.gui.theme import apply_modern_flat_theme
from simple_3d_step.main import main
from tests.generate_sample_step import generate_closed_enclosure_step


@pytest.fixture(autouse=True)
def _isolate_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep GUI tests away from the user's real config.json."""
    cfg_path = tmp_path / "batch_test_config.json"
    monkeypatch.setattr(
        "simple_3d_step.gui.app.get_default_config_path",
        lambda: cfg_path,
    )
    return cfg_path


@pytest.fixture
def panel(tmp_path: Path):
    """Standalone batch panel on a throwaway Tk root (no single-mode CAD worker)."""
    root = tk.Tk()
    root.geometry("1100x700")
    apply_modern_flat_theme(root)
    batch_panel = BatchPanel(root, config_path=tmp_path / "panel_config.json")
    batch_panel.pack(fill=tk.BOTH, expand=True)
    root.update()
    try:
        yield batch_panel
    finally:
        batch_panel.shutdown()
        root.destroy()


def test_default_output_path_is_next_to_source(tmp_path: Path) -> None:
    src = tmp_path / "nested dir" / "part_A.stp"
    out = default_output_path(src)
    assert out == tmp_path / "nested dir" / "part_A_simplified.step"
    assert out.parent == src.parent


def test_config_json_maps_to_pipeline_parameters() -> None:
    cfg = AppConfig(
        num_views=162,
        grid_resolution=200,
        min_visible_ratio_pct=0.75,
        enable_pass2_verify=False,
        fill_internal_core=False,
        core_bridge_gap_mm=5.5,
        core_inset_mm=3.0,
        connect_floating_parts=False,
        max_island_gap_mm=4.5,
        remove_small_holes=True,
        max_hole_diameter=8.0,
        unify_same_domain=False,
        keep_largest_only=False,
        step_schema="AP242",
    )

    vis = build_visibility_config(cfg)
    assert vis.num_views == 162
    assert vis.grid_resolution == 200
    assert vis.min_visible_ratio == pytest.approx(0.0075)
    assert vis.enable_pass2_verify is False

    simp = build_simplify_config(cfg)
    assert simp.fill_internal_core is False
    assert simp.core_bridge_gap_mm == pytest.approx(5.5)
    assert simp.core_inset_mm == pytest.approx(3.0)
    assert simp.connect_floating_parts is False
    assert simp.max_island_gap_mm == pytest.approx(4.5)
    assert simp.remove_small_holes is True
    assert simp.max_hole_diameter == pytest.approx(8.0)
    assert simp.unify_same_domain is False
    assert simp.keep_largest_only is False
    assert simp.step_schema == "AP242"


def test_unified_app_has_single_and_batch_tabs() -> None:
    app = Simple3DStepApp()
    try:
        app.update()
        assert len(app._notebook.tabs()) == 3
        assert "Single Mode" in app._notebook.tab(0, "text")
        assert "Batch Mode" in app._notebook.tab(1, "text")
        assert "PCB Mode" in app._notebook.tab(2, "text")
        assert app._current_mode() == "single"
        # Batch & PCB CAD workers start lazily, only once their tabs are opened
        assert app._batch_panel._worker is None
        assert app._pcb_panel._worker is None

        app._select_mode("batch")
        app.update()
        assert app._current_mode() == "batch"
        assert app._batch_panel._worker is not None

        app._select_mode("single")
        app.update()
        assert app._current_mode() == "single"
    finally:
        app._on_close()


def test_batch_queue_add_remove_dedupe_and_run_state(panel: BatchPanel, tmp_path: Path) -> None:
    step_a = tmp_path / "a.step"
    step_b = tmp_path / "b.stp"
    notes = tmp_path / "notes.txt"
    for p in (step_a, step_b, notes):
        p.write_text("dummy", encoding="utf-8")

    assert str(panel._btn_run.cget("state")) == "disabled"

    panel.queue_files([step_a, step_b, step_a, notes])
    assert [item.path for item in panel._items] == [step_a.resolve(), step_b.resolve()]
    assert str(panel._btn_run.cget("state")) == "normal"

    panel._tree.selection_set(("0",))
    panel._on_remove_selected()
    assert [item.path for item in panel._items] == [step_b.resolve()]

    panel._on_clear_list()
    assert panel._items == []
    assert str(panel._btn_run.cget("state")) == "disabled"


def test_run_processes_selected_rows_then_whole_queue(
    panel: BatchPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step_a = tmp_path / "a.step"
    step_b = tmp_path / "b.step"
    for p in (step_a, step_b):
        p.write_text("dummy", encoding="utf-8")

    panel.queue_files([step_a, step_b])
    advanced: list[str] = []
    monkeypatch.setattr(panel, "_advance", lambda: advanced.append("advance"))

    # Only row 1 selected -> a single-file batch
    panel._tree.selection_set(("1",))
    panel.run()
    assert advanced == ["advance"]
    assert panel.total == 1
    assert panel._queue == [1]

    # No selection -> the whole queue runs in list order
    panel._running = False
    panel._tree.selection_remove(*panel._tree.selection())
    panel.run()
    assert panel.total == 2
    assert panel._queue == [0, 1]
    assert all(item.status == ST_PENDING for item in panel._items)


def test_double_click_opens_existing_output_in_configured_viewer(
    panel: BatchPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step_a = tmp_path / "a.step"
    step_b = tmp_path / "b.step"
    for p in (step_a, step_b):
        p.write_text("dummy", encoding="utf-8")
    panel.queue_files([step_a, step_b])

    saved = tmp_path / "a_simplified.step"
    saved.write_text("ISO-10303-21;", encoding="utf-8")
    panel._items[0].status = ST_DONE
    panel._items[0].output_path = saved
    panel._update_row(0)
    panel.update()

    calls: list[tuple[Path, object]] = []

    def _fake_launch(path: Path, config: object) -> str:
        calls.append((path, config))
        return "Launched external viewer: test.exe (a_simplified.step)"

    monkeypatch.setattr("simple_3d_step.gui.batch_panel.launch_external_viewer", _fake_launch)

    bbox = panel._tree.bbox("0")
    assert bbox, "row 0 is not visible"
    panel._on_tree_double_click(SimpleNamespace(y=bbox[1] + bbox[3] // 2))

    assert len(calls) == 1
    assert calls[0][0] == saved
    assert calls[0][1] is panel._config
    assert "Launched external viewer" in panel._log_panel._text.get("1.0", "end")


def test_double_click_without_output_reports_missing_file(
    panel: BatchPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.step"
    src.write_text("dummy", encoding="utf-8")
    panel.queue_files([src])

    called = False

    def _fake_launch(path: Path, config: object) -> str:
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr("simple_3d_step.gui.batch_panel.launch_external_viewer", _fake_launch)

    # No output recorded yet
    panel.open_output_file(0)
    assert called is False
    assert "No simplified STEP file is available yet" in panel._log_panel._text.get("1.0", "end")

    # Output recorded but the file has been deleted
    panel._items[0].output_path = tmp_path / "a_simplified.step"
    panel.open_output_file(0)
    assert called is False


def test_double_click_viewer_error_is_reported_without_crashing(
    panel: BatchPanel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "a.step"
    src.write_text("dummy", encoding="utf-8")
    output = tmp_path / "a_simplified.step"
    output.write_text("ISO-10303-21;", encoding="utf-8")
    panel.queue_files([src])
    panel._items[0].output_path = output

    def _failing_launch(path: Path, config: object) -> str:
        raise FileNotFoundError("Configured external 3D viewer executable not found")

    shown: list[str] = []
    monkeypatch.setattr("simple_3d_step.gui.batch_panel.launch_external_viewer", _failing_launch)
    monkeypatch.setattr(
        "simple_3d_step.gui.batch_panel.messagebox.showerror",
        lambda title, message, **kwargs: shown.append(message),
    )

    panel.open_output_file(0)

    assert shown and "not found" in shown[0]
    assert "Could not open a_simplified.step" in panel._log_panel._text.get("1.0", "end")


def test_stop_request_aborts_at_stage_boundary(panel: BatchPanel, tmp_path: Path) -> None:
    """A stop clicked while a stage finishes must not start the next stage."""
    src = tmp_path / "a.step"
    src.write_text("dummy", encoding="utf-8")
    panel.queue_files([src])

    # Emulate a batch in flight without touching the background worker
    panel._running = True
    panel._total = 1
    panel._queue = []
    panel._current_idx = 0
    panel._items[0].status = "Running"
    panel._stop_requested = True

    panel._handle_stage_done("load_step", {"parts": [], "total_volume": 0.0, "unit": "MM"})

    assert panel.running is False
    assert panel._stop_requested is False
    assert panel._items[0].status == ST_CANCELLED


def test_batch_mode_simplifies_files_sequentially_into_source_dirs(
    _isolate_config_path: Path, tmp_path: Path
) -> None:
    save_config(AppConfig(num_views=26, grid_resolution=80), _isolate_config_path)

    dir_a = tmp_path / "assembly_a"
    dir_b = tmp_path / "assembly_b"
    src_a = generate_closed_enclosure_step(dir_a / "a.step")
    src_b = generate_closed_enclosure_step(dir_b / "b.step")

    app = Simple3DStepApp(initial_files=[src_a, src_b], start_mode="batch")
    try:
        app.update()
        assert app._current_mode() == "batch"
        assert [item.path for item in app._batch_panel._items] == [src_a.resolve(), src_b.resolve()]

        app._batch_panel._tree.selection_set(("0", "1"))
        app._batch_panel.run()
        assert app._batch_panel.running is True

        deadline = time.perf_counter() + 240.0
        while app._batch_panel.running and time.perf_counter() < deadline:
            app.update()
            time.sleep(0.02)
        assert app._batch_panel.running is False, "Batch run did not finish in time"

        out_a = dir_a / "a_simplified.step"
        out_b = dir_b / "b_simplified.step"
        assert out_a.exists() and out_a.stat().st_size > 1000
        assert out_b.exists() and out_b.stat().st_size > 1000

        assert [item.status for item in app._batch_panel._items] == [ST_DONE, ST_DONE]
        assert app._batch_panel.completed == 2

        # Sequential processing order is recorded in the batch log console
        log_text = app._batch_panel._log_panel._text.get("1.0", "end")
        assert "[1/2] a.step" in log_text
        assert "[2/2] b.step" in log_text
        assert log_text.index("[1/2] a.step") < log_text.index("[2/2] b.step")

        # Tab label reflects progress state again after completion
        assert "Batch Mode" in app._notebook.tab(1, "text")
    finally:
        app._on_close()


def test_main_opens_batch_tab_for_multiple_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src_a = tmp_path / "one.step"
    src_b = tmp_path / "two.stp"
    for p in (src_a, src_b):
        p.write_text("dummy", encoding="utf-8")

    created: list[dict[str, object]] = []

    class _FakeApp:
        def __init__(self, initial_step=None, initial_files=None, start_mode="single") -> None:
            created.append(
                {
                    "initial_step": initial_step,
                    "initial_files": list(initial_files or []),
                    "start_mode": start_mode,
                }
            )

        def mainloop(self) -> None:
            created[-1]["mainloop"] = True

    monkeypatch.setattr("simple_3d_step.gui.app.Simple3DStepApp", _FakeApp)

    assert main([str(src_a)]) == 0
    assert created[-1]["start_mode"] == "single"
    assert created[-1]["initial_step"] == src_a.resolve()
    assert created[-1]["initial_files"] == [src_a.resolve()]

    assert main([str(src_a), str(src_b)]) == 0
    assert created[-1]["start_mode"] == "batch"
    assert created[-1]["initial_step"] is None
    assert created[-1]["initial_files"] == [src_a.resolve(), src_b.resolve()]

    assert main(["--batch", str(src_a)]) == 0
    assert created[-1]["start_mode"] == "batch"
