"use strict";

const sections = ["overview", "candidates", "decision", "risk", "journal", "system"];
const titles = {
  overview: "今日の状況",
  candidates: "銘柄を選ぶ",
  decision: "判断を見る",
  risk: "安全ルール",
  journal: "振り返り",
  system: "接続状態",
};

const selectionLabels = {
  DRAFT: "選択を保存済み",
  VALIDATED: "安全条件を確認済み",
  ARMED_NEXT_SESSION: "次回の分析対象に確定済み",
  SESSION_LOCKED: "当日の分析対象を固定済み",
  EXPIRED: "期限切れです。日付と銘柄を選び直してください",
};

const actionLabels = {
  WAIT: "待機中",
  ENTER: "買い条件が成立",
  EXIT: "売り条件が成立",
};

const reasonLabels = {
  NO_SIGNAL: "まだRSIと価格の条件がそろっていません",
  NO_VALIDATED_CANDIDATE_SNAPSHOT: "分析する銘柄がまだ確定していません",
  DISARMED: "安全のため分析実行を停止しています",
  TRADING_HALTED: "この銘柄は現在取引停止中です",
  NO_VALIDATED_SELECTION: "分析する銘柄がまだ確定していません",
  DATA_UNAVAILABLE: "判断に必要なデータを取得できません",
  STALE_DATA: "データが古いため判断を待っています",
  DAILY_LOSS_LIMIT: "1日の損失上限に達しています",
  WEEKLY_LOSS_LIMIT: "1週間の損失上限に達しています",
  VOLUME_HISTORY_INSUFFICIENT: "出来高を比べる直前13本がそろっていません",
  VOLUME_DATA_INVALID: "出来高データに0または不正な値があるため待機します",
  VOLUME_CONFIRMATION_MISSING: "出来高が直前13本の中央値の1.5倍に届いていません",
  PRICE_REFERENCE_INVALID: "比較する直前高値を確認できません",
  PRICE_CONFIRMATION_MISSING: "価格が直前高値を上回っていません",
  BREAKOUT_TOO_EXTENDED: "直前高値から0.50%を超えて上昇したため追いかけません",
  SPREAD_TOO_WIDE: "売値と買値の開きが0.10%を超えています",
};

const riskLabels = {
  planned_risk: "1回の予定リスク",
  new_entries_permitted: "新しい買い判断",
  reason: "現在の状態",
  daily_loss: "今日の損失",
  weekly_loss: "今週の損失",
};

const systemLabels = {
  UI: "この画面",
  order_api: "注文機能",
  mode: "動作モード",
  broker_environment: "将来の検証環境",
  opend_endpoint: "ローカル接続先",
  activation_present: "注文の有効化",
  sensitive_data_exposed: "機密情報の表示",
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
    if (node.dataset.section === section) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  text("page-title", titles[section]);
}

function explainCode(value) {
  const code = String(value || "").toUpperCase();
  return reasonLabels[code] || "現在は安全のため判断を見送っています";
}

function displayValue(value) {
  if (value === true) return "許可";
  if (value === false) return "停止";
  if (value == null || value === "") return "未設定";
  return String(value);
}

