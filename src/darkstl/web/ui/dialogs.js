// Dialogs: adding a game folder, changing a folder's game, opening one model
// file, and confirming. Which one is open is `ui.dialog`
// ({ type: "add-folder" | "change-game" | "open-file" | "confirm", ... }).
import { html, useState, useLayoutEffect, useRef, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { api, info, followJob, loadOverview, pickFolder, toast, plural } from "./api.js";

export const close = () => ui.set({ dialog: null });

/** A dialog (from Model Library): Esc or Cancel closes it; `danger` makes the button red. */
export function Dialog({ title, id, onSubmit, busy, error, submitLabel, danger = false, cancelLabel = "Cancel", children }) {
  const form = useRef(null);
  useLayoutEffect(() => {
    const key = (e) => { if (e.key === "Escape") close(); };
    addEventListener("keydown", key);
    const f = form.current;
    if (f && !f.contains(document.activeElement)) (f.querySelector("input:not([type=hidden]):not([disabled]), select, textarea") || f).focus();
    return () => removeEventListener("keydown", key);
  }, []);
  return html`<div class="dialog-backdrop" onMouseDown=${(e) => { if (e.target === e.currentTarget) close(); }}>
    <form class="dialog" id=${id} ref=${form} tabindex="-1" role="dialog" aria-modal="true" aria-label=${title} onSubmit=${(e) => { e.preventDefault(); onSubmit(); }}>
      <div class="dialog-head"><h2>${title}</h2>
        <button type="button" class="ghost" aria-label="Close" title="Close (Esc)" onClick=${close}>${Icon.close(15)}</button></div>
      ${children}
      <div class="dialog-foot">
        ${error ? html`<p class="form-error" role="alert">${error}</p>` : null}
        <div class="dialog-actions">
          <button type="button" class="ghost" id=${id ? `${id}-cancel` : null} onClick=${close}>${cancelLabel}</button>
          ${submitLabel ? html`<button type="submit" id=${id ? `${id}-ok` : null} class=${danger ? "primary danger" : "primary"} disabled=${busy}>${submitLabel}</button>` : null}
        </div>
      </div>
    </form>
  </div>`;
}

const OTHER = "__other__";

/** A game picker: the games Dark STL reads, the library's own, or another name. */
function GamePicker({ value, onChange, id }) {
  const games = useStore(ui, (s) => s.overview?.games?.map((g) => g.name) || []);
  const known = [...new Set([...(info().games || []), ...games])];
  const [other, setOther] = useState(value && !known.includes(value));
  return html`<div class="field-row">
    <select id=${id} value=${other ? OTHER : value || ""} onChange=${(e) => { const v = e.target.value; setOther(v === OTHER); onChange(v === OTHER ? "" : v); }}>
      ${!value && !other ? html`<option value="" disabled>Choose…</option>` : null}
      ${known.map((g) => html`<option key=${g} value=${g}>${g}</option>`)}
      <option value=${OTHER}>Another name…</option>
    </select>
    ${other ? html`<input type="text" id=${`${id}-name`} placeholder="Name to list it under" value=${value} onInput=${(e) => onChange(e.target.value)} />` : null}
  </div>`;
}

function AddFolder({ game: forGame }) {
  const [path, setPath] = useState("");
  const [check, setCheck] = useState(null);
  const [game, setGame] = useState(forGame || "");
  const [touched, setTouched] = useState(!!forGame);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState("");
  const look = async (p) => {
    setError(null);
    if (!p.trim()) { setCheck(null); return; }
    try {
      const c = await api("folder_check", { path: p });
      setCheck(c);
      if (!touched && c.game) setGame(c.game);
    } catch (e) { setError(String(e.message || e)); }
  };
  useEffect(() => { const t = setTimeout(() => look(path), 350); return () => clearTimeout(t); }, [path]);
  const browse = async () => {
    const p = await pickFolder("Choose the game's folder", path || undefined);
    if (p) setPath(p);
  };
  const submit = async () => {
    if (!check?.exists) { setError(path.trim() ? "There's no folder there." : "Choose a folder first."); return; }
    const name = (game || check.game || "").trim();
    if (!name) { setError("Choose which game these files are from."); return; }
    setBusy(true); setError(null);
    try {
      const { job } = await api("folder_add", { path: check.path, game: name });
      loadOverview();
      const r = await followJob(job, `Scanning ${check.path.split(/[\\/]/).filter(Boolean).pop()}`, (j) => setProgress(j.message || ""));
      await loadOverview();
      close();
      toast(`Found ${plural(r.count, "model")} for ${r.game}`);
      location.hash = routeHash(placeRoute(r.game));
    } catch (e) {
      setBusy(false);
      setError(String(e.message || e));
    }
  };
  const dialogs = info().dialogs;
  return html`<${Dialog} title="Add a game folder" id="add-folder-dialog" onSubmit=${submit} busy=${busy} error=${error} submitLabel=${busy ? "Scanning…" : "Add folder"}>
    <p class="muted">Choose the game's install folder (once it is unpacked), or any folder of model files you extracted from it.
      Dark STL lists what it finds by game and type; nothing in the folder is changed.</p>
    <div class="field-block"><label class="field-label" for="folder-path">Folder</label>
      <div class="field-row"><input type="text" id="folder-path" placeholder=${dialogs ? "Choose with Browse…, or paste a path" : "Paste the folder's path, e.g. D:\\Steam\\steamapps\\common\\DARK SOULS REMASTERED"}
        value=${path} onInput=${(e) => setPath(e.target.value)} disabled=${busy} />
        ${dialogs ? html`<button type="button" class="ghost" id="folder-browse" onClick=${browse} disabled=${busy}>${Icon.folder(14)} Browse…</button>` : null}</div>
      ${check && !check.exists ? html`<small class="danger-text" id="folder-missing">There's no folder at ${check.path}</small>` : null}
      ${check?.inside ? html`<small class="warn-text">This folder is already in the library as part of ${check.inside_game} (${check.inside}).</small>` : null}
      ${check?.added ? html`<small class="muted">This folder is already in the library; adding it again rescans it.</small>` : null}
      ${check?.contains?.length ? html`<small class="muted">It holds ${plural(check.contains.length, "folder")} already in the library; they become part of this one.</small>` : null}
    </div>
    <div class="field-block"><label class="field-label" for="folder-game">Game</label>
      <${GamePicker} id="folder-game" value=${game} onChange=${(g) => { setGame(g); setTouched(true); }} />
      <small class="muted">${check?.game ? `Recognised as ${check.game}.` : check?.exists ? "Not recognised: choose the game, or another name for a folder of other models." : "Dark STL recognises Dark Souls, Bloodborne, Dark Souls III, Sekiro and Elden Ring folders by their files."}</small>
    </div>
    ${busy ? html`<p class="muted" id="folder-progress">${progress || "Looking for models…"}</p>` : null}
  <//>`;
}

function ChangeGame({ path, game: current }) {
  const [game, setGame] = useState(current || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async () => {
    setBusy(true);
    try {
      await api("folder_set_game", { path, game });
      await loadOverview();
      close();
      location.hash = routeHash(placeRoute(game));
    } catch (e) { setBusy(false); setError(String(e.message || e)); }
  };
  return html`<${Dialog} title="Which game is this folder from?" id="change-game-dialog" onSubmit=${submit} busy=${busy} error=${error} submitLabel="Save">
    <p class="muted" style="overflow-wrap:anywhere">${path}</p>
    <div class="field-block"><label class="field-label" for="change-game">Game</label><${GamePicker} id="change-game" value=${game} onChange=${setGame} /></div>
  <//>`;
}

/** Open one model file without adding its folder to the library. */
function OpenFile() {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const dialogs = info().dialogs;
  const browse = async () => {
    try {
      const r = await api("pick_file", { title: "Open a model file", patterns: info().open_patterns || [] });
      if (r.path) setPath(r.path);
    } catch (e) { setError(String(e.message || e)); }
  };
  const submit = async () => {
    const p = path.trim().replace(/^"|"$/g, "");
    if (!p) { setError("Choose a file first."); return; }
    setBusy(true); setError(null);
    try {
      const m = await api("model_open", { path: p });
      close();
      location.hash = routeHash(`model:${m.id}`);
    } catch (e) { setBusy(false); setError(String(e.message || e)); }
  };
  return html`<${Dialog} title="Open a model file" id="open-file-dialog" onSubmit=${submit} busy=${busy} error=${error} submitLabel=${busy ? "Opening…" : "Open"}>
    <p class="muted">A .partsbnd, .chrbnd, .objbnd, .flver or .obj file (also inside .dcx). It opens on its own page; its folder isn't added to the library.</p>
    <div class="field-block"><label class="field-label" for="file-path">File</label>
      <div class="field-row"><input type="text" id="file-path" placeholder=${dialogs ? "Choose with Browse…, or paste a path" : "Paste the file's path"}
        value=${path} onInput=${(e) => setPath(e.target.value)} disabled=${busy} />
        ${dialogs ? html`<button type="button" class="ghost" id="file-browse" onClick=${browse} disabled=${busy}>${Icon.folder(14)} Browse…</button>` : null}</div>
    </div>
  <//>`;
}

function ConfirmDialog({ title, text, button, run }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const submit = async () => {
    setBusy(true);
    try { await run(); close(); } catch (e) { setBusy(false); setError(String(e.message || e)); }
  };
  return html`<${Dialog} title=${title} id="confirm-dialog" onSubmit=${submit} busy=${busy} error=${error} submitLabel=${button} danger=${true}><p>${text}</p><//>`;
}

export function Dialogs() {
  const dialog = useStore(ui, (s) => s.dialog);
  if (!dialog) return null;
  if (dialog.type === "add-folder") return html`<${AddFolder} game=${dialog.game} />`;
  if (dialog.type === "change-game") return html`<${ChangeGame} key=${dialog.path} path=${dialog.path} game=${dialog.game} />`;
  if (dialog.type === "open-file") return html`<${OpenFile} />`;
  if (dialog.type === "confirm") return html`<${ConfirmDialog} ...${dialog} />`;
  return null;
}
