// SkillForge dashboard — vanilla JS + Chart.js
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};

// ---- number formatting ----
function fmtLift(v) {
  // Accepts 2.3, "+2.3", or "+2.3 points (top-3 A/B)" — renders "+2.3" or em-dash.
  if (v == null) return "\u2014";
  const m = String(v).match(/[-+]?\d+(\.\d+)?/);
  if (!m) return "\u2014";
  const n = Math.round(parseFloat(m[0]) * 10) / 10;
  return (n >= 0 ? "+" : "") + n;
}
function fmt(n) {
  n = Number(n) || 0;
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(Math.round(n));
}
function pct(n) {
  if (n == null) return "—";
  return (Math.round(Number(n) * 10) / 10) + "%";
}
function esc(s) {
  // Escape for both element and ATTRIBUTE contexts (quotes included) — values
  // flow into `attr="${esc(...)}"` and several are LLM-generated skill names.
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

let RESULTS = null;

// current analysis window (days; 0 = all)
function currentWindow() {
  const sel = $("#windowSel");
  return sel ? Number(sel.value) : 14;
}

// ---- toast ----
let toastTimer = null;
function showToast(msg, kind) {
  const t = $("#toast");
  if (!t) return;
  t.className = "toast" + (kind ? " " + kind : "");
  t.textContent = msg;  // never render HTML — msg carries server/LLM-derived text
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 6000);
}

function emergingBadge() {
  return `<span class="badge emerging">EMERGING</span>`;
}

async function init() {
  // identity + coverage scan are independent of results; fire early
  loadIdentity();

  let res;
  try {
    res = await (await fetch("/api/results")).json();
  } catch (e) {
    res = { status: "pending" };
  }

  // header controls work even in pending state
  wireControls();

  // reflect the per-user view state (personal overlay vs shared baseline)
  renderViewLine(res && res.view);

  if (!res || res.status === "pending") {
    $("#pending").classList.remove("hidden");
    const bl = res && res.backlog;
    const blEl = document.getElementById("pendingBacklog");
    if (bl && blEl) {
      const parts = [`${fmt(bl.snapshot_prompts || 0)} prompts captured`];
      if (bl.injected_prompts != null) parts.push(`${fmt(bl.injected_prompts)} injected`);
      blEl.textContent = parts.join(" · ") + " — waiting to be mined.";
      blEl.hidden = false;
    }
    return;
  }
  RESULTS = res;
  $("#dashboard").classList.remove("hidden");

  renderHeader(res);
  renderKpis(res);
  renderPatterns(res);
  renderSkills(res);
  renderBench(res);
  renderInject();
  loadCoverage();
  loadCost();
  loadHistory();
  reloadCharts();
  resumeRefreshIfRunning();
  // published + adoption badges (async; re-render skills when they land)
  Promise.all([loadPublished(), loadAdoption()]).then(() => { if (RESULTS) renderSkills(RESULTS); });
}

function wireControls() {
  const sel = $("#windowSel");
  if (sel && !sel.dataset.wired) {
    sel.dataset.wired = "1";
    sel.addEventListener("change", reloadCharts);
  }
  const rb = $("#refreshBtn");
  if (rb && !rb.dataset.wired) {
    rb.dataset.wired = "1";
    rb.addEventListener("click", runRefresh);
  }
  const pi = $("#priceInput");
  if (pi && !pi.dataset.wired) {
    pi.dataset.wired = "1";
    pi.addEventListener("change", loadCost);
  }
  const si = $("#scaleInput");
  if (si && !si.dataset.wired) {
    si.dataset.wired = "1";
    si.addEventListener("change", loadCost);
  }
}

// ---- identity ----
async function loadIdentity() {
  let me;
  try {
    me = await (await fetch("/api/whoami")).json();
  } catch (e) {
    me = { email: "unknown", auth_mode: "local" };
  }
  const email = me.email || "unknown";
  $("#idEmail").textContent = email;
  const initial = (email.trim()[0] || "?").toUpperCase();
  $("#idAvatar").textContent = initial;
  const badge = $("#idBadge");
  if (me.auth_mode === "obo") {
    badge.textContent = "OBO";
    badge.className = "id-badge obo";
  } else if (me.auth_mode === "service_principal") {
    badge.textContent = "SP";
    badge.className = "id-badge sp";
  } else {
    badge.textContent = "LOCAL";
    badge.className = "id-badge sp";
  }
}

// ---- per-user view (personal overlay vs shared baseline) ----
// Shows a violet "personal view" line with a Reset link only when the user has
// a personal overlay; nothing extra on the shared baseline.
function renderViewLine(view) {
  const line = $("#viewLine");
  if (!line) return;
  const personal = !!(view && view.personal);
  line.hidden = !personal;
  const reset = $("#viewReset");
  if (reset && !reset.dataset.wired) {
    reset.dataset.wired = "1";
    reset.addEventListener("click", (e) => {
      e.preventDefault();
      resetView();
    });
  }
}

