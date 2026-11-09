# Game profiles

A game profile is a JSON file that tells Dark STL how to read one game: which engine reads it, how its folder is
recognised, the engine release and keys its files need, how the folders and names of its files sort its models
into kinds and groups, what its texture names mean, which of its text names its models, and what Collect keeps
besides the models.

Dark STL ships its profiles in `src/darkstl/importers/profiles/` (in the installed app, inside its `engine`
folder; Settings › Game tools › Game profiles lists them). Your own go in the **profiles folder** of Dark STL's settings
folder (Settings › Game tools › Game profiles › Open the profiles folder):

| System  | Profiles folder                                   |
|---------|---------------------------------------------------|
| Windows | `%APPDATA%\DarkSTL\profiles\`                     |
| macOS   | `~/Library/Application Support/DarkSTL/profiles/` |
| Linux   | `~/.config/darkstl/profiles/`                     |

* A file there with the same file name as a shipped one (`returnal.json`) **replaces** it. Settings › Game
  tools › Game profiles › *Copy to change* copies a shipped profile there for you.
* Any other file there **adds** a game.
* A file is read again whenever it changes; rescan the game's folder to see its models sorted the new way.
* Files that can't be read are listed under Settings › Game tools › Game profiles, with the reason.

When you add the folder of an Unreal Engine game Dark STL has no profile for, the add-folder dialog says so and
asks for the game's name; Dark STL then writes a profile for it into the profiles folder (recognised by its
executable and its project's `Content/Paks` folder, sorted by the shipped defaults), so the folder is recognised
from then on and its sorting can be changed in that file.

## The file

```json
{
  "format": 1,
  "name": "Returnal",
  "engine": "unreal4",
  "importer": "returnal",
  "extends": "",
  "recognise": {...},
  "unreal": {...},
  "models": {...},
  "textures": {...},
  "names": {...},
  "collect": {...}
}
```

| Key        | What it is |
|------------|------------|
| `format`   | 1. A file made for a newer Dark STL (a higher number) is left out and listed as a problem. |
| `name`     | The game's name, as the library lists it. Required. |
| `engine`   | Which backend reads it: `unreal4`, `unreal5`, `unreal3`, `fromsoft`, `reengine`, `redengine` (and `renderware` from the Grand Theft Auto importer). Required unless the profile `extends` another. |
| `importer` | The importer that reads it, when not the engine's usual one (`unreal` for Unreal 4/5). The shipped Returnal and Borderlands profiles name their own importers (`returnal`, `borderlands`). |
| `extends`  | Another profile's name: that profile's settings are taken first and this file's go over them (each section is merged key by key, one level down). |
| `defaults` | `true` only in `unreal-defaults.json`: the settings every Unreal 4/5 game starts from (its own file's settings go over them, and its `models` keys replace the defaults' one by one). |
| `notes`    | Free text, ignored. |

Sections other engines don't know are ignored, so one file can carry notes or settings for later versions.

### `recognise`: which folder is the game

```json
"recognise": {
  "files": ["Returnal/Binaries/Win64/Returnal-Win64-Shipping.exe", "Returnal.exe"],
  "folder_names": ["returnal"],
  "folder_pattern": "borderlands ?3|\\bbl3\\b"
}
```

* `files`: paths (with `/`) below the folder, or below its parents up to the game's install folder, that only
  this game has. Matched without regard to case. One is enough. For FromSoftware games, `a+b` means both.
* `folder_names`: a folder of this name in the path says it's the game (`returnal` for `D:\Games\Returnal\...`).
* `folder_pattern`: a regular expression the folder's path matches.

### `unreal`: Unreal 4/5 games

```json
"unreal": {
  "project": "Returnal",
  "release": "auto",
  "default_release": "4.25",
  "aes_keys": ["0x…64 hexadecimal digits…"],
  "usmap": {"words": ["returnal"], "type_prefix": ""},
  "layout": "",
  "look_through_for_animations": true
}
```

* `project`: the project folder the game's files are under (`<install>/<project>/Content/Paks`).
* `release`: the Unreal Engine release its packages were cooked with: `"auto"` reads it from the game's
  executable; `"4.26"` and the like say it outright (needed for games cooked without version numbers whose
  executable doesn't say). `default_release` is used when `auto` finds nothing.
* `aes_keys`: keys that open its encrypted `.pak` files. Keys from Settings › Game tools › Unreal games and from an
  `unreal-keys.txt` in the game's folder are tried too.
* `usmap.words`: words in a `.usmap` file's name that say it's this game's type mappings (from Settings ›
  Game tools › Unreal games); `type_prefix`: what the game's own classes start with (`Oak` for Borderlands 3).
* `layout`: `"bl3"` for Borderlands 3's own changes to the package layout; empty for every other game.
* `look_through_for_animations`: when no animation is named like one, look through every package for those
  made for a model's skeleton (slower the first time).

### `models`: sorting models into kinds and groups (Unreal 4/5)

```json
"models": {
  "skeletal": "^(sk|skm)_",
  "static": "^(sm|stm)_",
  "skip": "(_physics|_collision|_col)$",
  "label_prefix": "^(SK|SKM|SM)_",
  "peek": {"names": "unnamed", "limit": 60000, "not_a_mesh": "^(t|m|mi)_", "skip_folders": ["audio", "ui"]},
  "group_skip": ["meshes", "models"],
  "top_skip": ["art", "game"],
  "tables": {"makers": {"jak": "Jakobs"}},
  "kinds": [
    {"folders": ["weapons", "guns"], "kind": "Weapons", "group": ["{1|Weapons}"]},
    {"folders": ["characters"], "name_has": ["head", "hair"], "class": "SkeletalMesh",
     "kind": "Faces & Hair", "group": ["{1}"]},
    {"folders": ["enemies"], "kind": "Characters", "group": ["Creatures", "{1}"]}
  ],
  "otherwise": {
    "StaticMesh": {"kind": "Objects", "group": ["{top}"]},
    "SkeletalMesh": {"kind": "Characters", "group": ["{top}"]}
  }
}
```

* `skeletal` / `static`: a file name matching these is a skeletal or static mesh without reading it.
* `skip`: meshes the game doesn't show (physics, collision) are left out.
* `label_prefix`: taken off the file name for the model's label (`SK_Carbine` is listed as `Carbine`).
* `peek`: which packages whose names don't say what they are have their class read from their headers
  (once; kept until the paks change). `names`: `"none"`, `"unnamed"` (any not named as something else by
  `not_a_mesh`), or a regular expression the name must match; `limit`: at most this many; `skip_folders`:
  never in these folders; `folders`: only in these (with a regular expression); `containers_only`: only
  packages in IoStore containers, whose headers are quick to read.
* `kinds`: rules tried in order; the first that matches a model sorts it. A rule matches when one of its
  `folders` is among the folders below `Content` the model is in (its first word found there is the one used),
  its `name_has` words (any) are in the file name, its `name_matches` regular expression matches the file name,
  and its `class` (`SkeletalMesh` or `StaticMesh`) is the model's; a rule may leave any of these out. `kind` is
  one of `Weapons`, `Armor`, `Characters`, `Faces & Hair`, `Objects`, `Map Pieces`, `Models`, `Other`.
* `group`: the group's pieces, joined with ` / ` (empty pieces are dropped). In a piece, `{1}`, `{2}`... are the
  first, second... folder after the folder the rule matched (folders starting with `_` and those in
  `group_skip` aren't counted); `{top}` is the first folder below `Content` (not counting `top_skip`);
  `{stem}` is the file name; `{1:makers}` looks the folder up in the table `makers` of `tables` (by its
  lower-case name without spaces); `{1|top|Weapons}` takes the first alternative that has a value, and a word
  that isn't a placeholder (`Weapons`) is written as it is. Folder names are written with `_` as spaces.
* `otherwise`: how models no rule matches are sorted, by class.

### `textures`

```json
"textures": {"normal_green": "directx", "roles": {"normal": ["_comp\\d*$"]}}
```

* `normal_green`: which way the game's normal maps' green channel points: `directx` (down, Unreal's) or
  `opengl` (up).
* `roles`: regular expressions for texture names that play a role (`normal`, `albedo`, `specular`) beyond the
  usual `_N`, `_D`, `_BaseColor`... endings.

### `names`: in-game names from string tables (Unreal 4/5)

```json
"names": {
  "tables": "(^|/)(st_[^/]*|[^/]*stringtable[^/]*)\\.uasset$",
  "keys": "([_.]|^)(name|displayname|itemname|title)$",
  "skip_keys": "desc|flavou?r|lore|tooltip",
  "limit": 400
}
```

After a scan, the `StringTable` assets whose paths match `tables` (at most `limit`) are read. A key ending
as `keys` says holds a name (`WPN_Blaster_Name`); without that ending and without a kind prefix or level of
detail, it names the mesh of the same name (`SK_WPN_Blaster_LOD0`). Keys matching `skip_keys` are left out, and so
is a mesh two texts claim. The names are shown in the library beside the models' own names (as the
FromSoftware games' are); names you give a model win.

### `collect`

```json
"collect": {"engine_release": true, "usmap": true}
```

What a collected copy keeps besides the models, so it reads without the game: the engine release (a collected
copy has no executable to read it from) and the game's `.usmap` type mappings.

### `fromsoft`, `reengine`, `redengine`: the other engines

The FromSoftware, RE Engine and Cyberpunk games keep their tables in Dark STL's code; a profile in the profiles
folder with the game's `name` and `engine` changes what it says, and a profile with a new name adds a game of
that engine:

```json
{"format": 1, "name": "Armored Core VI", "engine": "fromsoft",
 "recognise": {"files": ["armoredcore6.exe"], "folder_pattern": "armored\\s*core"},
 "fromsoft": {"compression": "KRAK", "texture_paths_in_flver": false, "archive_path": "^N:\\\\FNR\\\\",
              "platform": "PC", "notes": "Oodle-compressed"}}
```

* `fromsoft`: `recognise.files` are marker files (`a+b`: both), `recognise.folder_pattern` the game's name in a
  path; `compression` (`DFLT`, `KRAK`, `ZSTD`), `texture_paths_in_flver` (false when materials name their
  textures in MATBIN files), `archive_path` (how paths inside the game's archives begin), `platform`.
* `reengine`: `recognise.files` are executables, `recognise.folder_pattern` the name in a path;
  `reengine.lists` names bundled pak file lists (`RE4_STM_Release`).
* `redengine` (Cyberpunk 2077): `recognise.files` and `recognise.folder_pattern` add to the ways its folder is
  recognised.

## Shipped profiles

| File                   | Game                    | Importer      |
|------------------------|-------------------------|---------------|
| `unreal-defaults.json` | any other Unreal game   | (defaults)    |
| `returnal.json`        | Returnal                | `returnal`    |
| `borderlands3.json`    | Borderlands 3           | `borderlands` |
| `wonderlands.json`     | Tiny Tina's Wonderlands | `borderlands` |
| `borderlands4.json`    | Borderlands 4           | `borderlands` |
