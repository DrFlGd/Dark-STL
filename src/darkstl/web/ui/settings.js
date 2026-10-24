// Settings: the game folders (rescan, change game, remove, add), the folders
// searched for textures a model doesn't hold, the Oodle library Sekiro and
// Elden Ring files need, where exports go, the previews, and the look.
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, THEMES, SKINS, setTheme, setSkin } from "./state.js";
import { api, info, plural, bytesText, toast, showInFolder, pickFolder } from "./api.js";
import { CommandBar } from "./layout.js";
import { Icon } from "./icons.js";
import { GameBadge } from "./gameicons.js";
import { rescanFolder, removeFolder, changeGame } from "./games.js";
import { forgetThumbs } from "./thumbs.js";

const msg = (e) => String(e?.message || e);

function GameFolders() {
  const ov = useStore(ui, (s) => s.overview);
  const games = ov?.games || [];
  return html`<section class="ls-section" id="settings-games">
    <h2>Game folders</h2>
    <p class="muted">The folders Dark STL lists models from. It only reads them; nothing in them is changed.</p>
    ${games.length ? html`<ul class="ls-list folder-list" id="folder-list">${games.flatMap((g) => g.folders.map((f) => html`<li key=${f.path} data-folder=${f.path}>
      <span class="folder-what"><span class="folder-game"><${GameBadge} name=${g.name} /> ${g.name}</span>
        <span class="note-path">${f.path}</span>
        <small class=${f.missing ? "danger-text" : "muted"}>${f.missing ? "This folder isn't there any more." : plural(f.count, "model")}${f.collected ? " · copies kept by Dark STL (collected into the app)" : ""}</small>
        ${f.notes.map((n, i) => html`<small class="warn-text" key=${i}>${n}</small>`)}</span>
      <span class="folder-actions">
        <button type="button" class="ghost" title="Show in folder" aria-label="Show in folder" disabled=${f.missing} onClick=${() => showInFolder(f.path)}>${Icon.folder(14)}</button>
        <button type="button" class="ghost folder-rescan" disabled=${f.missing} onClick=${() => rescanFolder(f.path)}>Rescan</button>
        <button type="button" class="ghost folder-game-btn" onClick=${() => changeGame(f.path, g.name)}>Change game…</button>
        <button type="button" class="ghost danger-text folder-remove" onClick=${() => removeFolder(f.path, g.name)}>Remove…</button></span></li>`))}</ul>`
      : html`<p id="no-folders">${ov?.scanning ? "Reading…" : "None yet."}</p>`}
    <div><button type="button" class="primary compact" id="settings-add-folder" onClick=${() => ui.set({ dialog: { type: "add-folder" } })}>${Icon.plus(14)} Add game folder…</button></div>
  </section>`;
}

/** A folder or file path with Browse… (when the window has dialogs) and Save. */
function PathRow({ id, value, placeholder, onSave, browse, saveLabel = "Save", busy }) {
  const [v, setV] = useState(value || "");
  useEffect(() => setV(value || ""), [value]);
  return html`<form class="inline-form path-form" onSubmit=${(e) => { e.preventDefault(); onSave(v.trim()); }}>
    <input type="text" id=${id} value=${v} placeholder=${placeholder} onInput=${(e) => setV(e.target.value)} disabled=${busy} />
    ${browse && info().dialogs ? html`<button type="button" class="ghost" onClick=${async () => { const p = await browse(v); if (p) { setV(p); onSave(p); } }} disabled=${busy}>Browse…</button>` : null}
    <button type="submit" class="ghost" disabled=${busy || v.trim() === (value || "")}>${saveLabel}</button>
  </form>`;
}

