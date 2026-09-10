/* Teleporter frontend — vanilla JS, no dependencies. */
"use strict";

// ------------------------------------------------------------------ state
const S = {
  token: localStorage.getItem("tp_token") || "",
  boot: null,
  view: "ops",
  kind: null,
  source: null,          // resolved chat dict {id,title,type,...} or {raw:"text"}
  dest: null,
  filters: new Set(["all"]),
  jobId: null,
  job: null,
  pollTimer: null,
  histTimer: null,
  mapDraft: null,        // topic mapping draft
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ------------------------------------------------------------------ helpers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtSize(n) {
  n = Number(n || 0);
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + " " + u[i];
}
function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}
function fmtDur(sec) {
  sec = Math.max(1, Math.round(sec));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.round(sec / 60) + "m";
  return (sec / 3600).toFixed(1) + "h";
}
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5200);
}
// ---------- global activity bar ("heartbeat") ----------
let netCount = 0, netTimer = null;
function netStart() {
  netCount++;
  const el = $("#netbar");
  el.classList.add("on");
  if (netTimer) return;
  let w = 10;
  el.style.width = w + "%";
  netTimer = setInterval(() => {
    if (netCount > 0 && w < 90) { w += (90 - w) * 0.09; el.style.width = w + "%"; }
  }, 130);
}
function netDone() {
  netCount = Math.max(0, netCount - 1);
  if (netCount > 0) return;
  if (netTimer) { clearInterval(netTimer); netTimer = null; }
  const el = $("#netbar");
  el.style.width = "100%";
  setTimeout(() => {
    el.classList.remove("on");
    setTimeout(() => { el.style.width = "0%"; }, 300);
  }, 220);
}

// ---------- busy state for buttons ----------
function setBusy(btn, on, label) {
  if (!btn) return;
  if (on) {
    if (btn.dataset.orig === undefined) btn.dataset.orig = btn.innerHTML;
    btn.classList.add("busy");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner"></span>${esc(label || "Working…")}`;
  } else {
    btn.classList.remove("busy");
    btn.disabled = false;
    if (btn.dataset.orig !== undefined) btn.innerHTML = btn.dataset.orig;
  }
}

async function api(path, method = "GET", body, opts = {}) {
  if (!opts.quiet) netStart();
  const req = { method, headers: {} };
  if (S.token) req.headers["X-App-Token"] = S.token;
  if (body !== undefined) {
    req.headers["Content-Type"] = "application/json";
    req.body = JSON.stringify(body);
  }
  try {
    const r = await fetch("/api" + path, req);
    if (r.status === 401) { showPasswordModal(); throw new Error("unauthorized"); }
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty */ }
    if (!r.ok) {
      let detail = data.detail;
      if (typeof detail !== "string") {
        try {
          detail = Array.isArray(detail)
            ? detail.map((x) => (x && x.msg) || JSON.stringify(x)).join(" · ")
            : JSON.stringify(detail);
        } catch (_) { detail = String(detail); }
      }
      throw new Error(detail || r.statusText || "Request failed");
    }
    return data;
  } finally {
    if (!opts.quiet) netDone();
  }
}
function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// ------------------------------------------------------------------ navigation
function setStep(n) {
  $$("#stepper .step").forEach((el) => {
    const k = Number(el.dataset.step);
    el.classList.toggle("active", k === n);
    el.classList.toggle("done", k < n);
  });
}
function showView(name) {
  S.view = name;
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $("#view-" + name).classList.remove("hidden");
  const stepMap = { ops: 1, auth: 2, setup: 3, job: 4, history: 0 };
  setStep(stepMap[name] ?? 0);
  $$(".tab").forEach((t) => t.classList.toggle("active",
    (t.dataset.nav === "history") === (name === "history")));
  if (name === "history") refreshHistory(); else stopHistoryPoll();
}
function updateConnBadge() {
  const b = $("#conn-badge");
  if (S.boot && S.boot.authenticated && S.boot.profile) {
    b.className = "badge badge-ok";
    const p = S.boot.profile;
    b.textContent = "✓ " + (p.username ? "@" + p.username : p.name);
  } else {
    b.className = "badge badge-off";
    b.textContent = "Not connected";
  }
  // home-screen connect banner
  const banner = $("#connect-banner");
  if (!banner) return;
  const txt = $("#connect-banner-text");
  const cta = $("#connect-cta");
  if (S.boot && S.boot.authenticated && S.boot.profile) {
    banner.classList.add("ok");
    txt.textContent = "✅ Connected as " + S.boot.profile.name +
      (S.boot.profile.username ? " (@" + S.boot.profile.username + ")" : "") + " — now pick an operation below";
    cta.textContent = "Account settings";
  } else {
    banner.classList.remove("ok");
    txt.textContent = "🔌 You're not connected to a Telegram account yet";
    cta.textContent = "Connect account →";
  }
}

