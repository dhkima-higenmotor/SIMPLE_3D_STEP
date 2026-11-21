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

    print(f"입력 STEP: {in_path}")
    asm = load_step_assembly(in_path, progress_cb=_log_prog)
    print(f"로드 완료: 부품 {len(asm.parts)}개")

    vis_cfg = VisibilityConfig(
        num_views=args.views,
        grid_resolution=args.grid,
        min_visible_ratio=args.min_visible / 100.0,
    )
    vis = analyze_visibility(asm, vis_cfg, progress_cb=_log_prog)
    print(f"가시성 분석: 외부 유지 {vis.external_count}개 / 내부 삭제 {vis.internal_count}개")

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
    print(f"\n=== 완료 ({res.mode_used}, {res.elapsed_seconds:.2f}s) ===")
    print(f"출력 STEP: {saved} ({saved.stat().st_size / (1024 * 1024):.2f} MB)")
    print(
        f"Solid={v.solid_count}, Shell={v.shell_count}, Faces={v.face_count:,}, "
        f"Watertight={v.is_watertight}, Volume={v.volume:,.1f} mm³ (+{res.filled_volume_delta:,.1f})"
    )
    if v.face_types:
        print("곡면 구성:", v.face_types)
    for w in v.warnings:
        print(f"  [경고] {w}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="simple-3d-step",
        description="3D STEP 내부 부품 자동 삭제 및 외관 보존 정밀 B-Rep 단일 Solid 변환 도구",
    )
    parser.add_argument("step_file", nargs="?", help="GUI 시작 시 바로 열 STEP 파일 경로")
    parser.add_argument("--cli", action="store_true", help="GUI 없이 CLI 배치 모드로 실행")
    parser.add_argument("-i", "--input", help="CLI 입력 STEP 파일 경로")
    parser.add_argument("-o", "--output", help="CLI 출력 STEP 파일 경로")
    parser.add_argument("--views", type=int, default=64, help="가시성 분석 구면 시점 수")
    parser.add_argument("--grid", type=int, default=160, help="시점당 광선 격자 해상도")
    parser.add_argument("--min-visible", type=float, default=0.5, help="최소 가시성 임계값(%%)")
    parser.add_argument("--bridge-gap", type=float, default=3.5, help="내부 코어 브리지 간극(mm)")
    parser.add_argument("--core-inset", type=float, default=2.0, help="외관 보호 안전 여백(mm)")
    parser.add_argument("--island-gap", type=float, default=3.0, help="분리 부품 내부 연결 간격(mm)")
    parser.add_argument("--no-core", action="store_true", help="내부 코어 생성 비활성화")
    parser.add_argument("--no-island-bridge", action="store_true", help="분리 부품 내부 연결 비활성화")
    parser.add_argument("--remove-holes", action="store_true", help="외관 소구경 관통홀 막기 활성화")
    parser.add_argument("--max-hole-dia", type=float, default=10.0, help="소구경 홀 막기 기준(mm)")
    parser.add_argument("--no-unify", action="store_true", help="동일 곡면 통합 비활성화")
    parser.add_argument("--schema", choices=("AP214", "AP242", "AP203"), default="AP214")

    args = parser.parse_args(argv)

    if args.cli or args.input:
        if not args.input and args.step_file:
            args.input = args.step_file
        if not args.input:
            parser.error("CLI 모드에서는 --input 또는 STEP 파일 경로가 필요합니다.")
        return _run_cli(args)

    from simple_3d_step.gui.app import Simple3DStepApp

    initial = Path(args.step_file).resolve() if args.step_file else None
    app = Simple3DStepApp(initial_step=initial)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
