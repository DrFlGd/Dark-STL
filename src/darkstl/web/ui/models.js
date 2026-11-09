// A library model as a card or a list row, shared by a game's pages and the
// search across every game: its preview, its name (the one it has in the game
// first, then its file name, muted), and once known its size and triangles.
// The list view has columns that sort when their header is clicked; the sort
// chosen is kept for each page.
import { html, useState, useRef, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { routeHash } from "./context.js";
import { plural, showInFolder } from "./api.js";
import { openMenu } from "./actions.js";
import { Icon } from "./icons.js";
import { Thumb } from "./thumbs.js";
import { GameBadge } from "./gameicons.js";
import { collectModelItem, modelFilesItem, redrawModelItem, arrangeModel, combineModels } from "./games.js";
import { markItems, bulkItems, dragModels, relatedMenu } from "./organise.js";

export const openModel = (m) => { location.hash = routeHash(`model:${m.id}`); };

export function modelMenu(m) {
  return [
    { id: "open", label: "Open", icon: "eye", key: "Enter", run: () => openModel(m) },
    ...(m.configurable ? [] : [{ id: "arrange", label: "Rename or move…", sub: "a name or a folder of your own", icon: "edit", run: () => arrangeModel(m) },
      { id: "combine", label: "Combine with other models…", sub: "face, body and hair as one model", icon: "box", run: () => combineModels(m) }]),
    ...markItems(m),
    { sep: true },
    { id: "show", label: "Show in folder", icon: "folder", run: () => showInFolder(m.file) },
    { id: "copy-path", label: "Copy file path", icon: "copy", run: () => navigator.clipboard?.writeText(m.file) },
    { sep: true },
    collectModelItem(m),
    modelFilesItem(m),
    redrawModelItem(m),
  ];
}

/** What a configurable model's card says instead of its file: how many slots and parts it has. */
export const configText = (m) => `Configurable: ${plural(m.configurable.slots, "slot")}, ${plural(m.configurable.options, "part")}`;

/** A count as it fits a card: 950, 12.3k, 1.2M. */
export function shortCount(n) {
  if (n == null) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** The longest side, in mm, of a model whose size is known. */
export const longestMm = (st) => (st?.size_mm ? Math.max(...st.size_mm) : null);

export function sizeText(st) {
  const l = longestMm(st);
  if (l == null) return "";
  return l >= 100 ? `${Math.round(l).toLocaleString()} mm` : `${l.toFixed(1)} mm`;
}

/** The second line of a card: "1,234 mm · 12.3k triangles", once the model's size and counts are known. */
export function statsLine(st) {
  if (!st) return "";
  const parts = [];
  if (st.size_mm) parts.push(sizeText(st));
  if (st.triangles != null) parts.push(`${shortCount(st.triangles)} triangles`);
  return parts.join(" · ");
}

/** The tooltip of a model's stats: everything known of it. */
export function statsTitle(st) {
  if (!st) return "Its size and counts are read when its preview is drawn or it's opened.";
  const out = [];
  if (st.size_mm) out.push(`${st.size_mm.map((v) => Math.round(v).toLocaleString()).join(" × ")} mm (width × depth × height, as exported)`);
  if (st.vertices != null) out.push(plural(st.vertices, "vertex", "vertices"));
  if (st.triangles != null) out.push(plural(st.triangles, "triangle"));
  if (st.parts != null) out.push(plural(st.parts, "part"));
  if (st.textures != null) out.push(plural(st.textures, "texture"));
  if (st.animated != null) out.push(st.animated ? "has animations" : "no animations");
  if (st.from === "file") out.push("(read from its file; exact once it's opened)");
  return out.join(" · ");
}

/** Under a model's name: its file name when the name isn't the file's own. */
const fileShown = (m) => (m.configurable ? configText(m) : m.stem);

/** A model's star and its tags, as a card or a row shows them. */
function Marks({ m }) {
  if (!m.starred && !m.tags?.length) return null;
  return html`<span class="card-marks">${m.starred ? html`<span class="card-star" title="Starred" aria-label="Starred">${Icon.star(12, true)}</span>` : null}
    ${(m.tags || []).slice(0, 3).map((t) => html`<span class="card-tag" key=${t} title=${`Tagged ${t}`}>${t}</span>`)}${(m.tags || []).length > 3 ? html`<span class="card-tag muted">+${m.tags.length - 3}</span>` : null}</span>`;
}

/** "N related": the models that go with it, in a menu. */
function RelatedLink({ m }) {
  if (!m.related) return null;
  return html`<button type="button" class="linkish card-related" data-related=${m.related} title="The models that go with it: the rest of its set, its other copies, its variants"
    onClick=${(e) => { e.stopPropagation(); relatedMenu(e.currentTarget, m); }} onDblClick=${(e) => e.stopPropagation()}>${Icon.link(11)} ${m.related} related</button>`;
}

/** What a card or row does for the page: `onPick(m, e)` on a click (with its keys: see useModelSelection), `onMenu(m, e)`
 *  on a right-click, `dragIds(m)` the models dragged with it; `drop` (optional): models dropped on it ({ ids, before: m }). */
function cardEvents(m, { onPick, onMenu, dragIds, onDrop }) {
  return {
    onClick: (e) => onPick(m, e),
    onDblClick: () => openModel(m),
    onContextMenu: (e) => (onMenu ? onMenu(m, e) : (onPick(m), openMenu(e, modelMenu(m)))),
    draggable: "true",
    onDragStart: (e) => dragModels(e, dragIds ? dragIds(m) : [m.id]),
    ...(onDrop ? {
      onDragOver: (e) => { if ([...(e.dataTransfer?.types || [])].includes("application/x-darkstl-models")) { e.preventDefault(); e.currentTarget.classList.add("drop-before"); } },
      onDragLeave: (e) => e.currentTarget.classList.remove("drop-before"),
      onDrop: (e) => { e.currentTarget.classList.remove("drop-before"); e.preventDefault(); onDrop(e, m); },
    } : {}),
  };
}

export function Card({ m, selected, where, onPick, onMenu, dragIds, onDrop, game }) {
  const stats = statsLine(m.stats);
  return html`<div class="card" role="option" tabindex="0" data-model=${m.id} aria-selected=${selected ? "true" : "false"} ...${cardEvents(m, { onPick, onMenu, dragIds, onDrop })}>
    <${Thumb} id=${m.id} category=${m.category} turn=${true} />
    <span class="card-name" title=${m.game_name && m.game_name !== m.label ? `${m.label} (in the game: ${m.game_name})` : m.label}>${m.label}</span>
    <span class="card-meta"><span class="card-file" title=${m.name}>${fileShown(m)}</span>
      ${stats ? html`<span class="card-stats" title=${statsTitle(m.stats)}>${stats}</span>` : null}
      ${game ? html`<span class="card-location card-game"><${GameBadge} name=${m.game} size=${13} /> ${m.game}</span>` : null}
      ${where ? html`<span class="card-location" title=${where}>${where}</span>` : null}
      <${Marks} m=${m} /><${RelatedLink} m=${m} /></span>
  </div>`;
}

/** Choosing models on a page: a click chooses one (its details in the panel), Ctrl+click adds or takes one off,
 *  Shift+click a run from the one clicked before, Ctrl+A all of them, Esc none. Several chosen: the command bar
 *  and a right-click on one of them offer what can be done to them all (bulkItems, with the page's `extra`). */
export function useModelSelection(items, extra = []) {
  const [sel, setSel] = useState(() => new Set());
  const [picked, setPickedId] = useState(null);
  const anchor = useRef(null);
  const ids = items.map((m) => m.id);
  const idsKey = ids.join("|");
  useEffect(() => { // models no longer shown (a new search) aren't chosen any more
    const shown = new Set(ids);
    if ([...sel].some((id) => !shown.has(id))) setSel(new Set([...sel].filter((id) => shown.has(id))));
  }, [idsKey]);
  const one = (id) => { setSel(id ? new Set([id]) : new Set()); setPickedId(id); anchor.current = id; };
  const onPick = (m, e) => {
    if (e && (e.ctrlKey || e.metaKey)) {
      const next = new Set(sel);
      if (next.has(m.id)) next.delete(m.id); else next.add(m.id);
      setSel(next);
      setPickedId(next.has(m.id) ? m.id : [...next].pop() ?? null);
      anchor.current = m.id;
      return;
    }
    if (e && e.shiftKey && anchor.current && ids.includes(anchor.current)) {
      const a = ids.indexOf(anchor.current), b = ids.indexOf(m.id);
      setSel(new Set(ids.slice(Math.min(a, b), Math.max(a, b) + 1)));
      setPickedId(m.id);
      e.preventDefault?.();
      return;
    }
    one(m.id);
  };
  const chosen = items.filter((m) => sel.has(m.id));
  const multi = chosen.length > 1;
  return {
    sel, multi, chosen,
    picked: multi ? null : items.find((m) => m.id === picked) || null,
    pickedId: picked,
    setPicked: one,
    onPick,
    clear: () => one(null),
    selectAll: () => { setSel(new Set(ids)); anchor.current = ids[0] || null; },
    dragIds: (m) => (multi && sel.has(m.id) ? chosen.map((x) => x.id) : [m.id]),
    onMenu: (m, e) => {
      if (multi && sel.has(m.id)) { openMenu(e, bulkItems(chosen, extra)); return; }
      one(m.id);
      openMenu(e, modelMenu(m));
    },
    /** The command bar's selection, while several are chosen. */
    bar: multi ? { count: `${plural(chosen.length, "model")} chosen`, onClear: () => one(null), items: bulkItems(chosen, extra), buttons: 1 } : null,
    /** The keys the page passes on: Ctrl+A and Esc (true when it was one of them). */
    key: (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "a") { e.preventDefault(); setSel(new Set(ids)); return true; }
      if (e.key === "Escape") { one(null); return true; }
      return false;
    },
  };
}

const SIZES = [[132, "Small"], [172, "Medium"], [236, "Large"]];

/** The cards' size: small, medium or large (kept for every page). */
export function CardSize() {
  const size = useStore(ui, (s) => s.cardSize || 172);
  const i = SIZES.reduce((best, [v], k) => (Math.abs(v - size) < Math.abs(SIZES[best][0] - size) ? k : best), 1);
  return html`<label class="card-size" title=${`Card size: ${SIZES[i][1].toLowerCase()} (small, medium or large)`}>
    <span class="visually-hidden">Card size</span>${Icon.grid(12)}
    <input type="range" id="card-size" min="0" max="2" step="1" value=${i} aria-valuetext=${SIZES[i][1]}
      onInput=${(e) => setPref({ cardSize: SIZES[Number(e.target.value)][0] })} />${Icon.grid(16)}</label>`;
}

/** The style that sizes a grid's cards. */
export const cardStyle = (size) => `--card: ${size || 172}px`;

/** The list view's columns. `all`: the search across every game, which has a Game column. */
export function listColumns(all) {
  return [
    { id: "name", label: "Name", sort: "name", title: "The model's name: yours, the game's, or its file's" },
    ...(all ? [{ id: "game", label: "Game", sort: "game" }] : []),
    { id: "kind", label: "Kind", sort: "kind", title: "Its kind and group, as the library files it" },
    { id: "size", label: "Size", sort: "size", num: true, title: "Its longest side in millimetres, as exported" },
    { id: "triangles", label: "Triangles", sort: "triangles", num: true },
    { id: "parts", label: "Parts", sort: "parts", num: true, title: "The separate meshes it's made of" },
    { id: "textures", label: "Textures", sort: "textures", num: true },
    { id: "animated", label: "Animations", sort: "animated", title: "Whether it has animations to pose it with" },
    { id: "collected", label: "Collected", sort: "collected", title: "Whether it's a copy collected into the app (it opens without the game)" },
  ];
}

const UNKNOWN = html`<span class="muted" title="Not read yet: it is when its preview is drawn or it's opened">–</span>`;

function cell(c, m) {
  const st = m.stats || {};
  switch (c.id) {
    case "name": return html`<span class="lc-name-text">${m.starred ? html`<span class="card-star" title="Starred">${Icon.star(12, true)}</span> ` : null}${m.label}${m.configurable ? html` <small class="muted config-tag">${configText(m)}</small>` : null}${(m.tags || []).map((t) => html` <span class="card-tag" key=${t}>${t}</span>`)}</span>
      <span class="lc-file" title=${m.name}>${m.name}</span>`;
    case "game": return html`<span class="lc-game"><${GameBadge} name=${m.game} size=${13} /> ${m.game}</span>`;
    case "kind": return `${m.category}${m.group ? ` › ${m.group}` : ""}`;
    case "size": return st.size_mm ? sizeText(st) : UNKNOWN;
    case "triangles": return st.triangles != null ? st.triangles.toLocaleString() : UNKNOWN;
    case "parts": return st.parts != null ? st.parts.toLocaleString() : UNKNOWN;
    case "textures": return st.textures != null ? st.textures.toLocaleString() : UNKNOWN;
    case "animated": return st.animated == null ? UNKNOWN : st.animated ? "Yes" : html`<span class="muted">No</span>`;
    case "collected": return m.collected ? "Collected" : html`<span class="muted">No</span>`;
    default: return "";
  }
}

/** The list's header: each column's name, a button that sorts by it (again: the other way). */
export function ListHead({ cols, sort, onSort }) {
  return html`<div class="lib-list-head list-head" role="presentation" id="list-head">
    <span class="lc-thumb"></span>
    ${cols.map((c) => {
      const on = sort.by === c.sort;
      const arrow = on ? (sort.desc ? " ↓" : " ↑") : "";
      return html`<span key=${c.id} class=${`lc lc-${c.id}${c.num ? " lc-num" : ""}`}>
        <button type="button" class="linkish sort-head" data-sort=${c.sort} aria-pressed=${on ? "true" : "false"}
          title=${`${c.title ? `${c.title}. ` : ""}Click to sort by ${c.label.toLowerCase()}${on ? ", again for the other way" : ""}`}
          onClick=${() => onSort(on ? { by: c.sort, desc: !sort.desc } : { by: c.sort, desc: false })}>${c.label}${arrow}</button></span>`;
    })}
  </div>`;
}

export function ListRow({ m, cols, selected, onPick, onMenu, dragIds, onDrop }) {
  return html`<div class="row lib-row" role="option" tabindex="0" data-model=${m.id} aria-selected=${selected ? "true" : "false"} ...${cardEvents(m, { onPick, onMenu, dragIds, onDrop })}>
    <${Thumb} id=${m.id} category=${m.category} cls="thumb-sm lc-thumb" />
    ${cols.map((c) => html`<span key=${c.id} class=${`lc lc-${c.id}${c.num ? " lc-num" : ""}`} data-col=${c.id}>${cell(c, m)}</span>`)}
  </div>`;
}

/** The sorts the Sort menu offers (the list's headers sort by the same). */
export function sortOptions(all, collection = false) {
  return [...(collection ? [["collection", "Collection order"]] : []), ["kind", "Kind"], ["name", "Name"], ["file", "File name"], ...(all ? [["game", "Game"]] : []), ["size", "Size"],
    ["triangles", "Triangles"], ["parts", "Parts"], ["textures", "Textures"], ["animated", "Animations"], ["collected", "Collected"]];
}

const KEEP_SORTS = 60;

/** The sort of a page (`key`: its route, or "search"), kept in the preferences per page: { by, desc }. */
export function usePageSort(key) {
  const kept = useStore(ui, (s) => (s.sorts || {})[key] || null);
  const fallback = useStore(ui, (s) => s.sort);
  const sort = kept || { by: fallback === "file" ? "file" : "kind", desc: false };
  const set = (next) => {
    const all = { ...(ui.get().sorts || {}) };
    delete all[key];
    all[key] = { by: next.by, desc: !!next.desc };
    const keys = Object.keys(all);
    for (const k of keys.slice(0, Math.max(0, keys.length - KEEP_SORTS))) delete all[k];
    setPref({ sorts: all });
  };
  return [sort, set];
}
