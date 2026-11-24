#!/usr/bin/env bash
set -euo pipefail

# Resolve the real directory containing this script (even when invoked via symlink)
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
    DIR="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
    SOURCE="$(readlink -- "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"

VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
APP_BIN="$VENV_DIR/bin/simple-3d-step"

_exec_app() {
    if [[ -x "$APP_BIN" ]]; then
        exec "$APP_BIN" "$@"
    fi
    exec "$PYTHON_BIN" -m simple_3d_step.main "$@"
}

_find_xft_python() {
    if [[ -n "${SIMPLE_3D_STEP_PYTHON:-}" && -x "${SIMPLE_3D_STEP_PYTHON}" ]]; then
        echo "$SIMPLE_3D_STEP_PYTHON"
        return 0
    fi
    local candidate
    for candidate in /usr/bin/python3 /usr/bin/python3.14 /usr/bin/python3.13 /usr/local/bin/python3; do
        [[ -x "$candidate" ]] || continue
        if "$candidate" -c '
import subprocess, sys
if sys.version_info < (3, 13):
    raise SystemExit(1)
import _tkinter
out = subprocess.check_output(["ldd", _tkinter.__file__], text=True, stderr=subprocess.DEVNULL)
for line in out.splitlines():
    if "libtk" in line and "=>" in line:
        libtk = line.split("=>", 1)[1].strip().split()[0]
        tk_ldd = subprocess.check_output(["ldd", libtk], text=True, stderr=subprocess.DEVNULL)
        if "libXft" in tk_ldd:
            raise SystemExit(0)
raise SystemExit(1)
' >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# Ensure virtual environment exists
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[SIMPLE_3D_STEP] Setting up Python environment via uv..." >&2
    UV_BIN="$(command -v uv || true)"
    if [[ -z "$UV_BIN" ]]; then
        for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
            if [[ -x "$candidate" ]]; then
                UV_BIN="$candidate"
                break
            fi
        done
    fi
    if [[ -z "$UV_BIN" ]]; then
        echo "[ERROR] 'uv' is not installed or not on PATH." >&2
        echo "        Install uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
        exit 1
    fi
    # Prefer a system Python >= 3.13 whose Tk links libXft for crisp anti-aliased GUI fonts
    XFT_PYTHON="$(_find_xft_python || true)"
    if [[ -n "$XFT_PYTHON" ]]; then
        echo "[SIMPLE_3D_STEP] Using Xft-enabled Python for anti-aliased GUI fonts: $XFT_PYTHON" >&2
        if ! (cd "$SCRIPT_DIR" && "$UV_BIN" sync --python "$XFT_PYTHON"); then
            echo "[WARN] Sync with $XFT_PYTHON failed; falling back to default uv sync..." >&2
            if ! (cd "$SCRIPT_DIR" && "$UV_BIN" sync); then
                echo "[ERROR] Failed to sync Python environment via uv." >&2
                exit 1
            fi
        fi
    else
        if ! (cd "$SCRIPT_DIR" && "$UV_BIN" sync); then
            echo "[ERROR] Failed to sync Python environment via uv." >&2
            exit 1
        fi
    fi
fi

# Detect explicit CLI flags safely (immune to filenames containing -i, etc.)
RUN_CLI=0
for arg in "$@"; do
    case "${arg,,}" in
        --cli|-i|--input|--input=*|-h|--help)
            RUN_CLI=1
            break
            ;;
    esac
done

if [[ "$RUN_CLI" -eq 1 ]]; then
    _exec_app "$@"
fi

# Verify GUI prerequisites on Linux before launching Tkinter
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    echo "[ERROR] No graphical display detected (DISPLAY and WAYLAND_DISPLAY are unset)." >&2
    echo "        Run in headless CLI mode instead, e.g.:" >&2
    echo "        $0 --cli -i /path/to/assembly.step" >&2
    exit 1
fi

if ! "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
    echo "[ERROR] Python 'tkinter' module is not available in $PYTHON_BIN." >&2
    echo "        Install Tk support for your Linux distribution (e.g. python3-tk / tk) or use --cli mode." >&2
    exit 1
fi

# Launch the unified Tkinter GUI (Single Mode / Batch Mode tabs)
# - No args: empty GUI (Single Mode)
# - One .step/.stp file: opens directly in Single Mode
# - Multiple files or --batch: opens Batch Mode tab with files queued
_exec_app "$@"
