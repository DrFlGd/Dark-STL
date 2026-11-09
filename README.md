# Dark STL

Browse the models of Dark Souls, Bloodborne, Sekiro, Elden Ring and Cyberpunk 2077 (or any OBJ
file), add the surface detail their textures describe, and export print-ready meshes.

![A model enhanced in Dark STL](docs/images/model-page.png)

The game models are low-poly: most of their detail lives in normal maps. Dark STL turns that
detail into real geometry in three steps:

1. **Even out the vertices** to a target count, so flat areas get as many vertices as busy ones.
   Curved surfaces are rounded off along the model's own shading normals.
2. **Displace** every vertex by the height the normal map (or a height map) describes, at the
   physical scale the texture implies.
3. **Export** to STL, OBJ (with textures), PLY or GLB, in millimetres by default.

Before exporting, the model can be **made watertight**: its parts wrapped into one solid with the
surfaces inside one another removed and sealed hollows filled, so it prints as one piece. It can
also be **cut into pieces** for printing: planes placed in the 3D view
split it into watertight pieces, joined by pegs, dowels, magnets or a key, with a clearance for each.

See [docs/PLAN.md](docs/PLAN.md) for how each step works, the file formats, and the roadmap.

## Status

Early. All readers and all three steps work and are tested against synthetic game files built
to the published format descriptions. Checked on real files so far: a Dark Souls Remastered
weapon, and a Bloodborne character, object and armour piece (PS4 textures included). The
other games come next (Phase 1 of the plan).

