"""4단계 · 요약: 카테고리별 후보를 Gemini에게 주고 "고르기 + 구조화 요약"을 한 번에 맡긴다.

규칙 점수(3단계)는 빠르지만 보도자료·광고성 기사를 못 거른다. 마지막 선택은
맥락을 읽을 수 있는 LLM이 하고, 규칙은 후보를 좁히는 역할만 한다.
Gemini API 무료 등급을 쓴다 (하루 5회 호출이라 무료 한도 안).
카테고리별 호출이 서로 독립이라 병렬로 돌리고, 하나가 실패해도 그 카테고리만
"제목+링크"로 대체된다.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from .config import env
from .models import Cluster, Story

SYSTEM = """당신은 한 사람만을 위한 아침 뉴스 브리핑 에디터입니다.

독자: {about}

할 일: 한 카테고리의 후보 이슈 목록을 받아, 독자가 오늘 알아야 할 이슈를 최대 {pick}개 고르고 구조화 요약을 씁니다.

고르는 기준
- 실질적인 정보가 있는 뉴스를 고릅니다. 보도자료성 행사 소식, 단순 수상·협약·홍보, 광고성 기사는 고르지 않습니다.
- 여러 매체가 다룬 이슈는 무게가 있다는 신호지만, 홍보 기사가 여러 곳에 뿌려진 경우와 구별하세요.
- 서로 같은 사건을 다루는 후보는 하나만 고릅니다.
- 가치 있는 후보가 {pick}개보다 적으면 적게 고르세요. 억지로 채우지 않습니다.

쓰는 규칙 (모두 한국어, 영어 기사도 한국어로)
- headline: 사건의 핵심을 담은 제목, 40자 이내. 원문 제목을 그대로 베끼지 말고 새로 씁니다.
- short: 카카오톡 미리보기용 초압축 제목, 18자 이내.
- what: 무슨 일이 있었는지 한 문장, "~했다" 체.
- points: 핵심 포인트 2~3개. 각 50자 이내의 명사형 종결("~함", "~음", "~ 전망")로 통일. 숫자·고유명사·원인과 결과를 담습니다. 여러 매체가 다르게 강조하면 그 차이도 포인트로 씁니다.
- why: 한두 문장, 100자 이내. 반드시 "~다"로 끝나는 평서문("~입니다" 금지). 관점: {why}
  "중요하다", "주목할 만하다" 같은 일반론은 쓰지 않습니다.
