"use strict";
// SALU desktop UI. Talks to the local SALU server with the per-launch token.

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

// ---- token: from ?t= on first load, then kept for this window only ----
const params = new URLSearchParams(location.search);
let TOKEN = params.get("t") || "";
try {
  if (TOKEN) sessionStorage.setItem("salu_t", TOKEN); else TOKEN = sessionStorage.getItem("salu_t") || "";
} catch (_) { /* storage unavailable: token stays in memory */ }
if (params.has("t")) history.replaceState(null, "", location.pathname);

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "X-Salu-Token": TOKEN, ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

// ---- text rendering (escape first, then a small markdown subset) ----
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inline(s) {
  return s.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}
function md(src) {
  const blocks = [];
  let text = esc(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => { blocks.push(code); return `\u0000${blocks.length - 1}\u0000`; });
  const out = [];
  let list = null, table = [];
  const flushList = () => { if (list) { out.push(`<${list.t}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map((r) => r.replace(/^\s*\||\|\s*$/g, "").split("|").map((c) => inline(c.trim())));
    out.push("<table>" + rows.map((r, i) => `<tr>${r.map((c) => i ? `<td>${c}</td>` : `<th>${c}</th>`).join("")}</tr>`).join("") + "</table>");
    table = [];
  };
  for (const line of text.split("\n")) {
    let m;
    if (/^\s*\|.*\|\s*$/.test(line)) { flushList(); table.push(line); continue; } else flushTable();
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (!list || list.t !== "ul") { flushList(); list = { t: "ul", items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (!list || list.t !== "ol") { flushList(); list = { t: "ol", items: [] }; } list.items.push(m[1]); continue; }
    flushList();
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { out.push(`<h3>${inline(m[1])}</h3>`); continue; }
    if (/^\u0000\d+\u0000$/.test(line.trim())) { out.push(line.trim()); continue; }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  flushList(); flushTable();
  return out.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><code>${blocks[+i]}</code></pre>`);
}

// ---- views ----
$$(".rail-btn").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function showView(name) {
  $$(".rail-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  ({ tasks: loadTasks, memory: loadMemory, tools: loadTools, logs: loadLogs, settings: loadSettings }[name] || (() => {}))();
}

// ---- boot sequence (real checks from the server) ----
const greetingFor = (name) => {
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  return `${part}${name ? ", " + name : ""}. How can I assist you?`;
};
let STATE = null;

async function boot() {
  let state;
  for (let i = 0; i < 60; i++) {
    try { state = await api("/api/state"); if (state.checks.length) break; } catch (e) {
      if (String(e).includes("401")) { $("#boot-status").innerHTML = '<span class="dot bad"></span> Session token missing — start SALU with <code>python -m salu</code>.'; return; }
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!state) { $("#boot-status").innerHTML = '<span class="dot bad"></span> SALU server not reachable.'; return; }
  STATE = state;
  const ul = $("#boot-checks");
  for (const c of state.checks) {
    const li = document.createElement("li");
    li.className = c.ok ? "" : "failed";
    li.innerHTML = `<span class="mk ${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✗"}</span><span>${esc(c.name)}</span><span class="dt">${esc(c.detail)}</span>`;
    ul.appendChild(li);
    await new Promise((r) => setTimeout(r, 170));
    li.classList.add("in");
  }
  $("#boot-status").innerHTML = state.online ? '<span class="dot ok"></span> All systems checked' : '<span class="dot warn"></span> Some checks failed';
  const greet = greetingFor(state.user_name);
  $("#boot-result").innerHTML = state.online
    ? `<div class="big ok">SYSTEM ONLINE</div><div class="greet">"${esc(greet)}"</div>`
    : `<div class="big bad">SYSTEM DEGRADED</div><div class="greet muted">Fix the failed checks above, then restart SALU.</div>`;
  $("#greeting").textContent = greet;
  applyState(state);
  const finish = () => { $("#boot").classList.add("fade"); setTimeout(() => $("#boot").remove(), 700); $("#input").focus(); };
  if (state.online) setTimeout(finish, 1500);
  else { $("#boot-continue").classList.remove("hidden"); $("#boot-continue").onclick = finish; }
  connect();
}

function applyState(s) {
  const pill = $("#sys-pill");
  pill.querySelector(".dot").className = "dot " + (s.online ? "ok" : "warn");
  $("#sys-pill-text").textContent = s.online ? "SYSTEM ONLINE" : "DEGRADED";
  const b = s.brain || {};
  $("#brain-info").innerHTML = [
    b.cli_version && `claude code ${esc(b.cli_version)}`,
    b.auth_mode && `auth: ${esc(b.auth_mode)}${b.subscription ? " (" + esc(b.subscription) + ")" : ""}`,
    b.model && `model: ${esc(b.model)}`,
  ].filter(Boolean).join("<br>");
  if (!busy) setAgentState(s.agent?.state || "idle");
}

// ---- websocket ----
let ws = null, busy = false, retry = 0;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { retry = 0; if (STATE) applyState(STATE); };
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
  ws.onclose = (e) => {
    $("#sys-pill .dot").className = "dot bad"; $("#sys-pill-text").textContent = "DISCONNECTED";
    setBusy(false); hideConfirm();
    if (e.code === 4000) { $("#sys-pill-text").textContent = "OPENED IN ANOTHER WINDOW"; return; }
    if (e.code !== 4401) setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
  };
}

const transcript = $("#transcript");
let thinkingEl = null;
const toolCards = [];

function scrollDown() { transcript.scrollTop = transcript.scrollHeight; }
function addMsg(kind, html, who) {
  $("#welcome")?.remove();
  removeThinking();
  const d = document.createElement("div");
  d.className = `msg ${kind}`;
  d.innerHTML = `<div class="who">${who}</div><div class="body">${html}</div>`;
  transcript.appendChild(d); scrollDown();
  return d;
}
function showThinking(text) {
  if (!thinkingEl) { thinkingEl = document.createElement("div"); thinkingEl.className = "thinking"; transcript.appendChild(thinkingEl); }
  thinkingEl.innerHTML = `<span class="spin"></span>${esc(text)}`; transcript.appendChild(thinkingEl); scrollDown();
}
function removeThinking() { thinkingEl?.remove(); thinkingEl = null; }

function handleEvent(ev) {
  switch (ev.type) {
    case "task_started": setBusy(true); $("#current-task").textContent = ev.request; $("#current-task").classList.remove("muted"); break;
    case "status": setAgentState(ev.state, ev.detail); if (busy && ev.state !== "idle") showThinking(ev.detail || ev.state); break;
    case "text": addMsg("salu", md(ev.text), "SALU"); break;
    case "tool_call": {
      removeThinking();
      const d = document.createElement("details");
      d.className = "toolcard";
      d.innerHTML = `<summary><span class="spin"></span><span class="tname">${esc(ev.tool)}</span><span class="risk ${esc(ev.risk)}">${esc(ev.risk)}</span><span class="tstate">${ev.needs_approval ? "awaiting confirmation" : "running"}</span></summary><pre>${esc(JSON.stringify(ev.args, null, 1))}</pre>`;
      $("#welcome")?.remove(); transcript.appendChild(d); scrollDown();
      toolCards.push({ tool: ev.tool, el: d });
      break;
    }
    case "tool_result": {
      const i = toolCards.findIndex((c) => c.tool === ev.tool);
      let el = i >= 0 ? toolCards.splice(i, 1)[0].el : null;
      if (!el) { el = document.createElement("details"); el.className = "toolcard"; el.innerHTML = `<summary><span class="tname">${esc(ev.tool)}</span><span class="tstate"></span></summary>`; transcript.appendChild(el); }
      el.classList.add(ev.ok ? "ok" : "fail");
      el.querySelector(".spin")?.remove();
      el.querySelector(".tstate").textContent = ev.ok ? `✓ ${ev.duration}s` : `✗ ${ev.stage === "denied" ? "not approved" : "failed"}`;
      const pre = document.createElement("pre"); pre.textContent = ev.output || "(no output)"; el.appendChild(pre);
      for (const src of ev.images || []) {
        if (!String(src).startsWith("data:image/")) continue;
        const img = document.createElement("img"); img.className = "shot"; img.src = src; img.alt = "Screenshot";
        el.appendChild(img); el.open = true;
      }
      if (!ev.ok) el.open = true;
      scrollDown();
      break;
    }
    case "approval_required": showConfirm(ev); break;
    case "approval_resolved": hideConfirm(ev.id); break;
    case "error": addMsg("error", esc(`Task failed.\n\n${ev.message}`), "SALU · ERROR"); break;
    case "task_finished":
      removeThinking(); setBusy(false);
      $("#current-task").textContent = "None"; $("#current-task").classList.add("muted");
      loadRecent();
      break;
    case "session_reset": transcript.innerHTML = ""; addMsg("salu", "<p>New conversation started.</p>", "SALU"); break;
  }
}

function setAgentState(state, detail) {
  $("#agent-state").textContent = state.replace("_", " ");
  $("#agent-dot").className = "dot " + ({ idle: "ok", thinking: "busy", executing: "busy", awaiting_approval: "warn" }[state] || "");
}
function setBusy(b) {
  busy = b;
  $("#send").disabled = b; $("#stop").classList.toggle("hidden", !b);
  if (!b) { setAgentState("idle"); removeThinking(); }
}

// ---- composer ----
const input = $("#input");
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(180, input.scrollHeight) + "px"; });
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy || !ws || ws.readyState !== 1) return;
  addMsg("user", esc(text), "YOU");
  ws.send(JSON.stringify({ type: "chat", text }));
  input.value = ""; input.style.height = "auto"; setBusy(true); showThinking("Planning");
});
$("#stop").addEventListener("click", () => ws?.send(JSON.stringify({ type: "interrupt" })));
$$(".chip").forEach((c) => c.addEventListener("click", () => { input.value = c.dataset.prompt; $("#composer").requestSubmit(); }));