Importing is modular: each game family has its own importer, and everything after import
(the explorer, the three steps, export) is shared. FromSoftware files (Dark Souls, Bloodborne,
Dark Souls III, Sekiro, Elden Ring), Cyberpunk 2077 files, Capcom RE Engine files (Resident Evil
2, 3, 4, 7 and Village, Devil May Cry 5), Borderlands (Borderlands, Borderlands 2, The
Pre-Sequel, Borderlands 3, Tiny Tina's Wonderlands, Borderlands 4), Returnal, Grand Theft Auto III,
Vice City, San Andreas, IV and V, and Wavefront OBJ are supported today.

## Install

**Windows:** download `DarkSTL_<version>_x64-setup.exe` from the
[releases page](https://github.com/DrFlGd/Dark-STL/releases) and run it. It needs no Python
and installs for your account only. It isn't signed, so SmartScreen asks once: *More info >
Run anyway*. The `-portable.zip` is the same app in a folder to run from anywhere. The app is
`Dark STL.exe`; its engine sits in the `engine` folder beside it, and `engine\darkstl.exe` is
also the command line. If the app has a problem, `%LOCALAPPDATA%\DarkSTL\cache\darkstl.log`
says why.

**From source** (any system): Python 3.10 or newer.

```
git clone https://github.com/DrFlGd/Dark-STL.git
cd Dark-STL
pip install -e ".[gui,remesh]"
```

`gui` adds pywebview, so the app opens in a window of its own; without it the app opens in
your web browser. `remesh` adds the optional isotropic remesher. The command line needs
neither.

## Getting the game files

- **PC games (Dark Souls PTDE, Dark Souls II, Dark Souls III, Sekiro, Elden Ring):** add the
  game's install folder as it is. Its models are still packed in the game's archives
  (`Data0.bhd`/`.bdt` and so on), so Dark STL offers to **unpack** them (also *Unpack…* in the
  folder's menu, and *Settings > Game archives*): it decrypts the archives' file lists with
  the games' published keys and writes out the groups you choose (characters, weapons and
  armour, objects and assets, map pieces) with their textures and animations, into its own
  library folder, laid out as `chr/`, `parts/`, `obj/` or `asset/`, and `map/`. Nothing in
  the game's folder is changed, and nothing else (sound, scripts, collision) is unpacked.
  File names come from the community's lists (UXM's, as Smithbox ships them, are included;
  newer ones, which name Elden Ring's Shadow of the Erdtree files, download from
  UXM Selective Unpacker in *Settings*), and models that follow the pattern of known names
  are found too. From the command line: `darkstl unpack <game folder> -o <folder>
  [--only characters,parts,objects,maps]`. A folder already unpacked with UXM Selective
  Unpacker or UDSFM works as it is.
- **Bloodborne:** use the extracted `dvdroot_ps4` folder from your own console dump, or any
  folder of `.chrbnd.dcx`, `.objbnd.dcx` and `.partsbnd.dcx` files taken from it. The PS4
  textures are rebuilt into ordinary DDS on the fly.
- Folders unpacked further with WitchyBND or Yabber (`wp_a_0210-partsbnd-dcx/`) work too.
- **Sekiro and Elden Ring** compress with Oodle. Dark STL uses the `oo2core_6_win64.dll`
  that ships with the game. It is found automatically when the model files are inside the
  unpacked game folder; otherwise point to it in *Settings > Oodle* (or `--oodle`).
- **Cyberpunk 2077:** add the game's install folder as it is. Its `.archive` files are read
  directly (the first scan reads through them once to learn the models' names, and keeps
  what it found), using the game's own `bin\x64\oo2ext_7_win64.dll` to decompress. A folder
  of `.mesh` and `.xbm` files exported with WolvenKit (kept under their `base\...` paths)
  works too, and the two can be mixed: a model's textures are looked up in the loose folder
  first, then in the archives. Weapons are listed as configurable models, put together from the
  game's own appearance files (`.app`): the body, a slot for each part the appearances swap
  (magazine, stock, decals...), the body's finish, and the attachment points (scope, muzzle) with
  the attachments that fit them; each in-game appearance (*frank*, *military1*...) is a preset.
  Multilayered materials (most weapon surfaces, skin) are blended from their layers and masks
  into one colour map and one normal map.
- **Resident Evil (RE Engine):** add the game's install folder as it is: Dark STL reads the
  models straight from its `.pak` archives. Paks only store hashes of the file names; the
  names come from the community file lists Dark STL ships (Ekey's, from
  [REE.PAK.Tool](https://github.com/Ekey/REE.PAK.Tool), cut to meshes, textures,
  materials and animations) for Resident Evil 2, 3, 7 (both the ray-tracing and the original releases), 4,
  Village and Devil May Cry 5. After a game update, a newer `.list` file put in the game's
  folder is read too. A folder extracted with a pak tool (REtool, ree-pak), holding
  `natives/`, works as well.
  Models are `.mesh` files with their materials in a `.mdf2` and textures in `.tex` files;
  the full-size textures and the most detailed geometry of newer games (Resident Evil 4) sit
  under `natives/stm/streaming/` and are used when present. Characters can be posed on the
  Pose tab with the game's `.motlist` animations filed under their id (`pl0000`, `cha0`...). Games whose archives are
  encrypted (Monster Hunter Wilds and later) need extracting first; their GDeflate textures
  are not read yet.

- **Borderlands, Borderlands 2, The Pre-Sequel (Unreal Engine 3):** add the game's install
  folder as it is. The models are read straight from the `.upk` packages in
  `WillowGame\CookedPCConsole` (`CookedPC` for the first game), LZO-compressed as they are;
  the large texture mips come from the `.tfc` texture caches beside them. The first scan
  opens every package once and keeps what it found. Borderlands 2 and The Pre-Sequel keep
  every gun part of a weapon type in one shared mesh in `Startup.upk`; it is listed under
  Weapons with each part as a part of the model, and each weapon type is also a
  configurable model (body, grip, barrel, sight, stock, elemental, accessory) built from the
  game's own part definitions in the `GD_Weap_*` packages.

- **Borderlands 3, Tiny Tina's Wonderlands (Unreal Engine 4), Borderlands 4 (Unreal Engine 5):**
  add the game's install folder as it is. Models are read from the `.pak` files (BL3, Wonderlands)
  or the IoStore `.utoc`/`.ucas` containers (BL4) in `OakGame\Content\Paks`. Encrypted files need
  the game's AES key (Borderlands 3's is included; paste others in *Settings > Unreal games* or
  put them in `unreal-keys.txt` in the game folder). Borderlands 4 also needs a `.usmap` type
  mappings file for the installed version of the game, added the same way, because it cooks
  its objects without property names. Guns are configurable from their part meshes, the slots
  guessed from the part names.

- **Returnal (Unreal Engine 4):** add the game's install folder as it is. Models are read from the
  `.pak` files in `Returnal\Content\Paks`, sorted into weapons, characters (Selene, creatures),
  map pieces and objects by their folders. Returnal cooks its objects without property names: Dark
  STL finds each mesh's and texture's own data past them, and picks a material's textures by their
  names. A `.usmap` type mappings file for the installed version, if you have one, lets it read the
  materials' settings instead (game folder or *Settings > Unreal games*). Dark STL doesn't include
  Returnal's AES key: if the paks are encrypted, paste the key there too and rescan.

- **Grand Theft Auto III, Vice City, San Andreas (RenderWare, PC):** add the game's install
  folder as it is. Models (`.dff`) and their texture dictionaries (`.txd`) are read straight from
  the `.img` archives (`models\gta3.img` with its `gta3.dir` in III and Vice City, the single-file
  `VER2` archives in San Andreas) or loose. The game's own lists (`data\gta.dat`, `default.dat`
  and the `.ide` files they name) sort them into characters, vehicles, weapons, map pieces and
  objects, and say which dictionary each model's textures are in; vehicles are named as in the game
  (from `text\american.gxt`). Cars are built with their wheels on every axle, without their damaged
  and low-detail panels (both can be turned on), and painted in each colour pair from
  `data\carcols.dat` (a variant each). Characters are posed on the Pose tab with `anim\ped.ifp`.
  A folder of `.dff` and `.txd` files exported with another tool opens too, its textures looked
  up by name. The Definitive Edition (Unreal Engine 4) isn't read by this importer.

