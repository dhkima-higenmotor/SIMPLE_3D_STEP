"""Entry point for SIMPLE_3D_STEP (Tkinter GUI & optional CLI batch mode)."""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path
from typing import Optional

from simple_3d_step.models import SimplifyConfig, VisibilityConfig


def _run_cli(args: argparse.Namespace) -> int:
    from simple_3d_step.core.solidifier import simplify_assembly_to_single_solid
    from simple_3d_step.core.step_io import load_step_assembly, save_shape_to_step
    from simple_3d_step.core.visibility import analyze_visibility

    in_path = Path(args.input).resolve()
    out_path = (
        Path(args.output).resolve()
        if args.output
        else in_path.with_name(f"{in_path.stem}_simplified.step")
    )

    def _log_prog(frac: float, msg: str) -> None:
        print(f"[{frac * 100:5.1f}%] {msg}")

    print(f"Input STEP: {in_path}")
    asm = load_step_assembly(in_path, progress_cb=_log_prog)
    print(f"Loaded: {len(asm.parts)} parts")

    vis_cfg = VisibilityConfig(
        num_views=args.views,
        grid_resolution=args.grid,
        min_visible_ratio=args.min_visible / 100.0,
    )
    vis = analyze_visibility(asm, vis_cfg, progress_cb=_log_prog)
    print(f"Visibility analysis: keep {vis.external_count} external / drop {vis.internal_count} internal")

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
    res = simplify_assembly_to_single_solid(asm, simp_cfg, progress_cb=_log_prog)
    saved = save_shape_to_step(res.shape, out_path, schema=simp_cfg.step_schema, unit=asm.unit)

    v = res.validation
    print(f"\n=== Completed ({res.mode_used}, {res.elapsed_seconds:.2f}s) ===")
    print(f"Output STEP: {saved} ({saved.stat().st_size / (1024 * 1024):.2f} MB)")
    print(
        f"Solid={v.solid_count}, Shell={v.shell_count}, Faces={v.face_count:,}, "
        f"Watertight={v.is_watertight}, Volume={v.volume:,.1f} mm³ (+{res.filled_volume_delta:,.1f})"
    )
    if v.face_types:
        print("Surface types:", v.face_types)
    for w in v.warnings:
        print(f"  [Warning] {w}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="simple-3d-step",
        description="Remove internal STEP assembly parts and convert exterior B-Rep into a single solid body",
    )
    parser.add_argument("step_file", nargs="?", help="Optional STEP file to open automatically in GUI")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI batch mode")
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

    args = parser.parse_args(argv)

    if args.cli or args.input:
        if not args.input and args.step_file:
            args.input = args.step_file
        if not args.input:
            parser.error("CLI mode requires --input or a STEP file path.")
        return _run_cli(args)

    from simple_3d_step.gui.app import Simple3DStepApp

    initial = Path(args.step_file).resolve() if args.step_file else None
    app = Simple3DStepApp(initial_step=initial)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