// ------------------------------------------------------------------ bootstrap & password
async function boot() {
  try {
    S.boot = await api("/bootstrap");
  } catch (e) { return; }
  if (S.boot.need_password) { showPasswordModal(); return; }
  $("#pwd-modal").classList.add("hidden");
  updateConnBadge();
  renderOps();
  showView("ops");
}
function showPasswordModal() {
  $("#pwd-modal").classList.remove("hidden");
  $("#pwd-input").focus();
}
$("#pwd-submit").addEventListener("click", async () => {
  const v = $("#pwd-input").value.trim();
  if (!v) return;
  S.token = v; localStorage.setItem("tp_token", v);
  try {
    const r = await fetch("/api/bootstrap", { headers: { "X-App-Token": v } });
    const d = await r.json();
    if (d.need_password) { $("#pwd-error").textContent = "Wrong password"; S.token = ""; return; }
    $("#pwd-modal").classList.add("hidden");
    boot();
  } catch (_) { $("#pwd-error").textContent = "Wrong password"; }
});
$("#pwd-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#pwd-submit").click(); });

// ------------------------------------------------------------------ ops view
document.addEventListener("click", (e) => {
  if (e.target.closest("#connect-cta") || e.target.closest("#conn-badge")) {
    showAuthView();
  }
});
const OP_META = {
  c2c:   { icon: "📢➜📢", desc: "Copy everything from one channel straight into another. No indexing." },
  c2g:   { icon: "📢➜👥", desc: "Copy channel content into a group chat." },
  g2c:   { icon: "👥➜📢", desc: "Copy group content into a channel." },
  g2g:   { icon: "👥➜👥", desc: "Copy content between two groups." },
  c2t:   { icon: "📢➜🗂️", desc: "Index the channel, classify posts, and file them into forum topics.", tag: "INDEXES" },
  g2t:   { icon: "👥➜🗂️", desc: "Index the group, classify posts, and file them into forum topics.", tag: "INDEXES" },
  index: { icon: "🔍", desc: "Scan a channel or group and generate a detailed HTML report. Nothing is copied.", tag: "NO MIGRATION" },
};
function renderOps() {
  const grid = $("#ops-grid");
  grid.innerHTML = "";
  const kinds = (S.boot && S.boot.kinds) || {};
  for (const [key, meta] of Object.entries(OP_META)) {
    const info = kinds[key] || {};
    const card = document.createElement("div");
    card.className = "op-card";
    card.innerHTML = `
      ${meta.tag ? `<span class="op-tag">${meta.tag}</span>` : ""}
      <div class="op-icon">${meta.icon}</div>
      <h3>${esc(info.label || key)}</h3>
      <p>${esc(meta.desc)}</p>`;
    card.addEventListener("click", () => {
      S.kind = key; S.source = null; S.dest = null;
      if (S.boot.authenticated) goSetup(); else showAuthView();
    });
    grid.appendChild(card);
  }
}
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  if (t.dataset.nav === "history") showView("history");
  else showView(S.jobId ? "job" : "ops");
}));
document.addEventListener("click", (e) => {
  if (e.target.matches("[data-goto-ops]")) showView("ops");
});

// ------------------------------------------------------------------ auth view
function showAuthView() { showView("auth"); renderAuth(); }

