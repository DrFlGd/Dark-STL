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
  sort: "kind",            // kind (category, group, file) | file (file name): the sort before 0.44, as a fallback
  sorts: {},               // per page (its route, or "search"): { by, desc } (see models.js usePageSort)
  recent: [],              // the ids of the models opened last, newest first (Home's Recent row)
  searchQ: "",             // the search across every game (route "search")
  searchFilters: {},       // its filters: { games, kinds, animated, textured, tris, collected }
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
  catPanelModel: null,     // the same beside a model, when it was opened or folded there by hand (else folded under 1440 px)
  tipsOpen: null,          // per model page tab: its explanation open (true) or folded (false); unset: open until its job has run
  enhance: null,           // the Mesh, Textures and Displace tabs' settings, kept between models
  exportOpts: null,        // the Export tab's settings
  texturePreview: null,    // { sid, key }: the Textures tab's chosen texture draped over every part of the model
  view: null,              // the 3D view's shading and edges
  cleanPlans: null,        // per model id: its Clean tab steps [{ op, ... }] (the last 20 models)
  configChoices: null,     // per configurable model id: the part chosen in each slot { slot: option }
  configEdit: null,        // the configurable model whose configuration is being edited (Configure tab)
  configRev: 0,            // counts the configurations saved, so the model page opens its model again
  libRev: 0,               // counts the user's renames and moves, so the lists are read again
  thumbGen: 0,             // counts the times every preview was let go (a rescan): see thumbs.js
});

let prefs = null;
const KEY = "ds-ui";
const KEPT = ["skin", "theme", "layout", "sort", "sorts", "recent", "lod", "cardSize", "open", "navCollapsed", "inspector", "inspectorTab",
  "inspectorWidth", "catPanel", "catPanelWidth", "catPanelModel", "tipsOpen", "enhance", "exportOpts", "view", "cleanPlans", "configChoices",
  "showBones"];
/** Finish, Cut and the painted 3MF moved to Model Painter in 0.55.0: their folded explanations, and a painted 3MF
 *  chosen as the export format with its paint settings, are let go (their own settings aren't KEPT, so they go
 *  with the next save); a model page last left on Finish or Cut opens on Export, the way across to Model Painter. */
const RETIRED_TIPS = ["finish", "cut", "paint"];
function withoutRetired(patch) {
  if (["finish", "cut"].includes(patch.inspectorTab?.model)) patch.inspectorTab = { ...patch.inspectorTab, model: "export" };
  if (patch.exportOpts && typeof patch.exportOpts === "object") {
    const { paint: _paint, ...rest } = patch.exportOpts;
    if (String(rest.format || "").toLowerCase() === ".3mf") delete rest.format;
    patch.exportOpts = rest;
  }
  if (patch.tipsOpen && typeof patch.tipsOpen === "object") {
    patch.tipsOpen = Object.fromEntries(Object.entries(patch.tipsOpen).filter(([k]) => !RETIRED_TIPS.includes(k)));
  }
  return patch;
}

/** Load saved preferences (call once the platform store exists). */
export function initState(store) {
  prefs = store.prefs;
  const saved = prefs.get(KEY, {}) || {};
  const patch = {};
  for (const k of KEPT) if (saved[k] !== undefined && saved[k] !== null) patch[k] = saved[k];
  withoutRetired(patch);
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

const RECENT = 8;

/** Remember that a library model was opened, for Home's Recent row (the last 8). */
export function rememberOpened(id) {
  if (!id) return;
  const was = ui.get().recent || [];
  if (was[0] === id) return;
  setPref({ recent: [id, ...was.filter((x) => x !== id)].slice(0, RECENT) });
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
