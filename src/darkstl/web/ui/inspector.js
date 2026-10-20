// The inspector on the right of a page, from Model Library:
// a pane with tabs, and a rail along the window's edge with each tab's icon and
// name written down it. Click a name to open that tab; click the open one, its »
// button or ] to fold the pane to its rail. Its width, whether it's folded and
// the tab open are kept per page.
import { html, useEffect, useRef, useState, useLayoutEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, setPref } from "./state.js";
import { Icon } from "./icons.js";
import { typing } from "./actions.js";

const MIN = 260, MAX = 520;

/** `page`: whose settings these are (model, browse).
 *  `tabs`: [{ id, label, icon, body }] (a tab without a body isn't shown).
 *  `open`: whether it starts open on this page. `want`: a tab to show now
 *  (when it changes, e.g. a model was selected in Unsorted). */
export function Inspector({ page, tabs, open: startOpen = true, want = null, label = "Details", crowded = false }) {
  const st = useStore(ui, (s) => ({ open: s.inspector[page], tab: s.inspectorTab[page], width: s.inspectorWidth }));
  const list = tabs.filter((t) => t.body !== undefined && t.body !== false);
  // Below 900 px a pane covers the page, so it starts folded there and opening
  // it isn't kept. `crowded`: the page has no room for it beside what it shows
  // (the model page's 3D view), so it folds the same way until there's room.
  const narrow = useNarrow();
  const local = narrow || crowded;
  const [localOpen, setLocalOpen] = useState(false);
  useLayoutEffect(() => setLocalOpen(false), [local]);
  const isOpen = local ? localOpen : st.open == null ? startOpen : st.open === "open";
  // a tab marked `empty` (nothing in it) isn't opened by itself: the page shows
  // the first tab with something in it, unless you pick the empty one
  const [picked, setPicked] = useState(null);
  const tab = list.find((t) => t.id === picked) || list.find((t) => t.id === st.tab && !t.empty) || list.find((t) => !t.empty) || list[0];
  const width = Math.max(MIN, Math.min(MAX, st.width || 340));
  const set = (patch) => {
    const prefs = {};
    if ("tab" in patch) prefs.inspectorTab = { ...ui.get().inspectorTab, [page]: patch.tab };
    if ("open" in patch) {
      if (local) setLocalOpen(patch.open);
      else prefs.inspector = { ...ui.get().inspector, [page]: patch.open ? "open" : "folded" };
    }
    setPref(prefs);
  };
  const toggle = () => set({ open: !isOpen });
  // a page asks for a tab with ui.inspectorAsk (the model page's Edit details…),
  // which opens it even where opening isn't kept, then focuses a box in it
  const ask = useStore(ui, (s) => s.inspectorAsk);
  const focus = useRef(null);
  useLayoutEffect(() => {
    if (!ask || ask.page !== page || !list.some((t) => t.id === ask.tab)) return;
    setPicked(null);
    set({ open: true, tab: ask.tab });
    focus.current = ask;
    ui.set({ inspectorAsk: null });
  }, [ask]);
  useEffect(() => {
    const el = focus.current?.focus && document.getElementById(focus.current.focus);
    if (!el) return;
    const select = focus.current.select;
    focus.current = null;
    const put = () => { el.focus(); if (select) el.select?.(); };
    put();
    // and again on the next frame, if the click that asked took focus back
    requestAnimationFrame(() => { if (el.isConnected && document.activeElement !== el && !typing({ target: document.activeElement })) put(); });
  });
  const last = useRef(want);
  useEffect(() => {
    if (want && want !== last.current && list.some((t) => t.id === want)) set({ tab: want });
    last.current = want;
  }, [want]);
  useEffect(() => {
    const key = (e) => {
      if (e.key !== "]" || e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || typing(e) || ui.get().dialog || ui.get().menu) return;
      e.preventDefault();
      toggle();
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  });
  const stopResize = useRef(null);
  useEffect(() => () => stopResize.current?.(), []);
  const resize = (e) => {
    e.preventDefault();
    stopResize.current?.();
    const start = e.clientX, before = width;
    const move = (ev) => setPref({ inspectorWidth: Math.max(MIN, Math.min(MAX, before + start - ev.clientX)) });
    const stop = () => { removeEventListener("pointermove", move); removeEventListener("pointerup", stop); removeEventListener("pointercancel", stop); };
    stopResize.current = stop;
    addEventListener("pointermove", move); addEventListener("pointerup", stop); addEventListener("pointercancel", stop);
  };
  const pick = (t) => { setPicked(t.empty ? t.id : null); isOpen && tab?.id === t.id ? set({ open: false }) : set({ open: true, tab: t.id }); };
  return html`<div class=${`inspector-side${isOpen ? " open" : ""}`} id="inspector" data-page=${page} data-tab=${isOpen ? tab?.id : null}>
    ${isOpen && tab ? html`<section class="inspector-pane" style=${{ width: `${width}px` }} aria-label=${tab.label} id=${`pane-${tab.id}`}>
      <header class="pane-head"><strong>${tab.label}</strong>
        <button type="button" class="ghost panel-fold" id="inspector-fold" title=${`Hide ${label.toLowerCase()} (])`} aria-label=${`Hide ${label.toLowerCase()} (])`} onClick=${toggle}>${Icon.chevronsRight(14)}</button></header>
      <div class="pane-body">${tab.body}</div>
      <div class="pane-resize" role="separator" aria-label="Width" aria-orientation="vertical" aria-valuemin=${MIN} aria-valuemax=${MAX} aria-valuenow=${width} tabIndex="0"
        onPointerDown=${resize} onKeyDown=${(e) => { if (["ArrowLeft", "ArrowRight"].includes(e.key)) { e.preventDefault(); setPref({ inspectorWidth: Math.max(MIN, Math.min(MAX, width + (e.key === "ArrowLeft" ? 20 : -20))) }); } }}></div>
    </section>` : null}
    <${Rail} side="right" tabs=${list} current=${isOpen ? tab?.id : null} onPick=${pick} />
  </div>`;
}

function useNarrow() {
  const q = "(max-width: 899px)";
  const [n, setN] = useState(() => matchMedia(q).matches);
  useLayoutEffect(() => {
    const mq = matchMedia(q), on = () => setN(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return n;
}

/** The width of the element `ref` points at, in CSS pixels (0 until it's laid out). */
export function useWidth(ref) {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.round(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return w;
}

/** A rail: each tab's icon with its name written down the strip. */
export function Rail({ side, tabs, current, onPick }) {
  return html`<nav class=${`rail rail-${side}`} aria-label=${side === "right" ? "Inspector" : "Panes"}>
    ${tabs.map((t) => html`<button type="button" key=${t.id} class="rail-tab" id=${`rail-${t.id}`} data-tab=${t.id} aria-pressed=${current === t.id ? "true" : "false"}
      title=${current === t.id ? `Hide ${t.label}` : `Show ${t.label}`} onClick=${() => onPick(t)}>
      ${Icon[t.icon] ? Icon[t.icon](16) : null}<span class="rail-name">${t.label}</span></button>`)}
  </nav>`;
}
