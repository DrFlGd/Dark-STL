// Inline stroke icons (no icon font, no network).
import { html } from "../lib/html.js";

// Line weight, ends and corners come from the skin (--icon-stroke and friends in styles.css).
const svg = (d, size = 16, fill = "none") => html`<svg class="icon" width=${size} height=${size} viewBox="0 0 24 24" fill=${fill} stroke="currentColor"
  stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

// Icons drawn for each skin (docs/PLAN.md, "Skins design"): every drawing is there
// and the skin in use shows its own, so changing skin needs no new render.
const skinned = (classic, own) => html`<g data-sk="classic">${classic}</g>${Object.entries(own).map(([sk, d]) => html`<g data-sk=${sk}>${d}</g>`)}`;
const OWN = {
  home: {
    partsbin: html`<path d="M3 11 12 4l9 7" /><path d="M5.5 9.5V20h13V9.5" /><path d="M10 14h4v6h-4z" fill="currentColor" stroke="none" />`,
    drafting: html`<path d="M3.5 11.5 12 4.5l8.5 7" /><path d="M6 9.5V20h12V9.5" /><path d="M10 20v-5h4v5" /><path d="M2 20h20" stroke-dasharray="1.5 1.5" />`,
    layerline: html`<path d="M4 11.5 12 5l8 6.5V19a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1z" /><path d="M10 20v-4.5h4V20" />`,
  },
  inbox: {
    partsbin: html`<path d="M12 3v10" /><path d="M7.5 9 12 13.5 16.5 9" /><path d="M3.5 15.5V20h17v-4.5" />`,
    drafting: html`<path d="M12 3v11" /><path d="M8.5 10.5 12 14l3.5-3.5" /><path d="M4 16v4h16v-4" /><path d="M2 16h4M18 16h4" />`,
    layerline: html`<path d="M12 4v10M8 10.5l4 4 4-4M5 16.5V19h14v-2.5" />`,
  },
  layers: {
    partsbin: html`<path d="M3 3.5h18V11H3zM3 13h18v7.5H3z" /><path d="M9.5 7.25h5M9.5 16.75h5" />`,
    drafting: html`<path d="M12 4 21 8.5 12 13 3 8.5z" /><path d="M3 12.5 12 17l9-4.5" /><path d="M3 16.5 12 21l9-4.5" stroke-dasharray="2 1.5" />`,
    layerline: html`<path d="M6 6.5h12M4.5 10.5h15M4.5 14.5h15M6 18.5h12" />`,
  },
  star: {
    partsbin: html`<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.8L12 16.8l-5.3 2.8 1.1-5.8-4.3-4.1 5.9-.8z" />`,
    drafting: html`<path d="M12 3.5l2.6 5.4 5.9.8-4.3 4.1 1.1 5.8L12 16.8l-5.3 2.8 1.1-5.8-4.3-4.1 5.9-.8z" /><path d="M11 12.5h2M12 11.5v2" />`,
    layerline: html`<path d="M12 4.2l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.6l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z" />`,
  },
  folder: {
    partsbin: html`<path d="M3 5h6.5l2 2.5H21V19H3z" /><path d="M3 10.5h18" />`,
    drafting: html`<path d="M3 6h6l2 2h10v11H3z" /><path d="M3 11h18" stroke-dasharray="1.5 1.5" />`,
    layerline: html`<path d="M4 7.5A1.5 1.5 0 0 1 5.5 6H9l2 2h7.5A1.5 1.5 0 0 1 20 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z" />`,
  },
  box: {
    partsbin: html`<path d="M12 3 3.5 7.5 12 12l8.5-4.5z" fill="currentColor" /><path d="M3.5 7.5v9L12 21l8.5-4.5v-9M12 12v9" />`,
    drafting: html`<path d="M12 3l8.5 4.5v9L12 21l-8.5-4.5v-9z" /><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9" /><circle cx="12" cy="12" r="1.1" />`,
    layerline: html`<path d="M12 4 19.5 8v8L12 20l-7.5-4V8z" /><path d="M4.5 8 12 12l7.5-4M12 12v8" />`,
  },
  cog: {
    partsbin: html`<path d="M12 2.8l8 4.6v9.2l-8 4.6-8-4.6V7.4z" /><circle cx="12" cy="12" r="3.2" />`,
    drafting: html`<circle cx="12" cy="12" r="6.5" /><circle cx="12" cy="12" r="2.5" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" />`,
    layerline: html`<circle cx="12" cy="12" r="7.5" /><circle cx="12" cy="12" r="2.2" /><path d="M12 4.5v3" />`,
  },
  search: {
    partsbin: html`<circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.8 15.8 20.5 20.5" />`,
    drafting: html`<circle cx="10.5" cy="10.5" r="6.5" /><path d="M15.2 15.2 21 21" /><path d="M8 10.5h5M10.5 8v5" />`,
    layerline: html`<circle cx="10.5" cy="10.5" r="6" /><path d="M15 15l4.5 4.5" />`,
  },
};