- **Grand Theft Auto V (PC, the original release and Enhanced):** add the game's install folder as
  it is (the one with `GTA5.exe`). Models are read straight from the `.rpf` archives, nested ones and
  the `update` and DLC archives included (an update's copy wins). The archives are encrypted with
  keys that are in the game's executable: Dark STL doesn't include them, but finds them in your
  `GTA5.exe` the first time (this reads through the executable once, which takes a while) and keeps
  them in its settings folder (`keys\gta5.bin`). Vehicles (`.yft`) are sorted by class and named as
  in the game (from `vehicles.meta` and the `.gxt2` text), with a wheel on every corner; characters
  (`.ydd`) are listed as outfits, a slot for each component (top, legs, feet, hair...), and posed on
  the Pose tab with the game's clips (`.ycd`); weapons are named and grouped from `weapons.meta`;
  props and map pieces are grouped by archive. Textures come from the model's `.ytd`, its parents in
  `gtxd.meta` and the shared vehicle dictionaries. Files exported with OpenIV or CodeWalker (`.ydr`,
  `.ydd`, `.yft` with their `.ytd`) open too, without the keys. Vehicle paint colours aren't applied
  yet (painted panels show their base texture).

- **Grand Theft Auto IV (PC, with The Lost and Damned and The Ballad of Gay Tony):** add the game's
  install folder as it is (the one with `GTAIV.exe`). Models (`.wdr`, `.wdd`, `.wft` with their
  `.wtd` textures) are read straight from the `.img` archives in `pc\models\cdimages` and
  `pc\data\maps`. The archives' tables are encrypted with a key that is in `GTAIV.exe`: Dark STL
  doesn't include it, but finds it there the first time and keeps it in its settings folder
  (`keys\gta4.bin`). The game's `.ide` files and `american.gxt` sort and name the vehicles; peds are
  outfits of their components, as in GTA V; the episodes' models are listed with the base game's,
  their groups named after the episode. GTA IV's animations aren't read yet, so its characters
  can't be posed. Files exported with OpenIV or SparkIV open too.

- **Posing Unreal 4/5 models** (Returnal, Borderlands 3, Wonderlands, Borderlands 4): a skeletal
  mesh can be posed on the Pose tab with every `AnimSequence` made for its `Skeleton` asset. The
  first time a game's animations are listed, Dark STL reads the header of each package that looks
  like an animation (named `A_`, `AS_`, `Anim_`... or in an animation folder) and keeps what it found;
  for Returnal, whose animations may be named otherwise, it looks through every package that may be
  one when the names find none. When none are found, the Pose tab says what was looked through.
  Animations compressed with Unreal's own codecs are read (4.12 to 5.x); those compressed with the
  ACL plugin are counted on the Pose tab but not read yet.

- **Posing Borderlands, Borderlands 2 and The Pre-Sequel characters (Unreal Engine 3):** a skeletal
  mesh can be posed on the Pose tab with the sequences of every `AnimSet` in the game whose bones are
  the mesh's (listed as *AnimSet: sequence*). Nothing in the packages says which AnimSets a mesh
  uses, so the first time a game's animations are listed Dark STL lists the AnimSets of every package
  (a few minutes for Borderlands 2 when the folder was scanned with an earlier version) and keeps
  them; each is read from its own package, or from a map that carries a copy when that is all there
  is. Borderlands 2's polar-encoded rotations are read; the mesh keeps its own bone lengths unless
  the AnimSet moves them, as the game does. Gun meshes made of parts aren't posed.

## Using the app

Start *Dark STL* from the Start menu, or from a source install:

```
darkstl            # or darkstl-gui; darkstl gui --browser opens it in your browser
```

1. **Add your games.** *Add game folder…* takes a game's install folder (offering to unpack
   the games that keep their models in archives) or any folder of extracted model files. Dark STL recognises the game from its files and lists it in the
   menu. Add as many games and folders as you like; *Settings* lists them, with Rescan,
   Change game and Remove. Nothing in your folders is ever changed. Models you *Collect into
   the app* are copied into Dark STL's library folder (movable in *Settings*); on a fresh
   install, *Add collected files…* lists them from there without the games.
