# Configuration lists

A configuration list tells Dark STL which models are a **base** (a weapon's body) and which
**parts** fit it, slot by slot. Each configuration shows in the library as a model of its own,
and its page opens on the **Configure** tab, where a part is chosen for each slot. The parts
chosen are put together into one model that the Mesh, Displace, Cut and Export tabs work on.

## Where lists are read from

Later sources replace earlier ones when a game has two configurations with the same `id`:

1. **Guessed from the game's folders.** For Cyberpunk 2077 and the RE Engine games, each
   weapon folder whose files are named after one model (`w_rifle_ajax.mesh`,
   `w_rifle_ajax_mag_std.mesh`, `w_rifle_ajax_scope.mesh`) becomes a configuration: the model
   with the shortest name, or one ending in `base`/`body`, is the base; the others are sorted
   into slots by words in their names (mag, scope, muzzle, stock, barrel, grip...). These say
   *Guessed* on the Configure tab. An importer can work configurations out of game data instead
   through its `configurations(folder, game, entries)` hook.
2. **Shipped with Dark STL:** `src/darkstl/importers/<importer>/configurations/*.json`.
3. **In a game folder:** any `*.darkstl-configs.json` at the top of a folder you added, or one
   folder down. Lists you share with others go here.
4. **Your own:** what *Edit…* on the Configure tab and *Make configurable…* in a model's More
   menu save, one file per game in `<settings>/configurations/` (on Windows
   `%APPDATA%\DarkSTL\configurations`). Remove on the editor deletes yours, and the list it
   replaced shows again.

## The format

```json
{
  "format": "darkstl-configurations",
  "version": 1,
  "game": "Cyberpunk 2077",
  "configurations": [
    {
      "id": "ajax",
      "label": "Militech M251s Ajax",
      "category": "Weapons",
      "group": "Assault rifles",
      "base": {"file": "base/weapons/assault_rifles/militech_ajax/entities/meshes/w_rifle_assault_militech_ajax.mesh"},
      "slots": [
        {
          "id": "mag", "label": "Magazine", "required": true, "default": "std",
          "options": [
            {"id": "std", "label": "Standard", "model": "w_rifle_assault_militech_ajax_mag_std"},
            {"id": "ext", "label": "Extended", "model": {"file": "w_rifle_assault_militech_ajax_mag_ext.mesh", "variant": "default"}}
          ]
        },
        {
          "id": "sight", "label": "Sight",
          "options": [
            {"id": "scope", "label": "Long scope", "model": "w_att_scope_long_01",
             "attach": {"socket": "scope", "offset": [0, 0, 0.004]}}
          ]
        }
      ],
      "presets": {"Stock": {"mag": "std", "sight": ""}, "Marksman": {"mag": "ext", "sight": "scope"}}
    }
  ]
}
```

- **`base`, `model`**: a model file, written as a string or as `{"file", "variant", "parts"}`.
  `file` is an absolute path, a path below the game folder (forward slashes, any case; files
  inside `.archive` and `.pak` packs too), or just a file name or stem, looked up among the
  game's models. `variant` picks a model in a file that holds several (a Cyberpunk appearance,
  a FromSoftware binder's second model); `parts` keeps only some of its parts (indices, as the
  Model tab lists them).
- **`slots`**: `required` slots always have a part; the others can be *None*. `default` is the
  option chosen when the model is opened (else the first for a required slot, none otherwise).
  `kind` is `"parts"` (the default: the chosen option adds a part) or `"material"`: the
  chosen option adds no geometry; its materials repaint every part of the model instead (its first
  material replaces each part model's first material, its second the second, and so on; materials
  beyond those it has stay as they are). Borderlands 2 and The Pre-Sequel use it for gun skins,
  which the game puts in the first material slot of the gun's meshes.
- **`attach`** places a part. Without it the part is taken to be modelled in place, as game
  weapon parts usually are. Otherwise, in this order:
  - `socket`: a named point of the base model, or of the part chosen for slot `to`
    (`"to": "rail"` puts a sight on whichever rail is chosen). Importers that know a model's
    attachment points list them as its sockets.
  - `matrix`: 16 numbers, row by row, in metres.
  - `offset`: `[x, y, z]` in metres, along the model's axes (Z up).
  - `rotate`: degrees about X, then Y, then Z.
  - `scale`: one number.
- **`presets`**: names for a whole choice, slot id to option id (`""` for none).

Files that aren't found are left out of their slot, and a configuration whose base isn't found
isn't listed.
