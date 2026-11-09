// A game's Problems, on its page under its kinds: what needs a look in its library, each with what fixes it.
// Its folders' notes (still packed: Unpack; gone: Remove), models that couldn't be read (Export model files…,
// to send them for a look), model files gone since the scan (Rescan), models without textures and textures not
// found (Find textures… adds a folder where the game keeps them; Choose texture… picks one by name), and
// animations that couldn't be read. Models are known to have a problem once their preview was drawn or they
// were opened (darkstl/app/organise.py, cmd_problems). Home lists how many each game has.
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute } from "./context.js";
import { Icon } from "./icons.js";
import { api, info, plural, toast, pickFolder, showInFolder } from "./api.js";
import { Thumb } from "./thumbs.js";
import { rescanFolder, removeFolder, unpackFolder, UNPACK_HINT, modelFilesItem, redrawPreviews } from "./games.js";

const msg = (e) => String(e?.message || e);

/** The game's folder a model's file is in. */
function folderOf(game, file) {
  const under = (f) => { const p = f.path.replace(/[\\/]+$/, ""); return file.startsWith(p) && /[\\/]/.test(file.charAt(p.length)); };
  return game.folders.find(under) || null;
}

/** Find textures…: a folder searched for the textures models name but don't hold (Settings › Library › Texture
 *  folders); the previews of `models` are drawn again with it. */
export async function findTextures(models) {
  if (!info().dialogs) { location.hash = "#/settings/library"; toast("Add the folder where the game keeps them under Texture folders.", 6000); return; }
  const path = await pickFolder("Folder with the game's textures");
  if (!path) return;
  try {
    const s = await api("settings_get");
    if (!s.texture_dirs.includes(path)) await api("settings_set", { texture_dirs: [...s.texture_dirs, path] });
    if (models.length) await redrawPreviews({ ids: models.map((m) => m.id) }, plural(models.length, "preview"));
    toast(`Added ${path} to the texture folders: models open with it from now on.`, 6000);
  } catch (e) { toast(msg(e)); }
}

/** Choose texture…: the model's Textures tab, where each material's maps can be picked by name. */
function chooseTexture(m) {
  ui.set({ inspectorAsk: { page: "model", tab: "textures" } });
  location.hash = routeHash(`model:${m.id}`);
}

function rowFixes(group, m, game) {
  const files = modelFilesItem(m);
  switch (group) {
    case "unreadable":
    case "animations":
      return [{ id: "files", label: "Export model files…", title: "A zip of its files and a report, to send for a look", run: files.run }];
    case "missing": {
      const f = folderOf(game, m.file);
      return f ? [{ id: "rescan", label: "Rescan", title: `Read ${f.path} again, to list what's there now`, run: () => rescanFolder(f.path) }] : [];
    }
    case "untextured":
    case "textures":
      return [{ id: "choose", label: "Choose texture…", title: "Pick its textures by name from the game's, on its Textures tab", run: () => chooseTexture(m) },
        { id: "find", label: "Find textures…", title: "Add a folder where the game keeps its textures", run: () => findTextures([m]) }];
    default:
      return [];
  }
}

function groupFixes(g, game) {
  if (g.id === "missing") {
    const folders = [...new Set(g.models.map((m) => folderOf(game, m.file)).filter(Boolean))];
    return folders.map((f) => ({ id: `rescan-${f.path}`, label: folders.length > 1 ? `Rescan ${f.path.split(/[\\/]/).filter(Boolean).pop()}` : "Rescan the folder", run: () => rescanFolder(f.path) }));
  }
  if (g.id === "untextured" || g.id === "textures") return [{ id: "find-all", label: "Find textures…", title: "Add a folder where the game keeps its textures; these previews are drawn again with it", run: () => findTextures(g.models) }];
  return [];
}

function Group({ g, game }) {
  const [open, setOpen] = useState(g.count <= 12);
  const fixes = groupFixes(g, game);
  return html`<div class="problem-group" data-problem=${g.id}>
    <button type="button" class="problem-head" aria-expanded=${open ? "true" : "false"} title=${open ? "Fold" : "Show the models"} onClick=${() => setOpen(!open)}>
      <span class=${`fold-icon${open ? " open" : ""}`}>${Icon.chevron(13)}</span><b>${g.title}</b><span class="nav-count">${g.count.toLocaleString()}</span></button>
    <p class="muted problem-hint">${g.hint}</p>
    ${fixes.length ? html`<div class="insp-actions">${fixes.map((f) => html`<button type="button" key=${f.id} class="ghost compact" data-fix=${f.id} title=${f.title || f.label} onClick=${f.run}>${f.label}</button>`)}</div>` : null}
    ${open ? html`<ul class="problem-list">${g.models.map((m) => html`<li key=${m.id} class="problem-row" data-model=${m.id}>
      <a class="problem-model" href=${routeHash(`model:${m.id}`)} title=${`Open ${m.label}`}><${Thumb} id=${m.id} category=${m.category} cls="thumb-sm" />
        <span class="problem-text"><span>${m.label}</span><small class="muted">${m.why || `${m.category}${m.group ? ` › ${m.group}` : ""}`}</small></span></a>
      <span class="problem-fixes">${rowFixes(g.id, m, game).map((f) => html`<button type="button" key=${f.id} class="ghost compact" data-fix=${f.id} title=${f.title} onClick=${f.run}>${f.label}</button>`)}</span>
    </li>`)}
      ${g.count > g.models.length ? html`<li class="muted">and ${(g.count - g.models.length).toLocaleString()} more</li>` : null}</ul>` : null}
  </div>`;
}

