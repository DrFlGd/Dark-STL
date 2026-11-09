// A game's library (browse:<game>[/<category>[/<group>]]): the game shows its
// kinds of model as tiles; a kind shows its groups as chips and its models as
// cards or rows, with a preview each. Click selects (its details are in the
// inspector), double-click or Enter opens it. A search looks through the whole
// place shown (the whole game from the game's page).
import { html, useState, useEffect, useLayoutEffect, useRef } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { routeHash, placeOf, placeRoute, searchHash } from "./context.js";
import { Icon } from "./icons.js";
import { api, plural, loadOverview, showInFolder } from "./api.js";
import { CommandBar, ViewSwitch, SortMenu, SearchNote } from "./layout.js";
import { Inspector } from "./inspector.js";
import { usePageKeys } from "./actions.js";
import { Thumb } from "./thumbs.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { gameMenu, rescanFolder, collectItem, collectModelItem, unpackFolder, UNPACK_HINT, arrangeModel } from "./games.js";
import { openModel, configText, Card, ListHead, ListRow, listColumns, sortOptions, usePageSort, statsTitle, useModelSelection, CardSize, cardStyle } from "./models.js";
import { Related, SelectionSummary, markItems } from "./organise.js";
import { Problems } from "./problems.js";

const PAGE = 300;
const VIEWS = [["grid", "Grid", "grid"], ["list", "List", "list"]];

/** What a model's card says under its name: where it is below the place shown, and its file. */
function whereOf(m, place) {
  const parts = [];
  if (!place.category) parts.push(m.category);
  if (!place.group && m.group) parts.push(m.group);
  return parts.join(" › ");
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

/** The groups of the kind shown, as chips: All, then each group. A kind with
 * many groups (Cyberpunk's depot folders run to hundreds) keeps them to two
 * rows, with the group shown moved up front and a button to see the rest in a
 * short scrolling box; every group is also listed in the panel on the right. */
function GroupChips({ game, cat, group }) {
  const box = useRef(null);
  const [open, setOpen] = useState(false);
  const [clipped, setClipped] = useState(false);
  useEffect(() => { setOpen(false); }, [game, cat.name]);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const check = () => setClipped(el.scrollHeight > el.clientHeight + 1);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [cat, open]);
  if (cat.groups.length < 2) return null;
  const chip = (name, count, route) => html`<a class=${`chip-btn${group === name ? " on" : ""}`} key=${name ?? ""} href=${routeHash(route)}
    aria-current=${group === name ? "page" : null} data-group=${name ?? ""} title=${`${name ?? "All"} (${count.toLocaleString()})`}><span class="chip-name">${name ?? "All"}</span> <span class="muted">${count.toLocaleString()}</span></a>`;
  // collapsed, the group shown comes straight after All so it is never hidden
  const shown = !open && group ? [...cat.groups.filter((g) => g.name === group), ...cat.groups.filter((g) => g.name !== group)] : cat.groups;
  return html`<div class=${`group-chips-wrap${open ? " open" : ""}`}>
    <div class="filters group-chips" id="group-chips" aria-label="Groups" ref=${box}>
      ${chip(null, cat.count, placeRoute(game, cat.name))}
      ${shown.map((g) => chip(g.name, g.count, placeRoute(game, cat.name, g.name)))}
    </div>
    ${clipped || open ? html`<button type="button" class="ghost compact group-chips-more" id="group-chips-more" aria-expanded=${open ? "true" : "false"}
      onClick=${() => setOpen(!open)}>${open ? "Fewer groups" : `All ${cat.groups.length.toLocaleString()} groups`}</button>` : null}
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
        ${f.packed ? html`<button type="button" class="primary compact folder-unpack" disabled=${f.missing} title=${UNPACK_HINT} onClick=${() => unpackFolder(f.path, f.packed.game)}>${Icon.inbox(14)} Unpack…</button>` : null}
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
      ${game.count ? html`<div class="insp-actions"><button type="button" class="ghost" id="place-collect" disabled=${!!collect.disabled} title=${collect.disabled || "Copy the files these models need into Dark STL's own folder, so the game's folder isn't required"} onClick=${collect.run}>${Icon.inbox(14)} Collect into the app…</button>
        ${cat ? html`<button type="button" class="ghost" id="place-rename" title="Rename this folder, or move it into another, for every model in it"
          onClick=${() => ui.set({ dialog: { type: "arrange-folder", game: game.name, category: cat.name, group } })}>${Icon.edit(14)} Rename or move this folder…</button>` : null}</div>` : null}</section>
    ${!cat && game.categories.length ? html`<section class="insp-section"><h3>Kinds</h3><ul class="ls-list">${game.categories.map((c) => html`<li key=${c.name}>
      <a href=${routeHash(placeRoute(game.name, c.name))}>${c.name}</a><span class="muted">${c.count.toLocaleString()}</span></li>`)}</ul></section>` : null}
    ${cat && !group && groups.length > 1 ? html`<section class="insp-section"><h3>Groups</h3><ul class="ls-list">${groups.map((g) => html`<li key=${g.name}>
      <a href=${routeHash(placeRoute(game.name, cat.name, g.name))}>${g.name}</a><span class="muted">${g.count.toLocaleString()}</span></li>`)}</ul></section>` : null}
    <${GameFolders} game=${game} />
  </div>`;
}

