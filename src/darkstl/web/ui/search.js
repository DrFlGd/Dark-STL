// The search across every game (route "search", its words in the hash:
// #/search?q=…): one box that looks through every model's name, its name in
// the game, its file name and its kind and group, in every game of the
// library. Results come grouped by game, as cards or as a list with columns.
// Chips narrow them down: by game, by kind, to models with animations or
// textures, by how many triangles they have, collected or not.
import { html, useState, useEffect, useLayoutEffect, useRef } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { searchHash } from "./context.js";
import { Icon } from "./icons.js";
import { api, plural } from "./api.js";
import { CommandBar, ViewSwitch, SortMenu } from "./layout.js";
import { Inspector } from "./inspector.js";
import { openMenu, usePageKeys } from "./actions.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { openModel, Card, ListHead, ListRow, listColumns, sortOptions, usePageSort } from "./models.js";
import { ModelSummary } from "./browser.js";

const PAGE = 300;
const VIEWS = [["grid", "Grid", "grid"], ["list", "List", "list"]];
/** Triangle ranges offered: [id, label, min, max]. */
export const TRI_RANGES = [
  ["any", "Any number of triangles", null, null],
  ["lt10k", "Under 10,000 triangles", null, 9999],
  ["10k-50k", "10,000 to 50,000 triangles", 10000, 50000],
  ["50k-200k", "50,000 to 200,000 triangles", 50000, 200000],
  ["gt200k", "Over 200,000 triangles", 200001, null],
];
const COLLECTED = [["any", "Collected or not"], ["yes", "Collected into the app"], ["no", "Not collected yet"]];

const toggled = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);

/** The query the filters make (see the engine's models_query). */
function filterArgs(f) {
  const tri = TRI_RANGES.find(([id]) => id === f.tris) || TRI_RANGES[0];
  return {
    games: f.games || [], kinds: f.kinds || [],
    animated: f.animated ? true : null, textured: f.textured ? true : null,
    tri_min: tri[2], tri_max: tri[3],
    collected: f.collected === "yes" ? true : f.collected === "no" ? false : null,
  };
}

const anyFilter = (f) => !!((f.games || []).length || (f.kinds || []).length || f.animated || f.textured || (f.tris && f.tris !== "any") || (f.collected && f.collected !== "any"));

function Chip({ id, on, label, count, title, onClick, icon }) {
  return html`<button type="button" class=${`chip-btn${on ? " on" : ""}`} id=${id} aria-pressed=${on ? "true" : "false"} title=${title || label} onClick=${onClick}>
    ${icon || null}<span class="chip-name">${label}</span>${count != null ? html` <span class="muted">${count.toLocaleString()}</span>` : null}</button>`;
}

function MenuChip({ id, options, value, onChange, title }) {
  const cur = options.find(([k]) => k === value) || options[0];
  const on = cur[0] !== options[0][0];
  const items = () => options.map(([k, l]) => ({ id: `${id}-${k}`, label: l, icon: k === cur[0] ? "check" : null, run: () => onChange(k) }));
  return html`<button type="button" class=${`chip-btn menu-chip${on ? " on" : ""}`} id=${id} aria-haspopup="menu" title=${title}
    onClick=${(e) => openMenu(e.currentTarget, items())}><span class="chip-name">${cur[1]}</span> ▾</button>`;
}

