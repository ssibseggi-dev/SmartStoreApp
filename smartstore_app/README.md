# 스마트스토어 상품 등록 자동화 앱

> 프로젝트 소개와 기능은 [저장소 첫 화면 README](../README.md), 사용 순서는 [사용법.md](사용법.md)를 보세요.
> 이 문서는 소스에서 실행 · 테스트 · 빌드하는 **개발자용 안내**입니다.

## ▶️ 실행

`run.bat` 더블클릭, 또는 PowerShell에서:

```powershell
cd smartstore_app
..\.venv\Scripts\python.exe main.py
```

처음 한 번만 (저장소 루트에서):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r smartstore_app\requirements.txt
```

## 🧪 테스트

```powershell
cd smartstore_app
..\.venv\Scripts\python -m pytest -q
```

- 테스트 205개. `tests/conftest.py`가 네트워크 차단 · keyring 대역 · 임시 데이터 폴더를 걸어 두므로 실제 스토어나 API를 건드리지 않습니다.

## 📦 실행 파일(.exe) 빌드

`build.bat` 더블클릭 (PyInstaller가 없으면 `requirements-build.txt`로 자동 설치)

| 결과 (`dist\SmartStoreApp\`) | 용도 |
|---|---|
| `SmartStoreApp.exe` | 화면 앱 |
| `SmartStoreCli.exe` | 명령줄 도구 |
| `SmartStoreAuto.exe` | 예약 실행용 (창 없음, 작업 스케줄러가 사용) |
| `사용법.md` | 사용 설명서 |

- 폴더 전체가 하나의 프로그램입니다. 다른 PC에는 **폴더째로** 복사하세요.
- `build.bat lite` — 배경 제거(rembg · onnxruntime · scipy 등) 없이 작게 빌드합니다.
- 빌드 확인 — `SmartStoreApp.exe --self-test [--api]` 를 실행하면 데이터 폴더의 `selftest.txt`에 `RESULT=OK`가 남습니다.
  `SMARTSTORE_DATA_DIR` 환경변수로 데이터 폴더를 바꾸면 실제 데이터와 분리해서 점검할 수 있습니다.
- 버전을 올릴 때는 `version.py`와 `version_info.txt`를 함께 고칩니다.
- 아이콘을 바꿀 때는 `tools\make_icon.py`를 실행해 `resources\app.ico`, `app.png`를 다시 만듭니다.

## 💾 데이터 위치

| 항목 | 위치 |
|---|---|
| DB · 로그 | `%LOCALAPPDATA%\SmartStoreApp\` (`app.db`, `app.log`) |
| 작업 엑셀 · 사진 | 기본 `문서\스마트스토어\` (자동화 설정에서 변경) |
| ID · 시크릿 · API 키 · 토큰 | Windows 자격 증명 관리자 (서비스명 `smartstore_app`) |

다른 PC에서 쓸 때는 설정 화면에서 ID · 시크릿을 다시 입력하고(자격 증명은 PC별 저장), 그 PC의 공인 IP를 커머스API센터에 등록해야 합니다.

## ✅ 진행 현황 (v1.1.0)

- [x] **상품 등록 앱** — 설정 · 토큰 · 카테고리, 단일 등록, 조합형 옵션 · 템플릿 · 임시저장, 상품 관리, 엑셀 일괄 등록, exe 패키징
- [x] **자동화 9단계** — 작업 엑셀 · 분류 매핑, 실제 등록 · 한도 · 중복 확인, 사진 보정 · 배경 제거, 상세페이지, 검수 · 판매 시작, 예약 실행 · 카카오톡 알림, QR 사진 분리, AI 초안(Claude), 배포
- [x] **실제 상품 등록** — L001 꽈배기 니트, L002 코듀로이 후드
- [ ] 엑셀 일괄 등록 화면(수동용)의 실제 스토어 등록 검증
- [ ] 다음 할 일: 동영상 넣기, 검색 태그 자동 입력
