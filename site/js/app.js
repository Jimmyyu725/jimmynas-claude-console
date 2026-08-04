// Claude Console - session picker in front of the ttyd web terminal.
// The terminal itself lives at TERM_BASE; we open a session by attaching the
// ttyd client-supplied arg (?arg=<label>) that claude-web-term turns into the
// tmux session name. The picker talks to the local console backend at /api.
"use strict";

const TERM_BASE = "/t-jx9mQ4wRk7/";
const $ = (s, r = document) => r.querySelector(s);

// Inline icons so they render regardless of the system font.
const ICON_PAUSE = '<svg class="i" viewBox="0 0 12 14"><rect x="1" y="1" width="3.2" height="12" rx="1"/><rect x="7.8" y="1" width="3.2" height="12" rx="1"/></svg>';
const ICON_PLAY = '<svg class="i" viewBox="0 0 12 14"><path d="M2 1 11 7 2 13Z"/></svg>';
const ICON_BOLT = '<svg class="i" viewBox="0 0 12 14"><path d="M7 1 2 8h3l-1 5 6-8H7z"/></svg>';

const cards = $("#cards");
const emptyMsg = $("#empty");
const errBox = $("#err");
const frame = $("#frame");
let current = null;       // label of the session shown in the terminal view
let pollTimer = null;

