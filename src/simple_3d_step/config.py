"""Configuration management and external 3D viewer integration."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG_FILENAME = "config.json"


@dataclass
class AppConfig:
    """Persistent application configuration saved in config.json."""

    viewer_executable: str = ""
    viewer_args: str = "{file}"
    last_open_dir: str = ""
    last_save_dir: str = ""

    # Default visibility analysis parameters
    num_views: int = 64
    grid_resolution: int = 160
    min_visible_ratio_pct: float = 0.5
    enable_pass2_verify: bool = True

    # Default exact B-Rep solidification parameters
    fill_internal_core: bool = True
    core_bridge_gap_mm: float = 3.5
    core_inset_mm: float = 2.0
    connect_floating_parts: bool = True
    max_island_gap_mm: float = 3.0
    remove_small_holes: bool = False
    max_hole_diameter: float = 10.0
    unify_same_domain: bool = True
    keep_largest_only: bool = True
    step_schema: str = "AP214"


def get_default_config_path() -> Path:
    """Return the path to config.json in the project root / current working directory."""
    return Path.cwd() / DEFAULT_CONFIG_FILENAME


def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """Load AppConfig from JSON file, creating defaults if missing."""
    path = config_path or get_default_config_path()
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg, path)
        return cfg

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        valid_fields = {f for f in AppConfig.__dataclass_fields__}
        filtered = {k: v for k, v in raw.items() if k in valid_fields}
        return AppConfig(**filtered)
    except Exception:
        return AppConfig()


def save_config(config: AppConfig, config_path: Optional[Path] = None) -> Path:
    """Persist AppConfig to JSON file."""
    path = config_path or get_default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def launch_external_viewer(step_file: Path, config: AppConfig) -> str:
    """Launch the configured external 3D viewer with the given STEP file."""
    target = Path(step_file).resolve()
    if not target.exists():
        raise FileNotFoundError(f"STEP 파일을 찾을 수 없습니다: {target}")

    exe = config.viewer_executable.strip()
    if exe:
        exe_path = Path(exe)
        if not exe_path.exists():
            raise FileNotFoundError(
                f"설정된 외부 3D 뷰어 실행 파일을 찾을 수 없습니다:\n{exe_path}\n"
                "설정 메뉴에서 실행 파일 경로를 확인해 주세요."
            )

        args_template = (config.viewer_args or "{file}").strip()
        if "{file}" in args_template:
            placeholder = "__STEP_FILE_PLACEHOLDER__"
            parsed = shlex.split(args_template.replace("{file}", placeholder), posix=False)
            cmd_args = [
                str(target) if tok == placeholder else tok.replace(placeholder, str(target))
                for tok in parsed
            ]
        else:
            parsed = shlex.split(args_template, posix=False) if args_template else []
            cmd_args = parsed + [str(target)]

        cmd = [str(exe_path)] + cmd_args
        subprocess.Popen(cmd)
        return f"외부 뷰어 실행: {exe_path.name} ({target.name})"

    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return f"시스템 기본 연결 프로그램으로 열기: {target.name}"

    raise RuntimeError(
        "외부 3D 뷰어 실행 파일이 설정되지 않았습니다. config.json 또는 설정 메뉴에서 지정해 주세요."
    )
