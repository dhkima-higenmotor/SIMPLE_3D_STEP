"""Configuration management and external 3D viewer integration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
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

    # GUI appearance (empty ui_font_family = Auto OS default)
    ui_font_family: str = ""
    ui_font_size: int = 9

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

    # Parallel batch worker count (0 = Auto based on CPU cores)
    batch_workers: int = 0

    # PCB Mode parameters (Altium STEP + IDF simplification)
    pcb_simplify_leads: bool = True
    pcb_detect_cylinders: bool = True
    pcb_detect_hex_posts: bool = True
    pcb_cylinder_circularity_min: float = 0.80
    pcb_remove_text_features: bool = True
    pcb_small_part_box_mm: float = 4.0
    pcb_preserve_simple_max_faces: int = 55
    pcb_preserve_colors: bool = True
    pcb_use_idf_supplement: bool = True
    pcb_keep_board_holes: bool = True
    pcb_min_board_hole_mm: float = 0.8
    pcb_exclude_small_part_mm: float = 0.0
    pcb_min_color_brightness: float = 0.12
    pcb_idf_default_color: list[float] = field(default_factory=lambda: [0.35, 0.35, 0.38])


def resolve_batch_workers(configured: int = 0, task_count: int = 0) -> int:
    """Resolve concurrent batch CAD worker process count (0 = Auto)."""
    cpu_cnt = max(1, os.cpu_count() or 1)
    if int(configured) > 0:
        workers = min(int(configured), cpu_cnt)
    else:
        # Auto: balance CPU utilization vs per-worker OCCT/Embree RAM footprint (~0.5-2 GB/worker)
        workers = min(6, max(1, cpu_cnt // 4))
    if task_count > 0:
        workers = min(workers, task_count)
    return max(1, workers)


def threads_per_batch_worker(num_workers: int) -> int:
    """Compute per-worker C++/Embree thread budget to prevent core oversubscription."""
    cpu_cnt = max(1, os.cpu_count() or 1)
    return max(1, cpu_cnt // max(1, int(num_workers)))


def get_os_config_template_filename(platform: Optional[str] = None) -> Optional[str]:
    """Return the OS-specific default config template filename (Windows or Linux)."""
    plat = (platform if platform is not None else sys.platform).lower()
    if plat.startswith("win") or (platform is None and os.name == "nt"):
        return f"{DEFAULT_CONFIG_FILENAME}.win"
    if plat.startswith("linux"):
        return f"{DEFAULT_CONFIG_FILENAME}.linux"
    return None


def get_default_config_path() -> Path:
    """Return the path to config.json in the current working directory or project root."""
    env_override = os.environ.get("SIMPLE_3D_STEP_CONFIG", "").strip()
    if env_override:
        return Path(env_override).expanduser()

    cwd_cfg = Path.cwd() / DEFAULT_CONFIG_FILENAME
    if cwd_cfg.exists():
        return cwd_cfg

    template_name = get_os_config_template_filename()
    if template_name and (Path.cwd() / template_name).is_file():
        return cwd_cfg

    # When launched from another directory (e.g. via run_simple_3d_step.sh), prefer the
    # repository/project root config.json if this is a source checkout.
    project_root = Path(__file__).resolve().parents[2]
    root_cfg = project_root / DEFAULT_CONFIG_FILENAME
    if (project_root / "pyproject.toml").is_file() and (
        root_cfg.is_file()
        or (template_name is not None and (project_root / template_name).is_file())
    ):
        return root_cfg

    return cwd_cfg


def ensure_config_file(
    config_path: Optional[Path] = None,
    *,
    platform: Optional[str] = None,
) -> Optional[Path]:
    """Copy the OS-specific template (.win / .linux) to config.json when missing.

    Returns the copied template path if bootstrapping occurred, otherwise None.
    """
    path = config_path or get_default_config_path()
    if path.exists():
        return None

    template_name = get_os_config_template_filename(platform=platform)
    if not template_name:
        return None

    suffix = template_name.removeprefix(DEFAULT_CONFIG_FILENAME)
    candidates = [path.with_name(f"{path.name}{suffix}")]
    if path.name != DEFAULT_CONFIG_FILENAME:
        candidates.append(path.parent / template_name)

    for template_path in candidates:
        if template_path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template_path, path)
            return template_path

    return None


def default_viewer_label() -> str:
    """Return a platform-appropriate label for the OS default STEP viewer."""
    if os.name == "nt":
        return "(Windows Default Application)"
    if sys.platform == "darwin":
        return "(macOS Default Application)"
    return "(System Default Application / xdg-open)"


def resolve_viewer_executable(exe: str) -> Optional[Path]:
    """Resolve a configured viewer path, supporting ~ expansion and commands on PATH."""
    cleaned = exe.strip()
    if not cleaned:
        return None

    candidate = Path(cleaned).expanduser()
    if candidate.is_file():
        return candidate

    found = shutil.which(cleaned)
    if found:
        return Path(found)

    return None


def load_config(
    config_path: Optional[Path] = None,
    *,
    platform: Optional[str] = None,
) -> AppConfig:
    """Load AppConfig from JSON file, copying OS template or creating defaults if missing."""
    path = config_path or get_default_config_path()
    if not path.exists():
        copied_from = ensure_config_file(path, platform=platform)
        if copied_from is None:
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


def resolve_last_open_dir(
    config_path: Optional[Path] = None,
    fallback: Optional[str] = None,
) -> str:
    """Return the most recently opened STEP directory persisted in config.json (or cwd)."""
    candidates: list[str] = []
    path = config_path or get_default_config_path()
    if path.is_file():
        try:
            fresh = load_config(path)
            if fresh.last_open_dir:
                candidates.append(fresh.last_open_dir)
        except Exception:
            pass
    if fallback:
        candidates.append(fallback)

    for raw in candidates:
        try:
            p = Path(raw).expanduser()
            if p.is_dir():
                return str(p)
        except OSError:
            continue
    return str(Path.cwd())


def remember_last_open_dir(
    step_path: str | Path,
    config_path: Optional[Path] = None,
) -> str:
    """Persist the directory of the most recently opened STEP file into config.json."""
    target = Path(step_path).expanduser().resolve()
    folder = target if target.is_dir() else target.parent
    folder_str = str(folder)
    path = config_path or get_default_config_path()
    cfg = load_config(path)
    if cfg.last_open_dir != folder_str:
        cfg.last_open_dir = folder_str
        save_config(cfg, path)
    return folder_str


def resolve_last_save_dir(
    config_path: Optional[Path] = None,
    fallback: Optional[str] = None,
) -> str:
    """Return the most recently used save directory persisted in config.json (or fallback/cwd)."""
    candidates: list[str] = []
    path = config_path or get_default_config_path()
    if path.is_file():
        try:
            fresh = load_config(path)
            if fresh.last_save_dir:
                candidates.append(fresh.last_save_dir)
        except Exception:
            pass
    if fallback:
        candidates.append(fallback)

    for raw in candidates:
        try:
            p = Path(raw).expanduser()
            if p.is_dir():
                return str(p)
        except OSError:
            continue
    return str(Path.cwd())


def remember_last_save_dir(
    output_path: str | Path,
    config_path: Optional[Path] = None,
) -> str:
    """Persist the directory of the most recently saved STEP file into config.json."""
    target = Path(output_path).expanduser().resolve()
    folder = target if target.is_dir() else target.parent
    folder_str = str(folder)
    path = config_path or get_default_config_path()
    cfg = load_config(path)
    if cfg.last_save_dir != folder_str:
        cfg.last_save_dir = folder_str
        save_config(cfg, path)
    return folder_str


def launch_external_viewer(step_file: Path, config: AppConfig) -> str:
    """Launch the configured external 3D viewer with the given STEP file."""
    target = Path(step_file).resolve()
    if not target.exists():
        raise FileNotFoundError(f"STEP file not found: {target}")

    exe = config.viewer_executable.strip()
    if exe:
        exe_path = resolve_viewer_executable(exe)
        if exe_path is None:
            raise FileNotFoundError(
                f"Configured external 3D viewer executable not found:\n{exe}\n"
                "Please verify the path in Viewer Settings (or clear it to use the system default application)."
            )

        args_template = (config.viewer_args or "{file}").strip()
        posix_split = os.name != "nt"
        if "{file}" in args_template:
            placeholder = "__STEP_FILE_PLACEHOLDER__"
            parsed = shlex.split(args_template.replace("{file}", placeholder), posix=posix_split)
            cmd_args = [
                str(target) if tok == placeholder else tok.replace(placeholder, str(target))
                for tok in parsed
            ]
        else:
            parsed = shlex.split(args_template, posix=posix_split) if args_template else []
            cmd_args = parsed + [str(target)]

        cmd = [str(exe_path)] + cmd_args
        popen_kwargs: dict[str, object] = {}
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **popen_kwargs)  # type: ignore[arg-type]
        return f"Launched external viewer: {exe_path.name} ({target.name})"

    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return f"Opened with Windows default application: {target.name}"

    if sys.platform == "darwin":
        opener = shutil.which("open")
        if opener:
            subprocess.Popen([opener, str(target)], start_new_session=True)
            return f"Opened with macOS default application: {target.name}"
    else:
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(target)], start_new_session=True)
            return f"Opened with system default application (xdg-open): {target.name}"

    raise RuntimeError(
        "No external 3D viewer executable configured and no system opener (xdg-open/open) found. "
        "Set viewer_executable in config.json or Viewer Settings."
    )