/** One model selected: its preview, where it is, and Open. */
export function ModelSummary({ m }) {
  const collect = collectModelItem(m);
  const marks = markItems(m);
  return html`<div class="place-summary model-summary" id="model-summary" data-model=${m.id}>
    <button type="button" class="insp-thumb" title="Open" onClick=${() => openModel(m)}><${Thumb} id=${m.id} category=${m.category} /></button>
    <h3 class="insp-title" id="summary-name">${m.label}</h3>
    <div class="insp-actions">
      <button type="button" class="primary compact" id="summary-open" onClick=${() => openModel(m)}>${Icon.eye(15)} Open</button>
      <button type="button" class="ghost" id="summary-show" onClick=${() => showInFolder(m.file)}>${Icon.folder(14)} Show in folder</button>
      <button type="button" class="ghost" id="summary-collect" disabled=${!!collect.disabled} title=${collect.disabled || "Copy this model's files into Dark STL's own folder"} onClick=${collect.run}>${Icon.inbox(14)} Collect…</button>
      ${m.configurable ? null : html`<button type="button" class="ghost" id="summary-arrange" title="Give it its in-game name, or file it in a folder of your own" onClick=${() => arrangeModel(m)}>${Icon.edit(14)} Rename or move…</button>`}
      ${marks.map((it) => html`<button type="button" key=${it.id} class="ghost" id=${`summary-${it.id}`} title=${it.sub || it.label} onClick=${it.run}>${Icon[it.icon](14, it.filled)} ${it.label}</button>`)}
    </div>
    ${m.tags?.length || m.collections?.length ? html`<p class="summary-marks" id="summary-marks">${(m.tags || []).map((t) => html`<a key=${t} class="card-tag" href=${searchHash(t)} title=${`Search every game for ${t}`}>${t}</a>`)}
      ${(m.collections || []).map((c) => html`<a key=${c.id} class="card-location summary-collection" href=${routeHash(`collection:${c.id}`)} title=${`In ${c.name}`}>${Icon.stack(12)} ${c.name}</a>`)}</p>` : null}
    <dl class="insp-dl">
      ${m.game_name && m.game_name !== m.label ? html`<dt>In-game name</dt><dd id="summary-game-name">${m.game_name}</dd>` : null}
      ${m.game_name && m.file_label && m.file_label !== m.game_name ? html`<dt>From its file</dt><dd id="summary-file-label">${m.file_label}</dd>` : null}
      <dt>File</dt><dd>${m.name}</dd>
      <dt>Kind</dt><dd>${m.category}${m.group ? ` › ${m.group}` : ""}</dd>
      ${m.original ? html`<dt>In the game</dt><dd id="summary-original">${m.original.label} · ${m.original.category}${m.original.group ? ` › ${m.original.group}` : ""}</dd>` : null}
      ${m.stats ? html`<dt>Size</dt><dd id="summary-size" title=${statsTitle(m.stats)}>${m.stats.size_mm ? `${m.stats.size_mm.map((v) => Math.round(v).toLocaleString()).join(" × ")} mm` : "–"}</dd>
        <dt>Mesh</dt><dd id="summary-counts">${[m.stats.triangles != null ? `${m.stats.triangles.toLocaleString()} triangles` : null,
          m.stats.parts != null ? `${m.stats.parts} part${m.stats.parts === 1 ? "" : "s"}` : null,
          m.stats.textures != null ? `${m.stats.textures} texture${m.stats.textures === 1 ? "" : "s"}` : null,
          m.stats.animated ? "animated" : null].filter(Boolean).join(" · ")}${m.stats.from === "file" ? html` <span class="muted">(from its file)</span>` : null}</dd>` : null}
      ${m.configurable ? html`<dt>Parts</dt><dd id="summary-config">${configText(m).replace("Configurable: ", "")} to choose from${m.configurable.guessed ? " (matched by file names)" : ""}</dd>` : null}
      <dt>Game</dt><dd>${m.game}</dd>
      <dt>Folder</dt><dd class="note-path">${m.file.slice(0, m.file.length - m.name.length - 1)}</dd>
      ${m.lod ? html`<dt>Detail</dt><dd>Low-detail copy (seen from afar in the game)</dd>` : null}
      ${collect.disabled ? html`<dt>Kept</dt><dd>A copy Dark STL keeps in its own library folder</dd>` : null}
    </dl>
    ${m.configurable || m.related ? html`<${Related} m=${m} id="summary-related" />` : null}
  </div>`;
}

