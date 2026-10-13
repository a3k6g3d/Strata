// serve/web/js/toast.js - the notices at the bottom right, and copying text to the clipboard.
// A notice can carry one button that does the thing it is asking for; that is what makes a refusal actionable.
import {$, icon, SPRITE} from "./util.js";

export function toast(kind, title, text = "", ms = 3500, action = null) {
  const names = {info: "info", success: "check", warn: "warning", error: "error"};
  const el = document.createElement("div");
  el.className = `st-toast st-toast--${kind}`;
  if (kind === "error") el.setAttribute("role", "alert");   // a failure must interrupt a screen reader, not wait its turn
  else el.setAttribute("role", "status");                    // everything else waits its turn politely
  el.innerHTML = `${icon(names[kind] || "info")}<div><div class="st-toast__title"></div><div class="t-text"></div></div>`;
  el.querySelector(".st-toast__title").textContent = title;
  el.querySelector(".t-text").textContent = text;
  if (action) {
    const b = document.createElement("button");
    b.className = "st-btn st-btn--secondary";
    b.style.height = "32px";
    b.style.marginLeft = "auto";
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  const x = document.createElement("button");            // a toast you did not read yet stays until you dismiss it
  x.type = "button"; x.className = "st-toast__x"; x.innerHTML = "&times;";
  x.setAttribute("aria-label", "Dismiss this notification");
  x.onclick = () => el.remove();
  el.appendChild(x);
  const kill = () => el.remove();
  let timer = setTimeout(kill, ms);
  el.addEventListener("mouseenter", () => clearTimeout(timer));   // reading it, or tabbed into it: it does not vanish
  el.addEventListener("focusin", () => clearTimeout(timer));
  el.addEventListener("mouseleave", () => { timer = setTimeout(kill, 2500); });
  el.addEventListener("focusout", () => { timer = setTimeout(kill, 2500); });
  $("toasts").appendChild(el);
}

export async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {                                   // http on another host: no async clipboard
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    const use = btn.querySelector("use");
    use.setAttribute("href", `${SPRITE}#i-check`);
    setTimeout(() => use.setAttribute("href", `${SPRITE}#i-copy`), 1500);
  }
  toast("success", "Copied to clipboard", "", 1800);
}