function renderAuth() {
  const body = $("#auth-body");
  const cont = $("#auth-continue");
  if (S.boot.authenticated && S.boot.profile) {
    const p = S.boot.profile;
    const initials = (p.name || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    body.innerHTML = `
      <div class="panel profile-card">
        <div class="avatar">${esc(initials)}</div>
        <div class="grow">
          <div style="font-weight:700;font-size:17px">Logged in as ${esc(p.name)}
            ${p.username ? `<span class="muted">@${esc(p.username)}</span>` : ""}</div>
          <div class="profile-meta">ID: <code>${p.id}</code>
            ${p.phone_number ? " · Phone: <code>+" + esc(p.phone_number) + "</code>" : ""}
            ${p.is_premium ? " · ⭐ Premium" : ""}${p.is_bot ? " · ⚠️ This is a bot account" : ""}</div>
        </div>
        <button class="btn danger" id="disconnect-btn">Disconnect</button>
      </div>`;
    $("#disconnect-btn").addEventListener("click", async () => {
      if (!confirm("Disconnect and forget the saved session on this server?")) return;
      const btn = $("#disconnect-btn"); setBusy(btn, true, "Disconnecting…");
      try { await api("/auth/disconnect", "POST"); } catch (e) { toast(e.message, "err"); }
      S.boot.authenticated = false; S.boot.profile = null;
      updateConnBadge(); renderAuth(); toast("Disconnected", "ok");
    });
    cont.disabled = false;
    $("#auth-hint").textContent = "Connected ✓";
    return;
  }
  cont.disabled = true;
  $("#auth-hint").textContent = "Connect to continue";
  const d = (S.boot && S.boot.defaults) || {};
  body.innerHTML = `
  <div class="panel">
    <p class="muted small" style="margin:0 0 12px">👇 Pick one of the two options. <b>First time here? Use "Generate"</b> — it creates the session string for you from your phone number, no string needed.</p>
    <div class="auth-tabs">
      <button class="btn" id="tab-session">🔑 I have a Session String</button>
      <button class="btn ghost" id="tab-generate">✨ Generate a new Session String (recommended)</button>
    </div>

    <div id="form-session">
      <p class="muted small" style="margin-top:0">Get your <b>API ID</b> and <b>API Hash</b> from
      <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a> → API development tools.
      Paste an existing session string to connect instantly.</p>
      <div class="row2">
        <div><label class="lbl">API ID</label><input type="text" id="s-api-id" placeholder="e.g. 1234567" value="${esc(d.api_id || "")}"></div>
        <div><label class="lbl">API Hash</label><input type="text" id="s-api-hash" placeholder="e.g. 5f4dcc3b5aa765d61d8327deb882cf99" value="${esc(d.api_hash || "")}"></div>
      </div>
      <label class="lbl">Session String</label>
      <textarea id="s-session" placeholder="Paste your Telegram session string…"></textarea>
      <div class="wizard-nav">
        <span id="s-error" class="error"></span>
        <button class="btn primary" id="s-connect">🔌 Connect</button>
      </div>
    </div>

    <div id="form-generate" class="hidden">
      <p class="muted small" style="margin-top:0">No session string? Generate one now: enter your phone number,
      then the code Telegram sends you (and your 2FA password if enabled). The string is saved on this server
      and shown once so you can keep a copy.</p>
      <div class="row2">
        <div><label class="lbl">API ID</label><input type="text" id="g-api-id" placeholder="e.g. 1234567" value="${esc(d.api_id || "")}"></div>
        <div><label class="lbl">API Hash</label><input type="text" id="g-api-hash" placeholder="e.g. 5f4dcc3b5aa765d61d8327deb882cf99" value="${esc(d.api_hash || "")}"></div>
      </div>
      <div id="gen-step-1">
        <label class="lbl">Phone number (international format)</label>
        <input type="text" id="g-phone" placeholder="+919812345678">
        <div class="wizard-nav">
          <span id="g-error" class="error"></span>
          <button class="btn primary" id="g-send-code">Send code</button>
        </div>
      </div>
      <div id="gen-step-2" class="hidden">
        <label class="lbl">Login code from Telegram</label>
        <input type="text" id="g-code" placeholder="12345" autocomplete="one-time-code">
        <div id="g-2fa-wrap" class="hidden">
          <label class="lbl">Two-Step Verification password</label>
          <input type="password" id="g-password" placeholder="Your 2FA password">
        </div>
        <div class="wizard-nav">
          <span id="g-error-2" class="error"></span>
          <div>
            <button class="btn ghost" id="g-restart">← Start over</button>
            <button class="btn primary" id="g-verify">Verify &amp; connect</button>
          </div>
        </div>
      </div>
      <div id="gen-step-3" class="hidden">
        <p style="margin-bottom:6px">✅ <b>Connected!</b> Save this session string somewhere safe — it logs into your account:</p>
        <div class="session-box" id="g-session-out"></div>
        <div class="wizard-nav"><span></span><button class="btn" id="g-copy">📋 Copy string</button></div>
      </div>
    </div>
  </div>`;

  const tabS = $("#tab-session"), tabG = $("#tab-generate");
  const switchTab = (gen) => {
    $("#form-session").classList.toggle("hidden", gen);
    $("#form-generate").classList.toggle("hidden", !gen);
    tabS.className = "btn" + (gen ? " ghost" : "");
    tabG.className = "btn" + (gen ? "" : " ghost");
  };
  tabS.addEventListener("click", () => switchTab(false));
  tabG.addEventListener("click", () => switchTab(true));

  $("#s-connect").addEventListener("click", async () => {
    const btn = $("#s-connect"); setBusy(btn, true, "Connecting to Telegram…"); $("#s-error").textContent = "";
    try {
      const r = await api("/auth/session", "POST", {
        api_id: Number($("#s-api-id").value.trim()),
        api_hash: $("#s-api-hash").value.trim(),
        session_string: $("#s-session").value.trim(),
      });
      S.boot.authenticated = true; S.boot.profile = r.profile;
      updateConnBadge(); renderAuth(); toast(`Welcome, ${r.profile.name}!`, "ok");
    } catch (e) { $("#s-error").textContent = e.message; setBusy(btn, false); }
  });

  $("#g-send-code").addEventListener("click", async () => {
    const btn = $("#g-send-code"); setBusy(btn, true, "Sending code…"); $("#g-error").textContent = "";
    try {
      await api("/auth/send-code", "POST", {
        api_id: Number($("#g-api-id").value.trim()),
        api_hash: $("#g-api-hash").value.trim(),
        phone: $("#g-phone").value.trim(),
      });
      $("#gen-step-1").classList.add("hidden");
      $("#gen-step-2").classList.remove("hidden");
      $("#g-code").focus();
      toast("Code sent — check your Telegram messages", "ok");
    } catch (e) { $("#g-error").textContent = e.message; setBusy(btn, false); }
  });

  $("#g-restart").addEventListener("click", async () => {
    try { await api("/auth/cancel-login", "POST"); } catch (_) {}
    $("#gen-step-2").classList.add("hidden");
    $("#gen-step-1").classList.remove("hidden");
    $("#g-2fa-wrap").classList.add("hidden");
  });

  $("#g-verify").addEventListener("click", async () => {
    const btn = $("#g-verify"); setBusy(btn, true, "Verifying…"); $("#g-error-2").textContent = "";
    try {
      const r = await api("/auth/verify-code", "POST", {
        code: $("#g-code").value.trim(),
        password: $("#g-password").value || null,
      });
      if (r.need_password) {
        $("#g-2fa-wrap").classList.remove("hidden");
        $("#g-error-2").textContent = r.message;
        setBusy(btn, false);
        return;
      }
      S.boot.authenticated = true; S.boot.profile = r.profile;
      updateConnBadge();
      $("#gen-step-2").classList.add("hidden");
      $("#gen-step-3").classList.remove("hidden");
      $("#g-session-out").textContent = r.session_string;
      $("#auth-continue").disabled = false;
      $("#auth-hint").textContent = "Connected ✓";
      toast(`Welcome, ${r.profile.name}!`, "ok");
    } catch (e) { $("#g-error-2").textContent = e.message; setBusy(btn, false); }
  });

  $("#g-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#g-session-out").textContent); toast("Copied!", "ok"); }
    catch (_) { toast("Select the text and copy manually"); }
  });
}
$("#auth-continue").addEventListener("click", () => {
  if (!S.boot.authenticated) return;
  if (S.kind) goSetup();
  else { toast("You're connected! Now pick an operation 👇", "ok"); showView("ops"); }
});

