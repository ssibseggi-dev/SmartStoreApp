# 스마트스토어 상품 등록 자동화 앱

사용 설명서: [사용법.md](사용법.md)

## 실행
`smartstore_app\run.bat` 더블클릭, 또는 PowerShell에서:
```
cd C:\Users\ssibs\workspace\product_listing\smartstore_app
..\.venv\Scripts\python.exe main.py
```
최초 설치: `python -m venv .venv` → `.venv\Scripts\python -m pip install -r smartstore_app\requirements.txt`

## 테스트
```
cd smartstore_app
..\.venv\Scripts\python -m pytest -q
```

## 실행파일(.exe) 빌드
`smartstore_app\build.bat` 더블클릭 (PyInstaller가 없으면 requirements-build.txt로 자동 설치)
- 결과: `dist\SmartStoreApp\` 폴더 전체가 프로그램 (라이브러리 공유). 다른 PC에는 이 폴더를 통째로 복사
  - `SmartStoreApp.exe` 화면 앱 · `SmartStoreCli.exe` 명령줄 · `SmartStoreAuto.exe` 예약 실행용(창 없음) · `사용법.md`
- `build.bat lite`: 배경 제거(rembg·onnxruntime·scipy 등) 없이 작게 빌드
- 빌드 확인: `SmartStoreApp.exe --self-test [--api]` → 데이터 폴더의 `selftest.txt`에 점검 결과(RESULT=OK)
  - `SMARTSTORE_DATA_DIR` 환경변수로 데이터 폴더를 바꿔 실제 데이터와 분리해 점검할 수 있음
- 버전을 올릴 때: `version.py`와 `version_info.txt`를 함께 수정
- 아이콘을 바꿀 때: `tools\make_icon.py` 실행 → `resources\app.ico`, `app.png` 재생성

다른 PC에서 쓸 때: 설정 화면에서 ID·시크릿을 다시 입력(자격 증명은 PC별 저장)하고, 그 PC의 공인 IP를 커머스API센터에 등록해야 합니다.

## 데이터 위치
- DB·로그: `%LOCALAPPDATA%\SmartStoreApp\` (app.db, app.log)
- ID·시크릿: Windows 자격 증명 관리자 (서비스명 `smartstore_app`)

## 진행 현황
- [x] 1단계: 설정 화면, 토큰 발급, 카테고리 조회·검색(로컬 캐시)
- [x] 2단계: 단일 상품 등록(옵션 없음) — 테스트 등록 시 전시중지로 등록 후 판매중지 전환
  - 실제 API로 등록·판매중지 전환 검증 완료 (2026-10-29)
- [x] 3단계: 조합형 옵션(최대 3그룹, 조합표 자동 생성), 템플릿(배송·반품·A/S·고시), 임시저장함
  - 옵션 상품 실제 API 등록 검증 완료 (2026-11-01)
- [x] 4단계: 상품 관리 — 스토어 상품 조회·필터, 가격·재고·옵션·판매/전시 상태 수정, 판매 상태 일괄 변경
  - 조회·원상품 수정(PUT v2)·판매 상태 변경 모두 실제 API로 검증 완료 (2026-11-01)
- [x] 5단계: 엑셀 일괄 등록 — 양식 다운로드(상품/옵션상세/안내 시트) → 불러오기·검증·미리보기 → 일괄 등록(진행률·중지) → 행별 결과 저장
  - 등록은 2~3단계 단일 등록 흐름을 그대로 사용. 실제 스토어 일괄 등록은 검증 전
- [x] 디자인: Fusion + resources/style.qss 테마(다크 사이드바, 카드형 폼, 하단 고정 액션바)
- [x] 6단계: PyInstaller 패키징 (폴더형 약 82MB) — exe 자가진단·실제 API 호출 확인 (2026-11-01)
