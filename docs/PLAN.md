# Dark STL: plan

Dark STL is a desktop app for game models: a flexible import system (the FromSoftware games,
Cyberpunk 2077, the RE Engine games, Borderlands, Returnal and any Unreal 4/5 game, Grand Theft
Auto, DOOM Eternal and DOOM: The Dark Ages, and OBJ, glTF, FBX and PSK files from other tools), an intuitive library to
explore and organise them across games, and the tools to extract, manipulate and clean a model
before printing (configure, pose, clean, enhance). Print post-processing (making a model
watertight, cutting it into pieces, painting it for a multi-colour printer) belongs to the
companion app [Model Painter](https://github.com/DrFlGd/Model-Painter), which Export hands models
to; Dark STL's own Finish and Cut tabs and its painted 3MF moved there in 0.55.0 (Phase 8 of the
roadmap). Stands and bases are out of scope.

Enhancing a model takes three steps:

1. **Even out the vertices.** Resample the model to a user-chosen vertex count, with the
   vertices spread evenly over the surface.
2. **Displace with texture detail.** Push every vertex in or out along the surface normal
   by the height that the model's textures describe.
3. **Export.** Write STL, OBJ (with textures), PLY or GLB.

Step 1 comes first because game models put their vertices where the silhouette needs
them: a flat blade face may be two triangles while the guard has hundreds. Displacing those
vertices directly gives lots of detail in busy areas and none on the large flat ones. After
step 1 every area has the same vertex spacing, so the texture detail comes through evenly.

## How each step works

### Importers: one per game family, everything after is shared

Reading a game's files is the only part that depends on the game. Each importer turns its
files into one common model (`SourceModel` in `model.py`): a triangle mesh in metres, Z up,
with per-corner UVs and shading normals; its parts; its materials, saying which texture is
the albedo, normal or height map; the textures as images; and any switches the importer
needs (for example "place rigid parts using their bones"). The explorer, the three steps and
the exporters only ever see that model, so supporting another game means writing one
importer and registering it in `importers/__init__.py`:

