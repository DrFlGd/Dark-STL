// Library II on the page: collections, tags and stars (one file for the whole library, every game:
// darkstl/library_collections.py), what can be done to several models at once (the queue runs it a job at a
// time: darkstl/app/bulk.py), dragging models onto a collection, and the models that go with one (related).
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash } from "./context.js";
import { Icon } from "./icons.js";
import { api, followJob, loadOverview, toast, plural, showInFolder } from "./api.js";
import { openMenu } from "./actions.js";
import { Thumb } from "./thumbs.js";
import { GameBadge } from "./gameicons.js";
import { Dialog, close } from "./dialogs.js";
import { combineModels } from "./games.js";

const msg = (e) => String(e?.message || e);
const named = (models) => (models.length === 1 ? models[0].label : plural(models.length, "model"));

/** Read the collections, tags and stars again (the menu, Home and the search's filters show them). */
export async function loadCollections() {
  try { ui.set({ collections: await api("collections") }); } catch { /* listed as none */ }
}

/** After a change to the marks: the lists are read again (the stars on the cards, a collection's models). */
async function marked() {
  ui.set((s) => ({ libRev: (s.libRev || 0) + 1 }));
  await loadCollections();
}

export async function starModels(models, on) {
  try {
    await api("models_star", { ids: models.map((m) => m.id), star: on });
    await marked();
    toast(on ? `Starred ${named(models)}` : `Took the star off ${named(models)}`, { ms: 3500, action: on ? { label: "Show starred", run: () => { location.hash = "#/starred"; } } : null });
  } catch (e) { toast(msg(e)); }
}

export const addToCollection = (models) => ui.set({ dialog: { type: "collection-add", models } });
export const tagModels = (models) => ui.set({ dialog: { type: "tag", models } });
export const newCollection = (models = []) => ui.set({ dialog: { type: "collection-name", models } });
export const renameCollection = (collection) => ui.set({ dialog: { type: "collection-name", collection } });

export function deleteCollection(c, after) {
  ui.set({ dialog: { type: "confirm", title: `Delete ${c.name}?`, button: "Delete",
    text: `The collection goes; its ${plural(c.count, "model")} stay in the library, with their tags and stars.`,
    run: async () => { await api("collection_delete", { id: c.id }); await marked(); after?.(); toast(`Deleted ${c.name}`); } } });
}

/** Put models in a collection (dropped onto it in the menu, or chosen in Add to collection…). */
export async function addIdsTo(c, ids) {
  try {
    const r = await api("collection_add", { id: c.id, ids });
    await marked();
    const already = ids.length - r.added;
    toast(r.added ? `Added ${plural(r.added, "model")} to ${c.name}${already ? ` (${already} ${already === 1 ? "was" : "were"} in it already)` : ""}`
      : `${ids.length === 1 ? "It's" : "They're"} in ${c.name} already`, { ms: 4500, action: { label: "Show it", run: () => { location.hash = routeHash(`collection:${c.id}`); } } });
  } catch (e) { toast(msg(e)); }
}

export async function removeFrom(c, models) {
  try {
    await api("collection_remove", { id: c.id, ids: models.map((m) => m.id) });
    await marked();
    toast(`Took ${named(models)} out of ${c.name}`);
  } catch (e) { toast(msg(e)); }
}

/** A collection's menu (right-click in the menu, ⋯ on its page). */
export function collectionMenu(c, after) {
  return [
    { id: "collection-open", label: "Open", icon: "eye", run: () => { location.hash = routeHash(`collection:${c.id}`); } },
    { id: "collection-rename", label: "Rename…", icon: "edit", run: () => renameCollection(c) },
    { sep: true },
    { id: "collection-delete", label: "Delete…", sub: "its models stay in the library", icon: "trash", danger: true, run: () => deleteCollection(c, after) },
  ];
}

/** Add to collection…, Tag… and Star for one model's menus. */
export function markItems(m) {
  return [
    { id: "add-collection", label: "Add to collection…", sub: "a list of your own, across games", icon: "stack", run: () => addToCollection([m]) },
    { id: "tag", label: "Tag…", sub: "words of your own to find it by", icon: "tag", run: () => tagModels([m]) },
    { id: "star", label: m.starred ? "Take the star off" : "Star", sub: m.starred ? null : "to come back to (Starred, in the menu)", icon: "star", filled: !m.starred,
      run: () => starModels([m], !m.starred) },
  ];
}