// ------------------------------------------------------------------ chat picker
function chatIcon(type, isForum) {
  if (isForum) return "🗂️";
  return type === "channel" ? "📢" : type === "supergroup" ? "👥" : "👤";
}
function chatPicker(container, opts) {
  const { onSelect } = opts;
  container.innerHTML = `
    <input type="text" class="pk-input" placeholder="Search your chats, or paste @username / link / ID…">
    <div class="picker-results hidden"></div>
    <div class="chat-selected hidden"></div>`;
  const input = container.querySelector(".pk-input");
  const results = container.querySelector(".picker-results");
  const selBox = container.querySelector(".chat-selected");

  async function resolveDirect(q) {
    results.classList.remove("hidden");
    results.innerHTML = `<div class="pk-loading"><span class="spinner dark"></span> Resolving “${esc(q)}”…</div>`;
    try {
      const r = await api("/chats/resolve", "POST", { target: q });
      toast(`Found: ${r.chat.title}`, "ok");
      select(r.chat);
    } catch (e) {
      results.innerHTML = `<div class="pk-loading">⚠ ${esc(e.message)}</div>`;
    }
  }

  const search = debounce(async () => {
    const q = input.value.trim();
    if (!q || q.length < 1) { results.classList.add("hidden"); return; }
    results.classList.remove("hidden");
    results.innerHTML = `<div class="pk-loading"><span class="spinner dark"></span> Searching your chats…</div>`;
    try {
      const r = await api("/chats?q=" + encodeURIComponent(q));
      let html = "";
      if (!r.chats.length) {
        html += `<div class="pk-loading">No matches in your recent chat list — that's OK if you're not subscribed to it. Use the option below. 👇</div>`;
      } else {
        html += r.chats.map((c) => `
          <div class="picker-item" data-id="${c.id}">
            <span class="pi-icon">${chatIcon(c.type, c.is_forum)}</span>
            <div class="grow">
              <div class="pi-title">${esc(c.title)}</div>
              <div class="pi-sub">${c.type}${c.is_forum ? " · forum" : ""} · id ${c.id}${c.username ? " · @" + esc(c.username) : ""}</div>
            </div>
          </div>`).join("");
      }
      // Always offer direct resolution for anything that looks like a username/link/id
      html += `<div class="picker-item pk-direct" style="border-top:1px dashed var(--border2)">
        <span class="pi-icon">🔎</span>
        <div class="grow">
          <div class="pi-title">Use “${esc(q)}” directly</div>
          <div class="pi-sub">works for @usernames, t.me links, invite links and -100… IDs — even chats not in your list</div>
        </div>
      </div>`;
      results.innerHTML = html;
      results.querySelectorAll(".picker-item[data-id]").forEach((el) => {
        el.addEventListener("click", () => {
          const chat = r.chats.find((c) => String(c.id) === el.dataset.id);
          select(chat);
        });
      });
      results.querySelector(".pk-direct").addEventListener("click", () => resolveDirect(q));
      results.classList.remove("hidden");
    } catch (e) {
      results.innerHTML = `<div class="pk-loading">⚠ ${esc(e.message)}</div>`;
      results.classList.remove("hidden");
    }
  }, 350);

  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      e.preventDefault();
      resolveDirect(input.value.trim());
    }
  });

  function select(chat) {
    results.classList.add("hidden");
    selBox.classList.remove("hidden");
    input.classList.add("hidden");
    selBox.innerHTML = `
      <span class="pi-icon">${chatIcon(chat.type, chat.is_forum)}</span>
      <div class="grow">
        <div class="pi-title">${esc(chat.title)}</div>
        <div class="pi-sub">${chat.type === "?" ? "will be resolved when the job starts" :
          chat.type + (chat.is_forum ? " · forum" : "")} · ${esc(String(chat.id))}</div>
      </div>
      <button class="btn small ghost pk-clear">✕</button>`;
    selBox.querySelector(".pk-clear").addEventListener("click", () => {
      selBox.classList.add("hidden");
      input.classList.remove("hidden");
      input.value = ""; input.focus();
      onSelect(null);
    });
    onSelect(chat);
  }
}

