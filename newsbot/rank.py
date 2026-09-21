"""3단계 · 관련도 필터링: 이슈마다 점수를 매겨 카테고리별 후보(shortlist)를 고른다.

점수 = 관련도 × w_rel + 중요도 × w_imp + 신선도 − 재방송 패널티
  - 관련도 : 내 관심 키워드가 제목/설명에 얼마나 등장하나 (config.profile.interests)
  - 중요도 : 몇 개 언론이 같은 사건을 다뤘나 (클러스터 크기 → 놓치면 안 되는 뉴스)
  - 신선도 : 최근일수록 가산
  - 패널티 : 최근 N일 안에 이미 보낸 이슈와 비슷하면 감점/제외
카테고리마다 w_rel / w_imp 비율이 달라서 "시사=모두가 다루는 뉴스", "전공=나와 맞는 뉴스"가 된다.
"""
from __future__ import annotations

import math
from datetime import datetime

from .cluster import similarity
from .collect import keyword_pattern
from .models import Cluster


def relevance(cluster: Cluster, patterns: list[tuple]) -> float:
    score = 0.0
    texts = [(a.title, a.summary) for a in cluster.articles[:6]]
    for pat, weight in patterns:
        if any(pat.search(t) for t, _ in texts):
            score += weight * 1.0
        elif any(pat.search(s) for _, s in texts):
            score += weight * 0.5
    return min(score, 8.0)


def importance(cluster: Cluster) -> float:
    # 서로 다른 매체 수 기준, 로그로 완만하게 (1곳=0, 2곳=1.6, 4곳=3.2, 8곳=4.8)
    return 1.6 * math.log2(len(cluster.sources))


def freshness(cluster: Cluster, now: datetime) -> float:
    latest = cluster.latest
    if not latest:
        return 0.0
    hours = max(0.0, (now - latest).total_seconds() / 3600)
    return max(0.0, 1.5 - hours / 16)       # 방금=1.5, 24시간 전=0


def shortlist(clusters: list[Cluster], cfg: dict, now: datetime, recent_titles: list[str],
              interests_map: dict[str, float] | None = None,
              only: list[str] | None = None) -> dict[str, list[Cluster]]:
    """interests_map / only 를 주면 그 사용자 기준으로 후보를 고른다 (없으면 config 기본값)."""
    interests = [(keyword_pattern(k), w) for k, w in (interests_map or cfg["profile"]["interests"]).items()]
    size = cfg["report"]["shortlist_per_category"]
    weights = {c["id"]: c["weights"] for c in cfg["categories"]}
    # 관련도 = 내 관심사 + 그 카테고리다운 주제인가 (인문 카테고리에선 '철학·역사'가 가산)
    patterns = {c["id"]: interests + [(keyword_pattern(k), 1.5) for k in c.get("keywords") or []]
                for c in cfg["categories"]}

    for cl in clusters:
        w = weights.get(cl.category, {"relevance": 1, "importance": 1})
        cl.relevance = relevance(cl, patterns.get(cl.category, interests))
        cl.importance = importance(cl)
        cl.score = cl.relevance * w["relevance"] + cl.importance * w["importance"] + freshness(cl, now)
        # 이미 보낸 이슈: 거의 같으면 제외, 비슷하면(후속 보도) 절반만 인정
        dup = max((similarity(cl.lead.title, t) for t in recent_titles), default=0.0)
        if dup >= 0.55:
            cl.score = -1
        elif dup >= 0.35:
            cl.score *= 0.5

    result: dict[str, list[Cluster]] = {}
    for cat in cfg["categories"]:
        if only is not None and cat["id"] not in only:
            continue
        pool = [c for c in clusters if c.category == cat["id"] and c.score > 0]
        pool.sort(key=lambda c: c.score, reverse=True)
        result[cat["id"]] = pool[:size]
    return result
