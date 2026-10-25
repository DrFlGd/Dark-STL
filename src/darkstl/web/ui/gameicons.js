// Icons for games (a short badge) and for the kinds of model in them.
import { html } from "../lib/html.js";
import { Icon } from "./icons.js";

const SHORT = {
  "Dark Souls": "DS1", "Dark Souls Remastered": "DSR", "Dark Souls II": "DS2", "Dark Souls III": "DS3",
  "Bloodborne": "BB", "Sekiro": "SEK", "Elden Ring": "ER", "Cyberpunk 2077": "CP77", "Other models": "3D",
};

export const gameShort = (name) => SHORT[name] || (name || "?").split(/\s+/).filter(Boolean).map((w) => w[0]).join("").slice(0, 3).toUpperCase();

/** A game's badge: its initials in a small plate. */
export function GameBadge({ name, size = 16 }) {
  return html`<span class="game-badge" style=${{ "--badge": `${size}px` }} aria-hidden="true">${gameShort(name)}</span>`;
}

const svg = (d, size = 16) => html`<svg class="icon" width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

const DRAW = {
  Weapons: html`<path d="M19.5 3.5 21 5 10.5 15.5 8.5 13.5z" /><path d="M6.5 13.5l4 4M8 17l-3.5 3.5M5.5 15.5 3 18" />`,
  Armor: html`<path d="M12 3c-3.5 0-6.5 2.7-6.5 7v4.5L8 17h8l2.5-2.5V10c0-4.3-3-7-6.5-7z" /><path d="M8.5 11.5h7M12 11.5V17" /><path d="M9 20h6" />`,
  Characters: html`<circle cx="12" cy="6.5" r="3" /><path d="M6 21v-5a6 6 0 0 1 12 0v5" /><path d="M9.5 21v-4M14.5 21v-4" />`,
  "Faces & Hair": html`<path d="M7 10a5 5 0 0 1 10 0v3a5 5 0 0 1-10 0z" /><path d="M7 10c2-1 4.5-3 5-5 .5 2 3 4 5 5" /><path d="M10 12.5h.01M14 12.5h.01" />`,
  Objects: null,
  "Map Pieces": html`<path d="M3.5 6.5 9 4l6 2.5L20.5 4v13.5L15 20l-6-2.5-5.5 2.5z" /><path d="M9 4v13.5M15 6.5V20" />`,
  Models: null,
};

/** The icon of a kind of model (a category). */
export function CategoryIcon({ name, size = 16 }) {
  const d = DRAW[name];
  if (d) return svg(d, size);
  if (name === "Objects" || name === "Models") return Icon.box(size);
  return Icon.file(size);
}
