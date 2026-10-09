/* NQIS Vision Intelligence public/static or same-origin local API bridge.
 * Explicit user action only. No model or dataset is ever shipped in the web app.
 * Tokens are kept in the password field, never persisted to localStorage.
 */
(function () {
  "use strict";
  const q = (id) => document.getElementById(id);
  const fileInput = q("upload");
  const inspectButton = q("liveInspect");
  const checkButton = q("checkVisionApi");
  const serverField = q("inferenceUrl");
  const secretField = q("apiToken");
  const connection = q("apiConnectionState");
  const message = q("message");
  const mode = document.querySelector(".mode");
  if (!fileInput || !inspectButton || !checkButton || !serverField || !secretField) return;

  const originalRender = window.render;
  let inspection = null;
  let heatmapImage = null;

  function endpoint() {
    const local = ["localhost", "127.0.0.1"].includes(window.location.hostname);
    const raw = serverField.value.trim() || (local ? window.location.origin : "");
    if (!raw) throw new Error("아직 AI 서버 주소가 없어요. HTTPS API 서버 주소를 입력하세요.");
    let url;
    try { url = new URL(raw); }
    catch { throw new Error("AI 서버 주소는 https://로 시작해야 합니다."); }
    const isSameOriginLocal = local && url.origin === window.location.origin;
    if (url.protocol !== "https:" && !isSameOriginLocal)
      throw new Error("원격 AI 서버에는 HTTPS 주소만 사용할 수 있습니다.");
    if (url.username || url.password || url.search || url.hash ||
        (url.pathname !== "/" && url.pathname !== ""))
      throw new Error("도메인 주소만 입력하세요. 경로, 토큰, 검색문자열은 제외하세요.");
    return url.origin;
  }

  function reset() {
    inspection = null;
    heatmapImage = null;
    window.nqisOverlay = null;
    mode.textContent = "● SIMULATION / AI 서버 미연결";
  }

  function setError(err) {
    reset();
    connection.textContent = "서버 연결되지 않음";
    message.textContent = err instanceof Error ? err.message : "AI 서버와 통신할 수 없습니다.";
    if (typeof originalRender === "function") originalRender();
  }

  function paintHeatmap(ctx, x, y, width, height) {
    if (!heatmapImage || !inspection || !uploaded) return;
    ctx.save();
    ctx.drawImage(heatmapImage, x, y, width, height);
    ctx.strokeStyle = "#ffcf91";
    ctx.lineWidth = 2.5;
    ctx.setLineDash([8, 5]);
    for (const [x0, y0, x1, y1] of inspection.boxes || []) {
      ctx.strokeRect(x + x0 * width, y + y0 * height,
        (x1 - x0) * width, (y1 - y0) * height);
    }
    ctx.restore();
  }

  function displayReal() {
    if (!inspection || !uploaded) return;
    q("mScore").textContent = inspection.anomaly_score.toFixed(3);
    q("scoreFill").style.width = (inspection.anomaly_score * 100) + "%";
    q("gradeText").textContent = inspection.is_anomaly ? "불량 의심" : "정상 후보";
    q("status").textContent = inspection.is_anomaly ? "SUSPECT" : "PASS";
    q("status").className = "status " + (inspection.is_anomaly ? "suspect" : "");
    q("coord").textContent =
      "실제 모델 결과 · " + inspection.tile_count + "타일 · " +
      inspection.width + " × " + inspection.height;
    q("previewLabel").textContent = "Real model inference / research-only";
    q("type").textContent = "이상 여부 · 종류 분류는 미지원";
    q("sourceInfo").textContent = "사용자 이미지 · 명시적 AI 검사";
    mode.textContent = "● REAL VISION INFERENCE / 연구용";
    connection.textContent = "실제 추론 응답 수신 · " + inspection.model;
    message.textContent =
      "실제 모델 추론 결과입니다. 이상점수는 불량 확률이 아니며 왼쪽 Threshold 슬라이더는 아직 시뮬레이션입니다.";
  }

  window.render = function () {
    if (typeof originalRender === "function") originalRender();
    displayReal();
  };

  function asBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("이미지 파일을 읽지 못했습니다."));
      reader.onload = () => {
        const raw = String(reader.result);
        resolve(raw.slice(raw.indexOf(",") + 1));
      };
      reader.readAsDataURL(file);
    });
  }

  function loadPng(data) {
    return new Promise((resolve, reject) => {
      const im = new Image();
      im.onload = () => resolve(im);
      im.onerror = () => reject(new Error("AI 결과 이미지를 열지 못했습니다."));
      im.src = "data:image/png;base64," + data;
    });
  }

  async function getJson(url, init = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 120000);
    try {
      const res = await fetch(url, { ...init, signal: controller.signal, cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const reason = typeof data.detail === "string" ? data.detail : "API 상태를 확인하세요.";
        throw new Error("AI 서버 응답 " + res.status + ": " + reason);
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  async function checkConnection() {
    const origin = endpoint();
    const status = await getJson(origin + "/fabric/v1/status");
    connection.textContent = status.configured ?
      "서버 응답 확인 · 모델 초기화 전" : "서버 접속됨 · 모델 미설정";
    if (!status.configured)
      throw new Error("서버에 승인된 모델 가중치와 정상 원단 이미지가 설정되지 않았습니다.");
  }

  checkButton.addEventListener("click", async () => {
    checkButton.disabled = true;
    connection.textContent = "연결 확인 중…";
    try { await checkConnection(); message.textContent = "서버 응답 확인. 이미지를 선택하고 검사하세요."; }
    catch (err) { setError(err); }
    finally { checkButton.disabled = false; }
  });

  fileInput.addEventListener("change", () => {
    reset();
    connection.textContent = "AI 검사 대기";
  });

  document.querySelectorAll(".option").forEach((button) => button.addEventListener("click", () => {
    reset();
    connection.textContent = "합성 시뮬레이션";
  }));

  inspectButton.addEventListener("click", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) { message.textContent = "먼저 ‘내 이미지 미리보기’에서 파일을 선택하세요."; return; }
    if (file.size > 6 * 1024 * 1024) {
      message.textContent = "검사할 이미지는 최대 6 MiB까지 지원합니다.";
      return;
    }
    if (!["image/png", "image/jpeg"].includes(file.type)) {
      message.textContent = "실제 AI 검사는 PNG/JPEG 이미지만 지원합니다.";
      return;
    }
    reset();
    inspectButton.disabled = true;
    inspectButton.textContent = "실제 모델 분석 중…";
    try {
      const origin = endpoint();
      const state = await getJson(origin + "/fabric/v1/status");
      if (!state.configured)
        throw new Error("실제 모델이 준비되지 않았습니다. Mac mini에서 모델을 설정해야 합니다.");
      const token = secretField.value;
      const headers = { "Content-Type": "application/json" };
      if (token) headers["X-NQIS-Token"] = token;
      const result = await getJson(origin + "/fabric/v1/inspect", {
        method: "POST", headers,
        body: JSON.stringify({
          image_id: "fabric-" + Date.now(),
          image_base64: await asBase64(file),
        }),
      });
      if (!result.heatmap_png_base64 || typeof result.anomaly_score !== "number")
        throw new Error("AI 서버가 유효한 분석 결과를 반환하지 않았습니다.");
      const im = await loadPng(result.heatmap_png_base64);
      if (!uploaded)
        throw new Error("이미지 미리보기가 아직 준비되지 않았습니다. 다시 시도해 주세요.");
      inspection = result;
      heatmapImage = im;
      window.nqisOverlay = paintHeatmap;
      window.render();
    } catch (err) { setError(err); }
    finally {
      inspectButton.disabled = false;
      inspectButton.textContent = "실제 AI 검사";
    }
  });
})();