function displaySystemValue(label, value) {
  if (label === "activation_present") return value ? "有効" : "無効（安全）";
  if (label === "sensitive_data_exposed") return value ? "要確認" : "表示していません";
  if (label === "mode" && value === "SHADOW_ORDER_DISABLED") return "分析のみ（注文停止）";
  if (label === "mode" && value === "SIMULATE_ONLY") return "デモ環境向け（注文停止）";
  if (label === "broker_environment" && value === "SIMULATE") return "デモ環境（現在は未接続）";
  if (label === "order_api") return "無効（デモ注文を含む）";
  return displayValue(value);
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
  const noCandidates = candidates.length === 0;
  const stateName = record ? record.state : "";

  if (!candidates.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty";
    cell.textContent = "候補銘柄はまだありません。現在は表示専用です。操作は必要ありません。";
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
    radio.disabled = candidate.eligible !== true || stateName === "SESSION_LOCKED";
    radio.checked = candidate.symbol === selectedSymbol;
    radio.setAttribute("aria-label", `${candidate.symbol || "不明"}を選択`);
    choiceCell.appendChild(radio);

    const symbolCell = document.createElement("td");
    symbolCell.className = "symbol-cell";
    symbolCell.textContent = candidate.symbol || "—";

    const statusCell = document.createElement("td");
    statusCell.appendChild(makePill(candidate.eligible ? "対象" : "対象外", candidate.eligible === true));

    const priorityCell = document.createElement("td");
    priorityCell.textContent = candidate.priority == null ? "—" : String(candidate.priority);

    const reasonsCell = document.createElement("td");
    reasonsCell.className = "reason-list";
    const reasons = Array.isArray(candidate.reason_codes) ? candidate.reason_codes : [];
    reasonsCell.textContent = reasons.length ? reasons.map(explainCode).join(" ／ ") : "すべての安全条件を満たしています";
    choiceCell.dataset.label = "選ぶ";
    symbolCell.dataset.label = "銘柄";
    statusCell.dataset.label = "対象にできる？";
    priorityCell.dataset.label = "表示順";
    reasonsCell.dataset.label = "理由";
    row.append(choiceCell, symbolCell, statusCell, priorityCell, reasonsCell);
    tbody.appendChild(row);
  });

  const noTrade = document.querySelector('input[name="candidate"][value=""]');
  if (noTrade) noTrade.checked = Boolean(record && selectedSymbol == null);

  const saveButton = byId("save-selection");
  const validateButton = byId("validate-selection");
  const armButton = byId("arm-selection");
  const selectionAvailability = byId("selection-availability");
  saveButton.hidden = Boolean(record) && stateName !== "EXPIRED";
  validateButton.hidden = stateName !== "DRAFT";
  armButton.hidden = stateName !== "VALIDATED";
  saveButton.disabled = noCandidates;
  byId("target-session").disabled = noCandidates || stateName === "SESSION_LOCKED";
  if (noTrade) noTrade.disabled = noCandidates || stateName === "SESSION_LOCKED";
  selectionAvailability.hidden = !noCandidates;

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
  text("metric-state", record && record.selected_symbol ? record.selected_symbol : record ? "この日は選ばない" : "未選択");
  text("metric-state-note", record ? (selectionLabels[record.state] || "保存状態を確認してください") : "候補画面から選んでください");

  document.querySelectorAll("[data-flow]").forEach((node) => {
    node.classList.toggle("is-current", Boolean(record && node.dataset.flow === record.state));
  });
}

function renderDecision(state) {
  const decision = state.decision && typeof state.decision === "object" ? state.decision : {};
  const action = String(decision.action || decision.signal || "WAIT").toUpperCase();
  const badge = byId("decision-badge");
  badge.textContent = actionLabels[action] || "確認が必要";
  badge.className = `pill ${action === "WAIT" ? "pill-wait" : action === "ENTER" ? "pill-safe" : "pill-info"}`;
  text("decision-summary", action === "WAIT" ? "条件がそろうまで待ちます" : action === "ENTER" ? "買い候補の条件がそろいました" : action === "EXIT" ? "終了を検討する条件がそろいました" : "判断内容を確認してください");
  const reasonValues = [];
  if (Array.isArray(decision.reasons)) reasonValues.push(...decision.reasons);
  if (Array.isArray(decision.reason_codes)) reasonValues.push(...decision.reason_codes);
  if (decision.reason) reasonValues.push(decision.reason);
  text("decision-reasons", reasonValues.length ? reasonValues.map(explainCode).join("。") : "判断に必要なデータを待っています。");
  byId("decision-detail").textContent = Object.keys(decision).length
    ? JSON.stringify(decision, null, 2)
    : "判断データ待ち（条件が不足している間は待機します）";
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
    small.textContent = riskLabels[label] || label.replaceAll("_", " ");
    const strong = document.createElement("strong");
    strong.textContent = typeof value === "object" ? JSON.stringify(value) : label === "reason" ? explainCode(value) : displayValue(value);
    article.append(small, strong);
    host.appendChild(article);
  });
}

function formatBasisPoints(value) {
  if (!Number.isInteger(value)) return "—";
  const whole = Math.floor(value / 100);
  const fraction = String(value % 100).padStart(2, "0");
  return `${whole}.${fraction}%`;
}

