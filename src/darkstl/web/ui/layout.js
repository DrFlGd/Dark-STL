// One page layout, from Model Library: the command bar every page starts with,
// the toolbar's view switcher and sort menu, the note that says a search is on,
// and the box the details panels edit in.
import { html, useState, useLayoutEffect } from "../lib/html.js";
import { Icon } from "./icons.js";
import { openMenu } from "./actions.js";
import { ui } from "./state.js";

/** The command bar every page starts with (docs/PLAN.md, "Inspector rails
 *  design"): one row with fixed places, left to right: ‹ Back (`back`), where
 *  you are (`up`: [[name, href]] above, then `title`, its `count`), the page's
 *  `search`, its view switches and Sort (`tools`), one main button (`primary`)
 *  and ⋯ (`more`: { id, items }). With `selection` ({ count, onClear, items })
 *  the same bar shows what's selected, its first actions as buttons and the
 *  rest under ⋯; ✕ (or Esc) clears it. The search, view and sort stay, so they
 *  can be changed with something selected. */
export function CommandBar({ id, back, up, title, count, search, tools, primary, more, selection }) {
  if (selection) return html`<header class="cmdbar selecting" id=${id} data-selecting="1">
    <${Burger} />
    <button type="button" class="ghost bar-clear" id="bar-clear" title="Clear the selection (Esc)" aria-label="Clear the selection (Esc)" onClick=${selection.onClear}>${Icon.close(15)}</button>
    <div class="bar-place"><strong class="bar-count" id="bar-count">${selection.count}</strong>${search ? null : html`<span class="muted bar-hint">Esc also clears</span>`}</div>
    <span class="bar-spacer"></span>
    ${search ? html`<div class="bar-search">${search}</div>` : null}
    ${tools || null}
    <${BarActions} items=${selection.items} buttons=${selection.buttons ?? 2} />
  </header>`;
  return html`<header class="cmdbar" id=${id}>
    <${Burger} />
    ${back ? html`<button type="button" class="ghost bar-back" id=${back.id || "bar-back"} title="Back (Alt+←)" aria-label="Back" onClick=${back.run}>‹ Back</button>` : null}
    <div class="bar-place">
      ${up?.length ? html`<span class="bar-up" id="crumbs-up">${up.map(([name, href], i) => html`<a key=${i} href=${href}>${name}</a><span class="crumb-sep" aria-hidden="true">›</span>`)}</span>` : null}
      ${typeof title === "string" ? html`<h1>${title}</h1>` : title}
      ${count == null ? null : typeof count === "string" || typeof count === "number" ? html`<span class="page-count">${count}</span>` : count}
    </div>
    <span class="bar-spacer"></span>
    ${search ? html`<div class="bar-search">${search}</div>` : null}
    ${tools || null}
    ${primary || null}
    ${more ? html`<${MoreButton} id=${more.id} items=${more.items} title=${more.title} />` : null}
  </header>`;
}

/** The menu button, in the bar only while the menu is a drawer (narrow windows). */
function Burger() {
  return html`<button type="button" class="ghost bar-burger" aria-label="Menu" title="Menu"
    onClick=${() => ui.set({ navOpen: !ui.get().navOpen, mobilePanel: null })}>${Icon.menu(17)}</button>`;
}

/** ⋯: a page's other actions. */
export function MoreButton({ id = "bar-more", items, title = "More actions", label }) {
  return html`<button type="button" class="ghost more-btn-bar" id=${id} aria-haspopup="menu" title=${title} aria-label=${label || title}
    onClick=${(e) => openMenu(e.currentTarget, typeof items === "function" ? items() : items)}>${Icon.more(16)}${label ? html`<span>${label}</span>` : null}</button>`;
}

/** The first `buttons` actions (menu items) as buttons, the rest under ⋯. */
function BarActions({ items, buttons }) {
  const room = useRoom();
  buttons = Math.min(buttons, room);
  const list = (typeof items === "function" ? items() : items) || [];
  const real = list.filter((it) => !it.sep && !it.head);
  const shown = real.slice(0, buttons);
  const rest = list.filter((it) => !shown.includes(it));
  while (rest.length && (rest[0].sep || rest[rest.length - 1]?.sep)) rest[0].sep ? rest.shift() : rest.pop();
  return html`${shown.map((it, i) => html`<button type="button" key=${it.id} id=${`bar-${it.id}`} data-action=${it.id} class=${i === 0 ? "primary compact" : "ghost"}
      disabled=${!!it.disabled} title=${typeof it.disabled === "string" ? it.disabled : it.key ? `${it.label.replace(/…$/, "")} (${it.key})` : it.label}
      onClick=${() => it.run()}>${it.icon && Icon[it.icon] ? Icon[it.icon](15, it.filled) : null} ${it.label}</button>`)}
    ${rest.some((it) => !it.sep && !it.head) ? html`<${MoreButton} items=${rest} />` : null}`;
}