// ------------------------------------------------------------------ setup view
function goSetup() {
  if (!S.kind || !(S.boot.kinds || {})[S.kind]) {
    toast("Pick an operation first (e.g. Index Only) 👇");
    showView("ops");
    return;
  }
  showView("setup");
  const spec = (S.boot.kinds || {})[S.kind] || {};
  $("#setup-title").textContent = spec.label || S.kind;
  $("#setup-sub").textContent = spec.topics
    ? "Step 1: The source will be indexed first. You’ll review the topic plan before anything is copied."
    : (spec.index ? "Scan only — nothing will be copied." : "Choose exactly what gets copied.");
  $("#dest-panel").style.display = spec.has_dest ? "" : "none";
  $("#start-btn").textContent = spec.index && S.kind === "index" ? "🔍 Start indexing" : "🚀 Start";

  chatPicker($("#source-picker"), { onSelect: (c) => { S.source = c; validateStart(); } });
  if (spec.has_dest) {
    chatPicker($("#dest-picker"), { onSelect: (c) => { S.dest = c; validateStart(); } });
  }
  renderFilters();
  $("#delay-input").value = (S.boot.defaults && S.boot.defaults.delay) || 1;
  $("#setup-error").textContent = "";
  validateStart();

  $("#setup-back").onclick = () => showAuthView();
}

function renderFilters() {
  const grid = $("#filters-grid");
  const defs = (S.boot && S.boot.filters) || [];
  grid.innerHTML = "";
  for (const f of defs) {
    const chip = document.createElement("label");
    chip.className = "filter-chip" + (S.filters.has(f.key) ? " on" : "");
    chip.innerHTML = `<input type="checkbox" ${S.filters.has(f.key) ? "checked" : ""}>
      <span>${esc(f.label)}${f.hint ? `<span class="fc-hint">${esc(f.hint)}</span>` : ""}</span>`;
    const cb = chip.querySelector("input");
    cb.addEventListener("change", () => {
      if (f.key === "all") {
        S.filters = cb.checked ? new Set(["all"]) : new Set();
        if (cb.checked) S.filters.add("all");
      } else {
        S.filters.delete("all");
        const allCb = grid.querySelector('.filter-chip input');
        if (cb.checked) S.filters.add(f.key); else S.filters.delete(f.key);
        if (allCb) allCb.checked = false;
        if (S.filters.size === 0) { S.filters.add("all"); }
      }
      // re-render to keep "all" state consistent
      renderFilters();
      validateStart();
    });
    grid.appendChild(chip);
  }
}

function validateStart() {
  const spec = (S.boot.kinds || {})[S.kind] || {};
  const btn = $("#start-btn");
  if (btn.classList.contains("busy")) return;   // don't fight an in-flight start
  const ok = S.source && (!spec.has_dest || S.dest) && S.filters.size > 0;
  btn.disabled = !ok;
}

$("#start-btn").addEventListener("click", async () => {
  const btn = $("#start-btn");
  const spec = (S.boot.kinds || {})[S.kind] || {};
  const isIndex = S.kind === "index";
  const busyLabel = isIndex ? "Scanning source…"
    : spec.topics ? "Resolving chats & starting indexer…"
    : "Resolving chats & starting…";
  setBusy(btn, true, busyLabel);
  $("#setup-error").textContent = "";
  const payload = {
    kind: S.kind,
    source: String(S.source.id),
    dest: S.dest ? String(S.dest.id) : null,
    filters: Array.from(S.filters),
    delay: Number($("#delay-input").value || 1),
  };
  try {
    const r = await api("/jobs", "POST", payload);
    S.jobId = r.job_id;
    toast("Job started", "ok");
    setBusy(btn, false);
    openJobView();
  } catch (e) {
    $("#setup-error").textContent = e.message;
    setBusy(btn, false);
    validateStart();
  }
});

// ------------------------------------------------------------------ job view
function openJobView() {
  S.lastBeat = null;
  showView("job");
  pollJob(true);
}
function stopPoll() { if (S.pollTimer) { clearTimeout(S.pollTimer); S.pollTimer = null; } }
function stopHistoryPoll() { if (S.histTimer) { clearInterval(S.histTimer); S.histTimer = null; } }

async function pollJob(immediate) {
  stopPoll();
  if (!S.jobId || S.view !== "job") return;
  try {
    const r = await api("/jobs/" + S.jobId, "GET", undefined, { quiet: true });
    S.job = r.job;
    S.lastBeat = Date.now();
    // ---- live speed tracking (msgs/sec) for real progress readout ----
    const now = Date.now();
    const p = r.job.processed || 0;
    if (S.rate && S.rate.jobId === r.job.id) {
      const dt = (now - S.rate.ts) / 1000;
      if (dt >= 0.5) {
        const inst = Math.max(0, (p - S.rate.processed) / dt);
        S.rate.speed = S.rate.speed > 0 ? S.rate.speed * 0.6 + inst * 0.4 : inst;
        S.rate.ts = now; S.rate.processed = p;
      }
    } else {
      S.rate = { jobId: r.job.id, ts: now, processed: p, speed: 0 };
    }
    renderJob();
    const st = S.job.status;
    if (["indexing", "migrating", "pending"].includes(st)) {
      S.pollTimer = setTimeout(() => pollJob(), 1500);
    }
  } catch (e) {
    if (e.message !== "unauthorized") toast(e.message, "err");
  }
}

const STATUS_LABEL = {
  pending: "Starting…", indexing: "Indexing", migrating: "Migrating",
  awaiting_mapping: "Topic plan ready — review below", paused: "Paused",
  done: "Completed", failed: "Failed", canceled: "Canceled",
};

