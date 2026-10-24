// Dialogs: adding a game folder, changing a folder's game, opening one model
// file, collecting models into the app, and confirming. Which one is open is
// `ui.dialog` ({ type: "add-folder" | "change-game" | "open-file" | "collect" | "confirm", ... }).
import { html, useState, useLayoutEffect, useRef, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { api, info, followJob, loadOverview, pickFolder, showInFolder, toast, plural, bytesText } from "./api.js";

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

function AddFolder({ game: forGame, collected = false }) {
  const [path, setPath] = useState("");
  const [useHere, setUseHere] = useState(null); // collect new files into the folder added (null: not asked yet)
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
      if ((!touched || c.collection) && c.game) setGame(c.game);
      if (c.collection || c.collections.length) setUseHere(!c.library_has_files); // the first collection added: collect into it
    } catch (e) { setError(String(e.message || e)); }
  };
  useEffect(() => { const t = setTimeout(() => look(path), 350); return () => clearTimeout(t); }, [path]);
  const browse = async () => {
    const p = await pickFolder(collected ? "Choose the folder of collected files" : "Choose the game's folder", path || undefined);
    if (p) setPath(p);
  };
  const submit = async () => {
    if (!check?.exists) { setError(path.trim() ? "There's no folder there." : "Choose a folder first."); return; }
    const several = check.collections.length > 0;
    const name = (game || check.game || "").trim();
    if (!name && !several) { setError("Choose which game these files are from."); return; }
    setBusy(true); setError(null);
    try {
      const { job } = await api("folder_add", { path: check.path, game: several ? null : name, library: !!(useHere && (check.collection || several)) });
      loadOverview();
      const r = await followJob(job, `Scanning ${check.path.split(/[\\/]/).filter(Boolean).pop()}`, (j) => setProgress(j.message || ""));
      await loadOverview();
      close();
      toast(r.games.length > 1 ? `Found ${plural(r.count, "model")} for ${r.games.join(", ")}` : `Found ${plural(r.count, "model")} for ${r.game}`);
      location.hash = routeHash(placeRoute(r.game));
    } catch (e) {
      setBusy(false);
      setError(String(e.message || e));
    }
  };
  const dialogs = info().dialogs;
  const several = check?.collections?.length > 0;
  const ours = check?.collection || several;
  const where = check?.collection_root || "";
  return html`<${Dialog} title=${collected ? "Add collected files" : "Add a game folder"} id="add-folder-dialog" onSubmit=${submit} busy=${busy} error=${error} submitLabel=${busy ? "Scanning…" : several ? `Add ${plural(check.collections.length, "game")}` : "Add folder"}>
    ${collected ? html`<p class="muted">Choose the folder Dark STL collected models into (Settings, App library), or one game's folder inside it.
      The models are listed from there, so the games themselves don't need to be installed.</p>`
    : html`<p class="muted">Choose the game's install folder (once it is unpacked), any folder of model files you extracted from it,
      or a folder of models Dark STL collected before. Dark STL lists what it finds by game and type; nothing in the folder is changed.</p>`}
    <div class="field-block"><label class="field-label" for="folder-path">Folder</label>
      <div class="field-row"><input type="text" id="folder-path" placeholder=${dialogs ? "Choose with Browse…, or paste a path" : "Paste the folder's path, e.g. D:\\Steam\\steamapps\\common\\DARK SOULS REMASTERED"}
        value=${path} onInput=${(e) => setPath(e.target.value)} disabled=${busy} />
        ${dialogs ? html`<button type="button" class="ghost" id="folder-browse" onClick=${browse} disabled=${busy}>${Icon.folder(14)} Browse…</button>` : null}</div>
      ${check && !check.exists ? html`<small class="danger-text" id="folder-missing">There's no folder at ${check.path}</small>` : null}
      ${check?.inside ? html`<small class="warn-text">This folder is already in the library as part of ${check.inside_game} (${check.inside}).</small>` : null}
      ${check?.added ? html`<small class="muted">This folder is already in the library; adding it again rescans it.</small>` : null}
      ${check?.contains?.length ? html`<small class="muted">It holds ${plural(check.contains.length, "folder")} already in the library; they become part of this one.</small>` : null}
    </div>
    ${several ? html`<div class="field-block" id="folder-collections"><span class="field-label">Collected files for</span>
      <ul class="ls-list">${check.collections.map((c) => html`<li key=${c.path}><span>${c.game}</span><span class="muted">${plural(c.models, "model")} collected</span></li>`)}</ul></div>`
    : html`<div class="field-block"><label class="field-label" for="folder-game">Game</label>
      <${GamePicker} id="folder-game" value=${game} onChange=${(g) => { setGame(g); setTouched(true); }} />
      <small class="muted" id="folder-recognised">${check?.collection ? `Files Dark STL collected for ${check.collection.game} (${plural(check.collection.models, "model")}).`
        : check?.game ? `Recognised as ${check.game}.` : check?.exists ? "Not recognised: choose the game, or another name for a folder of other models." : "Dark STL recognises Dark Souls, Bloodborne, Dark Souls III, Sekiro and Elden Ring folders by their files."}</small>
    </div>`}
    ${ours && where !== check.library_dir ? html`<label class="check-row" title=${`Now: ${check.library_dir}`}>
      <input type="checkbox" id="folder-use-library" checked=${!!useHere} disabled=${busy} onChange=${(e) => setUseHere(e.target.checked)} />
      <span>Collect new files into this folder too<small class="muted">${where}</small></span></label>` : null}
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

const SCOPE_NAMES = { model: "This model", group: "This group", kind: "This kind", game: "The whole game" };

/** Copy the files chosen models need into the app's own library folder, so the game's folder
 *  isn't needed afterwards. `game`, `category`, `group` say where the dialog was opened from;
 *  `model` ({ id, label, category, group }) when it was opened for one model. */
