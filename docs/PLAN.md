# Dark STL: plan

Dark STL is a desktop tool that reads models and textures extracted from FromSoftware games
(Dark Souls, Bloodborne, Sekiro, Elden Ring), lets you browse them grouped by type, and turns
them into high-fidelity meshes for 3D printing and rendering. It does that in three steps:

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

### Reading the game files

| Layer | Formats | Notes |
| --- | --- | --- |
| Compression | DCX (`DFLT` zlib, `KRAK` Oodle Kraken, `ZSTD`), DCP | Kraken (Sekiro, Elden Ring) uses the game's own `oo2core_*_win64.dll`, found in the game folder or set in the app. |
| Archives | BND3, BND4, BXF3/BXF4 (`.xxxbhd` + `.xxxbdt`) | Nested archives and `.dcx` members are opened on demand. |
| Models | FLVER2 (version 0x2000C to 0x2001A) | Every vertex layout the PC and PS4 releases use, including the packed ones (PS3 edge compression is not supported); triangle strips and lists; LOD and motion-blur face sets are skipped; rigid parts are placed by their bones. |
| Textures | TPF, DDS | BC1 to BC7 and the uncompressed formats. Bloodborne's PS4 textures have no DDS header and are tiled; they are rebuilt into DDS and untiled. |

Textures are looked up in this order: inside the model's own archive, in a sibling
`.texbnd` / `_h.texbnd` / `.tpf` (characters and objects), in unpacked folders next to the model,
and finally in any extra texture folders you add (map pieces share `mXX_XX_XX_XX_tex` packs).
Each material's maps are taken from the texture paths stored in the FLVER; when those are
empty (Elden Ring keeps them in MATBIN files) the suffix convention is used instead
(`_a` albedo, `_n` normal, `_h` height).

The library groups models by their FromSoftware naming:

| Prefix | Category | Grouped by |
| --- | --- | --- |
| `wp_` | Weapons | ID range (`IDs 02xx`) |
| `hd_` `bd_` `am_` `lg_` | Armor | Head, Body, Arms, Legs, with Male/Female |
| `fc_` `fg_` `hr_` `ey_` ... | Faces & Hair | part (Face, Hair, Eyes...) |
| `c0000`, `cXXXX` | Characters | Player, then family (`c11xx`) |
| `oXXXXXX`, `aegXXX_XXX` | Objects | ID range (`o12xxxx`) or asset group (`aeg099`) |
| `mXX_XX_XX_XX_...` | Map Pieces | map (`m10_00`) |

Low-detail copies (`_l`) are hidden by default, and the game is detected from the folder
(executable, `dvdroot_ps4`, or the folder name).

### Step 1: even vertex spacing

Two methods, both in the app and the command line:

- **Adaptive subdivision** (default). Every edge longer than a limit is split at its midpoint,
  repeatedly, until none is longer. The limit is chosen so the result lands near the target
  vertex count (within about 10 %). New vertices sit on the original triangles, so UVs are
  interpolated exactly and there is no texture drift, and hard edges stay sharp.
- **Isotropic remesh.** Hits the target count closely and gives near-equilateral triangles of
  one size everywhere (ACVD clustering, then every new vertex is projected back onto the
  original surface to recover exact UVs). Useful when the triangle shape matters, for example
  for further sculpting.

Both methods can **round off curved surfaces**: each new midpoint is bent onto the curve that
the model's own shading normals describe (Phong tessellation at edge midpoints). A
16-sided grip comes out round instead of keeping 16 flat facets, flat-shaded parts stay flat,
and hard edges stay closed. This is on by default.

### Step 2: displacement from textures

- **Normal maps (default).** FromSoftware models carry detail mostly in normal maps, so the
  normal map is turned into a height map. The slopes it encodes are integrated with a
  Poisson solve, limited to the parts of the texture the UVs actually use. The height comes
  out in texel units and is converted to model units with the measured size of one texel on
  the model, so strength 1.0 means "the relief the normal map implies". A high-pass then
  removes the very broad shapes the mesh already has (the "Detail radius" setting).
- **Height / grayscale maps.** Used directly, with a chosen height range in millimetres.
- **Green channel.** DirectX and OpenGL normal maps flip green. The convention is
  auto-detected per texture and can be forced.
- **Texture detail finer than the vertex spacing** is blurred out first, so it cannot alias
  into noise.
- **Seams.** The mesh is welded before resampling, and every vertex averages the height of
  all the UV islands it touches, so the surface never tears along UV seams.

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
  formats/      binary reader, DCX (+ Oodle), BND3/BND4/BXF, TPF, DDS (+ BC decoders), PS4 untiling, FLVER2
  geometry/     TriMesh, FLVER -> mesh (bones, axes, welding), subdivision, isotropic remesh,
                closest-point queries, displacement
  textures/     normal map -> height, grayscale height, UV coverage, texel size
  assets.py     finds a model and all of its textures on disk
  materials.py  works out which texture is the albedo / normal / height map for each material
  library.py    scans game folders and classifies models
  pipeline.py   the three steps with their settings
  export.py     STL, OBJ, PLY, GLB writers
  cli.py        command line
  ui/           explorer window (PySide6 + VTK viewport)
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

- [ ] Dark Souls (PTDE and Remastered): weapon, armor, character, map piece
- [ ] Dark Souls III: same set
- [ ] Bloodborne: same set, and confirm the PS4 texture untiling
- [ ] Sekiro and Elden Ring: same set with Oodle, and Elden Ring's material lookup
- [ ] Rigid parts placed by bones match the in-game pose
- [ ] Compare against the before/after example: same weapon, same look

### Phase 2: print-ready output

- [ ] Watertight option: game meshes are open shells with overlapping parts, so add a
      solidify step (voxel or winding-number remesh) that outputs one closed manifold
- [ ] Thin-part thickening for blades and capes
- [ ] Per-material strength and on/off in the material list
- [ ] Use detail and blend maps as well as the main normal map
- [ ] Height-map preview painted on the model before running the full step
- [ ] Streaming export for 10M+ vertex results

### Phase 3: explorer polish

- [ ] In-game names (weapon, armor and enemy names from the game's text and param files)
- [ ] Thumbnails in the library
- [ ] Batch queue: enhance and export a whole group with one set of settings
- [ ] Read Elden Ring MATBIN files for exact material maps
- [ ] Dark Souls II (FLVER2 with its own texture layout)

### Phase 4: distribution

- [ ] Windows build (single folder, no Python install needed)
- [ ] User guide with extraction steps for each game

## Known limits today

- Real game files have not been loaded yet (Phase 1).
- Output is not guaranteed watertight; most slicers repair it, but Phase 2 adds a proper fix.
- Skinned meshes (characters, armor) are shown in their bind pose, which is a T or A pose.
- Isotropic remeshing needs the optional `pyacvd` package; the app falls back to subdivision
  without it.