// ---- confirmation dialog ----
let confirmId = null, confirmTimer = null;
function showConfirm(req) {
  confirmId = req.id;
  $("#confirm-risk").className = `risk ${req.risk}`; $("#confirm-risk").textContent = req.risk;
  $("#confirm-title").textContent = "SALU CONFIRMATION";
  $("#confirm-summary").textContent = req.summary;
  $("#confirm-tool").textContent = req.tool;
  $("#confirm-details").innerHTML = req.details.map((d) => `<li>${esc(d)}</li>`).join("");
  $("#confirm-proceed").classList.toggle("high", req.risk === "high");
  $("#confirm").classList.remove("hidden");
  const bar = $("#confirm-timer"), total = 120000, start = Date.now();
  clearInterval(confirmTimer);
  confirmTimer = setInterval(() => { const left = Math.max(0, 1 - (Date.now() - start) / total); bar.style.width = left * 100 + "%"; if (!left) hideConfirm(); }, 250);
  $("#confirm-cancel").focus();
}
function hideConfirm(id) {
  if (id && id !== confirmId) return;
  clearInterval(confirmTimer); confirmId = null; $("#confirm").classList.add("hidden");
}
function answer(ok) { if (confirmId) ws?.send(JSON.stringify({ type: "approve", id: confirmId, approved: ok })); hideConfirm(); }
$("#confirm-cancel").onclick = () => answer(false);
$("#confirm-proceed").onclick = () => answer(true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && confirmId) answer(false); });

