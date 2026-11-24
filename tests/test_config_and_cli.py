"""Tests for config persistence, external viewer validation, and CLI pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from simple_3d_step.config import (
    AppConfig,
    launch_external_viewer,
    load_config,
    resolve_viewer_executable,
    save_config,
)
from simple_3d_step.main import main
from tests.generate_sample_step import generate_closed_enclosure_step


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = AppConfig(
        viewer_executable=r"C:\Tools\viewer.exe",
        viewer_args="--view {file}",
        ui_font_family="Noto Sans",
        ui_font_size=11,
        core_bridge_gap_mm=4.0,
        core_inset_mm=2.5,
    )
    save_config(cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.viewer_executable == r"C:\Tools\viewer.exe"
    assert loaded.viewer_args == "--view {file}"
    assert loaded.ui_font_family == "Noto Sans"
    assert loaded.ui_font_size == 11
    assert loaded.core_bridge_gap_mm == pytest.approx(4.0)
    assert loaded.core_inset_mm == pytest.approx(2.5)


def test_launch_viewer_missing_exe_raises(tmp_path: Path) -> None:
    dummy_step = tmp_path / "model.step"
    dummy_step.write_text("ISO-10303-21;", encoding="utf-8")
    cfg = AppConfig(viewer_executable=str(tmp_path / "nonexistent_viewer.exe"))
    with pytest.raises(FileNotFoundError):
        launch_external_viewer(dummy_step, cfg)


def test_resolve_and_launch_viewer_cross_platform(tmp_path: Path) -> None:
    dummy_step = tmp_path / "model with spaces.step"
    dummy_step.write_text("ISO-10303-21;", encoding="utf-8")

    # Resolving an existing executable path works on all platforms
    assert resolve_viewer_executable(sys.executable) == Path(sys.executable)
    assert resolve_viewer_executable("") is None

    # Quoted args template should split properly without keeping literal quotes on POSIX
    cfg = AppConfig(
        viewer_executable=sys.executable,
        viewer_args='--flag "hello world" {file}',
    )
    with patch("simple_3d_step.config.subprocess.Popen") as mock_popen:
        msg = launch_external_viewer(dummy_step, cfg)
        assert "Launched external viewer:" in msg
        mock_popen.assert_called_once()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "--flag"
        assert cmd[3] == str(dummy_step.resolve())
        if os.name != "nt":
            assert cmd[2] == "hello world"
            assert mock_popen.call_args[1].get("start_new_session") is True

    # On Linux/POSIX, empty viewer_executable falls back to xdg-open
    if os.name != "nt" and sys.platform != "darwin":
        cfg_empty = AppConfig(viewer_executable="")
        with (
            patch("simple_3d_step.config.shutil.which", return_value="/usr/bin/xdg-open"),
            patch("simple_3d_step.config.subprocess.Popen") as mock_popen,
        ):
            msg = launch_external_viewer(dummy_step, cfg_empty)
            assert "xdg-open" in msg
            mock_popen.assert_called_once_with(
                ["/usr/bin/xdg-open", str(dummy_step.resolve())],
                start_new_session=True,
            )


@pytest.mark.skipif(os.name == "nt", reason="Shell launcher script is for Linux/POSIX")
def test_linux_shell_launcher_help_and_cli(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[1] / "run_simple_3d_step.sh"
    assert launcher.is_file()
    assert os.access(launcher, os.X_OK), "run_simple_3d_step.sh must be executable"

    # --help from an external working directory works cleanly
    res = subprocess.run(
        [str(launcher), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "simple-3d-step" in res.stdout

    # Relative path CLI invocation from another working directory preserves caller CWD
    src = generate_closed_enclosure_step(tmp_path / "rel_in.step")
    res_cli = subprocess.run(
        [str(launcher), "--cli", "-i", "rel_in.step", "-o", "rel_out.step", "--views", "26", "--grid", "80"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_cli.returncode == 0, res_cli.stderr
    out = tmp_path / "rel_out.step"
    assert out.is_file() and out.stat().st_size > 1000
    assert src.is_file()


def test_cli_end_to_end(tmp_path: Path) -> None:
    src = generate_closed_enclosure_step(tmp_path / "cli_in.step")
    out = tmp_path / "cli_out.step"
    rc = main(["--cli", "-i", str(src), "-o", str(out), "--views", "26", "--grid", "80"])
    assert rc == 0
    assert out.exists()
    assert out.stat().st_size > 1000
