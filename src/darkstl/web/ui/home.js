// Home: each game in the library as a tile with four of its models, what needs
// doing about a folder (still packed, gone missing), and Add game folder…. The
// first time, with no folders yet, it says what Dark STL reads and how to start.
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute, searchHash } from "./context.js";
import { api, info, plural } from "./api.js";
import { CommandBar } from "./layout.js";
import { Icon } from "./icons.js";
import { Thumb } from "./thumbs.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { gameMenu, unpackFolder } from "./games.js";
import { openMenu } from "./actions.js";
import { statsLine, statsTitle } from "./models.js";

const addFolder = () => ui.set({ dialog: { type: "add-folder" } });
export const openFile = () => ui.set({ dialog: { type: "open-file" } });

/** A game as a tile: four of its models, its name and what it holds. */
function GameTile({ game, total }) {
  const [models, setModels] = useState(null);
  useEffect(() => {
    let live = true;
    if (!game.count) { setModels([]); return; }
    api("models_query", { game: game.name, limit: 4 }).then((r) => { if (live) setModels(r.items); }, () => { if (live) setModels([]); });
    return () => { live = false; };
  }, [game.name, total]);
  const cats = game.categories.length;
  return html`<a class="subcat-tile game-tile" href=${routeHash(placeRoute(game.name))} data-game=${game.name} title=${game.name}
      onContextMenu=${(e) => openMenu(e, gameMenu(game))}>
    <span class=${`subcat-pics subcat-pics-${Math.max(1, models?.length || 0)}`}>${models?.length
      ? models.map((m) => html`<${Thumb} key=${m.id} id=${m.id} category=${m.category} cls="subcat-pic" />`)
      : html`<span class="subcat-empty"><${GameBadge} name=${game.name} size=${34} /></span>`}</span>
    <span class="subcat-name"><${GameBadge} name=${game.name} /><span>${game.name}</span></span>
    <span class="subcat-meta">${plural(game.count, "model")}${cats ? ` · ${plural(cats, "kind")}` : ""}</span>
  </a>`;
}

/** The search across every game, from Home: Enter (or the button) shows what it finds. */
function HomeSearch() {
  const [text, setText] = useState("");
  const go = (e) => { e.preventDefault(); location.hash = searchHash(text.trim()); };
  return html`<form class="home-search" id="home-search-form" role="search" onSubmit=${go}>
    <label class="search small toolbar-search"><span class="visually-hidden">Search every game</span>
      <input id="home-search" data-search type="search" value=${text} placeholder="Search every game…" autocomplete="off" spellcheck="false"
        title="Search every game's models by name, name in the game or file name; Enter shows what's found (/)" onInput=${(e) => setText(e.target.value)} /></label>
  </form>`;
}

/** The models opened last (newest first), each a link back to it. */
function Recent({ ids }) {
  const [models, setModels] = useState(null);
  const key = ids.join(",");
  useEffect(() => {
    let live = true;
    if (!ids.length) { setModels([]); return; }
    api("models_get", { ids }).then((r) => { if (live) setModels(r.items); }, () => { if (live) setModels([]); });
    return () => { live = false; };
  }, [key]);
  if (!models?.length) return null;
  return html`<section class="home-card home-recent" id="home-recent">
    <div class="home-card-head"><h2>Recent</h2><span class="muted">The models you opened last</span></div>
    <div class="home-strip" id="recent-models">${models.map((m) => html`<a key=${m.id} class="home-model" href=${routeHash(`model:${m.id}`)} data-model=${m.id} title=${`${m.label} (${m.game})`}>
      <${Thumb} id=${m.id} category=${m.category} />
      <span class="card-name">${m.label}</span>
      <span class="card-location"><${GameBadge} name=${m.game} size=${12} /> ${statsLine(m.stats) ? html`<span title=${statsTitle(m.stats)}>${statsLine(m.stats)}</span>` : m.category}</span>
    </a>`)}</div>
  </section>`;
}

