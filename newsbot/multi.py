"""여러 사용자용 실행: 수집·묶기는 한 번, 고르기·요약·발송은 사람마다.

비용 구조가 핵심이다.
  - 수집/묶기/본문 보강: 전체 1회  → 사용자가 늘어도 그대로
  - AI 요약: 1인당 1회            → 10명이면 하루 10회 (무료 한도 안)
한 사람이 실패해도 나머지는 계속 보낸다.
"""
from __future__ import annotations

import copy
from datetime import datetime

from . import glossary, kakao, users as users_mod, vocab
from .cluster import cluster_articles
from .collect import collect
from .config import env
from .enrich import enrich
from .models import Cluster
from .rank import shortlist
from .render import kakao_text, report_html
from .summarize import summarize_user
from .users import User

MAX_EXTRA_QUERIES = 12      # 전공 검색어는 너무 늘리지 않는다 (수집 시간·API 한도)


def expand_queries(cfg: dict, people: list[User]) -> dict:
    """사용자들이 적은 전공·키워드를 '전공' 카테고리의 검색어로 추가한 설정 사본."""
    terms: list[str] = []
    for u in people:
        for t in u.interest_terms():
            if t not in terms:
                terms.append(t)
    if not terms:
        return cfg
    cfg = copy.deepcopy(cfg)
    for cat in cfg["categories"]:
        if cat["id"] == "major":
            cat["naver"] = list(dict.fromkeys((cat.get("naver") or []) + terms[:MAX_EXTRA_QUERIES]))
            cat["google"] = list(dict.fromkeys((cat.get("google") or []) + terms[:MAX_EXTRA_QUERIES]))
            cat["keywords"] = list(dict.fromkeys((cat.get("keywords") or []) + terms))
    return cfg


def build_for(user: User, cfg: dict, clusters: list[Cluster], now: datetime) -> tuple[str, str, list[str], int, int]:
    """(리포트 HTML, 카톡 문구, 실은 이슈 제목들, 다음 용어 순번, 다음 토익 진도)"""
    interests = {t: 3.0 for t in user.interest_terms()}
    shortlists = shortlist(clusters, cfg, now, user.recent_titles, interests, only=user.categories)
    stories, error = summarize_user(cfg, shortlists, user)

    by_cat: dict[str, list] = {}
    for s in stories:
        by_cat.setdefault(s.category, []).append(s)

    terms, next_cursor = glossary.todays_terms(user.term_cursor, cfg["report"]["terms_per_day"])
    words, next_word_day = (None, user.word_day)
    if user.toeic:
        words, next_word_day = vocab.todays_vocab(user.word_day)

    stats = {"articles": 0, "clusters": len(clusters),
             "shortlisted": sum(len(v) for v in shortlists.values()), "enriched": 0,
             "errors": [f"요약: {error}"] if error else [], "sources": []}
    html = report_html(cfg, now, by_cat, terms, stats, words)
    text = kakao_text(cfg, now, by_cat, terms, words)
    return html, text, [s.lead_title for s in stories], next_cursor, next_word_day


def run_all(cfg: dict, force: bool = False) -> None:
    now = datetime.now(cfg["tz"])
    today = now.date()
    site = env("SITE_URL").rstrip("/")
    if not site:
        raise SystemExit("SITE_URL(가입·리포트 사이트 주소)이 필요합니다")

    people = users_mod.fetch_users(cfg["report"]["history_days"], today)
    todo = [u for u in people if force or u.last_sent != today.isoformat()]
    print(f"[1] 구독자 {len(people)}명 중 오늘 보낼 사람 {len(todo)}명", flush=True)
    if not todo:
        return

    cfg_expanded = expand_queries(cfg, todo)
    articles, _ = collect(cfg_expanded, now)
    clusters = cluster_articles(articles, [c["id"] for c in cfg_expanded["categories"]])
    print(f"[2] 기사 {len(articles)}건 → 이슈 {len(clusters)}개", flush=True)

    # 본문 보강은 "누군가의 후보"에 오른 이슈만, 전체에서 한 번만 한다
    candidates: dict[str, Cluster] = {}
    for u in todo:
        picks = shortlist(clusters, cfg_expanded, now, u.recent_titles,
                          {t: 3.0 for t in u.interest_terms()}, only=u.categories)
        for group in picks.values():
            for cl in group:
                candidates[cl.id] = cl
    n_body = enrich({"all": list(candidates.values())})
    print(f"[3] 후보 {len(candidates)}개 중 본문 확보 {n_body}개", flush=True)

    ok = 0
    for u in todo:
        try:
            html, text, titles, next_cursor, next_word_day = build_for(u, cfg_expanded, clusters, now)
            users_mod.save_report(u.id, today, html, titles, sent=False)
            new_token = kakao.send_text(text, f"{site}/r/{u.slug}", cfg["kakao"]["button_title"], u.refresh_token)
            if new_token:
                users_mod.save_refresh_token(u.id, new_token)
            users_mod.save_report(u.id, today, html, titles, sent=True)
            users_mod.save_progress(u.id, next_word_day, next_cursor, today)
            ok += 1
            print(f"  ✅ {u.label}: 이슈 {len(titles)}개 발송", flush=True)
        except Exception as exc:
            message = str(exc)[:200]
            print(f"  ❌ {u.label}: {message}", flush=True)
            try:
                users_mod.save_report(u.id, today, "", [], sent=False, error=message)
                # 토큰이 끊긴 사용자는 발송 대상에서 제외 (다시 로그인하면 되살아남)
                if "invalid_grant" in message or "KOE320" in message or "401" in message:
                    users_mod.deactivate(u.id)
                    print("     └ 카카오 연결이 끊겨 발송을 중단합니다 (재로그인 필요)", flush=True)
            except Exception:
                pass
    print(f"[4] 완료: {ok}/{len(todo)}명 발송", flush=True)
