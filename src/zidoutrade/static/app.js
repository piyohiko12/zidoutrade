"use strict";

const sections = ["overview", "candidates", "decision", "risk", "journal", "system"];
const titles = {
  overview: "運用概要",
  candidates: "候補銘柄",
  decision: "売買判断",
  risk: "リスク制御",
  journal: "株式日記",
  system: "システム状態",
};

let csrfToken = "";
let currentState = null;

const byId = (id) => document.getElementById(id);
const text = (id, value) => { const node = byId(id); if (node) node.textContent = value == null ? "—" : String(value); };

function showMessage(message, isError = false) {
  const node = byId("global-message");
  node.textContent = message;
  node.classList.toggle("is-error", isError);
  node.hidden = false;
}

function clearMessage() {
  const node = byId("global-message");
  node.hidden = true;
  node.textContent = "";
  node.classList.remove("is-error");
}

function activateSection(name) {
  const section = sections.includes(name) ? name : "overview";
  document.querySelectorAll(".workspace-section").forEach((node) => {
    const active = node.id === section;
    node.hidden = !active;
    node.classList.toggle("is-active", active);
  });
  document.querySelectorAll(".nav-link").forEach((node) => {
    node.classList.toggle("is-active", node.dataset.section === section);
  });
  text("page-title", titles[section]);
}

function recordFromState(state) {
  return state && state.selection && state.selection.record ? state.selection.record : null;
}

function makePill(label, pass) {
  const span = document.createElement("span");
  span.className = `pill ${pass ? "pill-safe" : "pill-fail"}`;
  span.textContent = label;
  return span;
}

function renderCandidates(state) {
  const tbody = byId("candidate-rows");
  tbody.replaceChildren();
  const candidates = Array.isArray(state.candidates) ? state.candidates : [];
  const record = recordFromState(state);
  const selectedSymbol = record ? record.selected_symbol : null;

  if (!candidates.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty";
    cell.textContent = "候補データがありません。取引は行われません。";
    row.appendChild(cell);
    tbody.appendChild(row);
  }

  candidates.forEach((candidate) => {
    const row = document.createElement("tr");
    const choiceCell = document.createElement("td");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "candidate";
    radio.value = String(candidate.symbol || "");
    radio.disabled = candidate.eligible !== true;
    radio.checked = candidate.symbol === selectedSymbol;
    radio.setAttribute("aria-label", `${candidate.symbol || "不明"}を選択`);
    choiceCell.appendChild(radio);

    const symbolCell = document.createElement("td");
    symbolCell.className = "symbol-cell";
    symbolCell.textContent = candidate.symbol || "—";

    const statusCell = document.createElement("td");
    statusCell.appendChild(makePill(candidate.eligible ? "PASS" : "FAIL", candidate.eligible === true));

    const priorityCell = document.createElement("td");
    priorityCell.textContent = candidate.priority == null ? "—" : String(candidate.priority);

    const reasonsCell = document.createElement("td");
    reasonsCell.className = "reason-list";
    const reasons = Array.isArray(candidate.reason_codes) ? candidate.reason_codes : [];
    reasonsCell.textContent = reasons.length ? reasons.join(" · ") : "全適格条件を通過";
    row.append(choiceCell, symbolCell, statusCell, priorityCell, reasonsCell);
    tbody.appendChild(row);
  });

  const noTrade = document.querySelector('input[name="candidate"][value=""]');
  if (noTrade) noTrade.checked = selectedSymbol == null;

  const target = (record && record.target_session)
    || (state.overview && state.overview.target_session)
    || (state.system && state.system.target_session)
    || "";
  byId("target-session").value = target;
}

function renderOverview(state) {
  const candidates = Array.isArray(state.candidates) ? state.candidates : [];
  const eligible = candidates.filter((candidate) => candidate.eligible === true).length;
  const record = recordFromState(state);
  text("metric-eligible", `${eligible} / ${candidates.length}`);
  text("metric-session", record ? record.target_session : ((state.overview || {}).target_session || "未設定"));
  text("metric-state", record ? record.state : "未選択");
  text("metric-state-note", record && record.selected_symbol ? record.selected_symbol : "選ばなければ取引しません");

  document.querySelectorAll("[data-flow]").forEach((node) => {
    node.classList.toggle("is-current", Boolean(record && node.dataset.flow === record.state));
  });
}

function renderDecision(state) {
  const decision = state.decision && typeof state.decision === "object" ? state.decision : {};
  const action = String(decision.action || decision.signal || "WAIT").toUpperCase();
  const badge = byId("decision-badge");
  badge.textContent = action;
  badge.className = `pill ${action === "WAIT" ? "pill-wait" : action === "ENTER" ? "pill-safe" : "pill-info"}`;
  byId("decision-detail").textContent = Object.keys(decision).length
    ? JSON.stringify(decision, null, 2)
    : "判断データ待ち（条件が不足している間は WAIT）";
}

