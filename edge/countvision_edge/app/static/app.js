/* CountVision local app. Plain JavaScript, no build step, works offline.
 *
 * Data flow:
 *   /api/live (server-sent events)  -> status + totals twice per second
 *   /api/preview.mjpg               -> annotated picture (boxes and track ids only)
 *   /api/geometry                   -> lines and zones (0..1 coordinates), drawn on a canvas
 *   /api/timeline                   -> chart data
 */
"use strict";

const $ = (id) => document.getElementById(id);
const HEADERS = { "X-CountVision": "1" };

const S = {
  meta: { class_colors: {}, default_class_color: "#c8c8c8" },
  status: null,
  totals: null,
  geom: { lines: [], zones: [], version: -1, anchor: "bottom_center" },
  draft: null,           // copy of geom while editing
  editing: false,
  tool: "select",
  sel: null,             // { type: "line"|"zone", i }
  drag: null,            // current pointer action
  pending: null,         // zone being drawn: [[x,y], ...]
  pointer: null,         // last pointer position (normalized) for rubber band
  snapGuide: null,
  history: [],
  future: [],
  snap: true,
  chartLine: null,
  chartKey: "",
  runKey: "",
  previewOk: false,
};

// ------------------------------------------------------------------ helpers

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...HEADERS, ...(options.headers || {}) } };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const data = await res.json();
      msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* not JSON */ }
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return res.json();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

