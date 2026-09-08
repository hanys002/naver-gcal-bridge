#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
naver_to_ics.py
────────────────────────────────────────────────────────────────────────────
네이버 캘린더(CalDAV)의 모든 일정을 읽어 단일 .ics 파일로 병합 생성한다.

접속
  - 진입 경로를 공개하지 않으므로 후보 URL을 순서대로 시도한다.
    (실측: https://caldav.calendar.naver.com/ 루트에서 principal 탐색 성공)

일정 조회
  - 네이버는 CalDAV REPORT(calendar-query)에 비-XML 응답을 돌려주는 경우가 있어
    caldav 라이브러리가 XMLSyntaxError 로 실패한다.
  - 따라서 1차로 라이브러리 조회를 시도하고, 실패하면
    PROPFIND(Depth:1) 로 .ics 리소스 목록을 얻어 개별 GET 하는 표준 WebDAV 경로로 우회한다.

환경변수
  NAVER_ID, NAVER_APP_PASSWORD, NAVER_CALDAV_URL(선택),
  SYNC_PAST_DAYS, SYNC_FUTURE_DAYS, CALENDAR_NAME, TIMEZONE
"""
from __future__ import annotations

import os
import sys
import uuid
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import caldav
import requests
from caldav.elements import dav
from icalendar import Calendar

LOG = logging.getLogger("naver_to_ics")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "calendar.ics"

BASE = "https://caldav.calendar.naver.com"
MAX_OBJECTS_PER_CAL = 3000

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:getcontenttype/><d:resourcetype/><d:getetag/>"
    "</d:prop></d:propfind>"
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _int_env(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _brief(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return (text[:110] + "…") if len(text) > 110 else text


# ── 접속 ────────────────────────────────────────────────────────────────────

def _candidate_urls(user: str) -> list[str]:
    paths = [
        f"{BASE}/",
        f"{BASE}/principals/",
        f"{BASE}/principals/{user}/",
        f"{BASE}/calendars/{user}/",
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


def _try_url(url: str, user: str, pw: str):
    client = caldav.DAVClient(url=url, username=user, password=pw)
    try:
        cals = client.principal().calendars()
        if cals:
            LOG.info("  ✓ principal 탐색 성공 (%d개)", len(cals))
            return client, cals
    except Exception as exc:  # noqa: BLE001
        LOG.info("  · principal() 실패: %s", _brief(exc))

    try:
        cals = caldav.CalendarSet(client=client, url=url).calendars()
        if cals:
            LOG.info("  ✓ 홈셋 직접 조회 성공 (%d개)", len(cals))
            return client, cals
    except Exception as exc:  # noqa: BLE001
        LOG.info("  · 홈셋 조회 실패: %s", _brief(exc))

    return None, []


def connect_and_list():
    raw_user = _env("NAVER_ID")
    pw = _env("NAVER_APP_PASSWORD")
    if not raw_user or not pw:
        LOG.error("NAVER_ID / NAVER_APP_PASSWORD 가 설정되지 않았습니다.")
        sys.exit(2)

    short = raw_user.split("@", 1)[0]

    for user in (short, f"{short}@naver.com"):
        for url in _candidate_urls(short):
            LOG.info("시도: %s (user=%s)", url, user)
            client, cals = _try_url(url, user, pw)
            if cals:
                LOG.info("접속 확정 → %s", url)
                return client, cals, user, pw

    LOG.error(
        "모든 후보 경로에서 캘린더를 찾지 못했습니다.\n"
        "  1) 네이버 캘린더의 외부 연동(CalDAV) 사용 여부\n"
        "  2) 2단계 인증 사용 시 '애플리케이션 비밀번호' 사용 여부\n"
        "  3) NAVER_ID 에 @naver.com 이 붙어 있지 않은지 확인하세요."
    )
    sys.exit(3)


# ── 일정 조회 ────────────────────────────────────────────────────────────────

def _cal_name(cal) -> str:
    try:
        return str(
            cal.get_properties([dav.DisplayName()]).get(
                "{DAV:}displayname", str(cal.url)
            )
        )
    except Exception:  # noqa: BLE001
        return str(cal.url)


def _make_session(user: str, pw: str) -> requests.Session:
    sess = requests.Session()
    sess.auth = (user, pw)
    sess.headers.update({"User-Agent": "naver-gcal-bridge/1.0"})
    return sess


def _list_ics_hrefs(sess: requests.Session, cal_url: str) -> list[str]:
    """PROPFIND Depth:1 로 캘린더 아래 .ics 리소스 목록을 얻는다."""
    resp = sess.request(
        "PROPFIND",
        cal_url,
        headers={"Depth": "1", "Content-Type": 'application/xml; charset="utf-8"'},
        data=PROPFIND_BODY.encode("utf-8"),
        timeout=30,
    )
    if resp.status_code not in (207, 200):
        raise RuntimeError(f"PROPFIND {resp.status_code}")

    root = ET.fromstring(resp.content)
    self_path = urlparse(cal_url).path.rstrip("/")
    hrefs: list[str] = []

    for node in root.findall("{DAV:}response"):
        href = (node.findtext("{DAV:}href") or "").strip()
        if not href:
            continue
        if urlparse(href).path.rstrip("/") == self_path:
            continue  # 컬렉션 자기 자신
        ctype = " ".join(
            (el.text or "") for el in node.iter("{DAV:}getcontenttype")
        ).lower()
        if "calendar" in ctype or href.lower().endswith(".ics"):
            hrefs.append(href)

    if not hrefs:
        LOG.info(
            "  PROPFIND status=%s body=%dB responses=%d sample=%s",
            resp.status_code,
            len(resp.content),
            len(root.findall("{DAV:}response")),
            resp.text[:200].replace("\n", " "),
        )

    return hrefs[:MAX_OBJECTS_PER_CAL]


def _fetch_ics(sess: requests.Session, base_url: str, href: str) -> str | None:
    url = urljoin(base_url, href)
    try:
        resp = sess.get(url, timeout=30)
        if resp.status_code != 200:
            return None
        resp.encoding = resp.encoding or "utf-8"
        return resp.text
    except Exception:  # noqa: BLE001
        return None


def _raw_items(sess: requests.Session, cal_url: str) -> list[str]:
    hrefs = _list_ics_hrefs(sess, cal_url)
    out = []
    for href in hrefs:
        text = _fetch_ics(sess, cal_url, href)
        if text and "BEGIN:VCALENDAR" in text:
            out.append(text)
    return out


def _as_utc(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def _in_range(comp, start: datetime, end: datetime) -> bool:
    if comp.get("RRULE") is not None or comp.get("RDATE") is not None:
        return True  # 반복 일정은 규칙째 보존
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return True
    moment = _as_utc(dtstart.dt)
    if moment is None:
        return True
    return start <= moment <= end


def collect_components(cals, start: datetime, end: datetime, user: str, pw: str):
    events, tzids, seen = [], {}, set()
    sess = _make_session(user, pw)

    for cal in cals:
        name = _cal_name(cal)
        cal_url = str(cal.url)
        raws: list[str] = []

        # 라이브러리 조회와 PROPFIND 조회를 모두 수행하고 UID 로 중복 제거한다.
        try:
            lib = [o.data for o in cal.date_search(start=start, end=end, expand=False)]
        except Exception as exc:  # noqa: BLE001
            lib = []
            LOG.info("[%s] lib 예외: %s", name, _brief(exc))
        try:
            alt = _raw_items(sess, cal_url)
        except Exception as exc:  # noqa: BLE001
            alt = []
            LOG.info("[%s] dav 예외: %s", name, _brief(exc))
        LOG.info("[%s] lib=%d dav=%d", name, len(lib), len(alt))
        raws = list(lib) + list(alt)

        for raw in raws:
            if not raw:
                continue
            try:
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
                if kind == "VEVENT" and not _in_range(comp, start, end):
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
                    comp.add("X-NAVER-CALENDAR", name)
                events.append(comp)

    return events, tzids


# ── 출력 ────────────────────────────────────────────────────────────────────

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

    _client, cals, user, pw = connect_and_list()
    LOG.info("캘린더 %d개 확보", len(cals))

    events, tzids = collect_components(cals, start, end, user, pw)
    if not events:
        LOG.warning("수집된 일정이 0건입니다. 빈 캘린더를 생성합니다.")

    data = build_ics(events, tzids)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_bytes(data)
    LOG.info("생성 완료: %s (%d건, %d bytes)", OUT_PATH, len(events), len(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())

      
        
      
      Stop Claude
    원본 텍스트번역 평가보내주신 의견은 Google 번역을 개선하는 데 사용됩니다.
