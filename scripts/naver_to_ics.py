#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
naver_to_ics.py
────────────────────────────────────────────────────────────────────────────
네이버 캘린더(CalDAV)의 모든 일정을 읽어 단일 .ics 파일로 병합 생성한다.

- 인증: 네이버 ID + '애플리케이션 비밀번호' (2단계 인증 사용 시 필수)
- 출력: docs/calendar.ics  (GitHub Pages로 게시 → 구글 캘린더 'URL로 추가')
- 반복 일정(RRULE)은 원본 규칙을 그대로 보존한다.

환경변수
  NAVER_ID, NAVER_APP_PASSWORD, NAVER_CALDAV_URL,
  SYNC_PAST_DAYS, SYNC_FUTURE_DAYS, CALENDAR_NAME, TIMEZONE
"""
from __future__ import annotations

import os
import sys
import uuid
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import caldav
from caldav.elements import dav
from icalendar import Calendar

LOG = logging.getLogger("naver_to_ics")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "calendar.ics"

DEFAULT_CALDAV = "https://caldav.calendar.naver.com/caldav/"


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def connect() -> caldav.DAVClient:
    user = _env("NAVER_ID")
    pw = _env("NAVER_APP_PASSWORD")
    url = _env("NAVER_CALDAV_URL", DEFAULT_CALDAV)

    if not user or not pw:
        LOG.error("NAVER_ID / NAVER_APP_PASSWORD 가 설정되지 않았습니다.")
        sys.exit(2)

    # 네이버는 아이디에 @naver.com 이 붙어도, 안 붙어도 동작하는 경우가 있어
    # 앞부분만 사용하는 것을 기본으로 한다.
    if "@" in user:
        user = user.split("@", 1)[0]

    LOG.info("CalDAV 접속: %s (user=%s)", url, user)
    return caldav.DAVClient(url=url, username=user, password=pw)


def fetch_calendars(client: caldav.DAVClient):
    """principal 탐색이 실패하는 서버를 위해 단계적으로 폴백한다."""
    try:
        principal = client.principal()
        cals = principal.calendars()
        if cals:
            return cals
        LOG.warning("principal 에서 캘린더를 찾지 못했습니다. 홈셋 직접 탐색 시도.")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("principal() 실패(%s). 홈셋 직접 탐색 시도.", exc)

    user = _env("NAVER_ID").split("@", 1)[0]
    base = _env("NAVER_CALDAV_URL", DEFAULT_CALDAV).rstrip("/")
    guess = f"{base}/{user}/"
    LOG.info("홈셋 추정 경로: %s", guess)
    home = caldav.CalendarSet(client=client, url=guess)
    return home.calendars()


def collect_components(cals, start: datetime, end: datetime):
    """각 캘린더에서 VEVENT/VTODO 컴포넌트와 VTIMEZONE 을 수집한다."""
    events, tzids, seen = [], {}, set()

    for cal in cals:
        try:
            name = cal.get_properties([dav.DisplayName()]).get(
                "{DAV:}displayname", str(cal.url)
            )
        except Exception:  # noqa: BLE001
            name = str(cal.url)

        try:
            items = cal.date_search(start=start, end=end, expand=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("[%s] date_search 실패(%s) → 전체 조회로 대체", name, exc)
            try:
                items = cal.events()
            except Exception as exc2:  # noqa: BLE001
                LOG.error("[%s] 조회 실패: %s", name, exc2)
                continue

        LOG.info("[%s] %d건 수신", name, len(items))

        for item in items:
            try:
                raw = item.data
                if not raw:
                    continue
                sub = Calendar.from_ical(raw)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("[%s] 파싱 실패: %s", name, exc)
                continue

            for comp in sub.walk():
                kind = comp.name
                if kind == "VTIMEZONE":
                    tzid = str(comp.get("TZID", ""))
                    if tzid and tzid not in tzids:
                        tzids[tzid] = comp
                    continue
                if kind not in ("VEVENT", "VTODO"):
                    continue

                uid = str(comp.get("UID", "")) or f"{uuid.uuid4()}@naver-gcal-bridge"
                rid = str(comp.get("RECURRENCE-ID", ""))
                key = (uid, rid)
                if key in seen:
                    continue
                seen.add(key)

                if not comp.get("UID"):
                    comp.add("UID", uid)
                # 캘린더 구분이 보이도록 소스명을 주석 필드에 남긴다.
                if name and not comp.get("X-NAVER-CALENDAR"):
                    comp.add("X-NAVER-CALENDAR", str(name))
                events.append(comp)

    return events, tzids


def build_ics(events, tzids) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//naver-gcal-bridge//KR")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", _env("CALENDAR_NAME", "Naver Calendar"))
    cal.add("x-wr-timezone", _env("TIMEZONE", "Asia/Seoul"))
    # 구글이 재조회하는 최소 주기 힌트
    cal.add("x-published-ttl", "PT1H")
    cal.add("refresh-interval;value=duration", "PT1H")

    for comp in tzids.values():
        cal.add_component(comp)
    for comp in events:
        cal.add_component(comp)

    return cal.to_ical()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )

    past = _int_env("SYNC_PAST_DAYS", 30)
    future = _int_env("SYNC_FUTURE_DAYS", 365)
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(days=past), now + timedelta(days=future)
    LOG.info("동기화 구간: %s ~ %s", start.date(), end.date())

    client = connect()
    cals = fetch_calendars(client)
    if not cals:
        LOG.error("접근 가능한 캘린더가 없습니다. 아이디/애플리케이션 비밀번호를 확인하세요.")
        return 3
    LOG.info("캘린더 %d개 발견", len(cals))

    events, tzids = collect_components(cals, start, end)
    if not events:
        LOG.warning("수집된 일정이 0건입니다. 빈 캘린더를 생성합니다.")

    data = build_ics(events, tzids)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(data)
    LOG.info("생성 완료: %s (%d건, %d bytes)", OUT_PATH, len(events), len(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
