// The engine's commands as the pages use them, the library overview, jobs
// followed in the status bar, and the message at the foot of the window.
import { ctx } from "./context.js";
import { ui } from "./state.js";

export const api = (cmd, args) => ctx.platform.api(cmd, args);
export const binUrl = (cmd, args) => ctx.platform.binUrl(cmd, args);
export const info = () => ctx.platform?.info || {};
export const plural = (n, one, many = `${one}s`) => `${Number(n).toLocaleString()} ${n === 1 ? one : many}`;
/** A size in bytes as people read it: "1.2 GB", "48.3 MB", "12 KB". */
export const bytesText = (n) => (n >= 1073741824 ? `${(n / 1073741824).toFixed(2)} GB` : n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

let overviewTimer = null;
/** Read the games and their categories again (and keep reading while folders are being scanned). */
export async function loadOverview() {
  clearTimeout(overviewTimer);
  try {
    const ov = await api("overview", { lod: ui.get().lod });
    ui.set({ overview: ov });
    if (ov.scanning) overviewTimer = setTimeout(loadOverview, 700);
  } catch (e) {
    ui.set({ overview: { games: [], total: 0, scanning: false, error: String(e.message || e) } });
  }
}

/** Follow a job in the status bar until it ends. Resolves with its result; rejects with its error
 *  (one with `stopped` set when the user stopped it: see stopJob). */
export function followJob(id, label, onProgress) {
  const started = Date.now();
  ui.set((s) => ({ jobs: [...s.jobs, { id, label, started, progress: 0, message: "", stoppable: false, stopping: false }] }));
  const done = () => ui.set((s) => ({ jobs: s.jobs.filter((j) => j.id !== id) }));
  return new Promise((resolve, reject) => {
    const poll = async () => {
      let job;
      try { job = await api("job", { id }); } catch (e) { done(); reject(e); return; }
      ui.set((s) => ({ jobs: s.jobs.map((j) => (j.id === id ? { ...j, progress: job.progress, message: job.message, stoppable: job.stoppable, stopping: job.stopping } : j)) }));
      onProgress?.(job);
      if (job.state === "running") { setTimeout(poll, 250); return; }
      done();
      if (job.state === "done") resolve(job.result);
      else reject(Object.assign(new Error(job.state === "stopped" ? "Stopped." : job.error || "It didn't work."), { stopped: job.state === "stopped" }));
    };
    poll();
  });
}

/** Stop a job the status bar shows (generating, exporting, scanning, collecting). It ends at its next step. */
export async function stopJob(id) {
  ui.set((s) => ({ jobs: s.jobs.map((j) => (j.id === id ? { ...j, stopping: true, message: "Stopping…" } : j)) }));
  try { await api("job_stop", { id }); } catch (e) { toast(String(e.message || e)); }
}

/** Show a short message at the foot of the window. `opts`: how long (ms), or { ms, action: { label, run } }. */
export function toast(text, opts = {}) {
  const { ms = 3500, action = null } = typeof opts === "number" ? { ms: opts } : opts;
  const el = document.getElementById("toast");
  if (!el) return;
  const span = document.createElement("span");
  span.textContent = text;
  el.replaceChildren(span);
  if (action) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "toast-action";
    b.textContent = action.label;
    b.onclick = () => { el.hidden = true; action.run(); };
    el.append(b);
  }
  el.hidden = false;
  const hide = (after) => { clearTimeout(toast.timer); toast.timer = setTimeout(() => { el.hidden = true; }, after); };
  el.onmouseenter = () => clearTimeout(toast.timer);
  el.onmouseleave = () => hide(2500);
  hide(ms);
}

/** Show a file or folder in the system's file manager. */
export async function showInFolder(path) {
  try { await api("open_path", { path }); } catch (e) { toast(String(e.message || e)); }
}

/** A folder from the system's dialog, or null (also null when the window has no dialogs). */
export async function pickFolder(title, start) {
  if (!info().dialogs) return null;
  try { return (await api("pick_folder", { title, start })).path || null; } catch (e) { toast(String(e.message || e)); return null; }
}

/** A file from the system's dialog, or null. `patterns` are "Name (*.a;*.b)" filters. */
export async function pickFile(title, patterns, start) {
  if (!info().dialogs) return null;
  try { return (await api("pick_file", { title, patterns, start })).path || null; } catch (e) { toast(String(e.message || e)); return null; }
}
