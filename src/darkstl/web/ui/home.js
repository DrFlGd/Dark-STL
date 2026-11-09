// Home: each game in the library as a tile with four of its models, the games
// with something to look at (a folder still packed or gone, models that couldn't
// be read or have no textures: their pages list them), the collections, and Add
// game folder…. The first time, with no folders yet, each game Dark STL reads is
// a tile: click one for what to choose for it.
import { html, useState, useEffect } from "../lib/html.js";
import { useStore } from "../lib/store.js";
import { ui } from "./state.js";
import { routeHash, placeRoute, searchHash } from "./context.js";
import { api, info, plural } from "./api.js";
import { CommandBar } from "./layout.js";
import { Icon } from "./icons.js";
import { Thumb } from "./thumbs.js";
import { GameBadge, CategoryIcon } from "./gameicons.js";
import { gameMenu } from "./games.js";
import { HomeProblems } from "./problems.js";
import { collectionMenu, newCollection } from "./organise.js";
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

/** The collections (and Starred) as tiles: four of their models, their name and how many. */
function HomeCollections() {
  const c = useStore(ui, (st) => st.collections);
  if (!c) return null;
  const tiles = [...(c.starred ? [{ id: "starred", name: "Starred", count: c.starred, preview: c.starred_preview, href: "#/starred", star: true }] : []),
    ...c.collections.map((x) => ({ ...x, href: routeHash(`collection:${x.id}`) }))];
  return html`<section class="home-card home-collections" id="home-collections">
    <div class="home-card-head"><h2>Collections</h2><span class="muted">Lists of your own, across every game</span>
      <button type="button" class="ghost compact" id="home-new-collection" title="A list of your own: drag models onto it in the menu" onClick=${() => newCollection()}>${Icon.plus(13)} New collection…</button></div>
    ${tiles.length ? html`<div class="subcat-tiles collection-tiles" id="collection-tiles">${tiles.map((t) => html`<a key=${t.id} class="subcat-tile collection-tile" href=${t.href} data-collection=${t.id} title=${t.name}
        onContextMenu=${t.star ? null : (e) => openMenu(e, collectionMenu(t))}>
      <span class=${`subcat-pics subcat-pics-${Math.max(1, t.preview.length)}`}>${t.preview.length
        ? t.preview.map((m) => html`<${Thumb} key=${m.id} id=${m.id} category=${m.category} cls="subcat-pic" />`)
        : html`<span class="subcat-empty">${t.star ? Icon.star(30) : Icon.stack(30)}</span>`}</span>
      <span class="subcat-name">${t.star ? Icon.star(15, true) : Icon.stack(15)}<span>${t.name}</span></span>
      <span class="subcat-meta">${plural(t.count, "model")}${t.games?.length ? ` · ${t.games.length === 1 ? t.games[0] : plural(t.games.length, "game")}` : ""}</span>
    </a>`)}</div>`
      : html`<p class="muted" id="no-collections">None yet. Right-click a model for Add to collection… or Star; several chosen at once (Ctrl+click, Shift+click) can be added together, or dragged onto a collection in the menu.</p>`}
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
      <${HomeProblems} games=${ov.games} />
      <${HomeCollections} />
      <div class="home-grid">
        <${Kinds} games=${ov.games} />
        <${HowItWorks} />
      </div>
    </div>`;
}