/** The game's Problems (nothing when none is known). */
export function Problems({ game }) {
  const [p, setP] = useState(null);
  const libRev = useStore(ui, (s) => s.libRev);
  const gen = useStore(ui, (s) => s.thumbGen || 0);
  useEffect(() => {
    let live = true;
    api("problems", { game: game.name }).then((r) => { if (live) setP(r); }, () => { if (live) setP(null); });
    return () => { live = false; };
  }, [game.name, game.count, JSON.stringify(game.folders.map((f) => [f.path, f.notes.length, f.missing])), libRev, gen]);
  if (!p || !p.count) return null;
  const folders = game.folders.filter((f) => f.notes.length || f.missing);
  return html`<section class="problems" id="problems" aria-label=${`Problems in ${game.name}`}>
    <h2 class="problems-title">${Icon.alert(16)} Needs a look <span class="muted">${plural(p.count, "problem")}</span></h2>
    ${p.unknown ? html`<p class="muted problems-unknown">${plural(p.unknown, "model")} haven't been looked at yet: a model's problems are found when its preview is drawn or it's opened.</p>` : null}
    ${folders.map((f) => html`<div class="warn-note folder-note" key=${f.path} data-folder=${f.path}>
      <p class="note-path">${f.path}</p>${f.missing ? html`<p>This folder isn't there any more.</p>` : null}
      ${f.notes.map((n, i) => html`<p key=${i}>${n}</p>`)}
      <p class="insp-actions">
        ${f.packed ? html`<button type="button" class="primary compact folder-unpack" title=${UNPACK_HINT} onClick=${() => unpackFolder(f.path, f.packed.game)}>${Icon.inbox(14)} Unpack ${f.packed.game}…</button>` : null}
        ${f.missing ? html`<button type="button" class="ghost compact" onClick=${() => removeFolder(f.path, game.name)}>Remove from library…</button>`
          : html`<button type="button" class="ghost compact" onClick=${() => rescanFolder(f.path)}>${Icon.refresh(13)} Rescan</button>
            <button type="button" class="ghost compact" onClick=${() => showInFolder(f.path)}>${Icon.folder(13)} Show</button>`}</p>
    </div>`)}
    ${p.groups.map((g) => html`<${Group} key=${g.id} g=${g} game=${game} />`)}
  </section>`;
}

/** Home's line for each game with problems: how many, and a link to them on its page. */
export function HomeProblems({ games }) {
  const [counts, setCounts] = useState({});
  const libRev = useStore(ui, (s) => s.libRev);
  const key = games.map((g) => `${g.name}:${g.count}:${g.folders.map((f) => f.notes.length + (f.missing ? 1 : 0)).join(",")}`).join("|");
  useEffect(() => {
    let live = true;
    Promise.all(games.map((g) => api("problems", { game: g.name, summary: true }).then((r) => [g.name, r.count], () => [g.name, 0])))
      .then((all) => { if (live) setCounts(Object.fromEntries(all)); });
    return () => { live = false; };
  }, [key, libRev]);
  const shown = games.filter((g) => counts[g.name]);
  if (!shown.length) return null;
  return html`<section class="home-notes" id="home-problems" aria-label="Games with something to look at">
    ${shown.map((g) => {
      const packed = g.folders.find((f) => f.packed);
      return html`<div class="warn-note folder-note home-problem" key=${g.name} data-game=${g.name}>
        <p><b>${g.name}</b> · <a href=${`${routeHash(placeRoute(g.name))}`} onClick=${() => setTimeout(() => document.getElementById("problems")?.scrollIntoView({ block: "start" }), 400)}
          title="Its page lists them, each with what fixes it">${plural(counts[g.name], "thing")} to look at</a></p>
        ${g.folders.flatMap((f) => f.notes).slice(0, 2).map((n, i) => html`<p key=${i}>${n}</p>`)}
        ${packed ? html`<p><button type="button" class="primary compact folder-unpack" onClick=${() => unpackFolder(packed.path, packed.packed.game)}>${Icon.inbox(14)} Unpack ${packed.packed.game}…</button></p>` : null}
      </div>`;
    })}
  </section>`;
}
