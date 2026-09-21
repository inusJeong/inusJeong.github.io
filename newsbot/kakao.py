"""7단계 · 발송: 카카오톡 "나에게 보내기".

토큰 구조
  - access token  : 약 6시간 유효 → 매번 refresh token으로 새로 발급
  - refresh token : 약 2개월 유효, 만료 1개월 전부터 갱신 요청 시 새 토큰을 준다
    → 새 토큰이 오면 out/new_refresh_token 파일로 남기고, GitHub Actions가 Secret을 교체한다
"""
from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import OUT, env

AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
REDIRECT_URI = "http://localhost:5555"


def _client_params() -> dict:
    params = {"client_id": env("KAKAO_REST_API_KEY")}
    if env("KAKAO_CLIENT_SECRET"):
        params["client_secret"] = env("KAKAO_CLIENT_SECRET")
    return params


def access_token(refresh: str = "") -> tuple[str, str]:
    """(액세스 토큰, 새 refresh token 또는 빈 문자열). 인자가 없으면 .env의 내 토큰을 쓴다."""
    refresh = refresh or env("KAKAO_REFRESH_TOKEN")
    if not env("KAKAO_REST_API_KEY") or not refresh:
        raise RuntimeError("KAKAO_REST_API_KEY / KAKAO_REFRESH_TOKEN 이 설정되지 않았습니다")
    resp = requests.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh,
                                          **_client_params()}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"카카오 토큰 갱신 실패 ({resp.status_code}): {resp.text[:200]}\n"
                           f"→ refresh token이 만료됐다면 `python -m newsbot kakao-auth`로 다시 발급하세요")
    data = resp.json()
    new_refresh = data.get("refresh_token", "")   # 만료가 가까우면 새 토큰을 함께 준다
    if new_refresh and not refresh_given(refresh):
        OUT.mkdir(exist_ok=True)
        (OUT / "new_refresh_token").write_text(new_refresh, encoding="utf-8")
    return data["access_token"], new_refresh


def refresh_given(refresh: str) -> bool:
    """사용자별 토큰으로 호출된 경우인지 (내 .env 토큰이면 파일로 저장해 Actions가 교체한다)."""
    return refresh != env("KAKAO_REFRESH_TOKEN")


def send_text(text: str, url: str, button_title: str, refresh: str = "") -> str:
    """보내고, 새 refresh token이 발급됐으면 돌려준다 (호출한 쪽이 저장)."""
    token, new_refresh = access_token(refresh)
    template = {"object_type": "text", "text": text,
                "link": {"web_url": url, "mobile_web_url": url}, "button_title": button_title}
    resp = requests.post(SEND_URL, headers={"Authorization": f"Bearer {token}"},
                         data={"template_object": json.dumps(template, ensure_ascii=False)}, timeout=15)
    if resp.status_code != 200 or resp.json().get("result_code") != 0:
        raise RuntimeError(f"카카오톡 발송 실패 ({resp.status_code}): {resp.text[:200]}")
    return new_refresh


def authorize() -> None:
    """최초 1회: 브라우저로 카카오 로그인 → 동의 → refresh token 출력."""
    if not env("KAKAO_REST_API_KEY"):
        raise SystemExit(".env에 KAKAO_REST_API_KEY를 먼저 넣어주세요")
    code_box: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            code_box["code"] = qs.get("code", [""])[0]
            code_box["error"] = qs.get("error_description", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>인증 완료. 이 창을 닫고 터미널로 돌아가세요.</h2>".encode())

        def log_message(self, *args):
            pass

    url = AUTH_URL + "?" + urlencode({"client_id": env("KAKAO_REST_API_KEY"), "redirect_uri": REDIRECT_URI,
                                      "response_type": "code", "scope": "talk_message"})
    print(f"브라우저에서 카카오 로그인 후 '동의하고 계속하기'를 눌러주세요.\n(안 열리면 직접 열기: {url})")
    webbrowser.open(url)
    server = HTTPServer(("localhost", 5555), Handler)
    while "code" not in code_box:
        server.handle_request()
    if not code_box["code"]:
        raise SystemExit(f"인증 실패: {code_box['error']}")

    resp = requests.post(TOKEN_URL, data={"grant_type": "authorization_code", "redirect_uri": REDIRECT_URI,
                                          "code": code_box["code"], **_client_params()}, timeout=15)
    resp.raise_for_status()
    token = resp.json()["refresh_token"]
    print("\n✅ 발급 완료. 아래 값을 .env의 KAKAO_REFRESH_TOKEN과 GitHub Secret에 넣으세요:\n")
    print(token)
