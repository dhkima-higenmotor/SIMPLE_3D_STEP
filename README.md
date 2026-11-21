# SIMPLE 3D STEP

내부 부품(모터 로터·스테이터, 기어, 베어링, PCB, 내부 체결부품 등)까지 모두 포함된 3D STEP 어셈블리 파일에서 **외부에서 보이지 않는 내부 부품을 자동 식별·삭제**하고, **외관 B-Rep 형상(평면·원통·원추·토러스·필렛·방열핀·홀 등)을 원본 그대로 유지한 채 내부 빈 공간만 솔리드로 채워 단일 Solid Body STEP 파일**로 변환하는 데스크톱/CLI 도구입니다.

---

## 주요 특징

### 1. XCAF 기반 STEP 어셈블리 파싱 (`STEPCAFControl_Reader`)
- 어셈블리 계층 구조, 부품 이름, 인스턴스 위치 변환(`TopLoc_Location`), 부피·표면적·면 수·외형 크기(BBox)를 보존하여 로드합니다.
- 한글/유니코드가 포함된 Windows 파일 경로도 안전하게 읽고 저장합니다.

### 2. 2-Pass 외부 가시성 기반 내부 부품 자동 식별 (Intel Embree BVH)
- **Pass 1 (외부→내부 다방향 Ray Casting)**: Fibonacci 구면 시점(기본 64시점)에서 직교 광선 격자를 투사하고, Cauchy 적분 기하 면적 추정으로 삼각형 크기에 무관한 노출 비율을 계산합니다.
- **Pass 2 (내부→외부 반구 역방향 검증)**: 경계·소형 부품 표면에서 외부로 반구 광선을 발사해 소형 외부 볼트나 돌기가 오삭제되지 않도록 검증합니다.
- **수동 조정 지원**: GUI 부품 테이블에서 검색·필터·정렬 및 더블클릭/`Space` 키로 유지↔삭제 상태를 즉시 변경할 수 있습니다.

### 3. 100% 외관 원본 보존 정밀 B-Rep 솔리드화 (삼각 복셀 왜곡 없음)
- **외벽 안쪽 해석적 B-Rep 내부 코어**: 외피에서 안전 여백(`core_inset_mm`) 안쪽이면서 26방향 Embree 가시성 보호(방열핀 골짜기·외부 홈 제외) 영역에만 해석적 원통(`MakeCylinder`)·다각 프리즘(`MakePrism`) 코어를 생성해 회전부·커버 공차 간극을 내부에서만 연결합니다.
- **정밀 우선 Boolean Fuse**: `fuzzy=0.0` 우선 결합과 부피 보존 검증으로 SolidWorks/CATIA 등의 미세 필렛·탄젠트 엣지 손상을 방지합니다.
- **밀폐 내부 공동(Void Shell) 완전 제거**: `BRepClass3d.OuterShell_s` + `BRepBuilderAPI_MakeSolid`로 내부 공동과 내벽 기어 치형 등을 완전히 소거합니다.
- **공차 분리 외관 부품 내부 연결 & 동일 곡면 통합**: 미세 공차로 떠 있는 커넥터/볼트 등을 숨겨진 내부 B-Rep 핀으로 연결하고 `ShapeUpgrade_UnifySameDomain`으로 동일 평면·곡면을 통합해 단일 Watertight B-Rep Solid를 생성합니다.

