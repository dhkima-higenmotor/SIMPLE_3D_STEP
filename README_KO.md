# SIMPLE 3D STEP

> **Language:** [English (README.md)](README.md) | **한국어**
>
> **지원 운영체제:** **Microsoft Windows (64-bit)** · **Linux (64-bit)** (GUI 및 헤드리스 CLI)

내부 부품(모터 로터·스테이터, 기어, 베어링, PCB, 내부 체결부품 등)이 포함된 **기구 어셈블리 STEP 파일의 단일 Watertight Solid Body 변환(Single / Batch Mode)**과, Altium Designer에서 생성된 **복잡한 PCB STEP + IDF(`.bdf`/`.ldf`) 파일의 부품별 단일 Solid 경량화 변환(PCB Mode)**을 모두 지원하는 데스크톱/CLI 도구입니다. 기구 어셈블리에서는 **외부에서 보이지 않는 내부 부품을 자동 식별·삭제**하고, **외관 B-Rep CAD 형상을 100% 원본 그대로 보존**한 채 해석적 B-Rep 코어로 내부 빈 공간과 폐쇄 공동을 제거하여 단면(Clipping Plane) 뷰에서도 속이 꽉 찬 솔리드로 변환합니다.

## 핵심 기능 한눈에 보기 (변환 전 / 후)

### 기구 어셈블리 단일 솔리드화 (Single / Batch Mode)

![변환 전후 단면 비교: 복잡한 다부품 STEP 어셈블리가 외관 100% 보존 및 내부 완전 충진 단일 Solid Body로 변환된 모습](docs/screenshots/function.png)

> **왼쪽 (`원본 어셈블리 STEP`) → 오른쪽 (`단순화 단일 Solid STEP`)** 단면 절단(Clipping Plane) 비교:
> - **내부 설계 기밀 자동 보호 (IP Protection)**: 외부에서 보이지 않는 내부 부품(PCB, 모터 권선·코일, 마그넷, 베어링, 샤프트, 기어, 내부 볼트 등)을 자동 판별·제거하여 외부 공유 시 내부 구조 유출을 원천 차단합니다.
> - **외관 B-Rep CAD 형상 100% 원본 보존**: 하우징 외경, 모따기, 체결부, 외부 커넥터 포트 등 모든 외관 곡면을 삼각 메쉬/복셀 왜곡 없이 정확한 해석적 B-Rep 곡면 그대로 유지합니다.
> - **속이 완전히 꽉 찬 단일 Solid Body & 대폭 경량화**: 모든 내부 빈 공간과 폐쇄 공동을 제거한 엄격한 2-매니폴드 단일 솔리드(`Solid 1 / Shell 1`)를 생성하여 **SolidWorks**, **eDrawings**, **FreeCAD**, **OpenCASCADE CAD Assistant** 모두에서 단면 캡핑까지 정상 Solid로 열리며, STEP 파일 크기도 대폭 줄어듭니다(일반적으로 **85~95% 경량화**).

### PCB 어셈블리 부품별 경량화 (PCB Mode — Altium STEP + IDF)

![PCB 변환 전후 비교: Altium PCB STEP + IDF가 색상 유지 부품별 단일 Solid 및 누락 부품 보완 모델로 경량화된 모습](docs/screenshots/function_pcb.png)

> **왼쪽 (`원본 Altium PCB STEP`) → 오른쪽 (`단순화 PCB STEP + IDF`)**:
> - **부품별 1-Solid 단순화**: 내부 부품 삭제 과정을 생략하고 PCB 위의 각 부품(IC 칩, 커넥터, 인덕터, 전해콘덴서, 소형 SMD 등)을 **부품당 1개의 Watertight Solid**로 단순화합니다. IC 본체·냉각판·리드 단자 군집 및 커넥터 하우징·핀 배열을 직육면체 블록으로 통합 결합하고, 전해콘덴서 등 원통형 부품은 실린더로 단순화하며, 각인/양각 글자 피처는 제거합니다. 기판은 외곽선과 주요 마운팅 홀(`≥ pcb_min_board_hole_mm`, 기본 `0.8 mm`)을 보존합니다.
> - **본체 색상 유지·최소 명도 자동 보정 및 2단계 명칭 보존**: 각 부품의 본체 대표 RGB 색상을 유지하면서 지나치게 어두운 검은색 부품(`J1_HDR1x2` 등)은 보기 편한 명도로 자동 보정합니다. Altium의 2단계 XCAF 계층(`부품번호(RefDes) → 최하단 부품명칭`)과 동일 부품 프로토타입 인스턴싱을 유지해 파일 크기를 대폭 줄입니다(**최대 ~85–90% 경량화**).
> - **Altium IDF (`.bdf` / `.ldf`) 누락 부품 보완**: 동일 폴더의 `.bdf`/`.ldf` 파일을 자동 연결해 STEP에서 누락된 부품만 정확한 위치·회전 각도의 직육면체로 보완하며, STEP에 이미 존재하는 부품은 절대 덮어쓰지 않습니다.