function basisPointsInput(value) {
  if (!Number.isInteger(value)) return "";
  return `${Math.floor(value / 100)}.${String(value % 100).padStart(2, "0")}`;
}

function centsInput(value) {
  if (!Number.isSafeInteger(value) || value <= 0) return "";
  return `${Math.floor(value / 100)}.${String(value % 100).padStart(2, "0")}`;
}

function displayInvestmentCents(value) {
  if (!Number.isSafeInteger(value) || value <= 0) return "未設定（新規買い停止）";
  return new Intl.NumberFormat("ja-JP", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value / 100);
}

function renderRiskSettings(state) {
  const settings = state.risk_settings && typeof state.risk_settings === "object"
    ? state.risk_settings
    : {};
  text("current-planned-risk", formatBasisPoints(settings.planned_risk_basis_points));
  text("current-daily-limit", formatBasisPoints(settings.daily_loss_limit_basis_points));
  text("current-weekly-limit", formatBasisPoints(settings.weekly_loss_limit_basis_points));
  text("current-investment-limit", displayInvestmentCents(settings.maximum_investment_cents));

  const status = byId("risk-settings-status");
  if (settings.saved === true) {
    status.textContent = settings.entry_blocked
      ? `保存済み r${settings.revision}・新規買い停止`
      : `保存済み r${settings.revision}`;
    status.className = settings.entry_blocked ? "pill pill-wait" : "pill pill-safe";
  } else {
    status.textContent = "既定値・新規買い停止";
    status.className = "pill pill-wait";
  }

  const editable = settings.editable === true;
  const readonly = byId("risk-settings-readonly");
  readonly.hidden = editable;
  const inputs = [
    "risk-target-session",
    "risk-planned-percent",
    "risk-daily-percent",
    "risk-weekly-percent",
    "risk-investment-dollars",
  ];
  inputs.forEach((id) => { byId(id).disabled = !editable; });
  byId("save-risk-settings").disabled = !editable;

  const target = settings.target_session
    || (state.overview && state.overview.target_session)
    || "";
  byId("risk-target-session").value = target;
  byId("risk-planned-percent").value = basisPointsInput(settings.planned_risk_basis_points);
  byId("risk-daily-percent").value = basisPointsInput(settings.daily_loss_limit_basis_points);
  byId("risk-weekly-percent").value = basisPointsInput(settings.weekly_loss_limit_basis_points);
  byId("risk-investment-dollars").value = centsInput(settings.maximum_investment_cents);
}

function exactDecimalUnits(rawValue, label, maximumUnits, allowEmpty = false) {
  const value = String(rawValue || "").trim();
  if (allowEmpty && value === "") return null;
  if (!/^\d+(?:\.\d{1,2})?$/.test(value)) {
    throw new Error(`${label}は小数点以下2桁までで入力してください`);
  }
  const [whole, fraction = ""] = value.split(".");
  const units = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  if (units <= 0n || units > BigInt(maximumUnits)) {
    throw new Error(`${label}が安全範囲を超えています`);
  }
  const result = Number(units);
  if (!Number.isSafeInteger(result)) throw new Error(`${label}が大き過ぎます`);
  return result;
}

