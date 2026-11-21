"""Tests for config persistence, external viewer validation, and CLI pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from simple_3d_step.config import AppConfig, launch_external_viewer, load_config, save_config
from simple_3d_step.main import main
from tests.generate_sample_step import generate_closed_enclosure_step


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = AppConfig(
        viewer_executable=r"C:\Tools\viewer.exe",
        viewer_args="--view {file}",
        core_bridge_gap_mm=4.0,
        core_inset_mm=2.5,
    )
    save_config(cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.viewer_executable == r"C:\Tools\viewer.exe"
    assert loaded.viewer_args == "--view {file}"
    assert loaded.core_bridge_gap_mm == pytest.approx(4.0)
    assert loaded.core_inset_mm == pytest.approx(2.5)


def test_launch_viewer_missing_exe_raises(tmp_path: Path) -> None:
    dummy_step = tmp_path / "model.step"
    dummy_step.write_text("ISO-10303-21;", encoding="utf-8")
    cfg = AppConfig(viewer_executable=str(tmp_path / "nonexistent_viewer.exe"))
    with pytest.raises(FileNotFoundError):
        launch_external_viewer(dummy_step, cfg)


def test_cli_end_to_end(tmp_path: Path) -> None:
    src = generate_closed_enclosure_step(tmp_path / "cli_in.step")
    out = tmp_path / "cli_out.step"
    rc = main(["--cli", "-i", str(src), "-o", str(out), "--views", "26", "--grid", "80"])
    assert rc == 0
    assert out.exists()
    assert out.stat().st_size > 1000
