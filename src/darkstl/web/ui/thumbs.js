// A model's preview picture, drawn by the engine the first time it's asked for
// (darkstl/thumbnails.py) and kept. Pictures load as they scroll into sight, a
// few at a time, so the page's own requests never wait behind a screenful of them.
import { html, useState, useEffect, useRef } from "../lib/html.js";
import { binUrl } from "./api.js";
import { CategoryIcon } from "./gameicons.js";

const AT_ONCE = 3;
let active = 0;
const waiting = [];

function next() {
  while (active < AT_ONCE && waiting.length) {
    const task = waiting.shift();
    if (task.cancelled) continue;
    active++;
    task.start(() => { active--; next(); });
  }
}

export function Thumb({ id, category, cls = "" }) {
  const ref = useRef(null);
  const [src, setSrc] = useState(null);
  const [state, setState] = useState("wait"); // wait | ok | none
  useEffect(() => {
    setSrc(null);
    setState("wait");
    let task = null;
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting || task) return;
      io.disconnect();
      task = {
        cancelled: false,
        start(done) {
          const img = new Image();
          img.onload = () => { if (!task.cancelled) { setSrc(img.src); setState("ok"); } done(); };
          img.onerror = () => { if (!task.cancelled) setState("none"); done(); };
          img.src = binUrl("thumb", { id });
        },
      };
      waiting.push(task);
      next();
    }, { rootMargin: "300px" });
    io.observe(ref.current);
    return () => { io.disconnect(); if (task) task.cancelled = true; };
  }, [id]);
  return html`<span class=${`thumb ${cls}`} ref=${ref} data-thumb=${state} role="img" aria-label="Preview">
    ${src ? html`<img src=${src} alt="" draggable="false" />`
      : html`<span class=${`thumb-none${state === "wait" ? " thumb-wait" : ""}`}>${CategoryIcon({ name: category, size: 30 })}</span>`}
  </span>`;
}
