// serve/web/app.js - the Strata web app: Chat, Monitor, About. No framework, no network beyond this server.
// The Monitor tab rebuilds PR #22's dashboard idea (code-martin) on the server's own /metrics.

// The pieces with no knowledge of the chat or the server live beside it as ES modules (web/js/):
// util (ids, icons, numbers, storage), toast (the notices), markdown (the answer as HTML).  The rest is
// here because it shares the conversation state - messages, attachments, which chat is open - and cannot be cut.
import {$, SPRITE, icon, esc, fmt, kfmt, ctxfmt, gb, store, syncGutter} from "./js/util.js";
import {toast, copyText} from "./js/toast.js";
import {markdown} from "./js/markdown.js";

// ------------------------------------------------------------------ theme and tabs
// the system's theme until the user picks one (only a click is saved)
function setTheme(t, save) {
  document.documentElement.dataset.theme = t;
  if (save) try { localStorage.setItem("strata.theme", t); } catch (e) { /* ignore */ }
  $("theme-icon").setAttribute("href", `${SPRITE}#i-${t === "dark" ? "sun" : "moon"}`);
  $("dark-toggle").setAttribute("aria-checked", String(t === "dark"));
}
const flipTheme = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
$("theme-btn").onclick = flipTheme;
$("dark-toggle").onclick = flipTheme;
setTheme(document.documentElement.dataset.theme || "light", false);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  let saved = null;
  try { saved = localStorage.getItem("strata.theme"); } catch (err) { /* ignore */ }
  if (!saved) setTheme(e.matches ? "dark" : "light", false);
});

let tab = "chat";
function showTab(name) {
  tab = ["chat", "monitor", "about"].includes(name) ? name : "chat";
  for (const b of document.querySelectorAll(".st-tab")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const v of ["chat", "monitor", "about"]) $(`view-${v}`).hidden = v !== tab;
  if (location.hash.slice(1) !== tab) history.replaceState(null, "", tab === "chat" ? location.pathname : `#${tab}`);
  if (tab === "chat") $("input").focus();
  if (tab === "monitor") loadMcp();
  if (tab === "about") loadConfig();
  if (lastMetrics) render(lastMetrics);
}
for (const b of document.querySelectorAll(".st-tab")) b.onclick = () => showTab(b.dataset.tab);
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ------------------------------------------------------------------ server access
function headers(json = false) {
  const h = {};
  const key = store.get("apikey", "");
  if (key) h.Authorization = "Bearer " + key;
  if (json) h["Content-Type"] = "application/json";
  return h;
}
$("api-key").value = store.get("apikey", "");
$("api-key").onchange = () => { store.set("apikey", $("api-key").value.trim()); toast("success", "API key saved", "Kept in this browser only."); };

let health = {model: "strata", images: false, max_context: 0};
async function loadHealth() {
  try {
    health = await (await fetch("health")).json();
    $("attach-btn").title = health.images ? "Attach a text file or a picture (or drop it here)"
                                          : "Attach a text file (or drop it here)";
    $("chat-empty-sub").textContent = `${health.model} runs on this PC. Nothing leaves it.`;
  } catch (e) {
    setTimeout(loadHealth, 2000);
  }
}

// ------------------------------------------------------------------ Monitor
const METRICS = [
  {key: "speed", label: "Speed", icon: "gauge", unit: "t/s", series: "tok_s"},
  {key: "gpu", label: "GPU load", icon: "gpu", unit: "%", series: "gpu_util", max: 100},
  {key: "vram", label: "VRAM", icon: "layers", unit: "GB", series: "gpu_mem_used"},
  {key: "temp", label: "GPU temp", icon: "thermometer", unit: "°C", series: "gpu_temp", tone: "warn"},
  {key: "power", label: "Power", icon: "bolt", unit: "W", series: "gpu_power"},
  {key: "pcie", label: "PCIe", icon: "link", unit: "", series: "gpu_pcie_rx_mb", tone: "info"},
  {key: "cpu", label: "CPU", icon: "cpu", unit: "%", series: "cpu", max: 100},
  {key: "disk", label: "Disk read", icon: "disk", unit: "MB/s", series: "disk_read_mb", tone: "info"},
];
$("metrics").innerHTML = METRICS.map((m) => `
  <div class="st-card metric-card"><div class="st-metric">
    <span class="st-metric__label">${icon(m.icon, "st-icon st-icon--sm")}${esc(m.label)}</span>
    ${m.key === "speed" ? `<div class="speed-values">
      <div><span class="st-metric__value" id="mv-speed">-</span><span class="st-metric__sub" id="ms-speed">Decode</span></div>
      <div class="speed-prefill"><span class="st-metric__value" id="mv-prefill">-</span><span class="st-metric__sub" id="ms-prefill">Prefill</span></div>
    </div>` : `<span class="st-metric__value" id="mv-${m.key}">–</span>
    <span class="st-metric__sub" id="ms-${m.key}"></span>`}
    <svg class="st-metric__spark" id="sp-${m.key}" viewBox="0 0 100 32" preserveAspectRatio="none"${m.tone ? ` data-tone="${m.tone}"` : ""}>
      <path class="area" fill="currentColor" opacity=".12"/><path class="line" fill="none" stroke="currentColor"
      stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
      ${m.key === "speed" ? `<g id="sp-prefill" class="speed-prefill"><path class="area" fill="currentColor" opacity=".12"/>
        <path class="line" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"
        stroke-linecap="round" vector-effect="non-scaling-stroke"/></g>` : ""}</svg>
  </div></div>`).join("");

function spark(id, values, max) {
  const svg = $(id);
  const v = (values || []).map((x) => (x == null ? 0 : x));
  if (v.length < 2) { svg.querySelector(".line").setAttribute("d", ""); svg.querySelector(".area").setAttribute("d", ""); return; }
  const top = Math.max(max || 0, ...v, 1);
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  svg.querySelector(".line").setAttribute("d", line);
  svg.querySelector(".area").setAttribute("d", `${line}L100,32L0,32Z`);
}
function setMetric(key, value, unit, sub) {
  $(`mv-${key}`).innerHTML = value == null ? "–" : `${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}`;
  $(`ms-${key}`).textContent = sub || "";
}

let lastMetrics = null, metricsFailures = 0, keyWarned = false, mcpTick = 0;
let reqShowAll = false;   // the Monitor's request table: the last 12, or every one the server keeps (issue #35)
async function poll() {
  try {
    const r = await fetch(reqShowAll ? "metrics?requests=all" : "metrics", {headers: headers()});
    if (r.status === 401) {
      setPill("error", "API key needed");
      if (!keyWarned) { keyWarned = true; toast("warn", "API key needed", "This server needs a key: add it under About > Settings.", 6000); }
    } else if (r.ok) {
      lastMetrics = await r.json();
      metricsFailures = 0;
      render(lastMetrics);
    } else {
      throw new Error(`HTTP ${r.status}`);
    }
  } catch (e) {
    if (++metricsFailures === 3) setPill("error", "Server not reachable");
  }
  if (tab === "monitor" && ++mcpTick % 10 === 0) loadMcp();       // server states change rarely: every 10 s
  setTimeout(poll, 1000);
}

function setPill(state, text) {
  $("pill").dataset.state = state === "error" ? "queued" : state;
  $("pill-text").textContent = text;
}

// Windows commit headroom (RAM + pagefile): warn once when it runs low, again only if it gets worse or after 10 minutes
let commitSeen = {level: "ok", at: 0};
function warnCommit(c) {
  if (!c || c.level === "ok") { commitSeen.level = "ok"; return; }
  const worse = c.level === "critical" && commitSeen.level !== "critical";
  if (!worse && Date.now() - commitSeen.at < 600000 && commitSeen.level !== "ok") return;
  commitSeen = {level: c.level, at: Date.now()};
  const fix = c.pagefile_gib < 16 ? " The pagefile is small or off: set it to 64 GB on the fast drive (Advanced system settings > Performance > Virtual memory), or lower the resident budget." : " Close other programs, or lower the resident budget.";
  toast(c.level === "critical" ? "error" : "warn", c.level === "critical" ? "Memory almost full" : "Memory getting low",
    `Commit ${c.used_gib} of ${c.limit_gib} GiB (${c.free_gib} GiB left).` + fix, 15000);
}

function render(m) {
  warnCommit(m.commit);
  const live = m.live || {}, hw = m.hardware || {}, st = m.hardware_static || {}, eng = m.engine || {}, h = m.history || {};
  const last = (m.requests || [])[0];
  // the header pill
  if (live.state === "reading") {
    const pct = live.prompt_total ? Math.round((100 * live.prompt_read) / live.prompt_total) : null;
    setPill("reading", pct != null ? `Reading prompt · ${pct}%` : "Reading prompt");
  } else if (live.state === "generating") {
    setPill("generating", `Generating · ${fmt(live.tok_s, 1)} tok/s`);
  } else {
    setPill("idle", "Idle");
  }
  if (live.queued > 0) setPill("queued", `${live.queued} queued`);
  if (tab === "monitor") renderMonitor(live, hw, st, eng, h, last, m.requests || [], m.totals, m.requests_kept);
  if (tab === "monitor") renderConvCache(m.conversation_cache);
  if (tab === "about") renderAbout(eng, hw, st);
}

// #596: the conversation cache - the prompt's state the engine keeps between requests (always), and the whole
// conversations it parks in RAM when "--conversation-cache-mib N" is in the run config's args (opt-in)
function since(t) {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "just now" : s < 3600 ? `${fmt(s / 60)} min ago` : `${fmt(s / 3600, 1)} h ago`;
}
function renderConvCache(c) {
  $("cc-card").hidden = !c;
  if (!c) return;                                  // an older server
  const pct = (a, b) => (b ? `${Math.min(100, (100 * a) / b)}%` : "0%");
  $("cc-bars").hidden = !c.enabled;
  if (c.enabled) {
    $("cc-slots-text").textContent = `${fmt(c.parked)} / ${fmt(c.slots)}`;
    $("cc-slots-bar").style.width = pct(c.parked, c.slots);
    const budget = c.budget_mib * 1048576;
    $("cc-mem-text").textContent = `${gb(c.bytes)} / ${gb(budget)} GB`;
    $("cc-mem-bar").style.width = pct(c.bytes, budget);
  }
  $("cc-sum").textContent = c.requests ? `${fmt(c.requests_reused)} of ${fmt(c.requests)} requests reused part of their prompt` : "";
  const share = c.prompt_tokens ? ` (${fmt((100 * c.reused_tokens) / c.prompt_tokens)}% of all prompt tokens)` : "";
  const event = c.last_event ? `${c.last_event === "parked" ? "Parked" : "Restored"} ${fmt(c.last_tokens)} tokens, ${since(c.last_at)}` : null;
  facts($("cc-facts"), [
    ["Last request", c.last_prompt != null ? `${fmt(c.last_reused || 0)} of ${fmt(c.last_prompt)} prompt tokens reused` : null],
    ["Reused since start", c.requests ? `${fmt(c.reused_tokens)} tokens${share}` : null],
    ["Parked / restored", c.enabled ? `${fmt(c.parks)} / ${fmt(c.restores)}${c.evictions ? ` · ${fmt(c.evictions)} evicted` : ""}` : null],
    ["Last switch", c.enabled ? event : null],
  ]);
  $("cc-note").textContent = c.enabled
    ? "A request that continues a parked conversation gets its state back instead of reading it again; the oldest goes when the slots or the memory are full."
    : "The engine keeps the last conversation's state, so a follow-up reads only what is new. To keep several conversations (agents taking turns), add \"--conversation-cache-mib\", \"8192\" to the run config's args (docs/DETAILS.md).";
}