function renderJournal(state) {
  const host = byId("journal-list");
  host.replaceChildren();
  const events = Array.isArray(state.journal) ? state.journal : [];
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "panel empty";
    empty.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = "記録はまだありません";
    const description = document.createElement("p");
    description.textContent = "銘柄を確定し、判断が発生すると、日付・銘柄・理由がここに表示されます。";
    empty.append(title, description);
    host.appendChild(empty);
    return;
  }
  events.forEach((event) => {
    const article = document.createElement("article");
    article.className = "panel journal-item";
    const time = document.createElement("time");
    time.textContent = event.session_date || "—";
    const detail = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = actionLabels[String(event.decision || "").toUpperCase()] || event.kind || "記録";
    const paragraph = document.createElement("p");
    paragraph.textContent = Array.isArray(event.reason_codes) ? event.reason_codes.map(explainCode).join(" ／ ") : "";
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
    dt.textContent = systemLabels[label] || label.replaceAll("_", " ");
    dd.textContent = typeof value === "object" ? JSON.stringify(value) : displaySystemValue(label, value);
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
  renderRiskSettings(state);
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
    pill.textContent = "画面データ：正常";
    pill.className = "pill pill-safe";
  } catch (error) {
    const pill = byId("connection-pill");
    pill.textContent = "画面データ：取得できません";
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
    showMessage("選択内容を保存しました。次に「内容を確認」を押してください。");
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
  if (!record || !expected) return showMessage("先に「選択を保存」を押してください。", true);
  if (!csrfToken) return showMessage("安全トークンがありません。画面を更新してください。", true);
  const isArm = action === "arm";
  if (isArm && !window.confirm(`${record.target_session} の分析対象を「${record.selected_symbol || "この日は選ばない"}」に確定します。注文は行いません。続けますか？`)) return;
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
    showMessage(isArm ? "次回の分析対象として確定しました。注文は行われません。" : "安全条件を確認しました。次に「分析対象に確定」を押してください。");
    await refresh();
  } catch (error) {
    showMessage(`更新できません: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function saveRiskSettings(event) {
  event.preventDefault();
  clearMessage();
  const settings = currentState && currentState.risk_settings;
  if (!settings || settings.editable !== true) {
    return showMessage("安全設定は表示専用です。保存先を指定して起動してください。", true);
  }
  if (!csrfToken) return showMessage("安全トークンがありません。画面を更新してください。", true);

  let planned;
  let daily;
  let weekly;
  let maximumInvestment;
  try {
    planned = exactDecimalUnits(byId("risk-planned-percent").value, "1回の予定損失", 100);
    daily = exactDecimalUnits(byId("risk-daily-percent").value, "1日の停止線", 200);
    weekly = exactDecimalUnits(byId("risk-weekly-percent").value, "1週間の停止線", 500);
    maximumInvestment = exactDecimalUnits(
      byId("risk-investment-dollars").value,
      "最大投資額",
      Number.MAX_SAFE_INTEGER,
      true,
    );
  } catch (error) {
    return showMessage(error.message, true);
  }
  if (!(planned <= daily && daily <= weekly)) {
    return showMessage("1回の予定損失 ≤ 1日の停止線 ≤ 1週間の停止線にしてください。", true);
  }
  const targetSession = byId("risk-target-session").value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(targetSession)) {
    return showMessage("適用する対象日を選んでください。", true);
  }
  const capText = maximumInvestment == null
    ? "最大投資額は未設定（新規買い停止）"
    : `最大投資額は ${displayInvestmentCents(maximumInvestment)}（買い代金 + 買い手数料）`;
  if (!window.confirm(`${targetSession} から安全設定を適用します。${capText}です。注文機能は有効になりません。保存しますか？`)) return;

  const button = byId("save-risk-settings");
  button.disabled = true;
  try {
    const response = await fetch("/api/risk-settings", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({
        confirmation: "SAVE_NEXT_SESSION_RISK",
        daily_loss_limit_basis_points: daily,
        expected_sha256: settings.sha256 || null,
        maximum_investment_cents: maximumInvestment,
        planned_risk_basis_points: planned,
        risk_policy_version: "RSI_RISK_POLICY_V2",
        target_session: targetSession,
        weekly_loss_limit_basis_points: weekly,
      }),
    });
    await readJson(response);
    showMessage("次回セッション用の安全設定を保存しました。注文機能は停止したままです。");
    await refresh();
  } catch (error) {
    showMessage(`安全設定を保存できません: ${error.message}`, true);
  } finally {
    const editable = currentState && currentState.risk_settings
      && currentState.risk_settings.editable === true;
    button.disabled = !editable;
  }
}

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", () => activateSection(link.dataset.section));
});
document.querySelectorAll("[data-go]").forEach((link) => {
  link.addEventListener("click", () => activateSection(link.dataset.go));
});
window.addEventListener("hashchange", () => activateSection(location.hash.slice(1)));
byId("refresh-button").addEventListener("click", () => { clearMessage(); refresh(); });
byId("save-selection").addEventListener("click", saveSelection);
byId("validate-selection").addEventListener("click", () => advanceSelection("validate"));
byId("arm-selection").addEventListener("click", () => advanceSelection("arm"));
byId("risk-settings-form").addEventListener("submit", saveRiskSettings);

activateSection(location.hash.slice(1));
refresh();