2. **Browse a game.** Each game has its own library: its kinds of model (weapons, armour,
   characters, objects, map pieces) as tiles, then each kind's models with a preview, in
   groups. Previews are drawn once and kept until you rescan the folder; *Redraw preview*
   (right-click a card) draws one again, and *Redraw previews here* under ⋯ a whole game,
   kind or group, for pictures an older version drew without textures. Search with the box
   at the top (or press /). *Open a model file…* under ⋯ on
   Home opens a single file, including `.obj`.
3. **Open a model** (double-click or Enter). It's shown with the game's textures, wherever in
   the game folder they are (a map's texture packs, shared packs, another model's archive);
   *Model* lists its parts and options, *Textures* every texture found. A file that holds several
   models (a weapon and its sheath) has a switch at the top. When a model doesn't open, pose or
   texture as it should, *Export model files…* (under ⋯ on its page, or right-click its card) puts
   its files into one zip to send for review: the game's own files it is read from, and/or the
   model package Dark STL imports it as, with a report of what Dark STL made of it (materials,
   textures, UV ranges, animations and those it couldn't read, which of the game's archives each
   file came from) and what it logged. The zip's folders open again with *Add collected files…*.
4. **Clean** takes off what shouldn't be printed, as a list of steps you can take away again: parts
   left out (the part checkboxes), parts and faces hidden inside others, thin sheets (capes, linings,
   cloth drawn from both sides, even inside a bigger part: listed biggest first, shown in the 3D view
   as you point at them, removed when ticked), and faces you pick by clicking the model (the pick
   spreads to the creases; Shift adds, Alt takes away). It also fills small holes, drops duplicate
   faces, welds vertices, turns flipped faces, and sets the print height, turns, mirror and place
   (from then on sizes read in print millimetres). Every later tab works on the cleaned model.
5. **Mesh** evens out the vertices (step 1): set the count (it shows the spacing that gives on this
   model) and the method, then *Even out the vertices*. The model is shown, not displaced yet, and
   the mesh is kept while you work on the next two tabs.
6. **Textures** also cleans up and upsamples the normal maps for displacement: pick the clean-up,
   the upsampling and the factor, *Upscale*, and compare **Before / After** (drag the line, zoom
   into single texels), then change the settings and upscale again until the detail looks right.