- 주어진 텍스트에 있는 사실만 씁니다. 추측으로 수치나 사실을 만들지 않습니다.
- 원문 문장을 인용하지 말고 자신의 말로 요약합니다."""


RETRY_ELSEWHERE = {429, 500, 503}   # 다른 모델로 넘어가 볼 만한 오류


def model_chain(cfg: dict) -> list[str]:
    """기본 모델 → 예비 모델 순서. 한도 초과·과부하 때 차례로 시도한다."""
    chain = [cfg["llm"]["model"]]
    if cfg["llm"].get("fallback_model"):
        chain.append(cfg["llm"]["fallback_model"])
    return chain


# 카테고리에 why가 없을 때: 전공에 억지로 연결하지 않는 교양 관점
DEFAULT_WHY = ("세상을 이해하는 데 왜 알아둘 만한지(배경, 파장, 논쟁점). "
               "독자의 전공(SCM·산업공학)에 억지로 연결하지 않는다.")


class Pick(BaseModel):
    candidate: int = Field(description="고른 후보의 번호")
    headline: str
    short: str
    what: str
    points: list[str]
    why: str


class Picks(BaseModel):
    picks: list[Pick]


def _candidate_text(i: int, cl: Cluster, tz) -> str:
    lead = cl.lead
    when = cl.latest.astimezone(tz).strftime("%m/%d %H:%M") if cl.latest else "시각 미상"
    content = lead.body or max((a.summary for a in cl.articles), key=len) or "(본문 없음 — 제목만 참고)"
    others = [a.title for a in cl.articles if a.title != lead.title][:3]
    lines = [
        f"[{i}] {lead.title}",
        f"매체 {len(cl.sources)}곳: {', '.join(cl.sources[:6])} · {when}",
        f"내용: {content}",
    ]
    if others:
        lines.append("다른 매체 제목: " + " / ".join(others))
    return "\n".join(lines)


def summarize_category(client: genai.Client, cfg: dict, cat: dict, clusters: list[Cluster]) -> list[Story]:
    """기본 모델의 무료 한도(하루 호출 수)가 차면 한도가 따로인 예비 모델로 한 번 더 시도한다."""
    for i, model in enumerate(model_chain(cfg)):
        try:
            return _summarize_with(client, model, cfg, cat, clusters)
        except genai_errors.APIError as exc:
            # 429 한도 초과, 503 과부하 → 예비 모델로 한 번 더
            if exc.code not in RETRY_ELSEWHERE or i == len(model_chain(cfg)) - 1:
                raise


def _summarize_with(client: genai.Client, model: str, cfg: dict, cat: dict, clusters: list[Cluster]) -> list[Story]:
    candidates = "\n\n".join(_candidate_text(i, cl, cfg["tz"]) for i, cl in enumerate(clusters, 1))
    response = client.models.generate_content(
        model=model,
        contents=f"카테고리: {cat['name']}\n\n후보 이슈:\n\n{candidates}",
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM.format(about=cfg["profile"]["about"].strip(), pick=cat["pick"],
                                             why=cat.get("why", DEFAULT_WHY).strip()),
            response_mime_type="application/json",
            response_schema=Picks,     # 이 구조의 JSON만 돌려받는다
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    parsed = response.parsed
    if not isinstance(parsed, Picks):
        reason = response.candidates[0].finish_reason if response.candidates else "응답 없음"
        raise RuntimeError(f"요약 실패 ({reason})")

    stories = []
    for p in parsed.picks[: cat["pick"]]:
        if not 1 <= p.candidate <= len(clusters):
            continue
        cl = clusters[p.candidate - 1]
        stories.append(Story(category=cat["id"], headline=p.headline, short=p.short, what=p.what,
                             points=p.points[:3], why=p.why, url=cl.lead.url, sources=cl.sources,
                             lead_title=cl.lead.title))
    return stories


def fallback(cat: dict, clusters: list[Cluster]) -> list[Story]:
    """요약이 실패한 카테고리는 점수 상위 이슈를 제목+링크로만 보여준다."""
    return [Story(category=cat["id"], headline=cl.lead.title, short=cl.lead.title[:18], what="", points=[],
                  why="", url=cl.lead.url, sources=cl.sources, lead_title=cl.lead.title, summarized=False)
            for cl in clusters[: cat["pick"]]]


def summarize(cfg: dict, shortlists: dict[str, list[Cluster]]) -> tuple[dict[str, list[Story]], list[str]]:
    cats = [c for c in cfg["categories"] if shortlists.get(c["id"])]
    errors: list[str] = []
    if not env("GEMINI_API_KEY"):
        errors.append("GEMINI_API_KEY 없음 → 요약 없이 제목만")
        return {c["id"]: fallback(c, shortlists[c["id"]]) for c in cats}, errors

    # 무료 등급은 분당 호출 수 제한이 있어 429(한도 초과)·5xx는 기다렸다가 재시도
    client = genai.Client(api_key=env("GEMINI_API_KEY"), http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=4, initial_delay=8, max_delay=45,
                                             http_status_codes=[429, 500, 502, 503, 504])))

    def run(cat):
        try:
            return cat["id"], summarize_category(client, cfg, cat, shortlists[cat["id"]])
        except Exception as exc:
            errors.append(f"{cat['name']}: {exc}")
            return cat["id"], fallback(cat, shortlists[cat["id"]])

    with ThreadPoolExecutor(max_workers=2) as pool:   # 무료 한도를 고려해 동시 2개만
        results = dict(pool.map(run, cats))
    return results, errors


# ── 여러 사용자용: 1인당 1회 호출로 전체 리포트 요약 ──────────────

PERSONAS = {
    "jobseeker": "취업을 준비 중입니다. 지원 직무·기업 준비, 면접에서 쓸 수 있는 관점으로 연결하세요.",
    "worker": "현업에서 일하고 있습니다. 실무 판단과 업무에 어떤 의미인지로 연결하세요.",
    "student": "학생입니다. 전공 수업에서 배우는 개념의 실제 사례로 연결하세요.",
}

SYSTEM_USER = """당신은 한 사람만을 위한 아침 뉴스 브리핑 에디터입니다.

독자
- 전공·관심 분야: {major}
- 상황: {persona}
- 관심 키워드: {keywords}

할 일: 카테고리별 후보 이슈 목록을 받아, 카테고리마다 정해진 개수 이내로 이슈를 고르고 구조화 요약을 씁니다.

고르는 기준
- 실질적인 정보가 있는 뉴스를 고릅니다. 보도자료성 행사 소식, 단순 수상·협약·홍보, 광고성 기사는 고르지 않습니다.
- 여러 매체가 다룬 이슈는 무게가 있다는 신호지만, 홍보 기사가 여러 곳에 뿌려진 경우와 구별하세요.
- 같은 사건을 다루는 후보는 카테고리가 달라도 하나만 고릅니다.
- 가치 있는 후보가 정해진 개수보다 적으면 적게 고르세요. 억지로 채우지 않습니다.