// ------------------------------------------------------------------ several models at once

/** What can be done to several models at once: the bulk actions (queued, a job at a time) and the marks. `extra`:
 *  the page's own items (Remove from this collection). */
export function bulkItems(models, extra = []) {
  const all = models.every((m) => m.starred);
  const configs = models.filter((m) => m.configurable).length;
  const kept = models.every((m) => m.collected);
  return [
    { id: "bulk-collect", label: "Collect into the app", sub: "copy their files into Dark STL's own folder, a game at a time", icon: "inbox",
      disabled: kept ? "These are already the copies Dark STL keeps." : false, run: () => queueAction("collect", models) },
    { id: "bulk-glb", label: "Export as GLB", sub: "each with its textures, into the export folder (Settings)", icon: "download", run: () => queueAction("export_glb", models) },
    { id: "bulk-packages", label: "Export packages", sub: "write each as a model package in the app's library, to open without the game", icon: "box",
      run: () => queueAction("packages", models) },
    { id: "bulk-enhance", label: "Enhance with the current settings", sub: "the settings you last generated each game's models with; each kept as a saved variation", icon: "sparkle",
      disabled: configs === models.length ? "Configurable models are enhanced one at a time, from their own page." : false, run: () => queueAction("enhance", models) },
    { sep: true },
    { id: "bulk-collection", label: "Add to collection…", icon: "stack", run: () => addToCollection(models) },
    { id: "bulk-tag", label: "Tag…", icon: "tag", run: () => tagModels(models) },
    { id: "bulk-star", label: all ? "Take the star off" : "Star", icon: "star", filled: !all, run: () => starModels(models, !all) },
    ...extra,
  ];
}

/** The details panel while several models are chosen: how many, from which games, and what can be done to them. */
export function SelectionSummary({ models, extra = [] }) {
  const games = [...new Set(models.map((m) => m.game))];
  const items = bulkItems(models, extra).filter((it) => !it.sep);
  return html`<div class="place-summary selection-summary" id="selection-summary">
    <section class="insp-section"><h3>${plural(models.length, "model")} chosen</h3>
      <p class="muted">Shift+click chooses a run of models, Ctrl+click adds or takes one off, Ctrl+A chooses them all; Esc lets them go.</p>
      <p class="selection-games">${games.map((g) => html`<span key=${g} class="card-location"><${GameBadge} name=${g} size=${13} /> ${g}</span>`)}</p>
      <div class="insp-actions insp-actions-col">${items.map((it) => html`<button type="button" key=${it.id} class="ghost" id=${`sel-${it.id}`} disabled=${!!it.disabled}
        title=${typeof it.disabled === "string" ? it.disabled : it.sub || it.label} onClick=${() => it.run()}>${it.icon && Icon[it.icon] ? Icon[it.icon](14, it.filled) : null} ${it.label}</button>`)}</div>
    </section>
    <section class="insp-section"><h3>Chosen</h3><ul class="ls-list selection-list">${models.slice(0, 40).map((m) => html`<li key=${m.id}><span>${m.label}</span><span class="muted">${m.game}</span></li>`)}
      ${models.length > 40 ? html`<li class="muted">and ${(models.length - 40).toLocaleString()} more</li>` : null}</ul></section>
  </div>`;
}

// ------------------------------------------------------------------ the queue

/** Queue a bulk action for these models; the queue runs one job after another (the Queue page lists them). */
export async function queueAction(action, models, extra = {}) {
  try {
    const r = await api("queue_add", { action, ids: models.map((m) => m.id), ...extra });
    const added = r.items.filter((i) => r.added.includes(i.id));
    ui.set({ queue: r });
    for (const it of added) asked.add(it.id);
    toast(added.length === 1 ? `Queued: ${added[0].label}` : `Queued ${added.length} jobs: one for each game`,
      { ms: 5000, action: { label: "Show the queue", run: () => { location.hash = "#/queue"; } } });
    watchQueue();
  } catch (e) { toast(msg(e)); }
}

let watching = false;
const followed = new Set(); // the queue's jobs followed in the status bar
const asked = new Set(); // the items queued from this window, whose outcome it says when they end