7. **Displace** raises the texture detail on the evened-out mesh (step 2): the strength, a detail
   texture for each material and the finer settings. Whatever the Mesh and Textures tabs haven't
   made yet is made first. Show *Original* or *Enhanced* under the view: both stay loaded, so
   switching is instant, and the last three models generated are kept too. Click **Stop** in the
   status bar (or under a progress bar) to stop a job.
8. **Finish** holds post-processing for printing. *Make watertight* wraps the whole model into one
   solid: every part is made solid (openings closed, open surfaces thickened) and they're joined,
   so where parts cross (an arm in a sleeve, armour over a body) the surfaces inside are removed
   and only the outside stays, exactly as it was. Hollows sealed inside are filled; optionally
   narrow gaps between parts are bridged and loose bits removed. *Show › Finished* shows it, and
   Cut and Export can use it.
9. **Cut** splits the model into pieces for printing. *Add a cut* across X, Y or Z (or facing you),
   then drag the plane in the 3D view to slide it, drag its round handle to tilt it, or *Pick a
   point* on the model. Each cut has its own joint: flat (to glue), pegs, dowels, magnet pockets
   or a key shaped like the cut face, and its own clearance. The model is made solid first
   (openings closed, open surfaces such as capes thickened), so every piece is watertight.
   *Show › Pieces* shows them in colour, spread apart with a slider.
10. **Export** writes STL, OBJ, PLY or GLB, in millimetres by default; *Finished* the watertight
   model; *Pieces* writes each piece
   to its own file, laid on a cut face ready to print. **Painted 3MF** paints the model from its
   colour textures for a multi-colour printer (Bambu Studio, OrcaSlicer, PrusaSlicer), with the
   settings in the Finish tab's *Paint for colour printing*: the textures are reduced to a palette
   of 2 to 12 colours (suggested from the model, or chosen swatch by swatch; a colour tolerance
   merges near shades and the rarest colours can be dropped), each material painted from its colour
   texture or another one you choose, with brightness, contrast and saturation adjusted first. Each
   triangle is painted finer than the mesh, down to the smallest feature you name at the print size,
   the way the slicers' own brush paint is stored. *Show the colours on the model* previews it, and a
   picture of the palette is written beside the file to set the filament slots by. The Finished model and cut pieces are painted too, from the textured model
   they were made from (cut faces, joints and filled gaps take filament 1).