async function resetView() {
  if (!confirm("Reset your personal view back to the shared baseline? Your refreshed classifications, emerging patterns/skills and A/B results will be discarded.")) {
    return;
  }
  try {
    const resp = await fetch("/api/state/reset", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "HTTP " + resp.status);
    // full re-render from the (now baseline) results
    const fresh = await (await fetch("/api/results")).json();
    renderViewLine(fresh && fresh.view);
    if (fresh && fresh.status !== "pending") {
      RESULTS = fresh;
      renderKpis(fresh);
      renderPatterns(fresh);
      renderSkills(fresh);
      renderBench(fresh);
    }
    reloadCharts();
    showToast("View reset to the shared baseline.", "");
  } catch (e) {
    showToast("Reset failed: " + e.message, "err");
  }
}

// ---- gateway coverage scan ----
async function loadCoverage() {
  let data;
  try {
    data = await (await fetch("/api/endpoints/scan")).json();
  } catch (e) {
    $("#coverageBanner").textContent = "Endpoint scan unavailable.";
    return;
  }
  if (data.error) {
    $("#coverageBanner").textContent = "Endpoint scan failed: " + data.error;
    return;
  }
  // mine/don't-mine state for each discovered table (UC-backed)
  let mining = {};
  try {
    const mc = await (await fetch("/api/mining/config")).json();
    (mc.tables || []).forEach((m) => { mining[m.table] = m.enabled; });
  } catch (e) { /* default everything to enabled */ }

  const eps = data.endpoints || [];
  const mined = eps.filter((e) => e.inference_table && mining[e.inference_table] !== false).length;
  $("#coverageBanner").innerHTML =
    `<b>${fmt(data.configured)}</b> of <b>${fmt(data.total)}</b> endpoints have inference tables — ` +
    `<b>${fmt(mined)}</b> feed${mined === 1 ? "" : "s"} enabled for mining.`;
  const tb = $("#coverageRows");
  tb.innerHTML = "";
  eps.forEach((e) => {
    const on = !!e.inference_table;
    const enabled = on && mining[e.inference_table] !== false;
    const tr = el("tr");
    tr.innerHTML =
      `<td><div class="pname">${esc(e.name)}</div></td>` +
      `<td class="pdesc">${esc(e.endpoint_type || "—")}</td>` +
      `<td class="pdesc">${esc(e.state || "—")}</td>` +
      `<td class="pdesc">${e.tokens_7d ? fmt(e.tokens_7d) : "—"}</td>` +
      `<td>` +
      (on
        ? `<span class="dot on"></span><code class="tbl">${esc(e.inference_table)}</code>` +
          (e.plane === "v2" ? `<span class="plane-badge">V2</span>` : (e.plane === "legacy" ? `<span class="plane-badge legacy">LEGACY</span>` : ""))
        : `<span class="dot off"></span><span class="muted-txt">no payload capture</span> ` +
          `<a class="mini-link" href="${esc(e.gateway_page || "#")}" target="_blank" rel="noopener" ` +
          `title="Payload logging for Unity AI Gateway is UI-configured (Beta). Opens this endpoint's Gateway page — use 'Inference table: Set up' with an external-storage catalog (e.g. skillforge_inference.feeds).">Create…</a>`) +
      `</td>` +
      `<td>` +
      (on
        ? `<label class="switch" title="Mine this feed on Refresh"><input type="checkbox" aria-label="Mine ${esc(e.inference_table)}" data-table="${esc(e.inference_table)}" ${enabled ? "checked" : ""}/><span class="slider"></span></label>`
        : `<span class="muted-txt">—</span>`) +
      `</td>`;
    tb.appendChild(tr);
  });
  tb.querySelectorAll('input[type="checkbox"][data-table]').forEach((cb) => {
    cb.addEventListener("change", async () => {
      const table = cb.dataset.table;
      const want = cb.checked;
      cb.disabled = true;
      try {
        const resp = await fetch("/api/mining/toggle", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ table: table, enabled: want }),
        });
        const out = await resp.json();
        if (!resp.ok || out.error) throw new Error(out.error || "HTTP " + resp.status);
        showToast(`${want ? "Mining enabled" : "Mining disabled"} for ${table}`, want ? "good" : "");
        loadCoverage();
      } catch (err) {
        cb.checked = !want; // revert
        showToast("Toggle failed: " + err.message, "bad");
      } finally {
        cb.disabled = false;
      }
    });
  });
}

// ---- refresh (background job + status polling) ----
let REFRESH_POLL = null;
let REFRESH_ELAPSED = null;

