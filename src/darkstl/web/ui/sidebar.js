// The menu on the left: Home, each game (with its model count), Add game
// folder…, and settings. The game being browsed shows its categories and their
// groups in a panel beside the menu (as Model Library shows a category's
// subcategories), which folds to a strip; in narrow windows the menu is a
// drawer and the categories unfold in it instead.
import { html, useRef, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { routeHash, placeOf, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { NavHead, ThemeButton, useMedia, useNavFolded } from "./chrome.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { openMenu } from "./actions.js";
import { gameMenu } from "./games.js";

const NARROW = "(max-width: 900px)";

function NavLink({ route, label, icon, count, current, depth = 0, toggle = null, on = null, id = null, menu = null }) {
  const active = on ?? current === route;
  return html`<li class="nav-row" style=${depth ? `--depth: ${depth}` : null}>
    ${toggle || html`<span class="nav-toggle-space"></span>`}
    <a class=${`nav-link${active ? " active" : ""}`} id=${id} href=${routeHash(route)} title=${label} aria-label=${label} aria-current=${current === route ? "page" : null}
      onClick=${() => ui.set({ navOpen: false })} onContextMenu=${menu ? (e) => openMenu(e, menu()) : null}>
      ${typeof icon === "string" ? html`<span class="nav-icon">${Icon[icon](15)}</span>` : icon ? html`<span class="nav-icon">${icon}</span>` : null}<span class="nav-label">${label}</span>
      ${count != null ? html`<span class="nav-count">${Number(count).toLocaleString()}</span>` : null}</a></li>`;
}

const Fold = ({ open, label, onClick }) => html`<button type="button" class="nav-toggle fold-btn" aria-expanded=${open ? "true" : "false"}
  aria-label=${`${open ? "Fold" : "Unfold"} ${label}`} onClick=${onClick}>${Icon.chevron(14)}</button>`;

/** A game's categories and their groups as tree rows. */
function CategoryRows({ game, current, open, depth }) {
  return game.categories.map((c) => {
    const key = `${game.name}/${c.name}`;
    const unfolded = !!open[key];
    const route = placeRoute(game.name, c.name);
    const toggle = c.groups.length > 1 ? html`<${Fold} open=${unfolded} label=${c.name} onClick=${() => setPref({ open: { ...open, [key]: !unfolded } })} />` : null;
    return html`<${NavLink} key=${key} route=${route} label=${c.name} count=${c.count} depth=${depth} current=${current} toggle=${toggle}
        icon=${html`<${CategoryIcon} name=${c.name} size=${15} />`} />
      ${unfolded && c.groups.length > 1 ? c.groups.map((g) => html`<${NavLink} key=${`${key}/${g.name}`} route=${placeRoute(game.name, c.name, g.name)}
        label=${g.name} count=${g.count} depth=${depth + 1} current=${current} />`) : null}`;
  });
}

/** Unfold the way to the category shown, so it's in sight in the tree. */
function useOpenTo(place) {
  const key = place?.category ? `${place.game}/${place.category}` : null;
  useEffect(() => {
    if (!key || !place.group) return;
    const open = ui.get().open;
    if (!open[key]) setPref({ open: { ...open, [key]: true } });
  }, [key, place?.group]);
}

/** The game's categories beside the menu: resizable, folds to a strip with its name. */
function GamePanel({ game, place, route, open }) {
  const prefs = useStore(ui, (s) => [s.catPanel, s.catPanelWidth]);
  const medium = useMedia("(max-width: 1250px)");
  const folded = prefs[0] === "folded" || (prefs[0] == null && medium);
  const width = Math.max(180, Math.min(420, prefs[1] || 230));
  const stopResize = useRef(null);
  useEffect(() => () => stopResize.current?.(), []);
  useOpenTo(place);
  const top = placeRoute(game.name);
  const resize = (e) => {
    e.preventDefault();
    stopResize.current?.();
    const start = e.clientX, before = width;
    const move = (event) => setPref({ catPanelWidth: Math.max(180, Math.min(420, before + event.clientX - start)) });
    const stop = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", stop); removeEventListener("pointercancel", stop); };
    stopResize.current = stop;
    addEventListener("pointermove", move); addEventListener("pointerup", stop); addEventListener("pointercancel", stop);
  };
  if (folded) {
    return html`<aside class="category-panel folded" id="category-panel" aria-label=${`${game.name}'s categories`}>
      <button type="button" class="ghost panel-strip" id="category-panel-open" title="Show categories" aria-label="Show categories" onClick=${() => setPref({ catPanel: "open" })}>
        <span class="panel-strip-icon"><${GameBadge} name=${game.name} /></span><span class="panel-strip-name">${game.name}</span>${Icon.chevronsRight(13)}</button>
    </aside>`;
  }
  return html`<aside class="category-panel" id="category-panel" style=${{ width: `${width}px` }} aria-label=${`${game.name}'s categories`}>
    <header class="cat-panel-head">
      <a class=${`cat-panel-title${route === top ? " active" : ""}`} href=${routeHash(top)} title=${game.name}
        onContextMenu=${(e) => openMenu(e, gameMenu(game))}><${GameBadge} name=${game.name} size=${17} /><span>${game.name}</span>
        <span class="nav-count">${game.count.toLocaleString()}</span></a>
      <button type="button" class="ghost panel-fold" id="category-panel-close" title="Hide categories" aria-label="Hide categories" onClick=${() => setPref({ catPanel: "folded" })}>${Icon.chevronsLeft(14)}</button>
    </header>
    <ul class="nav-list cat-panel-tree" id="category-tree">
      <${CategoryRows} game=${game} current=${route} open=${open} depth=${0} />
    </ul>
    ${!game.categories.length ? html`<p class="muted nav-note">No models found yet.</p>` : null}
    <div class="cat-panel-resize" role="separator" aria-label="Category panel width" aria-orientation="vertical" tabIndex="0" onPointerDown=${resize}></div>
  </aside>`;
}

export function Sidebar() {
  const s = useStore(ui, (st) => ({ route: st.route, navOpen: st.navOpen, overview: st.overview, open: st.open, modelPlace: st.modelPlace }));
  const narrow = useMedia(NARROW);
  const { folded } = useNavFolded();
  const games = s.overview?.games || [];
  const shown = placeOf(s.route) || (s.route.startsWith("model:") ? s.modelPlace : null);
  const panelGame = !narrow && shown ? games.find((g) => g.name === shown.game) : null;
  return html`${s.navOpen ? html`<div class="nav-backdrop" onClick=${() => ui.set({ navOpen: false })}></div>` : null}
    <nav class=${`sidebar${s.navOpen ? " open" : ""}${folded ? " collapsed" : ""}`} aria-label="Library">
      <${NavHead} />
      <ul class="nav-list">
        <${NavLink} route="home" label="Home" icon="home" current=${s.route} />
      </ul>
      <p class="nav-head">Games</p>
      ${games.length ? html`<ul class="nav-list" id="game-list">${games.map((g) => {
        const route = placeRoute(g.name);
        const icon = html`<${GameBadge} name=${g.name} />`;
        if (!narrow) return html`<${NavLink} key=${g.name} route=${route} label=${g.name} icon=${icon} count=${g.count} current=${s.route} on=${shown?.game === g.name} menu=${() => gameMenu(g)} />`;
        const unfolded = s.open[g.name] !== false;
        const toggle = g.categories.length ? html`<${Fold} open=${unfolded} label=${g.name} onClick=${() => setPref({ open: { ...s.open, [g.name]: !unfolded } })} />` : null;
        return html`<${NavLink} key=${g.name} route=${route} label=${g.name} icon=${icon} count=${g.count} current=${s.route} toggle=${toggle} menu=${() => gameMenu(g)} />
          ${unfolded ? html`<${CategoryRows} game=${g} current=${s.route} open=${s.open} depth=${1} />` : null}`;
      })}</ul>` : html`<p class="muted nav-note">${s.overview?.scanning ? "Reading…" : "None yet."}</p>`}
      <button type="button" class="ghost nav-add" id="add-folder" onClick=${() => ui.set({ dialog: { type: "add-folder" }, navOpen: false })}>${Icon.plus(14)} Add game folder…</button>
      <ul class="nav-list nav-foot">
        <${NavLink} route="settings" label="Settings" icon="cog" current=${s.route} />
        <${NavLink} route="about" label="About" icon="info" current=${s.route} />
        <${ThemeButton} />
      </ul>
    </nav>
    ${panelGame ? html`<${GamePanel} key=${panelGame.name} game=${panelGame} place=${shown} route=${s.route} open=${s.open} />` : null}`;
}
