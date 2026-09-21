"""Supabase에서 구독자와 설정을 읽고, 진도·리포트를 저장한다.

Supabase REST(PostgREST)를 그대로 호출한다 (의존성 추가 없이 requests만 사용).
service_role 키를 쓰므로 이 코드는 서버(GitHub Actions)에서만 돌아야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import requests

from .config import env

TIMEOUT = 20


@dataclass
class User:
    id: str
    nickname: str
    refresh_token: str
    slug: str
    categories: list[str]
    major: str
    keywords: list[str]
    persona: str
    toeic: bool
    word_day: int
    term_cursor: int
    last_sent: str | None
    recent_titles: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.nickname or self.id[:8]

    def interest_terms(self) -> list[str]:
        """전공·키워드를 검색어와 관련도 판정에 쓸 단어로 쪼갠다."""
        parts = [p.strip() for p in self.major.replace("·", ",").replace("/", ",").split(",")]
        return [p for p in parts + [k.strip() for k in self.keywords] if len(p) >= 2]


def _headers() -> dict:
    key = env("SUPABASE_SERVICE_KEY")
    if not env("SUPABASE_URL") or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY 가 설정되지 않았습니다")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _url(path: str) -> str:
    return f"{env('SUPABASE_URL').rstrip('/')}/rest/v1/{path}"


def fetch_users(history_days: int, today: date) -> list[User]:
    resp = requests.get(
        _url("users"),
        params={"select": "id,nickname,refresh_token,slug,prefs(*),progress(*)", "active": "eq.true"},
        headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()

    users = []
    for row in resp.json():
        prefs = row.get("prefs") or {}
        prog = row.get("progress") or {}
        users.append(User(
            id=row["id"], nickname=row.get("nickname") or "", refresh_token=row["refresh_token"], slug=row["slug"],
            categories=prefs.get("categories") or ["current"], major=prefs.get("major") or "",
            keywords=prefs.get("keywords") or [], persona=prefs.get("persona") or "jobseeker",
            toeic=bool(prefs.get("toeic", True)), word_day=prog.get("word_day") or 0,
            term_cursor=prog.get("term_cursor") or 0, last_sent=prog.get("last_sent"),
        ))

    # 최근 N일 안에 보낸 이슈 제목 (같은 뉴스를 다시 보내지 않으려고)
    if users:
        since = date.fromordinal(today.toordinal() - history_days).isoformat()
        r = requests.get(_url("reports"), params={"select": "user_id,titles", "date": f"gte.{since}"},
                         headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        by_user: dict[str, list[str]] = {}
        for row in r.json():
            by_user.setdefault(row["user_id"], []).extend(row.get("titles") or [])
        for u in users:
            u.recent_titles = by_user.get(u.id, [])
    return users


def save_report(user_id: str, day: date, html: str, titles: list[str], sent: bool, error: str = "") -> None:
    requests.post(
        _url("reports"), headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
        json={"user_id": user_id, "date": day.isoformat(), "html": html, "titles": titles,
              "sent_at": f"{day.isoformat()}T00:00:00Z" if sent else None, "error": error or None},
        timeout=TIMEOUT).raise_for_status()


def save_progress(user_id: str, word_day: int, term_cursor: int, last_sent: date) -> None:
    requests.post(
        _url("progress"), headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
        json={"user_id": user_id, "word_day": word_day, "term_cursor": term_cursor,
              "last_sent": last_sent.isoformat()},
        timeout=TIMEOUT).raise_for_status()


def save_refresh_token(user_id: str, token: str) -> None:
    requests.patch(_url("users"), params={"id": f"eq.{user_id}"}, headers=_headers(),
                   json={"refresh_token": token}, timeout=TIMEOUT).raise_for_status()


def deactivate(user_id: str) -> None:
    """토큰이 만료·해지된 사용자는 발송 대상에서 빼고, 다시 로그인하면 되살아난다."""
    requests.patch(_url("users"), params={"id": f"eq.{user_id}"}, headers=_headers(),
                   json={"active": False}, timeout=TIMEOUT).raise_for_status()