function renderTotals(t) {
  if (!t || !t.requests) return "";
  const since = new Date(t.since * 1000).toLocaleString([], {weekday: "short", hour: "2-digit", minute: "2-digit"});
  const read = t.prompt_tokens - t.reused;
  const pSpeed = t.prompt_ms > 0 && read > 0 ? ` at ${fmt(read / (t.prompt_ms / 1000))} tok/s` : "";
  const oSpeed = t.decode_ms > 0 && t.output_tokens > 0 ? ` at ${fmt(t.output_tokens / (t.decode_ms / 1000), 1)} tok/s` : "";
  return `Since ${since}: ${fmt(t.requests)} requests · ${fmt(read)} prompt tokens read${pSpeed} (${fmt(t.reused)} reused) · ` +
         `${fmt(t.output_tokens)} written${oSpeed}`;
}
function renderMonitor(live, hw, st, eng, h, last, requests, totals, kept) {
  // model state
  const on = live.queued > 0 ? "queued" : live.state;
  for (const b of document.querySelectorAll("#state-badges .st-badge")) b.classList.toggle("on", b.dataset.s === on || b.dataset.s === live.state);
  const prog = $("state-progress");
  let label = "Waiting for a request", detail = "", pct = 0;
  if (live.state === "reading") {
    label = "Reading prompt";
    prog.dataset.tone = "info";
    if (live.prompt_total) {
      pct = (100 * live.prompt_read) / live.prompt_total;
      detail = `${fmt(live.prompt_read)} / ${fmt(live.prompt_total)} tokens · ${fmt(pct)}%`;
    } else {
      detail = `${fmt(live.prompt_tokens)} tokens`;
    }
  } else if (live.state === "generating") {
    label = live.phase ? live.phase[0].toUpperCase() + live.phase.slice(1) : "Generating";
    delete prog.dataset.tone;
    pct = live.max_tokens ? Math.min(100, (100 * live.generated) / live.max_tokens) : 0;
    detail = `${fmt(live.generated)} tokens · ${fmt(live.tok_s, 1)} tok/s`;
  } else if (last) {
    delete prog.dataset.tone;
    detail = `last: ${fmt(last.output_tokens)} tokens${last.decode_tok_s ? ` at ${fmt(last.decode_tok_s, 1)} tok/s` : ""}`;
  }
  $("state-label").textContent = label;
  $("state-detail").textContent = detail;
  $("state-bar").style.width = `${pct}%`;

  // the eight cards
  const speed = live.state === "generating" ? live.tok_s : last ? last.decode_tok_s : null;
  setMetric("speed", speed == null ? null : fmt(speed, 1), "t/s",
            live.state === "generating" ? "Decode now" : last ? "Decode last request" : "Decode");
  const prefill = live.state !== "idle" ? live.prefill_tok_s_mean
                : last && last.prompt_ms > 0 ? Math.max(0, last.prompt_tokens - (last.reused || 0)) / (last.prompt_ms / 1000) : null;
  setMetric("prefill", prefill == null ? null : fmt(prefill), "t/s",
            live.state === "reading" ? "Prefill now" : live.state === "generating" ? "Prefill this request" : last ? "Prefill last request" : "Prefill");
  spark("sp-speed", h.tok_s);
  spark("sp-prefill", h.prefill_tok_s_mean);
  // a model split across several cards (issue #112): the cards show their total / mean / hottest, and each card's own
  const per = (f) => (hw.gpus || []).map((g) => `GPU ${g.index} ${f(g)}`).join(" · ");
  const multi = (hw.gpus || []).length > 1;
  setMetric("gpu", hw.gpu_util == null ? null : fmt(hw.gpu_util), "%",
            multi ? per((g) => (g.util == null ? "–" : `${fmt(g.util)}%`)) : st.gpu_name || (st.gpu_note ? "not available" : ""));
  spark("sp-gpu", h.gpu_util, 100);
  setMetric("vram", hw.gpu_mem_used == null ? null : gb(hw.gpu_mem_used), hw.gpu_mem_total ? `/ ${gb(hw.gpu_mem_total, 0)} GB` : "GB",
            multi ? per((g) => (g.mem_used == null ? "–" : `${gb(g.mem_used)} GB`))
                  : eng.expert_slots ? `${fmt(eng.expert_slots)} experts cached` : (st.gpu_note ? "not available on Windows AMD yet" : ""));
  spark("sp-vram", h.gpu_mem_used, hw.gpu_mem_total);
  setMetric("temp", hw.gpu_temp == null ? null : fmt(hw.gpu_temp), "°C",
            multi ? per((g) => (g.temp == null ? "–" : `${fmt(g.temp)}°`)) : "");
  spark("sp-temp", h.gpu_temp, 90);
  setMetric("power", hw.gpu_power == null ? null : fmt(hw.gpu_power), "W", hw.gpu_power_limit ? `of ${fmt(hw.gpu_power_limit)} W limit` : "");
  spark("sp-power", h.gpu_power, hw.gpu_power_limit);
  const gen = hw.gpu_pcie_gen_max || hw.gpu_pcie_gen;
  setMetric("pcie", gen ? `Gen${gen}` : null, hw.gpu_pcie_width ? `x${hw.gpu_pcie_width}` : "",
            hw.gpu_pcie_rx_mb == null ? "" : `to GPU ${fmt(hw.gpu_pcie_rx_mb, hw.gpu_pcie_rx_mb < 10 ? 1 : 0)} MB/s` +
            (hw.gpu_pcie_gen && gen && hw.gpu_pcie_gen < gen ? ` · idle Gen${hw.gpu_pcie_gen}` : ""));
  spark("sp-pcie", h.gpu_pcie_rx_mb);
  setMetric("cpu", hw.cpu == null ? null : fmt(hw.cpu), "%", st.threads ? `${st.cores ? `${st.cores} cores · ` : ""}${st.threads} threads` : "");
  spark("sp-cpu", h.cpu, 100);
  if (hw.disk_read_mb == null) {
    setMetric("disk", null, "", st.psutil ? "" : "needs psutil (setup installs it)");
  } else {
    const big = hw.disk_read_mb >= 1000;
    setMetric("disk", big ? fmt(hw.disk_read_mb / 1024, 2) : fmt(hw.disk_read_mb, hw.disk_read_mb < 10 ? 1 : 0), big ? "GB/s" : "MB/s",
              hw.disk_write_mb == null ? "" : `write ${fmt(hw.disk_write_mb, 1)} MB/s`);
  }
  spark("sp-disk", h.disk_read_mb);

  // context fill: the running request, else the last one
  const ctx = eng.max_context || 0;
  let used = 0;
  if (live.state !== "idle") used = (live.prompt_tokens || 0) + (live.generated || 0);
  else if (last) used = (last.prompt_tokens || 0) + (last.output_tokens || 0);
  const frac = ctx ? Math.min(1, used / ctx) : 0;
  $("ctx-fill").setAttribute("stroke-dasharray", `${(235.6 * frac).toFixed(1)} 314.2`);
  $("ctx-fill").style.opacity = 235.6 * frac >= 3 ? "1" : "0";         // a near-zero arc would draw just its round cap
  $("ctx-pct").textContent = `${Math.round(frac * 100)}%`;
  $("ctx-sub").textContent = ctx ? `${kfmt(used)} / ${ctxfmt(ctx)}` : "–";
  const cacheBytes = (eng.expert_cache_mib || 0) * 1048576;
  $("slots-text").textContent = eng.expert_slots ? `${fmt(eng.expert_slots)} · ${gb(cacheBytes)} GB` : "–";
  $("slots-bar").style.width = hw.gpu_mem_total ? `${Math.min(100, (100 * cacheBytes) / hw.gpu_mem_total)}%` : "0%";
  $("ram-text").textContent = hw.ram_total ? `${gb(hw.ram_used)} / ${gb(hw.ram_total, 0)} GB` : "–";
  const ramPct = hw.ram_total ? (100 * hw.ram_used) / hw.ram_total : 0;
  $("ram-bar").style.width = `${ramPct}%`;
  if (ramPct > 92) $("ram-progress").dataset.tone = "danger"; else delete $("ram-progress").dataset.tone;
  $("temp-text").textContent = hw.gpu_temp == null ? "–" : `${fmt(hw.gpu_temp)} °C`;
  $("temp-bar").style.width = hw.gpu_temp == null ? "0%" : `${Math.min(100, hw.gpu_temp)}%`;

  // recent requests
  const body = $("req-body");
  if (!requests.length) {
    body.innerHTML = `<tr><td colspan="9" class="muted">No requests yet</td></tr>`;
  } else {
    const badge = {stop: ["", "Done"], length: ["", "Max tokens"], cancel: ["st-badge--queued", "Stopped"],
                   disconnect: ["st-badge--queued", "Closed"], error: ["st-badge--error", "Error"]};
    body.innerHTML = requests.slice(0, reqShowAll ? requests.length : 12).map((r) => {
      const [cls, text] = badge[r.finish] || ["", r.finish || "–"];
      const t = new Date(r.time * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      const proj = r.projection == null ? "" : ` <span class="st-badge${r.projection ? " st-badge--reading" : ""}" title="experimental speed projection ${r.projection ? "on" : "off"}">${r.projection ? "ESP" : "stock"}</span>`;
      // #588: the VRAM share; the PCIe share (--pcie-frac) beside it when there is one
      const hit = r.hit_rate == null ? "–" : `${(r.hit_rate * 100).toFixed(1)}%` +
        (r.pcie_share ? ` <span class="muted" title="routed experts the GPU read over PCIe (--pcie-frac) or another GPU computed">+${(r.pcie_share * 100).toFixed(1)}% PCIe</span>` : "");
      // Reused tokens are not prefetched again. Match the Monitor's existing Prefill metric.
      const fresh = r.prompt_tokens == null ? null : Math.max(0, r.prompt_tokens - (r.reused || 0));
      const prefillRate = fresh > 0 && r.prompt_ms > 0 ? fresh / (r.prompt_ms / 1000) : null;
      return `<tr><td>${esc(t)}</td><td><span class="st-badge ${cls}">${esc(text)}</span>${proj}</td><td class="num">${fmt(r.prompt_tokens)}</td>
        <td class="num">${fmt(r.reused)}</td><td class="num">${fmt(r.output_tokens)}</td><td class="num">${fmt(prefillRate)}</td><td class="num">${fmt(r.decode_tok_s, 1)}</td>
        <td class="num">${hit}</td><td class="num">${fmt(r.duration_s, 1)} s</td></tr>`;
    }).join("");
  }
  const all = $("req-all");
  kept = kept == null ? requests.length : kept;
  all.hidden = kept <= 12;
  all.textContent = reqShowAll ? "Show fewer" : `Show all (${kept})`;
  $("req-wrap").classList.toggle("all", reqShowAll);
  $("req-totals").textContent = renderTotals(totals);
}

function facts(el, rows) {
  el.innerHTML = rows.filter((r) => r[1] != null && r[1] !== "").map(([k, v, copy]) =>
    `<dt>${esc(k)}</dt><dd>${copy ? `<code>${esc(v)}</code><button class="st-btn st-btn--icon" data-copy="${esc(v)}" aria-label="Copy">${icon("copy")}</button>` : esc(v)}</dd>`).join("");
}
// INFO cvec=project:4-44[:singleL] | add:A-B | 0
function projectionText(c) {
  if (!c || c === "0" || c === 0) return null;
  const [mode, range, single] = String(c).split(":");
  const [a, b] = (range || "").split("-");
  return `${mode === "project" ? "Projection" : "Additive"} control vector on layers ${a}–${b}` +
         `${single ? ` (layer ${single.replace("single", "")}'s direction)` : ""}. Per chat in Sampling. Its package ` +
         "describes the vector as a refusal-direction projection; measure the speed yourself";
}
function renderAbout(eng, hw, st) {
  const kv = {int8: "8-bit", q4_0: "4-bit (Hadamard-rotated)", fp16: "16-bit"}[eng.kv] || eng.kv;
  facts($("facts-engine"), [
    ["Model", eng.model],
    ["Engine", eng.version ? `v${eng.version}` : "built from source"],
    ["Context", eng.max_context ? `${fmt(eng.max_context)} tokens` : null],
    ["KV cache", kv ? `${kv}${eng.kv_resident ? `, streamed: ${fmt(eng.kv_resident)} positions per layer in VRAM, the rest in RAM` : ", all in VRAM"}` : null],
    ["Experts in VRAM", eng.expert_slots ? `${fmt(eng.expert_slots)} (${gb((eng.expert_cache_mib || 0) * 1048576)} GB)` : null],
    ["Speculation", eng.spec ? `MTP drafts up to ${Math.max(0, (eng.mtp_max || eng.spec) - 1)} tokens${eng.lookup ? ", prompt lookup on" : ""}` : null],
    ["Images", eng.images ? "on" : "off"],
    ["Experimental speed projection", projectionText(eng.cvec)],
  ]);
  facts($("facts-hw"), [
    ["GPU", st.gpu_name ? `${st.gpu_name}${hw.gpu_mem_total ? `, ${gb(hw.gpu_mem_total, 0)} GB` : ""}` : (st.gpu_note || "not readable (NVML)")],
    ["CPU", st.cpu_name ? `${st.cpu_name}${st.threads ? `, ${st.threads} threads` : ""}` : null],
    ["RAM", hw.ram_total ? `${gb(hw.ram_total, 0)} GB` : null],
  ]);
  const base = location.origin;
  facts($("facts-api"), [
    ["OpenAI base URL", `${base}/v1`, true],
    ["Anthropic base URL", base, true],
    ["Model name", eng.model, true],
  ]);
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) copyText(b.dataset.copy, b);
});
$("req-all").addEventListener("click", () => { reqShowAll = !reqShowAll; if (lastMetrics) render(lastMetrics); });

// ------------------------------------------------------------------ MCP servers (GET /mcp)
// Tools from the MCP servers in the run config: the chat offers them to the model (opt-in per request,
// "strata_mcp": true, which only this page sends); the Monitor lists the servers and what they offer.
let mcpInfo = {servers: [], tools: 0}, mcpRetry = null;
async function loadMcp() {
  try {
    const r = await fetch("mcp", {headers: headers()});
    if (!r.ok) return;
    mcpInfo = await r.json();
  } catch (e) { return; /* an older server: no MCP */ }
  renderMcp();
  clearTimeout(mcpRetry);                          // right after the start, servers may still be starting (npx downloads)
  if ((mcpInfo.servers || []).some((s) => s.status === "starting")) mcpRetry = setTimeout(loadMcp, 3000);
}
const MCP_STATE = {ready: ["st-badge--generating", "Connected"], starting: ["st-badge--reading", "Starting"],
                   failed: ["st-badge--error", "Failed"], stopped: ["st-badge--queued", "Stopped"], idle: ["", "Waiting"]};
function renderMcp() {
  const servers = mcpInfo.servers || [];
  $("mcp-card").hidden = !servers.length;
  $("mcp-row").hidden = !servers.length;
  const ready = servers.filter((s) => s.status === "ready" || s.status === "stopped");
  $("mcp-sum").textContent = servers.length ? `${fmt(mcpInfo.tools)} tools · ${ready.length} of ${servers.length} servers connected` : "";
  $("mcp-row-sub").textContent = mcpInfo.tools ? `${fmt(mcpInfo.tools)} tools from ${ready.map((s) => s.name).join(", ")}; the model calls them when it decides to`
                                               : "no server is connected yet (see the Monitor)";
  $("mcp-list").innerHTML = servers.map((s) => {
    const [cls, text] = MCP_STATE[s.status] || ["", s.status];
    const info = s.info && s.info.name ? ` · ${s.info.name}${s.info.version ? ` ${s.info.version}` : ""}` : "";
    return `<div class="mcp-server"><div class="mcp-server__head"><span class="st-badge ${cls}">${esc(text)}</span>` +
      `<strong>${esc(s.name)}</strong><span class="muted small">${esc(s.transport)} · ${fmt(s.tools.length)} tools${esc(info)}</span></div>` +
      (s.error ? `<div class="msg-error">${esc(s.error)}</div>` : "") +
      (s.tools.length ? `<div class="mcp-server__tools">${s.tools.map((t) => `<span class="chip" title="${esc(t.description || "")}">${esc(t.tool)}</span>`).join("")}</div>` : "") +
      `</div>`;
  }).join("");
  renderPerm();
}
// The permission mode (the drop-down beside the composer's buttons): what the model may do with the tools.  The server
// enforces it (serve/mcp.py MODES); this page only sends the choice with each request and shows the approval buttons.
let perm = store.get("perm", null);
function permMode() {
  const info = mcpInfo.permissions;
  if (!info) return "ask";
  return info.modes.some((m) => m.id === perm) ? perm : info.default;
}
function renderPerm() {
  const info = mcpInfo.permissions, wrap = $("perm-wrap");
  wrap.hidden = !(info && mcpInfo.tools > 0) || settings.mcp === false;
  if (wrap.hidden) return;
  const sel = $("perm-select");
  sel.innerHTML = info.modes.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
  sel.value = permMode();
  sel.title = (info.modes.find((m) => m.id === sel.value) || {}).hint || "";
  wrap.dataset.mode = sel.value;
}
$("perm-select").onchange = () => {
  const v = $("perm-select").value;
  if (v === "full" && !confirm("Full access lets the model use every tool without asking you first, including " +
                               "deleting and moving files and running commands.\n\nUse it only with tools and folders you trust.")) {
    renderPerm();
    return;
  }
  perm = v;
  store.set("perm", perm);
  renderPerm();
};