// ---- side panel ----
async function loadStats() {
  try {
    const s = await api("/api/stats");
    const set = (id, v) => { $(`#${id}-bar`).style.width = v + "%"; $(`#${id}-bar`).style.background = v > 85 ? "var(--bad)" : v > 65 ? "var(--warn)" : "var(--accent)"; $(`#${id}-val`).textContent = Math.round(v) + "%"; };
    set("cpu", s.cpu_percent); set("ram", s.ram_percent);
    $("#ram-val").title = `${s.ram_used_gb} / ${s.ram_total_gb} GB`;
    $("#net-val").innerHTML = s.network_online ? '<span class="dot ok"></span>ONLINE' : '<span class="dot bad"></span>OFFLINE';
  } catch (_) { /* server restarting */ }
}
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? "now" : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h` : `${Math.floor(s / 86400)}d`; };
const mark = { done: "✓", failed: "✗", running: "•", cancelled: "–" };
async function loadRecent() {
  try {
    const tasks = await api("/api/tasks?limit=12");
    $("#recent-tasks").innerHTML = tasks.map((t) => `<li title="${esc(t.summary || "")}"><span class="st ${t.status}">${mark[t.status] || "?"}</span><span class="rq">${esc(t.request)}</span></li>`).join("") || '<li class="muted">No tasks yet</li>';
  } catch (_) {}
}
async function loadTasks() {
  const tasks = await api("/api/tasks?limit=100");
  $("#tasks-list").innerHTML = tasks.map((t) => `<div class="row"><span class="t"><span class="st ${t.status}">${mark[t.status] || ""}</span> ${esc(t.request)}</span><span class="m">${ago(t.started_at)} ago · ${esc(t.status)}</span><span class="s">${esc(t.summary || "")}${t.tools_used.length ? " · tools: " + esc(t.tools_used.join(", ")) : ""}</span></div>`).join("") || '<p class="muted">No tasks yet.</p>';
}
async function loadMemory() {
  const rows = await api("/api/memory");
  $("#memory-list").innerHTML = rows.map((m) => `<div class="row"><span class="t mono">${esc(m.key)}</span><span class="m">${esc(m.category)} <button class="btn ghost sm" data-forget="${esc(m.key)}">Forget</button></span><span class="s">${esc(m.value)}</span></div>`).join("") || '<p class="muted">Nothing remembered yet. Try: "Remember that my main project is result-system."</p>';
  $$("[data-forget]").forEach((b) => b.onclick = async () => { if (confirm(`Forget "${b.dataset.forget}"?`)) { await api(`/api/memory/${encodeURIComponent(b.dataset.forget)}`, { method: "DELETE" }); loadMemory(); } });
}
async function loadTools() {
  const s = await api("/api/state");
  $("#tools-list").innerHTML = s.tools.map((t) => `<div class="row"><span class="t mono">${esc(t.name)}</span><span class="m"><span class="risk ${t.risk}">${t.risk}</span> ${esc(t.category)}</span><span class="s">${esc(t.description)}</span></div>`).join("");
}
async function loadLogs() { const lines = await api("/api/logs?limit=400"); const box = $("#logs-box"); box.textContent = lines.join("\n"); box.scrollTop = box.scrollHeight; }
$("#logs-refresh").onclick = loadLogs;
async function loadSettings() { $("#settings-box").textContent = JSON.stringify(await api("/api/settings"), null, 2); }

boot();
loadStats(); loadRecent();
setInterval(loadStats, 3000);