![A game's library](docs/images/library.png)

## Command line

```
darkstl scan "D:/Games/DARK SOULS III/Game"              # list models by game and type
darkstl info parts/wp_a_0210.partsbnd.dcx                # models, parts, materials, textures
darkstl textures parts/wp_a_0210.partsbnd.dcx -o tex/    # save every texture as PNG
darkstl export parts/wp_a_0210.partsbnd.dcx -o grip.obj  # original model with textures
darkstl export chr/c2010.chrbnd.dcx -o c2010.3mf --colours 6 --print-height 200 --feature 0.5  # painted 3MF
darkstl enhance parts/wp_a_0210.partsbnd.dcx -o grip.stl -n 2000000
darkstl enhance chr/c2010.chrbnd.dcx -o c2010.stl -n 4000000 --strength 1.5 --save-heights heights/
darkstl export chr/c2010.chrbnd.dcx -o c2010.stl --clean hidden-parts --clean sheets:0.5 --clean height:150
```

Useful `enhance` options: `--method isotropic` for an exact count of uniform triangles,
`--source height` to use a grayscale height map, `--flat` to skip rounding, `--green
opengl|directx` if the relief comes out inverted, `--max-mm` to clamp displacement, and
`--parts 0,2` to keep only some parts, `--clean` steps done first (repeatable: `hidden-parts`,
`hidden-faces:2`, `sheets:0.5`, `holes:5`, `duplicates`, `weld:0.01`, `normals`, `height:150` or `scale:0.1`,
`rotate:0,0,90`, `mirror:x`, `floor`), `--cut z=40:pegs` (repeatable; `z=50%`, joints `flat`,
`pegs`, `dowels`, `magnets`, `key`, and `--clearance 0.2`) to write the model as pieces,
`--watertight` to wrap it into one watertight solid first (with `--close-gaps 1` to bridge gaps up
to 1 mm, `--drop-loose 1` to remove loose bits under 1% of it, `--keep-hollows`), and `--variant WP_A_0402_1.flver` to pick a model in a
file that holds several (see `info`). A `.3mf` output is painted from the colour textures: `--colours 6` or
`--palette "#1a1a1a,#c8b89a,..."`, `--print-height` and `--feature` (mm) for how fine the paint goes, `--cleanup
none|light|strong`, `--tolerance 10` to merge near colours, `--min-share 1` to drop colours under 1% of the surface,
`--brightness`/`--contrast`/`--saturation`, and `--paint-texture 2=WP_A_0300_m` to paint material 2 from another texture. Run `darkstl <command> -h` for all.

## Development

```
pip install -e ".[gui,remesh,dev]"
pytest
```

The tests build game files byte by byte (`tests/synth.py`), so no game data is needed or
stored in the repository. `tests/test_page.py` also drives the page in Chromium when
Playwright is installed (`pip install playwright && python -m playwright install chromium`).

The page (`src/darkstl/web/`) needs no build step. Its interface comes from
[Model Library](https://github.com/DrFlGd/Model-Library): `styles.css`, `lib/`, and the
layout, inspector, menu and icon modules in `ui/` are shared with it, so changes there are
best made in both.

To support another game, write an importer (see `src/darkstl/importers/base.py` and the
OBJ importer as a short example) and add it to `IMPORTERS` in `importers/__init__.py`.

The Windows app (`desktop/`) is a [Tauri](https://tauri.app) app, like Model Library's. Its
window shows the same page, and the page reaches the engine through the window
(`web/platform-desktop.js`, `desktop/src/main.rs`): the window runs `darkstl engine`
(`app/engine.py`) and passes the page's commands to it over its stdin and stdout, so the
app opens no port. The engine is frozen with PyInstaller (`desktop/engine/engine.spec`, with
only the parts of VTK the remesher needs) and bundled beside the app. To build it, with Rust
and Node installed:

```
pip install ".[remesh]" pyinstaller
pyinstaller desktop/engine/engine.spec --noconfirm --distpath build --workpath build/pyinstaller
python desktop/engine/smoke.py build/engine
cd desktop && npx @tauri-apps/cli@2 build
```

`desktop/drive.py` drives the built app through tauri-driver (WebDriver). The Windows app
workflow does all of this on every pull request that touches the app.

Releases publish themselves: once the tests pass on main and the version in
`pyproject.toml` has no release yet, the Release workflow builds the Windows app, the wheel
and the source archive, and releases them as `v<version>`, with `docs/releases/v<version>.md`
as the notes. To release, bump the version (in `pyproject.toml`, `src/darkstl/__init__.py`
and `desktop/Cargo.toml`, then `cargo update -p dark-stl` in `desktop/`), add the notes, and
merge.

## Credits

The file format knowledge comes from the FromSoftware modding community, in particular
[SoulsFormats](https://github.com/JKAnderson/SoulsFormats) by TKGP and
[Soulstruct](https://github.com/Grimrukh/soulstruct) by Grimrukh. Unpacking follows the
archive format as BinderTool (Atvaark), UXM (TKGP) and
[UXM Selective Unpacker](https://github.com/Nordgaren/UXM-Selective-Unpack) (Nordgaren)
read it; the archive keys and the bundled file lists are copied from
[Smithbox](https://github.com/vawser/Smithbox) (MIT, see
`src/darkstl/importers/fromsoft/filelists/LICENSE.txt`). Dark STL is an independent
reimplementation and contains no code or data from the games; you need your own copies.

The page bundles [Preact](https://preactjs.com) (MIT), [htm](https://github.com/developit/htm)
(Apache 2.0) and [three.js](https://threejs.org) r186 (MIT), unmodified apart from import
paths; their licences are in `src/darkstl/web/vendor/`.