// ------------------------------------------------------------------ Model settings (GET / POST /config, #564)
// A few documented keys of the run config (strata-<model>.json), for every client, from the next start on.  The
// server lists them, checks every value and keeps every other key of the file as it is.
let cfgKeys = [];
async function loadConfig() {
  let r;
  try { r = await fetch("config", {headers: headers()}); } catch (e) { return; }
  if (!r.ok) { $("cfg-card").hidden = true; return; }       // no run config, an older server, or no key yet
  const c = await r.json();
  cfgKeys = c.keys || [];
  $("cfg-file").textContent = c.file || "";
  $("cfg-form").innerHTML = cfgKeys.map((k, i) => {
    const id = `cfg-${i}`, v = k.value;
    let input;
    if (k.kind === "bool" || k.kind === "enum") {
      const opts = k.kind === "bool" ? [["true", "on"], ["false", "off"]] : k.choices.map((x) => [x, x]);
      const cur = v == null ? "" : String(v);
      input = `<select class="st-input" id="${id}"><option value=""${cur === "" ? " selected" : ""}>default</option>` +
        opts.map(([val, text]) => `<option value="${esc(val)}"${cur === val ? " selected" : ""}>${esc(text)}</option>`).join("") + `</select>`;
    } else {
      const text = v == null ? "" : Array.isArray(v) ? v.join(", ") : String(v);
      input = `<input class="st-input" id="${id}" ${k.kind === "number" ? 'type="number" step="any" min="0"' : 'type="text"'} ` +
        `value="${esc(text)}" placeholder="default" autocomplete="off">`;
    }
    return `<label for="${id}" title="${esc(k.help)}">${esc(k.help)}<code>${esc(k.key)}</code></label>${input}`;
  }).join("");
  $("cfg-card").hidden = false;
}
function configValue(k, el) {
  const s = el.value.trim();
  if (s === "") return null;
  if (k.kind === "bool") return s === "true";
  if (k.kind === "number") return Number(s);
  return s;                                         // enum, or names (the server splits them at commas)
}
$("cfg-save").addEventListener("click", async () => {
  const set = {};
  cfgKeys.forEach((k, i) => {
    const v = configValue(k, $(`cfg-${i}`));
    const old = Array.isArray(k.value) ? k.value.join(", ") : k.value;
    if (JSON.stringify(v) !== JSON.stringify(old ?? null)) set[k.key] = v;
  });
  if (!Object.keys(set).length) { $("cfg-msg").textContent = "Nothing changed."; return; }
  try {
    const r = await fetch("config", {method: "POST", headers: headers(true), body: JSON.stringify({set})});
    const b = await r.json();
    if (!r.ok) throw new Error((b.error || {}).message || `HTTP ${r.status}`);
    $("cfg-msg").textContent = b.changed.length
      ? `Saved (${b.changed.join(", ")}); the earlier file is ${b.file}.bak. Start the model again to use it.` : "Nothing changed.";
    loadConfig();
  } catch (e) {
    $("cfg-msg").textContent = "";
    toast("error", "Not saved", String(e.message || e), 6000);
  }
});

// ------------------------------------------------------------------ side panel: Browser and Gallery (on the right of the chat)
// Links in the chat open in the Browser tab instead of a separate window (Ctrl-click still opens your own browser); the
// pictures of the chat are the Gallery.  A site that forbids being framed is said so (the server checks: GET /panel/frameable)
// and offered in your own browser.
const panelState = {open: false, tab: "browser", url: ""};
let galleryAt = -1;
function galleryItems() {
  return messages.flatMap((m) => (m.images || []).filter((i) => i.url).map((i) => ({url: i.url, name: i.name || "picture"})));
}
function updateGalleryCount() {
  const n = galleryItems().length;
  $("gallery-count").textContent = n ? String(n) : "";
}
function setPanel(open, tab) {
  panelState.open = open;
  if (tab) panelState.tab = tab;
  $("sidepanel").hidden = !open;
  $("panel-toggle").setAttribute("aria-expanded", String(open));
  for (const b of document.querySelectorAll(".sidepanel__tab")) b.setAttribute("aria-selected", String(b.dataset.ptab === panelState.tab));
  $("panel-browser").hidden = panelState.tab !== "browser";
  $("panel-gallery").hidden = panelState.tab !== "gallery";
  if (open && panelState.tab === "gallery") renderGallery();
}
function panelUrl(raw) {
  // an http(s) page, and not this server's own page: framed with allow-same-origin it would run inside Strata with Strata's
  // rights.  A page of this PC on another port is fine (a development server, Strata's own monitor page); the server answers
  // for the rest, so an address of another machine on the local network is refused there and said there, not here.
  let u;
  try { u = new URL(/^[a-z][a-z0-9+.-]*:/i.test(raw.trim()) ? raw.trim() : "https://" + raw.trim()); } catch (e) { return null; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  if (u.username || u.password || u.origin === location.origin) return null;
  return u;
}
function openExternal(href) {
  const a = document.createElement("a");
  a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
  document.body.appendChild(a); a.click(); a.remove();
}
async function openInPanel(raw) {
  const u = panelUrl(raw);
  if (!u) { toast("warn", "Not shown here", "The side panel shows a web page (http or https) - not Strata's own page, and not an address with a password in it."); return; }
  setPanel(true, "browser");
  const frame = $("panel-frame"), note = $("panel-notice");
  panelState.url = u.href;
  $("panel-url").value = u.href;
  $("panel-empty").hidden = true;
  note.hidden = true;
  frame.hidden = true;
  frame.removeAttribute("src");
  let info = null;
  try { info = await sessionApi("panel/frameable?url=" + encodeURIComponent(u.href)); } catch (e) { /* unsure: try showing it */ }
  if (panelState.url !== u.href) return;                                     // another page was opened meanwhile
  if (info && info.frameable === false) {
    note.textContent = "";
    const t = document.createElement("div");
    t.textContent = "This page can't be shown inside Strata: " + (info.reason || "the site does not allow it") + ".";
    const b = document.createElement("button");
    b.type = "button"; b.className = "st-btn st-btn--primary"; b.textContent = "Open in your browser";
    b.onclick = () => openExternal(u.href);
    note.append(t, b);
    note.hidden = false;
    return;
  }
  frame.src = u.href;
  frame.hidden = false;
}
function renderGallery() {
  const items = galleryItems();
  updateGalleryCount();
  if (galleryAt >= items.length) galleryAt = items.length - 1;
  const grid = $("gallery-grid");
  grid.textContent = "";
  items.forEach((it, i) => {
    const b = document.createElement("button");
    b.type = "button"; b.title = it.name;
    const img = document.createElement("img");
    img.src = it.url; img.alt = it.name;
    b.appendChild(img);
    b.onclick = () => showPicture(i);
    grid.appendChild(b);
  });
  $("gallery-empty").hidden = items.length > 0;
  grid.hidden = items.length === 0 || galleryAt >= 0;
  $("gallery-viewer").hidden = galleryAt < 0;
  if (galleryAt >= 0) {
    const it = items[galleryAt];
    $("gallery-img").src = it.url;
    $("gallery-img").alt = it.name;
    $("gallery-name").textContent = `${galleryAt + 1} of ${items.length} · ${it.name}`;
    $("gallery-save").href = it.url;
    $("gallery-save").download = it.name;
  }
}
function showPicture(i) { galleryAt = i; renderGallery(); }
function openGallery(url) {
  const at = galleryItems().findIndex((it) => it.url === url);
  galleryAt = at;
  setPanel(true, "gallery");
  renderGallery();
}
$("panel-toggle").onclick = () => setPanel(!panelState.open);
$("sidepanel-close").onclick = () => setPanel(false);
for (const b of document.querySelectorAll(".sidepanel__tab")) b.onclick = () => { if (b.dataset.ptab === "gallery") galleryAt = -1; setPanel(true, b.dataset.ptab); };
$("panel-form").onsubmit = (e) => { e.preventDefault(); if ($("panel-url").value.trim()) openInPanel($("panel-url").value); };
$("panel-reload").onclick = () => { if (panelState.url) openInPanel(panelState.url); };
$("panel-external").onclick = () => { const raw = $("panel-url").value.trim() || panelState.url; const u = raw && panelUrl(raw); if (u) openExternal(u.href); };
$("gallery-back").onclick = () => { galleryAt = -1; renderGallery(); };
$("gallery-prev").onclick = () => { const n = galleryItems().length; if (n) showPicture((galleryAt - 1 + n) % n); };
$("gallery-next").onclick = () => { const n = galleryItems().length; if (n) showPicture((galleryAt + 1) % n); };
document.addEventListener("keydown", (e) => {
  if (!panelState.open || panelState.tab !== "gallery" || galleryAt < 0 || /^(INPUT|TEXTAREA|SELECT)$/.test((e.target || {}).tagName || "")) return;
  if (e.key === "ArrowLeft") $("gallery-prev").click();
  else if (e.key === "ArrowRight") $("gallery-next").click();
  else if (e.key === "Escape") $("gallery-back").click();
});
// a link in the chat opens in the panel (Ctrl / Shift / middle click: your own browser, as before)
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest("#chat a[href]");
  if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || !panelUrl(a.getAttribute("href"))) return;
  e.preventDefault();
  openInPanel(a.getAttribute("href"));
});
// the panel's width: drag its left edge (kept between visits)
(function () {
  const panel = $("sidepanel"), grip = $("sidepanel-resize"), saved = +store.get("panel_w", 0);
  if (saved >= 320) panel.style.setProperty("--panel-w", saved + "px");
  grip.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("dragging");
    $("panel-frame").style.pointerEvents = "none";                            // the page inside would swallow the drag
    const right = panel.getBoundingClientRect().right, room = panel.parentElement.getBoundingClientRect().width;
    const move = (ev) => { const w = Math.max(320, Math.min(room * 0.75, right - ev.clientX)); panel.style.setProperty("--panel-w", w + "px"); };
    const up = () => {
      grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up);
      grip.classList.remove("dragging");
      $("panel-frame").style.pointerEvents = "";
      store.set("panel_w", Math.round(panel.getBoundingClientRect().width));
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  });
})();

// ------------------------------------------------------------------ Chat
const DEFAULTS = {thinking: "high", temperature: 0.6, top_p: 0.95, top_k: 20, max: "", seed: "", show: true, esp: true, mcp: true};
let settings = {...DEFAULTS, ...store.get("sampling", {})};
let messages = store.get("chat", []);
let attachments = [];                 // {name, url}
let busy = null;   // the answer in flight: {id, controller, msg, list, el} - id/list are the chat it belongs to,
                   // el is its row on screen, or null while that chat is not the one being read

function timeStr(t) { return new Date(t).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }

// ------------------------------------------------------------------ Sessions (separate chats, kept by the server)
// With a run config the server keeps each chat in a file (serve/sessions.py) and the list sits on the left; without
// one (or when the server has no sessions folder) the page keeps one chat in the browser, as before.
let sessionsOn = false;
let savesRunning = 0;                     // saves not finished yet (the autosave skips a beat while one is)
let sessionList = [];                     // [{id, title, updated, messages, custom_title}]
let sessionOrdered = false;               // the user dragged the chats into their own order (the server keeps it): no day groups
let current = {id: null, title: "New chat", custom: false};
let saveTimer = null, saving = Promise.resolve();
let drafts = new Map();            // half-typed questions, one per chat: switching chats must not carry one along

