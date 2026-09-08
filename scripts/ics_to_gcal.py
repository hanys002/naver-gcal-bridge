#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ics_to_gcal.py
────────────────────────────────────────────────────────────────────────────
docs/calendar.ics 의 일정을 구글 캘린더 '보조 캘린더'에 직접 upsert 한다. (모드 B)

- 인증: 구글 서비스 계정 (도메인 위임 불필요)
- 사전 준비: 구글 캘린더 설정에서 대상 보조 캘린더를
             서비스 계정 이메일과 '변경 및 공유 관리' 권한으로 공유
- 매칭 키: 네이버 UID → extendedProperties.private.naverUid
           (중복 생성 방지 및 변경·삭제 반영)

환경변수
  GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_CALENDAR_ID, TIMEZONE
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import date, datetime
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from icalendar import Calendar

LOG = logging.getLogger("ics_to_gcal")

ROOT = Path(__file__).resolve().parent.parent
ICS_PATH = ROOT / "docs" / "calendar.ics"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
PROP_KEY = "naverUid"


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def gcal_service():
    raw = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        LOG.info("GOOGLE_SERVICE_ACCOUNT_JSON 미설정 → 모드 B 건너뜀 (ICS 게시만 수행)")
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOG.error("서비스 계정 JSON 파싱 실패: %s", exc)
        sys.exit(2)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _dt_field(value, tzid: str) -> dict:
    """icalendar 값 → 구글 API date/dateTime 필드"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"dateTime": value.isoformat(), "timeZone": tzid}
        return {"dateTime": value.isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    return {}


def to_gcal_event(comp, tzid: str) -> dict | None:
    uid = str(comp.get("UID", "")).strip()
    dtstart = comp.get("DTSTART")
    if not uid or dtstart is None:
        return None

    start = _dt_field(dtstart.dt, tzid)
    dtend = comp.get("DTEND")
    if dtend is not None:
        end = _dt_field(dtend.dt, tzid)
    elif isinstance(dtstart.dt, datetime):
        end = start
    else:
        end = start

    body: dict = {
        "summary": str(comp.get("SUMMARY", "(제목 없음)")),
        "start": start,
        "end": end,
        "extendedProperties": {"private": {PROP_KEY: uid}},
        "source": {"title": "Naver Calendar", "url": "https://calendar.naver.com/"},
    }

    if comp.get("DESCRIPTION"):
        body["description"] = str(comp.get("DESCRIPTION"))
    if comp.get("LOCATION"):
        body["location"] = str(comp.get("LOCATION"))

    rrule = comp.get("RRULE")
    if rrule is not None:
        try:
            body["recurrence"] = ["RRULE:" + rrule.to_ical().decode()]
        except Exception:  # noqa: BLE001
            LOG.warning("RRULE 변환 실패(UID=%s) → 단일 일정으로 처리", uid)

    status = str(comp.get("STATUS", "")).upper()
    if status == "CANCELLED":
        body["status"] = "cancelled"
    return body


def load_source_events(tzid: str) -> dict[str, dict]:
    if not ICS_PATH.exists():
        LOG.error("%s 가 없습니다. naver_to_ics.py 를 먼저 실행하세요.", ICS_PATH)
        sys.exit(3)

    cal = Calendar.from_ical(ICS_PATH.read_bytes())
    out: dict[str, dict] = {}
    for comp in cal.walk("VEVENT"):
        # 반복 예외(RECURRENCE-ID)는 마스터 일정에 위임
        if comp.get("RECURRENCE-ID") is not None:
            continue
        body = to_gcal_event(comp, tzid)
        if body:
            out[body["extendedProperties"]["private"][PROP_KEY]] = body
    return out


def load_remote_events(svc, cal_id: str) -> dict[str, dict]:
    remote: dict[str, dict] = {}
    token = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=cal_id,
                privateExtendedProperty=f"{PROP_KEY}=*",
                showDeleted=False,
                singleEvents=False,
                maxResults=2500,
                pageToken=token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            uid = (
                ev.get("extendedProperties", {})
                .get("private", {})
                .get(PROP_KEY)
            )
            if uid:
                remote[uid] = ev
        token = resp.get("nextPageToken")
        if not token:
            break
    return remote


def _changed(new: dict, old: dict) -> bool:
    for key in ("summary", "description", "location"):
        if (new.get(key) or "") != (old.get(key) or ""):
            return True
    for key in ("start", "end"):
        a, b = new.get(key, {}), old.get(key, {})
        if a.get("date") != b.get("date"):
            return True
        if (a.get("dateTime") or "")[:19] != (b.get("dateTime") or "")[:19]:
            return True
    if new.get("recurrence", []) != old.get("recurrence", []):
        return True
    return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    svc = gcal_service()
    if svc is None:
        return 0

    cal_id = _env("GOOGLE_CALENDAR_ID")
    if not cal_id:
        LOG.error("GOOGLE_CALENDAR_ID 가 설정되지 않았습니다.")
        return 2

    tzid = _env("TIMEZONE", "Asia/Seoul")
    source = load_source_events(tzid)
    remote = load_remote_events(svc, cal_id)
    LOG.info("원본 %d건 / 구글 측 기존 %d건", len(source), len(remote))

    created = updated = deleted = skipped = 0

    for uid, body in source.items():
        try:
            if uid in remote:
                old = remote[uid]
                if _changed(body, old):
                    svc.events().update(
                        calendarId=cal_id, eventId=old["id"], body=body
                    ).execute()
                    updated += 1
                else:
                    skipped += 1
            else:
                svc.events().insert(calendarId=cal_id, body=body).execute()
                created += 1
        except HttpError as exc:
            LOG.error("upsert 실패(UID=%s): %s", uid, exc)

    for uid, ev in remote.items():
        if uid in source:
            continue
        try:
            svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
            deleted += 1
        except HttpError as exc:
            if exc.resp.status not in (404, 410):
                LOG.error("삭제 실패(UID=%s): %s", uid, exc)

    LOG.info(
        "완료 — 생성 %d / 수정 %d / 삭제 %d / 변경없음 %d",
        created, updated, deleted, skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
