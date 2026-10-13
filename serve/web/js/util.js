// serve/web/js/util.js - the small things every part of the page uses: ids, icons, escaping, numbers, this browser's storage.
// Split out of app.js (it was one 2,000-line file); nothing here knows anything about the server or the chat.
export const $ = (id) => document.getElementById(id);
export const SPRITE = "web/sprite.svg";
export const icon = (name, cls = "st-icon") => `<svg class="${cls}" aria-hidden="true"><use href="${SPRITE}#i-${name}"/></svg>`;
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
export const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d}));
export const kfmt = (n) => (n == null ? "–" : n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : fmt(n));
// a context size: 32768 -> "32K" (powers of two), else like kfmt
export const ctxfmt = (n) => (n && n % 1024 === 0 ? `${fmt(n / 1024)}K` : kfmt(n));
export const gb = (b, d = 1) => (b == null ? "–" : fmt(b / 1073741824, d));   // memory: binary GB, as Windows shows it

export const store = {
  get(k, d) { try { const v = localStorage.getItem("strata." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("strata." + k, JSON.stringify(v)); } catch (e) { /* private mode: in memory only */ } },
  remove(k) { try { localStorage.removeItem("strata." + k); } catch (e) { /* ignore */ } },
};

// the messages keep a scrollbar gutter on both sides (so they stay centred); the message box gets the same margin, so both are as wide
export function syncGutter() {
  const sc = document.getElementById("chat-scroll");
  if (sc) document.documentElement.style.setProperty("--sbw", Math.max(0, (sc.offsetWidth - sc.clientWidth) / 2) + "px");
}
addEventListener("resize", syncGutter);
addEventListener("load", syncGutter);