// An answer that was still being written when the page, the app or the model stopped: its text and the tool calls it had
// finished were saved on the way; it is marked, and 'continue' carries on from what it did.
function fixLoaded(list) {
  const out = Array.isArray(list) ? list : [];
  for (const m of out) {
    if (!m || !m.partial) continue;
    delete m.partial;
    m.interrupted = true;
    for (const t of m.tools || []) if (t.state === "writing" || t.state === "running" || t.state === "waiting" || t.state === "asking") { t.state = "skipped"; t.ms = null; }
    m.meta = "Stopped before it finished (the app or the model was closed). What it did so far is kept; say continue to pick up from here.";
  }
  return out;
}
function newSessionId() { return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`; }
function autoTitle() { return autoTitleOf(messages); }
// the same for a chat that is not the one on screen: an answer arriving in the background still needs a title
function autoTitleOf(list) {
  const first = list.find((m) => m.role === "user" && (m.text || "").trim());
  const t = first ? first.text.replace(/\s+/g, " ").trim() : "";
  return t ? (t.length > 60 ? t.slice(0, 57) + "…" : t) : "New chat";
}
function chatPayload() { return chatPayloadOf(messages); }
function chatPayloadOf(list) {
  return list.map((m) => ({...m, images: (m.images || []).map((i) => ({name: i.name})),
                            files: (m.files || []).map((f) => ({name: f.name}))}));
}
async function sessionApi(path, body) {
  const r = await fetch(path, body === undefined ? {headers: headers(false)}
                                                 : {method: "POST", headers: headers(true), body: JSON.stringify(body), keepalive: JSON.stringify(body).length < 60000});
  if (!r.ok) { let msg = `HTTP ${r.status}`; try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ } throw new Error(msg); }
  return r.json();
}
// any chat, by id, from the list that holds it - the list is not always `messages`, because an answer can be
// arriving in a chat nobody is looking at
function saveSession(id, list, custom, title) {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!sessionsOn || !id || !list.length) return saving;                   // an empty chat is not kept
  if (!custom) title = autoTitleOf(list);
  const body = {title, custom_title: custom, messages: chatPayloadOf(list)};
  savesRunning++;
  saving = saving.then(() => sessionApi(`sessions/${id}`, body)).then((meta) => {
    if (current.id === id) current.syncedAt = meta.updated;                  // this window is in step with the server up to here
    const at = sessionList.findIndex((s) => s.id === id);
    if (at >= 0) sessionList[at] = meta; else sessionList.unshift(meta);
    if (!sessionOrdered) sessionList.sort((a, b) => b.updated - a.updated);       // a dragged order keeps its places; a new chat is on top
    renderSessions();
  }).catch((e) => { toast("error", "The chat was not saved", e.message, 6000); }).finally(() => { savesRunning--; });
  return saving;
}
function saveNow() {
  if (!sessionsOn || !current.id) return saving;
  if (!current.custom) current.title = autoTitle();
  return saveSession(current.id, messages, current.custom, current.title);
}
function saveChat() {
  updateGalleryCount();
  if (panelState.open && panelState.tab === "gallery") renderGallery();   // a picture just sent shows up
  if (!sessionsOn) {
    store.set("chat", chatPayload());
    return;
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 400);
  $("chat-title").textContent = current.custom ? current.title : autoTitle();
}
function dayLabel(t) {
  const d = new Date(t * 1000), now = new Date(), day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  return diff <= 0 ? "Today" : diff === 1 ? "Yesterday" : diff < 7 ? "Previous 7 days" : "Older";
}
// ---- dragging the chats into the user's own order (kept by the server: POST /sessions-order)
let dragId = null;
function clearDropMarks() {
  document.querySelectorAll(".sess--before, .sess--after").forEach((el) => el.classList.remove("sess--before", "sess--after"));
}
function wireDrag(row, id) {
  row.draggable = true;
  row.addEventListener("dragstart", (e) => {
    dragId = id;
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", id); } catch (err) { /* some webviews refuse */ }
    row.classList.add("sess--drag");
  });
  row.addEventListener("dragend", () => { dragId = null; row.classList.remove("sess--drag"); clearDropMarks(); });
  row.addEventListener("dragover", (e) => {
    if (!dragId || dragId === id) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const r = row.getBoundingClientRect(), after = e.clientY > r.top + r.height / 2;
    clearDropMarks();
    row.classList.add(after ? "sess--after" : "sess--before");
  });
  row.addEventListener("drop", (e) => {
    if (!dragId || dragId === id) return;
    e.preventDefault();
    const r = row.getBoundingClientRect();
    const moving = dragId;
    dragId = null;
    clearDropMarks();
    moveSession(moving, id, e.clientY > r.top + r.height / 2);
  });
}
function moveSession(id, targetId, after) {
  const from = sessionList.findIndex((s) => s.id === id);
  if (from < 0 || id === targetId) return;
  const [item] = sessionList.splice(from, 1);
  const to = sessionList.findIndex((s) => s.id === targetId);
  sessionList.splice(to < 0 ? sessionList.length : to + (after ? 1 : 0), 0, item);
  sessionOrdered = true;
  renderSessions();
  sessionApi("sessions-order", {ids: sessionList.map((s) => s.id)}).catch((e) => toast("error", "The order was not saved", e.message, 6000));
}
async function resetSessionOrder() {
  try {
    await sessionApi("sessions-order", {ids: null});
    sessionOrdered = false;
    sessionList.sort((a, b) => b.updated - a.updated);
    renderSessions();
  } catch (e) { toast("error", "Could not sort by date", e.message, 6000); }
}
function renderSessions() {
  const box = $("sessions-list");
  box.textContent = "";
  let last = "";
  let shown = sessionList.some((s) => s.id === current.id) || !messages.length
    ? sessionList : [{id: current.id, title: autoTitle(), updated: Date.now() / 1000}, ...sessionList];
  // a search box only earns its place once there are chats to find; with three chats it is an empty box
  // asking a question you do not have. It filters as you type, and vanishes when no longer wanted - so a
  // query left in it while it is hidden must not silently hide chats.
  const search = $("sessions-search");
  const enough = sessionList.length > 4;
  if (search) { search.hidden = !enough; if (!enough) search.value = ""; }
  const q = (enough && search ? search.value : "").trim().toLowerCase();
  if (q) shown = shown.filter((s) => (s.title || "").toLowerCase().includes(q));
  for (const s of shown) {
    const label = sessionOrdered ? "Your order" : dayLabel(s.updated);
    if (label !== last) {
      const h = document.createElement("div");
      h.className = "sessions__day";
      h.textContent = label;
      if (sessionOrdered) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "sessions__reset";
        b.textContent = "Sort by date";
        b.title = "Go back to newest first";
        b.onclick = resetSessionOrder;
        h.appendChild(b);
      }
      box.appendChild(h);
      last = label;
    }
    const row = document.createElement("div");
    row.className = "sess" + (s.id === current.id ? " sess--current" : "") + (busy && busy.id === s.id ? " sess--writing" : "");
    row.dataset.id = s.id;
    row.setAttribute("role", "listitem");
    if (sessionList.some((x) => x.id === s.id)) wireDrag(row, s.id);
    const title = document.createElement("button");
    title.type = "button";
    title.className = "sess__title";
    title.textContent = s.id === current.id ? (current.custom ? current.title : autoTitle()) : s.title;
    title.title = "Open this chat. F2 (or double-click) renames it. Drag to reorder (or Alt+Up / Alt+Down).";
    title.onkeydown = (e) => {
      if (e.key === "F2") { e.preventDefault(); renameSession(s.id); return; }   // double-click is invisible to a keyboard
      if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
      e.preventDefault();
      const at = sessionList.findIndex((x) => x.id === s.id), to = at + (e.key === "ArrowUp" ? -1 : 1);
      if (at < 0 || to < 0 || to >= sessionList.length) return;
      moveSession(s.id, sessionList[to].id, e.key === "ArrowDown");
      const again = box.querySelector(`.sess[data-id="${s.id}"] .sess__title`);
      if (again) again.focus();
    };
    const del = document.createElement("button");
    del.type = "button";
    del.className = "sess__del";
    del.title = "Delete this chat";
    del.setAttribute("aria-label", "Delete this chat");
    del.textContent = "×";
    row.append(title, writingMark(s.id), del);
    box.appendChild(row);
  }
  if (!shown.length) {
    const e = document.createElement("div");
    e.className = "sessions__empty muted small";
    e.textContent = q ? `Nothing matches “${($("sessions-search").value || "").trim()}”.` : "Your chats appear here.";
    box.appendChild(e);
    if (q) { const c = document.createElement("button"); c.type = "button"; c.className = "sessions__reset";
      c.textContent = "Clear the search"; c.style.marginTop = "8px";
      c.onclick = () => { $("sessions-search").value = ""; $("sessions-search").focus(); renderSessions(); };
      box.appendChild(c); }
  }
  $("chat-title").textContent = current.custom ? current.title : autoTitle();
}
// An answer being written holds its own chat, and only its own chat.  It keeps arriving when you go elsewhere -
// the stream writes into the chat that asked for it, not into whatever happens to be on screen - so browsing the
// list, starting a new chat or asking a second question are all allowed while another chat is busy.  What still
// cannot happen is two answers at once: a question typed while another chat writes waits, and the notice says
// which chat is writing and offers one button that stops that answer and sends this question (the stream ends a
// moment after the abort, so the wait is on the page, not on you).
function writingHere() { return !!busy && busy.id === current.id; }
function writingTitle() {
  if (!busy) return "";
  const at = sessionList.findIndex((s) => s.id === busy.id);
  if (at >= 0 && sessionList[at].title) return sessionList[at].title;
  return busy.list.some((m) => m.role === "user") ? autoTitleOf(busy.list) : "a chat";
}
// the mark on the row in the list: an answer is arriving there, so that chat is not "just sitting there"
function writingMark(id) {
  const w = document.createElement("span");
  w.className = "sess__writing";
  w.hidden = !(busy && busy.id === id);
  w.textContent = "writing";
  w.title = "An answer is being written here; open this chat to watch it";
  return w;
}
// the answer on screen: painted only while its own chat is the one being read, and re-found after a tool round
// has rebuilt the rows underneath it
function paintStream(streaming) {
  if (!busy || !writingHere()) return;
  if (!busy.el || !busy.el.isConnected) busy.el = $("chat").querySelector(`[data-i="${busy.list.indexOf(busy.msg)}"]`);
  if (busy.el) updateAssistant(busy.el, busy.msg, streaming);
  if (streaming) scrollDown();
}
// the chat an answer is going into, whether or not it is the one on screen
function saveWriting() {
  if (!busy) return saving;
  if (busy.id === current.id && busy.list === messages) return saveNow();
  const at = sessionList.findIndex((s) => s.id === busy.id);
  const entry = at >= 0 ? sessionList[at] : {};
  return saveSession(busy.id, busy.list, !!entry.custom_title, entry.title);
}

function stillWriting(then) {
  if (!busy) return false;
  const run = () => {
    if (busy) busy.controller.abort();
    const wait = setInterval(() => { if (busy) return; clearInterval(wait); then(); }, 50);
    setTimeout(() => { clearInterval(wait); }, 5000);           // never leave the click hanging if the stream stalls
  };
  const here = writingHere();
  toast("warn", here ? "Still writing" : `Still writing in “${writingTitle()}”`,
        here ? "Stop the answer first, or wait for it to finish."
             : "It keeps arriving in the background - open it in the list to watch it. One button stops it and asks here instead.",
        9000, {label: "Stop and do it", run});
  return true;
}
// the half-typed question belongs to the chat it was typed in: switching away keeps it there, and switching back
// hands it over again (attachments are in-memory data URLs, so they travel with it)
function stashDraft() {
  if (!current.id) return;
  const text = $("input").value;
  if (text.trim() || attachments.length || drafts.has(current.id)) drafts.set(current.id, {text, attachments});
  attachments = [];
  renderAttachments();
}
function takeDraft() {
  const d = drafts.get(current.id);
  $("input").value = d ? d.text : "";
  attachments = d ? d.attachments : [];
  renderAttachments();
  autosize();
}
async function newSession() {
  stashDraft();
  await saveNow();                                             // the chat being left keeps its own edits
  if (busy) busy.el = null;                                    // its answer keeps arriving, only off screen now
  current = {id: newSessionId(), title: "New chat", custom: false};
  messages = [];
  store.set("session", current.id);
  renderChat();
  renderSessions();
  setBusy(!!busy);                                             // another chat may still be writing: it says which
  renderQueued();
  takeDraft();
  maybeSendQueued();
  $("input").focus();
}
// opening the chat whose answer is arriving: the live text is ahead of the copy on the server, so it is shown
// from the stream instead of fetched, and the stream is pointed back at the rows just drawn
function adoptStream(id) {
  const at = sessionList.findIndex((s) => s.id === id);
  const entry = at >= 0 ? sessionList[at] : {};
  current = {id, title: entry.title || "Chat", custom: !!entry.custom_title, syncedAt: entry.updated || 0};
  messages = busy.list;
  store.set("session", id);
  renderChat();
  busy.el = $("chat").querySelector(`[data-i="${messages.indexOf(busy.msg)}"]`);
  if (busy.el) updateAssistant(busy.el, busy.msg, true);
  renderSessions();
  setBusy(true);
  renderQueued();
  takeDraft();
  maybeSendQueued();
}
async function openSession(id) {
  if (id === current.id) return;
  stashDraft();
  await saveNow();                                             // the chat being left keeps its own edits
  if (busy) busy.el = null;                                    // its answer keeps arriving, only off screen now
  if (busy && busy.id === id) { adoptStream(id); return; }
  try {
    const data = await sessionApi(`sessions/${id}`);
    current = {id, title: data.title || "Chat", custom: !!data.custom_title, syncedAt: data.updated || 0};
    messages = fixLoaded(data.messages);
    store.set("session", id);
    renderChat();
    renderSessions();
  } catch (e) {
    toast("error", "That chat could not be opened", e.message, 6000);
  }
  renderQueued();
  takeDraft();
  maybeSendQueued();
}
async function deleteSession(id) {
  if (busy && busy.id === id && stillWriting(() => deleteSession(id))) return;
  if (!confirm("Delete this chat? It cannot be brought back.")) return;
  try {
    await sessionApi(`sessions/${id}/delete`, {});
  } catch (e) {
    toast("error", "That chat was not deleted", e.message, 6000);
    return;
  }
  sessionList = sessionList.filter((s) => s.id !== id);
  if (id !== current.id) { renderSessions(); return; }
  if (sessionList.length) { current = {id: null, title: "", custom: false}; await openSession(sessionList[0].id); }
  else { current = {id: null, title: "", custom: false}; await newSession(); }
}
async function renameSession(id) {
  const row = sessionList.find((s) => s.id === id) || (id === current.id ? {title: current.title} : null);
  if (!row) return;
  const name = prompt("Name this chat:", id === current.id ? (current.custom ? current.title : autoTitle()) : row.title);
  if (name === null || !name.trim()) return;
  if (id === current.id) {
    current.title = name.trim();
    current.custom = true;
    renderSessions();
    await saveNow();
    return;
  }
  try {
    const data = await sessionApi(`sessions/${id}`);
    const meta = await sessionApi(`sessions/${id}`, {title: name.trim(), custom_title: true, messages: data.messages || []});
    sessionList = sessionList.map((s) => s.id === id ? meta : s);
    renderSessions();
  } catch (e) {
    toast("error", "That chat was not renamed", e.message, 6000);
  }
}
function showSessions(on) {
  $("sessions").hidden = !on;
  $("sessions-toggle").setAttribute("aria-expanded", String(on));
  store.set("sessions_open", on);
}
$("sessions-list").addEventListener("click", (e) => {
  const row = e.target.closest(".sess");
  if (!row) return;
  if (e.target.closest(".sess__del")) deleteSession(row.dataset.id);
  else openSession(row.dataset.id);
});
$("sessions-list").addEventListener("dblclick", (e) => {
  const row = e.target.closest(".sess");
  if (row && !e.target.closest(".sess__del")) renameSession(row.dataset.id);
});
$("chat-title").addEventListener("dblclick", () => { if (current.id) renameSession(current.id); });
if ($("sessions-search")) {
  $("sessions-search").addEventListener("input", renderSessions);
  $("sessions-search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.target.value = ""; renderSessions(); }   // Esc here empties the box, it does not close the list
  });
}
$("session-new").onclick = newSession;
$("sessions-toggle").onclick = () => showSessions($("sessions").hidden);
// leaving the page: what is waiting to be saved goes out now
addEventListener("pagehide", () => { if (saveTimer || busy) saveWriting(); });
// Chats can change behind this window's back - the Discord bot saves its conversations as chats here and continues the ones it is
// given.  Every few seconds, while the window is idle: refresh the list, and reload the open chat when the server has a newer copy.
let listSig = "";
async function pollSessions() {
  // a background answer does not stop this window from refreshing: only the chat being written into is off limits
  if (!sessionsOn || document.hidden || (busy && writingHere()) || savesRunning || saveTimer) return;
  let listing;
  try { listing = await sessionApi("sessions"); } catch (e) { return; }
  const list = listing.sessions || [];
  const sig = list.map((x) => `${x.id}:${x.updated}`).join("|") + (listing.ordered ? "o" : "");
  if (sig === listSig) return;
  listSig = sig;
  sessionList = list;
  sessionOrdered = !!listing.ordered;
  const mine = list.find((x) => x.id === current.id);
  const idleHere = !writingHere();                             // reloading the screen is safe while another chat writes
  if (mine && mine.updated > (current.syncedAt || 0) + 0.001 && idleHere && !savesRunning && !saveTimer) {
    try {
      const data = await sessionApi(`sessions/${current.id}`);
      if (idleHere && !savesRunning && !saveTimer) {
        messages = fixLoaded(data.messages);
        current.syncedAt = data.updated || mine.updated;
        current.title = data.title || current.title;
        renderChat();
      }
    } catch (e) { /* the next poll tries again */ }
  }
  renderSessions();
}
setInterval(pollSessions, 8000);
async function initSessions() {
  let listing;
  try { listing = await sessionApi("sessions"); } catch (e) { return false; }           // no sessions folder: one chat in the browser
  sessionsOn = true;
  sessionList = listing.sessions || [];
  sessionOrdered = !!listing.ordered;
  $("chat-top").hidden = false;
  showSessions(store.get("sessions_open", innerWidth > 900));
  const legacy = messages.length ? messages : null;                                      // the one chat the browser kept so far
  const wanted = store.get("session", null);
  if (legacy && !sessionList.length) {
    current = {id: newSessionId(), title: autoTitle(), custom: false};
    await saveNow();                                                                      // it becomes the first session
    store.remove("chat");
  } else if (wanted && sessionList.some((s) => s.id === wanted)) {
    const data = await sessionApi(`sessions/${wanted}`);
    current = {id: wanted, title: data.title || "Chat", custom: !!data.custom_title, syncedAt: data.updated || 0};
    messages = fixLoaded(data.messages);
  } else if (sessionList.length && !legacy) {
    const data = await sessionApi(`sessions/${sessionList[0].id}`);
    current = {id: sessionList[0].id, title: data.title || "Chat", custom: !!data.custom_title, syncedAt: data.updated || 0};
    messages = fixLoaded(data.messages);
  } else {
    current = {id: newSessionId(), title: "New chat", custom: false};
    if (!legacy) messages = [];
  }
  store.set("session", current.id);
  renderChat();
  renderSessions();
  return true;
}

function msgEl(m, i) {
  const el = document.createElement("div");
  el.className = `st-msg st-msg--${m.role}`;
  el.dataset.i = i;
  if (m.role === "user") {
    if (m.files && m.files.length) {
      const wrap = document.createElement("div");
      wrap.className = "msg-images";
      for (const f of m.files) {
        const c = document.createElement("span");
        c.className = "chip";
        c.innerHTML = icon("attach", "st-icon st-icon--sm");
        c.append(f.name);
        wrap.appendChild(c);
      }
      el.appendChild(wrap);
    }
    if (m.images && m.images.length) {
      const wrap = document.createElement("div");
      wrap.className = "msg-images";
      for (const im of m.images) {
        if (im.url) { const img = document.createElement("img"); img.src = im.url; img.alt = im.name || "image"; img.title = "Open in the gallery"; img.onclick = () => openGallery(im.url); wrap.appendChild(img); }
        else { const c = document.createElement("span"); c.className = "chip"; c.innerHTML = icon("image", "st-icon st-icon--sm"); c.append(im.name || "image"); wrap.appendChild(c); }
      }
      el.appendChild(wrap);
    }
    const b = document.createElement("div");
    b.className = "st-bubble";
    b.textContent = m.text;
    el.appendChild(b);
    const meta = document.createElement("div");
    meta.className = "st-msg__meta";
    meta.innerHTML = `<span class="meta-text"></span>` +
      `<button class="st-btn st-btn--icon" data-msg-copy aria-label="Copy what you asked" title="Copy">${icon("copy")}</button>`;
    meta.querySelector(".meta-text").textContent = `You · ${timeStr(m.time)}`;
    el.appendChild(meta);
  } else {
    el.innerHTML = `<details class="st-collapse think" hidden><summary>${icon("thinking", "st-icon st-icon--sm")}<span class="think-title"></span>` +
      `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary><div class="st-collapse__body thinking"></div></details>` +
      `<div class="st-bubble"></div><div class="st-msg__meta"><span class="meta-text"></span>` +
      `<button class="st-btn st-btn--icon" data-msg-copy aria-label="Copy the answer" title="Copy">${icon("copy")}</button>` +
      `<button class="st-btn st-btn--secondary" data-continue title="Send the answer on further">Continue</button></div>`;
    updateAssistant(el, m, false);
  }
  return el;
}
// One MCP tool call in the answer: a compact block (name, state, a one-line preview) that opens to the arguments and
// the result as the model read it.  Its body is built only while open: a result can be 20,000 characters.
const TOOL_STATE = {writing: ["st-badge--reading", "Writing"], running: ["st-badge--generating", "Running"], done: ["", "Done"],
                    error: ["st-badge--error", "Error"], skipped: ["st-badge--queued", "Not run"],
                    waiting: ["st-badge--queued", "Needs your OK"], blocked: ["st-badge--error", "Blocked"],
                    asking: ["st-badge--queued", "Asks you"]};
const KIND_TEXT = {write: "This tool changes things on this PC.", danger: "This tool can delete, move or run things on this PC.",
                   read: "This tool only looks.", net: "This tool reads from the internet.", ask: "The model is asking you something.",
                   exec: "This runs a command or program on this PC."};
// Why a call was announced but never ran.  There are only four reasons, and the badge has to say which one it was -
// "Not run" on its own reads like a failure of the tool, and is really the turn ending first.
const NOT_RUN = {limit: "stopped at the tool-round limit (mcp.max_rounds)", stop: "you stopped the answer",
                 length: "the answer ran out of tokens before the call ran",
                 // the model finished its own answer with a call still queued: nobody stopped anything, so saying
                 // "you stopped the answer" here accuses the user of what the model did
                 finished: "the answer ended before the call ran"};
function toolHtml(t, k) {
  const [cls, label] = TOOL_STATE[t.state] || ["", t.state];
  const args = t.arguments == null ? "" : JSON.stringify(t.arguments, null, 2);
  const preview = t.result != null ? t.result : args.replace(/\s+/g, " ");
  let body = "";
  if (t.open) {
    if (t.state === "asking") {
      body += `<div class="tool-call__question"><div class="tool-call__q">${esc(t.question || "")}</div>` +
        ((t.options || []).length ? `<div class="tool-call__opts">${t.options.map((o, i) =>
          `<button type="button" class="st-btn st-btn--secondary" data-answer-opt="${i}">${esc(o)}</button>`).join("")}</div>` : "") +
        `<div class="tool-call__reply"><input type="text" class="st-input tool-call__input" maxlength="4000" ` +
        `placeholder="Type an answer and press Enter" aria-label="Your answer">` +
        `<button type="button" class="st-btn st-btn--primary" data-answer-send>Send</button></div></div>`;
    }
    if (t.state === "waiting") {
      const prot = t.exec
        ? `<strong>This runs ${t.tool === "run_python" ? "a Python program" : "a command"} on this PC with your Windows account.</strong> ` +
          `The no-read list and the C: protection do not cover what it does once it runs. Read it below before you click. `
        : t.leak
        ? `<strong>This sends a request to the internet, and the model has read things on this PC in this chat.</strong> Check the address below for anything private. It always needs your OK. `
        : t.protectedPath
        ? `<strong>This changes something in a protected location (${esc(((mcpInfo.permissions || {}).protected_paths || []).join(", ") || "protected")}).</strong> It always needs your OK. `
        : "";
      const why = t.reason ? `<strong>Auto mode asks because ${esc(t.reason)}.</strong> ` : "";
      body += `<div class="tool-call__approve"><span class="tool-call__ask">${why}${prot}${esc(KIND_TEXT[t.kind] || "")} ` +
        `Allow it to run with the arguments below?</span><span class="tool-call__btns">` +
        `<button type="button" class="st-btn st-btn--primary" data-approve="allow">${t.exec ? "Run it" : "Allow once"}</button>` +
        (t.protectedPath ? "" : `<button type="button" class="st-btn st-btn--secondary" data-approve="always" title="Allow this tool for the rest of this server run">Always allow</button>`) +
        `<button type="button" class="st-btn st-btn--danger" data-approve="deny">Deny</button></span></div>`;
    }
    if (t.exec && t.state === "waiting" && t.arguments && (t.arguments.command || t.arguments.code)) {
      body += `<div class="tool-call__label">${t.arguments.code ? "The program" : "The command"}${t.arguments.cwd ? ` (in ${esc(t.arguments.cwd)})` : ""}</div>` +
              `<pre class="tool-call__pre tool-call__cmd">${esc(t.arguments.command || t.arguments.code)}</pre>`;
    }
    body += `<div class="tool-call__label">Arguments</div><pre class="tool-call__pre">${esc(args || "(being written)")}</pre>`;
    if (t.result != null) {
      body += `<div class="tool-call__label">${t.ok ? "Result" : "Error"}${t.chars ? ` · ${fmt(t.chars)} characters` : ""}` +
              `${t.truncated ? ", cut for the model" : ""}</div><pre class="tool-call__pre">${esc(t.result)}</pre>`;
    }
  }
  return `<details class="st-collapse tool-call" data-tool="${k}" data-state="${esc(t.state)}"${t.open ? " open" : ""}>` +
    `<summary>${icon("tool", "st-icon st-icon--sm")}<span class="tool-call__name" title="${esc(t.name || "")}">${esc(t.tool || t.name || "tool")}</span>` +
    (t.server ? `<span class="muted small">${esc(t.server)}</span>` : "") +
    `<span class="tool-call__preview muted">${esc(preview.slice(0, 200))}</span>` +
    `<span class="st-badge ${cls}"${t.state === "skipped" && t.reason ? ` title="It was ${esc(t.reason)}."` : ""}>${esc(label)}</span>` +
    // while it runs: the ticking clock; once it is back: the time the server measured
    (t.state === "running" || t.state === "writing" ? runNote(t)
     : t.ms != null && t.state !== "skipped" ? `<span class="muted small">${fmt(t.ms / 1000, 1)} s</span>` : "") +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary><div class="st-collapse__body">${body}</div></details>`;
}
// Tool calls that follow each other (no text between them) sit in ONE collapsible group, like the tool lines in Claude
// Code: a summary of what was done ("Ran 3 commands, searched 5 times"), what is happening now, and the calls inside.
const SEARCH_TOOL = /^(grep|glob|search|find|web_search)/i;
function groupLabel(items) {
  const n = {cmd: 0, read: 0, search: 0, change: 0, web: 0, ask: 0, other: 0};
  let failed = 0;
  for (const [t] of items) {
    const kind = t.kind, name = t.tool || t.name || "";
    if (kind === "exec") n.cmd++;
    else if (kind === "net") n.web++;
    else if (kind === "ask") n.ask++;
    else if (kind === "write" || kind === "danger") n.change++;
    else if (kind === "read") (SEARCH_TOOL.test(name) ? n.search++ : n.read++);
    else n.other++;
    if (t.state === "error" || t.state === "blocked") failed++;
  }
  const plural = (c, one, many) => `${c} ${c === 1 ? one : many}`;
  const parts = [];
  if (n.cmd) parts.push(`ran ${n.cmd === 1 ? "a command" : n.cmd + " commands"}`);
  if (n.read) parts.push(`read ${plural(n.read, "file or folder", "files and folders")}`);
  if (n.search) parts.push(`searched ${plural(n.search, "time", "times")}`);
  if (n.change) parts.push(`changed ${plural(n.change, "thing", "things")}`);
  if (n.web) parts.push(`${plural(n.web, "web lookup", "web lookups")}`);
  if (n.ask) parts.push("asked you a question");
  if (n.other) parts.push(`used ${plural(n.other, "tool", "tools")}`);
  let text = parts.join(", ") || "used tools";
  text = text.charAt(0).toUpperCase() + text.slice(1);
  return failed ? `${text} (${failed} failed)` : text;
}
function groupHtml(m, items) {
  const first = items[0][1];
  const live = items.find(([t]) => t.state === "writing" || t.state === "running");
  const needsYou = items.find(([t]) => t.state === "waiting" || t.state === "asking");
  // open while a call waits for the user (the buttons are inside), otherwise as the user left it (closed at first)
  const open = (m.gopen && m.gopen[first] != null) ? m.gopen[first] : !!needsYou;
  const state = needsYou ? needsYou[0].state : live ? live[0].state : "done";
  const [cls, badge] = needsYou ? TOOL_STATE[needsYou[0].state] : live ? TOOL_STATE[live[0].state] : ["", ""];
  const doing = needsYou ? needsYou[0].tool || needsYou[0].name : live ? live[0].tool || live[0].name : "";
  const total = items.reduce((s, [t]) => s + (t.ms || 0), 0);
  return `<details class="st-collapse tool-group" data-group="${first}" data-state="${esc(state)}"${open ? " open" : ""}>` +
    `<summary>${icon("tool", "st-icon st-icon--sm")}<span class="tool-group__label">${esc(groupLabel(items))}</span>` +
    (badge ? `<span class="tool-group__now muted small">${esc(doing)}</span><span class="st-badge ${cls}">${esc(badge)}</span>` +
            (needsYou ? "" : runNote(live && live[0])) : "") +
    (!badge && total ? `<span class="muted small">${fmt(total / 1000, 1)} s</span>` : "") +
    `${icon("chevron", "st-icon st-icon--sm st-chev")}</summary><div class="st-collapse__body">` +
    items.map(([t, k]) => toolHtml(t, k)).join("") + `</div></details>`;
}
// the answer's text with the tool groups where the model called them
function answerHtml(m) {
  if (!m.tools || !m.tools.length) return markdown(m.text || "");
  let html = "", pos = 0, group = [];
  const flush = () => { if (group.length) { html += groupHtml(m, group); group = []; } };
  m.tools.forEach((t, k) => {
    const at = Math.min(Math.max(t.at || 0, pos), m.text.length);
    if (at > pos) { flush(); html += markdown(m.text.slice(pos, at)); }
    pos = at;
    group.push([t, k]);
  });
  flush();
  return html + markdown(m.text.slice(pos));
}
// a tool event from the stream (the `strata_mcp` field of a chunk)
function onTool(m, x) {
  if (x.event === "limit") { m.limit = x.max_rounds; return; }
  if (x.event === "compacting") { m.compacting = true; return; }   // the server is summarizing the older chat (up to a minute)
  if (x.event === "compact") { m.compacting = false; m.compacted = (m.compacted || 0) + (x.shrunk || 0); m.summarized = (m.summarized || 0) + (x.summarized || 0); return; }   // older results shortened to fit
  m.tools = m.tools || [];
  let t = m.tools.find((y) => y.id === x.id);
  if (!t) { t = {id: x.id, name: x.name, at: m.text.length, rat: m.reasoning.length, state: "writing"}; m.tools.push(t); }
  if (x.event === "call") {
    // t0 is the clock the running call shows; a call event that arrives twice must not restart it
    Object.assign(t, {name: x.name, server: x.server, tool: x.tool, arguments: x.arguments, round: x.round, state: "running",
                      kind: x.kind, t0: t.t0 || Date.now()});
  } else if (x.event === "question") {
    // ask_user: the model waits for the user's typed or clicked answer (POST /mcp/answer)
    Object.assign(t, {state: "asking", approval: x.approval, question: x.question, options: x.options || [], open: true});
  } else if (x.event === "approval") {
    // the server holds the call until the user answers (Allow / Always allow / Deny, or it times out)
    Object.assign(t, {state: "waiting", approval: x.approval, kind: x.kind || t.kind, protectedPath: !!x.protected, leak: !!x.leak, exec: !!x.exec, reason: x.mode === "auto" ? x.reason : null, open: true});
  } else if (x.event === "result") {
    Object.assign(t, {result: x.text, ok: x.ok, chars: x.chars, truncated: x.truncated, ms: x.ms, approval: null,
                      reason: x.skipped ? (NOT_RUN[x.reason] || NOT_RUN.length) : t.reason,
                      state: x.skipped ? "skipped" : x.denied ? "blocked" : x.ok ? "done" : "error"});
  }
}
// the user's answer to an ask_user question (POST /mcp/answer)
async function answerQuestion(m, t, text, el) {
  const token = t.approval;
  if (!token) return;
  t.approval = null;
  t.state = "running";
  t.t0 = Date.now();                                        // the clock starts when it runs, not when it was announced
  updateAssistant(el, m, !!busy && busy.msg === m);
  try {
    await fetch("mcp/answer", {method: "POST", headers: headers(true), body: JSON.stringify({approval: token, text})});
  } catch (e) { /* the result event reports what happened */ }
}
// Allow / Always allow / Deny on a waiting tool call: the server is holding it (POST /mcp/approve)
async function answerApproval(m, t, how, el) {
  const token = t.approval;
  if (!token) return;
  t.approval = null;
  t.state = "running";
  t.t0 = Date.now();                                        // the clock starts when it runs, not when it was announced
  updateAssistant(el, m, !!busy && busy.msg === m);
  try {
    await fetch("mcp/approve", {method: "POST", headers: headers(true),
                                body: JSON.stringify({approval: token, allow: how !== "deny", always: how === "always"})});
  } catch (e) { /* the result event reports what happened */ }
}
// A long command is the other place the page can look broken: the stream sends nothing at all while the server waits
// for it, so a call that is running has to say how long it has taken, and against which limit.  Without that number a
// 10-second git command and a hung one look identical - a blinking badge and a blank pause.
function runningTool(m) {
  return ((m && m.tools) || []).find((t) => t.state === "running" || t.state === "writing") || null;
}
function runNote(t) {
  if (!t || !t.t0) return "";
  const s = Math.floor((Date.now() - t.t0) / 1000);
  if (s < 1) return "";
  const lim = Number(t.arguments && t.arguments.timeout_s) > 0 ? Number(t.arguments.timeout_s) : null;
  // the limit is what separates "slow" from "stuck": past it the call is past the time it was given
  return `<span class="muted small run-clock${lim != null && s > lim ? " is-over" : ""}` +
         ` title="${lim != null ? `it was given ${lim} s before it is cut off` : "no time limit was given"}">` +
         `${s} s${lim != null ? ` of ${lim} s` : ""}</span>`;
}