function setRefreshBusy(busy) {
  const btn = $("#refreshBtn");
  if (!btn) return;
  btn.disabled = busy;
  const label = btn.querySelector("span");
  if (label) label.textContent = busy ? "Re-classifying…" : "Refresh";
  btn.classList.toggle("spinning", busy);
}

function showRefreshBanner() {
  const banner = $("#refreshProgress");
  if (banner) banner.hidden = false;
}
function hideRefreshBanner() {
  const banner = $("#refreshProgress");
  if (banner) banner.hidden = true;
}

function fmtElapsed(s) {
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

// Render the progress banner from a /api/refresh/status payload.
function renderProgress(st) {
  const txt = $("#pbText");
  const bar = $("#pbBar");
  const fill = $("#pbFill");
  if (txt) {
    let line = "Re-classifying prompts through FMAPI…";
    const phase = st.phase ? st.phase.charAt(0).toUpperCase() + st.phase.slice(1) : "";
    if (phase) line = phase + "…";
    if (st.total_candidates) {
      line = `${phase || "Classifying"} — classified ${fmt(st.classified || 0)}/${fmt(st.total_candidates)}`;
    }
    txt.textContent = line;
  }
  // percent bar: prefer classified/total, fall back to batch ratio, else indeterminate
  let pctDone = null;
  if (st.total_candidates) pctDone = (st.classified || 0) / st.total_candidates;
  else if (st.batches_total) pctDone = (st.batches_done || 0) / st.batches_total;
  if (bar && fill) {
    if (pctDone == null) {
      bar.classList.add("indeterminate");
    } else {
      bar.classList.remove("indeterminate");
      fill.style.width = Math.max(2, Math.min(100, Math.round(pctDone * 100))) + "%";
    }
  }
}

function stopRefreshPolling() {
  if (REFRESH_POLL) { clearInterval(REFRESH_POLL); REFRESH_POLL = null; }
  if (REFRESH_ELAPSED) { clearInterval(REFRESH_ELAPSED); REFRESH_ELAPSED = null; }
}

async function refreshDone(st) {
  stopRefreshPolling();
  setRefreshBusy(false);
  hideRefreshBanner();
  const data = (st && st.result) || {};
  if (data.new_prompts === 0) {
    showToast("No new prompts to classify.", "");
  } else {
    const bits = [`${fmt(data.new_prompts)} new prompts`];
    const asgTotal = Object.values(data.assigned || {}).reduce((a, b) => a + b, 0);
    bits.push(`${fmt(asgTotal)} assigned`);
    let kind = "";
    if ((data.new_patterns || []).length) {
      bits.push(`NEW PATTERN: ${data.new_patterns.join(", ")}`);
      kind = "emerging";
    }
    showToast(bits.join(" — "), kind);
  }
  // full re-render from fresh results
  try {
    const fresh = await (await fetch("/api/results")).json();
    renderViewLine(fresh && fresh.view);
    if (fresh && fresh.status !== "pending") {
      RESULTS = fresh;
      renderKpis(fresh);
      renderPatterns(fresh);
      renderSkills(fresh);
      renderBench(fresh);
    }
  } catch (e) { /* ignore */ }
  reloadCharts();
}

// Poll /api/refresh/status; drive the banner + completion handling.
function startRefreshPolling() {
  setRefreshBusy(true);
  showRefreshBanner();
  stopRefreshPolling();
  const pbElapsed = $("#pbElapsed");
  const poll = async () => {
    let st;
    try {
      st = await (await fetch("/api/refresh/status")).json();
    } catch (e) { return; }
    if (pbElapsed) pbElapsed.textContent = fmtElapsed(st.elapsed_s);
    if (st.state === "running") {
      renderProgress(st);
    } else if (st.state === "done") {
      refreshDone(st);
    } else if (st.state === "error") {
      stopRefreshPolling();
      setRefreshBusy(false);
      hideRefreshBanner();
      showToast("Refresh failed: " + (st.error || "unknown error"), "err");
    } else {
      // idle (shouldn't normally happen mid-run) — stop quietly
      stopRefreshPolling();
      setRefreshBusy(false);
      hideRefreshBanner();
    }
  };
  poll();
  REFRESH_POLL = setInterval(poll, 3000);
}

async function runRefresh() {
  setRefreshBusy(true);
  showRefreshBanner();
  renderProgress({ phase: "starting" });
  try {
    const resp = await fetch("/api/refresh?window_days=" + currentWindow(), { method: "POST" });
    const data = await resp.json();
    if (resp.status === 409) {
      // already running — just attach to the existing job
      startRefreshPolling();
      return;
    }
    if (!resp.ok || data.error) throw new Error(data.error || "HTTP " + resp.status);
    startRefreshPolling();
  } catch (e) {
    setRefreshBusy(false);
    hideRefreshBanner();
    showToast("Refresh failed: " + e.message, "err");
  }
}

// On page load: if a refresh is already running, resume the banner + polling.
async function resumeRefreshIfRunning() {
  let st;
  try {
    st = await (await fetch("/api/refresh/status")).json();
  } catch (e) { return; }
  if (st && st.state === "running") {
    renderProgress(st);
    const pbElapsed = $("#pbElapsed");
    if (pbElapsed) pbElapsed.textContent = fmtElapsed(st.elapsed_s);
    startRefreshPolling();
  }
}

function renderHeader(res) {
  const parts = [];
  if (res.generated_at) {
    const d = new Date(res.generated_at);
    parts.push(`<b>Generated</b> ${isNaN(d) ? esc(res.generated_at) : d.toLocaleString()}`);
  }
  if (res.source) {
    const s = res.source;
    parts.push(`<b>${fmt(s.rows)}</b> rows · <b>${fmt(s.users)}</b> users · ${esc(s.window_days)}d window`);
  }
  $("#genInfo").innerHTML = parts.join("<br/>");
}

function renderKpis(res) {
  const o = res.overview || {};
  const sm = res.summary || {};
  const items = [
    { v: fmt(o.total_prompts), l: "Prompts analyzed" },
    { v: fmt(o.users), l: "Users" },
    { v: fmt(sm.skills_recommended), l: "Skills recommended", cls: "accent-red" },
    { v: pct(sm.prompts_consolidated_pct), l: "Prompts consolidated", cls: "accent-amber" },
    { v: fmt(sm.est_monthly_token_savings_total), l: "Est. monthly token savings", cls: "accent-green" },
    { v: fmtLift(sm.avg_quality_lift), l: "Avg quality lift", cls: "accent-green" },
  ];
  const wrap = $("#kpis");
  wrap.innerHTML = "";
  items.forEach((it) => {
    const k = el("div", "kpi " + (it.cls || ""));
    k.appendChild(el("div", "v", esc(it.v)));
    k.appendChild(el("div", "l", esc(it.l)));
    wrap.appendChild(k);
  });
}

function purityClass(p) {
  if (p >= 80) return "";
  if (p >= 60) return "mid";
  return "low";
}

function renderPatterns(res) {
  const tb = $("#patternRows");
  tb.innerHTML = "";
  (res.patterns || []).forEach((p) => {
    const tr = el("tr");
    const isEmerging = p.status === "emerging";
    const purityCell = isEmerging
      ? emergingBadge()
      : `<span class="badge purity ${purityClass(p.purity_pct)}">${pct(p.purity_pct)}</span>`;
    tr.innerHTML =
      `<td><div class="pname">${esc(p.name)} ${isEmerging ? emergingBadge() : ""}</div></td>` +
      `<td class="pdesc">${esc(p.description)}</td>` +
      `<td class="num">${fmt(p.prompt_count)}</td>` +
      `<td class="num">${fmt(p.user_count)}</td>` +
      `<td class="num">${fmt(p.total_tokens)}</td>` +
      `<td>${purityCell}</td>`;
    tb.appendChild(tr);
  });
}

function valueChips(v) {
  if (!v) return "";
  const chips = [
    { cl: "Users covered", b: fmt(v.users_covered) },
    { cl: "~Prompts / mo", b: fmt(v.prompts_per_month_est) },
    { cl: "Est. monthly token savings", b: fmt(v.est_monthly_token_savings) },
    { cl: "Input token savings", b: pct(v.input_token_savings_pct) },
  ];
  return `<div class="chips">` +
    chips.map((c) => `<div class="chip"><span class="cl">${esc(c.cl)}</span><b>${esc(c.b)}</b></div>`).join("") +
    `</div>`;
}

function beforeAfter(ab) {
  if (!ab) return "";
  const delta = (Number(ab.skill_score) - Number(ab.raw_score));
  const deltaStr = isNaN(delta) ? "" : `<span class="delta">+${Math.round(delta * 10) / 10}</span>`;
  return `<div class="ba">
    <h4>Quality A/B (LLM judge)</h4>
    <div class="ba-cols">
      <div class="ba-col raw">
        <div class="label">Raw prompt</div>
        <div class="score">${esc(ab.raw_score)}</div>
        <div class="ans">${esc(ab.raw_answer)}</div>
      </div>
      <div class="ba-col skill">
        <div class="label">Skill ${deltaStr}</div>
        <div class="score">${esc(ab.skill_score)}</div>
        <div class="ans">${esc(ab.skill_answer)}</div>
      </div>
    </div>
    <div class="rationale">${esc(ab.rationale)}</div>
  </div>`;
}

function renderSkills(res) {
  const grid = $("#skillGrid");
  grid.innerHTML = "";
  (res.skills || []).forEach((s) => {
    const v = s.value || {};
    const prio = (v.priority || "low").toLowerCase();
    const isEmerging = s.status === "emerging" || prio === "emerging";
    const card = el("div", "card skill" + (isEmerging ? " is-emerging" : ""));

    const params = (s.parameters || [])
      .map((p) => `<li><code>{${esc(p.name)}}</code> — ${esc(p.description)}</li>`)
      .join("");

    const hasAb = !!s.quality_ab;
    const abBtn = hasAb
      ? `<button class="ghost-btn ab-btn" data-skill="${esc(s.id)}">Re-run A/B</button>`
      : `<button class="ghost-btn ab-btn" data-skill="${esc(s.id)}">Run quality A/B</button>`;
    const pub = PUBLISHED[s.id];
    const pubBtn = `<button class="ghost-btn pub-btn" data-skill="${esc(s.id)}">${pub ? "Re-publish" : "Publish to Genie Code"}</button>`;
    const pubChip = pub ? `<span class="badge published" title="${esc(pub.path || "")}">✓ published v${pub.version}</span>` : "";
    const adp = ADOPTION[s.id];
    const adpChip = adp ? `<span class="badge adoption" title="${adp.adopted}/${adp.matches} matching prompts use the template">${adp.pct}% adopted</span>` : "";
    const adpBtn = pub ? `<button class="ghost-btn adp-btn" data-skill="${esc(s.id)}">${adp ? "Re-measure adoption" : "Measure adoption"}</button>` : "";

    card.innerHTML =
      `<div class="top">
         <div>
           <div class="title">${esc(s.title || s.name)}</div>
           <div class="name-mono">${esc(s.name)}</div>
         </div>
         <div class="badge-stack">
           ${isEmerging ? emergingBadge() : `<span class="badge prio-${prio}">${esc(prio)}</span>`}
           ${pubChip}
           ${adpChip}
         </div>
       </div>
       <div class="desc">${esc(s.description)}</div>
       ${valueChips(v)}
       <details class="tmpl">
         <summary>Template &amp; details</summary>
         <div class="body">
           <pre class="code">${esc(s.template)}</pre>
           ${params ? `<div class="kv"><b>Parameters</b></div><ul class="param-list">${params}</ul>` : ""}
           ${s.example_invocation ? `<div class="kv"><b>Example invocation</b></div><pre class="code">${esc(s.example_invocation)}</pre>` : ""}
         </div>
       </details>
       <div class="ab-slot">${beforeAfter(s.quality_ab)}</div>
       <div class="skill-footer">
         ${pubBtn}
         ${adpBtn}
         ${abBtn}
         <button class="ghost-btn" data-export="${esc(s.id)}" data-fmt="markdown">Export .md</button>
         <button class="ghost-btn" data-export="${esc(s.id)}" data-fmt="json">.json</button>
       </div>`;
    grid.appendChild(card);
  });

  // wire export (download) + quality A/B buttons
  grid.querySelectorAll("[data-export]").forEach((b) => {
    b.addEventListener("click", () => {
      const id = b.dataset.export;
      const fmt = b.dataset.fmt || "markdown";
      location.href = `/api/skills/${encodeURIComponent(id)}/export?format=${fmt}`;
    });
  });
  grid.querySelectorAll(".ab-btn").forEach((b) => {
    b.addEventListener("click", () => runQualityAb(b.dataset.skill, b));
  });
  grid.querySelectorAll(".pub-btn").forEach((b) => {
    b.addEventListener("click", () => publishSkill(b.dataset.skill, b));
  });
  grid.querySelectorAll(".adp-btn").forEach((b) => {
    b.addEventListener("click", () => measureAdoption(b.dataset.skill, b));
  });
}

let ADOPTION = {};
async function loadAdoption() {
  try { ADOPTION = (await (await fetch("/api/adoption")).json()).adoption || {}; }
  catch (e) { ADOPTION = {}; }
}
async function measureAdoption(skillId, btn) {
  if (!btn) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Measuring…`;
  try {
    const resp = await fetch(`/api/skills/${encodeURIComponent(skillId)}/adoption?window_days=${currentWindow() || 14}`, { method: "POST" });
    const d = await resp.json();
    if (!resp.ok || d.error) throw new Error(d.error || "HTTP " + resp.status);
    await loadAdoption();
    if (RESULTS) renderSkills(RESULTS);
    showToast(`Adoption: ${d.adoption_pct}% (${d.adopted}/${d.pattern_matches} matching prompts use the template)`, "good");
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showToast("Adoption measure failed: " + e.message, "err");
  }
}

// ---- mining history trend ----
let historyChart = null;
async function loadHistory() {
  let d;
  try { d = await (await fetch("/api/history")).json(); } catch (e) { return; }
  const runs = (d.runs || []);
  const sec = $("#sec-history");
  if (runs.length < 2) { if (sec) sec.hidden = true; return; }  // need ≥2 points to trend
  if (sec) sec.hidden = false;
  const note = $("#historyNote");
  if (note) { note.hidden = false; note.textContent = runs.length + " runs"; }
  const labels = runs.map((r) => (r.at || "").slice(5, 16));
  const ctx = document.getElementById("chartHistory");
  if (!ctx) return;
  if (historyChart) historyChart.destroy();
  historyChart = new Chart(ctx, {
    type: "line",
    data: { labels, datasets: [
      { label: "Patterns", data: runs.map((r) => r.patterns), borderColor: "#FF3620", tension: 0.3 },
      { label: "Skills", data: runs.map((r) => r.skills), borderColor: "#00B378", tension: 0.3 },
    ]},
    options: { plugins: { legend: { labels: { color: "#9eb7be" } } },
      scales: { x: { ticks: { color: "#9eb7be" } }, y: { ticks: { color: "#9eb7be" }, beginAtZero: true } } },
  });
}

// ---- cost & ROI ----
function money(n) {
  if (n == null) return "—";
  return "$" + Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 });
}
async function loadCost() {
  const price = Number(($("#priceInput") || {}).value) || undefined;
  const scale = Number(($("#scaleInput") || {}).value) || undefined;
  const qs = [];
  if (price) qs.push("price_per_1m=" + price);
  if (scale && scale > 1) qs.push("scale=" + scale);
  let d;
  try {
    d = await (await fetch("/api/cost" + (qs.length ? "?" + qs.join("&") : ""))).json();
  } catch (e) { return; }
  const note = $("#costNote");
  if (note) { note.hidden = false; note.textContent = "@ $" + d.price_per_1m + "/1M" + (d.scale > 1 ? " ×" + d.scale : "") + " — illustrative"; }
  const k = $("#costKpis");
  if (k) k.innerHTML =
    `<div class="cost-kpi"><div class="big accent-green">${money(d.annual_savings)}</div><div class="lbl">Est. annual savings</div></div>` +
    `<div class="cost-kpi"><div class="big">${money(d.monthly_savings)}</div><div class="lbl">Per month</div></div>` +
    `<div class="cost-kpi"><div class="big">${money(d.monthly_pattern_spend)}</div><div class="lbl">Monthly spend on these patterns</div></div>`;
  const tb = $("#costRows");
  if (tb) {
    tb.innerHTML = "";
    (d.skills || []).forEach((s) => {
      const tr = el("tr");
      const prio = (s.priority || "low").toLowerCase();
      tr.innerHTML =
        `<td><div class="pname">${esc(s.title)}</div></td>` +
        `<td><span class="badge prio-${prio}">${esc(prio)}</span></td>` +
        `<td class="pdesc">${fmt(s.monthly_tokens)}</td>` +
        `<td class="accent-green">${money(s.monthly_savings)}/mo</td>`;
      tb.appendChild(tr);
    });
  }
}

let PUBLISHED = {};
async function loadPublished() {
  try {
    const d = await (await fetch("/api/published")).json();
    PUBLISHED = d.published || {};
  } catch (e) { PUBLISHED = {}; }
}

async function publishSkill(skillId, btn) {
  if (!btn) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Publishing…`;
  try {
    const resp = await fetch(`/api/skills/${encodeURIComponent(skillId)}/publish`, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "HTTP " + resp.status);
    await loadPublished();
    if (RESULTS) renderSkills(RESULTS);
    showToast(`Published to Genie Code: ${data.path} (v${data.version})`, "good");
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showToast("Publish failed: " + e.message, "err");
  }
}

async function runQualityAb(skillId, btn) {
  if (!btn) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Running A/B…`;
  try {
    const resp = await fetch(`/api/skills/${encodeURIComponent(skillId)}/quality_ab`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "HTTP " + resp.status);
    // refetch results and re-render so the card shows the Before/After panel
    const fresh = await (await fetch("/api/results")).json();
    renderViewLine(fresh && fresh.view);
    if (fresh && fresh.status !== "pending") {
      RESULTS = fresh;
      renderSkills(fresh);
    }
    showToast("Quality A/B complete.", "");
  } catch (e) {
    btn.disabled = false;
    btn.textContent = orig;
    showToast("Quality A/B failed: " + e.message, "err");
  }
}

// ---- charts ----
let CHART_DAY = null;
let CHART_EP = null;

function setUsageSourcePill(source) {
  const pill = $("#usageSourcePill");
  if (!pill) return;
  if (source === "uc") {
    pill.textContent = "LIVE UC";
    pill.className = "source-pill live";
  } else {
    pill.textContent = "SNAPSHOT";
    pill.className = "source-pill snap";
  }
  pill.hidden = false;
}

async function reloadCharts() {
  let stats;
  try {
    // Prefer live Unity Catalog stats; the server falls back to the snapshot.
    stats = await (await fetch("/api/usage/stats?source=uc&window_days=" + currentWindow())).json();
  } catch (e) { return; }
  setUsageSourcePill(stats.source);

  if (CHART_DAY) { CHART_DAY.destroy(); CHART_DAY = null; }
  if (CHART_EP) { CHART_EP.destroy(); CHART_EP = null; }

  const tealGrid = "rgba(255,255,255,0.07)";
  const tickColor = "#93a8ad";

  // prompts per day — line
  const days = stats.prompts_per_day || [];
  CHART_DAY = new Chart($("#chartDay"), {
    type: "line",
    data: {
      labels: days.map((d) => d.date),
      datasets: [{
        data: days.map((d) => d.count),
        borderColor: "#ff3621",
        backgroundColor: "rgba(255,54,33,0.15)",
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: tealGrid }, ticks: { color: tickColor, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: { grid: { color: tealGrid }, ticks: { color: tickColor }, beginAtZero: true },
      },
    },
  });

  // tokens by endpoint — doughnut
  const eps = stats.tokens_by_endpoint || [];
  const palette = ["#ff3621", "#00b378", "#ffab00", "#4aa3df", "#9b8cff", "#ff7aa0", "#6e858b"];
  CHART_EP = new Chart($("#chartEndpoint"), {
    type: "doughnut",
    data: {
      labels: eps.map((e) => e.endpoint),
      datasets: [{
        data: eps.map((e) => e.tokens),
        backgroundColor: palette,
        borderColor: "rgba(0,0,0,0.25)",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: "58%",
      plugins: {
        legend: { position: "right", labels: { color: tickColor, boxWidth: 12, font: { size: 11 } } },
      },
    },
  });
}

// ---- test bench ----
function renderBench(res) {
  const sel = $("#benchSkill");
  sel.innerHTML = "";
  (res.skills || []).forEach((s) => {
    const opt = el("option");
    opt.value = s.id;
    opt.textContent = s.title || s.name;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => renderBenchParams(res));
  $("#benchRun").addEventListener("click", () => runBench(res));
  renderBenchParams(res);
}

function currentSkill(res) {
  const id = $("#benchSkill").value;
  return (res.skills || []).find((s) => s.id === id);
}

function renderBenchParams(res) {
  const wrap = $("#benchParams");
  wrap.innerHTML = "";
  const s = currentSkill(res);
  if (!s || !(s.parameters || []).length) return;
  const grid = el("div", "params-grid");
  s.parameters.forEach((p) => {
    const box = el("div");
    const inputId = "bench-param-" + String(p.name).replace(/[^a-zA-Z0-9_-]/g, "-");
    box.innerHTML = `<label for="${inputId}">${esc(p.name)} <span style="color:var(--muted2);font-weight:400">— ${esc(p.description)}</span></label>`;
    const inp = el("input");
    inp.id = inputId;
    inp.name = inputId;
    inp.setAttribute("aria-label", p.name);
    inp.dataset.param = p.name;
    inp.placeholder = p.description || p.name;
    box.appendChild(inp);
    grid.appendChild(box);
  });
  wrap.appendChild(grid);
}

async function runBench(res) {
  const s = currentSkill(res);
  if (!s) return;
  const btn = $("#benchRun");
  const errBox = $("#benchError");
  const out = $("#benchOut");
  errBox.classList.add("hidden");
  out.innerHTML = "";

  const parameters = {};
  document.querySelectorAll("#benchParams input[data-param]").forEach((i) => {
    parameters[i.dataset.param] = i.value;
  });
  const raw = $("#benchRaw").value.trim();

  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Running...`;

  try {
    const resp = await fetch("/api/test_skill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill_id: s.id, parameters, raw_prompt: raw || null }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      throw new Error(data.error || ("HTTP " + resp.status));
    }
    renderBenchResults(data);
  } catch (e) {
    errBox.textContent = "Test failed: " + e.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run skill";
  }
}

function tokLine(usage) {
  if (!usage) return "";
  const i = usage.prompt_tokens ?? usage.input_tokens;
  const o = usage.completion_tokens ?? usage.output_tokens;
  const t = usage.total_tokens;
  const bits = [];
  if (i != null) bits.push(`in ${fmt(i)}`);
  if (o != null) bits.push(`out ${fmt(o)}`);
  if (t != null) bits.push(`total ${fmt(t)}`);
  return bits.length ? `<div class="tok">tokens — ${bits.join(" · ")}</div>` : "";
}

function renderBenchResults(data) {
  const out = $("#benchOut");
  const hasRaw = data.raw_answer != null;
  const wrap = el("div", "bench-results" + (hasRaw ? "" : " single"));

  const skillCol = el("div", "result-col skill");
  skillCol.innerHTML =
    `<div class="label">Skill answer</div>` +
    `<div class="ans">${esc(data.skill_answer)}</div>` +
    tokLine(data.skill_usage);
  wrap.appendChild(skillCol);

  if (hasRaw) {
    const rawCol = el("div", "result-col raw");
    rawCol.innerHTML =
      `<div class="label">Raw prompt answer</div>` +
      `<div class="ans">${esc(data.raw_answer)}</div>` +
      tokLine(data.raw_usage);
    wrap.appendChild(rawCol);
  }

  out.innerHTML = "";
  // show the filled prompt that was sent
  const sent = el("details", "tmpl");
  sent.innerHTML = `<summary>Prompt sent</summary><div class="body"><pre class="code">${esc(data.skill_prompt)}</pre></div>`;
  out.appendChild(sent);
  out.appendChild(wrap);
}

// ---- inject prompts ----
function renderInject() {
  const btn = $("#injectRun");
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = "1";
    btn.addEventListener("click", runInject);
  }
}