function renderRisk(state) {
  const host = byId("risk-cards");
  host.replaceChildren();
  const risk = state.risk && typeof state.risk === "object" ? state.risk : {};
  const entries = Object.entries(risk);
  if (!entries.length) {
    entries.push(["状態", "未設定 — 取引停止"]);
  }
  entries.slice(0, 8).forEach(([label, value]) => {
    const article = document.createElement("article");
    article.className = "metric-card";
    const small = document.createElement("span");
    small.className = "metric-label";
    small.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
    article.append(small, strong);
    host.appendChild(article);
  });
}

function renderJournal(state) {
  const host = byId("journal-list");
  host.replaceChildren();
  const events = Array.isArray(state.journal) ? state.journal : [];
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "panel empty";
    empty.textContent = "記録はまだありません。判断が発生すると根拠とともに表示されます。";
    host.appendChild(empty);
    return;
  }
  events.forEach((event) => {
    const article = document.createElement("article");
    article.className = "panel journal-item";
    const time = document.createElement("time");
    time.textContent = event.timestamp || event.time || "—";
    const detail = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = event.event || event.action || "記録";
    const paragraph = document.createElement("p");
    paragraph.textContent = event.reason || event.detail || "";
    detail.append(strong, paragraph);
    const symbol = document.createElement("span");
    symbol.className = "pill pill-info";
    symbol.textContent = event.symbol || "SYSTEM";
    article.append(time, detail, symbol);
    host.appendChild(article);
  });
}

function renderSystem(state) {
  const list = byId("system-list");
  list.replaceChildren();
  const defaults = { UI: "127.0.0.1 loopback", order_api: "無効（SIMULATEを含む）", mode: "SHADOW_ORDER_DISABLED" };
  const system = Object.assign(defaults, state.system && typeof state.system === "object" ? state.system : {});
  Object.entries(system).forEach(([label, value]) => {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = typeof value === "object" ? JSON.stringify(value) : String(value);
    row.append(dt, dd);
    list.appendChild(row);
  });
}

function render(state) {
  currentState = state;
  renderOverview(state);
  renderCandidates(state);
  renderDecision(state);
  renderRisk(state);
  renderJournal(state);
  renderSystem(state);
}

async function readJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `HTTP_${response.status}`);
  return body;
}

async function refresh() {
  const button = byId("refresh-button");
  button.disabled = true;
  try {
    const [stateResponse, csrfResponse] = await Promise.all([
      fetch("/api/state", { credentials: "same-origin", cache: "no-store" }),
      csrfToken ? Promise.resolve(null) : fetch("/api/csrf", { credentials: "same-origin", cache: "no-store" }),
    ]);
    const state = await readJson(stateResponse);
    if (csrfResponse) {
      const csrf = await readJson(csrfResponse);
      csrfToken = csrf.csrf_token || "";
    }
    render(state);
    const pill = byId("connection-pill");
    pill.textContent = "LOCAL CONNECTED";
    pill.className = "pill pill-safe";
  } catch (error) {
    const pill = byId("connection-pill");
    pill.textContent = "DATA UNAVAILABLE";
    pill.className = "pill pill-fail";
    showMessage(`状態を取得できません: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function saveSelection() {
  clearMessage();
  const selectedNode = document.querySelector('input[name="candidate"]:checked');
  const targetSession = byId("target-session").value;
  if (!selectedNode) return showMessage("候補または「取引しない」を選んでください。", true);
  if (!targetSession) return showMessage("対象セッションを指定してください。", true);
  if (!csrfToken) return showMessage("安全トークンがありません。画面を更新してください。", true);

  const button = byId("save-selection");
  button.disabled = true;
  try {
    const response = await fetch("/api/selection", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ selected_symbol: selectedNode.value || null, target_session: targetSession }),
    });
    const result = await readJson(response);
    showMessage(result.message || "次セッション候補を新しいリビジョンとして保存しました。");
    await refresh();
  } catch (error) {
    showMessage(`保存できません: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function advanceSelection(action) {
  clearMessage();
  const record = recordFromState(currentState);
  const expected = currentState && currentState.selection ? currentState.selection.sha256 : "";
  if (!record || !expected) return showMessage("先にDRAFTを保存してください。", true);
  if (!csrfToken) return showMessage("安全トークンがありません。画面を更新してください。", true);
  const isArm = action === "arm";
  if (isArm && !window.confirm(`${record.target_session} の ${record.selected_symbol || "NO TRADE"} をARMします。続けますか？`)) return;
  const button = byId(isArm ? "arm-selection" : "validate-selection");
  button.disabled = true;
  try {
    const body = isArm
      ? { expected_sha256: expected, confirmation: "ARM_NEXT_SESSION" }
      : { expected_sha256: expected };
    const response = await fetch(`/api/selection/${action}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(body),
    });
    const result = await readJson(response);
    showMessage(result.message || `${result.state}として保存しました。`);
    await refresh();
  } catch (error) {
    showMessage(`更新できません: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", () => activateSection(link.dataset.section));
});
window.addEventListener("hashchange", () => activateSection(location.hash.slice(1)));
byId("refresh-button").addEventListener("click", () => { clearMessage(); refresh(); });
byId("save-selection").addEventListener("click", saveSelection);
byId("validate-selection").addEventListener("click", () => advanceSelection("validate"));
byId("arm-selection").addEventListener("click", () => advanceSelection("arm"));

activateSection(location.hash.slice(1));
refresh();