/** Folders with something to say: still packed, gone, nothing found. */
function FolderNotes({ games }) {
  const notes = games.flatMap((g) => g.folders.filter((f) => f.notes.length).map((f) => ({ game: g, folder: f })));
  if (!notes.length) return null;
  return html`<section class="home-notes" id="home-notes" aria-label="Folders to look at">
    ${notes.map(({ game, folder }) => html`<div class="warn-note folder-note" key=${folder.path} data-folder=${folder.path}>
      <p><b>${game.name}</b> · <span class="note-path">${folder.path}</span></p>
      ${folder.notes.map((n, i) => html`<p key=${i}>${n}</p>`)}
      ${folder.packed ? html`<p><button type="button" class="primary compact folder-unpack" onClick=${() => unpackFolder(folder.path, folder.packed.game)}>${Icon.inbox(14)} Unpack ${folder.packed.game}…</button></p>` : null}
    </div>`)}
  </section>`;
}

/** The kinds of model across every game, each a link to the first game that has it. */
function Kinds({ games }) {
  const kinds = new Map();
  for (const g of games) for (const c of g.categories) {
    const k = kinds.get(c.name) || { name: c.name, count: 0, games: [] };
    k.count += c.count;
    k.games.push(g.name);
    kinds.set(c.name, k);
  }
  if (!kinds.size) return null;
  return html`<section class="home-card" id="home-kinds">
    <h2>By kind</h2>
    <ul class="ls-list">${[...kinds.values()].map((k) => html`<li key=${k.name}>
      <span class="home-cat"><${CategoryIcon} name=${k.name} size=${16} /><span>${k.name}</span></span>
      <span class="kind-games">${k.games.map((g) => html`<a key=${g} href=${routeHash(placeRoute(g, k.name))} title=${`${k.name} in ${g}`}><${GameBadge} name=${g} /></a>`)}
        <span class="muted">${k.count.toLocaleString()}</span></span></li>`)}</ul>
  </section>`;
}

function HowItWorks() {
  return html`<section class="home-card" id="home-how">
    <h2>From game model to print</h2>
    <ol class="how-list">
      <li><b>Pick a model</b> in a game's library and open it to see it with its textures.</li>
      <li><b>Generate</b> evens out its vertices to the count you choose, then raises the surface detail its textures carry.</li>
      <li><b>Export</b> it as STL for your slicer, as a painted 3MF for a multi-colour printer, or OBJ, PLY or GLB.</li>
    </ol>
  </section>`;
}

export function Home() {
  const ov = useStore(ui, (s) => s.overview);
  const recent = useStore(ui, (s) => s.recent || []);
  if (!ov) return html`<${CommandBar} id="home-head" title="Dark STL" /><div class="home"><p class="muted">Reading the library…</p></div>`;
  if (!ov.games.length && !ov.scanning) return html`<${CommandBar} id="home-head" title="Dark STL" /><${FirstRun} error=${ov.error} />`;
  return html`<${CommandBar} id="home-head" title="Your games"
      count=${html`<span class="page-count" id="home-counts">${plural(ov.games.length, "game")} · ${plural(ov.total, "model")}</span>`}
      search=${ov.games.length ? html`<${HomeSearch} />` : null}
      primary=${html`<button type="button" class="primary compact" id="home-add-folder" onClick=${addFolder}>${Icon.plus(15)} Add game folder…</button>`}
      more=${{ id: "home-more", items: [{ id: "open-file", label: "Open a model file…", sub: "one file, without adding its folder", icon: "file", run: openFile }] }} />
    <div class="home">
      ${ov.error ? html`<p class="warn-note" role="alert">${ov.error}</p>` : null}
      <${Recent} ids=${recent} />
      <div class="subcat-tiles game-tiles" id="game-tiles">${ov.games.map((g) => html`<${GameTile} key=${g.name} game=${g} total=${ov.total} />`)}
        ${ov.scanning ? html`<div class="subcat-tile game-tile scanning" aria-busy="true"><span class="subcat-pics"><span class="subcat-empty thumb-wait">${Icon.refresh(30)}</span></span>
          <span class="subcat-name">Reading folders…</span></div>` : null}</div>
      <${FolderNotes} games=${ov.games} />
      <div class="home-grid">
        <${Kinds} games=${ov.games} />
        <${HowItWorks} />
      </div>
    </div>`;
}

