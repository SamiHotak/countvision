"""The start screen shown in the window while CountVision starts (a few seconds; the first start
also creates the database). Same look as the app: dark slate, one teal accent.

The window calls ``window.cvStatus(status)`` with the launcher's status whenever it changes.
Buttons call the Python side through pywebview's ``window.pywebview.api``.
"""

from __future__ import annotations

import html
import json

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; background: #0e1820; color: #dce6ea;
  font: 15px/1.5 "Barlow", "Bahnschrift", "Segoe UI", system-ui, sans-serif; }
main { min-height: 100%; display: grid; place-items: center; padding: 32px 16px; }
.box { width: min(440px, 100%); }
.mark { width: 48px; height: 48px; display: block; }
h1 { font-size: 28px; font-weight: 600; margin: 20px 0 4px; letter-spacing: -0.01em; }
.sub { color: #7f96a2; margin: 0 0 22px; }
.rail { position: relative; height: 2px; background: #233746; margin: 0 0 22px; overflow: hidden; }
.rail i { position: absolute; top: -3px; left: 0; width: 8px; height: 8px; border-radius: 50%;
  background: #19c3b1; animation: cross 2.4s ease-in-out infinite; }
@keyframes cross { from { transform: translateX(-12px); } to { transform: translateX(460px); } }
ol { list-style: none; margin: 0; padding: 0; }
li { display: grid; grid-template-columns: 18px 1fr; gap: 10px; padding: 7px 0; border-top: 1px solid #1a2a36; }
li:first-child { border-top: 0; }
.dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 7px; background: #4d6370; }
li[data-state=working] .dot { background: #e8b04b; }
li[data-state=done] .dot { background: #19c3b1; }
li[data-state=error] .dot { background: #e5675f; }
li[data-state=waiting] .label { color: #7f96a2; }
.detail { display: block; color: #7f96a2; font-size: 14px; }
.error { display: none; margin-top: 20px; padding: 12px 14px; border: 1px solid rgba(229,103,95,.5);
  border-left-width: 3px; border-radius: 6px; background: #13202a; }
body[data-phase=error] .error { display: block; }
body[data-phase=error] .rail i, body[data-phase=stopping] .rail i { animation: none; opacity: 0; }
.buttons { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
button { font: inherit; height: 36px; padding: 0 14px; border-radius: 6px; border: 1px solid #233746;
  background: #1a2a36; color: #dce6ea; cursor: pointer; }
button.primary { background: #19c3b1; color: #062621; border-color: #19c3b1; font-weight: 600; }
button:focus-visible { outline: 2px solid #19c3b1; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .rail i { animation: none; left: 50%; } }
"""

MARK = """<svg class="mark" viewBox="0 0 48 48" aria-hidden="true">
<rect width="48" height="48" rx="11" fill="#13202a" stroke="#233746"/>
<rect x="7" y="26.5" width="34" height="4" rx="2" fill="#19c3b1"/>
<path d="M24 9 L32 18 H26.5 V39 H21.5 V18 H16 Z" fill="#dce6ea"/></svg>"""

SCRIPT = """
const TEXT = { starting: ["Starting CountVision", "This takes a few seconds."],
  ready: ["Opening CountVision", ""], stopping: ["Stopping CountVision", "Saving the last numbers."],
  stopped: ["CountVision has stopped", ""], error: ["CountVision could not start", ""] };
function esc(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
window.cvStatus = function (st) {
  document.body.dataset.phase = st.phase;
  const t = TEXT[st.phase] || TEXT.starting;
  document.getElementById("title").textContent = t[0];
  document.getElementById("sub").textContent = t[1];
  document.getElementById("steps").innerHTML = st.steps.map(s =>
    `<li data-state="${s.state}"><span class="dot"></span><span><span class="label">${esc(s.label)}</span>` +
    (s.detail ? `<span class="detail">${esc(s.detail)}</span>` : "") + `</span></li>`).join("");
  document.getElementById("message").textContent = st.message || "";
};
function call(name) { if (window.pywebview && window.pywebview.api) window.pywebview.api[name](); }
"""


def page(status: dict) -> str:
    """The whole start screen with the current status already filled in."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CountVision</title>
<style>{CSS}</style></head>
<body data-phase="starting"><main><div class="box">
{MARK}
<h1 id="title">Starting CountVision</h1><p class="sub" id="sub">This takes a few seconds.</p>
<div class="rail" aria-hidden="true"><i></i></div>
<ol id="steps" aria-live="polite"></ol>
<div class="error" role="alert"><p id="message" style="margin:0"></p>
<div class="buttons"><button class="primary" onclick="call('retry')">Try again</button>
<button onclick="call('open_logs')">Open the log folder</button><button onclick="call('quit')">Quit</button></div></div>
</div></main>
<script>{SCRIPT}
window.cvStatus({json.dumps(status)});</script></body></html>"""


def text_report(status: dict) -> str:
    """The same status as plain text lines (``--no-window``)."""
    lines = [f"{html.unescape(s['label'])}: {s['state']}" + (f" ({s['detail']})" if s["detail"] else "")
             for s in status["steps"]]
    if status.get("message"):
        lines.append(status["message"])
    return "\n".join(lines)
