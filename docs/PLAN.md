# Dark STL: plan

Dark STL is a desktop tool that reads models and textures extracted from games, starting with
the FromSoftware games (Dark Souls, Bloodborne, Sekiro, Elden Ring) and Cyberpunk 2077, lets you browse them
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
| REDengine 4 (Cyberpunk 2077) | `.mesh` and `.xbm` in the CR2W container, loose (WolvenKit exports under their depot paths) or inside the game's `.archive` packs, read directly; `.mi`/`.mt` material chains; multilayered materials (`.mlsetup`, `.mltemplate`, `.mlmask`) blended into one colour and one normal map; `.app`/`.ent` appearance packages for weapon configurations; Kraken buffers through the game's `oo2ext_7_win64.dll`. Formats worked out with WolvenKit as the reference | Built on synthetic files; awaiting a check against the game |
| RE Engine (Capcom) | `.mesh` models (every generation from RE2 to RE Requiem, streamed buffers included), `.mdf2` materials, `.tex` textures, read from extracted files or from `.pak` archives through a community file list. Games in `importers/reengine/games.py` | Synthetic RE2 and RE4 files; real files next |
| Wavefront OBJ | OBJ + MTL with PNG/JPG/TGA/DDS textures, from any other tool | Done |
| glTF | `.gltf` (with its `.bin` and pictures) and `.glb`, as Blender, FModel and most tools export: each mesh a part placed by its node, base colour and normal textures (green up), Y up turned to Z up; a model too big to be in metres read as centimetres. Skinned meshes in their bind pose | Synthetic files (0.35.0) |
| ActorX PSK/PSKX | UModel's and FModel's exports of Unreal models: points, wedges, faces and materials, mirrored from Unreal's frame like the Unreal games' own models; each material's textures found by name beside the model, from UModel's `.mat`, FModel's `.json` and UModel's `.props.txt` material files, then from texture names like the material's. The skeleton isn't read | Synthetic files (0.35.0) |

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
model's enhanced model, so it is exported, made watertight or cut like a fresh one; "Use its
settings" chooses what it was made with again.

Models combined into one (Combine with other models…: a character's face, body and hair) are
configurations of the user's own (`configs.py`) whose base is the model and whose parts are the
others, each part a slot that is on by default. Configurations name their models by the path in
the game, which also finds the model's package, so a combined model holds once the game is
imported and removed.

### The app: a game library on Model Library's interface

The app is a small local web server (`app/server.py`) and a page (`web/`) shown in the
system's own web view through pywebview, or in the browser with `--browser`. The page is the
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
  collected files), with the Oodle DLL. Engine commands `unpack_plan`, `unpack_run`,
  `filelists`, `filelists_download`; CLI `darkstl unpack`. Dark Souls II `.bnd` models are now
  listed too (unchecked on real files).
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
skeleton in a game that may name its animations otherwise (Returnal: `vfs.LOOK_THROUGH_FOR_ANIMATIONS`),
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

### Reading id Tech 6/7/8 files (DOOM, DOOM Eternal, DOOM: The Dark Ages)

Planned (requested by Jerred on 2026-11-09); built in three releases, one per game, by a single
importer, `importers/idtech/`, with a profile per game. The layouts below were worked out from
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
| DOOM: The Dark Ages | id Tech 8 | `DOOMTheDarkAges.exe`, `base/packagemapspec.json` | `IDCL` v13 `.resources`, `.streamdb` | Oodle Kraken / Leviathan | none shipped (built into the exe): Eternal's `oo2core_8`, any `oo2core_9_win64.dll`, or Settings › Oodle |

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
the StreamDB; then a 36-byte record per mip (40 in The Dark Ages, with depth) and the mips kept
in the container. Decoded through `texture2ddecoder` and wrapped as DDS for packages like the
other importers. Albedo is sRGB; normal maps are two-channel BC5 (Z rebuilt), their green
direction to be found on a known flat sample and set in the profile.

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
quaternions. The Dark Ages streams long animations (a stream info and disk layout like geometry).
Animations are matched to a model by skeleton name; the list is built once per game and cached.

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
architecture and level geometry → Map Pieces; gore, wound and lower-detail copies hidden by
default. Paths not matching a rule fall back to their first folder. In-game names come from the
`entityDef` decls (`generated/decls/entitydef/...`, whose render model names the md6) and the
`.blang` string tables (`strings/english.blang`) through the names hook (R2).