| Importer | Reads | Status |
| --- | --- | --- |
| FromSoftware | FLVER2 models in BND/DCX archives, TPF/DDS textures. Per-game differences (markers, compression, PS4 textures, where material data lives) are profiles in `importers/fromsoft/games.py` | Dark Souls Remastered and Bloodborne checked on real files; the rest on synthetic files |
| REDengine 4 (Cyberpunk 2077) | `.mesh` and `.xbm` in the CR2W container, loose (WolvenKit exports under their depot paths) or inside the game's `.archive` packs, read directly; `.mi`/`.mt` material chains; multilayered materials (`.mlsetup`, `.mltemplate`, `.mlmask`) blended into one colour and one normal map; `.app`/`.ent` appearance packages for weapon configurations; Kraken buffers through the game's `oo2ext_7_win64.dll`. Formats worked out with WolvenKit as the reference | Built on synthetic files; awaiting a check against the game |
| RE Engine (Capcom) | `.mesh` models (every generation from RE2 to RE Requiem, streamed buffers included), `.mdf2` materials, `.tex` textures, read from extracted files or from `.pak` archives through a community file list. Games in `importers/reengine/games.py` | Synthetic RE2 and RE4 files; real files next |
| Unreal Engine 3 (Borderlands, Borderlands 2, The Pre-Sequel) | `.upk` packages (LZO, and whole-file compressed ones such as `Startup.upk`) with their `.tfc` texture caches: `SkeletalMesh` and `StaticMesh` objects, `Texture2D`, material instances followed to their parents, guns as configurable models from the `GD_Weap_*` part definitions with their skins mixed per part, `AnimSet` sequences for the Pose tab, names from the `.INT` text (`importers/borderlands/`) | Borderlands 2 guns and skins checked on a real install; the rest on synthetic files |
| Wavefront OBJ | OBJ + MTL with PNG/JPG/TGA/DDS textures, from any other tool | Done |
| glTF | `.gltf` (with its `.bin` and pictures) and `.glb`, as Blender, FModel and most tools export: each mesh a part placed by its node, base colour and normal textures (green up), Y up turned to Z up; a model too big to be in metres read as centimetres. Skinned meshes in their bind pose | Synthetic files (0.35.0) |
| Unreal Engine 4/5 games, by game profile | Any Unreal 4/5 game's `.pak` files and IoStore containers (`.utoc`/`.ucas`), or files extracted from them, with the readers the Borderlands importer brought (skeletal and static meshes, textures, material instances, animations). What differs per game is its JSON profile (`importers/profiles/*.json`, see [profiles.md](profiles.md)): Returnal, Borderlands 3, Wonderlands and Borderlands 4 ship one; a folder of a game no profile names is added under the name the user types, sorted by `unreal-defaults.json`'s folder rules, and a profile for it is written into the settings folder. Names from the game's string tables | Synthetic files (0.49.0); Returnal and Borderlands 3 as before |
| FBX | `.fbx`, binary (6.1 to 7.7) and text, as Blender, Maya, 3ds Max, Unreal and the modding tools write it, read by Dark STL's own reader (`importers/fbx/`): every mesh the scene places (a part per mesh and material), the file's axes and units turned into metres with Z up, materials' textures from the paths the file stores, by name in the export folder, or embedded in the file; skin clusters as skin weights with the skeleton and bind pose, and the file's animation stacks for the Pose tab and packages | Synthetic files (0.49.0) |
| ActorX PSK/PSKX | UModel's and FModel's exports of Unreal models: points, wedges, faces and materials, mirrored from Unreal's frame like the Unreal games' own models; each material's textures found by name beside the model, from UModel's `.mat`, FModel's `.json` and UModel's `.props.txt` material files, then from texture names like the material's. The skeleton isn't read | Synthetic files (0.35.0) |
| RenderWare (GTA III, Vice City, San Andreas) | `.img` archives (with `.dir`, or `VER2`) read in place, `.dff` models (parts placed by frames, skins, vehicles with their wheels copied to every axle and their colours from `carcols.dat` as variants), `.txd` textures (DXT, paletted and raw rasters), `.ifp` animations for peds, `.ide`/`.ipl`/`.gxt` for kinds, areas and vehicle names. Per-game differences are JSON profiles in `importers/profiles/` (`gta3`, `gtavc`, `gtasa`), read by the shared profile loader. See Reading Grand Theft Auto files | Synthetic files (0.50.0) |
| RAGE (GTA V, then GTA IV) | GTA V: `.rpf` v7 archives (open, AES, NG, nested) with keys found in the user's `GTA5.exe`, RSC7 resources in the original and the Enhanced layouts: `.ydr` drawables, `.ydd` ped components (each ped an outfit), `.yft` vehicles with their wheels, `.ytd` textures, `.ycd` animations, names from `.gxt2` and the `.meta` lists. GTA IV (and its episodes): IMG3 archives with the key found in `GTAIV.exe`, RSC5 `.wdr`, `.wdd` (outfits), `.wft` (vehicles with their wheels), `.wtd`, names from the `.ide` files and `.gxt` | Synthetic files (0.51.0 GTA V, 0.52.0 GTA IV) |
| id Tech 6/7/8 (DOOM, DOOM Eternal, DOOM: The Dark Ages) | `.resources` containers (IDCL; DOOM 2016's `.index`) and `.streamdb` with the game's Oodle, BIM textures, `.lwo` static and `.md6mesh` skinned models, `.md6skl` skeletons, `.md6anim` animations, `material2` decls. See Reading id Tech 6/7/8 files | DOOM Eternal (0.53.0) and DOOM: The Dark Ages (0.54.0) built, synthetic files only; DOOM 2016 waits on Jerred's sample files |

An importer implements `can_open` and `load`, and optionally `scan` (to list its models in a
folder), `detect_game`, `variants` (files holding several models) and `describe`.

### Model packages: a model written out once, opened without the game

Collect imports models as **model packages** by default (since 0.27.0; "Game files" still copies
the game's own files). Each model file becomes a folder named after it,
`<library>/<game> packages/<path in the game>.darkpkg/`, holding `package.json` (what the model
is: game, kind, group, name, the file it came from, the models the file holds) and per model a
glTF binary (`.glb`: mesh with one primitive per part and material, UVs, shading normals,
skeleton and skin weights, so Blender opens it as it is; the extra `_VERTEX` attribute names the
welded vertex each glTF vertex is, so it reads back exactly as the importer built it) and a
JSON sidecar (materials and texture roles, normal-map convention, sockets, notes, animations).
Textures and animations go into the game's asset store (`assets/` beside the packages), each
once: textures under their name in the game (kept compressed as the game keeps them, as DDS files,
where the importer gives them so: every importer does since 0.34, the Unreal and Cyberpunk ones by
wrapping the game's mips in a DDS header, `dds.wrap_mips`; else as the decoded picture, PNG: a
Cyberpunk map blended from layers, a Borderlands V8U8 normal map),
animations by content (the local matrix each bone takes per frame, sampled for every frame of an
animation at once), so armour pieces posed by the same animations share them. A whole game's import
writes four models at a time where the importer allows it (FromSoftware), and lists each of the
game's folders once. `package.py` writes and reads packages; `posing.py` poses a package's mesh from its
skeleton and weights (importers that can pose give a `Rig` and per-vertex weights for this);
`importers/package.py` lists and opens them like any other model, and the library prefers a
model's package to its game file. Configurable models (weapons with parts and skins) are imported
too: every file their configurations name is written as a package (with any model of the file only
a configuration names, kept but not listed), and the configurations are kept in the packages'
folder (`collected.darkstl-configs.json`), naming files by their path in the game, which finds
their packages. A Borderlands 2 skin, whose colour is mixed for each part it goes on (`paint_for`), is
kept as how it mixes (its `PaintRecipe`: the paint's values, and its textures in the asset store), and
each gun part's package keeps the two sets of texture coordinates the mix reads (`<name>.paintuv.npz`);
the colour is mixed when it's shown, from the packages alone. An importer whose packages come to hold
more raises its `package_version`, so packages it wrote before are written again.

**Every texture of a game** (Collect › Model packages › Every texture of the game, offered for a whole
game; `game_textures.py`) goes into the asset store too, not only those its models use. Each importer
lists a folder's textures (`list_textures`: FromSoftware's texture files, RE Engine's `.tex` files,
Borderlands 1/2/TPS's `Texture2D` objects in every package, the Unreal 4/5 games' `T_...` assets,
Cyberpunk's loose `.xbm` files and those in archives something in the game names) and opens them in
bulk (`open_textures`: FromSoftware reads each texture pack once). Before anything is written the plan
lists them, leaves out those stored already (the store's index remembers which listed texture each file
is), and estimates the size of the rest from a sample of 16. Not listed: FromSoftware textures kept
only inside model archives (their models store them), Unreal textures not named `T_...`, and Cyberpunk
textures nothing refers to by name (archives keep hashes only).

The **library overlay** (`overlay.py`) is one file per game in the settings folder
(`overlays/<game>.json`) holding the user's own changes, keyed by the model's path in the game
(the same for the game's file, a collected copy and its package), so they survive rescans and
importing. It holds:

- texture assignments made with the texture picker (Change… beside a material's maps on the
  Textures tab): the picked texture is stored in the game's asset store, and the model opens with
  it in place of the one it names. Importers list a game folder's textures for the picker with
  `list_textures` / `open_texture`;
- a model's own name and folder (Rename or move…: `Kind/Group`, any depth), and folder renames (a
  kind or group renamed or moved as a whole, from its summary). The library files each model by
  them as it lists it (`Library.arrange`), keeping the game's own place (`original_place`), which
  the model's summary shows. They don't change what a model opens as, so nothing generated is redone.

**Saved variations** (`variations.py`): the Displace tab's Saved variations keep the enhanced model
shown under a name, beside the model's package (`<path in the game>.darkpkg/variations/<id>/`, made
before the game is imported too, and kept when it is): `variation.json` (the name, the settings,
parts, options and pose it was made with, recorded with each Generate result, and what was shown of
it), `mesh.npz` (the mesh, positions as 32-bit floats) and `thumb.png`. Shown again it is the
model's enhanced model, so it is shown and exported like a fresh one; "Use its settings" chooses
what it was made with again. Records written before 0.55.0 can hold the Finish, Cut and paint
settings of then: `retired.py` leaves them out when a record is read and when it's written again
(as it does for the settings file and the bulk Enhance preset).

Models combined into one (Combine with other models…: a character's face, body and hair) are
configurations of the user's own (`configs.py`) whose base is the model and whose parts are the
others, each part a slot that is on by default. Configurations name their models by the path in
the game, which also finds the model's package, so a combined model holds once the game is
imported and removed.

### The app: a game library on Model Library's interface

The app is a page (`web/`) in front of the Python engine. The Windows app is a Tauri window
(`desktop/`) that runs the engine as `darkstl engine` (`app/engine.py`) and passes the page's
commands to it over its stdin and stdout, so it opens no port; a source install serves the page
from a small local web server (`app/server.py`) in pywebview's window, or in the browser with
`--browser`. The page is the
interface of Model Library and SCAD Workshop (Preact + htm, no build step, the same skins,
themes, command bar, inspector rails, menus and three.js viewer), so the three apps look and
work alike. Everything the page does goes through one command table (`app/service.py`), which
the tests call directly.

- **Game folders.** *Add game folder…* takes a game's install folder or any folder
  of extracted files. The game is recognised from its files (or chosen, or named freely), and
  the folder is scanned in the background. Folders are kept in the app's settings file and
  scanned again at start-up; each can be rescanned, moved to another game or removed. A folder
  inside one already added is refused; adding a parent folder takes in the ones inside it. A
  game still packed in its archives gets a note and an *Unpack…* button.
- **Unpacking (0.12.0).** `importers/fromsoft/unpack.py` with `formats/bhd5.py`: Dark Souls
  PTDE (plain BHD5), Dark Souls II (keys from its `*KeyCode.pem` files), Dark Souls III,
  Sekiro and Elden Ring (published keys in `formats/bhd5_keys.py`). The RSA public operation
  undoes the header encryption (kept decrypted in the cache afterwards), AES-128-ECB ranges are
  decrypted per file, and files are cut to their unpadded size. Names: bundled file lists
  (Smithbox's copies of UXM's), downloaded UXM Selective Unpacker lists in the settings folder,
  and names guessed by varying the last number of known ones (checked against the file's
  contents for the 32-bit hash games). Only model, texture and animation files of `chr/`,
  `parts/`, `obj/`, `asset/` and `map/` are written, into `<app library>/<game>/` (a folder of
  collected files), with the Oodle DLL, plus the item text (`msg/`) and tables (`param/gameparam/`,
  and Dark Souls III's `Data0.bdt` or Elden Ring's `regulation.bin` copied beside them) that the
  in-game names come from (0.44.0). Engine commands `unpack_plan`, `unpack_run`,
  `filelists`, `filelists_download`; CLI `darkstl unpack`. Dark Souls II `.bnd` models are now
  listed too (unchecked on real files).
- **One library per game.** The menu lists each game with its model count. A game's page shows
  its kinds of model (weapons, armour, characters...) as tiles; a kind shows its groups as chips
  and its models as cards or rows, searchable, with the selected model's details beside them.
- **In-game names (0.44.0).** `Importer.names(folder, game, entries)` gives the names the game
  itself uses, {model key: name}, called after each scan (and by a game's *Read in-game names*,
  which reads the files again). They're kept in the game's library overlay as its `game_names`
  layer; a model shows the user's name first, then the game's, then the one its file gives
  (shown muted under it). FromSoftware (`importers/fromsoft/names.py`): the item text
  (`msg/<language>/item*.msgbnd.dcx`, FMG files read by `formats/fmg.py`) joined through the item
  tables (PARAM files, `formats/param.py`: EquipParamWeapon's model number, EquipParamProtector's
  model number and slot bits, NpcParam's name id) from `param/gameparam/gameparam.parambnd.dcx`
  or the encrypted regulation file (Dark Souls III's `Data0.bdt`, Elden Ring's `regulation.bin`).
  A weapon model takes the name of the lowest row that uses it (the plain weapon, not its +1 or
  infusions); a character the name most of its rows give. The table is cached per folder by its
  files' sizes and dates. Packages keep the name, so a collected copy is named without the game.
  From 0.49.0: the Unreal 4/5 games' string tables (`importers/unreal/names.py`: `StringTable` assets
  whose keys end as a name, `Carbine_Name`, matched to the mesh of the same name, `SK_Carbine`, by the
  `names` rules of the game's profile), Borderlands 1/2/TPS's localised text (`importers/borderlands/names.py`:
  the `.INT` file beside each package, whose objects with a name text refer to the meshes they show,
  followed into other packages), and Cyberpunk's on-screen text (`importers/redengine/names.py`:
  `onscreens.json`, loose, WolvenKit's JSON or in `lang_en_text.archive`; a weapon's meshes are named after
  the text that is exactly its model's name, as the game joins items to text in TweakDB, which isn't read).
  The RE Engine games give none yet: their text (`.msg`) is joined to models through item data not read.
- **The library index and the search across every game (0.44.0).** `library_index.py` keeps a
  JSON file per game (`<cache>/library/index-<game>-<hash>.json`) written at each scan: every model's
  names, kind, file and what's known of it (size in mm, vertices, triangles, parts, textures,
  animations). Sizes and counts come from a quick read of the file where the importer can do one
  (`Importer.stats`: FromSoftware FLVER headers, packages' indexes or their glTF's head), read in
  the background after a scan, and otherwise when a model's preview is drawn or it's opened. The
  search (the box at the top of the menu, on Home, and its page `#/search?q=`; `/` goes to it) looks
  through names, in-game names, file names (and tags, once there are any) of every game, grouped by game, with chips for
  game, kind, has animations, has textures, a triangle range and collected or not.
- **List view columns (0.44.0).** Name, game (when searching every game), kind and group, size,
  triangles, parts, textures, animations and collected; a header sorts by it (again: the other way),
  and each page keeps its own sort in the preferences. Cards show "size · triangles" once known.
  Home has the search box and the last eight models opened above the game tiles.
- **Collections, tags and stars (0.48.0).** `library_collections.py` keeps one file for the whole
  library, `<settings>/library/collections.json`: named, ordered collections of models from any game,
  tags per model and a star per model. A model is referred to by its game and its overlay key (its path
  in the game, the same for the game's file, a collected copy and the package made from it; `config:<id>`
  for a configurable model), so nothing is lost when a folder is rescanned, moved, collected or added
  again: a model not in the library just isn't shown until it's back. The library gives every entry its
  marks through a `mark` hook (`app/organise.py`), so cards, rows, the search (a tag is searched like a
  name, and offered as a filter chip with Starred) and the library index show them. Page: right-click a
  model (or the model page's ⋯) for *Add to collection…*, *Tag…* and *Star*; cards drag onto a
  collection (or Starred) in the menu; a collection's page (`#/collection/<id>`, and `#/starred`)
  lists its models in its own order, dragged into place under the sort *Collection order*; Home shows
  them as tiles. Commands `collections`, `collection_create/rename/delete/add/remove/move`,
  `models_tag`, `models_star`, `tag_rename`; `models_query` takes `collection`, `tags` and `starred`.
- **Related models (0.48.0).** `Importer.related(entry, entries) -> [(relation, entry)]`:
  FromSoftware armour sets (`hd/bd/am/lg` of one number and sex), a weapon and its `_1` sheath or
  variant, a model and its low-detail copy, character and object variants (`catalog.family`); RE
  Engine faces, bodies and hair of one character folder (`reengine/characters.character_part`);
  packages ask the importer their model came from. The app adds a configurable model's base and
  parts, and the configurable models a model is part of. `importers/base.index_entries` keeps a
  per-importer index of the library so each lookup is a dictionary read. A card says "N related"
  (a menu of them), the details panel and the model page's Model tab list them, and Combine
  offers them first.
- **Choosing several, and the queue (0.48.0).** Click chooses a model, Ctrl+click adds or takes one
  off, Shift+click a run, Ctrl+A every model shown, Esc none (`useModelSelection` in `ui/models.js`).
  With several chosen the command bar and their right-click menu offer *Collect into the app*,
  *Export as GLB*, *Export packages*, *Enhance with the current settings*, *Add to collection…*,
  *Tag…* and *Star*. The bulk actions go to the queue (`app/bulk.py`): one item per action (Collect
  and packages one per game), run one after another as jobs of the job system, each model through
  the same commands the page uses for one (a bulk export writes what Export would; a bulk enhance
  generates with the settings that game's models were last generated with, `config.last_enhance`,
  else the defaults, and saves a variation). A model that fails doesn't stop the rest; it's listed
  with why. The Queue page (`#/queue`) shows each with its progress and Stop (a waiting one is taken
  off); the running one is in the status bar. Commands `queue`, `queue_add`, `queue_stop`, `queue_clear`.
- **Problems (0.48.0).** A game's page lists what needs a look, each with its fix (`problems`
  command): its folders' notes (still packed: *Unpack…*; gone: *Remove*), models that couldn't be
  read (*Export model files…*), model files gone since the scan (*Rescan*), models without textures
  and textures not found (*Choose texture…* on the Textures tab, *Find textures…* adds a texture
  folder and draws their previews again), and animations that couldn't be read. They're learnt as
  previews are drawn and models opened, and kept in the library index (`error`, `missing_textures`,
  `anim_error`; FromSoftware reports the texture slots that name a file it didn't find). Home has a
  line for each game with any.
- **Collect into the app.** A game install is tens of gigabytes; the files Dark STL reads for
  the models someone wants are a few megabytes. *Collect into the app…* (a game's ⋯ menu, a
  kind's or group's details, a model's right-click menu or the model page's ⋯ menu) copies the
  chosen models' files into the app's own library folder (`<settings>/library/<game>/`, movable
  in Settings): the model archive (or its unpacked folder), its texture archives and split
  texture packs, its animations, and, when asked, the player's animations and the low-detail
  copies, plus what the game needs as a whole (the Oodle library). Each file keeps its path
  below the game folder, so the same sibling lookups work on the copies. The dialog shows what
  would be copied and what is already there (same size and date) before copying; the library
  folder is then listed as a folder of that game, and a model present in both the game folder
  and the library is listed once. The game's folder can then be removed from the library, or
  the game uninstalled, and the collected models stay, with their textures and poses. Each
  importer says which files go with a model (`Importer.related_files`) and which a game needs
  as a whole (`Importer.shared_files`); `collect.py` plans and copies.
- **Previews.** Each card's picture is drawn by the engine the first time it scrolls into view
  (`thumbnails.py`: the model from the 3D view's angle, coloured from its albedo texture, long
  weapons laid corner to corner) and cached by file date, a few at a time so browsing stays quick.
  Since 0.48.0 characters are drawn in the first frame of an idle-like animation (idle, wait, stand,
  or FromSoftware's `a000_000000`) when they have one (Settings › General › Previews, on by
  default; their previews are kept under a key of their own), and each preview gets a turntable:
  eight views round the model in one strip (`thumbnails.turntable`, a z-buffered point scatter of
  the shaded surface, 0.3 s for 19,000 triangles), drawn in the background from the model just
  drawn, or when a card is first hovered (`thumb?turn=1`), and played on the card while it's
  hovered. The browser's toolbar has a small, medium or large card size.
- **Settings in three pages (0.48.0).** General (Exports, Model Painter, Previews, Appearance),
  Library (Game folders, App library, Texture folders) and Game tools (Oodle, Unreal games, Game
  archives): `#/settings`, `#/settings/library`, `#/settings/tools`. The first start shows a tile for
  each game Dark STL reads; clicking one says what folder to choose for it, with *Add this game's
  folder…*.
- **The model page.** The 3D view with the game's textures (or clay, or wireframe), and the
  inspector's tabs in the order the work happens, in three groups on the rail (each tab's icon
  with its short name upright under it; icons only when the page is crowded): **Model**
  (Configure, Model: parts (a read-only list since 0.45.0, with a link to Clean, where parts are left
  out), importer options, size, file; Textures; Pose: an animation and a frame, with a slider,
  stepping and playback, bones turned on top, saved poses and attached models, for models with a
  skeleton, sockets or models attached), **Edit** (Clean, Mesh, Displace; see "Cleaning" below)
  and **Export**. Since 0.43.0:
  - *One place chooses the model worked on*: the Show buttons under the 3D view (Original,
    Enhanced). Export says "Working on: Enhanced · change" (the link puts the keyboard on those
    buttons and makes them flash).
  - *Explanations fold away*: each tab's opening paragraph (Mesh, Displace) shows its first
    sentence, with the rest open until that tab's job has run once (the `tipsOpen` preference);
    ⓘ beside the heading opens it again.
  - *A Next strip* under each result opens the next tab: Mesh → Displace; Displace → Export;
    Export → Open in Model Painter.
  - *The Export tab leads with For Model Painter* (0.55.0): "To make it watertight, cut it into
    pieces or paint it for a colour printer, export it for Model Painter.", then Save a file.
  - Preferences saved before 0.55.0 still load: a page left on Finish or Cut opens on Export, a
    painted 3MF chosen as the format goes back to STL, and the retired tabs' own settings are left
    out of the next save (`web/ui/state.js` and `retired.prefs` in the engine).
  - Toasts show at the top right of the 3D view, never over the buttons under it. The footer
    stays one row: without room for every view button, shading, edges and the view's turns fold
    into one *View* menu. Under a 1440 px window the game's tree beside the menu folds as a model
    opens and comes back on the way back (opening or folding it there by hand is remembered for
    model pages, `catPanelModel`).
  - The Displace tab names its per-material columns (Texture, Strength); the Export tab shows its
    folder as the last two parts of the path (the whole path in its tooltip and while editing).
  Meshes travel to the page in a packed binary form (`app/meshpack.py`) split at UV seams and
  grouped by material.
- **The handoff to Model Painter (0.43.0).** Print finishing and painting move to Model Painter
  (github.com/DrFlGd/Model-Painter). The Export tab's *Export for Model Painter* writes the model
  shown as a GLB with its textures and materials, Y up, in metres, named
  `<model>_for_painter.glb` (the `painter` preset of the `export` command), and *Open in Model
  Painter* (beside *Show in folder*) starts Model Painter with the file, which opens it as the
  model it was started with. Where Model Painter is: Settings › Model Painter, or found by
  itself (`app/painter.py`: the per-user installer's folder, Program Files, or the portable
  zip's folder beside Dark STL's own or in Downloads). The engine checks the program and the
  file (`painter_target`); the desktop app's window starts it (`desktop/src/main.rs`), and
  `darkstl gui` and the local server start it from the engine (`painter_open`).
- **Safety.** The server listens on 127.0.0.1 only, checks the Host header (against DNS
  rebinding) and needs a token written into the page, so other web pages can't use it.

### Reading FromSoftware files

| Layer | Formats | Notes |
| --- | --- | --- |
| Compression | DCX (`DFLT` zlib, `KRAK` Oodle Kraken, `ZSTD`), DCP | Kraken (Sekiro, Elden Ring) uses the game's own `oo2core_*_win64.dll`, found in the game folder or set in the app. |
| Archives | BND3, BND4, BXF3/BXF4 (`.xxxbhd` + `.xxxbdt`) | Nested archives and `.dcx` members are opened on demand. |
| Models | FLVER2 (version 0x2000C to 0x2001A) | Every vertex layout the PC and PS4 releases use, including the packed ones (PS3 edge compression is not supported); triangle strips and lists; LOD and motion-blur face sets are skipped; rigid parts are placed by their bones. |
| Textures | TPF, DDS | BC1 to BC7 and the uncompressed formats. Bloodborne's PS4 textures have no DDS header and are tiled; they are rebuilt into DDS and untiled. |
| Animations | Havok HKX: packfiles (hk_2010 32-bit, hk_2014 64-bit) and tagfiles (2015 onwards, with Elden Ring's `.compendium`) | `Skeleton.HKX` and the `aXXX_XXXXXX.hkx` animations in a model's `.anibnd` (plus the `_divXX` / `_aXX` split archives and unpacked folders). Spline-compressed animations are decoded (ThreeComp40/48, Polar32 and uncompressed rotations); interleaved ones read as they are. |

Textures are looked up in this order: inside the model's own archive, in a sibling
`.texbnd` / `_h.texbnd` / `.tpf` (characters and objects), in unpacked folders next to the model,
and finally in any extra texture folders you add (map pieces share `mXX_XX_XX_XX_tex` packs).
Each material's maps are taken from the texture paths stored in the FLVER; when those are
empty (Elden Ring keeps them in MATBIN files) the suffix convention is used instead
(`_a` albedo, `_n` normal, `_h` height).

**Posing.** A model with an anibnd beside it can be held at any frame of any of its
animations (`animation.py`). The FLVER's bones are matched to the Havok skeleton by name; the
skeleton's bones take the animation's local transforms, the FLVER's extra bones (mesh, dummy
and cloth bones) keep their bind-pose ones under their parents. Skinned vertices follow their
bones' matrices by weight (`world · bind⁻¹`), rigid parts sit where their bone is. The pose is
part of the mesh options, so Generate and Export, the cache of generated models and the
previews all take it into account without knowing about animations. Root motion
(`extractedMotion`) is ignored: the model stays where its bind pose puts it.

RE Engine models are posed the same way (`reengine/animation.py`): the mesh's bone weights
(eight joints per vertex, six 10-bit ones in Street Fighter 6 and later) pick bones through the
mesh's skin list, and the game's `.motlist` / `.mot` motions (`formats/motion.py`, following
shadowcookie's REE-Lib, MIT) move them by name hash (MurmurHash3 of the UTF-16 bone name).
A model's motions are the ones filed under its character id (`pl0000`, RE4's `cha0` family
folder for `cha000`, Village's `ch01`): those of its own animation folders first, cutscenes
after, and only motion files that move one of the model's bones are offered. What each motion
file holds is kept in the importer's cache. Root bones' translation is left out, as above.

Unreal 4/5 skeletal meshes (Returnal, Borderlands 3/Wonderlands/4) are posed from the
`AnimSequence` assets of their `Skeleton` (`borderlands/ue4/anim.py`, `animset.py`, after UE Viewer
and CUE4Parse). The mesh's skin weights (`FSkinWeightVertexBuffer`, through each section's bone
map) and reference skeleton come from the mesh; the skeleton asset's reference skeleton maps each
animation's tracks (`CompressedTrackToSkeletonMapTable`) to bone names, matched to the mesh's.
Animations are found by reading the header of every package that looks like one (name or folder)
and keeping those importing the mesh's skeleton, cached per game
(`unreal-animations-<hash>.json`, with each animation's length). When that finds none for a
skeleton in a game that may name its animations otherwise (Returnal: `look_through_for_animations` in its profile),
or where nothing named like one is, every package whose name and folders don't rule it out is looked
through once; with none found, the Pose tab lists what was read (`AnimIndex.survey`). Keys in Unreal's own codecs
(constant or variable key spacing, per-track compression; float, fixed-point and interval
formats) are decoded in the 4.12-4.22, 4.23-4.24 and 4.25+ layouts; ACL-compressed animations
are recognised and reported. Rotations come from the animation; translations are the mesh's bone
offsets moved by the animation's change from the skeleton's reference pose; the root bone keeps
its place on the floor (only its height follows); additive animations are applied to the bind
pose. An animation's own data starts after its object GUID flag and its skeleton's GUID
(`UAnimationAsset::Serialize`; read without it too). Without type mappings (Returnal), the skeleton's and animation's own data are found past
their properties by signature, the frame count from the codec's header and the frame rate taken
as 30.

Unreal 3 skeletal meshes (Borderlands, Borderlands 2, The Pre-Sequel) are posed from the game's
`AnimSet` objects (`borderlands/animsets.py`, after UE Viewer's `UnAnim3.cpp`). An AnimSet names one
bone per track (`TrackBoneNames`) and holds its `AnimSequence` objects; nothing ties it to a mesh, so
a mesh is offered every AnimSet of which at least 80% of the tracks (and at least two) name its bones,
one of them a bone that moves its vertices. Every package's AnimSets are listed beside its other
objects in the scan's package lists (`package_contents(anim_sets=True)`; lists kept before 0.38.0
are read again when first asked), each AnimSet read once from its own package (else the smallest
holding a copy), and the result kept per game (`borderlands-animsets-<hash>.json`, with each
sequence's length, checked against every package's size and date). Sequences are decoded with the
Unreal 4 reader's codecs (the AnimSet's track order, 4 offsets per track, or 2 per track for
per-track compression) plus Borderlands 2's `PolarEncoded32/48` rotations, `Delta48` translations
and its own per-track format numbers; delta-compressed rotations aren't read (UE Viewer doesn't
either). Unreal 3 keys every bone but the root with its rotation conjugated relative to the mesh's
bones; translations come from the animation for the root, and for other bones only when the
AnimSet isn't `bAnimRotationOnly` (the default) or names them in `UseTranslationBoneNames`
(`ForceMeshTranslationBoneNames` keeps the mesh's); the root keeps its place on the floor. A
skeletal mesh with bones shows the Pose tab until the game's AnimSets are listed, then only if
one fits. Collect copies the packages its AnimSets are read from.

**Posing freedom (0.47.0).** On top of an animation's frame, or on the bind pose of a model
with no animations, any bone can be turned (`posing.py`, the same for every importer that gives a
`Rig`: FromSoftware and RE Engine models with or without animations, Unreal 3/4/5 skeletal meshes
whose animations are found, and the packages made of them). A turn is three
angles in degrees about the bone's own axes, X then Y then Z (three.js's "XYZ" Euler order:
`Rx · Ry · Rz`), kept by bone name in `MeshOptions.bones`. Each bone's local matrix is the bind
pose's, replaced by the animation's where it moves the bone, then multiplied by its turn on the
right; the hierarchy is composed again so children follow, and the bind-pose mesh is skinned with
its own weights (`world · bind⁻¹`). Without turns the importers pose the model as before. A model
made of others (configurable models, attached models) passes the turns to each part that has the
bone (`SourceModel.adjusts_bones`).
- *Mirror*: bones pair up by the side their names say (`L_`/`R_`, `_l`/`_r`, `Left`/`Right`,
  ` L `/` R `, `.L`/`.R`, `LUpperArm`/`RUpperArm`, `_L1`/`_R1`), only when the partner exists. A turn
  is carried across in the model's frame: `D = B·R·Bᵀ` with `B` the bone's bind rotation, reflected
  across the plane the two bones lie apart in (`S·D·S`, the axis along which the pairs' pivots
  differ most), then into the partner's frame (`R' = B'ᵀ·D'·B'`).
- *In the page*: the Pose tab lists the bones as a tree (those that move none of the model's
  vertices behind *Every bone*), with a find box; the chosen bone gets X, Y and Z sliders with
  boxes to type in, *Mirror to …*, *Reset bone*; *Reset all*, *Mirror left → right* and back.
  *Show bones in the 3D view* draws a ball at each bone's pivot (click one to choose it) and three.js's
  `TransformControls` rotate gizmo, in the bone's local space, on the chosen one; a drag is composed
  with the turn already there (`q_turn · q_drag`, a bone with mirrored axes conjugated) and sent when
  it ends.
- *Saved poses*: the animation, frame and turns kept under a name in the game's library overlay
  (`poses` of the model's entry, with the skeleton's signature: a hash of its sorted bone names),
  listed on the Pose tab to apply, rename or delete, and offered to every model of the game with
  the same signature. The footer under the 3D view names the pose shown; Export's file name, the
  Generate result's record and saved variations carry the turns.
- *Attached models*: a model of the library held by a bone or socket of this one, with an offset
  (millimetres, along the bone's axes), a turn and a scale (`attach.py`, kept in the overlay's
  `attach` list of the model, by the attached model's path in the game). The model opened is then a
  wrapper: the base's parts, materials and textures followed by each attached model's (named
  "Label / part"), its bind-pose mesh weighted whole to the holding bone, posed with the rest, so it
  follows every frame and turn and goes through Clean, Mesh, Displace and Export like
  the model's own parts. What is attached is part of what the page opens (the overlay's signature for
  the file); where each one sits is in `MeshOptions.attach`, so moving it only builds the mesh again.
  A socket holds it where the socket is in the bind pose. Up to 16 per model.
- *GLB export*: the original model exported as GLB carries its skeleton in the pose shown (each
  bone's local matrix in the export frame, Y up and scaled like the mesh), the posed mesh bound to it
  with its four largest weights per vertex, so Blender opens it posed and rigged. An enhanced model's
  GLB carries the skeleton in its pose without weights (remeshing doesn't keep them).
- *With the Clean tab (0.45.0)*: the pose, the bones' turns and the attached models make the mesh
  the Clean tab's steps work on (pose → Clean → Mesh). Its transform (print height, turn, mirror,
  move, on the floor) is kept as a matrix with the result (`clean.Cleaned.affine`), which moves the
  bones shown on the Pose tab and the GLB's skeleton with the model.

The library groups models by their FromSoftware naming:

| Prefix | Category | Grouped by |
| --- | --- | --- |
| `wp_` | Weapons | ID range (`IDs 02xx`) |
| `hd_` `bd_` `am_` `lg_` | Armor | Head, Body, Arms, Legs, with Male/Female |
| `fc_` `fg_` `hr_` `ey_` ... | Faces & Hair | part (Face, Hair, Eyes...) |
| `c0000`, `cXXXX` | Characters | Player, then family (`c11xx`) |
| `oXXXXXX`, `aegXXX_XXX` | Objects | ID range (`o12xxxx`) or asset group (`aeg099`) |
| `mXX_XX_XX_XX_...` | Map Pieces | map (`m10_00`) |

Low-detail copies (`_l`) are hidden by default when the full model is there too; a lone
low-detail copy is listed. The game is detected from the folder (executable, `dvdroot_ps4`,
or the folder name), and otherwise from the paths stored inside the archives, which name the
studio's project (`N:\SPRJ\...` is Bloodborne, `N:\FDP\...` Dark Souls III), so a folder of
loose extracted files is recognised too.

### Reading Cyberpunk 2077 files

Every REDengine resource is a CR2W file: tables of strings, names and imports (the depot paths
of the resources it refers to), then chunks, each a class instance serialised property by
property as `name, type, size, value`, then buffers (vertex data, pixels), each raw or
Kraken-compressed behind a `KARK` header. Because every property names its type, the reader
is generic (`Red` objects with plain Python values) and the mesh and texture readers pick what
they need; properties of types it does not know are kept as bytes and skipped by their size.

- **Meshes** (`CMesh`): the `rendRenderMeshBlob` header lists the chunks (sub-meshes) with
  their vertex layouts (per-stream packing elements) and one buffer holds every stream and the
  index buffer. Positions are signed 16-bit over `quantizationScale/Offset`, normals 10-10-10-2,
  UVs half floats. All levels of detail are chunks of the same file, told apart by `lodMask`;
  the full level is kept by default. Game space is right-handed, Z up, metres: nothing is rotated.
- **Textures** (`CBitmapTexture`): the setup's compression picks the codec (BC1, BC3, BC5 for
  normal maps, BC7, raw formats) and the PC render blob's buffer holds the mips largest first,
  decoded like DDS surfaces.
- **Materials**: a chunk's `CMaterialInstance` (kept in the mesh, or an external `.mi`) chains
  through `baseMaterial` to the `.mt` template; the merged parameters name the textures
  (`BaseColor`/`DiffuseTexture`, `Normal`/`NormalTexture`/`GlobalNormal`, `Roughness`,
  `Metalness`, detail normals). Multilayered materials blend tiled layers at run time; only
  their global normal map is used and the model says so. Appearances are the model's variants.
- **Archives** (`RDAR`): files keyed by the FNV-1a hash of their lower-case depot path, in
  segments (resource, then buffers). No names are stored, so the first scan reads every
  resource's import table and names the meshes that entities, appearances and other meshes refer
  to; the result is cached per archive. Members are listed under the pseudo path
  `<pack>.archive\<hash>.mesh`. Collecting extracts them under their depot paths, so the copies
  read like a WolvenKit export.

### Reading Grand Theft Auto files

The series runs on three engines, and each gets one importer family. The plan below was worked
out from the published format descriptions (gtamods.com wiki), librw (MIT, the RenderWare
reimplementation behind re3), CodeWalker (MIT, GTA V), and OpenIV, SparkIV, DragonFF and
Sollumz as cross-checks (GPL or closed: read for layouts only, nothing copied). No game key
is shipped: where an archive is encrypted, the key is found in the user's own executable at
scan time and kept in the settings folder, as the Oodle library is.

| Games | Engine | Archives | Models | Textures | Animations | Names |
| --- | --- | --- | --- | --- | --- | --- |
| GTA III, Vice City, San Andreas (PC) | RenderWare 3.1-3.6 | `.img` with a `.dir` beside it (III, VC) or `VER2` single file (SA), loose files in `models/` | `.dff` | `.txd` | `.ifp` (`ANPK` III/VC, `ANP3` SA) | `.ide` definitions, `.gxt` text |
| GTA IV, Episodes from Liberty City | RAGE, first generation | `IMG3` archives (`pc/models/cdimages`, `pc/data/maps`), table encrypted with AES-256 | `.wdr`, `.wdd`, `.wft` (RSC5) | `.wtd` | `.wad` | `.gxt`, `.ide` |
| GTA V (Legacy, then Enhanced) | RAGE, second generation | `.rpf` version 7: open, AES or NG encrypted; DLC packs in `update/x64/dlcpacks` | `.ydr`, `.ydd`, `.yft` (RSC7) | `.ytd` | `.ycd` | `.gxt2`, `vehicles.meta`, `.ymt` |
| Trilogy Definitive Edition | Unreal Engine 4.26 | `.pak` | through the Unreal readers | | | string tables |

#### RenderWare: GTA III, Vice City, San Andreas (`importers/renderware/`)

- **Chunks.** Every RenderWare file is a tree of chunks: `type`, `size`, `library id` (u32
  each), then `size` bytes. The library id encodes the RenderWare version
  (`((id >> 14) & 0x3ff00) + 0x30000 | ((id >> 16) & 0x3f)`, or `id << 8` for the oldest
  files): 3.1/3.2 in III, 3.3/3.4 in VC, 3.6 in SA. Layouts that changed with the version are
  picked from it, not from the game. Unknown chunks are skipped by their size, so plugins we
  don't read (collision, 2D effects, reflections, night colours) cost nothing.
- **Archives.** III and VC: `gta3.dir` holds 32-byte entries (offset and size in 2048-byte
  sectors, a 24-byte name) for the raw `gta3.img`. SA: `VER2`, a count, then the same entries
  with the size split into two u16s. Members are addressed as pseudo paths
  `<archive>.img/<name>.dff` (as RE Engine pak entries are), so the library, previews,
  packages and Collect work unchanged. `player.img` (SA clothes) and `cutscene.img` are read
  the same way.
- **Models (`.dff`).** A Clump holds a Frame List (per frame a 3x3 rotation, a position, a
  parent index, with a name and an HAnim bone id in its extensions), a Geometry List and
  Atomics (a frame and a geometry each). A Geometry has flags (positions, normals, prelit
  colours, one or two UV sets), triangles stored as `v1, v0, material, v2` (u16), one morph
  target with the positions and normals, a Material List (colour, texture name and mask name,
  material effects) and, for peds, the Skin plugin: bone indices and weights for four bones
  per vertex and an inverse bind matrix per bone (each preceded by `0xDEADDEAD` before 3.4).
  Game space is right-handed, Z up, metres, as Dark STL's: nothing is rotated. Each atomic is a
  part, placed by its frame; geometry without normals gets them averaged.
- **Textures (`.txd`).** A Texture Dictionary of Texture Natives: platform 8 (Direct3D 8, III
  and VC) or 9 (Direct3D 9, SA), name, mask name, raster format, size, mip count, then the
  palette (PAL8 or PAL4) and each level's pixels. Rasters are DXT1/3/5 (decoded through
  `texture2ddecoder` like the other importers), 8-bit paletted, or raw 8888, 888, 1555, 565,
  4444 and LUM8, in BGRA order. All are colour textures in sRGB. The games have no normal maps,
  so Displace uses the grayscale-height path from the colour texture.