---

## GUI 화면

**Slate + Indigo (Premium Light)** 테마와 Windows DPI 인식 `Segoe UI` 폰트를 적용한 고밀도 단일 창(기본 `1020×680`, 설정 폰트 크기에 맞춰 위젯 잘림 없이 자동 크기 최적화)에서 **Single Mode**(대화형 4단계 워크플로우), **Batch Mode**(다중 기구 STEP 병렬 처리), **PCB Mode**(Altium PCB STEP + IDF 병렬 단순화)의 세 탭을 언제든 전환할 수 있습니다.

### Single Mode — 대화형 4단계 워크플로우

![Single Mode 화면: 부품 분석과 정밀 B-Rep 솔리드화 완료 상태](docs/screenshots/gui_single_mode.png)

상단 4단계 워크플로우 버튼(**Open STEP → 1. Analyze Parts → 2. Convert to Solid → Save Result**)은 파란색(Indigo)으로 강조되며 완료된 단계는 녹색 `✔`로 바뀌고, **Cancel** 버튼은 빨간색(Rose) 스타일로 표시됩니다. 부품 테이블에서 외관 부품은 유지(Keep), 숨은 내부 부품은 연한 적색(Drop)으로 표시되며, 우측 결과 배지(`Solid 1 / Shell 1 · Fill +402,124 mm³`)와 하단 3행 라이트 **Output Log**로 진행 상태를 한눈에 확인할 수 있습니다. 우측 고급 파라미터 패널은 기본 숨김이며 **[Parameters]** 버튼으로 토글합니다.

### Batch Mode — 다중 파일 병렬 처리

![Batch Mode 화면: Batch Mode에서 STEP 파일을 처리하고 각 원본과 같은 폴더에 결과 저장](docs/screenshots/gui_batch_mode.png)

대기 중인 각 파일의 상태(`Pending` / `Running` / `Done` / `Failed` / `Skipped`), 원본 폴더, 출력 파일명이 표시됩니다. 주요 실행 버튼(**Add STEP Files...**, **RUN**)은 파란색(Indigo)으로 강조되고, **Force Stop**, **Stop**, **Cancel** 버튼은 빨간색(Rose) 스타일로 구분됩니다. **[RUN]**을 누르면 선택된 행(선택이 없으면 전체 큐)을 `config.json` 파라미터와 상주형 멀티 워커 CAD 풀(`batch_workers`, `0` = Auto) 및 워커별 스레드 예산 분할로 병렬 처리하고, 상태바에 `Batch finished — N succeeded, M failed` 요약이 표시됩니다. **출력이 생성된 행을 더블클릭**하면 해당 `<파일명>_simplified.step` 파일이 설정된 외부 3D 뷰어로 열립니다.

### PCB Mode — Altium PCB STEP + IDF 경량화

![PCB Mode 화면: 다중 Altium PCB STEP + IDF 대기열과 토글형 우측 PCB Parameters 패널](docs/screenshots/gui_pcb_mode.png)

Altium Designer에서 출력된 복잡한 PCB 어셈블리 경량화에 특화된 탭입니다. `.step` / `.stp` 파일을 추가하면 동일 폴더의 `.bdf`/`.ldf`(또는 `.brd`/`.pro`, `.emn`/`.emp`) IDF 파일을 자동 연결하며, **[Add IDF...]** 버튼(STEP 파일이 없어도 항상 활성화)으로 STEP 없이 IDF 파일만으로 PCB 모델을 생성하거나 STEP 행에 수동 연결할 수 있습니다. 우측 상단 **[Parameters]** 버튼(Single Mode와 동일한 토글 방식, 기본 숨김)을 열어 **Delete Board Holes < (mm)**, **Exclude Parts ≤ (mm)**, **Min Color Brightness** 값을 조정하고 `config.json`에 저장할 수 있습니다. **[RUN]** 실행 시 STEP 및 IDF 단독 항목을 병렬 처리해 원본 폴더에 `<파일명>_pcb_simplified.step`으로 저장하며, **완료된 행을 더블클릭**하면 외부 3D 뷰어로 즉시 열립니다.

---

## 주요 특징

> 📖 **수학·알고리즘 배경:** [한국어 (docs/theory_ko.md)](docs/theory_ko.md) · [English (docs/theory.md)](docs/theory.md)

### 1. XCAF 기반 STEP 어셈블리 파싱 (`STEPCAFControl_Reader`)
- 어셈블리 계층 구조, 부품 이름, 인스턴스 위치 변환(`TopLoc_Location`), 부피, 표면적, 면 수, 외형 크기(BBox)를 보존하여 로드합니다.
- 한글/유니코드가 포함된 Windows 파일 경로도 안전하게 읽고 저장합니다.