function Engine() {
  const [s, setS] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api("settings_get").then(setS, (e) => setError(msg(e))); }, []);
  const save = async (patch, said) => {
    setBusy(true); setError(null);
    try { setS(await api("settings_set", patch)); if (said) toast(said); } catch (e) { setError(msg(e)); }
    setBusy(false);
  };
  if (!s) return error ? html`<p class="warn-note" role="alert">${error}</p>` : null;
  const dirs = s.texture_dirs;
  const pickFile = async (start) => {
    try { return (await api("pick_file", { title: "Find oo2core (in the Sekiro or Elden Ring folder)", patterns: ["Oodle (oo2core*.dll;liboo2core*)", "All files (*.*)"], start })).path; }
    catch (e) { toast(msg(e)); return null; }
  };
  return html`
    ${error ? html`<p class="warn-note" role="alert" id="settings-error">${error}</p>` : null}
    <section class="ls-section" id="settings-library">
      <h2>App library</h2>
      <p class="muted">Where "Collect into the app" keeps its copies: the files chosen models need (the model, its textures, its animations),
        laid out like the game's folder, one folder per game. Collected models stay listed when the game's folder is removed or the game uninstalled.</p>
      <p id="library-status">${s.library_files ? html`${Icon.inbox(14)} ${plural(s.library_files, "file")} · ${bytesText(s.library_bytes)}` : html`<span class="muted">Nothing collected yet. Each game's ⋯ menu, a model's right-click menu and the model page's ⋯ menu have Collect into the app.</span>`}</p>
      <${PathRow} id="library-dir" value=${s.library_dir} placeholder="Folder for the copies" busy=${busy} browse=${(v) => pickFolder("Keep collected files in", v || undefined)}
        onSave=${(p) => save({ library_dir: p }, "Saved. Files collected so far stay where they were; collect them again to copy them here.")} />
      <div><button type="button" class="ghost" id="library-show" onClick=${() => showInFolder(s.library_dir)}>${Icon.folder(14)} Show in folder</button></div>
    </section>
    <section class="ls-section" id="settings-textures">
      <h2>Texture folders</h2>
      <p class="muted">Where to look for textures a model uses but doesn't hold itself: map pieces and some objects share textures kept elsewhere in the game.</p>
      ${dirs.length ? html`<ul class="ls-list" id="texture-dirs">${dirs.map((d) => html`<li key=${d}><span class="note-path">${d}</span>
        <button type="button" class="ghost" disabled=${busy} onClick=${() => save({ texture_dirs: dirs.filter((x) => x !== d) }, "Removed the texture folder.")}>Remove</button></li>`)}</ul>` : null}
      <${PathRow} id="texture-dir-add" placeholder="Add a folder: paste its path" saveLabel="Add" busy=${busy}
        browse=${(v) => pickFolder("Folder with textures", v || undefined)} onSave=${(p) => p && !dirs.includes(p) && save({ texture_dirs: [...dirs, p] }, "Added the texture folder. Models open with it from now on.")} />
    </section>
    <section class="ls-section" id="settings-oodle">
      <h2>Oodle</h2>
      <p class="muted">Sekiro and Elden Ring compress their files with Oodle. Dark STL uses the game's own copy of it, oo2core_6_win64.dll in the game's folder, and finds it by itself when a model comes from there.</p>
      <p id="oodle-status">${s.oodle_found ? html`${Icon.check(14)} Found: <span class="note-path">${s.oodle_found}</span>` : html`<span class="warn-text">Not found yet.</span> Dark Souls, Dark Souls II, Dark Souls III and Bloodborne don't need it.`}</p>
      <${PathRow} id="oodle-path" value=${s.oodle} placeholder="The DLL, or the game folder it's in" busy=${busy} browse=${pickFile}
        onSave=${(p) => save({ oodle: p }, p ? "Saved where Oodle is." : "Cleared.")} />
    </section>
    <section class="ls-section" id="settings-exports">
      <h2>Exports</h2>
      <p class="muted">Where exported models and saved textures go unless you choose another folder on the Export tab.</p>
      <${PathRow} id="export-dir" value=${s.export_dir} busy=${busy} browse=${(v) => pickFolder("Export to", v || undefined)} onSave=${(p) => save({ export_dir: p }, "Saved.")} />
      <div><button type="button" class="ghost" onClick=${() => showInFolder(s.export_dir)}>${Icon.folder(14)} Show in folder</button></div>
    </section>
    <section class="ls-section" id="settings-previews">
      <h2>Previews</h2>
      <p class="muted">The pictures on the cards are drawn the first time each model comes into view, and kept in <span class="note-path">${s.previews}</span> until the model's folder is rescanned.</p>
      <div><button type="button" class="ghost" id="previews-clear" onClick=${async () => {
        try { const r = await api("thumbs_clear"); forgetThumbs(); toast(`Cleared ${plural(r.removed, "preview")}. They're drawn again as they come into view.`); } catch (e) { toast(msg(e)); }
      }}>Draw them again</button></div>
    </section>`;
}

export function Settings() {
  const s = useStore(ui, (st) => ({ skin: st.skin, theme: st.theme }));
  return html`<${CommandBar} id="settings-head" title="Settings" /><div class="pages"><div class="library-settings">
    <${GameFolders} />
    <${Engine} />
    <section class="ls-section" id="settings-look">
      <h2>Appearance</h2>
      <label class="field-block"><span>Skin</span>
        <select id="skin-select" class="compact" value=${s.skin} onChange=${(e) => setSkin(e.target.value)}>
          ${SKINS.map(([v, label]) => html`<option value=${v} key=${v}>${label}</option>`)}
        </select>
        <small class="muted">The app's look, shared with Model Library and SCAD Workshop. Each skin has light and dark colours, chosen by Theme below.</small></label>
      <label class="field-block"><span>Theme</span>
        <select id="theme-select" class="compact" value=${s.theme} onChange=${(e) => setTheme(e.target.value)}>
          ${THEMES.map(([v, label]) => html`<option value=${v} key=${v}>${label}</option>`)}
        </select></label>
    </section>
    <section class="ls-section" id="settings-about">
      <h2>About</h2>
      <p>Dark STL ${info().version || ""}</p>
      <p><a href="#/about">What it does, and the projects it builds on</a></p>
    </section>
  </div></div>`;
}
