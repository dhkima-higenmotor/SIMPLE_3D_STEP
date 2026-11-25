"""Entry point for SIMPLE_3D_STEP (Tkinter GUI & optional CLI batch mode)."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
import sys
import time
from pathlib import Path
from typing import Optional

from simple_3d_step.config import (
    ensure_config_file,
    resolve_batch_workers,
    threads_per_batch_worker,
)
from simple_3d_step.models import SimplifyConfig, VisibilityConfig


def _cli_process_one_file(
    in_path_str: str,
    out_path_str: str,
    vis_cfg: VisibilityConfig,
    simp_cfg: SimplifyConfig,
    max_threads: Optional[int] = None,
    verbose: bool = False,
) -> dict:
    """Process a single STEP file end-to-end (picklable top-level worker for CLI batch)."""
    from simple_3d_step.core.occt_compat import configure_occt_parallelism
    from simple_3d_step.core.solidifier import simplify_assembly_to_single_solid
    from simple_3d_step.core.step_io import load_step_assembly, save_shape_to_step
    from simple_3d_step.core.visibility import analyze_visibility

    configure_occt_parallelism(max_threads=max_threads)
    t0 = time.perf_counter()

    in_path = Path(in_path_str).resolve()
    out_path = Path(out_path_str).resolve()

    def _log_prog(frac: float, msg: str) -> None:
        if verbose:
            print(f"[{frac * 100:5.1f}%] {msg}")

    if verbose:
        print(f"Input STEP: {in_path}")
    asm = load_step_assembly(in_path, progress_cb=_log_prog if verbose else None)
    if verbose:
        print(f"Loaded: {len(asm.parts)} parts")

    vis = analyze_visibility(
        asm,
        vis_cfg,
        progress_cb=_log_prog if verbose else None,
        max_workers=max_threads,
    )
    if verbose:
        print(
            f"Visibility analysis: keep {vis.external_count} external / drop {vis.internal_count} internal"
        )

    res = simplify_assembly_to_single_solid(
        asm,
        simp_cfg,
        progress_cb=_log_prog if verbose else None,
        tessellated=vis.tessellated,
        max_workers=max_threads,
    )
    saved = save_shape_to_step(res.shape, out_path, schema=simp_cfg.step_schema, unit=asm.unit)
    v = res.validation

    return {
        "input": str(in_path),
        "output": str(saved),
        "size_bytes": saved.stat().st_size,
        "mode_used": res.mode_used,
        "elapsed_seconds": time.perf_counter() - t0,
        "simplify_seconds": res.elapsed_seconds,
        "solid_count": v.solid_count,
        "shell_count": v.shell_count,
        "face_count": v.face_count,
        "is_watertight": v.is_watertight,
        "volume": v.volume,
        "filled_volume_delta": res.filled_volume_delta,
        "face_types": dict(v.face_types),
        "warnings": list(v.warnings),
    }


def _run_cli(args: argparse.Namespace) -> int:
    vis_cfg = VisibilityConfig(
        num_views=args.views,
        grid_resolution=args.grid,
        min_visible_ratio=args.min_visible / 100.0,
    )
    simp_cfg = SimplifyConfig(
        fill_internal_core=not args.no_core,
        core_bridge_gap_mm=args.bridge_gap,
        core_inset_mm=args.core_inset,
        connect_floating_parts=not args.no_island_bridge,
        max_island_gap_mm=args.island_gap,
        remove_small_holes=args.remove_holes,
        max_hole_diameter=args.max_hole_dia,
        unify_same_domain=not args.no_unify,
        step_schema=args.schema,
    )

    inputs: list[Path] = []
    if args.input:
        inputs.append(Path(args.input).resolve())
    for extra in args.step_file:
        p = Path(extra).resolve()
        if p not in inputs:
            inputs.append(p)

    # Single-file CLI path (preserves exact existing output format)
    if len(inputs) == 1:
        in_path = inputs[0]
        out_path = (
            Path(args.output).resolve()
            if args.output
            else in_path.with_name(f"{in_path.stem}_simplified.step")
        )
        info = _cli_process_one_file(
            str(in_path),
            str(out_path),
            vis_cfg,
            simp_cfg,
            max_threads=None,
            verbose=True,
        )
        print(f"\n=== Completed ({info['mode_used']}, {info['simplify_seconds']:.2f}s) ===")
        print(f"Output STEP: {info['output']} ({info['size_bytes'] / (1024 * 1024):.2f} MB)")
        print(
            f"Solid={info['solid_count']}, Shell={info['shell_count']}, Faces={info['face_count']:,}, "
            f"Watertight={info['is_watertight']}, Volume={info['volume']:,.1f} mm³ (+{info['filled_volume_delta']:,.1f})"
        )
        if info["face_types"]:
            print("Surface types:", info["face_types"])
        for w in info["warnings"]:
            print(f"  [Warning] {w}")
        return 0

    # Multi-file parallel CLI batch mode
    workers = resolve_batch_workers(args.jobs, len(inputs))
    per_worker_threads = threads_per_batch_worker(workers)
    print(
        f"CLI Batch: {len(inputs)} file(s) using {workers} parallel worker(s) "
        f"({per_worker_threads} threads/worker)"
    )
    t_all = time.perf_counter()
    ok_cnt = 0
    fail_cnt = 0

    if workers == 1:
        for idx, in_path in enumerate(inputs, start=1):
            out_path = in_path.with_name(f"{in_path.stem}_simplified.step")
            try:
                info = _cli_process_one_file(
                    str(in_path), str(out_path), vis_cfg, simp_cfg, max_threads=None, verbose=False
                )
                ok_cnt += 1
                print(
                    f"[{idx}/{len(inputs)}] OK {in_path.name} -> {Path(info['output']).name} "
                    f"({info['elapsed_seconds']:.2f}s, Solid={info['solid_count']}, Faces={info['face_count']:,})"
                )
            except Exception as exc:
                fail_cnt += 1
                print(f"[{idx}/{len(inputs)}] FAIL {in_path.name}: {exc}", file=sys.stderr)
    else:
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            fut_map = {}
            for in_path in inputs:
                out_path = in_path.with_name(f"{in_path.stem}_simplified.step")
                fut = ex.submit(
                    _cli_process_one_file,
                    str(in_path),
                    str(out_path),
                    vis_cfg,
                    simp_cfg,
                    per_worker_threads,
                    False,
                )
                fut_map[fut] = in_path

            done_i = 0
            for fut in as_completed(fut_map):
                done_i += 1
                in_path = fut_map[fut]
                try:
                    info = fut.result()
                    ok_cnt += 1
                    print(
                        f"[{done_i}/{len(inputs)}] OK {in_path.name} -> {Path(info['output']).name} "
                        f"({info['elapsed_seconds']:.2f}s, Solid={info['solid_count']}, Faces={info['face_count']:,})"
                    )
                except Exception as exc:
                    fail_cnt += 1
                    print(f"[{done_i}/{len(inputs)}] FAIL {in_path.name}: {exc}", file=sys.stderr)

    print(
        f"\n=== Batch Completed: {ok_cnt} succeeded, {fail_cnt} failed in {time.perf_counter() - t_all:.2f}s ==="
    )
    return 0 if fail_cnt == 0 else 1


def main(argv: Optional[list[str]] = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="simple-3d-step",
        description="Remove internal STEP assembly parts and convert exterior B-Rep into a single solid body",
    )
    parser.add_argument(
        "step_file",
        nargs="*",
        help="Optional STEP file(s): opened in the single-file GUI, or pre-queued in --batch mode",
    )
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI batch mode")
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Open the GUI on the Batch Mode tab (sequential multi-file simplify)",
    )
    parser.add_argument(
        "--pcb",
        action="store_true",
        help="Open the GUI on the PCB Mode tab (Altium PCB STEP + IDF simplifier)",
    )
    parser.add_argument("-i", "--input", help="Input STEP file path for CLI mode")
    parser.add_argument("-o", "--output", help="Output STEP file path for CLI mode")
    parser.add_argument("--views", type=int, default=64, help="Number of Fibonacci sphere viewpoints")
    parser.add_argument("--grid", type=int, default=160, help="Orthographic ray grid resolution per view")
    parser.add_argument("--min-visible", type=float, default=0.5, help="Minimum visibility threshold (%%)")
    parser.add_argument("--bridge-gap", type=float, default=3.5, help="Internal core gap bridging distance (mm)")
    parser.add_argument("--core-inset", type=float, default=2.0, help="Exterior protection safety inset (mm)")
    parser.add_argument("--island-gap", type=float, default=3.0, help="Max gap to bridge floating outer parts (mm)")
    parser.add_argument("--no-core", action="store_true", help="Disable analytic interior core generation")
    parser.add_argument("--no-island-bridge", action="store_true", help="Disable internal pin bridging for floating parts")
    parser.add_argument("--remove-holes", action="store_true", help="Enable small outer through-hole plugging")
    parser.add_argument("--max-hole-dia", type=float, default=10.0, help="Max diameter for small hole plugging (mm)")
    parser.add_argument("--no-unify", action="store_true", help="Disable UnifySameDomain face merging")
    parser.add_argument("--schema", choices=("AP214", "AP242", "AP203"), default="AP214")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="Parallel worker processes for multi-file CLI batch (0 = Auto)",
    )

    args = parser.parse_args(argv)

    if args.cli or args.input:
        if not args.input and args.step_file:
            args.input = args.step_file[0]
        if not args.input:
            parser.error("CLI mode requires --input or a STEP file path.")
        ensure_config_file()
        return _run_cli(args)

    step_files = [Path(p).resolve() for p in args.step_file]

    # --pcb opens PCB Mode; multiple files (or --batch) open Batch Mode; one file opens Single Mode
    if args.pcb:
        start_mode = "pcb"
    elif args.batch or len(step_files) > 1:
        start_mode = "batch"
    else:
        start_mode = "single"
    initial_step = step_files[0] if (step_files and start_mode == "single") else None

    import tkinter as tk
    from simple_3d_step.gui.app import Simple3DStepApp

    try:
        app = Simple3DStepApp(
            initial_step=initial_step,
            initial_files=step_files,
            start_mode=start_mode,
        )
    except tk.TclError as exc:
        print(
            f"[ERROR] Failed to initialize GUI display: {exc}\n"
            "If running on a headless Linux environment, use --cli mode instead, e.g.:\n"
            "  simple-3d-step --cli -i /path/to/assembly.step",
            file=sys.stderr,
        )
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