/** The chips that narrow the search down. */
function Filters({ f, setF, games, facets }) {
  const gameCounts = new Map((facets?.games || []).map((g) => [g.name, g.count]));
  const kinds = facets?.kinds || [];
  const chosenKinds = (f.kinds || []).filter((k) => !kinds.some((x) => x.name === k)).map((k) => ({ name: k, count: 0 }));
  return html`<div class="search-filters" id="search-filters" aria-label="Narrow the search">
    <div class="filters filter-row" id="filter-games" aria-label="Games">
      <${Chip} id="filter-game-all" on=${!(f.games || []).length} label="Every game" onClick=${() => setF({ games: [] })} title="Search every game in the library" />
      ${games.map((g) => html`<${Chip} key=${g} on=${(f.games || []).includes(g)} label=${g} count=${gameCounts.get(g) || 0}
        icon=${html`<${GameBadge} name=${g} size=${13} />`} title=${`Only ${g} (click again to leave it out of the filter)`} onClick=${() => setF({ games: toggled(f.games || [], g) })} />`)}
    </div>
    <div class="filters filter-row" id="filter-kinds" aria-label="Kinds">
      ${[...kinds, ...chosenKinds].map((k) => html`<${Chip} key=${k.name} on=${(f.kinds || []).includes(k.name)} label=${k.name} count=${k.count}
        icon=${html`<${CategoryIcon} name=${k.name} size=${13} />`} title=${`Only ${k.name.toLowerCase()} (click again to leave them out of the filter)`} onClick=${() => setF({ kinds: toggled(f.kinds || [], k.name) })} />`)}
      <span class="filter-gap"></span>
      <${Chip} id="filter-animated" on=${!!f.animated} label="Has animations" title="Only models that come with animations to pose them with" onClick=${() => setF({ animated: !f.animated })} />
      <${Chip} id="filter-textured" on=${!!f.textured} label="Has textures" title="Only models that come with textures" onClick=${() => setF({ textured: !f.textured })} />
      <${MenuChip} id="filter-tris" options=${TRI_RANGES} value=${f.tris || "any"} title="How many triangles the model has" onChange=${(v) => setF({ tris: v })} />
      <${MenuChip} id="filter-collected" options=${COLLECTED} value=${f.collected || "any"} title="Whether the model is a copy collected into the app, which opens without the game" onChange=${(v) => setF({ collected: v })} />
      ${anyFilter(f) ? html`<button type="button" class="ghost compact" id="filter-clear" title="Clear every filter" onClick=${() => ui.set({ searchFilters: {} })}>${Icon.close(12)} Clear filters</button>` : null}
    </div>
  </div>`;
}