- **Which textures a model uses.** The model's line in an `.ide` file names its `.txd`; SA's
  `txdp` sections chain dictionaries to parents; vehicles also read `vehicle.txd`, and anything
  still missing is looked up in `generic.txd`, then in any dictionary of the archive by name
  (with a note on the model saying the match was guessed).
- **Kinds and groups.** `gta.dat` (`default.dat` first) lists the `.ide` files. Their sections
  decide the kind: `peds` → Characters (group from the ped type: civilians, gangs, cops,
  special and cutscene characters), `cars` → Vehicles (group from the type: car, bike, boat,
  heli, plane, trailer), `weap` → Weapons, `objs`/`tobj`/`anim` → Map pieces and props, grouped
  by the `.ide` file's area (`data/maps/LA/...`, `SF`, `vegas`, `generic`), `hier` →
  Cutscene objects. The `.ipl` files (text, and San Andreas' binary `bnry` streams in `gta3.img`)
  say where each map object is placed; a map object's information names the area that places it. A `.dff` no `.ide` names (SA's `player.img` clothes,
  upgrades, `_l` LODs) goes under Other, low-detail copies hidden as for other games.
- **Vehicles.** A car's frames name its pieces (`chassis`, `door_lf_hi_ok`, `bonnet_dummy`,
  `wheel_rf_dummy`...). Each atomic is a part placed by its frame; `_dam` (damaged) and `_vlo`
  (low detail) parts are switched off by default. The game copies the one wheel to every
  `wheel_*_dummy` (mirrored on the left), and so does the importer. Paint: materials coloured
  with the placeholder colours (primary 60,255,0 and secondary 255,0,175; SA adds 0,255,255
  and 255,0,255) take the car's colours from `carcols.dat`; each colour pair is a variant, and
  the material colour is baked into the colour texture so the package and exports carry it.
