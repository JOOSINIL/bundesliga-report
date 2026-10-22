# 분데스리가 전술 리포트

매 라운드가 끝나면 자동으로 18개 팀의 전술 리포트를 만듭니다.

- 경기 결과: OpenLigaDB (무료, 키 필요 없음)
- 전술 분석: Claude API + 웹 검색 (Max 플랜의 월간 API 크레딧 사용)
- 실행: GitHub Actions, 매주 월·목 오전 9시 23분(한국 시간)
- 결과: `reports/latest.html` → Claude 예약 작업이 가져가 Claude 안의 리포트 페이지를 업데이트

## 리포트에 들어가는 것

- 최근 라운드 결과와 그 라운드의 전술 포인트
- 팀별: 기본 포메이션(전술판 그림), 기본 전술, 공격할 때, 수비할 때, 상대별 맞춤 전술, 최근 흐름, 출처

## 파일

| 파일 | 역할 |
|---|---|
| `report.py` | 프로그램 본체 |
| `.github/workflows/report.yml` | 자동 실행 일정 |
| `data/matches.json` | 경기별 분석 메모 (실행할 때마다 쌓임) |
| `reports/latest.html`, `latest.json` | 최신 리포트 |
| `reports/archive/` | 라운드별 지난 리포트 |

## 바꿀 수 있는 설정

`.github/workflows/report.yml`의 `env:` 아래에 추가하면 됩니다.

| 이름 | 기본값 | 설명 |
|---|---|---|
| `MODEL` | `claude-sonnet-5-5` | 더 깊은 분석은 `claude-opus-5-5` (비용 약 2배) |
| `MAX_SEARCHES` | `5` | 경기 하나당 웹 검색 최대 횟수 |
| `WORKERS` | `2` | 동시에 분석할 경기 수 |
| `MAX_NEW_MATCHES` | `0` | 한 번에 분석할 경기 수 제한 (0은 제한 없음) |

## 비용

실행할 때마다 리포트 상단에 그 실행의 대략적인 API 비용이 표시됩니다.
정확한 사용량은 Claude Console의 Usage 화면에서 확인하세요.
