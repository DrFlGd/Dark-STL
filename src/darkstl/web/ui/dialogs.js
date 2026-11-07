// Dialogs: adding a game folder, changing a folder's game, opening one model
// file, collecting models into the app, unpacking a game's archives, and confirming. Which one is open is
// `ui.dialog` ({ type: "add-folder" | "change-game" | "open-file" | "collect" | "unpack" | "confirm", ... }).
import { html, useState, useLayoutEffect, useRef, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { api, binUrl, info, followJob, loadOverview, pickFolder, showInFolder, toast, plural, bytesText } from "./api.js";

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
      location.hash = routeHash(placeRoute(r.game));
      if (r.packed) { ui.set({ dialog: { type: "unpack", path: r.path, game: r.packed.game, added: r.count } }); return; }
      close();
      toast(r.games.length > 1 ? `Found ${plural(r.count, "model")} for ${r.games.join(", ")}` : `Found ${plural(r.count, "model")} for ${r.game}`);
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
    : html`<p class="muted">Choose the game's install folder, any folder of model files you extracted from it,
      or a folder of models Dark STL collected before. Dark STL lists what it finds by game and type; nothing in the folder is changed.
      When the game keeps its models in archives (Dark Souls, Dark Souls II and III, Sekiro, Elden Ring), Dark STL offers to unpack them next.</p>`}
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
        : check?.game ? `Recognised as ${check.game}.` : check?.exists ? "Not recognised: choose the game, or another name for a folder of other models." : "Dark STL recognises Dark Souls, Bloodborne, Dark Souls III, Sekiro, Elden Ring, Cyberpunk 2077, the Resident Evil games (2, 3, 4, 7, Village) Borderlands (1, 2, The Pre-Sequel, 3, Wonderlands, 4) and Returnal folders by their files."}</small>
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
  const [opts, setOpts] = useState({ textures: true, animations: true, player_animations: false, lod: false, packages: true });
  const packages = !!opts.packages;
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState("");
  const seq = useRef(0);
  const args = { ...(scopes.find((s) => s.key === scope) || scopes[0]).args, textures: opts.textures, animations: opts.animations,
    player_animations: opts.player_animations, lod: opts.lod, lod_copies: opts.lod, packages };
  const argsKey = JSON.stringify(args);
  const [reading, setReading] = useState("");
  useEffect(() => {
    // a job: reading every model of a game takes a while. Changing an option (or closing) stops it.
    const n = ++seq.current;
    let job = null;
    setPlan(null); setPlanError(null); setReading("");
    const t = setTimeout(async () => {
      try {
        ({ job } = await api("collect_plan", { ...args, job: 1 }));
        if (n !== seq.current) { api("job_stop", { id: job }).catch(() => {}); return; }
        const p = await followJob(job, "Working out what to copy", (j) => {
          if (n === seq.current) setReading(`${j.message || ""}${j.progress ? ` · ${Math.round(j.progress * 100)}%` : ""}`);
        });
        if (n === seq.current) { setPlan(p); setPlanError(null); }
      } catch (e) {
        if (n === seq.current && !e.stopped) { setPlan(null); setPlanError(String(e.message || e)); }
      }
    }, 200);
    return () => { seq.current++; clearTimeout(t); if (job) api("job_stop", { id: job }).catch(() => {}); };
  }, [argsKey]);
  const submit = async () => {
    if (!plan) { setError(planError || "Still working out what to copy…"); return; }
    setBusy(true); setError(null);
    try {
      const { job } = await api("collect_run", args);
      const r = await followJob(job, `${packages ? "Importing" : "Collecting"} from ${plan.game}`, (j) => setProgress(j.message || ""));
      await loadOverview();
      close();
      const failed = r.failures ? ` · ${plural(r.failures, "model")} couldn't be read` : "";
      toast(packages ? (r.written ? `Imported ${plural(r.written, "model")} as packages (${bytesText(r.bytes)})${failed}`
          : `Everything was imported already${failed}`)
        : r.copied ? `Copied ${plural(r.copied, "file")} (${bytesText(r.bytes)}) into the app's library`
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
  const submitLabel = busy ? (packages ? "Importing…" : "Copying…")
    : packages ? (plan && !plan.to_write && !plan.configurable ? "Already imported" : "Import")
    : plan && !plan.to_copy && plan.files ? "Already collected" : "Copy";
  return html`<${Dialog} title="Collect into the app" id="collect-dialog" onSubmit=${submit} busy=${busy || !plan} error=${error}
      submitLabel=${submitLabel}>
    <p class="muted">${packages
      ? html`Writes each model as a model package in Dark STL's own library folder: its mesh, skeleton and animations as a
        glTF file, with its textures stored under their names in the game. Packages open without the game, so it can be
        removed from the library or uninstalled afterwards.`
      : html`Copies the files these models need (the model itself, its textures and animations) into Dark STL's own
        library folder, laid out like the game's folder. Afterwards the game's folder can be removed from the library, or
        the game uninstalled, and the models stay.`}</p>
    <div class="field-block"><span class="field-label">Save as</span>
      <label class="check-row"><input type="radio" name="collect-as" id="collect-as-packages" checked=${packages} disabled=${busy}
        onChange=${() => setOpts({ ...opts, packages: true })} />
        <span>Model packages<small class="muted">self-contained, opened by any game's tools alike; weapons with configurations are copied as game files for now</small></span></label>
      <label class="check-row"><input type="radio" name="collect-as" id="collect-as-files" checked=${!packages} disabled=${busy}
        onChange=${() => setOpts({ ...opts, packages: false })} />
        <span>Game files<small class="muted">copies of the game's own files, read by the importers as before</small></span></label>
    </div>
    ${scopes.length > 1 ? html`<div class="field-block"><label class="field-label" for="collect-scope">What to collect</label>
      <select id="collect-scope" value=${scope} disabled=${busy} onChange=${(e) => setScope(e.target.value)}>
        ${scopes.map((s) => html`<option key=${s.key} value=${s.key}>${SCOPE_NAMES[s.key]}: ${s.label}</option>`)}
      </select></div>` : html`<p><b>${scopes[0].label}</b></p>`}
    <div class="field-block"><span class="field-label">With</span>
      ${packages ? null : check("textures", "Textures", "the texture archives beside each model, and the shared texture packs map pieces use")}
      ${check("animations", "Animations", packages ? "the model's skeleton and every animation it can be posed with, sampled per frame"
        : "each model's own skeleton and animations (its .anibnd)")}
      ${packages ? null : check("player_animations", "Player animations", "the player character's animations, which armour pieces and weapons are animated with; large")}
      ${check("lod", "Low-detail copies", "the _l models the game shows from afar")}
    </div>
    <div class="collect-plan" id="collect-plan" aria-live="polite">
      ${planError ? html`<p class="warn-text" id="collect-plan-error">${planError}</p>`
      : !plan ? html`<p class="muted" id="collect-plan-reading">Working out what to copy…${reading ? html`<br /><small>${reading}</small>` : null}</p>`
      : plan.packages ? html`<p id="collect-plan-text"><b>${plural(plan.models, "model")}</b>
          ${plan.present ? html`<span class="muted"> · ${plan.to_write ? `${plural(plan.present, "model")} imported already, ${plural(plan.to_write, "model")} to write` : "all imported already"}</span>` : null}</p>
        ${plan.configurable ? html`<p class="muted" id="collect-plan-configurable">${plural(plan.configurable, "configurable model")} copied as game files, so their parts and skins can still be chosen</p>` : null}
        <p class="muted">Into <span class="note-path" id="collect-folder">${plan.folder}</span> · <a href=${routeHash("settings")} id="collect-change-folder" onClick=${close}>change where</a></p>`
      : html`<p id="collect-plan-text"><b>${plural(plan.models, "model")}</b> · ${plural(plan.files, "file")} · ${bytesText(plan.bytes)}
          ${plan.present ? html`<span class="muted"> · ${plan.to_copy ? `${plural(plan.present, "file")} already there, ${bytesText(plan.bytes_to_copy)} to copy` : "all already there"}</span>` : null}</p>
        <p class="muted" id="collect-plan-kinds">${kinds.join(" · ")}</p>
        <p class="muted">Into <span class="note-path" id="collect-folder">${plan.folder}</span> · <a href=${routeHash("settings")} id="collect-change-folder" onClick=${close}>change where</a></p>`}
    </div>
    ${busy ? html`<p class="muted" id="collect-progress">${progress || (packages ? "Importing…" : "Copying…")}</p>` : null}
  <//>`;
}

/** Unpack a game folder's archives (Data0.bhd/.bdt ...) into the app's library: which kinds of model, where to. */
function Unpack({ path, game, added }) {
  const [plan, setPlan] = useState(null);
  const [reading, setReading] = useState("Reading the game's archives…");
  const [planError, setPlanError] = useState(null);
  const [groups, setGroups] = useState(null);
  const [dest, setDest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState("");
  const [fetching, setFetching] = useState("");
  const read = async (where) => {
    setPlanError(null); setReading("Reading the game's archives…");
    try {
      const { job } = await api("unpack_plan", { path, game, dest: where || undefined });
      const p = await followJob(job, `Reading ${game}'s archives`, (j) => setReading(`${j.message || "Reading the game's archives"}${j.progress ? ` · ${Math.round(j.progress * 100)}%` : ""}`));
      setPlan(p);
      setDest(p.dest);
      setGroups((g) => g || p.groups.filter((x) => x.default && x.files).map((x) => x.id));
    } catch (e) { setPlan(null); setPlanError(String(e.message || e)); }
    setReading("");
  };
  useEffect(() => { read(""); }, [path]);
  const browse = async () => {
    const p = await pickFolder("Unpack the models into", dest || undefined);
    if (p) read(p);
  };
  const lists = async () => {
    setFetching("Downloading…"); setError(null);
    try {
      const { job } = await api("filelists_download", { games: [plan?.game || game] });
      await followJob(job, "Downloading file lists", (j) => setFetching(j.message || "Downloading…"));
      await read(dest);
    } catch (e) { setError(String(e.message || e)); }
    setFetching("");
  };
  const chosen = plan ? plan.groups.filter((g) => groups?.includes(g.id)) : [];
  const toWrite = chosen.reduce((n, g) => n + g.bytes_to_unpack, 0);
  const files = chosen.reduce((n, g) => n + g.to_unpack, 0);
  const tight = plan?.free_bytes != null && toWrite > plan.free_bytes;
  const submit = async () => {
    if (!plan) { setError(planError || "Still reading the archives…"); return; }
    if (!chosen.length) { setError("Choose what to unpack."); return; }
    setBusy(true); setError(null);
    try {
      const { job } = await api("unpack_run", { path, game: plan.game, dest, groups: chosen.map((g) => g.id) });
      const r = await followJob(job, `Unpacking ${plan.game}`, (j) => setProgress(`${j.message || "Unpacking…"}${j.progress ? ` · ${Math.round(j.progress * 100)}%` : ""}`));
      await loadOverview();
      close();
      location.hash = routeHash(placeRoute(plan.game));
      toast(`${r.written ? `Unpacked ${plural(r.written, "file")} (${bytesText(r.bytes)})` : "Everything was unpacked already"}: ${plural(r.count, "model")} listed for ${plan.game}${r.failed_count ? `; ${plural(r.failed_count, "file")} couldn't be read` : ""}`,
        { ms: 8000, action: { label: "Show in folder", run: () => showInFolder(r.dest) } });
    } catch (e) {
      setBusy(false);
      setError(String(e.message || e));
    }
  };
  const toggle = (id, on) => setGroups((g) => (on ? [...new Set([...(g || []), id])] : (g || []).filter((x) => x !== id)));
  return html`<${Dialog} title=${`Unpack ${plan?.game || game || "the game"}`} id="unpack-dialog" onSubmit=${submit} busy=${busy || !plan || !!reading || !chosen.length}
      error=${error} submitLabel=${busy ? "Unpacking…" : plan && chosen.length && !files ? "Unpacked already" : "Unpack"}>
    ${added != null ? html`<p id="unpack-why">${added ? `Only ${plural(added, "model")} were found loose in this folder:` : "No models were found loose in this folder:"} the game keeps them in its archives.</p>` : null}
    <p class="muted">Dark STL reads the game's archives and writes out the models you choose, with their textures and animations,
      laid out the way the game names them. The game's folder isn't changed; the files go into the app's library and are listed from there.</p>
    <p class="note-path" id="unpack-source">${plan?.source || path}</p>
    ${planError ? html`<p class="warn-text" id="unpack-plan-error" role="alert">${planError}</p>` : null}
    ${reading ? html`<p class="muted" id="unpack-reading" aria-live="polite">${reading}<br /><small>The first time, each archive's encrypted list of files is read, which takes up to a minute.</small></p>` : null}
    ${plan && !reading ? html`<div class="field-block" id="unpack-groups"><span class="field-label">What to unpack</span>
      ${plan.groups.map((g) => html`<label class="check-row" key=${g.id}>
        <input type="checkbox" data-group=${g.id} checked=${!!groups?.includes(g.id)} disabled=${busy || !g.files} onChange=${(e) => toggle(g.id, e.target.checked)} />
        <span>${g.label}${" "}<span class="muted">${`· ${g.files ? `${plural(g.files, "file")}, ${bytesText(g.bytes)}${g.files && !g.to_unpack ? " · unpacked already" : g.to_unpack < g.files ? ` · ${bytesText(g.bytes_to_unpack)} still to unpack` : ""}` : "none in this game"}`}</span>
          <small class="muted">${g.description}</small></span></label>`)}
    </div>
    <div class="field-block"><label class="field-label" for="unpack-dest">Into</label>
      <div class="field-row"><input type="text" id="unpack-dest" value=${dest} disabled=${busy} onInput=${(e) => setDest(e.target.value)} onChange=${(e) => read(e.target.value)} />
        ${info().dialogs ? html`<button type="button" class="ghost" id="unpack-browse" onClick=${browse} disabled=${busy}>${Icon.folder(14)} Browse…</button>` : null}</div>
      <small class=${tight ? "danger-text" : "muted"} id="unpack-space">${bytesText(toWrite || 0)} to write${plan.free_bytes != null ? ` · ${bytesText(plan.free_bytes)} free there` : ""}${tight ? ": not enough room" : ""}</small>
      ${dest !== plan.default_dest ? null : html`<small class="muted">The app's library folder (Settings, App library), beside anything collected from ${plan.game}.</small>`}
    </div>
    <p class="muted" id="unpack-names">${plan.unknown ? `${plural(plan.unknown, "other file")} in the archives ${plan.unknown === 1 ? "has" : "have"} no known name and ${plan.unknown === 1 ? "is" : "are"} left out (sounds, scripts and the like, mostly).` : "Every file in the archives has a known name."}
${plan.guessed ? ` ${plural(plan.guessed, "file")} were named by the pattern of the known ones.` : ""}${` Names come from ${plan.lists.join(", ")}. `}<button type="button" class="ghost compact" id="unpack-lists" disabled=${busy || !!fetching} onClick=${lists}>${fetching || "Download newer file lists"}</button></p>` : null}
    ${busy ? html`<p class="muted" id="unpack-progress" aria-live="polite">${progress || "Unpacking…"}</p>` : null}
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

const ROLE_LABELS = { albedo: "Colour map", normal: "Normal map", height: "Height map" };

/** The texture picker: find any texture of the game by its name, see it, and use it for one of a material's
 *  roles (kept in the game's library overlay; the texture is kept in its asset store). */
function TexturePicker({ sid, game, material, materialName, role: startRole, current, assigned }) {
  const [q, setQ] = useState(current || "");
  const [found, setFound] = useState(null);
  const [chosen, setChosen] = useState(null);
  const [role, setRole] = useState(startRole || "albedo");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    let live = true;
    const t = setTimeout(() => {
      api("textures_search", { sid, q, limit: 80 }).then((r) => live && (setFound(r), setError(null)), (e) => live && setError(String(e.message || e)));
    }, 200);
    return () => { live = false; clearTimeout(t); };
  }, [q]);
  const reopen = () => ui.set({ configRev: (ui.get().configRev || 0) + 1 });
  const assign = async (ref) => {
    setBusy(true); setError(null);
    try {
      const r = await api("texture_assign", { sid, material, role, ref });
      close();
      reopen();
      toast(ref ? `${materialName} uses ${r.texture.name} as its ${ROLE_LABELS[role].toLowerCase()}` : `${materialName} uses its own ${ROLE_LABELS[role].toLowerCase()} again`, { ms: 5000 });
    } catch (e) {
      setBusy(false);
      setError(String(e.message || e));
    }
  };
  const submit = () => { if (!chosen) { setError("Choose a texture first."); return; } assign(chosen.ref); };
  const items = found?.items || [];
  return html`<${Dialog} title=${`Choose a texture for ${materialName}`} id="texture-picker" onSubmit=${submit} busy=${busy || !chosen} error=${error}
      submitLabel=${busy ? "Saving…" : "Use it"}>
    <p class="muted">Search ${game}'s textures by their names in the game${current ? html`. This material uses <b>${current}</b> now` : ""}.</p>
    <div class="picker-top">
      <label class="field-block picker-search"><span>Name</span>
        <input type="search" id="texture-search" value=${q} placeholder="Part of the name: wall, _n, c1100" onInput=${(e) => setQ(e.target.value)} disabled=${busy} /></label>
      <label class="field-block"><span>Use as</span>
        <select id="texture-role" value=${role} onChange=${(e) => setRole(e.target.value)} disabled=${busy}>
          ${Object.entries(ROLE_LABELS).map(([k, v]) => html`<option key=${k} value=${k}>${v}</option>`)}</select></label>
    </div>
    <p class="muted small" id="texture-count">${found ? (found.listed ? `${plural(found.total, "texture")} of ${found.listed.toLocaleString()} match${found.total > items.length ? `; the first ${items.length} shown` : ""}`
      : "No textures can be listed for this game yet: import its models as packages, or choose a game whose textures are indexed (FromSoftware, RE Engine).") : "Looking…"}</p>
    <div class="picker-body">
      <ul class="picker-list" id="texture-results" role="listbox" aria-label="Textures">${items.map((t) => html`<li key=${t.ref}>
        <button type="button" role="option" aria-selected=${chosen?.ref === t.ref} class=${chosen?.ref === t.ref ? "picker-item selected" : "picker-item"}
          data-texture=${t.name} onClick=${() => setChosen(t)} onDblClick=${() => { setChosen(t); assign(t.ref); }}>
          <img loading="lazy" alt="" width="40" height="40" src=${binUrl("texture_preview", { game, ref: t.ref, max: 64 })} />
          <span>${t.name}<small class="muted">${t.where}</small></span></button></li>`)}</ul>
      <div class="picker-preview" id="texture-chosen">${chosen ? html`<img alt=${chosen.name} src=${binUrl("texture_preview", { game, ref: chosen.ref, max: 512 })} />
        <p><b>${chosen.name}</b><small class="muted">${chosen.where}</small></p>` : html`<p class="muted">Choose a texture to see it here.</p>`}</div>
    </div>
    ${assigned ? html`<p><button type="button" class="link-btn" id="texture-reset" disabled=${busy} onClick=${() => assign(null)}>Go back to the texture the model names itself</button></p>` : null}
  <//>`;
}

export function Dialogs() {
  const dialog = useStore(ui, (s) => s.dialog);
  if (!dialog) return null;
  if (dialog.type === "add-folder") return html`<${AddFolder} game=${dialog.game} collected=${!!dialog.collected} />`;
  if (dialog.type === "change-game") return html`<${ChangeGame} key=${dialog.path} path=${dialog.path} game=${dialog.game} />`;
  if (dialog.type === "open-file") return html`<${OpenFile} />`;
  if (dialog.type === "confirm") return html`<${ConfirmDialog} ...${dialog} />`;
  if (dialog.type === "collect") return html`<${Collect} ...${dialog} />`;
  if (dialog.type === "texture-picker") return html`<${TexturePicker} key=${`${dialog.sid}:${dialog.material}:${dialog.role}`} ...${dialog} />`;
  if (dialog.type === "unpack") return html`<${Unpack} key=${dialog.path} path=${dialog.path} game=${dialog.game} added=${dialog.added} />`;
  return null;
}
