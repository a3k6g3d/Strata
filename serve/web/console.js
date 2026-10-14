"use strict";
// The console the server's own window shows, in a browser tab.  GET /api/log hands out the lines the server printed
// (serve/server.py: ConsoleTee keeps the last 500 of them), and this page asks for the ones it has not seen yet.
// Nothing here shows a line twice: the server numbers every line, and `after` is the highest number we have seen.
const $ = id => document.getElementById(id);
const MAX_LINES = 500;                       // the same cap the server keeps; older lines are gone, not hidden
let apiKey = sessionStorage.getItem("strata.console.key") || "";
let after = 0, lines = [], shown = -1, follow = true, paused = false, dead = "", started = 0;
document.documentElement.dataset.theme = localStorage.getItem("strata.theme") || "dark";

async function api(path) {
  const response = await fetch(path, {cache: "no-store", headers: {...(apiKey ? {Authorization: `Bearer ${apiKey}`} : {})}});
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) $("auth").hidden = false;
    throw new Error(value.error?.message || `HTTP ${response.status}`);
  }
  return value;
}

// A line is plain text, never markup: the server prints model names, commands and file paths, and an angle bracket in
// one of those must not eat the rest of the page.  Only the wording picks a colour, so a path stays a path.
function row(text) {
  const el = document.createElement("div");
  el.className = "line" + (/\b(WARNING|ERROR|failed|did not|not applied|no longer)\b/i.test(text) ? " bad"
                           : /\b(still starting|again in|please wait|starting)\b/i.test(text) ? " wait" : "");
  el.textContent = text;
  return el;
}

// The starting message, kept while there is nothing to show (the HTML has the same words for a browser running no JS).
function empty() {
  const box = document.createElement("div"); box.className = "empty";
  const strong = document.createElement("strong"); strong.textContent = "Waiting for the server";
  const note = document.createElement("span");
  note.textContent = "Lines appear here as the server prints them - while a model loads, on every request, " +
                     "whenever something needs your attention.";
  box.append(strong, note);
  return box;
}

// Rebuild only when the number of lines moved: the poll runs every 1.2 s and a page that re-created 500 nodes each
// time would burn more CPU than the model does while idle.
function paint() {
  const box = $("log");
  if (shown !== lines.length) { box.replaceChildren(...(lines.length ? lines.map(row) : [empty()])); shown = lines.length; }
  if (follow) box.scrollTop = box.scrollHeight;
  $("count").textContent = lines.length ? `${lines.length} of ${MAX_LINES}` : "none yet";
  $("since").textContent = lines.length ? `since ${new Date(started).toLocaleTimeString()}` : "";
}

async function poll() {
  if (paused) return;
  try {
    const got = await api(`/api/log?after=${after}`);
    if (got.lines?.length) {
      started = started || Date.now();
      lines.push(...got.lines);
      while (lines.length > MAX_LINES) lines.shift();
    }
    after = got.after ?? after;
    dead = "";
  } catch (e) { dead = String(e.message || e); }
  $("error").hidden = !dead;
  $("error").textContent = dead ? `No lines from the server: ${dead}` : "";
  $("dot").className = "dot " + (dead ? "off" : "on");
  $("state").textContent = dead ? "disconnected" : paused ? "paused" : follow ? "following" : "watching";
  paint();
}

$("key").onclick = () => { $("auth").hidden = false; $("api-key").focus(); };
$("auth").onsubmit = async event => {
  event.preventDefault();
  apiKey = $("api-key").value.trim();
  sessionStorage.setItem("strata.console.key", apiKey);
  $("auth").hidden = true;
  await poll();
};
$("theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("strata.theme", next);
};
$("pause").onclick = async () => {                            // reading a long answer, and a new line keeps jumping you
  paused = !paused;
  $("pause").textContent = paused ? "Resume" : "Pause";
  $("pause").setAttribute("aria-pressed", String(paused));
  if (!paused) await poll();
};
$("follow").onclick = () => {
  follow = $("follow").getAttribute("aria-pressed") !== "true";
  paused = false;
  $("follow").textContent = follow ? "Following" : "Jump to end";
  $("follow").setAttribute("aria-pressed", String(follow));
  $("pause").textContent = "Pause"; $("pause").setAttribute("aria-pressed", "false");
  if (follow) $("log").scrollTop = $("log").scrollHeight;
};
$("log").addEventListener("scroll", () => {      // scrolled up by hand: stop yanking the view back down
  const atBottom = $("log").scrollHeight - $("log").scrollTop - $("log").clientHeight < 40;
  if (!atBottom && follow) {
    follow = false;
    $("follow").textContent = "Jump to end"; $("follow").setAttribute("aria-pressed", "false");
  }
});
$("copy").onclick = async () => {
  try { await navigator.clipboard.writeText(lines.join("\n")); $("copy").textContent = "Copied"; }
  catch { $("copy").textContent = "Copy failed"; }           // no clipboard permission (a non-secure origin)
  setTimeout(() => { $("copy").textContent = "Copy"; }, 1200);
};
$("clear").onclick = () => {                                  // this page only; the server keeps its own last 500
  lines = []; shown = -1; paint();
};

poll();
setInterval(poll, 1200);