쓰는 규칙 (모두 한국어, 영어 기사도 한국어로)
- category: 그 후보가 속한 카테고리 id를 그대로 적습니다.
- headline: 사건의 핵심을 담은 제목, 40자 이내. 원문 제목을 그대로 베끼지 말고 새로 씁니다.
- short: 카카오톡 미리보기용 초압축 제목, 18자 이내.
- what: 무슨 일이 있었는지 한 문장, "~했다" 체.
- points: 핵심 포인트 2~3개. 각 50자 이내의 명사형 종결("~함", "~음", "~ 전망")로 통일. 숫자·고유명사·원인과 결과를 담습니다.
- why: 한두 문장, 100자 이내. 반드시 "~다"로 끝나는 평서문("~입니다" 금지). 각 카테고리에 적힌 관점을 따릅니다.
  "중요하다", "주목할 만하다" 같은 일반론은 쓰지 않습니다.
- 주어진 텍스트에 있는 사실만 씁니다. 추측으로 수치나 사실을 만들지 않습니다.
- 원문 문장을 인용하지 말고 자신의 말로 요약합니다."""


class UserPick(Pick):
    category: str = Field(description="후보가 속한 카테고리 id")


class UserPicks(BaseModel):
    picks: list[UserPick]


def why_for(cat: dict, user) -> str:
    """'왜 중요한가' 관점. 전공·취업은 그 사용자의 전공을 기준으로 바꾼다
    (config.yaml의 문구는 운영자 본인 기준이라 그대로 쓰면 안 된다)."""
    major = user.major.strip()
    if cat["id"] == "major":
        return (f"이 뉴스가 독자의 전공·관심 분야({major})에서 배우는 개념의 실제 사례인지, "
                f"또는 그 분야 실무에서 어떤 판단으로 이어지는지." if major else DEFAULT_WHY)
    if cat["id"] == "jobs":
        field = f"{major} 분야 " if major else ""
        return f"{field}지원자의 취업 준비에 어떤 의미인지(지원할 만한 직무·일정, 채용 시장 흐름의 시사점)."
    return cat.get("why", DEFAULT_WHY).strip()


def summarize_user(cfg: dict, shortlists: dict[str, list[Cluster]], user) -> tuple[list[Story], str]:
    """사용자 1명의 리포트를 한 번의 호출로 요약한다. 실패하면 (제목만 목록, 오류 메시지)."""
    cats = {c["id"]: c for c in cfg["categories"] if shortlists.get(c["id"])}
    flat: list[tuple[str, Cluster]] = []
    blocks = []
    for cid, cat in cats.items():
        lines = [f"== 카테고리 {cid} · {cat['name']} (최대 {cat['pick']}개)",
                 f"   '왜 중요한가' 관점: {why_for(cat, user)}"]
        for cl in shortlists[cid]:
            flat.append((cid, cl))
            lines.append(_candidate_text(len(flat), cl, cfg["tz"]))
        blocks.append("\n\n".join(lines))

    if not flat:
        return [], "후보 기사가 없습니다"

    major = user.major or "특별히 정해진 전공 없음"
    system = SYSTEM_USER.format(major=major, persona=PERSONAS.get(user.persona, PERSONAS["jobseeker"]),
                                keywords=", ".join(user.keywords) or "없음")

    def fallback_stories() -> list[Story]:
        out = []
        for cid, cat in cats.items():
            out.extend(fallback(cat, shortlists[cid]))
        return out

    if not env("GEMINI_API_KEY"):
        return fallback_stories(), "GEMINI_API_KEY 없음"

    client = genai.Client(api_key=env("GEMINI_API_KEY"), http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=4, initial_delay=8, max_delay=45,
                                             http_status_codes=[429, 500, 502, 503, 504])))
    models = model_chain(cfg)
    last_error = ""
    for i, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model,
                contents="후보 이슈:\n\n" + "\n\n".join(blocks),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=UserPicks,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            parsed = response.parsed
            if not isinstance(parsed, UserPicks):
                raise RuntimeError(f"응답 형식 오류 ({response.candidates[0].finish_reason if response.candidates else '응답 없음'})")

            stories, used, per_cat = [], set(), {}
            for p in parsed.picks:
                if not 1 <= p.candidate <= len(flat):
                    continue
                cid, cl = flat[p.candidate - 1]
                limit = cats[cid]["pick"]
                if cl.id in used or per_cat.get(cid, 0) >= limit:
                    continue
                used.add(cl.id)
                per_cat[cid] = per_cat.get(cid, 0) + 1
                stories.append(Story(category=cid, headline=p.headline, short=p.short, what=p.what,
                                     points=p.points[:3], why=p.why, url=cl.lead.url, sources=cl.sources,
                                     lead_title=cl.lead.title))
            if not stories:
                raise RuntimeError("고른 이슈가 없습니다")
            return stories, ""
        except genai_errors.APIError as exc:
            last_error = str(exc)[:120]
            if exc.code not in RETRY_ELSEWHERE or i == len(models) - 1:
                break
        except Exception as exc:
            last_error = str(exc)[:120]
            break
    return fallback_stories(), last_error