const fmt = (n) => (n ?? 0).toLocaleString();
function duration(s) {
  if (!s) return "0 s";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m} min ${r.toString().padStart(2, "0")} s`;
}
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const clone = (o) => JSON.parse(JSON.stringify(o));
const classColor = (name) => S.meta.class_colors[name] || S.meta.default_class_color;

let toastTimer = null;
function toast(text, isError = false) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("err", isError);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3500);
}

// ------------------------------------------------------------------ start-up

/** Excel in Germany (and other comma-decimal countries) expects ";" between CSV columns. */
function csvDelimiter() {
  return (1.5).toLocaleString().includes(",") ? ";" : ",";
}

async function init() {
  try {
    S.meta = await api("/api/meta");
    document.documentElement.style.setProperty("--accent", S.meta.accent);
  } catch (_) { /* defaults are fine */ }
  await loadGeometry();
  const csvHref = `/api/export.csv?delimiter=${encodeURIComponent(csvDelimiter())}`;
  for (const link of document.querySelectorAll('a[href="/api/export.csv"]')) link.href = csvHref;
  bindUi();
  startPreview();
  startLive();
  resizeCanvas();
  new ResizeObserver(() => { resizeCanvas(); drawChart(); }).observe($("stage"));
  setInterval(loadTimeline, 20000);
}

async function loadGeometry() {
  try {
    const g = await api("/api/geometry");
    S.geom = g;
    $("anchorWord").textContent = g.anchor === "center" ? "body centre" : "feet";
    if (!S.editing) renderNumbers();
    draw();
  } catch (e) {
    toast(`Could not load the lines: ${e.message}`, true);
  }
}

// ------------------------------------------------------------------ live data

function startLive() {
  const source = new EventSource("/api/live");
  source.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    const before = S.status;
    S.status = data.status;
    S.totals = data.totals;
    renderStatus();
    if (!S.editing) renderNumbers();
    if (before && before.config_version !== S.status.config_version && !S.editing) loadGeometry();
    const run = S.status.run;
    const runKey = run ? `${run.camera_id}:${run.started_at}` : "";
    if (runKey !== S.runKey) { S.runKey = runKey; S.chartKey = ""; restartPreview(); }
    const key = chartKeyOf();
    if (key !== S.chartKey) { S.chartKey = key; loadTimeline(); }
    draw();
  };
  source.onerror = () => {
    setStatus("err", "No connection to CountVision. Is the app still running?", "");
  };
}

function chartKeyOf() {
  if (!S.totals) return "";
  let sum = 0;
  for (const v of Object.values(S.totals.lines)) sum += v.in + v.out;
  return `${S.totals.scope.camera_id}:${S.totals.scope.start}:${sum}:${S.chartLine}`;
}

function setStatus(kind, text, fps) {
  $("statusDot").className = `dot ${kind}`;
  $("statusText").textContent = text;
  $("statusFps").textContent = fps;
}

function renderStatus() {
  const st = S.status;
  $("camName").textContent = st.camera.name;
  $("detectorInfo").textContent = `Detector: ${st.detector.name}. Licence: ${st.detector.license}`;
  const run = st.run;
  const fps = st.fps ? `${st.fps.toFixed(1)} FPS` : "";
  if (!run) setStatus("", "Not running", "");
  else if (run.state === "error") setStatus("err", `Stopped: ${run.error}`, "");
  else if (run.state === "finished") setStatus("finished", run.kind === "file" ? `Video counted: ${run.label}` : "Finished", "");
  else if (run.state === "stopped") setStatus("", "Stopped", "");
  else if (st.source && !st.source.connected) setStatus("warn", `${run.label}: not connected, trying again`, "");
  else if (run.kind === "file") {
    const pct = run.progress != null ? ` ${Math.round(run.progress * 100)}%` : "";
    setStatus("live", `Counting video ${run.label}${pct}`, fps);
  } else setStatus("live", `Live, ${run.label}`, fps);

  const frame = st.frame || S.geom.frame;
  if (frame) {
    $("stage").style.aspectRatio = `${frame.width} / ${frame.height}`;
    $("stage").style.setProperty("--ar", String(frame.width / frame.height));
  }
  const msg = $("stageMsg");
  if (st.preview === "off") msg.textContent = "The preview picture is switched off. Lines still count.";
  else if (st.source && !st.source.connected) msg.textContent = "The camera is not connected. CountVision keeps trying.";
  else msg.textContent = "Waiting for the camera";
}

// ------------------------------------------------------------------ numbers panel

function renderNumbers() {
  const t = S.totals;
  const st = S.status;
  if (!t || !st) return;
  const video = t.scope.kind === "video";
  $("scopeTitle").textContent = video ? "This video" : "Today";
  $("chartTitle").textContent = video ? "This video, per minute" : "Today, per hour";
  $("scopeSub").textContent = video
    ? (st.run ? st.run.label : "")
    : new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  const run = st.run;
  const showProgress = video && run && run.state === "running" && run.progress != null;
  $("progress").hidden = !showProgress;
  if (showProgress) $("progressBar").style.width = `${Math.round(run.progress * 100)}%`;
  $("videoDone").hidden = !(video && run && run.state === "finished");

  const lineList = $("lineList");
  const zoneList = $("zoneList");
  const lineNames = S.geom.lines.map((l) => l.name);
  const zoneNames = S.geom.zones.map((z) => z.name);
  $("emptyNumbers").hidden = lineNames.length + zoneNames.length > 0;

  syncList(lineList, lineNames, (name) => lineMetric(name));
  syncList(zoneList, zoneNames, (name) => zoneMetric(name));
  for (const name of lineNames) updateLineMetric(lineList.querySelector(`[data-name="${CSS.escape(name)}"]`), t.lines[name]);
  for (const name of zoneNames) {
    const live = st.live.zones[name] || { occupancy: 0, queue_length: 0 };
    const zone = S.geom.zones.find((z) => z.name === name);
    updateZoneMetric(zoneList.querySelector(`[data-name="${CSS.escape(name)}"]`), t.zones[name], live, zone);
  }
  renderChartTabs(lineNames);
}

function syncList(container, names, build) {
  const current = [...container.children].map((c) => c.dataset.name);
  if (current.join("\n") === names.join("\n")) return;
  container.replaceChildren(...names.map(build));
}

function lineMetric(name) {
  return el("div", { class: "metric", "data-name": name },
    el("div", { class: "metric-name" }, el("span", { text: name }), el("span", { class: "kind", text: "line" })),
    el("div", { class: "pair" },
      el("div", {}, el("div", { class: "big accent", "data-k": "in", text: "0" }), el("div", { class: "lbl", text: "In" })),
      el("div", {}, el("div", { class: "big", "data-k": "out", text: "0" }), el("div", { class: "lbl", text: "Out" }))),
    el("div", { class: "facts", "data-k": "facts" }));
}

function setNumber(node, value) {
  const text = fmt(value);
  if (node.textContent !== text) {
    const grew = Number(node.dataset.v ?? value) < value;
    node.textContent = text;
    node.dataset.v = value;
    if (grew) { node.classList.remove("bump"); void node.offsetWidth; node.classList.add("bump"); }
  }
}

function updateLineMetric(node, data) {
  if (!node) return;
  data = data || { in: 0, out: 0, by_class: {} };
  setNumber(node.querySelector('[data-k="in"]'), data.in);
  setNumber(node.querySelector('[data-k="out"]'), data.out);
  const facts = node.querySelector('[data-k="facts"]');
  const parts = [el("span", {}, "In minus out ", el("b", { text: fmt(data.in - data.out) }))];
  for (const [cls, c] of Object.entries(data.by_class || {})) {
    const dot = el("i");
    dot.style.background = classColor(cls);
    parts.push(el("span", { class: "cls" }, dot, `${cls} ${fmt(c.in)} in, ${fmt(c.out)} out`));
  }
  facts.replaceChildren(...parts);
}

function zoneMetric(name) {
  return el("div", { class: "metric", "data-name": name },
    el("div", { class: "metric-name" }, el("span", { text: name }), el("span", { class: "kind", "data-k": "kind" })),
    el("div", { class: "pair" },
      el("div", {}, el("div", { class: "big mid accent", "data-k": "inside", text: "0" }), el("div", { class: "lbl", text: "Inside now" })),
      el("div", { "data-k": "qwrap" }, el("div", { class: "big mid", "data-k": "queue", text: "0" }), el("div", { class: "lbl", text: "Waiting now" }))),
    el("div", { class: "facts", "data-k": "facts" }));
}

function updateZoneMetric(node, data, live, zone) {
  if (!node) return;
  data = data || { visits: 0, dwell_avg_s: 0, occupancy_max: 0 };
  const queue = zone && zone.kind === "queue";
  node.querySelector('[data-k="kind"]').textContent = queue ? "queue" : "area";
  setNumber(node.querySelector('[data-k="inside"]'), live.occupancy);
  node.querySelector('[data-k="qwrap"]').hidden = !queue;
  setNumber(node.querySelector('[data-k="queue"]'), live.queue_length);
  const peak = Math.max(data.occupancy_max || 0, live.occupancy || 0);
  node.querySelector('[data-k="facts"]').replaceChildren(
    el("span", {}, "Visits ", el("b", { text: fmt(data.visits) })),
    el("span", {}, "Average stay ", el("b", { text: data.visits ? duration(data.dwell_avg_s) : "none yet" })),
    el("span", {}, "Most inside ", el("b", { text: fmt(peak) })));
}

// ------------------------------------------------------------------ chart

function renderChartTabs(lineNames) {
  if (S.chartLine !== null && !lineNames.includes(S.chartLine)) S.chartLine = null;
  const wrap = $("chartLines");
  const names = lineNames.length > 1 ? [null, ...lineNames] : [];
  const key = names.map(String).join("\n");
  if (wrap.dataset.key !== key) {
    wrap.dataset.key = key;
    wrap.replaceChildren(...names.map((n) => el("button", {
      type: "button", text: n === null ? "All lines" : n, "data-line": n ?? "",
      onclick: () => { S.chartLine = n; renderChartTabs(lineNames); S.chartKey = ""; loadTimeline(); },
    })));
  }
  for (const b of wrap.children) b.setAttribute("aria-pressed", String((b.dataset.line || null) === S.chartLine));
}

let timeline = null;
let timelineBusy = false;
async function loadTimeline() {
  if (timelineBusy) return;
  timelineBusy = true;
  try {
    const q = S.chartLine ? `?line=${encodeURIComponent(S.chartLine)}` : "";
    timeline = await api(`/api/timeline${q}`);
    drawChart();
  } catch (_) { /* next update tries again */ }
  finally { timelineBusy = false; }
}

function bucketLabel(t, bucket, scopeStart) {
  if (bucket >= 3600) return `${new Date(t * 1000).getHours().toString().padStart(2, "0")}:00`;
  const m = Math.round((t - scopeStart) / 60);
  return `minute ${m + 1}`;
}

function niceMax(v) {
  if (v <= 5) return 5;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function drawChart() {
  const svg = $("chart");
  const width = svg.clientWidth || 600;
  const height = svg.clientHeight || 190;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (!timeline) { svg.replaceChildren(); return; }
  const NS = "http://www.w3.org/2000/svg";
  const make = (tag, attrs) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const buckets = timeline.buckets;
  const scope = timeline.scope;
  const left = 34, right = 6, top = 6, bottom = 20;
  const plotW = width - left - right;
  const mid = top + (height - top - bottom) / 2;
  const half = (height - top - bottom) / 2 - 2;
  const peak = niceMax(Math.max(1, ...buckets.map((b) => Math.max(b.in, b.out))));
  const n = buckets.length;
  const step = plotW / n;
  const barW = Math.max(1.5, Math.min(26, step * 0.66));
  const nodes = [];
  nodes.push(make("line", { class: "grid", x1: left, x2: width - right, y1: mid - half, y2: mid - half }));
  nodes.push(make("line", { class: "grid", x1: left, x2: width - right, y1: mid + half, y2: mid + half }));
  const yTop = make("text", { class: "ymax", x: left - 6, y: mid - half + 4, "text-anchor": "end" });
  yTop.textContent = fmt(peak);
  const yBot = make("text", { class: "ymax", x: left - 6, y: mid + half + 4, "text-anchor": "end" });
  yBot.textContent = fmt(peak);
  nodes.push(yTop, yBot);
  const now = Date.now() / 1000;
  const labelEvery = scope.bucket_s >= 3600 ? 3 : Math.max(1, Math.ceil(n / 8));
  buckets.forEach((b, i) => {
    const cx = left + step * i + step / 2;
    if (now >= b.t && now < b.t + scope.bucket_s && scope.kind === "today") {
      nodes.push(make("rect", { class: "now", x: left + step * i, y: top, width: step, height: height - top - bottom }));
    }
    const hIn = (b.in / peak) * half;
    const hOut = (b.out / peak) * half;
    if (b.in) nodes.push(make("rect", { class: "bin", x: cx - barW / 2, y: mid - hIn, width: barW, height: hIn, rx: 1.5 }));
    if (b.out) nodes.push(make("rect", { class: "bout", x: cx - barW / 2, y: mid + 1, width: barW, height: hOut, rx: 1.5 }));
    const hit = make("rect", { x: left + step * i, y: top, width: step, height: height - top - bottom, fill: "transparent", "data-i": i });
    nodes.push(hit);
    if (i % labelEvery === 0) {
      const t = make("text", { x: cx, y: height - 5, "text-anchor": "middle" });
      t.textContent = scope.bucket_s >= 3600 ? new Date(b.t * 1000).getHours().toString().padStart(2, "0") : String(i + 1);
      nodes.push(t);
    }
  });
  nodes.push(make("line", { class: "base", x1: left, x2: width - right, y1: mid + 0.5, y2: mid + 0.5 }));
  svg.replaceChildren(...nodes);
  const total = buckets.reduce((a, b) => ({ in: a.in + b.in, out: a.out + b.out }), { in: 0, out: 0 });
  const showTotal = () => {
    $("chartReadout").replaceChildren("Total ", el("b", { text: fmt(total.in) }), " in, ", el("b", { text: fmt(total.out) }), " out");
  };
  showTotal();
  svg.onpointermove = svg.onpointerdown = (ev) => {
    const i = Number(ev.target.dataset && ev.target.dataset.i);
    if (Number.isNaN(i) || ev.target.dataset.i === undefined) return;
    const b = buckets[i];
    const label = bucketLabel(b.t, scope.bucket_s, scope.start);
    $("chartReadout").replaceChildren(`${label}: `, el("b", { text: fmt(b.in) }), " in, ", el("b", { text: fmt(b.out) }), " out");
  };
  svg.onpointerleave = showTotal;
}

// ------------------------------------------------------------------ preview picture

function startPreview() {
  const img = $("preview");
  img.onload = () => { S.previewOk = true; $("stage").classList.add("has-picture"); };
  img.onerror = () => {
    S.previewOk = false;
    $("stage").classList.remove("has-picture");
    setTimeout(restartPreview, 2000);
  };
  restartPreview();
}

function restartPreview() {
  if (S.status && S.status.preview === "off") { $("preview").removeAttribute("src"); return; }
  $("preview").src = `/api/preview.mjpg?t=${Date.now()}`;
}

// ------------------------------------------------------------------ canvas drawing

const canvas = $("overlay");
const ctx = canvas.getContext("2d");
let cssW = 1, cssH = 1;

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cssW = Math.max(1, rect.width);
  cssH = Math.max(1, rect.height);
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

const toPx = (p) => [p[0] * cssW, p[1] * cssH];
const geom = () => (S.editing ? S.draft : S.geom);
const accent = () => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#19c3b1";

function inArrow(line) {
  const [ax, ay] = toPx(line.p1);
  const [bx, by] = toPx(line.p2);
  const dx = bx - ax, dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  let nx = -dy / len, ny = dx / len;          // screen-right side of p1 -> p2
  if (line.in_direction === "to_left") { nx = -nx; ny = -ny; }
  return { mx: (ax + bx) / 2, my: (ay + by) / 2, nx, ny };
}

function label(text, x, y, color) {
  const size = cssW < 520 ? 11 : 13;   // smaller labels on phones
  const boxH = size + 7;
  ctx.font = `600 ${size}px Bahnschrift, 'DIN Alternate', 'Segoe UI', sans-serif`;
  const w = ctx.measureText(text).width + 10;
  const lx = Math.min(Math.max(2, x), cssW - w - 2);
  const ly = Math.min(Math.max(2, y), cssH - boxH - 2);
  ctx.fillStyle = "rgba(8, 16, 22, 0.82)";
  ctx.beginPath();
  ctx.roundRect(lx, ly, w, boxH, 4);
  ctx.fill();
  ctx.fillStyle = color;
  ctx.fillText(text, lx + 5, ly + size + 1.5);
}

function draw() {
  ctx.clearRect(0, 0, cssW, cssH);
  const g = geom();
  if (!g) return;
  const color = accent();
  const totals = S.totals || { lines: {}, zones: {} };
  const live = (S.status && S.status.live) || { zones: {} };
  const handleR = S.touch ? 11 : 6;

  g.zones.forEach((z, i) => {
    const selected = S.editing && S.sel && S.sel.type === "zone" && S.sel.i === i;
    const pts = z.polygon.map(toPx);
    ctx.beginPath();
    pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
    ctx.fillStyle = color + (selected ? "33" : "22");
    ctx.fill();
    ctx.lineWidth = selected ? 3 : 2;
    ctx.strokeStyle = color;
    ctx.setLineDash(z.kind === "queue" ? [8, 5] : []);
    ctx.stroke();
    ctx.setLineDash([]);
    const top = pts.reduce((a, b) => (b[1] < a[1] ? b : a));
    const lz = live.zones[z.name];
    const text = S.editing ? `${z.name} (${z.kind})` : `${z.name}: ${lz ? lz.occupancy : 0} inside${z.kind === "queue" && lz ? `, ${lz.queue_length} waiting` : ""}`;
    label(text, top[0] - 4, top[1] - 26, color);
    if (S.editing) pts.forEach(([x, y]) => handle(x, y, handleR, selected));
  });

  g.lines.forEach((l, i) => {
    const selected = S.editing && S.sel && S.sel.type === "line" && S.sel.i === i;
    const [ax, ay] = toPx(l.p1);
    const [bx, by] = toPx(l.p2);
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(0,0,0,0.55)";
    ctx.lineWidth = selected ? 9 : 7;
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    ctx.strokeStyle = color;
    ctx.lineWidth = selected ? 5 : 3;
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    const { mx, my, nx, ny } = inArrow(l);
    const tipX = mx + nx * 34, tipY = my + ny * 34;
    ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(tipX, tipY); ctx.stroke();
    const ang = Math.atan2(ny, nx);
    ctx.beginPath();
    ctx.moveTo(tipX + Math.cos(ang) * 4, tipY + Math.sin(ang) * 4);
    ctx.lineTo(tipX - Math.cos(ang - 0.55) * 12, tipY - Math.sin(ang - 0.55) * 12);
    ctx.lineTo(tipX - Math.cos(ang + 0.55) * 12, tipY - Math.sin(ang + 0.55) * 12);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    label("IN", tipX + nx * 10 - 12, tipY + ny * 10 - 10, color);
    const t = totals.lines[l.name];
    const text = S.editing ? l.name : `${l.name}  in ${t ? t.in : 0}  out ${t ? t.out : 0}`;
    label(text, Math.min(ax, bx), Math.min(ay, by) - 28, "#dce6ea");
    if (S.editing) { handle(ax, ay, handleR, selected); handle(bx, by, handleR, selected); }
  });

  if (S.editing && S.pending) {
    const pts = S.pending.map(toPx);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    if (S.pointer) { const [px, py] = toPx(S.pointer); ctx.lineTo(px, py); }
    ctx.stroke();
    ctx.setLineDash([]);
    pts.forEach(([x, y], k) => handle(x, y, k === 0 && pts.length >= 3 ? handleR + 3 : handleR, k === 0));
  }
  if (S.editing && S.snapGuide) {
    const [x, y] = toPx(S.snapGuide);
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(x, y, handleR + 6, 0, Math.PI * 2); ctx.stroke();
  }
}

function handle(x, y, r, strong) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fillStyle = strong ? accent() : "#dce6ea";
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#081016";
  ctx.stroke();
}

// ------------------------------------------------------------------ editor: geometry helpers

function allPoints(exclude) {
  const out = [];
  S.draft.lines.forEach((l, i) => ["p1", "p2"].forEach((k) => {
    if (!(exclude && exclude.type === "line" && exclude.i === i && exclude.key === k)) out.push(l[k]);
  }));
  S.draft.zones.forEach((z, i) => z.polygon.forEach((p, k) => {
    if (!(exclude && exclude.type === "zone" && exclude.i === i && exclude.key === k)) out.push(p);
  }));
  if (S.pending) S.pending.forEach((p) => out.push(p));
  return out;
}

/** Snap: to an existing corner within 12 px, else (lines) to 0/45/90 degrees within 6 degrees. */
function snapPoint(p, exclude, anchor) {
  S.snapGuide = null;
  if (!S.snap) return [clamp01(p[0]), clamp01(p[1])];
  const [px, py] = toPx(p);
  let best = null, bestD = 12;
  for (const q of allPoints(exclude)) {
    const [qx, qy] = toPx(q);
    const d = Math.hypot(qx - px, qy - py);
    if (d < bestD) { best = q; bestD = d; }
  }
  if (best) { S.snapGuide = best; return [best[0], best[1]]; }
  if (anchor) {
    const [ax, ay] = toPx(anchor);
    const dx = px - ax, dy = py - ay;
    const len = Math.hypot(dx, dy);
    const ang = Math.atan2(dy, dx);
    const target = Math.round(ang / (Math.PI / 4)) * (Math.PI / 4);
    if (Math.abs(ang - target) < (6 * Math.PI) / 180) {
      return [clamp01((ax + Math.cos(target) * len) / cssW), clamp01((ay + Math.sin(target) * len) / cssH)];
    }
  }
  return [clamp01(p[0]), clamp01(p[1])];
}

function distToSegment(p, a, b) {
  const [px, py] = p, [ax, ay] = a, [bx, by] = b;
  const dx = bx - ax, dy = by - ay;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function insidePolygon([x, y], poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** What is under the pointer (pixel position): a handle first, then a line, then a zone. */
function hitTest(px) {
  const r = (S.touch ? 18 : 10);
  const d = S.draft;
  for (let i = d.lines.length - 1; i >= 0; i--) {
    for (const key of ["p1", "p2"]) {
      const [x, y] = toPx(d.lines[i][key]);
      if (Math.hypot(x - px[0], y - px[1]) <= r) return { type: "line", i, key };
    }
  }
  for (let i = d.zones.length - 1; i >= 0; i--) {
    for (let k = 0; k < d.zones[i].polygon.length; k++) {
      const [x, y] = toPx(d.zones[i].polygon[k]);
      if (Math.hypot(x - px[0], y - px[1]) <= r) return { type: "zone", i, key: k };
    }
  }
  for (let i = d.lines.length - 1; i >= 0; i--) {
    if (distToSegment(px, toPx(d.lines[i].p1), toPx(d.lines[i].p2)) <= r) return { type: "line", i, key: null };
  }
  for (let i = d.zones.length - 1; i >= 0; i--) {
    if (insidePolygon(px, d.zones[i].polygon.map(toPx))) return { type: "zone", i, key: null };
  }
  return null;
}

function uniqueName(base) {
  const used = new Set([...S.draft.lines, ...S.draft.zones].map((x) => x.name));
  for (let n = 1; ; n++) if (!used.has(`${base} ${n}`)) return `${base} ${n}`;
}

// ------------------------------------------------------------------ editor: history

function pushHistory() {
  S.history.push(clone(S.draft));
  if (S.history.length > 100) S.history.shift();
  S.future = [];
  updateEditUi();
}
function undo() {
  if (S.pending) { S.pending = null; draw(); return; }
  if (!S.history.length) return;
  S.future.push(clone(S.draft));
  S.draft = S.history.pop();
  fixSelection();
  updateEditUi();
  draw();
}
function redo() {
  if (!S.future.length) return;
  S.history.push(clone(S.draft));
  S.draft = S.future.pop();
  fixSelection();
  updateEditUi();
  draw();
}
function fixSelection() {
  if (!S.sel) return;
  const list = S.sel.type === "line" ? S.draft.lines : S.draft.zones;
  if (S.sel.i >= list.length) S.sel = null;
}
const isDirty = () => S.editing && JSON.stringify({ l: S.draft.lines, z: S.draft.zones }) !== JSON.stringify({ l: S.geom.lines, z: S.geom.zones });

// ------------------------------------------------------------------ editor: pointer

function normFromEvent(ev) {
  const rect = canvas.getBoundingClientRect();
  return [(ev.clientX - rect.left) / rect.width, (ev.clientY - rect.top) / rect.height];
}

canvas.addEventListener("pointerdown", (ev) => {
  if (!S.editing || ev.button > 0) return;
  S.touch = ev.pointerType === "touch";
  const n = normFromEvent(ev);
  const px = toPx(n);
  canvas.setPointerCapture(ev.pointerId);
  ev.preventDefault();

  if (S.tool === "select") {
    const hit = hitTest(px);
    if (!hit) { S.sel = null; updateEditUi(); draw(); return; }
    S.sel = { type: hit.type, i: hit.i };
    const item = hit.type === "line" ? S.draft.lines[hit.i] : S.draft.zones[hit.i];
    S.drag = { kind: hit.key === null ? "move" : "point", hit, start: n, orig: clone(item), saved: false };
    updateEditUi();
    draw();
  } else if (S.tool === "line") {
    const p = snapPoint(n, null, null);
    S.drag = { kind: "newline", p1: p, p2: p };
    S.draft.__preview = { name: "", p1: p, p2: p, in_direction: "to_right" };
  } else if (S.tool === "zone") {
    const p = snapPoint(n, null, null);
    if (S.pending && S.pending.length >= 3) {
      const [fx, fy] = toPx(S.pending[0]);
      if (Math.hypot(fx - px[0], fy - px[1]) <= (S.touch ? 20 : 12)) { finishZone(); return; }
    }
    if (!S.pending) S.pending = [];
    S.pending.push(p);
    updateHint();
    draw();
  }
});

canvas.addEventListener("pointermove", (ev) => {
  if (!S.editing) return;
  const n = normFromEvent(ev);
  S.pointer = n;
  const d = S.drag;
  if (!d) {
    if (S.tool === "zone" && S.pending) { snapPoint(n, null, null); draw(); }
    else if (S.tool === "select") {
      const hit = hitTest(toPx(n));
      canvas.style.cursor = hit ? (hit.key === null ? "move" : "grab") : "default";
    }
    return;
  }
  if (d.kind === "point" || d.kind === "move") {
    if (!d.saved) {
      if (Math.hypot((n[0] - d.start[0]) * cssW, (n[1] - d.start[1]) * cssH) < 2) return;
      S.history.push(clone(S.draft)); S.future = []; d.saved = true;
    }
    const { hit } = d;
    if (d.kind === "point") {
      if (hit.type === "line") {
        const other = S.draft.lines[hit.i][hit.key === "p1" ? "p2" : "p1"];
        S.draft.lines[hit.i][hit.key] = snapPoint(n, hit, other);
      } else {
        S.draft.zones[hit.i].polygon[hit.key] = snapPoint(n, hit, null);
      }
    } else {
      let dx = n[0] - d.start[0], dy = n[1] - d.start[1];
      const pts = hit.type === "line" ? [d.orig.p1, d.orig.p2] : d.orig.polygon;
      // keep the whole shape inside the picture
      dx = Math.min(Math.max(dx, -Math.min(...pts.map((p) => p[0]))), 1 - Math.max(...pts.map((p) => p[0])));
      dy = Math.min(Math.max(dy, -Math.min(...pts.map((p) => p[1]))), 1 - Math.max(...pts.map((p) => p[1])));
      const move = (p) => [p[0] + dx, p[1] + dy];
      if (hit.type === "line") {
        S.draft.lines[hit.i].p1 = move(d.orig.p1);
        S.draft.lines[hit.i].p2 = move(d.orig.p2);
      } else {
        S.draft.zones[hit.i].polygon = d.orig.polygon.map(move);
      }
    }
    draw();
  } else if (d.kind === "newline") {
    d.p2 = snapPoint(n, null, d.p1);
    S.draft.__preview.p2 = d.p2;
    drawWithPreview();
  }
});

function drawWithPreview() {
  const p = S.draft.__preview;
  if (!p) { draw(); return; }
  S.draft.lines.push(p);
  draw();
  S.draft.lines.pop();
}

function endPointer() {
  const d = S.drag;
  S.drag = null;
  S.snapGuide = null;
  if (!d) return;
  if (d.kind === "newline") {
    delete S.draft.__preview;
    const [ax, ay] = toPx(d.p1), [bx, by] = toPx(d.p2);
    if (Math.hypot(bx - ax, by - ay) < 15) { updateHint("Drag across the picture to draw a line."); draw(); return; }
    pushHistory();
    S.draft.lines.push({ name: uniqueName("line"), p1: d.p1, p2: d.p2, in_direction: "to_right" });
    S.sel = { type: "line", i: S.draft.lines.length - 1 };
    setTool("select");
  }
  updateEditUi();
  draw();
}
canvas.addEventListener("pointerup", endPointer);
canvas.addEventListener("pointercancel", endPointer);
canvas.addEventListener("dblclick", (ev) => {
  if (S.editing && S.tool === "zone" && S.pending) {
    ev.preventDefault();
    // a double click also produced two single clicks: drop the duplicate last point
    if (S.pending.length > 3) S.pending.pop();
    finishZone();
  }
});

function finishZone() {
  if (!S.pending || S.pending.length < 3) { updateHint("A zone needs at least 3 corners."); return; }
  pushHistory();
  S.draft.zones.push({ name: uniqueName("zone"), kind: "area", polygon: S.pending });
  S.pending = null;
  S.sel = { type: "zone", i: S.draft.zones.length - 1 };
  setTool("select");
  updateEditUi();
  draw();
}

// ------------------------------------------------------------------ editor: modes and UI

function enterEdit() {
  if (!S.geom.ready) { toast("Wait until the camera shows a picture, then edit the lines.", true); return; }
  S.editing = true;
  S.draft = clone({ lines: S.geom.lines, zones: S.geom.zones });
  S.history = []; S.future = []; S.sel = null; S.pending = null;
  $("editBar").hidden = false;
  $("hint").hidden = false;
  $("numbers").hidden = true;
  $("inspector").hidden = false;
  $("stage").classList.add("editing");
  $("layout").classList.add("editing");
  $("btnEdit").hidden = true;
  setTool(S.draft.lines.length + S.draft.zones.length ? "select" : "line");
  updateEditUi();
  draw();
}

function exitEdit() {
  S.editing = false;
  S.draft = null; S.sel = null; S.pending = null; S.drag = null;
  $("editBar").hidden = true;
  $("hint").hidden = true;
  $("numbers").hidden = false;
  $("inspector").hidden = true;
  $("stage").classList.remove("editing", "tool-select");
  $("layout").classList.remove("editing");
  $("btnEdit").hidden = false;
  canvas.style.cursor = "";
  renderNumbers();
  draw();
}

function setTool(tool) {
  if (S.pending && tool !== "zone") S.pending = null;
  S.tool = tool;
  for (const b of document.querySelectorAll("[data-tool]")) b.setAttribute("aria-pressed", String(b.dataset.tool === tool));
  $("stage").classList.toggle("tool-select", tool === "select");
  updateHint();
  draw();
}

function updateHint(text) {
  const hints = {
    select: "Drag a corner to move it. Drag a line or zone to move all of it. Press F to flip the In direction.",
    line: "Drag across the door or path. The arrow shows the In direction.",
    zone: S.pending
      ? `Click the next corner (${S.pending.length} so far). Click the first corner, double-click or press Enter to finish.`
      : "Click the corners of the zone one by one.",
  };
  $("hint").textContent = text || hints[S.tool];
}

function updateEditUi() {
  $("btnUndo").disabled = !S.history.length && !S.pending;
  $("btnRedo").disabled = !S.future.length;
  $("btnSnap").setAttribute("aria-pressed", String(S.snap));
  const list = $("shapeList");
  const items = [
    ...S.draft.lines.map((l, i) => ({ type: "line", i, name: l.name, sub: l.in_direction === "to_right" ? "line" : "line, flipped" })),
    ...S.draft.zones.map((z, i) => ({ type: "zone", i, name: z.name, sub: z.kind })),
  ];
  list.replaceChildren(...items.map((it) => {
    const glyph = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    glyph.setAttribute("viewBox", "0 0 18 18");
    glyph.setAttribute("class", "glyph");
    glyph.innerHTML = it.type === "line" ? '<path d="M2 15 L16 3"/>' : '<path d="M3 4 L15 3 L15 15 L3 14 Z"/>';
    const current = S.sel && S.sel.type === it.type && S.sel.i === it.i;
    return el("li", {}, el("button", {
      type: "button", "aria-current": String(current),
      onclick: () => { S.sel = { type: it.type, i: it.i }; setTool("select"); updateEditUi(); },
    }, glyph, el("span", { text: it.name }), el("span", { class: "sub", text: it.sub })));
  }));
  if (!items.length) list.replaceChildren(el("li", { class: "note", text: "Nothing drawn yet." }));

  const sel = S.sel;
  $("props").hidden = !sel;
  if (sel) {
    const item = sel.type === "line" ? S.draft.lines[sel.i] : S.draft.zones[sel.i];
    if (document.activeElement !== $("propName")) $("propName").value = item.name;
    $("propDirWrap").hidden = sel.type !== "line";
    $("propKindWrap").hidden = sel.type !== "zone";
    if (sel.type === "zone") $("propKind").value = item.kind;
  }
}

function selectedItem() {
  if (!S.sel) return null;
  return S.sel.type === "line" ? S.draft.lines[S.sel.i] : S.draft.zones[S.sel.i];
}

function flipSelected() {
  const item = selectedItem();
  if (!item || S.sel.type !== "line") return;
  pushHistory();
  item.in_direction = item.in_direction === "to_right" ? "to_left" : "to_right";
  updateEditUi();
  draw();
}

function deleteSelected() {
  if (!S.sel) return;
  pushHistory();
  (S.sel.type === "line" ? S.draft.lines : S.draft.zones).splice(S.sel.i, 1);
  S.sel = null;
  updateEditUi();
  draw();
}

async function save() {
  const names = [...S.draft.lines, ...S.draft.zones].map((x) => x.name.trim());
  if (names.some((n) => !n)) { toast("Every line and zone needs a name.", true); return; }
  if (new Set(names).size !== names.length) { toast("Two shapes have the same name. Give each one its own name.", true); return; }
  const btn = $("btnSave");
  btn.disabled = true;
  try {
    const body = {
      lines: S.draft.lines.map((l) => ({ name: l.name.trim(), p1: l.p1, p2: l.p2, in_direction: l.in_direction })),
      zones: S.draft.zones.map((z) => ({ name: z.name.trim(), kind: z.kind, polygon: z.polygon })),
    };
    const result = await api("/api/geometry", { method: "PUT", body });
    S.geom = result;
    exitEdit();
    toast(result.saved_to ? `Saved to ${result.saved_to}. Counting uses the new lines now.` : "Saved. Counting uses the new lines now.");
    S.chartKey = "";
  } catch (e) {
    toast(`Not saved: ${e.message}`, true);
  } finally {
    btn.disabled = false;
  }
}

// ------------------------------------------------------------------ source and report dialogs

async function changeSource(body) {
  $("sourceError").textContent = "";
  try {
    await api("/api/source", { method: "POST", body });
    $("sourceDialog").close();
    toast("Source changed.");
  } catch (e) {
    $("sourceError").textContent = e.message;
  }
}

function uploadVideo() {
  const file = $("videoFile").files[0];
  if (!file) return;
  $("sourceError").textContent = "";
  const form = new FormData();
  form.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?realtime=${$("videoRealtime").checked}`);
  for (const [k, v] of Object.entries(HEADERS)) xhr.setRequestHeader(k, v);
  $("uploadProgress").hidden = false;
  $("btnUpload").disabled = true;
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) $("uploadBar").style.width = `${Math.round((ev.loaded / ev.total) * 100)}%`;
  };
  xhr.onload = () => {
    $("uploadProgress").hidden = true;
    $("btnUpload").disabled = false;
    if (xhr.status >= 200 && xhr.status < 300) {
      $("sourceDialog").close();
      $("videoFile").value = "";
      $("btnUpload").disabled = true;
      toast(`Counting ${file.name}`);
    } else {
      let msg = xhr.statusText;
      try { msg = JSON.parse(xhr.responseText).detail; } catch (_) { /* keep statusText */ }
      $("sourceError").textContent = msg;
    }
  };
  xhr.onerror = () => {
    $("uploadProgress").hidden = true;
    $("btnUpload").disabled = false;
    $("sourceError").textContent = "Upload failed. Is the app still running?";
  };
  xhr.send(form);
}