### 4. 프리징 없는 Modern Flat GUI (`tkinter` / `ttk`)
- **전용 백그라운드 CAD 프로세스 분리 (`multiprocessing`)**: 무거운 OpenCASCADE C++ 연산이 Python GIL을 점유하더라도 GUI 창이 절대 멈추지(`응답 없음`) 않습니다.
- **동적 작업 상태 표시**: 작업 중 상단에 회전 스피너, 실시간 경과 타이머, 연속 펄스 진행 바, 2단계 `작업 취소 / 강제 중단` 버튼이 표시되며 창 제목에도 진행 시간이 반영됩니다.
- **4단계 완료 상태 시각화**: `STEP 열기 › 1. 내부 부품 분석 › 2. 단일 Solid 변환 › 결과 저장` 버튼이 실행 중(파랑 `●`) / 정상 완료(녹색 `✔`) 상태로 자동 전환되며, 부품 선택을 수동 변경하면 하위 단계가 자동 초기화됩니다.
- **초보자 친화적 레이아웃**: 우측 고급 파라미터 패널은 기본적으로 숨겨져 있으며 툴바의 **[파라미터]** 토글 버튼으로 필요할 때만 열어 세부 값을 조정할 수 있습니다.
- **외부 3D 뷰어 연동**: 설정 파일(`config.json`) 또는 **[뷰어 설정]**에서 시스템에 설치된 3D 뷰어를 지정해 **원본 STEP / 유지 부품 미리보기 / 최종 결과 STEP**을 바로 열어 확인할 수 있습니다.

---

## 요구 환경 및 설치