// the wait before the first token is the part that feels broken: say what is happening and for how long
let waiting = null;
function waitNote() {
  if (!waiting) return "";
  const s = Math.floor((performance.now() - waiting.start) / 1000);
  if (s < 2) return "";
  const why = waiting.prompting ? " · reading your message into the context"
    : s >= 25 ? " · the model may still be loading" : "";
  return `<span class="muted wait-note">Waiting ${s} s${why}</span>`;
}

function updateAssistant(el, m, streaming) {
  const det = el.querySelector("details.think");
  if (m.reasoning) {
    det.hidden = false;
    const thinkingNow = streaming && !m.text;
    el.querySelector(".think-title").textContent = thinkingNow ? "Thinking…" :
      m.thinkSecs != null ? `Thought for ${fmt(m.thinkSecs, 1)} s` : "Thoughts";
    const body = el.querySelector(".thinking");
    if (det.open || thinkingNow) body.textContent = m.reasoning;
    else body.dataset.pending = "1";
    // open while it streams (if wanted), closed once the answer starts - unless the user toggled it themselves
    if (thinkingNow && settings.show && !det.dataset.touched && !det.open) { det._auto = true; det.open = true; }
    if (!thinkingNow && det.open && !det.dataset.touched) { det._auto = true; det.open = false; }
  }
  const bubble = el.querySelector(".st-bubble");
  if (m.error) {
    bubble.innerHTML = `<div class="msg-error"></div><button type="button" class="st-btn st-btn--secondary" data-retry>` +
      `Edit and resend</button>`;
    bubble.firstChild.textContent = m.error;
  } else if (!m.text && streaming && !(m.tools && m.tools.length)) {
    const note = waitNote();
    bubble.innerHTML = m.compacting ? `<span class="muted cursor">Summarizing the earlier conversation to fit the context (up to a minute)</span>${note}`
      : m.reasoning ? `<span class="muted cursor">Writing</span>` : `<span class="cursor"></span>${note}`;
  } else {
    bubble.innerHTML = answerHtml(m);
    if (streaming) bubble.classList.add("cursor"); else bubble.classList.remove("cursor");
  }
  el.querySelector(".meta-text").textContent = m.meta || (streaming ? "" : m.stopped ? "Stopped" : "");
  el.querySelector("[data-msg-copy]").hidden = streaming || !m.text;
  const cont = el.querySelector("[data-continue]");
  if (cont) cont.hidden = streaming || !m.text;      // a stopped or cut-off answer can be carried on
}
function renderChat() {
  const chat = $("chat");
  chat.querySelectorAll(".st-msg").forEach((e) => e.remove());
  $("chat-empty").hidden = messages.length > 0;
  messages.forEach((m, i) => chat.appendChild(msgEl(m, i)));
  scrollDown(true);
  updateGalleryCount();
  if (panelState.open && panelState.tab === "gallery") renderGallery();
}
// The chat follows the newest text: it stays pinned to the bottom, however much a frame adds (a tool block, a long
// answer), until the user scrolls up to read; scrolling back to the bottom (or sending a message) pins it again.
let pinned = true, ownScroll = false;
function nearBottom(px = 120) { const s = $("chat-scroll"); return s.scrollHeight - s.scrollTop - s.clientHeight < px; }
function toBottom() {
  const s = $("chat-scroll"), end = s.scrollHeight - s.clientHeight;
  if (Math.abs(s.scrollTop - end) > 1) { ownScroll = true; s.scrollTop = end; }   // our own scroll is not the user's
}
function scrollDown(force) {
  if (force) pinned = true;
  if (pinned) toBottom();
  showJump();
}
// an answer that outgrows the window while you are reading higher up needs a way back down that does not need the mouse
function showJump() {
  const b = $("jump-latest");
  if (b) b.hidden = pinned || !messages.length;
}
$("jump-latest").onclick = () => { pinned = true; toBottom(); showJump(); $("chat-scroll").focus?.(); };
$("chat-scroll").addEventListener("scroll", () => {
  if (ownScroll) { ownScroll = false; return; }
  pinned = nearBottom(40);                       // the user scrolled: up unpins, back to the bottom pins again
  showJump();
}, {passive: true});
$("chat-scroll").addEventListener("wheel", (e) => { if (e.deltaY < 0) pinned = false; }, {passive: true});
// growth from anything (images, a block opening, the markdown settling) keeps a pinned chat at the bottom
if (window.ResizeObserver) new ResizeObserver(() => { if (pinned) toBottom(); }).observe($("chat"));

