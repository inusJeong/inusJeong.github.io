"""사용법
  python -m newsbot preview      리포트만 만들고 브라우저로 열기 (발송 X)  ← 튜닝할 때
  python -m newsbot run          만들고 + 카톡 발송
  python -m newsbot build        만들기만 (GitHub Actions 1단계)
  python -m newsbot send         만든 리포트를 카톡 발송 (GitHub Actions 2단계, 페이지 배포 후)
  python -m newsbot run-users    구독자 전원에게 발송 (여러 사용자 모드)
  python -m newsbot check        뉴스 소스·API 키 점검
  python -m newsbot kakao-auth   카카오 refresh token 최초 발급
  python -m newsbot kakao-test   카톡 발송 테스트 (상태 기록 안 함)
옵션: --force  오늘 이미 발송했어도 다시 실행
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime

from . import glossary, kakao, state, vocab
from .cluster import cluster_articles
from .collect import collect
from .config import OUT, env, load_config
from .enrich import enrich
from .rank import shortlist
from .render import kakao_text, report_html, write_report
from .summarize import summarize


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def gh_output(key: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"{key}={value}\n")


def build(cfg: dict, force: bool, publish: bool = True) -> dict | None:
    """publish=False(미리보기)면 docs/ 아카이브를 건드리지 않고 out/preview.html에만 쓴다."""
    now = datetime.now(cfg["tz"])
    today = now.date()
    st = state.load()
    if st.get("last_sent") == today.isoformat() and not force:
        log("오늘은 이미 발송했습니다 (다시 하려면 --force)")
        gh_output("skip", "true")
        return None

    t0 = time.time()
    articles, source_stats = collect(cfg, now)
    log(f"1 수집: 기사 {len(articles)}건 ({time.time() - t0:.1f}s)")

    clusters = cluster_articles(articles, [c["id"] for c in cfg["categories"]])
    log(f"2 중복 제거: 이슈 {len(clusters)}개")

    recent = state.recent_titles(st, today, cfg["report"]["history_days"])
    shortlists = shortlist(clusters, cfg, now, recent)
    n_short = sum(len(v) for v in shortlists.values())
    log(f"3 관련도 필터: 후보 {n_short}개 (최근 발송 {len(recent)}건과 중복 제외)")

    n_body = enrich(shortlists)
    log(f"  본문 보강: {n_body}/{n_short}")

    stories, errors = summarize(cfg, shortlists)
    log(f"4 요약: {sum(len(v) for v in stories.values())}개 선정" + (f" / 경고 {errors}" if errors else ""))

    terms, next_cursor = glossary.todays_terms(st["term_cursor"], cfg["report"]["terms_per_day"])
    log(f"5 오늘의 용어: {', '.join(t['term'] for t in terms)}")
    words, next_word_day = vocab.todays_vocab(st.get("word_day", 0))
    log(f"  토익: Day {words['day']} {words['theme']} · LC {len(words['lc'])} · RC {len(words['rc'])}"
        f" · 복습 묶음 {len(words['reviews'])}")

    stats = {"articles": len(articles), "clusters": len(clusters), "shortlisted": n_short, "enriched": n_body,
             "errors": errors, "sources": [asdict(s) for s in source_stats]}
    date_str = today.isoformat()
    first = next((s for c in cfg["categories"] for s in stories.get(c["id"], [])), None)
    html = report_html(cfg, now, stories, terms, stats, words)
    OUT.mkdir(exist_ok=True)
    if publish:
        write_report(date_str, html, first.headline if first else "")
    else:
        (OUT / "preview.html").write_text(html, encoding="utf-8")
    text = kakao_text(cfg, now, stories, terms, words)
    log(f"6 조립: {'docs/' + date_str if publish else 'out/preview'}.html + 카톡 {len(text)}자")

    result = {
        "date": date_str,
        "text": text,
        "titles": [s.lead_title for v in stories.values() for s in v],
        "next_cursor": next_cursor,
        "next_word_day": next_word_day,
    }
    if not publish:
        return result
    (OUT / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    gh_output("skip", "false")
    return result


def send(cfg: dict) -> None:
    path = OUT / "result.json"
    if not path.exists():
        log("보낼 리포트가 없습니다 (build 단계가 건너뛰어졌거나 실패)")
        return
    result = json.loads(path.read_text(encoding="utf-8"))
    if cfg["kakao"]["enabled"]:
        base = env("REPORT_BASE_URL")
        if not base:
            raise SystemExit("REPORT_BASE_URL(리포트 주소)이 없어 카톡 버튼 링크를 만들 수 없습니다")
        url = f"{base.rstrip('/')}/{result['date']}.html"
        kakao.send_text(result["text"], url, cfg["kakao"]["button_title"])
        log("7 발송: 카카오톡 전송 완료")
    # 발송 성공 후에만 상태 전진 → 실패한 날의 용어를 건너뛰지 않고, 같은 이슈를 다시 보내지 않음
    st = state.load()
    today = datetime.fromisoformat(result["date"]).date()
    state.save(state.record_sent(st, today, result["titles"], result["next_cursor"],
                                 cfg["report"]["history_days"], result.get("next_word_day")))
    log("상태 저장: data/state.json")


def check(cfg: dict) -> None:
    for key in ["GEMINI_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "KAKAO_REST_API_KEY",
                "KAKAO_REFRESH_TOKEN", "REPORT_BASE_URL"]:
        print(f"{'✅' if env(key) else '⬜'} {key}")
    print()
    _, stats = collect(cfg, datetime.now(cfg["tz"]))
    for s in sorted(stats, key=lambda s: (s.category, s.count)):
        mark = "❌" if s.error else ("⚠️ " if s.count == 0 else "✅")
        print(f"{mark} [{s.category}] {s.name}: {s.count}건 {s.error}")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "preview"
    force = "--force" in args
    cfg = load_config()

    if cmd == "kakao-auth":
        kakao.authorize()
    elif cmd == "kakao-test":
        url = env("REPORT_BASE_URL") or "https://developers.kakao.com"
        kakao.send_text("✅ 데일리 뉴스 리포트 연결 테스트\n이 메시지가 보이면 카카오톡 발송 설정 완료!",
                        url, cfg["kakao"]["button_title"])
        log("카카오톡 테스트 메시지 발송 완료 — 카톡 '나와의 채팅'을 확인하세요")
    elif cmd == "check":
        check(cfg)
    elif cmd == "build":
        build(cfg, force)
    elif cmd == "send":
        send(cfg)
    elif cmd == "run-users":
        from .multi import run_all
        run_all(cfg, force)
    elif cmd == "run":
        if build(cfg, force):
            send(cfg)
    elif cmd == "preview":
        result = build(cfg, force=True, publish=False)
        print("\n── 카톡 미리보기 ──\n" + result["text"] + "\n───────────────────")
        webbrowser.open((OUT / "preview.html").as_uri())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
