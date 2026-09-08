# naver-gcal-bridge

네이버 캘린더 일정을 **구글 캘린더에서 함께 보기** 위한 단방향 동기화 릴레이.

```
네이버 캘린더 ──CalDAV──▶ GitHub Actions (30분 주기)
                              │
                              ├─▶ docs/calendar.ics ──GitHub Pages──▶ 구글 캘린더 "URL로 추가"   (모드 A)
                              └─▶ Google Calendar API ──▶ 구글 보조 캘린더에 직접 upsert          (모드 B)
```

## 두 가지 모드

| | 모드 A · ICS 구독 | 모드 B · 직접 upsert |
|---|---|---|
| 반영 지연 | 구글 재조회 주기에 종속 (수 시간) | 약 30분 |
| 필요 자격증명 | 네이버 앱 비밀번호 | + 구글 서비스 계정 |
| 일정 수정/삭제 반영 | 자동 | 자동 (UID 매칭) |
| 구글 측 캘린더 | 읽기 전용 구독 캘린더 | 일반 보조 캘린더 |

모드 A는 기본 동작이며, `GOOGLE_SERVICE_ACCOUNT_JSON` 시크릿을 등록하면 모드 B가 추가로 실행됩니다.

## 구성

| 경로 | 역할 |
|---|---|
| `scripts/naver_to_ics.py` | 네이버 CalDAV → 단일 `docs/calendar.ics` 병합 |
| `scripts/ics_to_gcal.py` | ICS → 구글 보조 캘린더 upsert (생성·수정·삭제) |
| `.github/workflows/sync.yml` | 30분 주기 실행 + 수동 실행 |
| `docs/index.html` | Pages 안내 페이지 (구독 URL 표시·복사) |
| `.env.example` | 로컬 테스트용 환경변수 예시 |

## 필요한 Secrets

| 이름 | 필수 | 설명 |
|---|---|---|
| `NAVER_ID` | ✅ | 네이버 아이디(@ 앞부분) |
| `NAVER_APP_PASSWORD` | ✅ | 네이버 **애플리케이션 비밀번호** (로그인 비밀번호 아님) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 모드 B | 서비스 계정 JSON 전문 |
| `GOOGLE_CALENDAR_ID` | 모드 B | 대상 보조 캘린더 ID |

설치 절차는 [SETUP.md](SETUP.md) 참고.

## 알려진 제약

- GitHub Actions의 `schedule` 은 정시 보장이 아니며, 부하에 따라 수 분~수십 분 지연될 수 있습니다.
- 공개 저장소의 Pages는 URL을 아는 누구나 접근할 수 있습니다. 일정 제목이 민감하다면 **비공개 저장소 + 모드 B**만 사용하세요(이 경우 Pages 게시는 하지 않습니다).
- 반복 일정의 개별 예외(RECURRENCE-ID)는 모드 A에서는 그대로 전달되고, 모드 B에서는 마스터 규칙으로 대체됩니다.
- 네이버는 CalDAV 쓰기를 제한하므로 **읽기 전용 단방향**입니다. 구글에서 수정해도 네이버로 돌아가지 않습니다.

## 라이선스

MIT