async function runInject() {
  const btn = $("#injectRun");
  const errBox = $("#injectError");
  const out = $("#injectOut");
  errBox.classList.add("hidden");
  out.innerHTML = "";

  const prompts = $("#injectText").value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 20);
  if (!prompts.length) {
    errBox.textContent = "Enter at least one prompt (one per line).";
    errBox.classList.remove("hidden");
    return;
  }
  const userEmail = $("#injectEmail").value.trim() || null;

  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Sending...`;
  try {
    const resp = await fetch("/api/inject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompts, user_email: userEmail }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || "HTTP " + resp.status);

    out.innerHTML =
      `<div class="inject-result">` +
      `<div><b>${fmt(data.sent)}</b> sent · <b>${fmt(data.failed)}</b> failed · <b>${fmt(data.inserted)}</b> recorded</div>` +
      `<div class="muted-txt note">${esc(data.note || "")}</div>` +
      `<span class="hint-chip">Now hit Refresh to re-classify</span>` +
      (data.errors && data.errors.length
        ? `<div class="muted-txt note">${esc(data.errors.join(" | "))}</div>`
        : "") +
      `</div>`;
    showToast(`Injected ${fmt(data.sent)} prompt(s) — hit Refresh to re-classify`, "");
  } catch (e) {
    errBox.textContent = "Inject failed: " + e.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

init();


// ---- sidebar nav (scrollspy + smooth scroll) ----
function wireSidebar() {
  const nav = document.getElementById("sideNav");
  if (!nav || nav.dataset.wired) return;
  nav.dataset.wired = "1";
  const items = Array.from(nav.querySelectorAll(".nav-item"));
  items.forEach((it) => {
    it.addEventListener("click", (e) => {
      const target = document.querySelector(it.getAttribute("href"));
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
  // Overview nav stays active across both the KPI and usage sections.
  const map = { "sec-usage": "#sec-overview" };
  const obs = new IntersectionObserver(
    (entries) => {
      const vis = entries.filter((en) => en.isIntersecting)
        .sort((x, y) => y.intersectionRatio - x.intersectionRatio)[0];
      if (!vis) return;
      const id = vis.target.id;
      const href = map[id] || "#" + id;
      items.forEach((it) => it.classList.toggle("active", it.getAttribute("href") === href));
    },
    { rootMargin: "-20% 0px -55% 0px", threshold: [0.05, 0.25, 0.5] }
  );
  document.querySelectorAll("section.section[id]").forEach((s) => obs.observe(s));
}
document.addEventListener("DOMContentLoaded", wireSidebar);


// ---- clear injected-prompt history (destructive, warned) ----
function wireInjectClear() {
  const btn = document.getElementById("injectClear");
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  btn.addEventListener("click", async () => {
    const ok = confirm(
      "Clear prompt history?\n\n" +
      "This permanently deletes ALL injected prompts from the shared " +
      "injected_prompts table — for every user, not just you. Prompts already " +
      "classified into patterns are not un-counted, and gateway usage / " +
      "inference tables are untouched.\n\nThis cannot be undone."
    );
    if (!ok) return;
    btn.disabled = true;
    try {
      const resp = await fetch("/api/inject/clear", { method: "POST" });
      const out = await resp.json();
      if (!resp.ok || out.error) throw new Error(out.error || "HTTP " + resp.status);
      showToast(`Prompt history cleared (${fmt(out.cleared)} prompts removed).`, "good");
      const outEl = document.getElementById("injectOut");
      if (outEl) outEl.innerHTML = "";
    } catch (err) {
      showToast("Clear failed: " + err.message, "bad");
    } finally {
      btn.disabled = false;
    }
  });
}
document.addEventListener("DOMContentLoaded", wireInjectClear);
