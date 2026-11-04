// usage_meter.js: the Usage Meter row in the dashboard's top bar.
// Loaded by one <script> tag at the end of dashboard.html and served by usage_meter.py.
// Shows how much of the subscription's 5-hour and 7-day windows is used, from the
// anthropic-ratelimit-unified-* headers of the latest reply. Those headers are
// undocumented, so every value is optional and anything missing shows "No data yet".
(() => {
  "use strict";

  const PREFIX = "anthropic-ratelimit-unified-";
  const POLL_MS = 2000;

  const css = `
.usage { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 18px; width: 100%; font: 12px var(--mono); color: var(--muted); }
.usage .u-title { font-weight: 700; color: var(--fg); }
.usage .u-win { display: inline-flex; align-items: center; gap: 6px; padding: 1px 8px; border: 1px solid transparent; border-radius: 6px; cursor: help; }
.usage .u-win.claim { border-color: var(--you); background: var(--you-bg); }
.usage .u-name { color: var(--fg); font-weight: 600; }
.usage .u-bar { display: inline-block; width: 160px; height: 8px; border-radius: 4px; background: var(--line); overflow: hidden; }
.usage .u-week .u-bar { width: 70px; height: 5px; }
.usage .u-bar > i { display: block; height: 100%; width: 0; background: var(--claude); }
.usage .warn .u-bar > i { background: var(--call); }
.usage .full .u-bar > i { background: var(--error); }
.usage .full .u-pct { color: var(--error); }
.usage .u-pct { color: var(--fg); }
.usage .u-binding { font-size: 10px; padding: 0 5px; border-radius: 4px; background: var(--you); color: #fff; }
.usage .u-limit { font-weight: 700; color: var(--error); padding: 1px 8px; border-radius: 6px; background: var(--error-bg); border: 1px solid var(--error); }
.usage .u-limit[hidden] { display: none; }
@media (prefers-color-scheme: dark) { .usage .u-binding { color: #111318; } }
`;
  document.head.append(Object.assign(document.createElement("style"), { textContent: css }));

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function windowView(label, cls) {
    const root = el("span", "u-win " + cls);
    const bar = el("span", "u-bar");
    const fill = el("i");
    bar.append(fill);
    const pct = el("span", "u-pct", "No data yet");
    const reset = el("span", "u-reset");
    const binding = el("span", "u-binding", "binding");
    binding.hidden = true;
    root.append(el("span", "u-name", label), bar, pct, reset, binding);
    return { root, fill, pct, reset, binding };
  }

  const row = el("div", "usage");
  row.id = "usage";
  const five = windowView("5h", "u-five");
  const week = windowView("7d", "u-week");
  const limit = el("span", "u-limit");
  limit.hidden = true;
  row.append(el("span", "u-title", "Usage"), five.root, week.root, limit);
  (document.querySelector(".bar") || document.body).append(row);

  let state = { headers: {}, updated: null };

  // A number, or null if the header is missing or not a number.
  function num(name) {
    const v = state.headers[PREFIX + name];
    if (v == null || String(v).trim() === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function fmtLeft(seconds) {
    if (seconds <= 0) return "now";
    const d = Math.floor(seconds / 86400), hh = Math.floor(seconds % 86400 / 3600),
      mm = Math.floor(seconds % 3600 / 60), ss = Math.floor(seconds % 60);
    const pad = n => String(n).padStart(2, "0");
    if (d) return `${d}d ${hh}h ${pad(mm)}m`;
    if (hh) return `${hh}h ${pad(mm)}m ${pad(ss)}s`;
    return `${mm}m ${pad(ss)}s`;
  }

  function fmtTime(unix) {
    const t = new Date(unix * 1000);
    return t.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
  }

  // Which window the representative claim names: "five_hour" -> 5h, "seven_day…" -> 7d.
  function claimed() {
    const c = String(state.headers[PREFIX + "representative-claim"] || "").toLowerCase();
    if (/five|5h|hour/.test(c)) return "5h";
    if (/seven|7d|day|week/.test(c)) return "7d";
    return null;
  }

  function renderWindow(view, key, now, long) {
    const util = num(`${key}-utilization`);
    const reset = num(`${key}-reset`);
    view.root.classList.remove("warn", "full");
    if (util == null) {
      view.fill.style.width = "0";
      view.pct.textContent = "No data yet";
    } else {
      const used = Math.max(0, Math.min(100, util * 100));
      view.fill.style.width = used.toFixed(1) + "%";
      const shown = used < 10 && used > 0 ? used.toFixed(1) : Math.round(used);
      const left = 100 - used;
      const leftShown = left < 10 && left > 0 ? left.toFixed(1) : Math.round(left);
      view.pct.textContent = long ? `${shown}% used · ${leftShown}% remaining` : `${shown}%`;
      if (used > 90) view.root.classList.add("full");
      else if (used >= 70) view.root.classList.add("warn");
    }
    if (reset == null) view.reset.textContent = util == null ? "" : "· reset: No data yet";
    else if (long) view.reset.textContent = `· resets in ${fmtLeft(reset - now)}`;
    else view.reset.textContent = `· resets ${fmtTime(reset)}`;
    view.binding.hidden = claimed() !== key;
    view.root.classList.toggle("claim", claimed() === key);
    return reset;
  }

  function render() {
    const now = Date.now() / 1000;
    renderWindow(five, "5h", now, true);
    renderWindow(week, "7d", now, false);

    const status = String(state.headers[PREFIX + "status"] || "").toLowerCase();
    if (status === "rejected") {
      // The window that blocks us: the claimed one, else the one with the later reset.
      const c = claimed();
      const r5 = num("5h-reset"), r7 = num("7d-reset");
      const at = num("reset") ?? (c === "7d" ? r7 : c === "5h" ? r5 : Math.max(r5 ?? 0, r7 ?? 0) || null);
      limit.textContent = at ? `Limit reached — resets in ${fmtLeft(at - now)}` : "Limit reached — reset time: No data yet";
      limit.hidden = false;
    } else {
      limit.hidden = true;
    }

    const lines = Object.entries(state.headers).map(([k, v]) => `${k.slice(PREFIX.length)}: ${v}`).sort();
    row.title = lines.length
      ? `From the latest reply${state.updated ? " at " + state.updated : ""}:\n${lines.join("\n")}`
      : "No rate-limit headers seen yet. They arrive with the first reply from the API.";
    five.root.title = week.root.title = row.title;
  }

  async function poll() {
    try {
      const data = await (await fetch("/__usage", { cache: "no-store" })).json();
      if (data && typeof data.headers === "object" && data.headers) state = data;
    } catch { /* proxy not reachable: keep the last values, the main page shows the status */ }
    render();
  }

  render();
  poll();
  setInterval(poll, POLL_MS);
  setInterval(render, 1000); // the countdown
})();
