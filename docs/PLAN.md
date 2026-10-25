# Dark STL: plan

Dark STL is a desktop tool that reads models and textures extracted from games, starting with
the FromSoftware games (Dark Souls, Bloodborne, Sekiro, Elden Ring), lets you browse them
grouped by type, and turns them into high-fidelity meshes for 3D printing and rendering. It
does that in three steps:

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
| RE Engine (Capcom) | `.mesh` models (every generation from RE2 to RE Requiem, streamed buffers included), `.mdf2` materials, `.tex` textures, read from extracted files or from `.pak` archives through a community file list. Games in `importers/reengine/games.py` | Synthetic RE2 and RE4 files; real files next |
| Wavefront OBJ | OBJ + MTL with PNG/JPG/TGA/DDS textures, from any other tool | Done |

An importer implements `can_open` and `load`, and optionally `scan` (to list its models in a
folder), `detect_game`, `variants` (files holding several models) and `describe`.

### The app: a game library on Model Library's interface

The app is a small local web server (`app/server.py`) and a page (`web/`) shown in the
system's own web view through pywebview, or in the browser with `--browser`. The page is the
interface of Model Library and SCAD Workshop (Preact + htm, no build step, the same skins,
themes, command bar, inspector rails, menus and three.js viewer), so the three apps look and
work alike. Everything the page does goes through one command table (`app/service.py`), which
the tests call directly.

- **Game folders.** *Add game folder…* takes a game's install folder (unpacked) or any folder
  of extracted files. The game is recognised from its files (or chosen, or named freely), and
  the folder is scanned in the background. Folders are kept in the app's settings file and
  scanned again at start-up; each can be rescanned, moved to another game or removed. A folder
  inside one already added is refused; adding a parent folder takes in the ones inside it. A
  game still packed in its archives gets a note saying how to unpack it.
- **One library per game.** The menu lists each game with its model count. A game's page shows
  its kinds of model (weapons, armour, characters...) as tiles; a kind shows its groups as chips
  and its models as cards or rows, searchable, with the selected model's details beside them.
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
- **The model page.** The 3D view with the game's textures (or clay, or wireframe), and the
  inspector's Model (parts, importer options, size, file), Textures, Pose (an animation and a
  frame, with a slider, stepping and playback; only for models with animations), Generate and
  Export tabs.
  Meshes travel to the page in a packed binary form (`app/meshpack.py`) split at UV seams and
  grouped by material.
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
  deblocking model), upsampling of the normal map (guided by the albedo's edges, or a 2x/4x
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

### Step 3: export

| Format | Content |
| --- | --- |
| STL (binary or ASCII) | Geometry only, Z up, millimetres by default |
| OBJ + MTL + PNG | Geometry, UVs, one material per game material with its albedo map, Y up |
| PLY (binary) | Geometry and normals |
| GLB | Geometry, UVs and embedded albedo textures |

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
                by FromSoftware naming)
    reengine/   formats/ (mesh, tex, mdf2, pak with MurmurHash3 names), games.py (RE Engine games and
                how a folder is recognised), files.py (a model, its streamed half, materials and textures,
                on disk or inside a pak), materials.py (texture roles from slot types), catalog.py
                (classifies models by the engine's folder layout)
    wavefront.py  OBJ + MTL
  geometry/     TriMesh, subdivision, isotropic remesh, UV seam matching, closest-point queries,
                displacement
  textures/     DDS decoding, normal map -> height, grayscale height, UV coverage, texel size
  library.py    the library: game folders scanned by every importer, by game, kind and group
  collect.py    copies the files chosen models need into the app's own library folder
  thumbnails.py preview pictures for the library's cards, cached on disk
  pipeline.py   the three steps with their settings
  export.py     STL, OBJ, PLY, GLB writers
  cli.py        command line
  app/          the desktop app: service.py (the page's commands), server.py (local web
                server), meshpack.py (meshes for the 3D view), config.py, desktop.py (window)
  web/          the page: Model Library's interface (styles.css, ui/ layout, inspector,
                menus) with Dark STL's pages: home.js, browser.js, modelpage.js, settings.js
tests/          synthetic game files built byte by byte (no game data in the repo)
```

## Roadmap

### Phase 0: foundations (this first pull request)

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

### Phase 2: print-ready output

- [ ] Watertight option: game meshes are open shells with overlapping parts, so add a
      solidify step (voxel or winding-number remesh) that outputs one closed manifold
- [ ] Thin-part thickening for blades and capes
- [x] Per-material strength and on/off in the material list
- [x] Use detail normal maps as well as the main normal map (blend maps still to do)
- [ ] Height-map preview painted on the model before running the full step
- [ ] Streaming export for 10M+ vertex results

### Phase 3: explorer polish

- [x] A library per game: game folders added, rescanned and removed in the app, kinds of
      model as tiles, groups, search, previews (on Model Library's interface)
- [x] Collect into the app: the files chosen models need (model, textures, animations) copied
      into the app's own library folder, so the game's install isn't needed afterwards
- [ ] In-game names (weapon, armor and enemy names from the game's text and param files)
- [ ] Batch queue: enhance and export a whole group with one set of settings
- [ ] Read Elden Ring MATBIN files for exact material maps
- [ ] Dark Souls II (FLVER2 with its own texture layout)
- [ ] Importers for other game families, added as they are needed

### Phase 4: distribution

- [x] Windows build (single folder, no Python install needed): an installer and a portable zip on
      each release, built and smoke-tested on GitHub's Windows machines
- [x] A Tauri app (`desktop/`), like Model Library: the page in the window, the Python engine
      behind it over a pipe (no local server), the isotropic remesher included
- [ ] User guide with extraction steps for each game

## Known limits today

- Of the real game files, Dark Souls Remastered weapons and Bloodborne characters, objects and
  armour have been loaded so far (Phase 1). The RE Engine importer has only seen synthetic files.
- RE Engine: `.pak` archives with an encrypted table of contents (Monster Hunter Wilds and later)
  and GDeflate-compressed textures are not read; extract those games with a pak tool first.
  MPLY stage meshes, blend shapes and the skeleton's poses are not read.
- Output is not guaranteed watertight; most slicers repair it, but Phase 2 adds a proper fix.
- Without an anibnd beside the model, skinned meshes (characters, armor) are shown in their
  bind pose, which is a T or A pose. Armour pieces take the player's animations
  (`c0000.anibnd`) only when that archive is copied next to them.
- Animations that use root motion are shown in place; cloth simulated in the game keeps its
  bind pose.
- Isotropic remeshing needs the optional `pyacvd` package; the app falls back to subdivision
  without it. The Windows app includes it, with only the parts of VTK it uses.