**Collect and packages.** Model packages (the default) hold the mesh, skeleton, weights,
materials, textures (as DDS) and sampled animations, so nothing of the game is needed. *Game
files* writes each resource a model reads, decompressed, under its resource name in
`<library>/<game>/`, with its StreamDB pieces (decompressed, `streamdb/<id>.bin`) and a small
index of the entries' types, hashes and sizes, so the copies open without the game and without
Oodle; the importer reads such a folder like an install.

**Layout.** `importers/idtech/`: `formats/` (`resources.py` IDCL containers and the container
mask, `index6.py` DOOM 2016's `.index`, `streamdb.py`, `image.py`, `geometry.py` vertex masks
and streams, `staticmodel.py`, `md6model.py`, `md6skel.py`, `md6anim.py`, `decl.py` the decl
parser), `games.py` (the three profiles: executable, container version, hash scheme, unit,
normal green, what is shared; read from R7's JSON profiles once those land), `store.py` (a
game's index: names → live copies, streamed pieces, loose extracted files), `materials.py`,
`catalog.py`, `animation.py` (poses for the Pose tab and packages) and `__init__.py`.

**Phases.** 0.53.0 DOOM Eternal (containers, mask, StreamDB, images, static and skinned models,
skeletons, animations, materials, catalog, names, Collect, packages); 0.54.0 DOOM (2016) (its
`.index` and the older layouts, from Jerred's samples); 0.55.0 DOOM: The Dark Ages (version 13
containers, new stream ids, the changed image, model and animation layouts; Valen reads them, so
this is mostly a profile and the differences). Every format is tested on byte-built files.
Risks: Oodle for The Dark Ages (no DLL in its folder; Eternal's or another `oo2core` needed, said
in the app when missing); BCPack entries (consoles) are skipped; id's unit and the normal-map
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

### Making it watertight (Finish tab, before cutting or export)

Post-processing for printing lives on the **Finish** tab (`finish.py`). *Make watertight* wraps the
whole model (enhanced or original) into one solid with nothing left inside:

- **Parts made solid and joined**, as for cutting (below): where parts cross, the union removes the
  triangles inside the other part, and the outside surface stays exactly as it was. Parts are
  connected through shared edges only, so two closed parts that touch at a vertex (a plate's corner
  on a body) are each kept as they are; a part still tangled is tried again split into the game's
  own sub-meshes before falling back to voxels.
- **Hollows filled** (default on): the union's surface is split into its closed shells; shells
  facing inwards (negative volume) bound space sealed inside, so only the outward shells are kept
  and united again.
- **Gaps closed** (optional, up to a width): the solid is marked in a voxel grid (a quarter of the
  width, at most about 6 M voxels), closed morphologically with a ball of half the width, eroded
  two voxels so the bridges sit under the real surface, and turned into a surface (manifold's level
  set) that is united with the exact solid. Only where it bridges a gap does it show.
- **Loose bits removed** (optional): separate solids under a share of the biggest one's volume.

The result is kept per model, source and settings (last 2), shown as *Finished*, exported as
`<name>_watertight`, and can be cut (*Cut › Finished*); it has no UVs or textures.

### Configurable models: a base and parts chosen per slot

Weapons in Cyberpunk 2077, the RE Engine games and Borderlands are kept as a body and separate
part models (magazines, sights, barrels, stocks, muzzles). A *configuration* (`configs.py`) names
the base model and, per slot, the parts that fit it and where each goes; choosing a part per slot
(or a preset that chooses them all) gives one assembled `SourceModel`, so the Mesh, Displace, Cut
and Export steps need nothing new. The library lists each configuration as a model of its own
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

### Cutting into pieces (before export)

A model too big for the printer, or with parts that print better apart, is cut on the **Cut** tab
(`cut.py`, on [manifold3d](https://github.com/elalish/manifold), whose booleans are exact and always
give closed meshes).

- **Made solid first.** A plane through an open shell has no inside to close, and game meshes are
  open shells with overlapping parts. So the model (the enhanced one, or the original) is welded,
  duplicate triangles are dropped (two-sided cloth is often one side and its mirror), and each
  connected part is taken in turn: already closed, it is used as it is; with small openings (fill
  area under 30 % of the part's), each opening is closed with a fan; an open surface (a cape) is
  thickened inwards by *Open surfaces* (1.2 mm); anything still broken (edges shared by three
  faces) is rebuilt from voxels as a last resort. The parts are then united into one solid. The
  solid is kept, so moving a cut only cuts again.
- **Cuts.** Each cut is a plane (a normal and a point, in model millimetres) split exactly through
  every piece it crosses. In the 3D view a plane is a translucent square through the model's middle;
  dragging it slides it along its normal (the closest point between the pointer's ray and the
  normal's line), dragging its round handle tilts it (the handle follows the pointer over a plane
  facing the camera), and *Pick a point* moves it through a clicked point. The tab has the same as
  a position slider, tilt and turn angles, and X / Y / Z / *Facing me* presets.
- **Joints**, per cut, each with its own **clearance** (the gap on each side between parts that
  fit together):

  | Joint | What it makes |
  | --- | --- |
  | Flat | The faces as they are, to glue |
  | Pegs | Pegs on the side the normal points to (or the other), holes wider by 2 × clearance and deeper by the clearance on the other |
  | Dowels | Holes on both sides, and dowel pieces of the exact diameter (or none, for bought pins or filament) |
  | Magnets | Round pockets on both sides for disc magnets, wider and deeper by the clearance |
  | Key | A tongue shaped like the cut face inset by the wall, into a groove inset by the wall less the clearance: the pieces only fit one way |

  The cut face is sliced (the solid turned into the plane's frame) and each island of it (a leg, a
  sleeve) gets its own features: round ones go where a grid of candidates inside the face, inset by
  the radius and the *Wall around joints* (1.6 mm), is deepest, or (two or more) as far apart as
  they can be. A feature that would break through the far side of a piece (tested by intersecting a
  slightly fatter probe with the piece) is shortened, tried in the middle alone, or left out, and the
  tab says how many places were left flat.
- **Results.** The pieces (top down, then dowels) are kept like generated models (the last three
  sets) and shown in colour under *Show › Pieces*, spread apart with a slider. Export writes each
  to its own file in a `<name>_pieces` folder, laid on its biggest flat cut face without pegs or a
  tongue on it, centred on the bed. On the command line, `--cut AXIS=MM[:JOINT]` (or `AXIS=50%`)
  and `--clearance` do the same for `export` and `enhance`.

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
    redengine/  formats/ (CR2W container, .archive packs, CMesh render blobs, CBitmapTexture),
                depot.py (depot paths -> loose files or archive members), materials.py (.mi/.mt
                chains -> texture roles), mesh.py (chunks -> mesh), catalog.py (names learnt
                from import tables, groups by depot path)
    reengine/   formats/ (mesh, tex, mdf2, pak with MurmurHash3 names), games.py (RE Engine games and
                how a folder is recognised), files.py (a model, its streamed half, materials and textures,
                on disk or inside a pak, and its animations), materials.py (texture roles from slot
                types), catalog.py (classifies models by the engine's folder layout), animation.py
                (.motlist / .mot motions -> poses, skinning)
    wavefront.py  OBJ + MTL
    gltf.py     glTF 2 (.gltf, .glb) from Blender, FModel and other tools
    psk.py      ActorX .psk/.pskx from UModel and FModel, and their material files
    loose.py    the loose pictures in a folder of other tools' models, listed for the texture picker
    package.py  model packages (see package.py), listed and opened like any game's models
  geometry/     TriMesh, subdivision, isotropic remesh, UV seam matching, closest-point queries,
                displacement
  textures/     DDS decoding, normal map -> height, grayscale height, UV coverage, texel size
  library.py    the library: game folders scanned by every importer, by game, kind and group
  configs.py    configurable models: configuration lists, finding their files, assembling a choice
  collect.py    copies the files chosen models need into the app's own library folder
  package.py    model packages: writing and reading them, the asset store (textures, animations)
  game_textures.py  every texture of a game kept in its asset store: listed, estimated, stored
  posing.py     posing a mesh from a skeleton and per-vertex weights (packages)
  overlay.py    the library overlay: the user's own changes per game (names, folders, textures)
  variations.py saved variations: enhanced models kept beside the model's package
  thumbnails.py preview pictures for the library's cards, cached on disk
  pipeline.py   the three steps with their settings
  cut.py        the model made solid and cut into pieces with joints (manifold3d)
  export.py     STL, OBJ, PLY, GLB writers
  cli.py        command line
  app/          the desktop app: service.py (the page's commands), server.py (local web
                server), meshpack.py (meshes for the 3D view), config.py, desktop.py (window)
  web/          the page: Model Library's interface (styles.css, ui/ layout, inspector,
                menus) with Dark STL's pages: home.js, browser.js, modelpage.js, configure.js,
                settings.js
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
      solidify step (voxel or winding-number remesh) that outputs one closed manifold. Cut
      pieces are watertight since 0.9.0 (openings closed, open surfaces thickened, parts
      united); the same could be offered for the whole model
- [x] Cut into pieces with joints (flat, pegs, dowels, magnets, key) and a clearance per cut
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
- [ ] DOOM (2016), DOOM Eternal and DOOM: The Dark Ages (id Tech 6/7/8), one release per game
      (0.53.0 Eternal, 0.54.0 DOOM 2016, 0.55.0 The Dark Ages); see "Reading id Tech 6/7/8 files"

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

### Backlog (on hold)

- [ ] **Game files instead of per-game code.** Most of what makes a game differ from others on
      the same engine is settings: its folder layout and executable, engine release, AES key and
      type mappings, how folders and names sort models into groups, texture role suffixes
      (`_BaseColor`, `_N`, ...), the normal maps' green direction, and what Collect copies. Move
      those into a JSON file per game that names the engine backend it uses (`unreal4`,
      `unreal3`, `fromsoft`, `reengine`, `redengine`): shipped with the app, overridable from a
      settings folder (like configurations), so a game is added or tuned without a release.
      Engine readers stay code, and format quirks (unversioned packages, compressed packages,
      appearance files) are still fixed there. Start with the Unreal 4/5 games (Returnal,
      Borderlands 3, Wonderlands, Borderlands 4) and a generic Unreal fallback, then the other
      engines. Raised by Jerred on 2026-11-07, held for now.
- [ ] **Model packages between the importers and the editor** (approved by Jerred on 2026-11-07
      and being built in stages, see Phase 5). Importers convert a game model
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
      Raised by Jerred on 2026-11-07, held for now.

## Known limits today

- Of the real game files, Dark Souls Remastered weapons and Bloodborne characters, objects and
  armour have been loaded so far (Phase 1). The RE Engine importer has only seen synthetic files.
- RE Engine: `.pak` archives with an encrypted table of contents (Monster Hunter Wilds and later)
  and GDeflate-compressed textures are not read; extract those games with a pak tool first.
  MPLY stage meshes and blend shapes are not read. Posing RE models has only been tried on
  synthetic motions; motion trees, additive layers and animations kept in `.motbank` /
  `.motfsm2` setups (rather than in the motlists themselves) are not used.
- Output is not guaranteed watertight (pieces cut on the Cut tab are); most slicers repair it,
  but Phase 2 adds a proper fix.
- Without an anibnd beside the model, skinned meshes (characters, armor) are shown in their
  bind pose, which is a T or A pose. Armour pieces take the player's animations
  (`c0000.anibnd`) only when that archive is copied next to them.
- Animations that use root motion are shown in place; cloth simulated in the game keeps its
  bind pose.
- Isotropic remeshing needs the optional `pyacvd` package; the app falls back to subdivision
  without it. The Windows app includes it, with only the parts of VTK it uses.
