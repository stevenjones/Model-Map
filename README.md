# Texture Studio — a local drop-and-edit app

A small web app that runs on your own machine. Drop files on it, see exactly what's broken,
repoint textures from dropdowns with thumbnails, save.

**No installs.** Python standard library only, so if you have Python 3 you can run it. It binds
to `127.0.0.1` — nothing is exposed to your network, and nothing leaves your machine.

## Run it

```
python3 texstudio.py                      # uses ./texstudio_workspace
python3 texstudio.py "C:/Models/Chair"     # or point it at an existing folder
```

It opens `http://127.0.0.1:8765` in your browser. Ctrl-C in the terminal to stop.

## Use it

1. **Drop files on the page** — `.obj`, `.mtl`, textures, or whole folders. They're copied into
   the workspace. (Or just put them in the folder yourself and hit Refresh.)
2. **Read the table.** One row per texture reference, showing the material, the slot, the raw
   path as written in the file, and a thumbnail of what it currently resolves to.
3. **Fix anything with the dropdown.** Every image in the workspace is listed. Pick a different
   one and the thumbnail updates immediately, so you can confirm you've grabbed the right map
   before committing.
4. **Save changes.** Writes the MTL with working relative paths. **The original is kept as
   `.bak`.**
5. **Collect into maps/** — copies every referenced texture into one `maps/` folder and
   repoints the MTL there. This is what makes a model portable.
6. **Download zip** — the tidied folder, ready to hand on.

## What it flags

Everything below is a real fault from files you've sent me this session:

| Row status | Meaning |
|---|---|
| **OK** | resolves as written |
| **RELINKED** | the path was dead but I found the file — by name, or by stem if the extension differs |
| **MISSING** | genuinely not in the workspace. Drop it on the page and hit Refresh |
| **CHECK** | a `Kd` value that will multiply a texture darker, with a one-click "set 1 1 1" |

It also reports, above the table:

- `mtllib` naming a file that doesn't exist
- **`mtllib` names containing spaces** — valid but breaks many parsers
- **wrong slots**: `map_Ks` holding a roughness map, `map_refl` holding metalness, `map_Ns`
  holding roughness. These are the ones that fail *quietly* — no error, just a wrong render
- **images in the folder that nothing references** — usually an AO or normal map the exporter
  forgot to wire

## Tested on your own files

I ran it against two of the models from this session:

```
Chair 2.obj    [warn] mtllib 'Chair 2.mtl' contains spaces

Chair 2.mtl
   map_Ka    relinked by name  -> Textures/Chair2Albedo.png
   map_Kd    relinked by name  -> Textures/Chair2Albedo.png
   map_Ks    relinked by name  -> Textures/Chair2Rough.png   << WRONG SLOT -> map_Pr
   map_bump  relinked by name  -> Textures/Map__7_Normal_Bump.png
   map_refl  relinked by name  -> Textures/Chair2Metal.png   << WRONG SLOT -> map_Pm

painting.mtl
   Kd        darkens any texture by 100%   << Kd trap
   Kd        darkens any texture by 20%    << Kd trap
   map_Kd    missing
   map_d     missing

images nothing references: ['Textures/Chair2AO.png', 'Textures/Chair2Normal.png']
```

Every one of those is a genuine fault I had to fix by hand earlier. It also correctly reported
the painting's textures as *missing* rather than inventing a match, and spotted the chair's AO
map sitting unused.

After "Collect into maps/" I re-checked the result with my separate inspector: all five paths
resolve, `-bm 0.3000` survived the rewrite, and the `.bak` was kept.

## Two tools, different jobs

- **`texstudio.py`** — the app. Best when you want to *see* the textures and make choices.
- **`texcheck.py`** — the command-line version from before. Best for batch work:
  `python3 texcheck.py C:/Models/ --fix --collect` walks an entire library in one go.

They share the same logic; use whichever suits the task.

## Honest limits

**It fixes plumbing, not judgement.** It will not tell you whether a normal map is OpenGL or
DirectX, whether a "roughness" map is really glossiness that needs inverting, or which of two
texture sets belongs to which object. Those need measuring against the actual pixels, which is
most of what I've been doing for you.

It flags the `Kd` trap and the wrong-slot traps because those are unambiguous — the file is
plainly saying something it doesn't mean.

**Also worth knowing:** thumbnails only render for formats browsers can display (PNG, JPG, WebP,
BMP, GIF). TGA, TIFF, EXR and PSD will still be listed and relinked correctly, they just won't
preview.

## Files

- `texstudio.py` — the app, 396 lines, no dependencies
- `texcheck.py` — the CLI version, 178 lines