/** Follow the queue while it runs: each job in the status bar (with its Stop), and what it did when it ends. */
export async function watchQueue() {
  if (watching) return;
  watching = true;
  try {
    for (;;) {
      let q;
      try { q = await api("queue"); } catch { break; }
      ui.set({ queue: q });
      for (const it of q.items) {
        if (it.state === "running" && it.job && !followed.has(it.job)) {
          followed.add(it.job);
          asked.delete(it.id);
          followJob(it.job, it.label).then((r) => finished(it, r), (e) => finished(it, null, e));
        } else if (asked.has(it.id) && !["waiting", "running"].includes(it.state)) { // over between two looks
          asked.delete(it.id);
          finished(it, it.result, it.state === "done" ? null : Object.assign(new Error(it.error || "It didn't work."), { stopped: it.state === "stopped" }));
        }
      }
      if (!q.active) break;
      await new Promise((r) => setTimeout(r, 700));
    }
  } finally { watching = false; }
}

async function finished(it, r, error) {
  try { ui.set({ queue: await api("queue") }); } catch { /* the page reads it again */ }
  if (error) { toast(error.stopped ? `Stopped: ${it.label}` : `${it.label}: ${msg(error)}`, 6000); return; }
  const failed = r?.failed?.length || 0;
  const but = failed ? ` (${plural(failed, "model")} couldn't be: the Queue page says why)` : "";
  if (it.action === "export_glb") {
    toast(`Exported ${plural(r.done, "model")} as GLB${but}`, { ms: 7000, action: r.folder ? { label: "Show folder", run: () => showInFolder(r.folder) } : null });
  } else if (it.action === "enhance") {
    toast(`Enhanced ${plural(r.done, "model")}${but}. Each is kept as a saved variation on its page.`, 7000);
  } else {
    await loadOverview();
    ui.set((s) => ({ libRev: (s.libRev || 0) + 1 }));
    toast(`${it.action === "packages" ? "Wrote packages of" : "Collected"} ${plural(r?.count ?? it.count, "model")}${but}`, 6000);
  }
}

// ------------------------------------------------------------------ dragging models onto a collection

export const DRAG_TYPE = "application/x-darkstl-models";

export function dragModels(e, ids) {
  e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(ids));
  e.dataTransfer.setData("text/plain", `${ids.length} model${ids.length === 1 ? "" : "s"}`);
  e.dataTransfer.effectAllowed = "copyMove";
}

export const dragging = (e) => [...(e.dataTransfer?.types || [])].includes(DRAG_TYPE);

export function draggedIds(e) {
  try { return JSON.parse(e.dataTransfer.getData(DRAG_TYPE) || "[]"); } catch { return []; }
}

/** Props that make an element a place to drop models on: `onIds(ids)` gets them. Its `over` is true while models
 *  are held over it. */