export function SearchPage() {
  const s = useStore(ui, (st) => ({ overview: st.overview, q: st.searchQ, layout: st.layout, lod: st.lod, f: st.searchFilters || {} }));
  const libRev = useStore(ui, (st) => st.libRev);
  const [sort, setSort] = usePageSort("search");
  const [text, setText] = useState(s.q);
  const [result, setResult] = useState(null);
  const [limit, setLimit] = useState(PAGE);
  const [picked, setPicked] = useState(null);
  const seq = useRef(0);
  const box = useRef(null);
  const list = s.layout === "list";
  const fKey = JSON.stringify(s.f);
  useLayoutEffect(() => { setText(s.q); }, [s.q]);
  useEffect(() => { box.current?.focus(); }, []);
  useEffect(() => { setLimit(PAGE); }, [s.q, sort.by, sort.desc, s.lod, fKey]);
  useEffect(() => {
    if (text === s.q) return;
    const t = setTimeout(() => { history.replaceState(null, "", searchHash(text)); ui.set({ searchQ: text }); }, 200);
    return () => clearTimeout(t);
  }, [text]);
  useEffect(() => {
    if (!s.overview) return;
    const n = ++seq.current;
    const q = s.q;
    api("models_query", { q, lod: s.lod, sort: sort.by, desc: sort.desc, by_game: !list, facets: true, limit, ...filterArgs(s.f) })
      .then((r) => { if (n === seq.current) setResult({ ...r, q }); }, (e) => { if (n === seq.current) setResult({ total: 0, items: [], q, error: String(e.message || e) }); });
  }, [s.q, s.lod, sort.by, sort.desc, list, limit, fKey, s.overview?.total, libRev]);
  const items = result?.items || [];
  const selected = items.find((m) => m.id === picked) || null;
  usePageKeys((e) => {
    const ids = items.map((m) => m.id);
    if (e.key === "Enter") { if (selected) { e.preventDefault(); openModel(selected); } return; }
    if (e.key === "Escape") { setPicked(null); return; }
    if (!e.key.startsWith("Arrow") || !ids.length || e.ctrlKey || e.metaKey || e.altKey) return;
    const d = { ArrowRight: list ? 0 : 1, ArrowLeft: list ? 0 : -1, ArrowDown: 1, ArrowUp: -1 }[e.key];
    if (!d) return;
    e.preventDefault();
    const cur = ids.indexOf(picked);
    const id = ids[cur < 0 ? 0 : Math.max(0, Math.min(ids.length - 1, cur + d))];
    setPicked(id);
    document.querySelector(`.results [data-model="${CSS.escape(id)}"]`)?.focus();
  });
  const games = (s.overview?.games || []).map((g) => g.name);
  const setF = (patch) => ui.set({ searchFilters: { ...s.f, ...patch } });
  const search = html`<label class="search small toolbar-search"><span class="visually-hidden">Search every game</span>
    <input id="search" ref=${box} data-search type="search" value=${text} placeholder="Search every game…" autocomplete="off" spellcheck="false"
      title="Search every game's models by name, name in the game, file name, kind or group (/)" onInput=${(e) => setText(e.target.value)} /></label>`;
  const cols = listColumns(true);
  const fresh = result && result.q === s.q ? result : null;
  const counts = new Map((result?.facets?.games || []).map((g) => [g.name, g.count]));
  // grouped by game: a heading where each game's models start (counts: every model of it found)
  const groups = [];
  if (!list) for (const m of items) {
    if (!groups.length || groups[groups.length - 1].game !== m.game) groups.push({ game: m.game, items: [] });
    groups[groups.length - 1].items.push(m);
  }
  const statsFilter = s.f.animated || s.f.textured || (s.f.tris && s.f.tris !== "any") || ["size", "triangles", "parts", "textures", "animated"].includes(sort.by);
  return html`<${CommandBar} id="search-head" title=${html`<h1 class="crumbs">${Icon.search(18)}<span class="crumbs-name">Search every game</span></h1>`}
      count=${html`<span class="page-count" id="search-count">${fresh ? plural(fresh.total, "model") : ""}</span>`}
      search=${search}
      tools=${html`<${ViewSwitch} id="layout-switch" views=${VIEWS} value=${list ? "list" : "grid"} onChange=${(v) => setPref({ layout: v })} />
        <${SortMenu} id="sort" options=${sortOptions(true)} value=${sort.by} onChange=${(v) => setSort({ by: v, desc: false })} />`} />
  <div class="browser">
    <div class="browse-main">
      <${Filters} f=${s.f} setF=${setF} games=${games} facets=${result?.facets} />
      <div class="browse-scroll" id="search-results">
        ${!s.q ? html`<p class="muted search-hint" id="search-hint">Type a name, a name from the game or a file name: every game in the library is searched.
          ${anyFilter(s.f) ? " The filters above narrow it down." : " Until then, every model is listed."}</p>` : null}
        ${fresh && statsFilter && fresh.unknown ? html`<p class="muted search-hint" id="search-unknown" role="status">Sizes and counts aren't known yet for ${plural(fresh.unknown, "of these models", "of these models")}: they're read in the background, and when a model's preview is drawn or it's opened.</p>` : null}
        ${result?.error ? html`<p class="warn-note" role="alert">${result.error}</p>` : null}
        ${fresh && !items.length && !result.error ? html`<div class="empty" id="no-results">${s.q ? `Nothing in the library matches “${s.q}”${anyFilter(s.f) ? " with these filters" : ""}.` : "No models match these filters."}</div>` : null}
        ${list ? html`${items.length ? html`<${ListHead} cols=${cols} sort=${sort} onSort=${setSort} />` : null}
          <div class="results list-view lib-list lib-list-all" role="listbox" aria-label="Models found">
            ${items.map((m) => html`<${ListRow} key=${m.id} m=${m} cols=${cols} selected=${picked === m.id} onPick=${(x) => setPicked(x.id)} />`)}</div>`
        : groups.map((g) => html`<section class="search-group" key=${g.game} data-game=${g.game}>
            <h2 class="search-game-head"><${GameBadge} name=${g.game} size=${16} /><span>${g.game}</span><span class="muted">${plural(counts.get(g.game) ?? g.items.length, "model")}</span></h2>
            <div class="results grid-view" role="listbox" aria-label=${`Models found in ${g.game}`}>
              ${g.items.map((m) => html`<${Card} key=${m.id} m=${m} selected=${picked === m.id} where=${`${m.category}${m.group ? ` › ${m.group}` : ""}`} onPick=${(x) => setPicked(x.id)} />`)}</div>
          </section>`)}
        ${result && result.total > items.length ? html`<button type="button" class="ghost more-btn" id="show-more" onClick=${() => setLimit(limit + PAGE)}>Show more (${(result.total - items.length).toLocaleString()} left)</button>` : null}
      </div>
    </div>
    <${Inspector} page="search" open=${!matchMedia("(max-width: 1250px)").matches} tabs=${[
      { id: "details", label: "Details", icon: "info", body: selected ? html`<${ModelSummary} key=${selected.id} m=${selected} />`
        : html`<div class="place-summary" id="place-summary"><section class="insp-section"><h3>Search every game</h3>
          <p>${fresh ? plural(fresh.total, "model") : "…"} found${s.q ? html` for <b>${s.q}</b>` : ""}.</p>
          <p class="muted">Click a model to see it here; double-click (or Enter) opens it.</p></section></div>` },
    ]} />
  </div>`;
}
