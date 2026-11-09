// A collection's page (collection:<id>), and Starred (starred): the models in it, from any game, as cards or a
// list. A collection keeps its own order: drag a card onto another to put it there (the sort "Collection order",
// with no search). Models whose game isn't in the library now aren't shown until it's back.
import { html, useState, useEffect, useLayoutEffect, useRef } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { collectionOf } from "./context.js";
import { Icon } from "./icons.js";
import { api, plural, toast } from "./api.js";
import { CommandBar, ViewSwitch, SortMenu, SearchNote } from "./layout.js";
import { Inspector } from "./inspector.js";
import { usePageKeys } from "./actions.js";
import { openModel, Card, ListHead, ListRow, listColumns, sortOptions, usePageSort, useModelSelection, CardSize, cardStyle } from "./models.js";
import { ModelSummary } from "./browser.js";
import { SelectionSummary, collectionMenu, removeFrom, starModels, draggedIds, loadCollections } from "./organise.js";

const PAGE = 300;
const VIEWS = [["grid", "Grid", "grid"], ["list", "List", "list"]];

export function CollectionPage({ route }) {
  const cid = collectionOf(route);
  const starred = route === "starred";
  const s = useStore(ui, (st) => ({ layout: st.layout, lod: st.lod, cardSize: st.cardSize, overview: st.overview }));
  const cols = useStore(ui, (st) => st.collections);
  const libRev = useStore(ui, (st) => st.libRev);
  const c = cid ? (cols?.collections || []).find((x) => x.id === cid) || null : null;
  const [sort, setSort] = usePageSort(starred ? "starred" : "collection");
  const by = !starred && sort.by === "kind" && !(ui.get().sorts || {}).collection ? "collection" : sort.by;
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [result, setResult] = useState(null);
  const [limit, setLimit] = useState(PAGE);
  const seq = useRef(0);
  useEffect(() => { if (!cols) loadCollections(); }, []);
  useEffect(() => { const t = setTimeout(() => setQ(text), 150); return () => clearTimeout(t); }, [text]);
  useEffect(() => {
    const n = ++seq.current;
    const args = starred ? { starred: true, lod: true } : { collection: cid };
    api("models_query", { ...args, q, sort: by, desc: sort.desc, limit })
      .then((r) => { if (n === seq.current) setResult({ ...r, q }); }, (e) => { if (n === seq.current) setResult({ total: 0, items: [], q, error: String(e.message || e) }); });
  }, [route, q, by, sort.desc, limit, libRev, s.overview?.total]);
  const items = result?.items || [];
  const extra = c ? [{ sep: true }, { id: "bulk-remove", label: `Take out of ${c.name}`, sub: "they stay in the library", icon: "close", run: () => removeFrom(c, pick.chosen) }]
    : starred ? [] : [];
  const pick = useModelSelection(items, extra);
  const list = s.layout === "list";
  usePageKeys((e) => {
    if (e.key === "Enter") { if (pick.picked) { e.preventDefault(); openModel(pick.picked); } return; }
    if (pick.key(e)) return;
    if ((e.key === "Delete" || e.key === "Backspace") && c && (pick.multi || pick.picked)) { e.preventDefault(); removeFrom(c, pick.multi ? pick.chosen : [pick.picked]); }
  });
  const ordered = c && by === "collection" && !sort.desc && !q;
  const reorder = async (e, before) => {
    const ids = draggedIds(e);
    if (!ids.length || ids.includes(before.id)) return;
    try {
      await api("collection_move", { id: c.id, ids, before: before.id });
      ui.set((st) => ({ libRev: (st.libRev || 0) + 1 }));
      loadCollections();
    } catch (err) { toast(String(err.message || err)); }
  };
  if (cid && cols && !c) {
    return html`<${CommandBar} id="collection-head" title="Collection" /><div class="pages"><p class="warn-note" role="alert">That collection isn't there any more. <a href="#/">Home</a> lists the collections there are.</p></div>`;
  }
  const name = starred ? "Starred" : c?.name || "";
  const title = html`<h1 class="crumbs" title=${name}>${starred ? Icon.star(18, true) : Icon.stack(18)}<span class="crumbs-name" id="collection-name-title">${name}</span></h1>`;
  const search = html`<label class="search small toolbar-search"><span class="visually-hidden">Search</span>
    <input id="search" data-search type="search" value=${text} placeholder=${`Search ${name}…`} autocomplete="off" spellcheck="false"
      title="Search its models' names, in-game names, file names and tags (/)" onInput=${(e) => setText(e.target.value)} /></label>`;
  const cardProps = (m) => ({ m, selected: pick.sel.has(m.id), onPick: pick.onPick, onMenu: pick.onMenu, dragIds: pick.dragIds, onDrop: ordered ? reorder : null });
  return html`<${CommandBar} id="collection-head" title=${title} selection=${pick.bar}
      count=${html`<span class="page-count" id="collection-count">${result ? plural(result.total, "model") : ""}</span>`}
      search=${search}
      tools=${html`${list ? null : html`<${CardSize} />`}<${ViewSwitch} id="layout-switch" views=${VIEWS} value=${list ? "list" : "grid"} onChange=${(v) => setPref({ layout: v })} />
        <${SortMenu} id="sort" options=${sortOptions(true, !starred)} value=${by} onChange=${(v) => setSort({ by: v, desc: false })} />`}
      more=${c ? { id: "collection-more", title: "This collection: rename, delete", items: collectionMenu(c, () => { location.hash = "#/"; }).filter((it) => it.id !== "collection-open") } : null} />
  <div class="browser">
    <div class="browse-main">
      <div class="browse-scroll" id="collection-results">
        <${SearchNote} q=${q} found=${result && result.q === q ? plural(result.total, "model") : ""} onClear=${() => { setText(""); setQ(""); }} />
        ${result?.missing ? html`<p class="muted search-hint" id="collection-missing">${plural(result.missing, "model")} in it ${result.missing === 1 ? "isn't" : "aren't"} in the library now (${result.missing === 1 ? "its" : "their"} game's folder isn't added): ${result.missing === 1 ? "it shows" : "they show"} again when it's back.</p>` : null}
        ${c && items.length > 1 && !ordered ? html`<p class="muted search-hint">Sorted by Collection order (and no search), its models can be dragged into an order of your own.</p>` : null}
        ${result?.error ? html`<p class="warn-note" role="alert">${result.error}</p>` : null}
        ${result && !items.length && !result.error ? html`<div class="empty" id="no-results">${q ? `Nothing in ${name} matches that search.`
          : starred ? "Nothing starred yet. Star a model from its right-click menu, or drag models onto Starred in the menu."
          : "Nothing in this collection yet. Drag models onto it in the menu, or use Add to collection… in a model's right-click menu."}</div>` : null}
        ${list && items.length ? html`<${ListHead} cols=${listColumns(true)} sort=${{ by, desc: sort.desc }} onSort=${setSort} />` : null}
        <div class=${`results ${list ? "list-view lib-list lib-list-all" : "grid-view"}${pick.multi ? " picking" : ""}${ordered ? " ordered" : ""}`} role="listbox"
            aria-label=${`Models in ${name}`} aria-multiselectable="true" style=${list ? null : cardStyle(s.cardSize)}>
          ${items.map((m) => list ? html`<${ListRow} key=${m.id} ...${cardProps(m)} cols=${listColumns(true)} />`
            : html`<${Card} key=${m.id} ...${cardProps(m)} game=${true} where=${`${m.category}${m.group ? ` › ${m.group}` : ""}`} />`)}
        </div>
        ${result && result.total > items.length ? html`<button type="button" class="ghost more-btn" id="show-more" onClick=${() => setLimit(limit + PAGE)}>Show more (${(result.total - items.length).toLocaleString()} left)</button>` : null}
      </div>
    </div>
    <${Inspector} page="collection" open=${!matchMedia("(max-width: 1250px)").matches} tabs=${[
      { id: "details", label: "Details", icon: "info", body: pick.multi ? html`<${SelectionSummary} models=${pick.chosen} extra=${extra} />`
        : pick.picked ? html`<${ModelSummary} key=${pick.picked.id} m=${pick.picked} />`
        : html`<div class="place-summary" id="place-summary"><section class="insp-section"><h3>${name}</h3>
          <p>${result ? plural(result.total, "model") : "…"}${c?.games?.length ? ` from ${c.games.join(", ")}` : ""}.</p>
          <p class="muted">${starred ? "The models you starred, in every game." : "A list of your own: its models stay where they are in the library."}
            Click a model to see it here; double-click (or Enter) opens it. Ctrl+click or Shift+click chooses several, to do something to them all.</p>
          ${starred && items.length ? html`<div class="insp-actions"><button type="button" class="ghost" id="unstar-all" title="Take the star off every model here" onClick=${() => starModels(items, false)}>${Icon.star(14)} Take every star off</button></div>` : null}
          </section></div>` },
    ]} />
  </div>`;
}