$("chat").addEventListener("click", (e) => {
  const cc = e.target.closest("[data-code-copy]");
  if (cc) { copyText(cc.closest(".st-code").querySelector("pre").textContent, cc); return; }
  const cn = e.target.closest("[data-continue]");
  if (cn) { if (!(busy && writingHere())) send({text: "Continue"}); return; }   // another chat's answer: the notice offers to stop it
  const rt = e.target.closest("[data-retry]");
  if (rt) { retryFrom(+rt.closest(".st-msg").dataset.i); return; }
  const mc = e.target.closest("[data-msg-copy]");
  if (mc) { const i = +mc.closest(".st-msg").dataset.i; copyText(messages[i].text, mc); return; }
  const qo = e.target.closest("[data-answer-opt], [data-answer-send]");
  if (qo) {
    e.preventDefault();
    const el = qo.closest(".st-msg"), m = messages[+el.dataset.i], block = qo.closest(".tool-call");
    const t = m && m.tools && m.tools[+block.dataset.tool];
    if (t) {
      const text = qo.dataset.answerOpt !== undefined ? (t.options || [])[+qo.dataset.answerOpt]
                                                        : block.querySelector(".tool-call__input").value.trim();
      if (text) answerQuestion(m, t, text, el);
    }
    return;
  }
  const ap = e.target.closest("[data-approve]");
  if (ap) {
    e.preventDefault();
    const el = ap.closest(".st-msg"), m = messages[+el.dataset.i], t = m && m.tools && m.tools[+ap.closest(".tool-call").dataset.tool];
    if (t) answerApproval(m, t, ap.dataset.approve, el);
    return;
  }
  // a tool block: its open state lives in the message (the answer is rebuilt while it streams), so the click sets it
  const gsum = e.target.closest(".tool-group > summary");
  if (gsum) {                                    // a group of tool calls: its open state lives in the message too
    e.preventDefault();
    const el = gsum.closest(".st-msg"), m = messages[+el.dataset.i], g = gsum.parentElement;
    if (!m) return;
    m.gopen = m.gopen || {};
    m.gopen[+g.dataset.group] = !g.open;
    updateAssistant(el, m, !!busy && busy.msg === m);
    return;
  }
  const sum = e.target.closest(".tool-call > summary");
  if (sum) {
    e.preventDefault();
    const el = sum.closest(".st-msg"), m = messages[+el.dataset.i], t = m && m.tools && m.tools[+sum.parentElement.dataset.tool];
    if (!t) return;
    t.open = !t.open;
    updateAssistant(el, m, !!busy && busy.msg === m);
  }
});
$("chat").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target.classList || !e.target.classList.contains("tool-call__input")) return;
  e.preventDefault();
  const send = e.target.closest(".tool-call__reply").querySelector("[data-answer-send]");
  if (send) send.click();
});
$("chat").addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.tagName !== "DETAILS" || !d.classList.contains("think")) return;
  if (d._auto) { d._auto = false; return; }          // our own open/close, not the user's
  d.dataset.touched = "1";
  const body = d.querySelector(".thinking");
  if (d.open && body.dataset.pending) { body.textContent = messages[+d.closest(".st-msg").dataset.i].reasoning; delete body.dataset.pending; }
}, true);