/** How many action buttons the selection bar has room for at this window
 *  width (the rest go under ⋯). */
function useRoom() {
  const of = () => (innerWidth >= 1380 ? 3 : innerWidth >= 1180 ? 2 : innerWidth >= 900 ? 1 : 0);
  const [n, setN] = useState(of);
  useLayoutEffect(() => {
    const on = () => setN(of());
    addEventListener("resize", on);
    return () => removeEventListener("resize", on);
  }, []);
  return n;
}

/** A button that opens a menu under it. `items`: menu items, or a function that
 *  makes them when it's pressed. */
export function MenuButton({ id, label = "More", icon = "more", title, items, disabled }) {
  return html`<button type="button" class="ghost menu-btn" id=${id} aria-haspopup="menu" title=${title || null} disabled=${disabled}
    onClick=${(e) => openMenu(e.currentTarget, typeof items === "function" ? items() : items)}>${icon && Icon[icon] ? Icon[icon](15) : null} ${label} ▾</button>`;
}

/** The view switcher: an icon and a label on each choice (the label goes in a
 *  narrow window; the tooltip stays). `views`: [[id, label, icon]]. */
export function ViewSwitch({ views, value, onChange, label = "View", id }) {
  return html`<div class="seg view-switch" role="group" aria-label=${label} id=${id}>${views.map(([k, l, icon]) =>
    html`<button type="button" key=${k} data-view=${k} aria-pressed=${value === k ? "true" : "false"} title=${l} onClick=${() => onChange(k)}>${icon && Icon[icon] ? Icon[icon](14) : null}<span class="view-label">${l}</span></button>`)}</div>`;
}

/** The sort menu: "Sort: Name ▾", with the same words everywhere. `options`:
 *  [[id, label]]; the menu's items are `sort-<id>`. */
export function SortMenu({ options, value, onChange, id }) {
  const cur = options.find(([k]) => k === value) || options[0];
  const items = () => options.map(([k, l]) => ({ id: `sort-${k}`, label: l, icon: k === cur[0] ? "check" : null, run: () => onChange(k) }));
  return html`<button type="button" class="ghost sort-btn" id=${id} data-sort=${cur[0]} aria-haspopup="menu" title=${`Sort: ${cur[1]}`}
    onClick=${(e) => openMenu(e.currentTarget, items())}>${Icon.sort(14)}<span class="sort-label"> Sort: ${cur[1]} ▾</span></button>`;
}

/** Above the results while a search is on, so a place that shows little or
 *  nothing doesn't look empty or broken: "Searching for … ×". */
export function SearchNote({ q, found, onClear, wider }) {
  if (!q) return null;
  return html`<p class="search-note" id="search-note" role="status">
    <span>Searching for <b>${q}</b>${found ? html` <span class="muted">· ${found}</span>` : null}</span>
    ${wider || null}
    <button type="button" class="ghost search-clear" id="search-clear" title="Clear the search" aria-label="Clear the search" onClick=${onClear}>${Icon.close(12)} Clear</button></p>`;
}

/** A box in a details panel, saved when you leave it (or press Enter); Esc puts
 *  it back. What's typed stays while the panel is drawn again; a new value from
 *  outside replaces it. `caption`: a label shown before it. */
export function EditBox({ id, cls = "", label, caption, title, placeholder, value, off, onSave }) {
  const [v, setV] = useState(value || "");
  // before the next paint: after it, text typed as the box appeared was put back
  useLayoutEffect(() => { setV(value || ""); }, [value]);
  const key = (e) => {
    if (e.key === "Escape") { e.stopPropagation(); e.currentTarget.value = value || ""; setV(value || ""); e.currentTarget.blur(); }
    else if (e.key === "Enter") e.currentTarget.blur();
  };
  const box = html`<input type="text" class=${`edit-box ${cls}`} id=${id} aria-label=${label} title=${title || label} placeholder=${placeholder} value=${v} disabled=${off}
    onInput=${(e) => setV(e.target.value)} onKeyDown=${key} onChange=${(e) => onSave(e.target.value.trim())} />`;
  return caption ? html`<label class="edit-row"><span class="edit-cap">${caption}</span>${box}</label>` : box;
}