function renderJob() {
  const j = S.job; if (!j) return;
  const spec = (S.boot.kinds || {})[j.kind] || {};
  const total = j.total || 0;
  const pct = total ? Math.min(100, Math.round((j.processed / total) * 100)) : 0;
  const active = ["indexing", "migrating", "pending"].includes(j.status);
  const indet = active && (!total || j.status === "pending");  // counting / warming up → sliding bar

  let route = `<b>${esc(j.source ? j.source.title : "?")}</b>`;
  if (j.dest) route += ` &nbsp;➜&nbsp; <b>${esc(j.dest.title)}</b>`;

  // LIVE indicator: pulses while the engine is running; shows heartbeat age when idle-paused
  let live = "";
  if (active) {
    live = `<span class="livedot"></span><span class="live-label">LIVE</span>`;
  } else if (S.lastBeat) {
    const age = Math.max(0, Math.round((Date.now() - S.lastBeat) / 1000));
    live = `<span class="livedot warn"></span><span class="live-label warn">last update ${age}s ago</span>`;
  }

  // live speed + ETA from the poll-rate tracker
  let rateTxt = "";
  if (active && S.rate && S.rate.jobId === j.id && S.rate.speed > 0.05) {
    rateTxt = ` · <span style="color:var(--accent)">≈ ${S.rate.speed.toFixed(1)} msg/s</span>`;
    if (total && total > j.processed) {
      rateTxt += ` · <span style="color:var(--accent)">ETA ${fmtDur((total - j.processed) / S.rate.speed)}</span>`;
    }
  }
  const beatHint = active
    ? (j.status === "pending" ? "Starting engine…"
        : !total ? "Scanning — Telegram reports no total for this chat type; running count shown"
        : j.status === "indexing" ? "Indexing in progress…" : "Copying in progress…")
    : "";

  let controls = "";
  if (active) {
    controls = `<button class="btn" id="jb-pause">⏸ Pause</button>
                <button class="btn danger" id="jb-cancel">✕ Cancel</button>`;
  } else if (j.status === "paused") {
    controls = `<button class="btn primary" id="jb-resume">▶ Resume</button>
                <button class="btn danger" id="jb-cancel">✕ Cancel</button>`;
  } else if (["failed", "canceled"].includes(j.status)) {
    controls = `<button class="btn primary" id="jb-resume">↻ Retry / Resume</button>
                <button class="btn danger" id="jb-delete">🗑 Delete job</button>`;
  }
  const sep = (u) => u + (S.token ? (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(S.token) : "");
  let reportLinks = "";
  if (spec.index && j.processed > 0) {
    reportLinks = `<a class="btn ok" href="${sep(`/api/jobs/${j.id}/report.html`)}" target="_blank" rel="noopener">📄 Open HTML report</a>
      <a class="btn" href="${sep(`/api/jobs/${j.id}/report.html?download=1`)}">⬇ Download HTML</a>
      <a class="btn small" href="${sep(`/api/jobs/${j.id}/report.json`)}">JSON</a>
      <a class="btn small" href="${sep(`/api/jobs/${j.id}/report.csv`)}">CSV</a>`;
  }
  let cleanupBanner = "";
  if (j.cleanup_at && spec.index && j.processed > 0) {
    const minsLeft = Math.max(0, Math.round((j.cleanup_at * 1000 - Date.now()) / 60000));
    cleanupBanner = `<div class="cleanup-note">🧹 To save space, this job and its exports are auto-deleted in ~${minsLeft} min — download your HTML / JSON / CSV now.</div>`;
  }

  const barCls = indet ? "progress-outer indet" : "progress-outer";
  const innerCls = active && !indet ? "progress-inner beating" : "progress-inner";
  const progressNote = total
    ? `${j.processed.toLocaleString()} / ${total.toLocaleString()} messages processed (${pct}%)`
    : `${j.processed.toLocaleString()} messages processed`;

  $("#job-content").innerHTML = `
    <div class="panel">
      <div class="job-head">
        <div>
          <h2 style="margin:0">${esc(j.kind_label)} ${live}</h2>
          <div class="job-route">${route}</div>
        </div>
        <span class="status-pill st-${j.status}">${esc(STATUS_LABEL[j.status] || j.status)}</span>
      </div>
      ${j.error ? `<p class="error">⚠ ${esc(j.error)}</p>` : ""}
      ${cleanupBanner}
      <div class="${barCls}"><div class="${innerCls}" style="width:${indet ? 0 : pct}%"></div></div>
      <div class="muted small">${progressNote}${rateTxt}${beatHint ? ` · <span style="color:var(--accent)">${esc(beatHint)}</span>` : ""}</div>
      <div class="stats-grid">
        <div class="stat"><div class="n">${j.copied.toLocaleString()}</div><div class="l">Copied</div></div>
        <div class="stat"><div class="n">${j.skipped.toLocaleString()}</div><div class="l">Skipped / filtered</div></div>
        <div class="stat"><div class="n">${j.failed.toLocaleString()}</div><div class="l">Failed</div></div>
        <div class="stat"><div class="n">${spec.index && j.processed && j.status !== "migrating" ? j.processed.toLocaleString() : "—"}</div><div class="l">Indexed rows</div></div>
      </div>
      <div class="job-controls">${controls} ${reportLinks}
        <button class="btn ghost" id="jb-back" style="margin-left:auto">← New job</button>
      </div>
    </div>
    ${j.status === "awaiting_mapping" ? renderMappingEditor(j) : ""}
    <div class="panel">
      <h3>🧾 Activity log</h3>
      <div class="logbox" id="job-logs">${(j.logs || []).map(logLine).join("") || '<span class="muted">No log entries yet.</span>'}</div>
    </div>`;

  const box = $("#job-logs"); if (box) box.scrollTop = box.scrollHeight;
  const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.addEventListener("click", fn); };
  bind("jb-pause", async () => {
    const b = document.getElementById("jb-pause"); setBusy(b, true, "Pausing…");
    try { await api(`/jobs/${j.id}/pause`, "POST"); pollJob(true); } catch (e) { toast(e.message, "err"); setBusy(b, false); }
  });
  bind("jb-cancel", async () => {
    const b = document.getElementById("jb-cancel"); setBusy(b, true, "Canceling…");
    try { await api(`/jobs/${j.id}/cancel`, "POST"); pollJob(true); } catch (e) { toast(e.message, "err"); setBusy(b, false); }
  });
  bind("jb-resume", async () => {
    const b = document.getElementById("jb-resume"); setBusy(b, true, "Resuming…");
    try { await api(`/jobs/${j.id}/resume`, "POST"); pollJob(true); } catch (e) { toast(e.message, "err"); setBusy(b, false); }
  });
  bind("jb-delete", async () => {
    if (!confirm("Delete this job and all its index data?")) return;
    const b = document.getElementById("jb-delete"); setBusy(b, true, "Deleting…");
    try { await api("/jobs/" + j.id, "DELETE"); S.jobId = null; showView("ops"); } catch (e) { toast(e.message, "err"); setBusy(b, false); }
  });
  bind("jb-back", () => { S.jobId = null; showView("ops"); });
  if (j.status === "awaiting_mapping") bindMapping(j);
}