function apiMessages() {
  const out = [];
  for (const m of messages) {
    if (m.role === "user") {
      const imgs = (m.images || []).filter((i) => i.url);
      const text = userText(m);
      out.push({role: "user", content: imgs.length ? [{type: "text", text},
        ...imgs.map((i) => ({type: "image_url", image_url: {url: i.url}}))] : text});
    } else if (!(busy && busy.msg === m)) {            // the answer being asked for now is not history yet
      out.push(...assistantMessages(m));
    }
  }
  return out;
}
// An answer that used MCP tools goes back as the model wrote it: per round the text before the calls, the calls and
// their results (as the model read them), then the rest - so the next question can build on what the tools found.
function assistantMessages(m) {
  const ran = (m.tools || []).filter((t) => t.round != null && t.result != null && t.state !== "skipped");
  // #1392: a turn with no answer text (only reasoning, a stop before the first content token, or an error) still goes
  // back as an assistant turn, so the history keeps alternating and a reasoning model sees that it already answered.
  // The server leaves such a turn out of the prompt itself (serve/frontend.py, #843); reasoning_content rides along.
  if (!ran.length) {
    const msg = {role: "assistant", content: m.text || ""};
    if (!m.text && m.reasoning) msg.reasoning_content = m.reasoning;
    return [msg];
  }
  const out = [];
  let pos = 0;
  for (const r of [...new Set(ran.map((t) => t.round))]) {
    const calls = ran.filter((t) => t.round === r);
    const at = Math.min(Math.max(pos, calls[0].at || 0), m.text.length);
    out.push({role: "assistant", content: m.text.slice(pos, at).trim(),
              tool_calls: calls.map((t) => ({id: t.id, type: "function", function: {name: t.name, arguments: JSON.stringify(t.arguments || {})}}))});
    for (const t of calls) out.push({role: "tool", tool_call_id: t.id, content: t.result});
    pos = at;
  }
  const rest = m.text.slice(pos).trim();
  if (rest) out.push({role: "assistant", content: rest});
  return out;
}

function setBusy(on) {
  $("stop-btn").hidden = !on;
  $("composer").classList.toggle("is-busy", on);
  // say what Enter does right now, and which chat is holding the line: it queues when the answer being written is
  // this chat's own, and it waits when the answer in flight belongs to another chat (the red button and Esc stop
  // that one, so the name has to be on show, not left to be guessed)
  const other = on && !writingHere() ? writingTitle() : "";
  $("stop-btn").title = on ? (other ? `Stop the answer writing in “${other}”` : "Stop this answer (Esc)") : "";
  $("input").placeholder = on ? (other ? `“${other}” is still writing\u2026` : "Type here and Enter queues it for the next turn\u2026") : "Ask anything\u2026";
  $("composer-hint").textContent = on ? (other ? "Enter: waits for the other chat" : "Enter: queue") : "Shift+Enter: new line";
}

// A message typed while the model is still answering waits here and goes out when the answer ends (or at once with
// "Send now", which stops the answer where it is).  One message waits; a second one is added to it.  It waits in
// the chat it was typed in: another chat's answer must not pick it up, and it is not sent while you are reading a
// different chat - it goes out when you come back to that chat and nothing is writing there.
let queued = null;
function renderQueued() {
  const mine = queued && queued.id === current.id;
  $("queued").hidden = !mine;
  if (mine) $("queued-text").textContent = queued.text || `${queued.images.length + queued.files.length} attachment(s)`;
}
function queueMessage(text) {
  const add = {id: current.id, text, images: attachments.filter((a) => a.kind !== "file"), files: attachments.filter((a) => a.kind === "file")};
  queued = queued ? {id: queued.id, text: [queued.text, text].filter(Boolean).join(" "), images: [...queued.images, ...add.images],
                     files: [...queued.files, ...add.files]} : add;
  attachments = [];
  renderAttachments();
  $("input").value = "";
  autosize();
  renderQueued();
}
function sendQueued() {
  if (!queued || busy || queued.id !== current.id) return;
  const q = queued;
  queued = null;
  renderQueued();
  send(q);
}

// a held message waits even across a change of chat: it is sent in the chat it was held in, once nothing writes
// there (coming back to that chat is what releases it)
function maybeSendQueued() {
  if (queued && !busy && queued.id === current.id) setTimeout(sendQueued, 0);
}

async function send(ready) {
  const typed = $("input").value.trim();
  // one answer at a time, but it belongs to the chat that asked for it: a question here while another chat is
  // writing is not refused, it is held - and the notice names that chat, with one button that stops it and asks
  if (busy && !writingHere()) {
    if (!ready && !typed && !attachments.length) return;
    stillWriting(() => send(ready));
    return;
  }
  if (!ready && (typed || attachments.length) && busy) { queueMessage(typed); return; }
  if ((!ready && !typed && !attachments.length) || busy) return;
  const text = ready ? ready.text : typed;
  const list = messages;                                       // this chat's own list: the answer goes into it, not
  list.push({role: "user", text, images: ready ? ready.images : attachments.filter((a) => a.kind !== "file"),     // into whatever
                 files: ready ? ready.files : attachments.filter((a) => a.kind === "file"), time: Date.now()});   // is on screen
  if (!ready) {
    attachments = [];
    renderAttachments();
    $("input").value = "";
    autosize();
  }
  const m = {role: "assistant", text: "", reasoning: "", time: Date.now()};
  list.push(m);
  renderChat();
  const el = $("chat").lastElementChild;
  const controller = new AbortController();
  busy = {id: current.id, controller, msg: m, list, el};
  setBusy(true);
  m.partial = true;                                   // in progress: saved every few seconds, so a stop mid-way loses little
  const autosave = setInterval(() => { if (sessionsOn && !savesRunning) saveWriting(); }, 4000);
  saveChat();                                         // the question is kept at once

  const body = {model: health.model, messages: apiMessages(), stream: true,
                reasoning_effort: settings.thinking};
  if (settings.temperature > 0) {
    Object.assign(body, {temperature: +settings.temperature, top_p: +settings.top_p, top_k: +settings.top_k});
  } else {
    body.temperature = 0;
  }
  if (settings.seed) body.seed = +settings.seed;
  if (settings.max) body.max_tokens = +settings.max;
  if (projectionLoaded()) body.experimental_speed_projection = !!settings.esp;
  if (settings.mcp !== false && mcpInfo.tools > 0 && permMode() !== "off") {
    body.strata_mcp = true;                           // this server may run MCP tools for it ...
    body.strata_permission = permMode();              // ... as far as this mode allows (the server enforces it)
  }

  let firstAt = null, thinkStart = null, usage = null, frame = 0;
  const startAt = performance.now();
  const markFirst = () => { if (!firstAt) { firstAt = performance.now(); waiting = null; } };
  waiting = {start: startAt, prompting: false};
  // the waiting clock: the seconds before the first token, and the seconds a running command has been taking
  const tick = setInterval(() => { if (waiting || runningTool(m)) paintStream(true); }, 1000);
  const paint = () => { frame = 0; paintStream(true); };
  try {
    const r = await fetch("v1/chat/completions", {method: "POST", headers: headers(true), body: JSON.stringify(body),
                                                   signal: controller.signal});
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
      if (r.status === 401) msg = "This server needs an API key: add it under About > Settings.";
      throw new Error(msg);
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) {                       // ": keep-alive" comments while a long prompt is read
          if (waiting && line.startsWith(":")) waiting.prompting = true;   // the engine is reading the prompt: say so
          continue;
        }
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let j;
        try { j = JSON.parse(data); } catch (e) { continue; }
        if (j.error) throw new Error(j.error.message || "the engine reported an error");
        if (j.usage) usage = j.usage;
        if (j.strata_mcp) onTool(m, j.strata_mcp);
        const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
        const lastTool = m.tools && m.tools.length ? m.tools[m.tools.length - 1] : null;   // a new round after a tool
        if (d.reasoning_content) {
          markFirst();
          if (!thinkStart) thinkStart = performance.now();
          if (lastTool && m.reasoning && lastTool.rat === m.reasoning.length) m.reasoning += "\n\n";
          m.reasoning += d.reasoning_content;
        }
        if (d.content) {
          markFirst();
          if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
          if (lastTool && m.text && lastTool.at === m.text.length) m.text += "\n\n";
          m.text += d.content;
        }
        if (!frame) frame = requestAnimationFrame(paint);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") m.stopped = true;
    else { m.error = e.message || String(e); toast("error", "The request failed", m.error, 6000); }
  }
  if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
  const n = usage ? usage.completion_tokens : null;
  if (n && firstAt) {
    const secs = (performance.now() - firstAt) / 1000;
    m.meta = `${fmt(n)} tokens${secs > 0.25 ? ` · ${fmt(n / secs, 1)} tok/s` : ""}${m.stopped ? " · stopped" : ""}` +
             (projectionLoaded() ? (settings.esp ? " · projection on" : " · projection off") : "");
  } else if (m.stopped) {
    m.meta = "Stopped";
  }
  // calls that never got an answer at all (the request ended first): the page knows why, the server had no chance to say
  for (const t of m.tools || []) if (t.state === "writing" || t.state === "running") {
    t.state = "skipped"; t.ms = null;
    t.reason = t.reason || (m.stopped ? NOT_RUN.stop : m.limit ? NOT_RUN.limit : NOT_RUN.finished);
  }
  const ran = (m.tools || []).filter((t) => t.state === "done" || t.state === "error").length;
  if (ran) m.meta = `${m.meta ? `${m.meta} · ` : ""}${ran} tool call${ran > 1 ? "s" : ""}`;
  if (m.summarized) m.meta = `${m.meta || ""} · summarized ${m.summarized} earlier messages to stay inside the context`;
  // calls that were announced and never ran: say how many and why, and that "continue" picks them up
  const notRun = (m.tools || []).filter((t) => t.state === "skipped" && t.reason);
  if (notRun.length) {
    const why = notRun.map((t) => t.reason).find((r) => r !== NOT_RUN.stop) || NOT_RUN.stop;
    m.meta = `${m.meta || ""} · ${notRun.length} queued tool call${notRun.length > 1 ? "s" : ""} did not run (${why}); say continue to pick them up`;
  } else if (m.limit) m.meta = `${m.meta || ""} · stopped at the limit of ${m.limit} tool rounds (mcp.max_rounds); say continue to keep going`;
  clearInterval(tick);
  waiting = null;
  const ttft = firstAt ? (firstAt - startAt) / 1000 : null;      // a long wait is worth remembering, a short one is noise
  if (ttft > 5) m.meta = `first token in ${fmt(ttft, 1)} s${m.meta ? ` · ${m.meta}` : ""}`;
  clearInterval(autosave);
  delete m.partial;
  const here = writingHere();
  if (here) { updateAssistant(el, m, false); scrollDown(); }   // a background answer lands without touching the screen
  if (here) saveChat(); else saveWriting();                    // its own chat, its own title, from the stream's list
  busy = null;
  setBusy(false);
  if (frame) cancelAnimationFrame(frame);
  if (queued && here) setTimeout(sendQueued, 0);              // the message typed meanwhile goes out now
}

$("composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("stop-btn").onclick = () => { if (busy) busy.controller.abort(); };
$("queued-now").onclick = () => { if (busy) busy.controller.abort(); else sendQueued(); };   // the end of send() sends it
$("queued-cancel").onclick = () => { queued = null; renderQueued(); };
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing && (e.ctrlKey || e.metaKey || !e.shiftKey)) { e.preventDefault(); send(); }
  // Escape is the fastest way to say "that is going wrong", and it keeps everything written so far
  if (e.key === "Escape" && busy) { e.preventDefault(); busy.controller.abort(); }
});
// a failed request costs you nothing but the click: the question, its pictures and its files come back editable
function retryFrom(i) {
  const um = messages[i - 1];
  if (!um || um.role !== "user") return;
  messages.splice(i - 1, 2);
  attachments = [...(um.images || []), ...(um.files || [])];
  renderAttachments();
  $("input").value = um.text; autosize(); $("input").focus();
  renderChat();
  saveChat();
}

