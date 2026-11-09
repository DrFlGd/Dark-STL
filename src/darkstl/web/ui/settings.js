// Settings: the game folders (rescan, change game, remove, add), the folders
// searched for textures a model doesn't hold, the Oodle library Sekiro and
// Elden Ring files need, where exports go, where Model Painter is, the previews,
// and the look.
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui, THEMES, SKINS, setTheme, setSkin } from "./state.js";
import { api, info, plural, bytesText, toast, showInFolder, pickFolder, followJob, loadOverview } from "./api.js";
import { CommandBar } from "./layout.js";
import { Icon } from "./icons.js";
import { GameBadge } from "./gameicons.js";
import { rescanFolder, removeFolder, changeGame, unpackFolder, UNPACK_HINT } from "./games.js";
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
        ${f.packed ? html`<button type="button" class="primary compact folder-unpack" disabled=${f.missing} title=${UNPACK_HINT} onClick=${() => unpackFolder(f.path, f.packed.game)}>Unpack…</button>` : null}
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
  const [moveToo, setMoveToo] = useState(true);
  const [moving, setMoving] = useState("");
  const moveLibrary = async (path) => {
    if (!path) { save({ library_dir: "" }, "Back to the app's own folder."); return; }
    setBusy(true); setError(null);
    try {
      const r = await api("library_move", { path, move: moveToo });
      if (r.job) {
        await followJob(r.job, "Moving collected files", (j) => setMoving(j.message || ""));
        await loadOverview();
        toast("Moved the collected files; they're listed from there now.");
      } else toast(s.library_files && !moveToo ? "Saved. Files collected so far stay where they are; new ones go here." : "Saved.");
      setS(await api("settings_get"));
    } catch (e) { setError(msg(e)); }
    setMoving(""); setBusy(false);
  };
  const save = async (patch, said) => {
    setBusy(true); setError(null);
    try { setS(await api("settings_set", patch)); if (said) toast(said); } catch (e) { setError(msg(e)); }
    setBusy(false);
  };
  if (!s) return error ? html`<p class="warn-note" role="alert">${error}</p>` : null;
  const dirs = s.texture_dirs;
  const pickFile = async (start) => {
    try { return (await api("pick_file", { title: "Find the Oodle library (oo2core in the Sekiro or Elden Ring folder, bin\\x64\\oo2ext_7_win64.dll in Cyberpunk 2077's)", patterns: ["Oodle (oo2core*.dll;oo2ext*.dll;liboo2core*;kraken.dll)", "All files (*.*)"], start })).path; }
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
        onSave=${moveLibrary} />
      ${s.library_files ? html`<label class="check-row" title="Moves each game's collected files to the new folder and lists them from there. Otherwise they stay listed where they are, and only new collections go to the new folder.">
        <input type="checkbox" id="library-move" checked=${moveToo} disabled=${busy} onChange=${(e) => setMoveToo(e.target.checked)} /><span>Move the files collected so far there too</span></label>` : null}
      <p class="muted">Each game's folder in it holds a note of what it is (darkstl-collection.json), so on another computer or a fresh install you can add the folder (or one game's folder in it) with <i>Add game folder…</i> instead of the games.</p>
      ${moving ? html`<p class="muted" id="library-moving">${moving}</p>` : null}
      <div><button type="button" class="ghost" id="library-show" onClick=${() => showInFolder(s.library_dir)}>${Icon.folder(14)} Show in folder</button></div>
    </section>
    <${GameArchives} />
    <${UnrealGames} s=${s} busy=${busy} save=${save} />
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
      <p class="muted">Sekiro, Elden Ring and Cyberpunk 2077 compress their files with Oodle. Dark STL uses the game's own copy of it (oo2core_6_win64.dll in the game's folder; bin\\x64\\oo2ext_7_win64.dll for Cyberpunk) and finds it by itself when a model comes from there.</p>
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
    <section class="ls-section" id="settings-painter">
      <h2>Model Painter</h2>
      <p class="muted">Model Painter paints models for colour printing and finishes them for printing. The Export tab's <i>Export for Model Painter</i>
        writes a GLB with the textures, and <i>Open in Model Painter</i> opens it there. Dark STL finds Model Painter where its installer or its portable
        zip put it; if yours is somewhere else, choose its program here. Get it from${" "}<a href="https://github.com/DrFlGd/Model-Painter/releases" target="_blank" rel="noreferrer">Model Painter's releases</a>.</p>
      <p id="painter-status">${s.model_painter_found ? html`${Icon.check(14)} ${s.model_painter ? "Using" : "Found"}: <span class="note-path">${s.model_painter_found}</span>`
        : s.model_painter_missing ? html`<span class="warn-text">${s.model_painter} isn't there any more.</span>`
        : html`<span class="muted">Not found. Exports are still saved; only Open in Model Painter needs it.</span>`}</p>
      <${PathRow} id="painter-path" value=${s.model_painter} placeholder=${s.model_painter_found && !s.model_painter ? "Found by itself: choose another to use that" : "Model Painter.exe"} busy=${busy}
        browse=${async (start) => { try { return (await api("pick_file", { title: "Find Model Painter", patterns: ["Model Painter (*.exe;*.app)", "All files (*.*)"], start })).path; } catch (e) { toast(msg(e)); return null; } }}
        onSave=${(p) => save({ model_painter: p }, p ? "Saved where Model Painter is." : "Cleared: Dark STL looks for Model Painter by itself.")} />
    </section>
    <section class="ls-section" id="settings-previews">
      <h2>Previews</h2>
      <p class="muted">The pictures on the cards are drawn the first time each model comes into view, and kept in <span class="note-path">${s.previews}</span> until the model's folder is rescanned. To draw one model's again, use Redraw preview in its ⋯ menu; a game's, Redraw this game's previews in its menu.</p>
      <div><button type="button" class="ghost" id="previews-clear" onClick=${async () => {
        try { const r = await api("thumbs_clear"); forgetThumbs(); toast(`Cleared ${plural(r.removed, "preview")}. They're drawn again as they come into view.`); } catch (e) { toast(msg(e)); }
      }}>Draw them again</button></div>
    </section>`;
}

/** Borderlands 3, Wonderlands and Borderlands 4: the AES keys their packed files need, and the type mappings (.usmap)
 * Unreal 5 games cooked without property names need. */
function UnrealGames({ s, busy, save }) {
  const [keys, setKeys] = useState(s.unreal_keys || "");
  useEffect(() => setKeys(s.unreal_keys || ""), [s.unreal_keys]);
  const pickUsmap = async (start) => {
    try { return (await api("pick_file", { title: "Choose the game's type mappings (.usmap)", patterns: ["Type mappings (*.usmap)", "All files (*.*)"], start })).path; }
    catch (e) { toast(msg(e)); return null; }
  };
  return html`<section class="ls-section" id="settings-unreal">
    <h2>Unreal games</h2>
    <p class="muted">Borderlands 3, Tiny Tina's Wonderlands, Borderlands 4 and Returnal keep their models packed (.pak, or .utoc and .ucas).
      Encrypted ones need the game's AES key: Borderlands 3's comes with Dark STL; for the others, paste the key (0x followed by 64 hexadecimal digits), one per line.
      A file named unreal-keys.txt in the game's folder works too. Rescan the game's folder after adding one.</p>
    <form class="inline-form" onSubmit=${(e) => { e.preventDefault(); save({ unreal_keys: keys }, keys.trim() ? "Saved the keys. Rescan the game's folder to use them." : "Cleared the keys."); }}>
      <textarea id="unreal-keys" rows="2" spellcheck="false" placeholder="0x…" style="flex: 1; min-width: 0; font-family: var(--mono, monospace); font-size: 12px; resize: vertical" value=${keys} disabled=${busy} onInput=${(e) => setKeys(e.target.value)}></textarea>
      <button type="submit" class="ghost" disabled=${busy || keys === (s.unreal_keys || "")}>Save</button>
    </form>
    <p class="muted">Borderlands 4 and Returnal store their objects without their property names, so reading them needs the game's type mappings: a .usmap file made for the
      installed version of the game (dumped from the running game with a tool such as UE4SS or Dumper-7, or shared by the modding community; it changes with
      game updates). Put it in the game's folder, or add it here with the game's name in the file name (Returnal.usmap) so each game finds its own.</p>
    ${s.usmaps?.length ? html`<ul class="ls-list" id="usmap-list">${s.usmaps.map((n) => html`<li key=${n}><span class="note-path">${n}</span>
      <button type="button" class="ghost" disabled=${busy} onClick=${() => save({ usmap_remove: n }, "Removed the type mappings.")}>Remove</button></li>`)}</ul>`
      : html`<p class="muted" id="usmap-none">No type mappings added.</p>`}
    <${PathRow} id="usmap-add" placeholder="Add a .usmap: paste its path" saveLabel="Add" busy=${busy} browse=${pickUsmap}
      onSave=${(p) => p && save({ usmap_add: p }, "Added the type mappings. Models open with them from now on.")} />
  </section>`;
}

/** Unpacking the games that keep their files in archives, and the file lists it names them with. */
function GameArchives() {
  const [lists, setLists] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState(null);
  const ov = useStore(ui, (s) => s.overview);
  useEffect(() => { api("filelists").then(setLists, (e) => setError(msg(e))); }, []);
  const packed = (ov?.games || []).flatMap((g) => g.folders.filter((f) => f.packed).map((f) => ({ ...f, game: g.name })));
  const download = async () => {
    setBusy("Downloading…"); setError(null);
    try {
      const { job } = await api("filelists_download");
      const r = await followJob(job, "Downloading file lists", (j) => setBusy(j.message || "Downloading…"));
      setLists(await api("filelists"));
      toast(`Downloaded ${plural(r.downloaded.length, "file list")}${r.failed.length ? ` (${r.failed.length} failed)` : ""}. Unpacking uses them from now on.`);
    } catch (e) { setError(msg(e)); }
    setBusy("");
  };
  const got = lists?.games.filter((g) => g.downloaded.length) || [];
  return html`<section class="ls-section" id="settings-archives">
    <h2>Game archives</h2>
    <p class="muted">Dark Souls, Dark Souls II, Dark Souls III, Sekiro and Elden Ring keep their models in archives (Data0.bhd and .bdt and so on).
      Dark STL unpacks them itself: add the game's install folder, then choose <i>Unpack…</i> on it. Only the models, their textures and their
      animations are written out, into the App library below; the game's folder isn't changed.</p>
    ${packed.length ? html`<ul class="ls-list" id="packed-folders">${packed.map((f) => html`<li key=${f.path}><span>${f.game}${" "}<span class="note-path">${f.path}</span></span>
      <button type="button" class="primary compact folder-unpack" onClick=${() => unpackFolder(f.path, f.packed.game)}>Unpack…</button></li>`)}</ul>` : null}
    <p class="muted">The archives only keep a number for each file, so names come from lists the modding community made (UXM's lists, as Smithbox ships them, come with Dark STL).
      Newer lists, such as those naming Elden Ring's Shadow of the Erdtree files, can be downloaded from${" "}<a href=${lists?.source || "https://github.com/Nordgaren/UXM-Selective-Unpack"} target="_blank" rel="noreferrer">UXM Selective Unpacker</a>.</p>
    <p id="filelists-status">${!lists ? html`<span class="muted">…</span>` : got.length ? html`${Icon.check(14)} Downloaded lists for ${got.map((g) => g.game).join(", ")} (${got[0].downloaded_at})` : html`<span class="muted">Using the bundled lists.</span>`}</p>
    ${error ? html`<p class="warn-text" role="alert" id="filelists-error">${error}</p>` : null}
    <div><button type="button" class="ghost" id="filelists-download" disabled=${!!busy} onClick=${download}>${busy || (got.length ? "Download again" : "Download newer file lists")}</button></div>
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
