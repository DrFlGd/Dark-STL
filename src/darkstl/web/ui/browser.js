// A game's library (browse:<game>[/<category>[/<group>]]): the game shows its
// kinds of model as tiles; a kind shows its groups as chips and its models as
// cards or rows, with a preview each. Click selects (its details are in the
// inspector), double-click or Enter opens it. A search looks through the whole
// place shown (the whole game from the game's page).
import { html, useState, useEffect, useLayoutEffect, useRef } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { routeHash, placeOf, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { api, plural, loadOverview, showInFolder } from "./api.js";
import { CommandBar, ViewSwitch, SortMenu, SearchNote } from "./layout.js";
import { Inspector } from "./inspector.js";
import { openMenu, usePageKeys } from "./actions.js";
import { Thumb } from "./thumbs.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { gameMenu, rescanFolder, collectItem, collectModelItem } from "./games.js";

const PAGE = 300;
const VIEWS = [["grid", "Grid", "grid"], ["list", "List", "list"]];
const SORTS = [["kind", "Kind"], ["file", "File name"]];

const openModel = (m) => { location.hash = routeHash(`model:${m.id}`); };

/** What a model's card says under its name: where it is below the place shown, and its file. */
function whereOf(m, place) {
  const parts = [];
  if (!place.category) parts.push(m.category);
  if (!place.group && m.group) parts.push(m.group);
  return parts.join(" › ");
}

function modelMenu(m) {
  return [
    { id: "open", label: "Open", icon: "eye", key: "Enter", run: () => openModel(m) },
    { id: "show", label: "Show in folder", icon: "folder", run: () => showInFolder(m.file) },
    { id: "copy-path", label: "Copy file path", icon: "copy", run: () => navigator.clipboard?.writeText(m.file) },
    { sep: true },
    collectModelItem(m),
  ];
}

function Card({ m, selected, where, onPick }) {
  return html`<div class="card" role="option" tabindex="0" data-model=${m.id} aria-selected=${selected ? "true" : "false"}
      onClick=${() => onPick(m)} onDblClick=${() => openModel(m)} onContextMenu=${(e) => { onPick(m); openMenu(e, modelMenu(m)); }}>
    <${Thumb} id=${m.id} category=${m.category} />
    <span class="card-name" title=${m.label}>${m.label}</span>
    <span class="card-meta"><span class="card-file" title=${m.name}>${m.stem}</span>${where ? html`<span class="card-location" title=${where}>${where}</span>` : null}</span>
  </div>`;
}

function Row({ m, selected, where, onPick }) {
  return html`<div class="row" role="option" tabindex="0" data-model=${m.id} aria-selected=${selected ? "true" : "false"}
      onClick=${() => onPick(m)} onDblClick=${() => openModel(m)} onContextMenu=${(e) => { onPick(m); openMenu(e, modelMenu(m)); }}>
    <${Thumb} id=${m.id} category=${m.category} cls="thumb-sm" />
    <span class="row-name">${m.label}</span>
    <span class="row-author" title=${m.name}>${m.name}</span>
    <span class="row-cat">${where}</span>
  </div>`;
}

/** A kind of model in a game, as a tile with four of its models. */
function KindTile({ game, cat, lod }) {
  const [models, setModels] = useState(null);
  useEffect(() => {
    let live = true;
    api("models_query", { game, category: cat.name, lod, limit: 4 }).then((r) => { if (live) setModels(r.items); }, () => { if (live) setModels([]); });
    return () => { live = false; };
  }, [game, cat.name, cat.count, lod]);
  const groups = cat.groups.length;
  return html`<a class="subcat-tile" href=${routeHash(placeRoute(game, cat.name))} data-sub=${cat.name} title=${cat.name}>
    <span class=${`subcat-pics subcat-pics-${Math.max(1, models?.length || 0)}`}>${models?.length
      ? models.map((m) => html`<${Thumb} key=${m.id} id=${m.id} category=${m.category} cls="subcat-pic" />`)
      : html`<span class="subcat-empty"><${CategoryIcon} name=${cat.name} size=${34} /></span>`}</span>
    <span class="subcat-name"><${CategoryIcon} name=${cat.name} size=${15} /><span>${cat.name}</span></span>
    <span class="subcat-meta">${plural(cat.count, "model")}${groups > 1 ? ` · ${plural(groups, "group")}` : ""}</span>
  </a>`;
}

/** The groups of the kind shown, as chips: All, then each group. */
function GroupChips({ game, cat, group }) {
  if (cat.groups.length < 2) return null;
  const chip = (name, count, route) => html`<a class=${`chip-btn${group === name ? " on" : ""}`} key=${name ?? ""} href=${routeHash(route)}
    aria-current=${group === name ? "page" : null} data-group=${name ?? ""}>${name ?? "All"} <span class="muted">${count.toLocaleString()}</span></a>`;
  return html`<div class="filters group-chips" id="group-chips" aria-label="Groups">
    ${chip(null, cat.count, placeRoute(game, cat.name))}
    ${cat.groups.map((g) => chip(g.name, g.count, placeRoute(game, cat.name, g.name)))}
  </div>`;
}

/** The game's folders, with Rescan and whatever their scan had to say. */
function GameFolders({ game }) {
  return html`<section class="insp-section" id="game-folders"><h3>${game.folders.length === 1 ? "Folder" : "Folders"}</h3>
    ${game.folders.map((f) => html`<div class="game-folder" key=${f.path} data-folder=${f.path}>
      <p class="note-path">${f.path}</p>
      <p class="muted">${f.missing ? "This folder isn't there any more." : plural(f.count, "model")}</p>
      ${f.notes.map((n, i) => html`<p class="warn-note" key=${i}>${n}</p>`)}
      <div class="insp-actions">
        <button type="button" class="ghost" disabled=${f.missing} onClick=${() => showInFolder(f.path)}>${Icon.folder(14)} Show</button>
        <button type="button" class="ghost" disabled=${f.missing} onClick=${() => rescanFolder(f.path)}>${Icon.refresh(14)} Rescan</button>
      </div></div>`)}
    <button type="button" class="ghost" id="game-add-folder" onClick=${() => ui.set({ dialog: { type: "add-folder", game: game.name } })}>${Icon.plus(14)} Add another folder…</button>
  </section>`;
}

/** Nothing selected: what the place holds. */
function PlaceSummary({ game, cat, group, total }) {
  const groups = cat?.groups || [];
  const collect = collectItem(game, { category: cat?.name, group });
  return html`<div class="place-summary" id="place-summary">
    <section class="insp-section"><h3>${group || cat?.name || game.name}</h3>
      <p>${plural(total ?? (cat ? cat.count : game.count), "model")}${!cat ? ` in ${plural(game.categories.length, "kind")}` : ""}.</p>
      <p class="muted">Click a model to see it here; double-click (or Enter) opens it.</p>
      ${game.count ? html`<div class="insp-actions"><button type="button" class="ghost" id="place-collect" disabled=${!!collect.disabled} title=${collect.disabled || "Copy the files these models need into Dark STL's own folder, so the game's folder isn't required"} onClick=${collect.run}>${Icon.inbox(14)} Collect into the app…</button></div>` : null}</section>
    ${!cat && game.categories.length ? html`<section class="insp-section"><h3>Kinds</h3><ul class="ls-list">${game.categories.map((c) => html`<li key=${c.name}>
      <a href=${routeHash(placeRoute(game.name, c.name))}>${c.name}</a><span class="muted">${c.count.toLocaleString()}</span></li>`)}</ul></section>` : null}
    ${cat && !group && groups.length > 1 ? html`<section class="insp-section"><h3>Groups</h3><ul class="ls-list">${groups.map((g) => html`<li key=${g.name}>
      <a href=${routeHash(placeRoute(game.name, cat.name, g.name))}>${g.name}</a><span class="muted">${g.count.toLocaleString()}</span></li>`)}</ul></section>` : null}
    <${GameFolders} game=${game} />
  </div>`;
}

/** One model selected: its preview, where it is, and Open. */
function ModelSummary({ m }) {
  const collect = collectModelItem(m);
  return html`<div class="place-summary model-summary" id="model-summary" data-model=${m.id}>
    <button type="button" class="insp-thumb" title="Open" onClick=${() => openModel(m)}><${Thumb} id=${m.id} category=${m.category} /></button>
    <h3 class="insp-title" id="summary-name">${m.label}</h3>
    <div class="insp-actions">
      <button type="button" class="primary compact" id="summary-open" onClick=${() => openModel(m)}>${Icon.eye(15)} Open</button>
      <button type="button" class="ghost" id="summary-show" onClick=${() => showInFolder(m.file)}>${Icon.folder(14)} Show in folder</button>
      <button type="button" class="ghost" id="summary-collect" disabled=${!!collect.disabled} title=${collect.disabled || "Copy this model's files into Dark STL's own folder"} onClick=${collect.run}>${Icon.inbox(14)} Collect…</button>
    </div>
    <dl class="insp-dl">
      <dt>File</dt><dd>${m.name}</dd>
      <dt>Kind</dt><dd>${m.category}${m.group ? ` › ${m.group}` : ""}</dd>
      <dt>Game</dt><dd>${m.game}</dd>
      <dt>Folder</dt><dd class="note-path">${m.file.slice(0, m.file.length - m.name.length - 1)}</dd>
      ${m.lod ? html`<dt>Detail</dt><dd>Low-detail copy (seen from afar in the game)</dd>` : null}
      ${collect.disabled ? html`<dt>Kept</dt><dd>A copy Dark STL keeps in its own library folder</dd>` : null}
    </dl>
  </div>`;
}

export function Browser({ route }) {
  const place = placeOf(route);
  const s = useStore(ui, (st) => ({ overview: st.overview, q: st.q, layout: st.layout, lod: st.lod, sort: st.sort }));
  const game = s.overview?.games.find((g) => g.name === place.game);
  const cat = game?.categories.find((c) => c.name === place.category);
  const tiles = !place.category && !s.q;
  const [text, setText] = useState(s.q);
  const [result, setResult] = useState(null);
  const [limit, setLimit] = useState(PAGE);
  const [picked, setPicked] = useState(null);
  const seq = useRef(0);
  useLayoutEffect(() => { setText(s.q); }, [s.q]);
  useEffect(() => { setLimit(PAGE); }, [route, s.q, s.sort, s.lod]);
  useEffect(() => {
    if (tiles || !game) return;
    const n = ++seq.current;
    const q = s.q;
    api("models_query", { game: place.game, category: place.category, group: place.group, q, lod: s.lod, sort: s.sort, limit })
      .then((r) => { if (n === seq.current) setResult({ ...r, q }); }, (e) => { if (n === seq.current) setResult({ total: 0, items: [], q, error: String(e.message || e) }); });
  }, [route, s.q, s.lod, s.sort, limit, tiles, game?.count]);
  useEffect(() => {
    if (text === s.q) return;
    const t = setTimeout(() => ui.set({ q: text }), 150);
    return () => clearTimeout(t);
  }, [text]);
  const items = tiles ? [] : result?.items || [];
  const selected = items.find((m) => m.id === picked) || null;
  usePageKeys((e) => {
    const ids = items.map((m) => m.id);
    if (e.key === "Enter") { if (selected) { e.preventDefault(); openModel(selected); } return; }
    if (e.key === "Escape") { setPicked(null); return; }
    if (!e.key.startsWith("Arrow") || !ids.length || e.ctrlKey || e.metaKey || e.altKey) return;
    let cols = 1;
    if (s.layout !== "list") {
      const els = [...document.querySelectorAll(".results > [data-model]")];
      cols = Math.max(1, els.filter((x) => x.offsetTop === els[0]?.offsetTop).length);
    }
    const d = { ArrowRight: s.layout === "list" ? 0 : 1, ArrowLeft: s.layout === "list" ? 0 : -1, ArrowDown: cols, ArrowUp: -cols }[e.key];
    if (!d) return;
    e.preventDefault();
    const cur = ids.indexOf(picked);
    const id = ids[cur < 0 ? 0 : Math.max(0, Math.min(ids.length - 1, cur + d))];
    setPicked(id);
    document.querySelector(`.results [data-model="${CSS.escape(id)}"]`)?.focus();
  });
  const toggleLod = () => { setPref({ lod: !ui.get().lod }); loadOverview(); };
  const search = html`<label class="search small toolbar-search"><span class="visually-hidden">Search</span>
    <input id="search" data-search type="search" value=${text} placeholder=${`Search ${place.category ? place.group || place.category : place.game}…`} autocomplete="off" spellcheck="false"
      title="Search names and file names (/)" onInput=${(e) => setText(e.target.value)} /></label>`;
  if (!s.overview) return html`<${CommandBar} id="browse-head" title="" /><div class="pages"></div>`;
  if (!game) {
    return html`<${CommandBar} id="browse-head" title=${place.game} /><div class="pages">
      <p class="warn-note" role="alert">${s.overview.scanning ? "Reading the game folders…" : html`${place.game} isn't in the library. <a href="#/">Home</a> lists the games that are.`}</p></div>`;
  }
  const up = [];
  if (place.category) up.push([game.name, routeHash(placeRoute(game.name))]);
  if (place.group) up.push([place.category, routeHash(placeRoute(game.name, place.category))]);
  const name = place.group || place.category || game.name;
  const icon = place.category ? html`<${CategoryIcon} name=${place.category} size=${20} />` : html`<${GameBadge} name=${game.name} size=${20} />`;
  const total = tiles ? game.count : result?.total;
  const more = [...gameMenu(game, place), { sep: true },
    { id: "lod", label: s.lod ? "Hide low-detail copies" : "Show low-detail copies", sub: "the _l models the game shows from afar", icon: s.lod ? "check" : null, run: toggleLod }];
  const View = s.layout === "list" ? Row : Card;
  const fresh = result && result.q === s.q ? result : null;
  const notes = !place.category ? game.folders.filter((f) => f.notes.length) : [];
  return html`<${CommandBar} id="browse-head" up=${up} title=${html`<h1 class="crumbs" title=${[...up.map(([n]) => n), name].join(" › ")}>${icon}<span class="crumbs-name">${name}</span></h1>`}
      count=${html`<span class="page-count" id="browse-count">${total == null ? "" : plural(total, "model")}</span>`}
      search=${search}
      tools=${tiles ? null : html`<${ViewSwitch} id="layout-switch" views=${VIEWS} value=${s.layout === "list" ? "list" : "grid"} onChange=${(v) => setPref({ layout: v })} />
        <${SortMenu} id="sort" options=${SORTS} value=${s.sort} onChange=${(v) => setPref({ sort: v })} />`}
      more=${{ id: "browse-more", title: "This game: folders, rescan, low-detail copies", items: more }} />
  <div class="browser">
    <div class="browse-main">
      ${cat ? html`<${GroupChips} game=${game.name} cat=${cat} group=${place.group} />` : null}
      <div class="browse-scroll">
        ${notes.map((f) => html`<div class="warn-note folder-note" key=${f.path}>${f.notes.map((n, i) => html`<p key=${i}>${n}</p>`)}</div>`)}
        <${SearchNote} q=${s.q} found=${fresh ? `${plural(fresh.total, "model")} found in ${name}` : ""}
          wider=${s.q && place.category ? html`<a href=${routeHash(placeRoute(game.name))} id="search-game">Search all of ${game.name}</a>` : null}
          onClear=${() => { setText(""); ui.set({ q: "" }); }} />
        ${tiles ? html`<div class="subcat-tiles" id="kind-tiles" aria-label="Kinds of model">${game.categories.map((c) => html`<${KindTile} key=${c.name} game=${game.name} cat=${c} lod=${s.lod} />`)}</div>
          ${!game.categories.length ? html`<div class="empty" id="no-results">No models were found in this game's ${game.folders.length === 1 ? "folder" : "folders"}.</div>` : null}` : null}
        ${result?.error ? html`<p class="warn-note" role="alert">${result.error}</p>` : null}
        ${!tiles && fresh && !items.length && !result.error ? html`<div class="empty" id="no-results">${s.q ? `Nothing in ${name} matches that search.` : "No models here."}</div>` : null}
        ${!tiles ? html`<div class=${`results ${s.layout === "list" ? "list-view" : "grid-view"}`} role="listbox" aria-label="Models">
          ${items.map((m) => html`<${View} key=${m.id} m=${m} selected=${picked === m.id} where=${whereOf(m, place)} onPick=${(x) => setPicked(x.id)} />`)}
        </div>` : null}
        ${!tiles && result && result.total > items.length ? html`<button type="button" class="ghost more-btn" id="show-more" onClick=${() => setLimit(limit + PAGE)}>Show more (${(result.total - items.length).toLocaleString()} left)</button>` : null}
      </div>
    </div>
    <${Inspector} page="browse" open=${!matchMedia("(max-width: 1250px)").matches} tabs=${[
      { id: "details", label: "Details", icon: "info", body: selected ? html`<${ModelSummary} key=${selected.id} m=${selected} />`
        : html`<${PlaceSummary} game=${game} cat=${cat} group=${place.group} total=${tiles ? null : result?.total} />` },
    ]} />
  </div>`;
}
