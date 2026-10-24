// The parts around the main view: the menu's head and theme button, and the
// status bar (from Model Library).
import { html, useEffect, useState } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref, cycleTheme, resolvedTheme, nextTheme, THEMES } from "./state.js";
import { Icon } from "./icons.js";
import { plural, stopJob } from "./api.js";

const themeName = (t) => (t === "system" ? "System" : THEMES.find(([v]) => v === t)?.[1].replace(/ \(.*\)$/, "") || t);

export function useMedia(query) {
  const [on, setOn] = useState(() => matchMedia(query).matches);
  useEffect(() => {
    const q = matchMedia(query);
    const changed = () => setOn(q.matches);
    q.addEventListener("change", changed);
    return () => q.removeEventListener("change", changed);
  }, [query]);
  return on;
}

// Beside a model in a window under 1150 px the menu folds to its icons to give
// the 3D view room; ☰ unfolds it there until it's folded again.
const MEDIUM = "(min-width: 901px) and (max-width: 1149px)";
const navSel = (s) => ({ open: s.navOpen, collapsed: s.navCollapsed, peek: s.navPeek, model: s.route.startsWith("model:") });
export function useNavFolded() {
  const n = useStore(ui, navSel);
  const auto = useMedia(MEDIUM) && n.model;
  return { folded: n.collapsed || (auto && !n.peek), auto };
}

/** The app's mark, drawn for each skin. */
export function BrandMark() {
  return html`<svg class="brand-mark icon" viewBox="0 0 40 40" aria-hidden="true">
    <g data-sk="classic" fill="none" stroke="currentColor" stroke-width="2"><path d="M29 5l6 6-16 16-6-6z" /><path d="M11 21l8 8" class="accent" /><path d="M13 27l-6 6M9 25l-4 4" /></g>
    <g data-sk="drafting" fill="none"><rect x="2.5" y="2.5" width="35" height="35" stroke="var(--ink)" stroke-width="1.5"/><path d="M28 7l5 5-14 14-5-5z" stroke="var(--tool)" stroke-width="1.6"/><path d="M12 21l7 7M14 26l-6 6" stroke="var(--tool)" stroke-width="1.6"/><path d="M7 34h26M7 32.5v3M33 32.5v3" stroke="var(--ink)" stroke-width="1"/></g>
    <g data-sk="partsbin" stroke="none"><rect x="3" y="5" width="34" height="30" rx="2" fill="var(--ink)"/><path d="M27 9l4 4-12 12-4-4z" fill="var(--tool)"/><path d="M12 22l6 6-1.6 1.6-6-6z" fill="var(--white)"/><path d="M10.8 26.4l2.8 2.8-3.4 3.4-2.8-2.8z" fill="var(--white)"/></g>
    <g data-sk="layerline" stroke="none"><path d="M27 4l5 5-13 13-5-5z" fill="var(--tool)"/><rect x="6" y="24" width="28" height="4.5" rx="2.25" fill="var(--ink)"/><rect x="6" y="30" width="28" height="4.5" rx="2.25" fill="var(--ink)"/><path d="M11 18l7 7-2 2-7-7z" fill="var(--ink)"/></g>
  </svg>`;
}

/** The menu's head: its fold button (the drawer's close button in a narrow
 *  window) and the app's mark, a link to Home. */
export function NavHead() {
  const nav = useStore(ui, (s) => ({ open: s.navOpen }));
  const { folded, auto } = useNavFolded();
  const toggleNav = () => {
    if (matchMedia("(max-width: 900px)").matches) ui.set({ navOpen: !ui.get().navOpen });
    else if (folded) { if (ui.get().navCollapsed) setPref({ navCollapsed: false }); if (auto) ui.set({ navPeek: true }); }
    else if (auto) ui.set({ navPeek: false });
    else setPref({ navCollapsed: true });
  };
  return html`<div class="nav-head-row">
    <button type="button" class="ghost topicon nav-burger" aria-label=${folded ? "Expand navigation" : "Collapse navigation"} aria-expanded=${nav.open ? "true" : "false"} title="Expand or collapse navigation" onClick=${toggleNav}>${Icon.menu(18)}</button>
    <a class="brand" href="#/" title="Home"><${BrandMark} /><span class="nav-label">Dark STL</span></a>
  </div>`;
}