export function Browser({ route }) {
  const place = placeOf(route);
  const s = useStore(ui, (st) => ({ overview: st.overview, q: st.q, layout: st.layout, lod: st.lod, cardSize: st.cardSize }));
  const [sort, setSort] = usePageSort(route);
  const libRev = useStore(ui, (st) => st.libRev);
  const game = s.overview?.games.find((g) => g.name === place.game);
  const cat = game?.categories.find((c) => c.name === place.category);
  const tiles = !place.category && !s.q;
  const [text, setText] = useState(s.q);
  const [result, setResult] = useState(null);
  const [limit, setLimit] = useState(PAGE);
  const seq = useRef(0);
  useLayoutEffect(() => { setText(s.q); }, [s.q]);
  useEffect(() => { setLimit(PAGE); }, [route, s.q, sort.by, sort.desc, s.lod]);
  useEffect(() => {
    if (tiles || !game) return;
    const n = ++seq.current;
    const q = s.q;
    api("models_query", { game: place.game, category: place.category, group: place.group, q, lod: s.lod, sort: sort.by, desc: sort.desc, limit })
      .then((r) => { if (n === seq.current) setResult({ ...r, q }); }, (e) => { if (n === seq.current) setResult({ total: 0, items: [], q, error: String(e.message || e) }); });
  }, [route, s.q, s.lod, sort.by, sort.desc, limit, tiles, game?.count, libRev]);
  useEffect(() => {
    if (text === s.q) return;
    const t = setTimeout(() => ui.set({ q: text }), 150);
    return () => clearTimeout(t);
  }, [text]);
  const items = tiles ? [] : result?.items || [];
  const pick = useModelSelection(items);
  const selected = pick.picked;
  const picked = pick.pickedId;
  const setPicked = pick.setPicked;
  usePageKeys((e) => {
    const ids = items.map((m) => m.id);
    if (e.key === "Enter") { if (selected) { e.preventDefault(); openModel(selected); } return; }
    if (pick.key(e)) return;
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
      title="Search names, names in the game and file names (/)" onInput=${(e) => setText(e.target.value)} /></label>`;
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
  const list = s.layout === "list";
  const cols = listColumns(false);
  const fresh = result && result.q === s.q ? result : null;
  return html`<${CommandBar} id="browse-head" up=${up} selection=${pick.bar} title=${html`<h1 class="crumbs" title=${[...up.map(([n]) => n), name].join(" › ")}>${icon}<span class="crumbs-name">${name}</span></h1>`}
      count=${html`<span class="page-count" id="browse-count">${total == null ? "" : plural(total, "model")}</span>`}
      search=${search}
      tools=${tiles ? null : html`${list ? null : html`<${CardSize} />`}<${ViewSwitch} id="layout-switch" views=${VIEWS} value=${s.layout === "list" ? "list" : "grid"} onChange=${(v) => setPref({ layout: v })} />
        <${SortMenu} id="sort" options=${sortOptions(false)} value=${sort.by} onChange=${(v) => setSort({ by: v, desc: false })} />`}
      more=${{ id: "browse-more", title: "This game: folders, rescan, low-detail copies", items: more }} />
  <div class="browser">
    <div class="browse-main">
      ${cat ? html`<${GroupChips} game=${game.name} cat=${cat} group=${place.group} />` : null}
      <div class="browse-scroll">
        <${SearchNote} q=${s.q} found=${fresh ? `${plural(fresh.total, "model")} found in ${name}` : ""}
          wider=${s.q ? html`${place.category ? html`<a href=${routeHash(placeRoute(game.name))} id="search-game">Search all of ${game.name}</a>` : null}
            <a href=${searchHash(s.q)} id="search-everywhere" title="The same search in every game in the library">Search every game</a>` : null}
          onClear=${() => { setText(""); ui.set({ q: "" }); }} />
        ${tiles ? html`<div class="subcat-tiles" id="kind-tiles" aria-label="Kinds of model">${game.categories.map((c) => html`<${KindTile} key=${c.name} game=${game.name} cat=${c} lod=${s.lod} />`)}</div>
          ${!game.categories.length ? html`<div class="empty" id="no-results">No models were found in this game's ${game.folders.length === 1 ? "folder" : "folders"}.</div>` : null}
          <${Problems} game=${game} />` : null}
        ${result?.error ? html`<p class="warn-note" role="alert">${result.error}</p>` : null}
        ${!tiles && fresh && !items.length && !result.error ? html`<div class="empty" id="no-results">${s.q ? `Nothing in ${name} matches that search.` : "No models here."}</div>` : null}
        ${!tiles && list && items.length ? html`<${ListHead} cols=${cols} sort=${sort} onSort=${setSort} />` : null}
        ${!tiles ? html`<div class=${`results ${list ? "list-view lib-list" : "grid-view"}${pick.multi ? " picking" : ""}`} role="listbox" aria-label="Models" aria-multiselectable="true"
            style=${list ? null : cardStyle(s.cardSize)}>
          ${items.map((m) => list ? html`<${ListRow} key=${m.id} m=${m} cols=${cols} selected=${pick.sel.has(m.id)} onPick=${pick.onPick} onMenu=${pick.onMenu} dragIds=${pick.dragIds} />`
            : html`<${Card} key=${m.id} m=${m} selected=${pick.sel.has(m.id)} where=${whereOf(m, place)} onPick=${pick.onPick} onMenu=${pick.onMenu} dragIds=${pick.dragIds} />`)}
        </div>` : null}
        ${!tiles && result && result.total > items.length ? html`<button type="button" class="ghost more-btn" id="show-more" onClick=${() => setLimit(limit + PAGE)}>Show more (${(result.total - items.length).toLocaleString()} left)</button>` : null}
      </div>
    </div>
    <${Inspector} page="browse" open=${!matchMedia("(max-width: 1250px)").matches} tabs=${[
      { id: "details", label: "Details", icon: "info", body: pick.multi ? html`<${SelectionSummary} models=${pick.chosen} />` : selected ? html`<${ModelSummary} key=${selected.id} m=${selected} />`
        : html`<${PlaceSummary} game=${game} cat=${cat} group=${place.group} total=${tiles ? null : result?.total} />` },
    ]} />
  </div>`;
}