export const Icon = {
  info: (s) => svg(html`<circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7.5v.01" />`, s),
  search: (s) => svg(skinned(html`<circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" />`, OWN.search), s),
  grid: (s) => svg(html`<rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" />`, s),
  list: (s) => svg(html`<rect x="3" y="4" width="5" height="4" rx="1" /><rect x="3" y="10" width="5" height="4" rx="1" /><rect x="3" y="16" width="5" height="4" rx="1" /><path d="M11 6h10M11 12h10M11 18h10" />`, s),
  table: (s) => svg(html`<rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M3 9h18M3 14h18M9 9v11" />`, s),
  grouped: (s) => svg(html`<path d="M3 5h8M3 12h8M3 19h8" /><rect x="14" y="3" width="7" height="4" rx="2" /><rect x="14" y="10" width="7" height="4" rx="2" /><rect x="14" y="17" width="7" height="4" rx="2" />`, s),
  star: (s, filled) => svg(skinned(html`<path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.7l5.9-.9z" />`, OWN.star), s, filled ? "currentColor" : "none"),
  eye: (s) => svg(html`<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" />`, s),
  moon: (s) => svg(html`<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />`, s),
  sun: (s) => svg(html`<circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />`, s),
  auto: (s) => html`<svg class="icon" width=${s || 16} height=${s || 16} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" aria-hidden="true"><circle cx="12" cy="12" r="8.5" /><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor" /></svg>`,
  check: (s) => svg(html`<path d="M5 12.5l4.5 4.5L19 7.5" />`, s),
  sort: (s) => svg(html`<path d="M7 4v16M4 17l3 3 3-3M14 6h7M14 12h5M14 18h3" />`, s),
  more: (s) => svg(html`<circle cx="5" cy="12" r="1.2" /><circle cx="12" cy="12" r="1.2" /><circle cx="19" cy="12" r="1.2" />`, s),
  copy: (s) => svg(html`<rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3" />`, s),
  trash: (s) => svg(html`<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" />`, s),
  external: (s) => svg(html`<path d="M14 4h6v6M20 4l-9 9" /><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />`, s),
  night: (s) => svg(html`<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /><path d="M17 3l.6 1.4L19 5l-1.4.6L17 7l-.6-1.4L15 5l1.4-.6z" />`, s),
  menu: (s) => svg(html`<path d="M4 6h16M4 12h16M4 18h16" />`, s),
  close: (s) => svg(html`<path d="M6 6l12 12M18 6L6 18" />`, s),
  chevron: (s) => svg(html`<path d="M9 6l6 6-6 6" />`, s),
  book: (s) => svg(html`<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z" /><path d="M4 21V5" />`, s),
  file: (s) => svg(html`<path d="M14 3H6v18h12V7z" /><path d="M14 3v4h4" />`, s),
  chevronsLeft: (s) => svg(html`<path d="M11 17l-5-5 5-5M18 17l-5-5 5-5" />`, s),
  chevronsRight: (s) => svg(html`<path d="M13 17l5-5-5-5M6 17l5-5-5-5" />`, s),
  panel: (s) => svg(html`<rect x="3" y="4" width="18" height="16" rx="1.5" /><path d="M15 4v16" />`, s),
  home: (s) => svg(skinned(html`<path d="M3 11l9-7 9 7" /><path d="M5 10v10h14V10" />`, OWN.home), s),
  lock: (s) => svg(html`<rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" />`, s),
  clock: (s) => svg(html`<circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" />`, s),
  box: (s) => svg(skinned(html`<path d="M3 7l9-4 9 4-9 4-9-4z" /><path d="M3 7v10l9 4 9-4V7" /><path d="M12 11v10" />`, OWN.box), s),
  alert: (s) => svg(html`<path d="M12 3l10 18H2z" /><path d="M12 10v5M12 18v.5" />`, s),
  gear: (s) => svg(html`<circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />`, s),
  stack: (s) => svg(html`<rect x="6" y="3" width="15" height="15" rx="2" /><path d="M3 7v12a2 2 0 0 0 2 2h12" />`, s),
  plus: (s) => svg(html`<path d="M12 5v14M5 12h14" />`, s),
  edit: (s) => svg(html`<path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M14 6l4 4" />`, s),
  download: (s) => svg(html`<path d="M12 4v11M7 10l5 5 5-5M5 20h14" />`, s),
  cog: (s) => svg(skinned(html`<circle cx="12" cy="12" r="2.5" /><path d="M10.5 3h3l.5 2.6 1.9.8 2.2-1.5 2.1 2.1-1.5 2.2.8 1.9 2.5.4v3l-2.5.5-.8 1.9 1.5 2.2-2.1 2.1-2.2-1.5-1.9.8-.5 2.5h-3l-.5-2.5-1.9-.8-2.2 1.5-2.1-2.1 1.5-2.2-.8-1.9L3 13.5v-3l2.6-.5.8-1.9-1.5-2.2 2.1-2.1 2.2 1.5 1.9-.8z" />`, OWN.cog), s),
  pin: (s) => svg(html`<path d="M9 3h6l-1 6 3 3v2H7v-2l3-3z" /><path d="M12 14v7" />`, s),
  archive: (s) => svg(html`<rect x="4" y="3" width="16" height="18" rx="2" /><path d="M12 3v2M12 7v2M12 11v2" /><rect x="10" y="14" width="4" height="4" rx="1" />`, s),
  folder: (s) => svg(skinned(html`<path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />`, OWN.folder), s),
  refresh: (s) => svg(html`<path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" />`, s),
  image: (s) => svg(html`<rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="2" /><path d="M21 16l-5-5-8 8" />`, s),
  inbox: (s) => svg(skinned(html`<path d="M3 13l3-8h12l3 8v6H3z" /><path d="M3 13h5l1 2h6l1-2h5" />`, OWN.inbox), s),
  move: (s) => svg(html`<path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M9 13h7M13 10l3 3-3 3" />`, s),
  layers: (s) => svg(skinned(html`<path d="M3 8.5l9-4.5 9 4.5-9 4.5z" /><path d="M3 12.5l9 4.5 9-4.5" /><path d="M3 16.5l9 4.5 9-4.5" />`, OWN.layers), s),
  code: (s) => svg(html`<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" />`, s),
};
