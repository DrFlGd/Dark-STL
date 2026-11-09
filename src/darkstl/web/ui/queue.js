// The Queue (queue): the bulk actions asked for (Collect into the app, Export as GLB, Export packages, Enhance with
// the current settings), run one after another; the one running shows its progress, and each has a Stop (one
// waiting is taken off the queue). What each did is kept for the last 30, with the models that couldn't be done
// and why.
import { html, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { Icon } from "./icons.js";
import { api, plural, toast, showInFolder } from "./api.js";
import { CommandBar } from "./layout.js";
import { watchQueue } from "./organise.js";

const STATES = { waiting: "Waiting", running: "Running", done: "Done", failed: "Didn't work", stopped: "Stopped" };

function outcome(it) {
  const r = it.result;
  if (it.state === "failed") return it.error || "It didn't work.";
  if (it.state === "stopped") return it.error || "Stopped: what was done is kept.";
  if (it.state !== "done" || !r) return it.message || "";
  const n = r.done ?? r.count ?? it.count;
  const what = { export_glb: "exported", enhance: "enhanced and saved as variations", collect: "collected", packages: "written as packages" }[it.action];
  return `${plural(n, "model")} ${what}${r.failed?.length ? `; ${r.failed.length} couldn't be` : ""}`;
}

function Item({ it }) {
  const live = it.state === "waiting" || it.state === "running";
  const stop = async () => {
    try { ui.set({ queue: await api("queue_stop", { id: it.id }) }); } catch (e) { toast(String(e.message || e)); }
  };
  const failed = it.result?.failed || [];
  return html`<li class=${`queue-item queue-${it.state}`} data-item=${it.id} data-state=${it.state}>
    <div class="queue-line">
      <span class="queue-what"><b>${it.label}</b><small class="muted">${STATES[it.state]}${it.state === "running" && it.progress > 0 ? ` · ${Math.round(it.progress * 100)}%` : ""}${it.stopping ? " · stopping at its next step" : ""}</small></span>
      <span class="queue-actions">
        ${it.state === "done" && it.result?.folder ? html`<button type="button" class="ghost compact" title="Show the folder it wrote to" onClick=${() => showInFolder(it.result.folder)}>${Icon.folder(13)} Show folder</button>` : null}
        ${live ? html`<button type="button" class="ghost compact queue-stop" disabled=${it.stopping} title=${it.state === "waiting" ? "Take it off the queue" : "Stop it at its next step (what's done is kept)"}
          onClick=${stop}>${Icon.close(12)} ${it.state === "waiting" ? "Take off" : "Stop"}</button>` : null}</span>
    </div>
    ${it.state === "running" ? html`<div class="queue-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(it.progress * 100)}><span style=${`width: ${Math.max(2, it.progress * 100)}%`}></span></div>` : null}
    ${outcome(it) ? html`<p class=${`queue-note${it.state === "failed" ? " warn-text" : " muted"}`}>${outcome(it)}</p>` : null}
    ${failed.length ? html`<details class="queue-failed"><summary>${plural(failed.length, "model")} couldn't be done</summary>
      <ul>${failed.map((f, i) => html`<li key=${i}><b>${f.label}</b>${f.error ? html`: <span class="muted">${f.error}</span>` : null}</li>`)}</ul></details>` : null}
  </li>`;
}

export function QueuePage() {
  const q = useStore(ui, (s) => s.queue);
  useEffect(() => {
    let live = true, timer = null;
    const poll = async () => {
      try { const r = await api("queue"); if (live) ui.set({ queue: r }); if (r.active) watchQueue(); } catch { /* shown as it was */ }
      if (live) timer = setTimeout(poll, 1000);
    };
    poll();
    return () => { live = false; clearTimeout(timer); };
  }, []);
  const items = [...(q?.items || [])].reverse(); // newest first
  const finished = items.filter((i) => !["waiting", "running"].includes(i.state)).length;
  const clear = async () => { try { ui.set({ queue: await api("queue_clear") }); } catch (e) { toast(String(e.message || e)); } };
  return html`<${CommandBar} id="queue-head" title=${html`<h1 class="crumbs">${Icon.queue(18)}<span class="crumbs-name">Queue</span></h1>`}
      count=${html`<span class="page-count" id="queue-count">${q ? (q.active ? `${q.active} to do` : "Nothing running") : ""}</span>`}
      primary=${finished ? html`<button type="button" class="ghost" id="queue-clear" title="Take the finished ones off the list" onClick=${clear}>Clear finished</button>` : null} />
    <div class="pages"><div class="queue-page">
      <p class="muted">What you asked to be done to several models at once runs here, one job after another, while you go on working. Choose models in a game,
        a search or a collection (Ctrl+click, Shift+click or Ctrl+A), then right-click them: <i>Collect into the app</i>, <i>Export as GLB</i>, <i>Export packages</i>
        or <i>Enhance with the current settings</i>.</p>
      ${items.length ? html`<ul class="queue-list" id="queue-list">${items.map((it) => html`<${Item} key=${it.id} it=${it} />`)}</ul>`
        : html`<div class="empty" id="queue-empty">${q ? "Nothing queued." : "…"}</div>`}
    </div></div>`;
}