function logLine(l) {
  const t = new Date(l.ts * 1000).toLocaleTimeString();
  return `<div class="log-line ${l.level}"><span class="ts">${t}</span>${esc(l.message)}</div>`;
}

// ------------------------------------------------------------------ mapping editor
function renderMappingEditor(j) {
  const m = j.mapping || {};
  S.mapDraft = JSON.parse(JSON.stringify(m));
  const counts = j.type_counts || {};
  const typeLabels = {
    photo: "📷 Photos", video: "🎬 Videos", video_note: "⭕ Video messages", animation: "🎞 GIFs",
    document: "📄 Documents", audio: "🎵 Audio", voice: "🎤 Voice messages", sticker: "😀 Stickers",
    poll: "📊 Polls", location: "📍 Locations", contact: "👤 Contacts", link: "🔗 Link posts",
    text: "💬 Text", other: "📦 Other", service: "⚙️ Service messages",
  };
  const types = Object.keys(typeLabels).filter((t) => t !== "service");
  const rows = types.map((t) => {
    const n = counts[t] || 0;
    if (!n && !(m.types && t in m.types)) return "";
    const val = m.types ? m.types[t] : "";
    return `<tr data-type="${t}">
      <td>${typeLabels[t]} <span class="count-badge">${n.toLocaleString()}</span></td>
      <td><select class="mt-topic">${topicOptions(S.mapDraft, val)}</select></td>
    </tr>`;
  }).join("");

  const kwRows = (m.keywords || []).map((r, i) => kwRowHtml(i)).join("");

  return `
  <div class="panel">
    <h3>🗂️ Topic plan — review &amp; adjust</h3>
    <p class="muted small" style="margin-top:0">The source was indexed. Below is the proposed assignment of each content type
    to a forum topic. Rename topics, add keywords to route specific content, or set a type to <i>Skip</i>.
    Topics that don’t exist yet will be created automatically in the destination forum.</p>
    <table class="map-table">
      <tr><th style="width:45%">Content type</th><th>Destination topic</th></tr>
      ${rows}
    </table>

    <h3 style="margin-top:20px">🔑 Keyword rules <span class="muted small">(optional, checked first)</span></h3>
    <table class="map-table" id="kw-table">
      <tr><th style="width:45%">If message contains any of…</th><th>Route to topic</th><th></th></tr>
      ${kwRows}
    </table>
    <div class="job-controls" style="margin-top:10px">
      <button class="btn small" id="kw-add">＋ Add keyword rule</button>
      <button class="btn small" id="topic-add">＋ Add new topic</button>
      <input type="text" id="new-topic-name" placeholder="New topic name" style="max-width:220px">
    </div>

    <div class="wizard-nav">
      <span class="muted small">Migration copies only the selected content types into their assigned topics.</span>
      <button class="btn primary big" id="mapping-start">🚀 Start migration with this plan</button>
    </div>
  </div>`;
}
function topicOptions(m, selected) {
  const topics = (m.topics && m.topics.length ? m.topics : []);
  const opts = topics.map((t) =>
    `<option value="${esc(t)}" ${selected === t ? "selected" : ""}>${esc(t)}</option>`).join("");
  return `<option value="" ${selected ? "" : "selected"}>— Skip this type —</option>` + opts;
}
function kwRowHtml(i) {
  const r = (S.mapDraft.keywords || [])[i] || { topic: "", words: [] };
  return `<tr class="kw-row" data-i="${i}">
    <td><input type="text" class="kw-words" value="${esc((r.words || []).join(", "))}" placeholder="pdf, ebook, notes"></td>
    <td><select class="kw-topic">${topicOptions(S.mapDraft, r.topic)}</select></td>
    <td style="width:36px"><button class="btn small danger kw-del">✕</button></td>
  </tr>`;
}
function bindMapping(j) {
  document.querySelectorAll("#job-content .mt-topic").forEach((sel) => {
    sel.addEventListener("change", () => {
      const t = sel.closest("tr").dataset.type;
      S.mapDraft.types[t] = sel.value || null;
    });
  });
  const refreshKw = () => {
    document.querySelectorAll("#kw-table .kw-row").forEach((tr) => {
      const i = Number(tr.dataset.i);
      const words = tr.querySelector(".kw-words").value.split(",").map((w) => w.trim()).filter(Boolean);
      const topic = tr.querySelector(".kw-topic").value;
      S.mapDraft.keywords[i] = { topic, words };
    });
  };
  const kwTable = $("#kw-table");
  kwTable.addEventListener("input", refreshKw);
  kwTable.addEventListener("change", refreshKw);
  kwTable.addEventListener("click", (e) => {
    if (e.target.classList.contains("kw-del")) {
      const i = Number(e.target.closest("tr").dataset.i);
      S.mapDraft.keywords.splice(i, 1);
      redrawKw();
    }
  });
  function redrawKw() {
    kwTable.querySelectorAll(".kw-row").forEach((tr) => tr.remove());
    (S.mapDraft.keywords || []).forEach((_, i) => {
      kwTable.insertAdjacentHTML("beforeend", kwRowHtml(i));
    });
  }
  $("#kw-add").addEventListener("click", () => {
    S.mapDraft.keywords = S.mapDraft.keywords || [];
    S.mapDraft.keywords.push({ topic: (S.mapDraft.topics || [])[0] || "", words: [] });
    redrawKw();
  });
  $("#topic-add").addEventListener("click", () => {
    const name = $("#new-topic-name").value.trim();
    if (!name) { toast("Enter a topic name first"); return; }
    if (!S.mapDraft.topics.includes(name)) S.mapDraft.topics.push(name);
    $("#new-topic-name").value = "";
    // refresh all selects with the new option
    document.querySelectorAll("#job-content select.mt-topic, #kw-table select.kw-topic").forEach((sel) => {
      const cur = sel.value;
      sel.innerHTML = topicOptions(S.mapDraft, cur);
    });
    toast(`Topic “${name}” added`, "ok");
  });
  $("#mapping-start").addEventListener("click", async () => {
    refreshKw();
    S.mapDraft.keywords = (S.mapDraft.keywords || []).filter((r) => r.words && r.words.length && r.topic);
    const btn = $("#mapping-start"); setBusy(btn, true, "Preparing topics & starting migration…");
    try {
      await api(`/jobs/${j.id}/mapping`, "POST", { mapping: S.mapDraft });
      toast("Migration started!", "ok");
      pollJob(true);
    } catch (e) { toast(e.message, "err"); setBusy(btn, false); }
  });
}

