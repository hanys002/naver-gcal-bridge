# 설치 절차

## 1. 네이버 애플리케이션 비밀번호 발급

1. 네이버 로그인 → **내정보 → 보안설정**
2. **2단계 인증**을 켭니다. (앱 비밀번호는 2단계 인증 상태에서만 발급됩니다)
3. **애플리케이션 비밀번호 관리 → 비밀번호 생성**
   - 애플리케이션 이름: `naver-gcal-bridge`
   - 생성된 12자리 문자열을 복사 (재확인 불가 → 즉시 등록)
4. **네이버 캘린더 → 설정 → 캘린더 연동/외부 접근**에서 CalDAV 사용이 켜져 있는지 확인

> 이 비밀번호는 대화창이나 파일에 남기지 말고, GitHub Secrets 화면에 직접 입력하십시오.

## 2. GitHub 저장소 준비

1. 저장소 `naver-gcal-bridge` 생성 (Private 권장)
2. 이 저장소의 파일 12개 업로드 후 `main` 브랜치에 커밋
3. **Settings → Secrets and variables → Actions → New repository secret**

   | Name | Value |
   |---|---|
   | `NAVER_ID` | 네이버 아이디 (@ 앞부분) |
   | `NAVER_APP_PASSWORD` | 1단계에서 발급한 12자리 |

4. **Settings → Actions → General → Workflow permissions**
   → `Read and write permissions` 선택 후 저장
   (워크플로가 `docs/calendar.ics` 를 커밋해야 함)

## 3. GitHub Pages 활성화 (모드 A)

- **Settings → Pages**
  - Source: `Deploy from a branch`
  - Branch: `main` / 폴더 `/docs` → **Save**
- 1~2분 뒤 `https://<사용자>.github.io/naver-gcal-bridge/` 접속
- Private 저장소는 Pages에 유료 플랜이 필요합니다. Free 플랜이면 저장소를 Public으로 두거나 모드 B만 사용하십시오.

## 4. 첫 실행

- **Actions → Naver → Google Calendar Sync → Run workflow**
- 로그에서 `캘린더 N개 발견`, `생성 완료: docs/calendar.ics` 확인
- 실패 시 점검 순서
  1. `NAVER_ID` 에 `@naver.com` 이 붙어 있지 않은지
  2. 앱 비밀번호가 로그인 비밀번호로 잘못 들어가 있지 않은지
  3. `principal() 실패` 로그 → 네이버 CalDAV 사용 설정 확인

## 5. 구글 캘린더 연결

### 모드 A — URL 구독
1. 구글 캘린더 → 왼쪽 **다른 캘린더 +** → **URL로 추가**
2. `https://<사용자>.github.io/naver-gcal-bridge/calendar.ics` 입력 → 추가
3. 구글은 자체 주기로 재조회하므로 즉시 반영되지 않습니다.

### 모드 B — 서비스 계정 직접 반영 (지연 30분)
1. [Google Cloud Console](https://console.cloud.google.com/) → 프로젝트 생성
2. **API 및 서비스 → 라이브러리 → Google Calendar API → 사용 설정**
3. **사용자 인증 정보 → 서비스 계정 만들기** → 키 추가 → **JSON** 다운로드
4. 구글 캘린더에서 보조 캘린더 하나를 만들고
   **설정 → 특정 사용자와 공유 → 서비스 계정 이메일 추가**
   → 권한 **`변경 및 공유 관리`**
5. 해당 캘린더 설정 하단의 **캘린더 ID** 복사
6. GitHub Secrets에 추가
   | Name | Value |
   |---|---|
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON 파일 내용 전체 |
   | `GOOGLE_CALENDAR_ID` | 5단계의 캘린더 ID |
7. 워크플로를 다시 실행하면 `생성 N / 수정 N / 삭제 N` 로그가 남습니다.

## 6. 운영 점검

| 증상 | 원인 / 조치 |
|---|---|
| 일정이 0건 | 앱 비밀번호 오류, 또는 동기화 구간(`SYNC_PAST_DAYS`) 밖 |
| 커밋 실패 (403) | Workflow permissions 를 Read and write 로 변경 |
| 구글에 중복 생성 | `naverUid` 확장속성 없이 수동 추가한 일정 — 수동 삭제 |
| 스케줄이 안 돎 | 60일간 저장소 활동이 없으면 GitHub가 schedule 을 자동 중지. 아무 커밋이나 하면 재개 |
