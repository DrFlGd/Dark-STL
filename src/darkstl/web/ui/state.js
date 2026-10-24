// Interface state shared by the islands (sidebar, main view, status bar).
// Preferences persist through the platform's store (the engine keeps them in
// its settings file on this computer).
import { createStore } from "../lib/store.js";

export const ui = createStore({
  ready: false,
  route: "home",           // see context.js
  overview: null,          // { scanning, total, games: [{ name, count, folders, categories }] }
  q: "",                   // search text on the browse pages
  layout: "grid",          // grid | list
  sort: "kind",            // kind (category, group, file) | file (file name)
  lod: false,              // show the low-detail (_l) copies too
  cardSize: 172,
  dialog: null,            // { type: "add-folder" | "confirm" | ..., ... }
  menu: null,              // a right-click or More menu: { x, y, items, from }
  open: {},                // sidebar rows unfolded: { "<game>": true, "<game>/<category>": true }
  jobs: [],                // [{ id, label, started, progress }]
  modelPlace: null,        // { game, category, group } of the model whose page is open
  theme: "system",
  skin: "drafting",
  navOpen: false,          // sidebar drawer on small screens
  navCollapsed: false,
  navPeek: false,
  inspector: {},           // per page: "open" | "folded"
  inspectorTab: {},        // per page: the tab shown
  inspectorWidth: 360,
  catPanel: null,          // the game's category panel beside the menu: open | folded
  catPanelWidth: 230,
  enhance: null,           // the Mesh, Textures and Displace tabs' settings, kept between models
  exportOpts: null,        // the Export tab's settings
  view: null,              // the 3D view's shading and edges
  thumbGen: 0,             // counts the times every preview was let go (a rescan): see thumbs.js
});

let prefs = null;
const KEY = "ds-ui";
const KEPT = ["skin", "theme", "layout", "sort", "lod", "cardSize", "open", "navCollapsed", "inspector", "inspectorTab",
  "inspectorWidth", "catPanel", "catPanelWidth", "enhance", "exportOpts", "view"];

/** Load saved preferences (call once the platform store exists). */
export function initState(store) {
  prefs = store.prefs;
  const saved = prefs.get(KEY, {}) || {};
  const patch = {};
  for (const k of KEPT) if (saved[k] !== undefined && saved[k] !== null) patch[k] = saved[k];
  if (!SKINS.some(([v]) => v === patch.skin)) delete patch.skin;
  if (!THEMES.some(([v]) => v === patch.theme)) delete patch.theme;
  ui.set(patch);
  applySkin();
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);
}

function savePrefs() {
  const s = ui.get();
  prefs?.set(KEY, Object.fromEntries(KEPT.map((k) => [k, s[k]])));
}

export function setPref(patch) {
  ui.set(patch);
  savePrefs();
}

/** The skins, shared with Model Library: data-skin on the page's root, read by styles.css and the icons. */
export const SKINS = [
  ["drafting", "Drafting Sheet"],
  ["partsbin", "Parts Bin"],
  ["layerline", "Layer Line"],
  ["classic", "Classic"],
];
export function applySkin() {
  document.documentElement.dataset.skin = (SKINS.find(([v]) => v === ui.get().skin) || SKINS[0])[0];
}
export function setSkin(skin) {
  setPref({ skin });
  applySkin();
}

export const THEMES = [["system", "Follow the system"], ["light", "Light"], ["dark", "Dark"], ["night", "Night (dim, warm, for a dark workshop)"]];

/** The theme in use: light, dark or night ("system" resolves to light or dark). */
export function resolvedTheme() {
  const t = ui.get().theme;
  if (t === "system") return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  return t;
}
export function applyTheme() {
  document.documentElement.dataset.theme = resolvedTheme();
  window.dispatchEvent(new CustomEvent("ds-theme"));
}
export const nextTheme = (theme) => THEMES[(THEMES.findIndex(([v]) => v === theme) + 1) % THEMES.length][0];
export function cycleTheme() { setTheme(nextTheme(ui.get().theme)); }
export function setTheme(theme) {
  setPref({ theme });
  applyTheme();
}