### 2. 멀티코어 2-Pass 외부 가시성 분석 (Intel Embree BVH)
- **병렬 버킷 테셀레이션**: 부품을 로그 Deflection 버킷별 `TopoDS_Compound`로 묶어 OCCT 네이티브 C++ 병렬 메셔(`BRepMesh_IncrementalMesh` / `OSD_Parallel`)를 일괄 실행해 모든 CPU 코어를 활용하며, 생성된 메시는 이후 내부 코어 생성 단계에서도 재사용합니다.
- **Pass 1 (외부→내부 다방향 Ray Casting)**: Fibonacci 구면 시점(기본 64시점)의 직교 광선 격자를 `ThreadPoolExecutor`(`nogil` Embree 쿼리)로 병렬 투사하고, Cauchy 적분 기하 면적 추정으로 삼각형 크기에 무관한 노출 비율을 계산합니다.
- **Pass 2 (내부→외부 반구 역방향 검증)**: 경계 및 소형 부품 표면에서 외부로 반구 광선을 병렬 발사(결정적 샘플링 유지)해 소형 외부 볼트·커넥터·돌기가 오삭제되지 않도록 검증합니다.
- **수동 조정 지원**: GUI 부품 테이블에서 검색·필터·정렬 및 더블클릭/`Space` 키로 언제든지 `Keep`(유지) ↔ `Drop`(삭제) 상태를 수동 변경할 수 있습니다.

### 3. 100% 외관 원본 보존 정밀 B-Rep 솔리드화 (삼각 복셀 왜곡 없음)
- **외벽 안쪽 해석적 B-Rep 내부 코어**: 외피에서 안전 여백(`core_inset_mm`) 안쪽 영역에 멀티스레드 Embree 3축 점유 스윕과 26방향 하늘 가시성 보호(방열핀 골짜기·외부 홈 유출 방지)를 적용하고, 동일 단면 연속 슬랩 병합 및 보수적 동축 원통 병합으로 불필요한 내부 분할을 줄인 해석적 원통(`MakeCylinder`)·다각 프리즘(`MakePrism`) 코어를 생성합니다.
- **정밀 우선 Boolean Fuse**: `fuzzy=0.0` 병렬 다중 도구 결합을 우선 시도하고 부피 보존 검증을 수행해 SolidWorks·CATIA·NX의 미세 필렛과 탄젠트 엣지 손상을 방지하며, 폴백 시 공간 근접 기반 균형 이진 트리($O(N \log N)$) 병합을 사용합니다.
- **밀폐 내부 공동(Closed Void Shell) 완전 제거**: 최종 후처리 단계에서 다중 쉘 내부 공동(`BRepClass3d.OuterShell_s` + `BRepBuilderAPI_MakeSolid`)뿐만 아니라 단일 쉘 내부에 숨어 있는 비연결 내부 공동 컴포넌트(고속 `Face→Edge` 인덱스 인접 그래프 BFS)까지 모두 소거하여, 단면 절단 시 속이 완전히 꽉 찬 솔리드를 보장합니다.
- **이종 CAD 커널 SolidWorks(Parasolid) 및 OpenCASCADE CAD Assistant 호환성**: 엄격한 2-매니폴드 솔리드 위상과 Parasolid 정밀도(`>= 1e-5 mm`)를 보장하기 위해 Boolean 탄젠트 슬릿(`TopAbs_INTERNAL` 엣지) 자동 감지 및 초정밀 `5e-5`–`1e-4` fuzzy 재시도, 비매니폴드 제로-두께(4면 교차) 엣지 및 핀치 정점의 미세 해석적 비드(`r = 0.05 mm`) 힐링, 정밀도 미만 미세 엣지(`< 5e-5 mm`) 정리, 최단 엣지 길이의 절반 이하로 상한을 맞춘 STEP `UNCERTAINTY` 클램핑(`1e-5`–`1e-3`), `ShapeUpgrade_ShapeDivideClosed` 주기 곡면 분할, 공차 팽창 방지 가드가 적용된 `ShapeFix_FixSmallFace` 슬리버 정리, 무왜곡 곡선 엣지 분할, 자동 **내접 핀치 와이어 홀 분할**, 병렬 `BRepCheck_Analyzer` 검증을 수행하여 SolidWorks, eDrawings, FreeCAD, OpenCASCADE CAD Assistant(OCCT 7.x) 모두에서 깨짐 없이 단면 캡핑까지 정상 Solid Body로 열립니다.
- **공차 분리 외관 부품 연결 및 동일 곡면 통합**: 멀티스레드 `cKDTree(workers=-1)` 탐색으로 미세 공차로 떠 있는 외관 부품(커넥터·볼트 등)을 숨겨진 내부 B-Rep 핀으로 연결하고 `ShapeUpgrade_UnifySameDomain`으로 동일 평면·곡면을 통합합니다.