function autosize() { const t = $("input"); t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, innerHeight * 0.4)}px`; }
$("input").addEventListener("input", autosize);

$("chat-empty-asks").addEventListener("click", (e) => {
  const a = e.target.closest("[data-ask]");
  if (!a) return;
  $("input").value = a.dataset.ask; autosize(); $("input").focus();
});

$("new-btn").onclick = () => {
  const clear = () => {
    if (!messages.length) return;
    const backup = messages;
    messages = [];
    saveChat();
    renderChat();
    toast("info", "New chat", "The last one was cleared.", 6000, {label: "Undo", run: () => { messages = backup; saveChat(); renderChat(); }});
  };
  if (sessionsOn) { newSession(); return; }                 // the old chat stays in the list on the left
  if (stillWriting(clear)) return;                          // no sessions to fall back on: the guard speaks for itself
};
$("export-btn").onclick = () => {
  if (!messages.length) { toast("info", "Nothing to save yet"); return; }
  const tools = (m) => (m.tools || []).filter((t) => t.result != null).map((t) =>
    `<details><summary>Tool ${t.server ? `${t.server} / ` : ""}${t.tool || t.name}${t.ok ? "" : " (error)"}</summary>\n\n` +
    `\`\`\`json\n${JSON.stringify(t.arguments || {}, null, 2)}\n\`\`\`\n\n\`\`\`\n${t.result}\n\`\`\`\n\n</details>\n\n`).join("");
  const md = messages.map((m) => m.role === "user" ? `## You\n\n${m.text}\n` :
    `## ${health.model}\n\n${m.reasoning ? `<details><summary>Thinking</summary>\n\n${m.reasoning}\n\n</details>\n\n` : ""}${tools(m)}${m.text || m.error || ""}\n`).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], {type: "text/markdown"}));
  a.download = `strata-chat-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
};

// pictures and text files: the attach button, dropping them on the chat, or pasting a picture (issue #30)
const TEXT_EXT = /\.(txt|md|markdown|rst|tex|py|pyi|ipynb|js|mjs|cjs|ts|tsx|jsx|vue|svelte|json|jsonl|csv|tsv|log|ya?ml|toml|ini|cfg|conf|env|xml|html?|css|scss|less|c|cc|cpp|cxx|h|hh|hpp|cu|cuh|rs|go|java|kt|kts|swift|rb|php|pl|lua|r|jl|scala|sql|sh|bash|zsh|fish|ps1|psm1|bat|cmd|diff|patch|gradle|cmake|mk|dockerfile|gitignore|proto|graphql)$/i;
const MAX_TEXT_FILE = 512 * 1024;
function isTextFile(f) {
  return f.type.startsWith("text/") || /json|xml|javascript|yaml|toml|x-sh|x-python/.test(f.type) ||
         TEXT_EXT.test(f.name) || /(^|[\\/])(makefile|dockerfile|readme|license)$/i.test(f.name);
}
function addFiles(files) {
  for (const f of files) {
    if (f.type.startsWith("image/")) {
      if (!health.images) { toast("warn", "Pictures are off", "This model was set up for text only."); continue; }
      if (f.size > 20e6) { toast("warn", "Picture too large", `${f.name} is over 20 MB.`); continue; }
      const r = new FileReader();
      r.onload = () => { attachments.push({kind: "image", name: f.name || "pasted image", url: r.result}); renderAttachments(); };
      r.readAsDataURL(f);
      continue;
    }
    if (!isTextFile(f)) { toast("warn", "Not a text file", `${f.name}: attach text files (code, notes, logs, data)${health.images ? " or pictures" : ""}.`); continue; }
    if (f.size > MAX_TEXT_FILE) { toast("warn", "File too large", `${f.name} is over 512 KB.`); continue; }
    const r = new FileReader();
    r.onload = () => {
      const text = String(r.result);
      if (text.includes("\u0000")) { toast("warn", "Not a text file", `${f.name} looks like a binary file.`); return; }
      attachments.push({kind: "file", name: f.name, text});
      renderAttachments();
    };
    r.readAsText(f);
  }
}
// a file's text in the message, fenced with more backticks than it contains itself
function fileBlock(f) {
  const longest = Math.max(2, ...(f.text.match(/`+/g) || []).map((s) => s.length));
  const fence = "`".repeat(longest + 1);
  return `File: ${f.name}\n${fence}\n${f.text}\n${fence}`;
}
function userText(m) {
  const files = (m.files || []).filter((f) => f.text != null);
  return [m.text, ...files.map(fileBlock)].filter((s) => s).join("\n\n");
}
function renderAttachments() {
  const box = $("attachments");
  box.hidden = !attachments.length;
  box.innerHTML = "";
  attachments.forEach((a, i) => {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = icon(a.kind === "file" ? "attach" : "image", "st-icon st-icon--sm");
    c.append(a.name);
    const x = document.createElement("button");
    x.type = "button"; x.className = "st-btn st-btn--icon"; x.setAttribute("aria-label", "Remove");
    x.innerHTML = icon("trash");
    x.onclick = () => { attachments.splice(i, 1); renderAttachments(); };
    c.appendChild(x);
    box.appendChild(c);
  });
}
$("attach-btn").onclick = () => $("file").click();
// drop files on the chat or the message box
for (const id of ["chat", "composer"]) {
  const el = $(id);
  el.addEventListener("dragover", (e) => {
    if (![...(e.dataTransfer || {}).types || []].includes("Files")) return;
    e.preventDefault();
    $("composer").classList.add("dragging");
  });
  el.addEventListener("dragleave", () => $("composer").classList.remove("dragging"));
  el.addEventListener("drop", (e) => {
    $("composer").classList.remove("dragging");
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    addFiles(e.dataTransfer.files);
    $("input").focus();
  });
}
$("file").onchange = () => { addFiles($("file").files); $("file").value = ""; };
$("input").addEventListener("paste", (e) => {
  if (!health.images) return;
  const files = [...(e.clipboardData || {}).files || []].filter((f) => f.type.startsWith("image/"));
  if (files.length) { e.preventDefault(); addFiles(files); }
});

// ------------------------------------------------------------------ the sampling drawer
let drawerReturn = null;                                     // where the focus was before the drawer opened
function openDrawer(open) {
  $("drawer").dataset.open = String(open);
  $("drawer").setAttribute("aria-hidden", String(!open));
  $("scrim").hidden = !open;
  if (open) {
    drawerReturn = document.activeElement;
    loadDrawer(); loadShared(); loadMcp();
    requestAnimationFrame(() => $("drawer-close").focus());  // a keyboard or screen-reader user lands inside what they opened
  } else if (drawerReturn && drawerReturn.isConnected) {
    drawerReturn.focus(); drawerReturn = null;               // and goes back where they came from
  }
}
// Tab stays inside the open drawer: it is a window of its own while it is up
function trapTab(e, box) {
  const f = [...box.querySelectorAll("button,[href],input,select,textarea,[tabindex]:not([tabindex='-1'])")]
    .filter((el) => !el.disabled && !el.hidden && el.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}
function loadDrawer(s = settings) {
  for (const b of $("s-thinking").children) b.setAttribute("aria-checked", String(b.dataset.v === s.thinking));
  $("s-temp").value = s.temperature; $("s-topp").value = s.top_p; $("s-topk").value = s.top_k;
  $("s-max").value = s.max; $("s-seed").value = s.seed;
  $("s-show").setAttribute("aria-checked", String(!!s.show));
  $("s-esp").setAttribute("aria-checked", String(s.esp !== false));
  $("esp-row").hidden = !projectionLoaded();
  $("s-mcp").setAttribute("aria-checked", String(s.mcp !== false));
  $("s-share").setAttribute("aria-checked", String(sharedOn));
  outputs();
}
// "Use for other apps too": the server keeps these settings as every client's defaults (GET/POST /settings)
let sharedOn = false;
async function loadShared() {
  try {
    const r = await fetch("settings", {headers: headers()});
    if (r.ok) sharedOn = !!(await r.json()).shared;
  } catch (e) { /* an older server: the switch just stays off */ }
  $("s-share").setAttribute("aria-checked", String(sharedOn));
}
function sharedDefaults(s) {
  const d = {reasoning_effort: s.thinking, temperature: +s.temperature};
  if (+s.temperature > 0) Object.assign(d, {top_p: +s.top_p, top_k: +s.top_k});
  if (s.seed) d.seed = +s.seed;
  if (s.max) d.max_tokens = +s.max;
  if (projectionLoaded()) d.experimental_speed_projection = s.esp !== false;
  return d;
}
async function saveShared(on, s) {
  const r = await fetch("settings", {method: "POST", headers: headers(true),
                                      body: JSON.stringify({defaults: on ? sharedDefaults(s) : null})});
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).error.message || msg; } catch (e) { /* not json */ }
    throw new Error(msg);
  }
  sharedOn = !!(await r.json()).shared;
}
// the engine was started with the experimental-speed-projection control vector (INFO cvec=...)
function projectionLoaded() {
  const c = lastMetrics && lastMetrics.engine ? lastMetrics.engine.cvec : 0;
  return !!c && c !== "0";
}
function outputs() {
  const t = +$("s-temp").value;
  $("o-temp").textContent = t === 0 ? "0 · greedy" : t.toFixed(2);
  $("o-topp").textContent = (+$("s-topp").value).toFixed(2);
  $("o-topk").textContent = $("s-topk").value;
  const sel = [...$("s-thinking").children].find((b) => b.getAttribute("aria-checked") === "true");
  $("o-thinking").textContent = sel ? {none: "answers right away", low: "short", medium: "medium", high: "thorough (default)"}[sel.dataset.v] : "";
  for (const id of ["s-topp", "s-topk"]) $(id).disabled = t === 0;
}
for (const b of $("s-thinking").children) b.onclick = () => { for (const x of $("s-thinking").children) x.setAttribute("aria-checked", String(x === b)); outputs(); };
for (const id of ["s-temp", "s-topp", "s-topk"]) $(id).oninput = outputs;
$("s-show").onclick = () => $("s-show").setAttribute("aria-checked", String($("s-show").getAttribute("aria-checked") !== "true"));
$("s-esp").onclick = () => $("s-esp").setAttribute("aria-checked", String($("s-esp").getAttribute("aria-checked") !== "true"));
$("s-mcp").onclick = () => $("s-mcp").setAttribute("aria-checked", String($("s-mcp").getAttribute("aria-checked") !== "true"));
$("s-share").onclick = () => $("s-share").setAttribute("aria-checked", String($("s-share").getAttribute("aria-checked") !== "true"));
$("s-reset").onclick = () => loadDrawer(DEFAULTS);
$("s-apply").onclick = async () => {
  const sel = [...$("s-thinking").children].find((b) => b.getAttribute("aria-checked") === "true");
  settings = {thinking: sel ? sel.dataset.v : "high", temperature: +$("s-temp").value, top_p: +$("s-topp").value,
              top_k: +$("s-topk").value, max: $("s-max").value.trim(), seed: $("s-seed").value.trim(),
              show: $("s-show").getAttribute("aria-checked") === "true",
              esp: $("s-esp").getAttribute("aria-checked") === "true",
              mcp: $("s-mcp").getAttribute("aria-checked") === "true"};
  store.set("sampling", settings);
  renderPerm();                                       // "Use tools" off hides the permission drop-down
  const share = $("s-share").getAttribute("aria-checked") === "true";
  openDrawer(false);
  if (share || sharedOn) {
    try {
      await saveShared(share, settings);
      toast("success", "Sampling saved", share ? "Other apps (omp, API clients) use these settings from their next request."
                                               : "Other apps use their own settings again.");
    } catch (e) {
      toast("error", "Saved here, but not for other apps", e.message, 6000);
    }
    return;
  }
  toast("success", "Sampling saved", settings.temperature === 0 ? "Greedy: the same question gives the same answer." : "");
};
$("sampling-btn").onclick = () => openDrawer(true);
$("drawer-close").onclick = () => openDrawer(false);
$("scrim").onclick = () => openDrawer(false);
document.addEventListener("keydown", (e) => {
  if ($("drawer").dataset.open !== "true") return;
  if (e.key === "Escape") { openDrawer(false); return; }      // close first, then hand the focus back
  if (e.key === "Tab") trapTab(e, $("drawer"));
});

// ------------------------------------------------------------------ start
setBusy(false);
renderChat();
const startQuestion = new URLSearchParams(location.search).get("q");   // /?q=... starts a chat (a shortcut)
if (startQuestion) history.replaceState(null, "", location.pathname + location.hash);
const sessionsReady = initSessions();
loadHealth().then(loadMcp).then(() => sessionsReady).then(() => { if (startQuestion) { $("input").value = startQuestion; send(); } });
showTab(location.hash.slice(1) || "chat");
poll();

// ------------------------------------------------------------------ keyboard shortcuts
// the shortcut sheet: a window like the drawer - focus goes in, comes back out
let helpReturn = null;
function openHelp(open) {
  const d = $("help-dialog");
  if (d.hidden === !open) return;
  if (open) {
    helpReturn = document.activeElement;
    d.hidden = false;
    requestAnimationFrame(() => $("help-close").focus());
  } else {
    d.hidden = true;
    if (helpReturn && helpReturn.isConnected) helpReturn.focus();
    helpReturn = null;
  }
}
$("help-btn").onclick = () => openHelp(true);
$("help-close").onclick = () => openHelp(false);
$("help-dialog").addEventListener("mousedown", (e) => { if (e.target === $("help-dialog")) openHelp(false); });

const isTyping = () => {
  const a = document.activeElement || {};
  return /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName || "") || !!a.isContentEditable;
};

document.addEventListener("keydown", (e) => {
  if ($("help-dialog").hidden === false) {
    if (e.key === "Escape") { e.preventDefault(); openHelp(false); return; }
    if (e.key === "Tab") trapTab(e, $("help-dialog"));
    return;
  }
  if ($("drawer").dataset.open === "true") return;   // the drawer is the window on top: its own keys (Esc, Tab) own the keyboard
  const k = e.key || "";
  if ((e.ctrlKey || e.metaKey) && !e.altKey && k.toLowerCase() === "k") {      // straight to the message box
    e.preventDefault(); showTab("chat");
    const i = $("input"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); return;
  }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && k.toLowerCase() === "d") { e.preventDefault(); flipTheme(); return; }
  // Alt+digit is a legacy menu mnemonic on Windows and needs a different key on some layouts, so the
  // plain Alt pair is kept as a courtesy but Ctrl+Shift is what the sheet promises. Read the physical
  // key: holding Shift turns e.key into '@' on a US board, and the digit sits elsewhere on others.
  const digit = (((e.code || "").match(/^(?:Digit|Numpad)([123])$/) || [])[1] || "");
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && digit) { e.preventDefault(); showTab(["chat", "monitor", "about"][+digit - 1]); return; }
  if (e.altKey && digit && !e.ctrlKey && !e.metaKey && !e.shiftKey) { e.preventDefault(); showTab(["chat", "monitor", "about"][+digit - 1]); return; }
  if (isTyping() || e.ctrlKey || e.metaKey || e.altKey) return;
  if (k === "?") { e.preventDefault(); openHelp(true); }
});

// the view tabs behave like a tab strip: arrows move between them
const tabBtns = [...document.querySelectorAll(".st-tab")];
document.querySelector(".st-tabs").addEventListener("keydown", (e) => {
  const at = tabBtns.findIndex((t) => t === document.activeElement);
  if (at < 0 || ["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(e.key) < 0) return;
  e.preventDefault();
  let to = at;
  if (e.key === "ArrowLeft") to = (at - 1 + tabBtns.length) % tabBtns.length;
  if (e.key === "ArrowRight") to = (at + 1) % tabBtns.length;
  if (e.key === "Home") to = 0;
  if (e.key === "End") to = tabBtns.length - 1;
  showTab(tabBtns[to].dataset.tab);
  const again = document.getElementById("tab-btn-" + tab);                    // showTab moves to the message box; a tab user stays on the tabs
  if (again) again.focus();
});
