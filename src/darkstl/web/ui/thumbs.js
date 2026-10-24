// A model's preview picture, drawn by the engine the first time it's asked for
// (darkstl/thumbnails.py) and kept on disk until the model's folder is rescanned.
// Pictures load as they scroll into sight. The engine is first asked only for the
// ones drawn already, several at a time, so those show at once; the rest are drawn
// a couple at a time behind them. A picture the page has shown is kept in memory,
// so going back to a place (or changing the search, sort or view) shows it again
// straight away instead of asking for it again.
import { html, useState, useEffect, useRef } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { binUrl } from "./api.js";
import { CategoryIcon } from "./gameicons.js";

const KEEP = 2000; // pictures kept in memory, the oldest shown let go first
const known = new Map(); // id -> { state: "ok" | "none", src }

function remember(id, entry) {
  known.delete(id);
  known.set(id, entry);
  while (known.size > KEEP) {
    const [oldest, e] = known.entries().next().value;
    known.delete(oldest);
    if (e.src) URL.revokeObjectURL(e.src);
  }
  return entry;
}

/** Let go of every picture shown, so they're asked for again (after a rescan, or Settings' Draw them again). */
export function forgetThumbs() {
  for (const e of known.values()) if (e.src) URL.revokeObjectURL(e.src);
  known.clear();
  ui.set((s) => ({ thumbGen: (s.thumbGen || 0) + 1 }));
}

/** A queue that runs at most `size` tasks at once. */
function queue(size) {
  let active = 0;
  const waiting = [];
  const next = () => {
    while (active < size && waiting.length) {
      const task = waiting.shift();
      if (task.cancelled) continue;
      active++;
      task.run().finally(() => { active--; next(); });
    }
  };
  return (task) => { waiting.push(task); next(); };
}
const look = queue(6); // pictures drawn already: quick to answer
const draw = queue(2); // the engine draws two at a time

/** The picture for `id`: { state: "ok", src } or { state: "none" }. Resolves undefined when cancelled. */
function load(id, gen, task) {
  const ask = async (cached) => {
    const r = await fetch(binUrl("thumb", { id, cached: cached ? 1 : null, v: gen }));
    if (r.status === 404) return { state: "none" };
    if (!r.ok) throw new Error(`thumb ${r.status}`);
    const blob = await r.blob();
    return blob.size ? { state: "ok", src: URL.createObjectURL(blob) } : null; // empty: not drawn yet
  };
  return new Promise((resolve) => {
    const finish = (entry) => {
      const keep = entry && gen === (ui.get().thumbGen || 0); // not from before a rescan
      if (keep) remember(id, entry); // kept even when the card has gone, for when it's back
      if (!task.cancelled) resolve(entry);
      else { if (!keep && entry?.src) URL.revokeObjectURL(entry.src); resolve(undefined); }
    };
    task.run = async () => {
      let entry;
      try { entry = await ask(true); } catch { entry = { state: "none" }; }
      if (entry || task.cancelled) { finish(entry); return; }
      draw({ get cancelled() { return task.cancelled; }, run: async () => {
        let drawn;
        try { drawn = await ask(false); } catch { drawn = { state: "none" }; }
        finish(drawn);
      } });
    };
    look(task);
  });
}

export function Thumb({ id, category, cls = "" }) {
  const ref = useRef(null);
  const gen = useStore(ui, (s) => s.thumbGen || 0);
  const [shown, setShown] = useState(() => known.get(id) || null);
  useEffect(() => {
    const have = known.get(id);
    setShown(have || null);
    if (have) return undefined;
    let task = null;
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting || task) return;
      io.disconnect();
      task = { cancelled: false };
      load(id, gen, task).then((entry) => { if (entry) setShown(entry); });
    }, { rootMargin: "300px" });
    io.observe(ref.current);
    return () => { io.disconnect(); if (task) task.cancelled = true; };
  }, [id, gen]);
  const state = shown?.state || "wait";
  return html`<span class=${`thumb ${cls}`} ref=${ref} data-thumb=${state} role="img" aria-label="Preview">
    ${shown?.src ? html`<img src=${shown.src} alt="" draggable="false" />`
      : html`<span class=${`thumb-none${state === "wait" ? " thumb-wait" : ""}`}>${CategoryIcon({ name: category, size: 30 })}</span>`}
  </span>`;
}