### 4. Altium PCB STEP + IDF 단순화 엔진 (`PCB Mode`)
- **PCB 기판 정리**: PCB 기판 솔리드를 자동 판별해 외곽선과 주요 마운팅 홀(`≥ pcb_min_board_hole_mm`, 기본 `0.8 mm`)은 그대로 유지하고, 기준 미만의 미세 비아 홀(`BRepAlgoAPI_Defeaturing`) 및 동일 평면을 정리합니다.
- **부품별 1-Solid 경량화**:
  - **IC 칩·커넥터**: 외곽 리드·핀 배열을 측면 뱅크별 단순 직육면체로 묶어 본체·냉각판(방열 탭)과 미세 중첩 후 `BRepAlgoAPI_Fuse` + `ShapeUpgrade_UnifySameDomain`으로 1개의 Watertight Solid로 통합합니다.
  - **원통형 부품**: 전해콘덴서·원통 캔·원형 인덕터 등을 원통면 면적비·부피비로 판별해 정확한 `BRepPrimAPI_MakeCylinder` 실린더로 단순화합니다(SMD 전해콘덴서의 넓은 사각 베이스는 실린더와 1-Solid 병합).
  - **각인 글자 제거 및 임의 각도 OBB**: 상단 미세 각인/양각 글자 Solid를 제거하고, 전체 정점 기반 2D Convex Hull Rotating Calipers OBB를 계산해 45°·115° 등으로 회전 실장된 커넥터/부품도 원래 위치와 각도에 정확히 밀착시킵니다.
  - **소형 부품 제외 필터**: `pcb_exclude_small_part_mm > 0` 설정 시 기판 외 소형 부품을 자동 제외합니다.
- **색상 유지·최소 명도 자동 보정 및 2단계 XCAF 명칭**: 본체 면적 가중 대표 RGB 색상을 추출하고 지나치게 어두운 부품은 `pcb_min_color_brightness`(기본 `0.12`) 이상으로 색조를 유지한 채 밝게 보정합니다. Altium의 2단계 계층(`RefDes → 최하단 부품명칭`)과 동일 규격 부품 프로토타입 인스턴싱을 유지해 경량화 효과를 극대화합니다(`AP214` / `AP242`).
- **Altium IDF (`.bdf` / `.ldf`) 누락 부품 보완**: IDF 2.0/3.0(`MM`/`THOU`, `TOP`/`BOTTOM`)을 파싱해 중앙값 잔차 보정으로 STEP 기판 좌표계와 정밀 정합하고, `RefDes` 우선 대조 + 공간 겹침 검사로 STEP 기존 부품은 절대 건드리지 않으면서 누락된 부품만 회전 직육면체로 추가합니다.

### 5. 프리징 없는 Slate + Indigo GUI (`tkinter` / `ttk`) 및 멀티코어 배치 풀
- **고밀도 Slate + Indigo 통일 테마**: DPI 인식 `Segoe UI` 폰트, 밝은 Slate-50/100 배경, Indigo 파란색 주요 실행 버튼, Rose 빨간색 중지·취소 버튼, 폰트 크기에 맞춰 자동 최적화되는 창 크기(기본 `1020×680`)로 위젯 잘림 없는 화면을 제공합니다.
- **하나의 창에서 세 가지 모드 탭**: **Single Mode**(대화형 4단계 워크플로우), **Batch Mode**(다중 기구 STEP 병렬 처리), **PCB Mode**(Altium PCB STEP + IDF 병렬 경량화)를 즉시 전환할 수 있으며, 모드별로 독립된 백그라운드 CAD 워커 풀을 사용해 서로 블로킹하지 않습니다.
- **전용 백그라운드 CAD 프로세스 풀 (`multiprocessing`)**: 무거운 OpenCASCADE C++ 연산을 상주형 워커 프로세스(들)에서 처리하므로 GUI 창이 절대 멈추지(`응답 없음`) 않습니다. Batch Mode는 `config.json`의 `batch_workers`(`0` = Auto)에 따라 다중 워커를 병렬 실행하며, 워커별 `OSD_ThreadPool`과 Embree 스레드 예산을 자동 분할해 코어 과구독을 방지합니다.
- **실시간 작업 상태 배너**: 작업 중 회전 스피너, 실시간 경과 타이머, 연속 펄스 진행 바, 빨간색 2단계 `Cancel / Force Stop` 버튼을 표시하며 창 제목에도 경과 시간을 반영합니다.
- **Bold 강조 4단계 워크플로우 바 및 상태 피드백**: `Open STEP › 1. Analyze Parts › 2. Convert to Solid › Save Result` 버튼이 파란색 볼드 스타일로 강조되며 대기 / 실행 중(`●`) / 정상 완료(녹색 `✔`) 상태로 자동 전환됩니다. 부품 선택을 수동 수정하면 하위 단계 상태가 자동 초기화됩니다.
- **깔끔하고 컴팩트한 레이아웃**: 우측 고급 파라미터 패널은 기본 숨김 상태이며 필요할 때만 툴바의 **[Parameters]** 버튼으로 토글해 조정할 수 있습니다.
- **외부 3D 뷰어 연동**: `config.json` 또는 GUI의 **[Viewer Settings]**에서 설치된 외부 3D CAD 뷰어를 지정하면 **원본 STEP**, **유지 부품 미리보기**, **최종 결과 STEP**을 원클릭으로 바로 열 수 있습니다.

