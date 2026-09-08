#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
naver_to_ics.py
────────────────────────────────────────────────────────────────────────────
네이버 캘린더(CalDAV)의 모든 일정을 읽어 단일 .ics 파일로 병합 생성한다.

- 인증: 네이버 ID + 비밀번호(2단계 인증 사용 시 '애플리케이션 비밀번호')
- 접속: 네이버는 CalDAV 진입 경로를 공식 문서로 공개하지 않으므로
        알려진 후보 경로를 순서대로 시도하고, 성공한 경로를 로그로 남긴다.
- 출력: docs/calendar.ics  (GitHub Pages로 게시 → 구글 캘린더 'URL로 추가')
- 반복 일정(RRULE)은 원본 규칙을 그대로 보존한다.

환경변수
  NAVER_ID, NAVER_APP_PASSWORD, NAVER_CALDAV_URL(선택),
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

BASE = "https://caldav.calendar.naver.com"


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _candidate_urls(user: str) -> list[str]:
    """네이버 CalDAV 진입점 후보. 앞에서부터 순서대로 시도한다."""
    paths = [
        f"{BASE}/",                    # /.well-known/caldav 자동 탐색
        f"{BASE}/principals/",         # CalendarServer 계열 표준 배치
        f"{BASE}/principals/{user}/",
        f"{BASE}/principals/users/{user}/",
        f"{BASE}/calendars/{user}/",   # 캘린더 홈셋 직접 지정
        f"{BASE}/caldav/",
        f"{BASE}/caldav/{user}/",
    ]
    override = _env("NAVER_CALDAV_URL")
    if override:
        override = override if override.endswith("/") else override + "/"
        paths.insert(0, override)

    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _brief(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return (text[:110] + "…") if len(text) > 110 else text


def _try_url(url: str, user: str, pw: str):
    """한 후보 URL로 접속해 캘린더 목록을 얻어본다. 실패하면 (None, [])."""
    client = caldav.DAVClient(url=url, username=user, password=pw)

    try:
        cals = client.principal().calendars()
        if cals:
            LOG.info("  ✓ principal 탐색 성공 (%d개)", len(cals))
            return client, cals
        LOG.info("  · principal 은 응답했으나 캘린더 0개")
    except Exception as exc:  # noqa: BLE001
        LOG.info("  · principal() 실패: %s", _brief(exc))

    try:
        cals = caldav.CalendarSet(client=client, url=url).calendars()
        if cals:
            LOG.info("  ✓ 홈셋 직접 조회 성공 (%d개)", len(cals))
            return client, cals
        LOG.info("  · 홈셋은 응답했으나 캘린더 0개")
    except Exception as exc:  # noqa: BLE001
        LOG.info("  · 홈셋 조회 실패: %s", _brief(exc))

    return None, []


def connect_and_list():
    """사용자명·경로 조합을 순회하며 접속 가능한 조합을 찾는다."""
    raw_user = _env("NAVER_ID")
    pw = _env("NAVER_APP_PASSWORD")

    if not raw_user or not pw:
        LOG.error("NAVER_ID / NAVER_APP_PASSWORD 가 설정되지 않았습니다.")
        sys.exit(2)

    short = raw_user.split("@", 1)[0]
    usernames = [short, f"{short}@naver.com"]

    for user in usernames:
        for url in _candidate_urls(short):
            LOG.info("시도: %s (user=%s)", url, user)
            client, cals = _try_url(url, user, pw)
            if cals:
                LOG.info("접속 확정 → URL=%s, user=%s", url, user)
                return client, cals

    LOG.error(
        "모든 후보 경로에서 캘린더를 찾지 못했습니다.\n"
        "  1) 네이버 캘린더 > 설정 > 외부 캘린더 연동(CalDAV) 사용이 켜져 있는지\n"
        "  2) 2단계 인증 사용 중이라면 '애플리케이션 비밀번호'를 넣었는지\n"
        "  3) NAVER_ID 에 @naver.com 이 붙어 있지 않은지 확인하세요."
    )
    sys.exit(3)


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
            LOG.warning("[%s] date_search 실패(%s) → 전체 조회로 대체", name, _brief(exc))
            try:
                items = cal.events()
            except Exception as exc2:  # noqa: BLE001
                LOG.error("[%s] 조회 실패: %s", name, _brief(exc2))
                continue

        LOG.info("[%s] %d건 수신", name, len(items))

        for item in items:
            try:
                raw = item.data
                if not raw:
                    continue
                sub = Calendar.from_ical(raw)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("[%s] 파싱 실패: %s", name, _brief(exc))
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

    _client, cals = connect_and_list()
    LOG.info("캘린더 %d개 확보", len(cals))

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