function Collect({ game, category, group, model }) {
  const scopes = [];
  if (model) scopes.push({ key: "model", label: model.label, args: { ids: [model.id], game: game || undefined } });
  const cat = model ? model.category : category;
  const grp = model ? model.group : group;
  if (game && cat && grp) scopes.push({ key: "group", label: `${grp} (${cat})`, args: { game, category: cat, group: grp } });
  if (game && cat) scopes.push({ key: "kind", label: `All ${cat}`, args: { game, category: cat } });
  if (game) scopes.push({ key: "game", label: `All of ${game}`, args: { game } });
  const [scope, setScope] = useState(scopes[0].key);
  const [opts, setOpts] = useState({ textures: true, animations: true, player_animations: false, lod: false });
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState("");
  const seq = useRef(0);
  const args = { ...(scopes.find((s) => s.key === scope) || scopes[0]).args, textures: opts.textures, animations: opts.animations,
    player_animations: opts.player_animations, lod: opts.lod, lod_copies: opts.lod };
  const argsKey = JSON.stringify(args);
  useEffect(() => {
    const n = ++seq.current;
    const t = setTimeout(() => {
      api("collect_plan", args).then((p) => { if (n === seq.current) { setPlan(p); setPlanError(null); } },
        (e) => { if (n === seq.current) { setPlan(null); setPlanError(String(e.message || e)); } });
    }, 200);
    return () => clearTimeout(t);
  }, [argsKey]);
  const submit = async () => {
    if (!plan) { setError(planError || "Still working out what to copy…"); return; }
    setBusy(true); setError(null);
    try {
      const { job } = await api("collect_run", args);
      const r = await followJob(job, `Collecting from ${plan.game}`, (j) => setProgress(j.message || ""));
      await loadOverview();
      close();
      toast(r.copied ? `Copied ${plural(r.copied, "file")} (${bytesText(r.bytes)}) into the app's library`
        : "Everything was already in the app's library", { ms: 6000, action: { label: "Show in folder", run: () => showInFolder(r.folder) } });
    } catch (e) {
      setBusy(false);
      setError(String(e.message || e));
    }
  };
  const kinds = plan ? Object.entries(plan.kinds).filter(([, v]) => v.files).map(([k, v]) => `${plural(v.files, k === "other" ? "other file" : `${k} file`)}`) : [];
  const check = (key, label, sub) => html`<label class="check-row">
    <input type="checkbox" data-collect=${key} checked=${!!opts[key]} disabled=${busy} onChange=${(e) => setOpts({ ...opts, [key]: e.target.checked })} />
    <span>${label}${sub ? html`<small class="muted">${sub}</small>` : null}</span></label>`;
  return html`<${Dialog} title="Collect into the app" id="collect-dialog" onSubmit=${submit} busy=${busy || !plan} error=${error}
      submitLabel=${busy ? "Copying…" : plan && !plan.to_copy && plan.files ? "Already collected" : "Copy"}>
    <p class="muted">Copies the files these models need (the model itself, its textures and animations) into Dark STL's own
      library folder, laid out like the game's folder. Afterwards the game's folder can be removed from the library, or
      the game uninstalled, and the models stay.</p>
    ${scopes.length > 1 ? html`<div class="field-block"><label class="field-label" for="collect-scope">What to collect</label>
      <select id="collect-scope" value=${scope} disabled=${busy} onChange=${(e) => setScope(e.target.value)}>
        ${scopes.map((s) => html`<option key=${s.key} value=${s.key}>${SCOPE_NAMES[s.key]}: ${s.label}</option>`)}
      </select></div>` : html`<p><b>${scopes[0].label}</b></p>`}
    <div class="field-block"><span class="field-label">With</span>
      ${check("textures", "Textures", "the texture archives beside each model, and the shared texture packs map pieces use")}
      ${check("animations", "Animations", "each model's own skeleton and animations (its .anibnd)")}
      ${check("player_animations", "Player animations", "the player character's animations, which armour pieces and weapons are animated with; large")}
      ${check("lod", "Low-detail copies", "the _l models the game shows from afar")}
    </div>
    <div class="collect-plan" id="collect-plan" aria-live="polite">
      ${planError ? html`<p class="warn-text" id="collect-plan-error">${planError}</p>`
      : !plan ? html`<p class="muted">Working out what to copy…</p>`
      : html`<p id="collect-plan-text"><b>${plural(plan.models, "model")}</b> · ${plural(plan.files, "file")} · ${bytesText(plan.bytes)}
          ${plan.present ? html`<span class="muted"> · ${plan.to_copy ? `${plural(plan.present, "file")} already there, ${bytesText(plan.bytes_to_copy)} to copy` : "all already there"}</span>` : null}</p>
        <p class="muted" id="collect-plan-kinds">${kinds.join(" · ")}</p>
        <p class="muted">Into <span class="note-path" id="collect-folder">${plan.folder}</span> · <a href=${routeHash("settings")} id="collect-change-folder" onClick=${close}>change where</a></p>`}
    </div>
    ${busy ? html`<p class="muted" id="collect-progress">${progress || "Copying…"}</p>` : null}
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
  if (dialog.type === "add-folder") return html`<${AddFolder} game=${dialog.game} collected=${!!dialog.collected} />`;
  if (dialog.type === "change-game") return html`<${ChangeGame} key=${dialog.path} path=${dialog.path} game=${dialog.game} />`;
  if (dialog.type === "open-file") return html`<${OpenFile} />`;
  if (dialog.type === "confirm") return html`<${ConfirmDialog} ...${dialog} />`;
  if (dialog.type === "collect") return html`<${Collect} ...${dialog} />`;
  return null;
}