- **Animations (`.ifp`).** III and VC: `ANPK` blocks (`INFO`, `NAME`, `DGAN`, per bone `CPAN`
  `ANIM` then `KR00`/`KRT0`/`KRTS` key frames: quaternion, position, scale, time). SA: `ANP3`
  with 24-byte names, per bone a frame type, a frame count and an HAnim bone id, frames as
  floats or compressed int16 (quaternion / 4096, position / 1024, time / 60). Peds share the
  skeleton, so `ped.ifp` poses every ped (bones matched by HAnim id in SA, by name in III/VC);
  other `.ifp` files (`anim/*.ifp`, SA's `anim.img`) add their animations to the peds that use
  them (the `.ide`'s animation file).
- **Names.** Vehicles' `.ide` lines carry a text key resolved through the game's `.gxt`
  (III: `TKEY`/`TDAT` UTF-16; VC: tables; SA: version 8 tables keyed by the CRC32 of the key),
  so `landstal` shows as "Landstalker". Weapons, peds and map pieces keep their model names (the
  games' text names weapons by slot, not by model).
- **Differences between the three games** (archive version, IFP and GXT format, colour
  placeholders, executable and folder layout, where `ped.ifp` lives) are a game profile each.
  They are JSON files in the shared profile folder (`importers/profiles/gta3.json`, `gtavc.json`,
  `gtasa.json`) naming their engine backend (`renderware`), read by the same loader as the other
  games' profiles: a file of the same name in the settings folder's `profiles/` folder replaces one
  (see `docs/profiles.md`). Bully: Scholarship Edition and Manhunt are RenderWare games that could follow with a
  profile and their own archive readers.
- **Collect.** A package keeps the mesh with its parts and skeleton, the baked paint variants,
  every texture the model reads (its dictionary, parents, `vehicle.txd` and `generic.txd`
  entries), the sampled `.ifp` animations for peds, and the in-game name. "Game files" Collect
  extracts the members from the archives under `models/<archive>/<name>` and copies the data
  files the importer reads (`gta.dat`, the `.ide` files, `carcols.dat`, the `.gxt`, the
  `.ifp` files), so the collected folder reads like a game folder.

#### RAGE: GTA V and GTA IV (`importers/rage/`)

GTA IV and V share most of the resource design, so one package holds both, with the
generation-specific layouts side by side.

- **RPF7 archives (GTA V).** Header: magic `RPF7`, entry count, names length, encryption
  (`0` or `OPEN` none, `0x0FFFFFF9` AES, `0x0FEFFFFF` NG). The entry table (16 bytes each:
  directories with their first child and count; files with a name offset, size and offset in
  512-byte blocks, and for resources the system and graphics flags) and the names follow,
  encrypted as the header says. Files inside are encrypted the same way, and archives nest
  (`x64v.rpf/models/cdimages/componentpeds_p.rpf/...`), which the pseudo paths follow.
  The NG cipher picks one of 101 keys by the JOAAT hash of the file name plus its length and
  runs 17 table rounds. The AES key, the 101 NG keys and the decryption tables are found in
  the user's `GTA5.exe` by searching for blocks whose SHA-1 matches the hashes CodeWalker
  publishes (MIT), once, and kept in the settings folder (`keys/gta5.bin`); the executable is
  looked for in the added install folder (`GTA5.exe` or `GTA5_Enhanced.exe`). If the search fails (a patched or
  protected executable), the folder note says so, and a folder of files exported with OpenIV
  or CodeWalker still works, since exported resources are not encrypted.
- **IMG3 archives (GTA IV).** Magic `0xA94E2A52`, entry table (size, resource type, offset,
  sizes) and names, encrypted with AES-256 (ECB, applied 16 times). The key is found in
  `GTAIV.exe` / `EFLC.exe` the same way (SHA-1 of every 4-byte-aligned 32 bytes against the hash
  SparkIV publishes) and kept as `keys/gta4.bin`. Built in 0.52.0 as `formats/img.py`,
  `formats/keys4.py`, `formats/rsc5.py` and `formats/drawable4.py`, which reads GTA IV's blocks into
  the same objects as GTA V's, so the rest of the importer (library, vehicles, outfits, Collect) is
  shared. The episodes (`TLAD/`, `TBoGT/` in the Complete Edition) are read with the base game, their
  models' groups named after the episode.
- **Resources.** RSC7 (V) and RSC5 (IV): a header with the resource version and the sizes of
  the system and graphics parts (encoded in flags), then the data deflated (zlib). Pointers in
  the data point into those parts (`0x5xxxxxxx` system, `0x6xxxxxxx` graphics), and one reader
  follows them for every resource type.
- **Drawables (`.ydr`, `.wdr`).** A shader group (each shader's parameters name its textures:
  `DiffuseSampler`, `BumpSampler`, `SpecSampler`; V can embed a texture dictionary), a
  skeleton (bones with rotation, translation, scale, parent and bone tag), and up to four
  levels of detail; the high one is read. Each geometry has a vertex declaration (which of
  position, blend weights, blend indices, normal, colours, UVs and tangent are present, and in
  what format) and a u16 index buffer. Game space is right-handed, Z up, metres.
- **Dictionaries and fragments.** `.ydd`/`.wdd` hold several drawables by name hash: GTA V
  ped components (`head_000_r`, `uppr_001_u`, `lowr_000_u`, `feet_*`, `hair_*`, `accs_*`...).
  `.yft`/`.wft` fragments are vehicles and breakable props: a main drawable with the skeleton
  plus child pieces, each a part (doors, bonnet, wheels at their bones; damaged and low-detail
  pieces off by default).
- **Textures (`.ytd`, `.wtd`).** Dictionaries of textures with name, size, mip count and
  Direct3D format (DXT1/3/5, BC5 for normal maps, BC7 in V, A8R8G8B8, L8), decoded like DDS.
  A model finds its textures in its own or embedded dictionary, then the parents named in
  `gtxd.meta` (V) or the `.ide` (IV), then the shared `vehshare` dictionaries for vehicles.
  Normal maps' green direction is found on real files and set per game (auto-detect until then).
- **Peds as combinable components.** A ped is its skeleton plus one drawable per component
  slot, each with texture variants (`uppr_diff_000_a_uni`, `_b_`...). The ped's variation file
  (`.ymt`, PSO binary in V) lists what goes together; Dark STL lists each ped as a configurable
  model (see Configurable models): a slot per component, options for each drawable and texture
  variant, assembled on its shared skeleton and posed together. GTA IV peds work the same way
  from their `.wdd`, with fewer slots.
- **Animations.** V: `.ycd` clip dictionaries (animations as sequences of compressed channels:
  quantised floats, cached quaternions, linear and constant tracks), bones matched by bone tag.
  IV: `.wad` dictionaries. Ped animations are found by the clip sets the ped uses and offered
  on the Pose tab.
- **Kinds, groups and names.** From the archive path: `componentpeds`, `streamedpeds`,
  `cutspeds` → Characters; `vehicles.rpf` and DLC vehicle archives → Vehicles (group by class
  from `vehicles.meta`); `weapons.rpf` → Weapons; `props` → Props; `levels/gta5/...` → Map
  pieces grouped by area. Display names from `.gxt2` (hash of the key, then UTF-8 text) for the
  `gameName` in `vehicles.meta` and the weapon names in `weapons.meta`; GTA IV from its `.gxt`.
  Weapon attachments (`weaponcomponents.meta`) become configurable weapons, as Cyberpunk's do.
- **Scale.** GTA V's archives hold over 100,000 drawables. The scan reads only archive tables
  (cached per archive, like Cyberpunk's), and the library lists without opening resources.
- **Enhanced edition.** GTA V Enhanced (2025) keeps RPF7 but changes some resource layouts
  (textures, shaders, vertex declarations). Both are read (0.51.0): each resource's version says
  which layout it has (as CodeWalker's gen9 reader tells them apart), so a folder can mix them.
- **Collect.** A package keeps each model as for other games (a ped's chosen components and
  every texture variant, a vehicle's parts and paint, sampled animations). "Game files" Collect
  extracts the members decrypted and still compressed as RSC files under their archive paths,
  as OpenIV exports them, plus the `.meta`, `.ymt` and `.gxt2` files the importer reads.

#### Mobile and console versions

Jerred asked on 2026-11-09 for the mobile and console releases too, after the PC games. They are
the same games with different packaging, so each is a profile plus the readers for what differs,
on the importer of its PC version. The user supplies their own copies: an Android `.obb` or the
app's data folder, or files dumped from their own discs or consoles; Dark STL ships no keys and
breaks no copy protection. In order of how much they share with what is built:

| Release | Engine | What differs from the PC version | Work |
|---|---|---|---|
| III, Vice City, San Andreas: Android and iOS (War Drum, 2011-2015) | RenderWare | Models are ordinary `.dff` in `.img` archives; textures are not `.txd` but texture databases (`texdb/<name>.txt` listing each texture, with `.dxt`, `.etc`, `.pvr` or `.unc` `.dat`/`.toc` files holding them per GPU). San Andreas mobile is 2.0+ with its own `.img` names | A texture-database reader (DXT as on PC; ETC1 and PVRTC decoders), the `.obb` as a zip folder, profiles. Smallest step |
| III, Vice City, San Andreas: Xbox | RenderWare | Native geometry (the Xbox native data plugin instead of triangle lists) and Xbox `.txd` rasters (DXT and swizzled RGBA) | Native geometry and raster readers; profiles |
| III, Vice City, San Andreas: PlayStation 2 | RenderWare | PS2 native geometry (VIF packets of strips, quantised positions) and GS rasters (swizzled, 4/8-bit palettes, PS2 alpha) | The largest RenderWare step: VIF unpacking and GS unswizzling |
| GTA IV: Xbox 360 and PS3 | RAGE (IV) | Big-endian `.img` (as on PC) holding `.xdr`/`.xtd` (360) or `.cdr`/`.ctd` (PS3); 360 resources compressed with LZX, textures tiled for the GPU | A big-endian reader for the RSC5 blocks (same layout), an LZX decoder, Xbox 360 untiling and PS3 swizzling |
| GTA V: Xbox 360 and PS3 | RAGE (V) | RPF7 big-endian, `.xdr`/`.cdr` resources, LZX (360), each console's own archive key (in the dumped executable, found by hash as on PC) | As GTA IV's consoles, on the GTA V layouts |
| GTA V: PS4, Xbox One, PS5, Xbox Series | RAGE (V) | The PC layouts (PS5/Series are Enhanced's), but archives keyed per console and executables encrypted on the console | Only if the user can supply decrypted files; otherwise exported files (which already open) |
| Definitive Edition (all platforms, mobile included) | Unreal 4 | As the PC Definitive Edition | The generic Unreal importer's profile, as planned above |

Each step is tested on byte-built files first, then on Jerred's own files. The Android releases come
first (one new reader serves three games), then Xbox, then PS2, then the RAGE consoles (which need
an LZX decoder and GPU untiling, more work than any one game so far).

#### Trilogy Definitive Edition

The Definitive Edition is Unreal Engine 4.26 with encrypted `.pak` files. It comes in through
the generic Unreal importer and a game profile (0.49.0); its AES key goes in Settings > Unreal
games like Returnal's. Nothing GTA-specific is planned for it unless its folder layout needs
more than a profile.

#### Phases

1. **Plan** (this section).
2. **RenderWare trilogy (0.50.0).** Archives, `.dff`, `.txd`, `.ide`, `.ipl` areas, `.ifp`
   posing for peds, vehicles with parts and paint, `.gxt` names, profiles for III, VC and SA,
   Collect and packages. Needs nothing else; uses 0.49.0's profile format.
3. **GTA V (0.51.0).** RPF7 and keys from the executable, RSC7, `.ytd`, `.ydr`, `.ydd` peds as
   configurable models, `.yft` vehicles, `.gxt2` and `.meta` names, then `.ycd` posing. Legacy
   first, Enhanced after a check on real files.
4. **GTA IV and Episodes (0.52.0).** IMG3, RSC5 and the `w*` resources, on the shared RAGE
   reader. Comes after GTA V lands, since most of its code is shared.
5. **Definitive Edition**: a profile on the generic Unreal importer, when someone has the files.

Everything is first built and tested on byte-built files (`tests/synth_rw.py`,
`tests/synth_rage.py`); each PR says what has only been checked that way.
### Reading id Tech 6/7/8 files (DOOM, DOOM Eternal, DOOM: The Dark Ages)

Requested by Jerred on 2026-11-09; built in three releases, one per game, by a single importer
(DOOM Eternal in 0.53.0, The Dark Ages in 0.54.0, both checked only on synthetic files so far), `importers/idtech/`, with a profile per game. The layouts below were worked out from
[Valen](https://github.com/jandk/valen) (Jan De Kock, MIT: DOOM Eternal and The Dark Ages, the
most complete reader of these games' models, textures and animations), EntityAtlan
(FlavorfulGecko5, MIT: The Dark Ages' containers, StreamDB and Oodle use), EternalResourceExtractor
and SAMUEL (Eternal's `.resources` and `.streamdb`, GPL: read only as a cross-check) and DOOMExtract
(emoose: DOOM 2016's `.index`/`.resources`). Dark STL re-implements them; no code is copied. All
values are little-endian unless said otherwise.

| Game | Engine | Folder recognised by | Containers | Compression | Oodle |
| --- | --- | --- | --- | --- | --- |
| DOOM (2016) | id Tech 6 | `DOOMx64vk.exe` / `DOOMx64.exe`, `base/*.index` | `.index` (table) + `.resources` + `.patch` / `_NNN.patch` | raw deflate (entry sizes differ) | not needed |
| DOOM Eternal | id Tech 7 | `DOOMEternalx64vk.exe`, `base/packagemapspec.json` | `IDCL` v12 `.resources`, `.streamdb` | Oodle Kraken | `oo2core_8_win64.dll` beside the exe |
| DOOM: The Dark Ages | id Tech 8 | `DOOMTheDarkAges.exe`, `base/packagemapspec.json` | `IDCL` v13 `.resources`, `.streamdb` | Oodle Kraken / Leviathan | none shipped (built into the exe): Eternal's `oo2core_8` (found by itself when `DOOMEternal` is beside it in the Steam library), any `oo2core_9_win64.dll`, or Settings › Oodle |

**Containers (Eternal, The Dark Ages).** `base/packagemapspec.json` lists every container
(`files`), the maps (`maps`) and which files each map loads (`mapFileRefs`); `common`,
`warehouse` (and `init` in The Dark Ages) hold what every map shares, each level's own files
hold the rest. A `.resources` file starts with an `IDCL` header (version 12 or 13, one segment,
counts of resources, dependencies, dependency indices and string indices, the string table size,
and 64-bit offsets of the string table, meta entries, resource entries, dependencies, special
hashes and data; version 12 adds a 64-bit meta size). Each 144-byte resource entry holds the
index of its strings, the data's offset, stored and full size, checksum, time stamp, its
`defaultHash` (which keys its streamed data, below), a version, flags, the compression mode
(0 stored, 1 zlib, 2 Kraken, 4 Kraken behind a 12-byte chunk header, 5 Leviathan, 6 BCPack) and a
platform variation. The string table is a count, 64-bit offsets and null-terminated strings;
an entry's type is `strings[stringIndex[e.strings]]` and its name `strings[stringIndex[e.strings + 1]]`.
Names are resource paths with options after `$` (`models/monsters/imp/imp_albedo.tga$bc7srgb$mtlkind=albedo`).
The same resource sits in several containers (a copy per level, older copies in patches);
`generated/buildgame/container.mask` in `meta.resources` gives, per container (keyed by the
FarmHash64 fingerprint of its entry table), a bit per entry saying which copies are live. Dark
STL indexes each game once (types, names, where each live copy is; kept in the importer's cache
keyed by the containers' sizes and dates) and reads an entry when a model needs it.

**StreamDB.** Large data is kept outside the containers, in `.streamdb` files (magic
`0x61C7F32E29C2A550`, a header with the entry count and flags, then entries of a 64-bit id, a
32-bit offset in 16-byte units and a 32-bit length): the full levels of detail of every model's
geometry and the large mips of every texture. A piece is Oodle-compressed when its stored length
differs from the size the resource expects. Ids are derived from the resource's `defaultHash`:
in Eternal `defaultHash << 4 | n` (n = level of detail for geometry, `mipCount - mipLevel` for a
texture mip); in The Dark Ages a boost-style `hash_combine` of the hash with MurmurHash3's
`fmix64` of two small numbers (the stream's index, 0).

**Textures (`image`, BIM).** Magic `BIM` and a version byte (21 in Eternal, 24 and later in The
Dark Ages, which adds a byte), the texture type (2D, 3D, cube), its material kind (albedo,
specular, normal, smoothness, cover, mask...), width, height, depth, mip count, the albedo
specular scale and bias, the format (an enum: BC1/BC3/BC4/BC5/BC6H/BC7, their sRGB forms,
RGBA8, RG8, R8, half and float formats), whether it is streamed and how many of its mips sit in
the StreamDB; then a 36-byte record per mip (the same size in The Dark Ages) and the mips kept
in the container. Decoded through `texture2ddecoder` and wrapped as DDS for packages like the
other importers. Albedo is sRGB; normal maps are two-channel BC5, decoded with an empty blue channel as every
game's are (the height step works Z out, and a package's DDS copy shows the same picture); their
green direction is to be found on a known flat sample and set in the profile.

**Static models (`model`, `.lwo` names: props, pickups, map pieces).** A header (reference
point, level-of-detail count, surface count, deviations, streamable), per surface the material
decl name and up to five levels of detail (vertex and index counts, a vertex mask naming the
attributes, bounds, position offset and scale, UV offset and scale), lightmap settings and
decal projections. Small models keep their one level inline; streamable ones keep, per level,
memory layouts (one per combination of attributes: each attribute's stream offset and the index
offset) and a disk layout (compression, sizes) of a StreamDB piece. Attributes: positions as
three 16-bit unsigned normals times scale plus offset (or floats), normal and tangent as signed
bytes with the tangent's sign in the last byte, UVs as 16-bit unsigned normals or floats with
their own scale and offset, colour, lightmap UVs; 16-bit indices. Space is Z up, already
right-handed; the unit (id units, taken as inches) is set in the profile and checked against a
character of known height.

**Skinned models (`baseModel`, `.md6mesh` names: monsters, characters, weapons).** A header
naming the skeleton (`.md6skl`), a joint remap table, bounds, per mesh its name, material decl
and five levels of detail (as above), material infos, decal data, and per level a memory and disk
layout of its StreamDB piece. Skin weights for four joints are packed into the spare bits of the
normal and tangent bytes, joint indices into the colour attribute (4, 6 and 8 joints have
their own attributes), and indices go through the remap table plus the mesh's joint offset.
**Skeletons (`skeleton`, `.md6skl`)**: a header of 16-bit offsets, then per joint (padded to a
multiple of 8) a local rotation (quaternion), scale and translation, the parent table, the
inverse bind matrices (3x4) and the names. **Animations (`anim`, `.md6anim`)**: the skeleton's
name, bounds, frame count and rate, constant rotations, scales and translations for the joints
that don't move, RLE joint lists, and frame sets, each holding every animated joint's first value,
a bit per frame saying where a key is, and the keys; rotations are 48-bit "smallest three"
quaternions. Animations are matched to a model by skeleton name; the list is built once per game and cached.

**What The Dark Ages changes (0.54.0).** *Static models*: a level of detail record starts with -5
(Eternal: -2) and has three more numbers before the vertex mask; decal projections are three
quaternions (Eternal: two); streamable models keep only a disk layout per level, no memory layouts.
*Geometry pieces*: without memory layouts, each surface's attribute streams follow one another in
vertex-mask order, each starting on a multiple of its record size, then its 16-bit indices, and
the next surface starts on 8 bytes. A model's level `n` is StreamDB id `hash_combine(hash,
hash_combine(fmix64(4 - n), fmix64(0)))`. *Skinned models*: the skeleton's name, bounds, then two
joint tables instead of a remap table (the skinned joints, and extra joints that repeat earlier
entries: the table is the skinned joints padded to 8, then `resolved[extra[i]]` per extra joint),
level-of-detail count and deviations, blend shape names, a bound radius per skeleton joint (so
the skeleton is read first), meshes (name, material, three numbers, per level its counts, joints
per vertex, bounds, scales, mask and a joint offset), surface infos, wounds, geo decals and per
stream a wound offset and disk layout. Joints per vertex 4, 6 or 8 sit in a skinning stream
(`SKINNING_4/6/8`: the joint bytes, then the middle weights as bytes /255); the last three
weights are packed in the normal's and tangent's spare bits (7 bits /254, 4 bits /45, /75 or /104,
4 bits /60, /89 or /120 for 4, 6, 8 joints) and the first is what's left. `SKINNING_1` takes the
joint from the normal's spare byte; with no skinning stream joints are in the colour as in
Eternal. Dark STL keeps the four heaviest joints and rescales them. *Skeletons*: the header starts
with 32-bit size, loaded size, base pose and inverse base pose offsets and two hashes, then the
joint count and 16-bit table offsets. *Animations*: an attachment count after the skeleton name,
a longer data block (stream method: kept in the container, streamed, or streamed per level of
detail), joint lists with a 16-bit size whose top bit says the joints are 16-bit, quaternions
scaled by 1/32768 (Eternal 1/32767), key bits read from the end of each joint's row, and frame
sets of 48 bytes ending with their level of detail. Streamed frame sets are in StreamDB piece
`hash_combine(hash, hash_combine(fmix64(0), fmix64(layout)))`, at offsets the stream info (after
the data block) lists; of per-level frame sets, the finest are read. *Materials*: `filePath`s
leave out `.tga`. Images and containers were covered in 0.53.0 (version 24 images, IDCL 13).

**Materials (`generated/decls/material2/<name>.decl`).** Text decls in id's syntax
(`{ inherit = "...", edit = { RenderLayers = { item[0] = { parms = { albedo = { filePath = ... } } } }, Parms = {...} } }`),
inheriting from parents that are merged in. Texture roles come from the parm names:
`albedo`/`decalalbedo`/`hairalbedomap`... → albedo, `normal`/`decalnormal`... → normal,
`specular`, `smoothness`, `bloommaskmap`. The image a parm names is found by its path, choosing
among the resource's `$`-option variants the one whose `$mtlkind=` matches the role.

**DOOM (2016).** `base/*.index` files start with `RES` and a version byte, then (big-endian)
the entry count and per entry an id, three length-prefixed strings (type, name, path), the
offset, size and stored size, and the number of the file the data is in (0 `.resources`,
1 `.patch`, n `_00n.patch`); stored size ≠ size means raw deflate. Its models (`bmd6model`,
`bmodel`), textures (`bimage`) and animations are earlier versions of the formats above, but no
open reader for the retail game exists: Phase 2 starts from a handful of Jerred's files (one
monster, one weapon, one prop, their textures) and pins the differences from Eternal's layouts.

**Catalog.** Models are sorted by their resource path: `md6/monsters/<name>/...` and
`md6/characters/...` → Characters, group by monster; `md6/player/...` → Characters › Player;
`md6/weapons/<name>/...` (first-person) and `models/weapons/...` → Weapons, group by weapon;
`models/pickups/...` → Objects › Pickups; `models/props/`, `models/mapobjects/` → Objects;
architecture and level geometry → Map Pieces; gore, wound and damage copies get their own
"(damage)" group beside the model's, lower-detail copies are hidden by default. Paths not matching
a rule fall back to their first folder. Not built yet: in-game names from the `entityDef` decls
(`generated/decls/entitydef/...`, whose render model names the md6) and the `.blang` string
tables (`strings/english.blang`) through the names hook (R2); 0.53.0 labels models by file name.

**Collect and packages.** Model packages (the default) hold the mesh, skeleton, weights,
materials, textures (as DDS) and sampled animations, so nothing of the game is needed. *Game
files* writes each resource a model reads (its model, skeleton, animations, decl chain and
images), decompressed, under `base/<container>.resources/<resource name>` in `<library>/<game>/`,
each with a `.idmeta` sidecar (type, hash, version, sizes), and its StreamDB pieces decompressed as
`base/streamdb/<id>.bin`, so the copies open without the game and without Oodle; the importer
reads such a folder like an install, and the copies keep the install's paths, so library names
and folders carry over.

**Layout.** `importers/idtech/`: `formats/` (`resources.py` IDCL containers and the container
mask, `farmhash.py` the fingerprint the mask is keyed by, `index6.py` DOOM 2016's `.index` (to come), `streamdb.py`, `image.py`, `geometry.py` vertex masks
and streams, `staticmodel.py`, `md6model.py`, `md6skel.py`, `md6anim.py`, `decl.py` the decl
parser), `games.py` (the three profiles: executable, container version, hash scheme, unit,
normal green, what is shared; a game profile in the settings folder with `"engine": "idtech"`
changes the unit, normal green and executables, see docs/profiles.md), `store.py` (a
game's index: names → live copies, streamed pieces, loose extracted files), `materials.py`,
`catalog.py`, `animation.py` (poses for the Pose tab and packages) and `__init__.py`.

**Phases.** 0.53.0 DOOM Eternal (containers, mask, StreamDB, images, static and skinned models,
skeletons, animations, materials, catalog, names, Collect, packages); 0.54.0 DOOM: The Dark Ages
(the changed model, skeleton and animation layouts above, streamed animations); DOOM (2016) and
VFR (its `.index` and the older layouts) next, from Jerred's samples, as the next free version.
Every format is tested on byte-built files.
Risks: Oodle for The Dark Ages (no DLL in its folder; Eternal's, found beside it, or another
`oo2core` needed, said in the app when missing); The Dark Ages's vegetation and strand-hair
models aren't read; BCPack entries (consoles) are skipped; id's unit and the normal-map
green direction are confirmed on the first real files; DOOM 2016 depends on samples.

### Step 1: even vertex spacing

Two methods, both in the app and the command line:

- **Adaptive subdivision** (default). Every edge longer than a limit is split at its midpoint,
  with the limit lowered step by step; the last step splits the longest remaining edges one
  at a time, so the result has exactly the target vertex count and even spacing. New
  vertices sit on the original triangles, so UVs are interpolated exactly and there is no
  texture drift, and hard edges stay sharp.
- **Isotropic remesh.** Hits the target count closely and gives near-equilateral triangles of
  one size everywhere (ACVD clustering, then every new vertex is projected back onto the
  original surface to recover exact UVs). Useful when the triangle shape matters, for example
  for further sculpting.

Both methods can **round off curved surfaces**: each new midpoint is bent onto the curve that
the model's own shading normals describe (Phong tessellation at edge midpoints). A
16-sided grip comes out round instead of keeping 16 flat facets, flat-shaded parts stay flat,
and hard edges stay closed. Where a long side shares averaged normals with a small end cap
(common at the end of grips and scabbards), only the lean both ends of an edge agree on is
used, so the side stays straight instead of swelling into a bulge. This is on by default.

### Step 2: displacement from textures

- **Normal maps (default).** FromSoftware models carry detail mostly in normal maps, so the
  normal map is turned into height. Every edge of the resampled mesh gets a target height
  difference: the slope the normal map gives at the edge's middle (read bicubically) times
  the edge's extent in texels, times the measured size of one texel on the model, so
  strength 1.0 means "the relief the normal map implies". One sparse least-squares solve
  over the welded mesh (`geometry/integrate.py`, multigrid through pyamg when installed)
  finds the vertex heights that match them best. UV seams are ordinary edges, so the
  surface is continuous without any seam fix-up, and a light screening term (heights pulled
  weakly to zero over the "Detail radius") stands in for a high-pass. The older route,
  integrating each texture on its own and matching islands at the seams, is still there
  ("Turn slopes into height: on each texture").
- **Flat-normal bias.** 8-bit and BC1 normal maps cannot store "flat" exactly, which leaves
  a constant slope that integrates into a tilt across the part. The median slope of the
  near-flat texels is subtracted first.
- **Options** (Enhance > More settings): edge keeping (re-weighted least squares, so slopes
  no real surface could have stay local), compression clean-up (a sub-texel blur, or a 1x AI
  deblocking model), smoothing of stair-stepped edges along their length (0.21.0), upsampling of the normal map (guided by the albedo's edges, or a 2x/4x
  AI model such as 4x-Normal-RG0 run through ONNX Runtime), specular/metallic mask edges
  as hints, fine relief guessed from the delit albedo, the material's tiled detail normal
  map, and a strength per material. Since 0.5.0 the Windows app includes two AI upscalers,
  RunDevelopment's 4x-Normal-RG0-BC1 and -BC7 (CC BY 4.0), as `.onnx` files in
  `engine\models` beside the engine (desktop/models/bundle.py converts them from PyTorch at
  build time); with no model picked the one trained for the texture's compression is used.
  The upscaled slopes keep the texture's own broad shape (only the model's detail finer than
  the original texels is added), and AI upscaling is skipped where the texels are already
  finer than the vertices. Users can still add their own `.onnx` files to the models folder.
- **Test bench.** `python -m darkstl.bench` bakes known relief (bumps, plates, engraving,
  cloth) into normal maps (8-bit, simulated BC5 and BC1), maps it onto a panel with a
  mirrored UV seam and reports the error of each method. At 70k vertices integration on the
  mesh cuts the error against v0.1 by 14 to 59 % on every scene; edge keeping and the blur
  clean-up measured worse on these scenes, so they are off by default.
- **Thin parts (0.46.0).** Relief is sized by the texture alone, so a claw tip a few
  millimetres thick can be given more relief than it is thick, pushing its two sides through
  each other, and a cape a few millimetres from the body can be pushed into it. Before the
  vertices move, `geometry/room.py` measures every vertex's room on both sides: a ball
  touching the surface at the vertex is grown inward and outward until it meets another part
  of the surface (the shrinking-ball method of Ma et al. 2012 on the evenly spaced vertices,
  with a KD-tree for nearest points). Inward room is half the thickness behind the vertex;
  outward room is half the gap to whatever is in front of it. Each height *h* is then capped
  softly by the room *R* on its side: *h' = uR tanh(h / uR)*, where *u* is "Room the relief
  may use" (Displace › More settings, 80 % by default). Small heights stay as they were, large
  ones bend away from the room and none reach it, and the cap fades out where nothing was met
  within the distance looked, so open and thick areas keep all their relief. Each vertex only
  looks 8 times as far as the largest relief two rings around it, so vertices that barely move
  cost next to nothing; on dense meshes the room is measured at about 150,000 vertices and the
  rest take the tightest of those near them. Where a part is thinner than the vertex spacing
  (a blade's edge, the last millimetre of a point) the cap alone still leaves folds, so the
  move directions of vertices using much of their room are blended with their neighbours',
  and triangles still crossing are found (`geometry/intersect.py`) and the heights around them
  damped, a few rounds. The Enhanced panel and the warnings say how many vertices were lowered
  and by how much. The test bench's thin scenes (a 4 mm blade, three 60 mm claws, a cape 3 mm
  behind a body; normal maps of 3-texel relief) count the triangle pairs that pass through
  each other, at 70k vertices:

  | Scene | Relief | Crossing before | Without | Cap and calmer directions | With (repaired) | Relief kept | Vertices lowered | Seconds (thin parts / whole step) |
  |---|---|---|---|---|---|---|---|---|
  | Blade | 2.57 mm | 0 | 6,145 | 450 | 0 | 87.1 % | 17,963 | 1.1 / 1.8 |
  | Claws | 1.38 mm | 0 | 6,171 | 0 | 0 | 98.4 % | 1,452 | 1.0 / 1.6 |
  | Cape | 4.45 mm | 0 | 245 | 0 | 0 | 94.1 % | 10,788 | 1.3 / 2.0 |

  At 1M vertices (4 cores) keeping thin parts adds 6.7 to 9.0 s to a 10 to 12 s displacement
  (claws 11.2 → 17.9 s, blade 11.9 → 21.4 s, cape 10.2 → 17.9 s), and every crossing found
  (2,104, 9,384 and 88) is repaired; 88 to 99 % of the relief is kept. Displacement of the
  relief bench's panels is unchanged (nothing on them is thin).
- **Thickness shading (0.46.0).** The 3D view's *Thickness* shading colours the model by the
  thickness behind each point, against the thinnest wall the printer makes (0.8 mm unless
  changed) at the height it will be printed (typed in the small panel over the view; when empty,
  the size shown: the game's, or the print height the Clean tab scaled it to): red under it, orange to yellow up to two and four times it, clay beyond. Single
  sheets (capes, banners: no thickness of their own) are red. It is measured the same way as
  the room above, inward only and against surfaces facing towards or away from the point (so a
  box's corner doesn't make its walls look thin), on an evenly spaced stand-in of up to 300k
  points for coarse game meshes. Game parts overlap, so a closed part counts as solid
  throughout, a ball inside it ends only where it does (not against another part pushed into
  it), and surfaces hidden inside another closed part are left out (shown as thick enough).
  The engine sends the thickness with the mesh (`model_mesh` with `thickness=1`, one float per
  vertex in the packed mesh); the page recolours at once when the height or the wall changes.
- **Height / grayscale maps.** Used directly, with a chosen height range in millimetres.
- **Green channel.** DirectX and OpenGL normal maps flip green. FromSoftware's games all use
  DirectX, so their models use that. For other models it is detected per texture (the
  convention whose slopes integrate without curl, measured after a slight blur so
  block-compression noise doesn't decide it), and it can always be forced.
- **Texture detail finer than the vertex spacing** is blurred out first, so it cannot alias
  into noise.
- **Seams.** The mesh is welded before resampling, so it never tears along UV seams.
  Integrating on the mesh needs nothing more. With per-texture integration a height is only
  known up to a constant on each UV island, so each island is shifted so the islands agree
  along their seams as well as possible, and what still differs is faded smoothly into each
  island over a few vertex spacings.

### Cleaning (Clean tab, before step 1)

Game models carry what shouldn't be printed: a body under the armour, a lining inside a cuirass, capes
and cloth with no thickness (often inside a bigger part, not a part of their own), stray faces. The
**Clean** tab (first in the Edit group, `clean.py`) takes them off before the Mesh step, and sets the
model's print size. Since 0.45.0.

- **A list of steps per model.** Each step is a JSON object (`op` and its settings) kept in the
  page's prefs (`cleanPlans`, the last 20 models) and saved with each variation (`mesh.clean`). The
  engine does the list again on the model as built and posed (`SourceModel.build_mesh`: after the
  pose and the parts, before everything else), so a step can be taken away at any time, and the
  list is part of every cache key (`MeshOptions.clean`, keyed by a hash of the list). The tab shows
  each step as a row with what it did ("removed 2 parts, 1,204 faces") and a button that takes it
  away. Steps run in order; the transform runs last.
- **Faces by stable ids.** A step that removes faces records them as part index and the face's place
  in that part (`part << 32 | index`, kept as runs per part), so it finds the same faces with other
  parts left out, and finds nothing (saying so) when its part is off. Faces a step adds (filled
  holes) get ids in a part of their own (`1 << 20` plus a hash of the step).
- **Parts in or out**: the part checkboxes (moved here from the Model tab) and *Remove parts hidden
  inside others*: a connected shell whose sample points are 99% inside another part's closed shape.
  The inside test is a winding number from rays cast along a slightly turned axis over a binned grid
  of the other part's triangles; open shells are closed first where their openings are small
  (`_closed`), so an armour piece open at the neck still counts as a shape.
- **Hidden faces** deeper than a depth (mm): each face's centre and corners are tested inside every
  other part's shape, and the distance to that shape's surface (a KD-tree over triangle samples plus
  the exact closest point, searched no further than the depth) must reach the depth. Faces close to
  an opening (an arm entering a sleeve) stay.
- **Thin sheets** (Jerred's cloth with little or no thickness): (a) *open* shells whose openings
  would take more than 30% of their area to close (a cape); (b) *two-sided* cloth, two layers within
  0.02 mm of each other back to back; (c) a *thin* layer, where the surface behind a face (the
  closest opposing face, along its inward normal) is nearer than the thickness asked for. Thin faces
  are joined across edges and with the faces facing them, and narrow rim slivers between the two
  sides join too, so a flap of cloth in a bigger part comes out as one region. The tab lists regions
  biggest first (at most 40, the smaller ones counted), each highlighted in the 3D view when pointed
  at; the ones ticked (none to start with) are removed as one step.
- **Pick and remove**: a click on the model picks the face under it and spreads across edges where
  the surface bends less than an angle (`FaceGraph.flood`); Shift+click adds, Alt+click takes away;
  the picked faces show in the 3D view and are removed as one step. The 3D view's triangles are
  matched to the engine's faces through the order the mesh is sent in (sorted by material).
- **Repair**: small holes filled (openings up to a size closed with a fan to their middle, taking the
  material and texture coordinates around them), duplicate two-sided faces dropped (the same
  triangle twice, either way round), vertices welded within a distance (faces that close up
  dropped), flipped faces turned (each surface's faces made to agree, outwards on a closed one, by
  2-colouring the doubled edge graph and choosing per shell by volume or by the shading normals).
- **Transform** (one step, changed in place): print height (mm, after the turns) or scale, quarter
  turns and free angles about X, Y, Z, mirror across X, Y, Z, move (mm), stand on the floor. From then
  on the model is in print millimetres: the stage footer, Model › Size, the Textures and Displace
  readouts and Export show print millimetres with the game's own muted beside them, and finders
  (hidden faces, sheets) measure on the model as it is shown.
- **Command line**: `--clean` steps on `export` and `enhance`: `hidden-parts`, `hidden-faces[:mm]`,
  `sheets[:mm]`, `holes[:mm]`, `duplicates[:mm]`, `weld[:mm]`, `normals`, `scale:`, `height:`,
  `rotate:x,y,z`, `mirror:xyz`, `floor`. Finders there work by rule (what they find now) rather than
  by recorded faces.
- **The app's side**: `model_stats` (also `clean_check`) does the list and returns each step's row;
  `clean_find` (a job: hidden parts, hidden faces, sheets), `clean_select` (a click's flood) and
  `clean_ids` (the 3D view's triangles as stable ids). Addresses of meshes name the list by the page's
  hash of it, which the engine learns from the JSON sent first (the last 64 kept).

### Making it watertight, cutting into pieces, painting: in Model Painter

Dark STL had a Finish tab (*Make watertight*, 0.17.0), a Cut tab (pieces with pegs, dowels,
magnets or a key, 0.9.0) and a painted 3MF export (0.22.0). Model Painter
([github.com/DrFlGd/Model-Painter](https://github.com/DrFlGd/Model-Painter)) ported all three (its
0.1.0 to 0.3.0), and they moved there in 0.55.0: Export › *Export for Model Painter*, then *Open in
Model Painter*. Their settings in a settings file, a saved variation or the bulk preset written
before then are ignored when read and left out when written again (`retired.py`); `-o model.3mf`
on the command line says to export a `.glb` for Model Painter and stops.

### Configurable models: a base and parts chosen per slot

Weapons in Cyberpunk 2077, the RE Engine games and Borderlands are kept as a body and separate
part models (magazines, sights, barrels, stocks, muzzles). A *configuration* (`configs.py`) names
the base model and, per slot, the parts that fit it and where each goes; choosing a part per slot
(or a preset that chooses them all) gives one assembled `SourceModel`, so the Mesh, Displace and
Export steps need nothing new. The library lists each configuration as a model of its own
(*Configurable: N slots, M parts*), and its page opens on a **Configure** tab.

Relationships come per game from lists, the later winning when two share an id: what an
importer works out from a folder (its `configurations` hook: Cyberpunk weapons are read from the
game's `.app` appearance files, each appearance's components giving the slots, options, body
finish and presets, and its `.ent` slot components the attachment points; RE Engine weapons, and
Cyberpunk folders without appearance files, are guessed from the part files beside each weapon),
lists shipped with the app
(`importers/<importer>/configurations/*.json`), `*.darkstl-configs.json` files in a game folder,
and the user's own list, which *Edit…* and *Make configurable…* write. The list format is in
`docs/configurations.md`. Parts are placed in place (game parts usually are), on a named socket of
the base or of another chosen part (`SourceModel.sockets`, which importers fill), and/or by a
matrix, offset, turn and scale.

### Step 3: export

| Format | Content |
| --- | --- |
| STL (binary or ASCII) | Geometry only, Z up, millimetres by default |
| OBJ + MTL + PNG | Geometry, UVs, one material per game material with its albedo map, Y up |
| PLY (binary) | Geometry and normals |
| GLB | Geometry, UVs and embedded albedo textures, Y up, in metres (glTF's unit), with the skeleton in the pose shown (0.47.0); *For Model Painter* is this, named `<model>_for_painter.glb` |

Units can be mm, cm, m or inches; the game's own scale is metres.

## Code layout

```
src/darkstl/
  model.py      SourceModel: what every importer produces and everything else uses
  importers/    base.py (the Importer interface) and the registry
    fromsoft/   formats/ (binary reader, DCX + Oodle, BND3/BND4/BXF, TPF, PS4 untiling, FLVER2,
                Havok HKX packfiles and tagfiles), games.py (per-game profiles), files.py (finds
                a model, its textures and its animations on disk), materials.py (albedo / normal /
                height per material), mesh.py (FLVER -> mesh, skinned to a pose), animation.py
                (skeletons, spline-compressed animations, poses), catalog.py (classifies models
                by FromSoftware naming), names.py (in-game names from the item text and tables:
                formats/fmg.py, formats/param.py)
    redengine/  formats/ (CR2W container, .archive packs, CMesh render blobs, CBitmapTexture),
                depot.py (depot paths -> loose files or archive members), materials.py (.mi/.mt
                chains -> texture roles), mesh.py (chunks -> mesh), catalog.py (names learnt
                from import tables, groups by depot path)
    reengine/   formats/ (mesh, tex, mdf2, pak with MurmurHash3 names), games.py (RE Engine games and
                how a folder is recognised), files.py (a model, its streamed half, materials and textures,
                on disk or inside a pak, and its animations), materials.py (texture roles from slot
                types), catalog.py (classifies models by the engine's folder layout), animation.py
                (.motlist / .mot motions -> poses, skinning)
    borderlands/  Unreal Engine 3 (Borderlands 1/2/TPS): formats/ (UPK packages, LZO, properties,
                skeletal and static meshes, textures), catalog.py, files.py, materials.py, mesh.py,
                weapons.py and configurations.py (guns from their part definitions), gunpaint.py (skins
                mixed per part), animsets.py (posing), names.py (`.INT` text); ue4/ (the Unreal 4/5
                readers: pak, IoStore, unversioned properties and `.usmap`, meshes, textures,
                materials, animations) that unreal/ builds on
    renderware/ GTA III, Vice City, San Andreas: formats/ (chunks, dff, txd, img, ifp, gxt, text
                lists), games.py, files.py, catalog.py, model.py, animation.py
    rage/       GTA V and GTA IV: formats/ (RPF7 and IMG3 archives, keys found in the executable,
                RSC7 and RSC5 resources, drawables, textures, clips, text), games.py, files.py,
                catalog.py, model.py, animation.py
    profiles/   game profiles: the loader (__init__.py: shipped files, the settings folder's over them,
                extends, overrides for the engines kept in code) and the shipped *.json (docs/profiles.md)
    unreal/     Unreal 4/5 games by profile: profile.py (a profile's rules: sorting into kinds and groups,
                release, keys, names), catalog.py (recognising a game's folder, listing its models,
                probing an unknown game), names.py (string tables), __init__.py (UnrealImporter, the
                generic importer Returnal's derives from)
    returnal/   Returnal: the Unreal importer with Returnal's profile
    fbx/        FBX: reader.py (binary and text node trees), scene.py (objects, connections, geometry
                layers, transforms, axes and units, skins, bind poses, animation curves), __init__.py
                (FbxImporter: meshes, materials and textures, skeleton, animations)
    wavefront.py  OBJ + MTL
    gltf.py     glTF 2 (.gltf, .glb) from Blender, FModel and other tools
    psk.py      ActorX .psk/.pskx from UModel and FModel, and their material files
    loose.py    the loose pictures in a folder of other tools' models, listed for the texture picker
    package.py  model packages (see package.py), listed and opened like any game's models
    idtech/     formats/ (IDCL .resources + container mask, StreamDB, BIM images, static and md6
                models, skeletons, animations, decls), games.py (per-game profiles), store.py (a
                game's index of live resources, or a collected copy), materials.py (material2 decls
                -> texture roles), catalog.py, animation.py (poses): DOOM Eternal, The Dark Ages
  geometry/     TriMesh, subdivision, isotropic remesh, UV seam matching, closest-point queries,
                displacement, room.py (room around thin parts, thickness, keeping thin parts from
                clipping), intersect.py (triangles passing through each other)
  textures/     DDS decoding, normal map -> height, grayscale height, UV coverage, texel size
  library.py    the library: game folders scanned by every importer, by game, kind and group
  configs.py    configurable models: configuration lists, finding their files, assembling a choice
  collect.py    copies the files chosen models need into the app's own library folder
  package.py    model packages: writing and reading them, the asset store (textures, animations)
  game_textures.py  every texture of a game kept in its asset store: listed, estimated, stored
  posing.py     posing a mesh from a skeleton and per-vertex weights (packages)
  overlay.py    the library overlay: the user's own changes per game (names, folders, textures) and
                the names the game gives its models
  library_index.py  the library index: every model of a game with its size and counts, for the search
  library_collections.py  the user's collections, tags and stars, across every game
                and the list's columns
  variations.py saved variations: enhanced models kept beside the model's package
  thumbnails.py preview pictures for the library's cards, cached on disk
  pipeline.py   the three steps with their settings
  attach.py     models attached to a bone or socket of another (a sword in a hand)
  access.py     what a model reads while it's opened (whole files, pieces of archives), for Collect
  listing.py    a game folder's files listed once per scan, and whether a folder has changed
  review.py     Export model files: a model's files and a report of what was made of them, in a zip
  bench.py      the displacement test bench (known relief in, error numbers out)
  clean.py      the Clean tab's steps: hidden parts and faces, thin sheets, picked faces, repairs, transform
  retired.py    what the Finish and Cut tabs and the painted 3MF (in Model Painter since 0.55.0) left
                in saved settings, variations, presets and page preferences, ignored and dropped
  export.py     STL, OBJ, PLY, GLB writers
  cli.py        command line
  app/          the desktop app: service.py (the page's commands), engine.py (the engine the
                Tauri window runs, over stdin and stdout), server.py (local web
                server), meshpack.py (meshes for the 3D view), config.py, desktop.py (window),
                painter.py (where Model Painter is, and opening an export in it),
                organise.py (collections, tags, stars, related models, problems), bulk.py
                (the queue of bulk actions)
  web/          the page: Model Library's interface (styles.css, ui/ layout, inspector,
                menus) with Dark STL's pages: home.js, browser.js, search.js (every game),
                models.js (cards, list rows and columns, choosing several), modelpage.js,
                configure.js, clean.js, settings.js, organise.js (collections, tags, bulk menus,
                related), collection.js (a collection's page, Starred), queue.js, problems.js
tests/          synthetic game files built byte by byte (no game data in the repo)
```

## Roadmap

### Milestones so far

Each release's notes are in [releases/](releases/).

| Releases | What they brought |
| --- | --- |
| 0.1 to 0.6 | The FromSoftware readers and the three steps (even out, displace from normal maps, export); the Windows app (Tauri, no Python needed, isotropic remesher included); posing with the games' Havok animations; Collect into the app; the bundled AI normal-map upscaler; the Mesh, Textures and Displace tabs |
| 0.9 to 0.21 | Cut into pieces; Cyberpunk 2077; the RE Engine games, straight from their paks; FromSoftware archives unpacked by the app; the Configure tab; every Borderlands game; Make watertight; a faster start-up; jagged-edge smoothing |
| 0.22 to 0.26 | Painted 3MF; Borderlands 2 gun skins; Returnal; collected games working with the game uninstalled |
| 0.27 to 0.35 | Model packages, the asset store and the library overlay (Phase 5): texture picker, own names and folders, Combine, saved variations, faster import, every texture of a game, glTF and PSK files |
| 0.36 to 0.42 | Unreal 3, 4 and 5 models posed with the games' animations; Export model files for review; Redraw previews; Returnal fixes from Jerred's own files |
| 0.43 to 0.49 | The build-out after the November 2026 review: the model page in work order and the Model Painter handoff, in-game names and one search across games, the Clean tab, thin parts kept safe and the Thickness view, posing freedom, Library II, game profiles, any Unreal 4/5 game, FBX |
| 0.50 to 0.54 | Grand Theft Auto III, Vice City, San Andreas, IV and V; DOOM Eternal and DOOM: The Dark Ages |
| 0.55 | Finish, Cut and the painted 3MF moved to Model Painter (Phase 8) |

### Where things stand (2026-11-09)

- **Done:** Finish, Cut and the painted 3MF moved to Model Painter in 0.55.0 (Phase 8).
- **Next:** in-game names for the RE Engine games.
- **Waiting on Jerred:** DOOM (2016) and DOOM VFR need sample files from the game; the mobile and
  console versions of GTA wait for his go-ahead; the checks on his own installs in Phase 1 (Cyberpunk
  2077, Returnal's first-person models, Grand Theft Auto, the DOOM games, the RE Engine games) and
  Library II on his libraries.

### Phase 0: foundations (the first pull request)

- [x] Readers for every container, model and texture format above
- [x] Library scan and grouping, with search
- [x] Step 1 with both methods and curved-surface rounding
- [x] Step 2 from normal maps or height maps
- [x] Step 3 to STL, OBJ, PLY, GLB
- [x] Explorer window and command line
- [x] Tests on synthetic files for every format and step

### Phase 1: check against real game files

Everything so far is tested against files built to the published format descriptions, not
against files from the games. This phase loads real samples from each game and fixes what
differs:

- [x] Dark Souls Remastered weapon: the katana (`WP_A_0402`, blade and scabbard) loads,
      enhances and exports
- [ ] Dark Souls (PTDE and Remastered): armor, character, map piece. `c2509.chrbnd` turned
      out to be an empty placeholder that only carries textures; the app now says so
- [ ] Dark Souls III: same set
- [x] Bloodborne: character (`c1120`, 22 parts), object (`o000612`) and armour
      (`am_f_9300_l`) load, enhance and export; the PS4 texture untiling is right for BC1, BC4
      and BC7. Found and fixed on the way: two of the character's normal maps were read with
      green flipped, a lone low-detail armour file was hidden, and loose files weren't
      recognised as Bloodborne
- [ ] Sekiro and Elden Ring: same set with Oodle, and Elden Ring's material lookup
- [ ] Rigid parts placed by bones match the in-game pose
- [x] Characters posed by their animations: Bloodborne's `c1120` (hk_2014 packfiles) at any
      frame; Dark Souls Remastered's `c2240.hkx` skeleton (tagfile) reads. Still to check on
      real files: Dark Souls PTDE (hk_2010), Dark Souls III, Sekiro and Elden Ring (compendium)
- [ ] Compare against the before/after example: same weapon, same look
- [ ] RE Engine: a Resident Evil 2 and a Resident Evil 4 character, weapon and object, extracted and
      straight from the paks (the readers follow the community's format descriptions and pass on
      synthetic files; the normal maps' green direction and the texture roles of each game's slot
      names need a look on real files)
- [x] Borderlands 2: guns with their skins on Jerred's install (0.24.1)
- [ ] Returnal: turrets pose right on Jerred's install (0.38.1); the Hulk, the Spore Wizard and the
      player suits were fixed from his exported files (0.41.0 to 0.42.0); the other first-person
      models are still to check
- [ ] Cyberpunk 2077: weapon cards, layered materials and Collect on a real install
- [ ] Grand Theft Auto III to V, DOOM Eternal and The Dark Ages: everything so far is on synthetic
      files, including the key search in `GTA5.exe` and `GTAIV.exe` and the DOOM games' size and green
      direction
- [x] Dark Souls: a whole install collected as packages (6.26 GB, see Phase 5)

### Phase 2: print-ready output

Since the November 2026 review, Dark STL's side of this is cleaning and enhancing the model; making
it watertight, cutting and painting moved to Model Painter in 0.55.0 (Phase 8). The three ticked
below were Dark STL's until then.

- [x] Make watertight (0.17.0, Finish tab): the parts made solid and united, hollows filled, gaps
      optionally bridged and loose bits removed. Cut pieces are watertight since 0.9.0
- [x] Cut into pieces with joints (flat, pegs, dowels, magnets, key) and a clearance per cut
- [x] Painted 3MF for multi-colour printers (0.22.0), the Finished model and cut pieces too (0.24.0),
      with its settings on the Finish tab (0.37.0)
- [x] Clean tab (0.45.0): parts in or out, parts and faces hidden inside others, thin sheets (open,
      two-sided, thin) listed and removed, faces picked in the 3D view, holes, duplicates, weld,
      flipped faces, print height and placement; a list of steps per model, done again on every build
- [x] Thin parts kept from being displaced through themselves or into close parts (room per
      vertex, soft cap, calmer directions, crossing repair; bench thin scenes) (0.46.0)
- [x] Thickness shading: what is too thin to print at a chosen height (0.46.0)
- Thickening thin parts for printing is print finishing, which moved to Model Painter (0.43.0)
- [x] Per-material strength and on/off in the material list
- [x] Use detail normal maps as well as the main normal map (blend maps still to do)
- [ ] Height-map preview painted on the model before running the full step
- [ ] Streaming export for 10M+ vertex results

### Phase 3: explorer polish

- [x] A library per game: game folders added, rescanned and removed in the app, kinds of
      model as tiles, groups, search, previews (on Model Library's interface)
- [x] Collect into the app: the files chosen models need (model, textures, animations) copied
      into the app's own library folder, so the game's install isn't needed afterwards
- [x] The model page in the order the work happens (0.43.0): the rail's Model, Edit and Export groups,
      one picker for the model worked on, explanations folded once used, a Next strip, a one-row footer,
      and the handoff to Model Painter (For Model Painter GLB, Open in Model Painter)
- [x] In-game names (weapon, armour and character names from the game's text and param files):
      FromSoftware games (0.44.0)
- [x] In-game names for the other importers (0.49.0): the Unreal games' string tables (Returnal,
      Borderlands 3, Wonderlands, Borderlands 4 and any other), Borderlands 1/2/TPS's `.INT` text, Cyberpunk's
      weapons from its on-screen text
- [ ] In-game names for the RE Engine games (their `.msg` text joined through the item data)
- [x] Import breadth (0.49.0): game profiles as JSON (below, from the backlog), any Unreal 4/5 game through
      the generic importer, FBX files
- [x] One search across every game, with filters, a list view with sortable columns, sizes and
      counts on the cards, and Recent on Home (0.44.0)
- [x] Library II (0.48.0): collections, tags and stars across games; related models (armour sets,
      sheaths, low-detail copies, a character's parts); turntable and posed previews, card sizes;
      choosing several models and the queue (Collect, Export as GLB, Export packages, Enhance with
      the current settings); each game's Problems; first-run game tiles; Settings in three pages
- [x] Posing freedom (0.47.0): bones turned on top of a frame or the bind pose (sliders and a
      rotate gizmo), mirrored left ↔ right by name, saved poses offered to models with the same
      skeleton, library models attached to a bone or socket, the GLB keeping the skeleton posed
- [x] **Game files instead of per-game code** (raised by Jerred on 2026-11-07, from the backlog, built in 0.49.0). A JSON
      profile per game names the engine backend (`unreal4`, `unreal5`, `unreal3`, `fromsoft`, `reengine`,
      `redengine`) and its settings: the files and folder names that say a folder is the game, the engine
      release, AES keys and type mappings, how folders and names sort models into kinds and groups, texture
      role names, the normal maps' green direction, which text names models, and what Collect copies.
      Shipped in `importers/profiles/`, replaced or added to from `<settings>/profiles/`. The Unreal 4/5
      games (Returnal, Borderlands 3, Wonderlands, Borderlands 4) are read entirely from theirs, and a
      generic Unreal importer reads any other with `unreal-defaults.json`; the FromSoftware, RE Engine and
      Cyberpunk tables stay in code and take a profile's overrides. Engine readers stay code. The format is
      in [profiles.md](profiles.md).
- [ ] Read Elden Ring MATBIN files for exact material maps
- [ ] Dark Souls II (FLVER2 with its own texture layout)
- [ ] Importers for other game families, added as they are needed

### Phase 4: distribution

- [x] Windows build (single folder, no Python install needed): an installer and a portable zip on
      each release, built and smoke-tested on GitHub's Windows machines
- [x] A Tauri app (`desktop/`), like Model Library: the page in the window, the Python engine
      behind it over a pipe (no local server), the isotropic remesher included
- [ ] User guide with extraction steps for each game

### Phase 5: model packages, asset store and library overlay

The plan in the backlog entry below, built in stages:

- [x] Packages: Collect imports each model as a glTF mesh with its skeleton, skin weights,
      materials and sockets, its textures and animations in the game's asset store; the library
      lists and opens packages, posing included, with the game removed (0.27.0)
- [x] Texture picker: search the game's textures by name (its asset store, and every texture its
      folders hold where the importer can list them: FromSoftware, RE Engine), preview, assign one to
      a material's colour, normal or height map; the texture is kept in the asset store and the choice
      in the game's library overlay (0.28.0)
- [x] Library overlay per game: own folders and hierarchy, in-game names, models combined into one
      (face, body and hair), keyed by the model's path in the game (0.29.0)
- [x] Saved variations: Generate results kept beside the model's package, listed, exported again or
      used as a starting point (0.30.0)
- [x] Configurable models as packages: their configurations kept with the packages, every part and
      skin a package (0.31.0)
- [x] Faster import (0.32.0): textures stored as DDS (no decoding and encoding, half the size of
      PNG), animations sampled every frame at once (16 times faster), folders listed once, four
      models at a time; 12 animated Bloodborne characters 138 s -> 38 s. Cancel stops an import,
      Run in background leaves it running
- [x] Package size checked on a real install: Dark Souls 6.26 GB, 1.15 GB of it animations, which
      Jerred found reasonable, so animations stay sampled (keeping the game's Havok files instead isn't needed)
- [x] Borderlands 2 skins mixed for each part kept in packages, and mixed from them (0.33.0)
- [x] The whole asset store: every texture of a game extracted, kept compressed (DDS, every importer),
      with a size estimate first (0.34.0)
- [x] Borderlands 1/2/TPS characters posed with the game's AnimSets (0.38.0)
- [x] Models from other tools imported as packages (0.35.0): glTF (Blender, FModel) and ActorX PSK/PSKX
      (UModel, FModel) importers beside OBJ, PSK materials' textures found through the tools' material
      files, and every loose picture in such a folder offered by the texture picker and kept by "Every
      texture of the game"

### Phase 6: Grand Theft Auto

The plan is in Reading Grand Theft Auto files above.

- [x] Plan: formats, importer layout, kinds and names, Collect, phases
- [x] RenderWare trilogy: GTA III, Vice City, San Andreas (0.50.0), on synthetic files
- [x] GTA V: RPF7 with keys from the executable, peds as configurable models, vehicles, posing (0.51.0), on synthetic files
- [x] GTA IV and Episodes from Liberty City on the shared RAGE reader (0.52.0), on synthetic files
- [ ] Trilogy Definitive Edition as a profile on the generic Unreal importer
- [ ] Mobile and console versions (see Mobile and console versions above): Android/iOS texture databases
      first, then Xbox and PS2 RenderWare, then the RAGE consoles
- [ ] Check each on Jerred's installs

### Phase 7: DOOM (id Tech 6/7/8)

The plan is in Reading id Tech 6/7/8 files above.

- [x] Plan: containers, StreamDB, formats per game, importer layout, kinds and names, Collect, phases
- [x] DOOM Eternal: `.resources` and `.streamdb` with the game's Oodle, textures, static and skinned
      models, skeletons, animations on the Pose tab, materials from decls (0.53.0; synthetic files
      only, in-game names still to come)
- [x] DOOM: The Dark Ages: its model, skeleton and animation layouts, stream ids, streamed animations
      (0.54.0; synthetic files only)
- [ ] DOOM (2016) and DOOM VFR: their `.index`/`.resources`/`.patch` and the older layouts, from
      Jerred's samples (the next free version once they arrive)
- [ ] Check each on Jerred's installs

### Phase 8: print finishing handed to Model Painter

Jerred, 2026-11-09: Dark STL is for importing, exploring and organising models and for extracting,
manipulating and cleaning them; print post-processing lives in Model Painter.

- [x] Export for Model Painter (a GLB with textures and materials) and Open in Model Painter (0.43.0)
- [x] Model Painter paints (its 0.1.0), makes watertight (0.2.0) and cuts into pieces with the same
      joints (0.3.0), ported from Dark STL's `paint.py`, `finish.py` and `cut.py`
- [x] Retire Dark STL's Finish and Cut tabs and the painted 3MF export (and their command-line
      options), leaving Export for Model Painter as the way across (0.55.0; manifold3d no longer
      needed; old settings, variations and presets still load)

### Backlog

Nothing is on hold at the moment. The entry below is the model-package plan as Jerred approved it;
it was built as Phase 5.

- [x] **Model packages between the importers and the editor** (approved by Jerred on 2026-11-07
      and built in stages in 0.27.0 to 0.35.0, see Phase 5). Importers convert a game model
      into a self-contained package (glTF for mesh, skeleton and animations; a JSON sidecar for
      texture roles, normal-map convention, sockets and configurations), and the editing side
      only reads packages. Collect becomes "write the package", which keeps the game uninstallable;
      models from other tools (FModel, UModel, Blender) could come in the same way. Cost: what
      importers do lazily now (Borderlands skins painted on request, Configure options over
      hundreds of parts, posing from game animations) has to be baked up front. Three layers:
  - **Game content**: read-only, never changed.
  - **Asset store**: every model and texture extracted once under its original name and path
      (textures kept in their compressed form, with a size estimate shown before a full
      extraction), so anything the game has stays available after uninstalling, even what no
      importer linked to a model.
  - **Library overlay** (a mapping file per game, never touching the stored files): the user's
      own folders and hierarchy (e.g. "Bosses", "Long swords"), in-game display names, models
      combined into one (face, body and hair in RE Engine games; generalises today's
      configurations), and texture assignments per material slot picked by original name from
      the asset store in the app. Entries are keyed by the model's path in the game, so they
      survive rescans; a model that disappears shows as missing instead of being dropped.
      A texture picker (search every texture by name, preview, assign to a slot and role) could
      come first on its own, since it also fixes models the importers guess wrong.
  - **Saved variations**: each Generate result kept with the original in the model's package (its
      name, the full settings it was made with, the result mesh and a thumbnail), listed under
      the model so earlier variations can be viewed, compared, exported again or used as the
      starting point for new settings. Today results are only in the cache (keyed by model and
      settings), which can be cleared and isn't browsable.
      Raised by Jerred on 2026-11-07; saved variations came in 0.30.0.

## Known limits today

- GTA III, Vice City and San Andreas have only been read from synthetic files so far. Not read
  yet: a car's wheels kept in the shared `wheels.dff` (when its own file has none; most cars carry
  their own), San Andreas' player clothes put together as one figure (they are listed one by one
  under Characters / Player clothes), night vertex colours, reflection and specular maps, and
  console (PS2, Xbox) files. Cutscene animations (`cuts.img`) aren't offered on the Pose tab.
- GTA V has only been read from synthetic files so far (archives encrypted with made-up NG
  tables, and resources in both layouts built byte by byte), so the archive keys have not yet
  been searched for in a real `GTA5.exe`. Not done yet: vehicle paint (the game picks the
  colours at run time; painted panels show their base texture), ped texture variations (`_b_`,
  `_c_`... textures; each component shows its first), the peds' `.ymt` variation files (outfit
  slots come from the component names instead), weapon attachments as configurable weapons,
  map placement (`.ymap`), cloth and damaged vehicle meshes, and the console and mobile releases.
- GTA IV has only been read from synthetic files so far, so the key search has not yet run on a
  real `GTAIV.exe`. Not done yet: posing (its `.wad` animations aren't read; characters open in
  their bind pose), vehicle paint, ped texture variations, weapon names (weapons keep their model
  names), map placement (`.wpl`), and the Xbox 360 and PlayStation 3 files.
- Of the real game files, Dark Souls Remastered weapons and Bloodborne characters, objects and
  armour have been loaded so far (Phase 1). The RE Engine importer has only seen synthetic files.
- RE Engine: `.pak` archives with an encrypted table of contents (Monster Hunter Wilds and later)
  and GDeflate-compressed textures are not read; extract those games with a pak tool first.
  MPLY stage meshes and blend shapes are not read. Posing RE models has only been tried on
  synthetic motions; motion trees, additive layers and animations kept in `.motbank` /
  `.motfsm2` setups (rather than in the motlists themselves) are not used.
- Exports are as watertight as the game's model: Model Painter makes them so (Export for Model
  Painter); most slicers repair the rest.
- Keeping thin parts measures room on the evenly spaced mesh, so parts thinner than about two
  vertex spacings are handled by the repair step rather than the cap, and lose more of their
  relief there. The Thickness shading counts an open part (most game parts have openings at
  the neck or cuffs) as a single sheet where its inward ball ends against the front of another
  part pushed into it; closed parts don't have this problem.
- Clean tab steps record faces by part and place in the part: a model whose importer later builds it
  differently (or a configurable model with other parts chosen) may find other faces there; each
  step's row says what it removed, so such a step can be taken away and made again. Thin sheets
  are measured on the model as it is: cloth thicker than the thickness asked for is a solid.
- Without an anibnd beside the model, skinned meshes (characters, armor) are shown in their
  bind pose, which is a T or A pose. Armour pieces take the player's animations
  (`c0000.anibnd`) only when that archive is copied next to them.
- Animations that use root motion are shown in place; cloth simulated in the game keeps its
  bind pose.
- Posing freedom: turning bones needs a skeleton and the bind-pose mesh's skin weights. Unreal
  skeletal meshes with no animations found, Cyberpunk models, and glTF and PSK files from other
  tools (read in their bind pose without weights) have no bones to turn yet (FBX files' do, from 0.49.0). Packages made
  before 0.47.0 of models without animations kept no skeleton: collect them again to turn their
  bones. A socket holds an attached model where the socket is in the bind pose (sockets aren't
  posed); attach to a bone to have it follow. Mirroring works out the plane from the bind pose's
  pairs of bones, so a skeleton whose sides aren't mirror images in its bind pose mirrors only
  roughly. Attaching is for library models (not files opened on their own, or configurable
  models). An enhanced model's GLB has the skeleton without skin weights, and none when the Clean
  tab's transform moved or sized the model it was made from.
- Isotropic remeshing needs the optional `pyacvd` package; the app falls back to subdivision
  without it. The Windows app includes it, with only the parts of VTK it uses.