/** The theme button at the foot of the menu: the same four choices as Settings, in turn. */
export function ThemeButton() {
  const theme = useStore(ui, (s) => s.theme);
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((n) => n + 1); window.addEventListener("ds-theme", f); return () => window.removeEventListener("ds-theme", f); }, []);
  const cur = resolvedTheme();
  const next = nextTheme(theme);
  const icon = theme === "system" ? Icon.auto(15) : cur === "light" ? Icon.sun(15) : cur === "dark" ? Icon.moon(15) : Icon.night(15);
  const now = theme === "system" ? `Follow the system (${cur})` : themeName(theme);
  return html`<li class="nav-row"><span class="nav-toggle-space"></span>
    <button type="button" class="nav-link nav-theme" id="theme-toggle" data-theme=${theme} aria-label=${`Theme: ${now}. Switch to ${themeName(next)}`} title=${`Theme: ${now}. Click for ${themeName(next)}.`}
      onClick=${cycleTheme}><span class="nav-icon">${icon}</span><span class="nav-label">Theme: ${themeName(theme)}</span></button></li>`;
}

function Elapsed({ since }) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  return html`${Math.round((Date.now() - since) / 1000)} s`;
}

/** A job's line: what it is, how far along, for how long. */
function jobText(j) {
  if (j.stopping) return html`Stopping ${j.label.charAt(0).toLowerCase() + j.label.slice(1)}…`;
  return html`${j.label}${j.progress > 0 && j.progress < 1 ? ` ${Math.round(j.progress * 100)}%` : "…"} <${Elapsed} since=${j.started} />`;
}

// The status bar's right end shows the work going on. Clicking it stops that work (when it can be
// stopped: generating, exporting, scanning, collecting); with more than one thing running, "N running"
// lists them all, each with its own Stop.
export function StatusBar() {
  const s = useStore(ui, (st) => ({ jobs: st.jobs, ready: st.ready, overview: st.overview }));
  const [open, setOpen] = useState(false);
  if (!s.ready) return null;
  const latest = s.jobs[s.jobs.length - 1];
  const ov = s.overview;
  const games = ov?.games?.length || 0;
  const canStop = latest?.stoppable && !latest.stopping;
  return html`<div class="statusbar-inner">
    <span class="status-library" id="status-library">${ov ? (games ? `${plural(games, "game")} · ${plural(ov.total, "model")}` : "No game folders yet") : "Reading the library…"}</span>
    <span class="status-jobs">
      ${latest ? html`<button type="button" class=${`status-job${canStop ? " can-stop" : ""}`} id="status-job" data-job=${latest.id}
          aria-expanded=${canStop ? null : open ? "true" : "false"}
          title=${canStop ? `Click to stop: ${latest.label}` : latest.stopping ? "Stopping at its next step" : "Show what's running"}
          onClick=${() => (canStop ? stopJob(latest.id) : setOpen(!open))}>
        <span class="dot busy"></span><span class="status-job-text">${jobText(latest)}</span>${canStop ? html`<span class="status-stop" aria-hidden="true">${Icon.close(12)} Stop</span>` : null}</button>
        ${s.jobs.length > 1 ? html`<button type="button" class="status-job status-more" id="status-more" aria-expanded=${open ? "true" : "false"} title="Show what's running" onClick=${() => setOpen(!open)}>· ${s.jobs.length} running</button>` : null}`
        : ov?.scanning ? html`<span class="status-idle"><span class="dot busy"></span>Reading game folders…</span>`
        : html`<span class="status-idle"><span class="dot"></span>Ready</span>`}
      ${open && s.jobs.length ? html`<div class="jobs-panel" role="dialog" aria-label="Background work">
        ${s.jobs.map((j) => html`<div class="job" key=${j.id} data-job=${j.id}><span>${j.stopping ? `Stopping ${j.label}…` : j.label}${j.message ? html`<br /><small class="muted">${j.message}</small>` : null}</span><span class="muted"><${Elapsed} since=${j.started} /></span>
          ${j.stoppable && !j.stopping ? html`<button type="button" class="ghost compact job-stop" title=${`Stop: ${j.label}`} onClick=${() => stopJob(j.id)}>${Icon.close(12)} Stop</button>` : null}</div>`)}
      </div>` : null}
    </span>
  </div>`;
}