async function openReport() {
  $("exportMenu").hidden = true;
  $("btnExport").setAttribute("aria-expanded", "false");
  $("reportText").textContent = "Loading";
  $("reportDialog").showModal();
  try {
    const r = await api("/api/report");
    const head = r.scope.kind === "video" ? "Video report" : "Report for today";
    $("reportTitle").textContent = head;
    $("reportText").textContent = r.text;
  } catch (e) {
    $("reportText").textContent = `Could not load the report: ${e.message}`;
  }
}

// ------------------------------------------------------------------ wiring

function bindUi() {
  $("btnEdit").onclick = enterEdit;
  $("btnCancel").onclick = () => {
    if (isDirty() && !confirm("Throw away your changes?")) return;
    exitEdit();
  };
  $("btnSave").onclick = save;
  $("btnUndo").onclick = undo;
  $("btnRedo").onclick = redo;
  $("btnSnap").onclick = () => { S.snap = !S.snap; updateEditUi(); };
  for (const b of document.querySelectorAll("[data-tool]")) b.onclick = () => setTool(b.dataset.tool);
  $("btnFlip").onclick = flipSelected;
  $("btnDelete").onclick = deleteSelected;
  $("propName").addEventListener("focus", () => { $("propName").dataset.before = $("propName").value; });
  $("propName").addEventListener("input", () => {
    const item = selectedItem();
    if (!item) return;
    const name = $("propName").value;
    if ($("propName").dataset.before !== undefined) { delete $("propName").dataset.before; pushHistory(); }
    item.name = name;
    updateEditUi();
    draw();
  });
  $("propKind").onchange = () => {
    const item = selectedItem();
    if (!item) return;
    const kind = $("propKind").value;  // read first: pushHistory() redraws the form
    pushHistory();
    item.kind = kind;
    updateEditUi();
    draw();
  };

  $("btnSource").onclick = () => {
    const st = S.status;
    $("sourceError").textContent = "";
    if (st) $("configSourceText").textContent = `${st.camera.name} (camera id ${st.camera.id}).`;
    $("sourceDialog").showModal();
  };
  $("btnUseConfig").onclick = () => changeSource({ kind: "config" });
  $("btnUseWebcam").onclick = () => changeSource({ kind: "webcam", index: Number($("webcamIndex").value || 0) });
  $("videoFile").onchange = () => { $("btnUpload").disabled = !$("videoFile").files.length; };
  $("btnUpload").onclick = uploadVideo;

  $("btnExport").onclick = (ev) => {
    ev.stopPropagation();
    const menu = $("exportMenu");
    menu.hidden = !menu.hidden;
    $("btnExport").setAttribute("aria-expanded", String(!menu.hidden));
  };
  document.addEventListener("click", (ev) => {
    if (!$("exportMenu").hidden && !ev.target.closest(".menu-wrap")) {
      $("exportMenu").hidden = true;
      $("btnExport").setAttribute("aria-expanded", "false");
    }
  });
  for (const a of $("exportMenu").querySelectorAll("a")) a.addEventListener("click", () => { $("exportMenu").hidden = true; });
  $("btnReport").onclick = openReport;
  $("btnDoneReport").onclick = openReport;

  window.addEventListener("beforeunload", (ev) => { if (isDirty()) { ev.preventDefault(); ev.returnValue = ""; } });
  document.addEventListener("keydown", (ev) => {
    if (!S.editing) return;
    const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key.toLowerCase() === "z" && !typing) { ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
    if (mod && ev.key.toLowerCase() === "y" && !typing) { ev.preventDefault(); redo(); return; }
    if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
    if (typing || mod) return;
    const key = ev.key.toLowerCase();
    const handled = ["v", "l", "z", "f", "s", "escape", "delete", "backspace"].includes(key) || (key === "enter" && S.pending);
    if (handled) ev.preventDefault();  // e.g. Enter must not also "click" the focused tool button
    if (key === "v") setTool("select");
    else if (key === "l") setTool("line");
    else if (key === "z") setTool("zone");
    else if (key === "f") flipSelected();
    else if (key === "s") { S.snap = !S.snap; updateEditUi(); }
    else if (key === "enter" && S.pending) finishZone();
    else if (key === "escape") {
      if (S.pending) { S.pending = null; updateHint(); draw(); } else { S.sel = null; updateEditUi(); draw(); }
    } else if (key === "delete" || key === "backspace") deleteSelected();
  });
}

window.__countvision = S;  // for browser tests and debugging in the console
init();