/** What to choose for each game, the first time (FirstRun's tiles). */
const FROMSOFT_PACKED = "Choose the install folder as it is: Dark STL offers to unpack the models (characters, weapons and armour, objects, map pieces) into its own library; the game's folder isn't changed. A folder UXM Selective Unpacker unpacked works too.";
const RE_ENGINE = "Choose the install folder as it is: the models are read straight from its .pak archives, named through the file lists that come with Dark STL. A folder a pak tool (REtool, ree-pak) extracted works too.";
const BL_UE3 = "Choose the install folder as it is: the models are read from the packages in WillowGame\\CookedPCConsole (the first scan reads through them once).";
const BL_UE4 = "Choose the install folder as it is: the models are read from the files in OakGame\\Content\\Paks.";
export const GAME_NOTES = {
  "Elden Ring": `${FROMSOFT_PACKED} Its files are compressed with Oodle: Dark STL uses the game's own copy (oo2core_6_win64.dll in its folder).`,
  "Sekiro": `${FROMSOFT_PACKED} Its files are compressed with Oodle: Dark STL uses the game's own copy (oo2core_6_win64.dll in its folder).`,
  "Dark Souls III": FROMSOFT_PACKED,
  "Dark Souls II": FROMSOFT_PACKED,
  "Dark Souls Remastered": "Choose its install folder: Dark Souls Remastered keeps its models loose, so it works as it is.",
  "Dark Souls": FROMSOFT_PACKED,
  "Bloodborne": "Choose the folder of files extracted from the game (its dvdroot): Dark STL reads its .chrbnd, .partsbnd and .objbnd files and their textures.",
  "Cyberpunk 2077": "Choose its install folder as it is: the models are read from its archives (the first scan reads through them once). A folder of files exported with WolvenKit works too.",
  "Resident Evil 2": RE_ENGINE, "Resident Evil 3": RE_ENGINE, "Resident Evil 4": RE_ENGINE, "Resident Evil 7": RE_ENGINE,
  "Resident Evil Village": RE_ENGINE, "Devil May Cry 5": RE_ENGINE,
  "Returnal": "Choose its install folder as it is: the models are read from Returnal\\Content\\Paks. It needs its type mappings (a .usmap file, from UE4SS) and, if its paks are encrypted, its AES key: add them in Settings › Game tools › Unreal games.",
  "Borderlands": BL_UE3, "Borderlands 2": BL_UE3, "Borderlands: The Pre-Sequel": BL_UE3,
  "Borderlands 3": `${BL_UE4} Its AES key comes with Dark STL.`,
  "Tiny Tina's Wonderlands": `${BL_UE4} If its files are encrypted, add the game's AES key in Settings › Game tools › Unreal games.`,
  "Borderlands 4": `${BL_UE4} It also needs its type mappings (a .usmap file), and the AES key if its containers are encrypted: add them in Settings › Game tools › Unreal games.`,
};

/** First start: no game folders yet. Each game Dark STL reads is a tile; clicking one says what to choose for it. */
function FirstRun({ error }) {
  const games = (info().games || []).filter((g) => g !== "Other models");
  const [chosen, setChosen] = useState(null);
  return html`<div class="pages"><div class="first-run" id="first-run">
    <h1>Add your games</h1>
    <p>Dark STL lists the models in your games by game and kind (weapons, armour, characters and more), shows them with their textures,
      and turns them into detailed meshes you can 3D print. It only reads a game's folder; nothing in it is changed.</p>
    ${error ? html`<p class="warn-note" role="alert">${error}</p>` : null}
    <h2 class="first-run-sub">Which game? <span class="muted">Click one for what to choose</span></h2>
    <div class="first-games" id="first-games" role="list">${games.map((g) => html`<button type="button" role="listitem" key=${g} class=${`first-game${chosen === g ? " on" : ""}`}
        data-game=${g} aria-pressed=${chosen === g ? "true" : "false"} title=${`What to choose for ${g}`} onClick=${() => setChosen(chosen === g ? null : g)}>
      <${GameBadge} name=${g} size=${22} /><span>${g}</span></button>`)}</div>
    ${chosen ? html`<div class="home-card first-game-note" id="first-game-note" data-game=${chosen}>
      <h2><${GameBadge} name=${chosen} size=${18} /> ${chosen}</h2>
      <p>${GAME_NOTES[chosen] || "Choose the game's install folder, or a folder of its extracted files."}</p>
      <div class="home-actions"><button type="button" class="primary" id="first-add-game" onClick=${() => ui.set({ dialog: { type: "add-folder", game: chosen } })}>${Icon.plus(15)} Add this game's folder…</button></div>
    </div>` : null}
    <div class="home-actions">
      <button type="button" class="primary" id="first-add-folder" onClick=${addFolder}>${Icon.plus(15)} Add a game folder…</button>
      <button type="button" class="ghost" id="first-add-collected" title="A folder of models Dark STL collected before, on this computer or another" onClick=${() => ui.set({ dialog: { type: "add-folder", collected: true } })}>${Icon.inbox(15)} Add collected files…</button>
      <button type="button" class="ghost" id="first-open-file" title="One model file, without adding its folder" onClick=${openFile}>Open one model file…</button>
    </div>
    <div class="home-card first-tips">
      <h2>Other folders</h2>
      <p><b>Any folder of extracted files</b>, such as .partsbnd, .chrbnd, .objbnd, .flver or .mesh files you've pulled out with other tools, or models FModel, UModel or Blender exported (.gltf, .glb, .psk, .pskx, .obj) with their textures beside them.</p>
      <p><b>Models Dark STL collected before</b> (on this computer or another): choose its library folder, or one game's folder in it, with <i>Add collected files</i>. Each game can come from either its own folder or its collected files.</p>
      <p class="muted">Once a game is listed, the models you want can be collected into the app, so the game's folder isn't needed afterwards.</p>
    </div>
  </div></div>`;
}