// ---- helpers ---------------------------------------------------------------
function termUrl(label) {
  return label ? `${TERM_BASE}?arg=${encodeURIComponent(label)}` : TERM_BASE;
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function ago(epoch) {
  if (!epoch) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - epoch);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function toast(msg, bad) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  $("#toast-root").appendChild(t);
  setTimeout(() => t.remove(), 3200);
}
async function api(path, opts) {
  const r = await fetch("/api" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// ---- session list ----------------------------------------------------------
async function load() {
  try {
    const { sessions } = await api("/sessions");
    errBox.classList.add("hidden");
    render(sessions);
  } catch (e) {
    errBox.textContent = "Cannot reach the console backend: " + e.message;
    errBox.classList.remove("hidden");
  }
}

function render(sessions) {
  emptyMsg.classList.toggle("hidden", sessions.length > 0);
  cards.innerHTML = sessions.map(s => {
    const paused = s.status === "paused";
    const remote = s.remote;
    return `
    <div class="card ${paused ? "is-paused" : ""}" data-label="${esc(s.label)}" data-status="${s.status}" data-main="${s.isMain}">
      <div class="head">
        <span class="name" data-act="enter" title="Open this conversation in the terminal">${esc(s.display)}</span>
        <span class="status">status:<b class="${paused ? "amber" : "green"}">${paused ? "Paused" : "Running"}</b>
          <span class="dot ${paused ? "amber" : "green"}"></span></span>
      </div>
      <div class="row">
        <button class="btn pause" data-act="pause">${paused ? ICON_PLAY + " Resume" : ICON_PAUSE + " Pause"}</button>
        <button class="btn danger" data-act="stop" title="End and remove this conversation">Delete</button>
      </div>
      <div class="foot">
        <div class="toggles">
          <button class="remote-toggle ${remote ? "on" : ""}" data-act="remote"
            title="Run /remote-control in this session and open the terminal to finish pairing">
            Remote-control:<b>${remote ? "ON" : "OFF"}</b>
            <span class="ricon">${remote ? ICON_PAUSE : ICON_PLAY}</span>
          </button>
          <button class="fast-toggle ${s.fast ? "on" : ""}" data-act="fast"
            title="Toggle Fast mode (/fast) for this session — $10/$50 per Mtok">
            ${ICON_BOLT} Fast:<b>${s.fast ? "ON" : "OFF"}</b>
          </button>
        </div>
        <button class="enter-link" data-act="enter">Open terminal &#8594;</button>
      </div>
    </div>`;
  }).join("");
}

// ---- terminal view ----------------------------------------------------------
function openTerminal(label) {
  current = label;
  $("#term-name").textContent = label ? "claude-" + label : "main";
  frame.src = termUrl(label);
  $("#picker").classList.add("hidden");
  $("#term").classList.remove("hidden");
}
function closeTerminal() {
  frame.src = "about:blank";
  current = null;
  $("#term").classList.add("hidden");
  $("#picker").classList.remove("hidden");
  load();
}

// ---- actions ----------------------------------------------------------------
async function stopSession(label) {
  const who = label ? "claude-" + label : "the main session";
  // Custom modal, NOT window.confirm() — mobile/in-app browsers often suppress
  // native dialogs (they silently return false), which made Delete look broken.
  if (!(await confirmDialog("Delete session",
        `Delete ${who}? The conversation in it will be ended.`, "Delete", true))) return;
  try {
    await api("/sessions/" + encodeURIComponent(label || "main"), { method: "DELETE" });
    toast("Deleted " + who);
    if (current === label) closeTerminal(); else load();
  } catch (e) { toast(e.message, true); }
}

async function togglePause(label, paused, isMain) {
  const action = paused ? "resume" : "pause";
  // Pausing the main session freezes the terminal you're likely using now.
  if (!paused && isMain &&
      !(await confirmDialog("Pause the main session?",
        "It freezes the Claude you may be using right now (0 CPU). It stays " +
        "frozen until you Resume it here.", "Pause", false))) return;
  try {
    await api("/sessions/" + encodeURIComponent(label || "main") + "/" + action, { method: "POST" });
    toast(paused ? "Resumed" : "Paused (frozen)");
    load();
  } catch (e) { toast(e.message, true); }
}
async function remoteSession(label) {
  try {
    await api("/sessions/" + encodeURIComponent(label || "main") + "/remote", { method: "POST" });
    toast("Sent /remote-control — open the terminal for the pairing link");
    openTerminal(label);
  } catch (e) { toast(e.message, true); }
}
// One-click Fast toggle: flips /fast for the session and reads the new state back.
async function fastSession(label) {
  try {
    const r = await api("/sessions/" + encodeURIComponent(label || "main") + "/fast", { method: "POST" });
    toast("Fast mode " + (r.fast ? "ON ⚡ ($10/$50 per Mtok)" : "OFF"));
    load();
  } catch (e) { toast(e.message, true); }
}
async function createSession(mode) {
  const name = await nameDialog(mode);
  if (name === null) return;          // cancelled
  try {
    const { label } = await api("/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, name }),
    });
    openTerminal(label);
  } catch (e) { toast(e.message, true); }
}

// ---- confirm dialog (replaces window.confirm, which mobile browsers block) --
function confirmDialog(title, msg, okLabel, danger) {
  return new Promise(resolve => {
    const root = $("#modal-root");
    root.innerHTML = `
      <div class="scrim">
        <div class="dialog">
          <h2>${esc(title)}</h2>
          <p>${esc(msg)}</p>
          <div class="row">
            <button class="btn ghost" data-x>Cancel</button>
            <button class="btn ${danger ? "danger solid" : "primary"}" data-ok>${esc(okLabel)}</button>
          </div>
        </div>
      </div>`;
    const done = v => { root.innerHTML = ""; resolve(v); };
    $("[data-ok]", root).onclick = () => done(true);
    $("[data-x]", root).onclick = () => done(false);
    $(".scrim", root).onclick = e => { if (e.target.classList.contains("scrim")) done(false); };
  });
}

// ---- name dialog ------------------------------------------------------------
function nameDialog(mode) {
  return new Promise(resolve => {
    const title = mode === "continue" ? "Continue last conversation" : "New session";
    const hint = mode === "continue"
      ? "Resumes the most recent Claude conversation in /srv/appdata, in a fresh named session. Leave blank to auto-name."
      : "Optional name (letters, digits, - or _). Leave blank to auto-name (2, 3, …).";
    const root = $("#modal-root");
    root.innerHTML = `
      <div class="scrim">
        <div class="dialog">
          <h2>${title}</h2>
          <p>${hint}</p>
          <input id="dlg-name" placeholder="e.g. flight, work" maxlength="24" autocomplete="off">
          <div class="row">
            <button class="btn ghost" data-x>Cancel</button>
            <button class="btn primary" data-ok>${mode === "continue" ? "Continue" : "Create"}</button>
          </div>
        </div>
      </div>`;
    const input = $("#dlg-name", root);
    const done = v => { root.innerHTML = ""; resolve(v); };
    input.focus();
    input.addEventListener("keydown", e => {
      if (e.key === "Enter") done(input.value.trim());
      if (e.key === "Escape") done(null);
    });
    $("[data-ok]", root).onclick = () => done(input.value.trim());
    $("[data-x]", root).onclick = () => done(null);
    $(".scrim", root).onclick = e => { if (e.target.classList.contains("scrim")) done(null); };
  });
}

// ---- wiring -----------------------------------------------------------------
cards.addEventListener("click", e => {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const card = el.closest(".card");
  const label = card.dataset.label;
  const act = el.dataset.act;
  if (act === "enter") openTerminal(label);
  else if (act === "remote") remoteSession(label);
  else if (act === "stop") stopSession(label);
  else if (act === "fast") fastSession(label);
  else if (act === "pause")
    togglePause(label, card.dataset.status === "paused", card.dataset.main === "true");
});
$("#new-btn").onclick = () => createSession("new");
$("#continue-btn").onclick = () => createSession("continue");
$("#refresh-btn").onclick = load;
$("#back-btn").onclick = closeTerminal;
$("#term-stop").onclick = () => stopSession(current);
$("#term-remote").onclick = () => remoteSession(current);

// Poll the list while the picker is visible.
load();
pollTimer = setInterval(() => {
  if (!$("#picker").classList.contains("hidden")) load();
}, 4000);