/** First start: no game folders yet. */
function FirstRun({ error }) {
  const games = (info().games || []).filter((g) => g !== "Other models");
  return html`<div class="pages"><div class="first-run" id="first-run">
    <h1>Add your games</h1>
    <p>Dark STL lists the models in your games (FromSoftware's, Cyberpunk 2077, the Resident Evil games and Borderlands) by game and kind (weapons, armour, characters and more),
      shows them with their textures, and turns them into detailed meshes you can 3D print.</p>
    <p>Add a game's folder to start. Dark STL only reads it; nothing in it is changed. Once a game is listed, the models you want
      can be collected into the app, so the game's folder isn't needed afterwards.</p>
    ${error ? html`<p class="warn-note" role="alert">${error}</p>` : null}
    <div class="home-actions">
      <button type="button" class="primary" id="first-add-folder" onClick=${addFolder}>${Icon.plus(15)} Add a game folder…</button>
      <button type="button" class="ghost" id="first-add-collected" title="A folder of models Dark STL collected before, on this computer or another" onClick=${() => ui.set({ dialog: { type: "add-folder", collected: true } })}>${Icon.inbox(15)} Add collected files…</button>
      <button type="button" class="ghost" id="first-open-file" onClick=${openFile}>Open one model file…</button>
    </div>
    <div class="home-card first-tips">
      <h2>What to choose</h2>
      <p><b>The game's install folder.</b> Dark Souls Remastered keeps its models loose, so its install folder works as it is.
        Dark Souls, Dark Souls II, Dark Souls III, Sekiro and Elden Ring pack them in archives: add the install folder as it is, and Dark STL
        offers to unpack the models (characters, weapons and armour, objects, map pieces) into its own library; the game's folder isn't changed.
        A folder unpacked with UXM Selective Unpacker works too. For Bloodborne, choose the folder of files extracted from the game.
        For Cyberpunk 2077, choose its install folder as it is: the models are read from its archives (the first scan reads through them once), or choose a folder of files exported with WolvenKit.
        For Resident Evil 2, 3, 4, 7, Village and Devil May Cry 5 (RE Engine), choose the install folder as it is: the models are read straight from
        its <code>.pak</code> archives, named through the file lists that come with Dark STL (Ekey's). A folder a pak tool (REtool, ree-pak) extracted works too.
        For Borderlands, Borderlands 2 and The Pre-Sequel, choose the install folder as it is: the models are read from the packages in <code>WillowGame\CookedPCConsole</code> (the first scan reads through them once).
        For Borderlands 3, Tiny Tina's Wonderlands and Borderlands 4, choose the install folder as it is: the models are read from the files in <code>OakGame\Content\Paks</code>.
        For Returnal, choose its install folder as it is: the models are read from the files in <code>Returnal\Content\Paks</code>. If its paks are encrypted, add the game's AES key in Settings under <i>Unreal games</i>; a .usmap type mappings file, if you have one, goes there too.
        Borderlands 4 also needs its type mappings (a .usmap file), and the AES key if its containers are encrypted; add them in Settings under <i>Unreal games</i>.
        For Grand Theft Auto III, Vice City and San Andreas, choose the install folder as it is: the models are read from its <code>.img</code> archives, sorted into characters, vehicles, weapons and map pieces by the game's own lists, with cars in their colours from the game. A folder of .dff and .txd files exported with another tool works too.
        For Grand Theft Auto V, choose the install folder as it is (the one with <code>GTA5.exe</code>): the models are read from its <code>.rpf</code> archives. The first time, Dark STL looks for the archive keys in <code>GTA5.exe</code> (this takes a while, once) and keeps them in its settings. Files exported with OpenIV or CodeWalker (.ydr, .ydd, .yft with their .ytd) work too.
        For Grand Theft Auto IV, choose the install folder as it is (the one with <code>GTAIV.exe</code>): the models are read from its <code>.img</code> archives, with the archive key found once in <code>GTAIV.exe</code>, and the episodes' models come along.</p>
      <p><b>Or any folder of extracted files</b>, such as .partsbnd, .chrbnd, .objbnd, .flver or .mesh files you've pulled out with other tools, or models FModel, UModel or Blender exported (.gltf, .glb, .psk, .pskx, .obj) with their textures beside them.</p>
      <p><b>Or models Dark STL collected before</b> (on this computer or another): choose its library folder, or one game's folder in it, with <i>Add collected files</i>. Each game can come from either its own folder or its collected files.</p>
      ${games.length ? html`<p class="muted">Recognised: ${games.join(", ")}. Other folders can be added under a name you choose.</p>` : null}
    </div>
  </div></div>`;
}