- **OS**: Windows (64-bit)
- **Python**: 3.13 이상 ([uv](https://docs.astral.sh/uv/) 패키지 매니저 사용)
- **핵심 라이브러리**: `cadquery-ocp-novtk` (OCCT 8.x), `trimesh`, `embreex` (Intel Embree), `rtree`, `numpy`, `scipy`, `scikit-image` (2D 내부 코어 윤곽 추출용)

```powershell
# 의존성 동기화
uv sync
```

---

## 실행 방법

### 1. 배치 파일로 실행 (권장)
- 탐색기에서 `run_simple_3d_step.bat`을 **더블클릭**하면 콘솔 창 없이 GUI가 바로 실행됩니다.
- `.step` / `.stp` 파일을 `run_simple_3d_step.bat` 아이콘 위로 **드래그 앤 드롭**하면 해당 파일을 바로 열면서 시작합니다.
- 최초 실행 시 가상환경(`.venv`)이 없으면 자동으로 `uv sync`를 수행합니다.

### 2. 명령줄에서 GUI 실행
```powershell
uv run simple-3d-step
uv run simple-3d-step path\to\assembly.step
```

### 3. CLI 배치 모드 실행
```powershell
# 기본 실행 (입력 파일과 같은 폴더에 *_simplified.step 생성)
uv run simple-3d-step --cli -i assembly.step -o assembly_simplified.step

# 주요 옵션 확인
run_simple_3d_step.bat --help
```

| CLI 옵션 | 기본값 | 설명 |
| :--- | :---: | :--- |
| `--views` | `64` | 가시성 분석 구면 시점 수 (빠름: 26, 표준: 64, 정밀: 162) |
| `--grid` | `160` | 시점당 직교 광선 격자 해상도 |
| `--min-visible` | `0.5` | 외부 부품 판정 최소 가시성 임계값 (`%`) |
| `--bridge-gap` | `3.5` | 내부 코어 생성 시 회전부·커버 간극 브리지 범위 (`mm`) |
| `--core-inset` | `2.0` | 외관 표면 보호 최소 안전 여백 (`mm`) |
| `--island-gap` | `3.0` | 공차 분리 외관 부품 내부 핀 연결 최대 간격 (`mm`) |
| `--remove-holes` | `False` | 외관 소구경 원통형 관통홀 자동 막기 활성화 |
| `--max-hole-dia` | `10.0` | `--remove-holes` 적용 시 최대 구멍 직경 (`mm`) |
| `--no-core` / `--no-unify` | `False` | 내부 코어 생성 / 동일 곡면 통합 비활성화 |
| `--schema` | `AP214` | 출력 STEP 스키마 (`AP214`, `AP242`, `AP203`) |

---

## GUI 기본 사용 흐름

1. **[STEP 열기]** (`Ctrl+O`): 내부 부품이 포함된 STEP 어셈블리 파일을 불러옵니다.
2. **[1. 내부 부품 분석]**: 외부에서 보이지 않는 내부 부품을 자동 식별해 삭제 대상으로 표시합니다.
   - 필요 시 부품 목록에서 더블클릭/`Space`로 유지·삭제를 수동 조정하고 **[유지 부품 미리보기]**로 외부 3D 뷰어에서 확인할 수 있습니다.
3. **[2. 단일 Solid 변환]**: 외관 형상을 보존한 채 내부 빈 공간을 솔리드로 채워 단일 B-Rep Solid로 변환합니다.
4. **[결과 뷰어]** / **[결과 저장]** (`Ctrl+S`): 결과를 외부 3D 뷰어로 확인하거나 원본 STEP과 같은 폴더에 저장합니다.

---

## 외부 3D 뷰어 설정 (`config.json`)

프로젝트 루트의 `config.json` 또는 GUI 상단 **[뷰어 설정]**에서 외부 3D 뷰어 실행 파일 경로를 지정할 수 있습니다. `viewer_executable`을 비워 두면 Windows 기본 연결 프로그램으로 STEP 파일을 엽니다.

```json
{
  "viewer_executable": "C:\\Program Files\\FreeCAD 1.0\\bin\\freecad.exe",
  "viewer_args": "{file}",
  "num_views": 64,
  "grid_resolution": 160,
  "min_visible_ratio_pct": 0.5,
  "fill_internal_core": true,
  "core_bridge_gap_mm": 3.5,
  "core_inset_mm": 2.0,
  "connect_floating_parts": true,
  "max_island_gap_mm": 3.0,
  "remove_small_holes": false,
  "unify_same_domain": true,
  "step_schema": "AP214"
}
```

---

## 프로젝트 구조

```text
SIMPLE_3D_STEP/
├── config.json                      # 외부 3D 뷰어 경로 및 기본 파라미터 설정
├── pyproject.toml                   # uv 프로젝트 및 의존성 정의
├── run_simple_3d_step.bat           # Windows 실행 배치 파일 (GUI/드래그앤드롭/CLI 겸용)
├── src/simple_3d_step/
│   ├── main.py                      # GUI 및 CLI 엔트리포인트
│   ├── config.py                    # config.json 로드/저장 및 외부 뷰어 실행
│   ├── models.py                    # 부품/분석/단순화/검증 데이터 모델
│   ├── core/
│   │   ├── occt_compat.py           # OCCT 7.x / 8.x 호환 래퍼
│   │   ├── step_io.py               # XCAF STEP 읽기 및 STEP 저장
│   │   ├── tessellation.py          # OCCT -> trimesh 테셀레이션 (부품/면 매핑 유지)
│   │   ├── visibility.py            # Intel Embree 2-Pass 외부 가시성 분석 엔진
│   │   ├── solidifier.py            # 외관 보존 정밀 B-Rep 솔리드화 및 단일화 엔진
│   │   ├── validator.py             # BRepCheck_Analyzer 및 곡면 구성 검증
│   │   └── worker.py                # GUI 프리징 방지용 상주형 multiprocessing CAD 워커
│   └── gui/
│       ├── theme.py                 # Modern Flat (Slate & Cobalt) 디자인 토큰/테마
│       ├── app.py                   # 메인 GUI 창 및 4단계 워크플로우 제어
│       ├── part_tree.py             # 어셈블리 부품 테이블 (검색/필터/정렬/수동 토글)
│       ├── control_panel.py         # 토글형 고급 파라미터 및 결과 요약 패널
│       ├── busy_banner.py           # 백그라운드 작업 애니메이션 배너
│       ├── log_panel.py             # 하단 상태바·진행률·로그 콘솔
│       └── settings_dialog.py       # 외부 3D 뷰어 설정 대화상자
└── tests/
    ├── generate_sample_step.py      # 테스트용 합성 STEP 어셈블리 생성기
    ├── test_visibility.py           # 내부 부품 식별 정확도 테스트
    ├── test_solidifier.py           # 정밀 B-Rep 단일 Solid 및 부피 채움 테스트
    ├── test_worker.py               # 멀티프로세싱 비차단 파이프라인 테스트
    ├── test_gui_layout.py           # Modern Flat 레이아웃·토글·버튼 상태·저장 경로 테스트
    └── test_config_and_cli.py       # 설정 저장 및 CLI 엔드투엔드 테스트
```

---

## 테스트 실행

```powershell
uv run pytest -v
```