export function useDrop(onIds) {
  const [over, setOver] = useState(false);
  return {
    over,
    props: {
      onDragOver: (e) => { if (!dragging(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; if (!over) setOver(true); },
      onDragLeave: () => setOver(false),
      onDrop: (e) => { if (!dragging(e)) return; e.preventDefault(); setOver(false); const ids = draggedIds(e); if (ids.length) onIds(ids); },
    },
  };
}

// ------------------------------------------------------------------ related models

/** A menu of the models that go with `m` (a card's "N related" link). */
export async function relatedMenu(at, m) {
  try {
    const r = await api("model_related", { id: m.id });
    openMenu(at, r.items.length ? [{ head: `Goes with ${m.label}` }, ...r.items.map((x) => ({ id: `related-${x.id}`, label: x.label, sub: x.relation, icon: "link",
      run: () => { location.hash = routeHash(`model:${x.id}`); } }))] : [{ id: "related-none", label: "Nothing goes with it", disabled: true, run() {} }]);
  } catch (e) { toast(msg(e)); }
}

/** The models that go with one (the rest of its armour set, its low-detail copy, its variants, the
 *  configurable models it's part of), each a link to it; Combine starts with them. */
export function Related({ m, id = "related" }) {
  const [items, setItems] = useState(null);
  const libRev = useStore(ui, (s) => s.libRev);
  useEffect(() => {
    let live = true;
    setItems(null);
    api("model_related", { id: m.id }).then((r) => { if (live) setItems(r.items); }, () => { if (live) setItems([]); });
    return () => { live = false; };
  }, [m.id, libRev]);
  if (!items?.length) return null;
  return html`<section class="insp-section related" id=${id}><h3>Related</h3>
    <ul class="related-list">${items.map((x) => html`<li key=${x.id}><a href=${routeHash(`model:${x.id}`)} class="related-item" data-model=${x.id} title=${`${x.label}: ${x.relation}`}>
      <${Thumb} id=${x.id} category=${x.category} cls="thumb-sm" /><span class="related-text"><span class="related-name">${x.label}</span><small class="muted">${x.relation}</small></span></a></li>`)}</ul>
    ${m.configurable ? null : html`<div class="insp-actions"><button type="button" class="ghost" id=${`${id}-combine`} title="Make one model of this one and those that go with it (face, body and hair, say)"
      onClick=${() => combineModels(m)}>${Icon.box(14)} Combine with them…</button></div>`}
  </section>`;
}

// ------------------------------------------------------------------ dialogs

/** Add to collection…: pick one (it's added at once), or name a new one. */
export function CollectionAdd({ models }) {
  const cols = useStore(ui, (s) => s.collections?.collections || []);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ids = models.map((m) => m.id);
  const pick = async (c) => { setBusy(true); close(); await addIdsTo(c, ids); };
  const create = async () => {
    if (!name.trim()) { setError("Name the new collection, or click one above."); return; }
    setBusy(true); setError(null);
    try {
      const r = await api("collection_create", { name: name.trim(), ids });
      close();
      await marked();
      toast(`Made ${r.collection.name} with ${named(models)}`, { ms: 4500, action: { label: "Show it", run: () => { location.hash = routeHash(`collection:${r.collection.id}`); } } });
    } catch (e) { setBusy(false); setError(msg(e)); }
  };
  return html`<${Dialog} title=${`Add ${named(models)} to a collection`} id="collection-add-dialog" onSubmit=${create} busy=${busy} error=${error} submitLabel="Make the new collection">
    <p class="muted">Collections are lists of your own, across every game ("To print", "Bosses"). Models stay where they are in the library; a collection only lists them.</p>
    ${cols.length ? html`<ul class="picker-list collection-picks" id="collection-picks" aria-label="Collections">${cols.map((c) => html`<li key=${c.id}>
      <button type="button" class="picker-item" data-collection=${c.id} disabled=${busy} title=${`Add to ${c.name}`} onClick=${() => pick(c)}>
        ${Icon.stack(14)}<span>${c.name}<small class="muted">${plural(c.count, "model")}</small></span></button></li>`)}</ul>` : null}
    <label class="field-block"><span>${cols.length ? "Or a new collection" : "A new collection"}</span>
      <input id="collection-new-name" value=${name} disabled=${busy} placeholder="To print, Bosses, Elden Ring armour…" maxlength="80" onInput=${(e) => setName(e.target.value)} /></label>
  <//>`;
}

/** A new collection, or a collection renamed. */
export function CollectionName({ collection, models = [] }) {
  const [name, setName] = useState(collection?.name || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const save = async () => {
    setBusy(true); setError(null);
    try {
      if (collection) await api("collection_rename", { id: collection.id, name });
      else {
        const r = await api("collection_create", { name, ids: models.map((m) => m.id) });
        location.hash = routeHash(`collection:${r.collection.id}`);
      }
      close();
      await marked();
    } catch (e) { setBusy(false); setError(msg(e)); }
  };
  return html`<${Dialog} title=${collection ? `Rename ${collection.name}` : "New collection"} id="collection-name-dialog" onSubmit=${save} busy=${busy} error=${error}
      submitLabel=${collection ? "Rename" : "Make it"}>
    ${collection ? null : html`<p class="muted">A list of your own, across every game. Drag models onto it in the menu, or use Add to collection… in a model's right-click menu.</p>`}
    <label class="field-block"><span>Name</span><input id="collection-name" value=${name} disabled=${busy} maxlength="80" placeholder="To print" onInput=${(e) => setName(e.target.value)} /></label>
  <//>`;
}

/** Tag…: the tags these models have (× takes one off them all), and new ones to put on them. */
export function TagDialog({ models }) {
  const known = useStore(ui, (s) => s.collections?.tags || []);
  const have = new Map();
  for (const m of models) for (const t of m.tags || []) have.set(t.toLowerCase(), t);
  const [drop, setDrop] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const typed = text.split(",").map((t) => t.trim()).filter(Boolean);
  const offer = known.filter((t) => !have.has(t.name.toLowerCase()) && !typed.some((x) => x.toLowerCase() === t.name.toLowerCase())).slice(0, 16);
  const save = async () => {
    setBusy(true); setError(null);
    try {
      await api("models_tag", { ids: models.map((m) => m.id), add: typed, remove: drop });
      close();
      await marked();
      if (typed.length) toast(`Tagged ${named(models)} ${typed.join(", ")}`);
    } catch (e) { setBusy(false); setError(msg(e)); }
  };
  return html`<${Dialog} title=${`Tag ${named(models)}`} id="tag-dialog" onSubmit=${save} busy=${busy} error=${error} submitLabel="Save">
    <p class="muted">Words of your own ("cloth", "needs supports") to find models by: the search finds them like names, and they're offered as filters when searching every game.</p>
    ${have.size ? html`<div class="filters tag-chips" id="tag-have" aria-label="Their tags">${[...have.values()].map((t) => {
      const off = drop.includes(t);
      return html`<button type="button" key=${t} class=${`chip-btn tag-chip${off ? " off" : ""}`} data-tag=${t} disabled=${busy}
        title=${off ? `Keep ${t}` : `Take ${t} off ${models.length === 1 ? "it" : "them all"}`} onClick=${() => setDrop(off ? drop.filter((x) => x !== t) : [...drop, t])}>
        ${off ? html`<s>${t}</s>` : t} ${off ? "↺" : "×"}</button>`;
    })}</div>` : null}
    <label class="field-block"><span>Add tags</span>
      <input id="tag-input" value=${text} disabled=${busy} placeholder="cloth, needs supports" list="tag-known" onInput=${(e) => setText(e.target.value)} />
      <datalist id="tag-known">${known.map((t) => html`<option key=${t.name} value=${t.name} />`)}</datalist>
      <small class="muted">Separate them with commas.</small></label>
    ${offer.length ? html`<div class="filters tag-chips" id="tag-offer" aria-label="Tags in use">${offer.map((t) => html`<button type="button" key=${t.name} class="chip-btn" data-tag=${t.name}
      disabled=${busy} title=${`Add ${t.name} (on ${plural(t.count, "model")})`} onClick=${() => setText(typed.length ? `${typed.join(", ")}, ${t.name}` : t.name)}>+ ${t.name}</button>`)}</div>` : null}
  <//>`;
}

// ------------------------------------------------------------------ the menu's collections

function CollectionLink({ c, current }) {
  const route = `collection:${c.id}`;
  const drop = useDrop((ids) => addIdsTo(c, ids));
  return html`<li class="nav-row"><span class="nav-toggle-space"></span>
    <a class=${`nav-link${current === route ? " active" : ""}${drop.over ? " drop-over" : ""}`} href=${routeHash(route)} data-collection=${c.id} title=${`${c.name}: drop models here to add them`}
      aria-current=${current === route ? "page" : null} onClick=${() => ui.set({ navOpen: false })} onContextMenu=${(e) => openMenu(e, collectionMenu(c))} ...${drop.props}>
      <span class="nav-icon">${Icon.stack(15)}</span><span class="nav-label">${c.name}</span><span class="nav-count">${c.count.toLocaleString()}</span></a></li>`;
}

function StarredLink({ current, count }) {
  const drop = useDrop((ids) => starModels(ids.map((id) => ({ id, label: "it" })), true));
  return html`<li class="nav-row"><span class="nav-toggle-space"></span>
    <a class=${`nav-link${current === "starred" ? " active" : ""}${drop.over ? " drop-over" : ""}`} href="#/starred" id="nav-starred" title="Starred: the models you starred, in every game (drop models here to star them)"
      aria-current=${current === "starred" ? "page" : null} onClick=${() => ui.set({ navOpen: false })} ...${drop.props}>
      <span class="nav-icon">${Icon.star(15)}</span><span class="nav-label">Starred</span>${count ? html`<span class="nav-count">${count.toLocaleString()}</span>` : null}</a></li>`;
}

/** The menu's Collections: Starred, each collection (drop models on one to add them), New collection…. */
export function CollectionsNav({ current }) {
  const c = useStore(ui, (s) => s.collections);
  return html`<p class="nav-head">Collections</p>
    <ul class="nav-list" id="collection-list">
      <${StarredLink} current=${current} count=${c?.starred || 0} />
      ${(c?.collections || []).map((col) => html`<${CollectionLink} key=${col.id} c=${col} current=${current} />`)}
    </ul>
    <button type="button" class="ghost nav-add" id="new-collection" title="A list of your own, across every game" onClick=${() => { ui.set({ navOpen: false }); newCollection(); }}>${Icon.plus(14)} New collection…</button>`;
}
