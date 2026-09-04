# Model-Map

Find and fix broken texture links in 3D model files — **OBJ/MTL, FBX, .blend and .gltf**.

Downloaded models routinely arrive with texture paths pointing at the author's hard drive
(`N:\M n B\_MESH\...`), or at a `.png` when the archive shipped `.jpg`, or with a roughness
map plugged into the specular slot. None of these throw an error. They render wrong, or
render grey, and you find out later.

This finds them, and fixes what can be fixed safely.

**No dependencies.** Python standard library only — nothing to `pip install`.

## Requirements

Python 3.7+ (uses `ThreadingHTTPServer`).

## The app

```bash
python3 texstudio.py                      # uses ./texstudio_workspace
python3 texstudio.py "/path/to/model"     # or point it at an existing folder
```

Opens `http://127.0.0.1:8765`. Drop `.obj`, `.mtl`, `.fbx`, `.blend`, `.gltf` and texture
files on the page — folders work too. You get one row per texture reference: the material,
the slot, the raw path as written in the file, and a thumbnail of what it currently resolves
to. Repoint anything from a dropdown of every image in the workspace; the thumbnail updates
before you commit.

Buttons: **Save changes**, **Collect into maps/** (copies every texture into one folder and
repoints to it — this is what makes a model portable), **Place FBX textures**,
**Repoint .blend paths**, **Repoint .gltf paths**, **Download zip**.

## The command line

```bash
python3 texcheck.py MODEL.obj                       # report only, changes nothing
python3 texcheck.py MODEL.obj --fix                 # rewrite paths (keeps .bak)
python3 texcheck.py MODEL.obj --fix --collect       # also copy textures into maps/
python3 texcheck.py FOLDER --search ~/Textures      # search elsewhere too (repeatable)
python3 texcheck.py FOLDER --fix                    # walk a whole library
```

## What it detects

- Dead absolute paths (`N:\...`, `C:\Users\...`, `D:/`)
- `mtllib` naming a file that doesn't exist — and it names the MTLs sitting beside it
- **Filenames containing spaces** — valid, but breaks many parsers
- Textures findable under a different extension (`.png` referenced, `.jpg` shipped)
- **`Kd` values that darken a texture** — `Kd 0.084` renders soil near-black
- **Maps in the wrong slot** — `map_Ks` holding roughness, `map_refl` holding metalness,
  `SpecularColor` holding a roughness map in FBX
- Images in the folder that nothing references — often an AO or normal map the exporter
  dropped
- In glTF: an image the spec does not permit, and a `mimeType` that contradicts its own uri

## Per-format behaviour, and why it differs

The rows below are long because the reasoning matters; markdown table rows cannot be
wrapped without breaking the table.

| Format | What happens |
|---|---|
| **OBJ / MTL** | Paths rewritten in place. `.bak` kept. MTL options such as `-bm 0.3000` are preserved. |
| **FBX** | **Read-only.** A binary FBX cannot be safely rewritten — its internal node offsets would all have to be rebuilt, and getting that wrong corrupts the file silently. Instead, textures are copied beside the FBX under the exact filename it asks for, which is what every importer falls back to. The FBX itself is never modified. |
| **.blend** | Paths repointed **in place**. Blender stores each path in a fixed-size buffer, so a same-length-or-shorter replacement (e.g. `.png` → `.jpg`) changes nothing else in the file — no offsets move, DNA untouched. `.bak` kept. |
| **.gltf** | Paths rewritten **in place**. A `.gltf` is JSON, so there is nothing delicate about it: the uris are edited and the file written back. The `.bin` buffer and all binary data are untouched, and the buffer gets its own row because without it there is no geometry. `.bak` kept. |

**.blend compression:** gzip and uncompressed are supported. Blender 3.0+ can save with
zstd, which the standard library cannot read — the tool detects this and tells you to
re-save with compression off, or `pip install zstandard`.

**glTF is PNG and JPEG only.** An `.exr`, `.tga` or `.tif` reference parses fine but no
viewer will load it, so it is flagged. If a broken reference's only match on disk is one of
those formats, it is reported as **wrong format** and left alone — wiring it in would
produce a file that still does not open. `.glb` is binary rather than JSON and is skipped.

**Percent-encoded uris.** Per spec a glTF uri is percent-encoded, so `my%20texture.png`
names the file `my texture.png`. Paths are decoded before resolving and re-encoded when
written, so a texture with a space in its name is neither reported missing nor written back
in a form that breaks the file.

## Security model

The app binds to `127.0.0.1` and is **not reachable from your network**. Be clear about what
that does and does not mean:

- There is **no authentication**. Any process on the same machine can call its endpoints.
- `/api/upload` writes files into the workspace. Path traversal is blocked, but it is a
  write endpoint.
- Paths read out of a model file are **untrusted input**. A `.gltf` can name any uri it
  likes, including `../../../../etc/passwd`; every such path is resolved through the same
  guard as uploads, and one that escapes the workspace is refused rather than resolved,
  served or repointed.
- **Do not change the bind address to `0.0.0.0`.** It is not built to be exposed.

Nothing is sent anywhere. No telemetry, and no network access beyond serving your own
browser.

## Platform

Tested on Linux, macOS and Windows, on Python 3.9 and 3.12, via CI on every push. Python 3.7
is the floor (`ThreadingHTTPServer`), though only 3.9 and above are exercised in CI.

`Start Texture Studio.command` is a macOS launcher; on Windows run `python3 texstudio.py`
directly.

Thumbnails render only for formats browsers can display (PNG, JPG, WebP, BMP, GIF). TGA,
TIFF, EXR and PSD are still listed and relinked correctly, they just do not preview.

## What it does not do

It fixes **plumbing, not judgement**. It cannot tell you:

- whether a normal map is OpenGL or DirectX (that needs measuring against a height or AO
  map)
- whether a "roughness" map is really glossiness that needs inverting
- which of two texture sets belongs to which mesh when the names do not say
- whether a map in an odd slot was a mistake or deliberate

It flags the `Kd` and wrong-slot cases because those are unambiguous — the file is plainly
saying something it does not mean. The rest needs a human looking at the image data.

## Files

| File | Purpose |
|---|---|
| `texstudio.py` | The drag-and-drop app |
| `texcheck.py` | The command-line version |
| `fbxread.py` | FBX reader — **required** for `.fbx` support |
| `blendread.py` | `.blend` reader and repointer — **required** for `.blend` support |
| `gltfread.py` | `.gltf` reader and repointer — **required** for `.gltf` support |

All five must sit in the **same folder**. `texstudio` and `texcheck` import the three
readers; if one is missing, that format is skipped with a note rather than an error.

## License

MIT — see `LICENSE`.