// ------------------------------------------------------------------ history
async function refreshHistory() {
  try {
    const body = $("#history-body");
    if (body && !body.dataset.loaded) {
      body.innerHTML = `<div class="pk-loading"><span class="spinner dark"></span> Loading job history…</div>`;
    }
    const r = await api("/jobs", "GET", undefined, { quiet: true });
    if (body) body.dataset.loaded = "1";
    if (!r.jobs.length) {
      body.innerHTML = `<p class="muted" style="margin:6px">No jobs yet — start one from the “New job” tab.</p>`;
      return;
    }
    body.innerHTML = `<table class="hist">
      <tr><th>Operation</th><th>Route</th><th>Status</th><th>Copied</th><th>Skip/Fail</th><th>Created</th><th></th></tr>
      ${r.jobs.map(jobRow).join("")}
    </table>`;
    body.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => {
      S.jobId = b.dataset.open; openJobView();
    }));
    body.querySelectorAll("[data-report]").forEach((b) => b.addEventListener("click", () => {
      window.open(`/api/jobs/${b.dataset.report}/report.html${S.token ? "?token=" + encodeURIComponent(S.token) : ""}`, "_blank");
    }));
    body.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Delete this job and all its index data?")) return;
      try { await api("/jobs/" + b.dataset.del, "DELETE"); refreshHistory(); } catch (e) { toast(e.message, "err"); }
    }));
    if (S.view === "history") { stopHistoryPoll(); S.histTimer = setInterval(refreshHistory, 4000); }
  } catch (e) { if (e.message !== "unauthorized") toast(e.message, "err"); }
}
function jobRow(j) {
  const route = `${esc(j.source ? j.source.title : "?")}${j.dest ? " ➜ " + esc(j.dest.title) : ""}`;
  const hasReport = ["c2t", "g2t", "index"].includes(j.kind) && j.processed > 0;
  return `<tr>
    <td>${esc(j.kind_label)}</td>
    <td>${route}</td>
    <td><span class="status-pill st-${j.status}" style="font-size:11px;padding:3px 10px">${esc(j.status)}</span></td>
    <td>${j.copied}</td>
    <td class="muted">${j.skipped} / ${j.failed}</td>
    <td class="muted">${fmtDate(j.created_at)}</td>
    <td style="white-space:nowrap">
      <button class="btn small" data-open="${j.id}">Open</button>
      ${hasReport ? `<button class="btn small ok" data-report="${j.id}">Report</button>` : ""}
      <button class="btn small danger" data-del="${j.id}">✕</button>
    </td>
  </tr>`;
}

// ------------------------------------------------------------------ go
boot();