---

## 지원 운영체제 및 요구 환경

### 지원 운영체제

| 운영체제 | 아키텍처 | 지원 실행 모드 | 런처 스크립트 | 기본 설정 템플릿 |
| :--- | :---: | :--- | :--- | :--- |
| **Microsoft Windows** (Windows 10 / 11) | 64-bit (`x86_64`) | 데스크톱 GUI (Single / Batch / PCB) 및 CLI | `run_simple_3d_step.bat` | `config.json.win` |
| **Linux** (Ubuntu, Fedora, Debian 등) | 64-bit (`x86_64`) | 데스크톱 GUI (Single / Batch / PCB, X11/Wayland) 및 헤드리스 CLI | `run_simple_3d_step.sh` | `config.json.linux` |

### 요구 환경 및 설치

- **Python**: 3.13 이상 ([uv](https://docs.astral.sh/uv/) 패키지 매니저 사용)
- **핵심 라이브러리**: `cadquery-ocp-novtk` (OCCT 8.x), `trimesh`, `embreex` (Intel Embree), `rtree`, `numpy`, `scipy`, `scikit-image` (2D 내부 코어 윤곽 추출용)
- **권장 외부 3D 뷰어 (별도 설치, 선택 사항)**: [OpenCASCADE CAD Assistant](https://opencascade.com/products/cad-assistant/) (`https://opencascade.com/products/cad-assistant/`) — Windows와 Linux를 모두 지원하는 무료 OpenCASCADE 기반 STEP 뷰어로, 유지 부품 미리보기 및 단면(Clipping Plane) 솔리드 검증에 권장합니다.

```bash
# 의존성 동기화
uv sync
```

---

## 실행 방법

### 1. 런처 스크립트로 실행 (Windows / Linux 권장)
- **Windows (`run_simple_3d_step.bat`)**:
  - 파일 탐색기에서 `run_simple_3d_step.bat`을 **더블클릭**하면 콘솔 창 없이 통합 GUI가 실행됩니다.
  - `.step` / `.stp` 파일 **1개**를 `run_simple_3d_step.bat` 아이콘 위로 **드래그 앤 드롭**하면 **Single Mode** 탭에서 바로 열립니다.
  - **여러 개**의 STEP 파일을 한꺼번에 드롭하면 **Batch Mode** 탭에 모두 대기 목록으로 추가된 상태로 실행됩니다.
- **Linux (`run_simple_3d_step.sh`)**:
  ```bash
  ./run_simple_3d_step.sh                        # Single Mode GUI 실행
  ./run_simple_3d_step.sh --batch                # Batch Mode GUI 실행
  ./run_simple_3d_step.sh --pcb                  # PCB Mode GUI 실행
  ./run_simple_3d_step.sh path/to/assembly.step  # Single Mode에서 파일 바로 열기
  ./run_simple_3d_step.sh a.step b.step          # Batch Mode 대기열에 추가하여 실행
  ./run_simple_3d_step.sh --cli -i model.step    # 헤드리스 CLI 모드 실행
  ```
- 최초 실행 시 `.venv` 가상환경이 없으면 자동으로 `uv sync`를 수행합니다.

### 2. 명령줄에서 GUI 실행
```powershell
uv run simple-3d-step                 # Single Mode 탭
uv run simple-3d-step --batch         # Batch Mode 탭
uv run simple-3d-step --pcb           # PCB Mode 탭 (Altium STEP + IDF)
uv run simple-3d-step --pcb a.step b.step  # PCB Mode에 파일 대기 후 실행
uv run simple-3d-step path\to\assembly.step
uv run simple-3d-step a.step b.step   # Batch Mode 탭에 2개 파일 대기
```

### 3. CLI 배치 모드 실행
```powershell
# 단일 파일 실행 (입력 파일과 같은 폴더에 *_simplified.step 생성)
uv run simple-3d-step --cli -i assembly.step -o assembly_simplified.step

# 다중 파일 병렬 배치 실행 (-j 0 = 자동 워커 풀, 또는 -j 4처럼 명시)
uv run simple-3d-step --cli a.step b.step c.step d.step -j 4

# 전체 CLI 옵션 확인
run_simple_3d_step.bat --help
```

| CLI 옵션 | 기본값 | 설명 |
| :--- | :---: | :--- |
| `--views` | `64` | 가시성 분석 Fibonacci 구면 시점 수 (빠름: 26, 표준: 64, 정밀: 162) |
| `--grid` | `160` | 시점당 직교 광선 격자 해상도 |
| `--min-visible` | `0.5` | 외부 부품 판정 최소 가시성 임계값 (`%`) |
| `--bridge-gap` | `3.5` | 로터·커버 간극을 연결하는 내부 코어 브리지 범위 (`mm`) |
| `--core-inset` | `2.0` | 외관 B-Rep 표면 보호 최소 안전 여백 (`mm`) |
| `--island-gap` | `3.0` | 공차 분리 외관 부품의 숨겨진 내부 핀 연결 최대 간격 (`mm`) |
| `--remove-holes` | `False` | 외관 소구경 원통형 관통홀 자동 막기 활성화 |
| `--max-hole-dia` | `10.0` | `--remove-holes` 사용 시 막을 최대 구멍 직경 (`mm`) |
| `--no-core` / `--no-unify` | `False` | 내부 코어 채움 / 동일 곡면 통합 비활성화 |
| `--schema` | `AP214` | 출력 STEP 스키마 (`AP214`, `AP242`, `AP203`) |
| `-j`, `--jobs` | `0` | 다중 파일 CLI 배치 병렬 워커 프로세스 수 (`0` = 자동) |

### 4. 모드 탭 — Single Mode / Batch Mode / PCB Mode

GUI 상단의 탭으로 언제든 세 가지 모드를 전환할 수 있습니다 (화면은 [GUI 화면](#gui-화면) 참고).

**Single Mode** — 아래에 설명된 대화형 4단계 워크플로우 (어셈블리 1개 열기 → 부품 분석 → 변환 → 저장).

**Batch Mode** — `config.json` 파라미터로 동작하는 기구 어셈블리 다중 파일 병렬 대기열:

- **[Add STEP Files...]** (`Ctrl+O`)로 여러 개의 `.step` / `.stp` 파일을 한 번에 선택할 수 있고, **[Remove Selected]** / **[Clear List]**로 목록을 정리합니다.
- **[RUN]** (`F5`)을 누르면 대기 중인 모든 파일(또는 행을 선택한 경우 선택된 파일만)이 `config.json`의 파라미터와 상주형 CAD 워커 풀(`batch_workers`, `0` = Auto)로 **병렬** simplify됩니다.
- 생성된 결과는 **각 원본 파일과 같은 폴더**에 `<파일명>_simplified.step`으로 저장됩니다.
- **[Stop]** (`Esc`)은 실행 중인 파일들을 취소하고 남은 파일을 건너뛰며, **[Force Stop]**은 C++ 연산이 멈추지 않을 때 활성 백그라운드 CAD 워커를 즉시 종료 후 재시작합니다.

**PCB Mode** — Altium Designer PCB STEP + IDF 다중 파일 병렬 경량화 대기열:

- **[Add STEP Files...]** (`Ctrl+O`)로 여러 PCB `.step` / `.stp` 파일을 추가하면 같은 폴더의 동명 `.bdf` / `.ldf`(및 보조 `.brd`/`.pro`, `.emn`/`.emp`) IDF 파일이 자동 연결됩니다. **[Add IDF...]** 버튼으로 STEP 파일 없이 IDF 단독 모델을 추가하거나 STEP 행에 IDF를 연결할 수 있으며, 행 우클릭 메뉴로 수동 변경·해제도 가능합니다.
- 우측 상단 **[Parameters]** 토글 버튼으로 **PCB Parameters** 패널을 열고 `Delete Board Holes < (mm)`, `Exclude Parts ≤ (mm)`, `Min Color Brightness`를 설정·저장할 수 있습니다.
- **[RUN]** (`F5`) 실행 시 병렬 처리 후 각 원본과 같은 폴더에 `<파일명>_pcb_simplified.step`으로 저장되며, 완료된 행을 더블클릭하면 외부 3D 뷰어로 열립니다.
- 작업 중 다른 탭을 보고 있어도 탭 라벨에 실시간 진행률(`● Batch Mode (2/5)`, `● PCB Mode (1/2)`)이 표시됩니다.

---

## GUI 기본 사용 흐름 (Single Mode)

1. **[Open STEP]** (`Ctrl+O`): 내부 부품이 포함된 3D STEP 어셈블리 파일을 엽니다.
2. **[1. Analyze Parts]**: 외부에서 보이지 않는 내부 부품을 자동 식별해 `Drop` 대상으로 분류합니다.
   - 필요 시 부품 목록에서 더블클릭 또는 `Space` 키로 `Keep` / `Drop`을 수동 조정하고 **[Preview Kept Parts]**로 외부 3D 뷰어에서 확인할 수 있습니다.
3. **[2. Convert to Solid]**: 내부 빈 공간을 채우고 밀폐 내부 공동을 제거한 뒤 유지된 외관 부품을 단일 Watertight B-Rep Solid로 변환합니다.
4. **[Open Result]** / **[Save Result]** (`Ctrl+S`): 결과를 외부 3D 뷰어로 확인하거나 원본 STEP과 같은 폴더에 저장합니다(기본 파일명 `*_simplified.step`).

---

## 외부 3D 뷰어 및 GUI 폰트 설정 (`config.json`)

> **운영체제별 기본 설정 자동 생성:** 프로그램 시작 시 `config.json` 파일이 존재하지 않으면, **Microsoft Windows**에서는 `config.json.win`, **Linux**에서는 `config.json.linux` 파일을 `config.json`으로 자동 복사한 뒤 즉시 반영합니다.

> **권장 외부 3D 뷰어:** 원본 STEP 어셈블리, 유지 부품 미리보기, 최종 솔리드 변환 결과(단면/클리핑 플레인 확인 포함)를 보기 위해 별도로 설치하는 뷰어로는 Windows와 Linux를 모두 지원하는 무료 뷰어인 **[OpenCASCADE CAD Assistant](https://opencascade.com/products/cad-assistant/)** (`https://opencascade.com/products/cad-assistant/`) 사용을 권장합니다.
> - **Windows 기본 경로**: `C:/Program Files/CAD Assistant/CADAssistant.exe`
> - **Linux 기본 경로**: `/usr/bin/cadassistant` (또는 `PATH`에 등록된 `cadassistant`)

프로젝트 루트의 `config.json` 또는 GUI 상단의 **[Settings]**에서 외부 3D 뷰어 경로와 GUI 폰트(`ui_font_family`, `ui_font_size`)를 설정할 수 있습니다. `viewer_executable`을 비워 두면 시스템 기본 연결 프로그램(Windows 기본 앱 / Linux `xdg-open`)으로 STEP 파일을 엽니다. Linux에서는 절대 경로(예: `"/usr/bin/cadassistant"`)뿐 아니라 `PATH`에 등록된 명령어(예: `"cadassistant"`)도 그대로 지정할 수 있습니다. GUI 폰트는 저장 즉시 재시작 없이 반영되며, `ui_font_family`를 비워 두면 OS 기본 폰트(Windows `Segoe UI`, Linux `Noto Sans` / `DejaVu Sans` 등)가 자동 선택됩니다.

```json
{
  "viewer_executable": "C:/Program Files/CAD Assistant/CADAssistant.exe",
  "viewer_args": "{file}",
  "ui_font_family": "",
  "ui_font_size": 9,
  "num_views": 162,
  "grid_resolution": 200,
  "min_visible_ratio_pct": 0.5,
  "fill_internal_core": true,
  "core_bridge_gap_mm": 3.5,
  "core_inset_mm": 2.0,
  "connect_floating_parts": true,
  "max_island_gap_mm": 3.0,
  "remove_small_holes": false,
  "unify_same_domain": true,
  "step_schema": "AP214",
  "batch_workers": 0,
  "pcb_simplify_leads": true,
  "pcb_detect_cylinders": true,
  "pcb_cylinder_circularity_min": 0.8,
  "pcb_remove_text_features": true,
  "pcb_small_part_box_mm": 4.0,
  "pcb_preserve_colors": true,
  "pcb_use_idf_supplement": true,
  "pcb_keep_board_holes": true,
  "pcb_min_board_hole_mm": 0.8,
  "pcb_exclude_small_part_mm": 0.0,
  "pcb_min_color_brightness": 0.12,
  "pcb_idf_default_color": [0.35, 0.35, 0.38]
}
```

> `batch_workers: 0`은 시스템 CPU 코어 수에 맞춰 워커 풀 크기를 자동 산정합니다(처리량과 워커당 RAM 균형을 위해 최대 6워커, 워커별 스레드 예산 자동 분할). 순차 실행을 원하면 `1`, 직접 지정하려면 원하는 정수 `N`을 설정하세요.

---

## 프로젝트 구조

```text
SIMPLE_3D_STEP/
├── README.md                        # 영문 문서
├── README_KO.md                     # 한국어 문서
├── config.json                      # 현재 적용 설정 (미존재 시 .win / .linux에서 자동 복사)
├── config.json.win                  # Microsoft Windows 기본 설정 템플릿
├── config.json.linux                # Linux 기본 설정 템플릿
├── pyproject.toml                   # uv 프로젝트 메타데이터 및 의존성 정의
├── run_simple_3d_step.bat           # Windows 실행 배치 파일 (Single/Batch 탭 GUI, 드래그앤드롭, CLI 겸용)
├── run_simple_3d_step.sh            # Linux/POSIX 실행 쉘 스크립트 (자동 uv sync, GUI 및 헤드리스 CLI)
├── docs/
│   ├── screenshots/                 # README 화면 캡처 (function, function_pcb, Single/Batch/PCB GUI)
│   ├── theory.md                    # 주요 특징의 수학·알고리즘 배경 (영문)
│   └── theory_ko.md                 # 동일 문서의 한국어 버전
├── src/simple_3d_step/
│   ├── main.py                      # GUI 및 CLI 엔트리포인트 (--batch, --pcb, --cli)
│   ├── config.py                    # config.json 저장/로드 및 외부 뷰어 실행
│   ├── models.py                    # 어셈블리/가시성/솔리드화/PCB 데이터 모델
│   ├── core/
│   │   ├── occt_compat.py           # OCCT 7.x / 8.x 호환, 병렬 런타임 설정 및 BinTools IPC
│   │   ├── step_io.py               # XCAF STEP 입출력 (단일 솔리드 및 2단계 컬러 PCB XCAF)
│   │   ├── idf_parser.py            # Altium IDF 2.0/3.0 (.bdf/.ldf) 파서·정합·누락 부품 보완
│   │   ├── pcb_simplifier.py        # 부품별 PCB 1-Solid 단순화 엔진 (실린더·리드·OBB·명도 보정)
│   │   ├── tessellation.py          # 병렬 버킷 OCCT -> trimesh 테셀레이션 및 서브셋 재사용
│   │   ├── visibility.py            # 멀티스레드 Intel Embree 2-Pass 외부 가시성 분석 엔진
│   │   ├── solidifier.py            # 외관 보존 정밀 B-Rep 솔리드화 및 내부 공동 소거 엔진
│   │   ├── validator.py             # 병렬 BRepCheck_Analyzer 및 해석 곡면 구성 검증
│   │   └── worker.py                # 스레드 예산 제어가 포함된 상주형 multiprocessing CAD 워커
│   └── gui/
│       ├── theme.py                 # Slate + Indigo (Premium Light, DPI 인식) 디자인 토큰 및 ttk 테마
│       ├── app.py                   # 통합 GUI 창: Single/Batch/PCB 모드 탭 + 폰트 인식 창 크기 최적화
│       ├── batch_panel.py           # Batch 모드 탭 (config.json 기반 다중 워커 병렬 RUN)
│       ├── pcb_panel.py             # PCB 모드 탭 (Altium STEP + IDF 대기열 및 토글형 PCB Parameters)
│       ├── part_tree.py             # 어셈블리 부품 테이블 (검색/필터/정렬/수동 토글)
│       ├── control_panel.py         # 토글형 고급 파라미터 및 결과 요약 패널
│       ├── busy_banner.py           # 백그라운드 작업 애니메이션 배너
│       ├── log_panel.py             # 하단 상태바·진행률·3행 라이트 Output Log
│       └── settings_dialog.py       # 외부 3D 뷰어 및 폰트 설정 대화상자
└── tests/
    ├── generate_sample_step.py      # 자동 테스트용 합성 기구 STEP 어셈블리 생성기
    ├── generate_sample_pcb.py       # 자동 테스트용 합성 Altium PCB STEP + IDF 생성기
    ├── test_visibility.py           # 내부 부품 식별 정확도 테스트
    ├── test_solidifier.py           # 정밀 B-Rep 단일 Solid 및 내부 공동 소거 테스트
    ├── test_idf_parser.py           # IDF 2.0/3.0 파싱·좌표 정합·비대체 매칭 테스트
    ├── test_pcb_simplifier.py       # 부품별 1-Solid·실린더·명도 보정·소형 부품 제외 테스트
    ├── test_pcb_gui.py              # PCB Mode 탭·IDF 연결·토글형 Parameters·배치 실행 테스트
    ├── test_multicore.py            # 멀티코어 Embree 결정성·버킷 메싱·병렬 CLI 배치 테스트
    ├── test_worker.py               # 멀티프로세싱 비차단 파이프라인 테스트
    ├── test_gui_layout.py           # Modern Flat 레이아웃·Bold 워크플로우 바·토글·저장 경로 테스트
    ├── test_batch_gui.py            # 모드 탭·배치 파라미터 매핑·동일 폴더 저장·배치 실행 테스트
    └── test_config_and_cli.py       # 설정 저장 및 CLI 엔드투엔드 테스트
```

---

## 테스트 실행

```powershell
uv run pytest -v
```
