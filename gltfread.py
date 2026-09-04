#!/usr/bin/env python3
"""
gltfread.py - read (and repoint) image URIs in a .gltf, stdlib only.

A .gltf is JSON, so unlike FBX and .blend there is nothing delicate about
rewriting it: parse, change the uri strings, write back. The buffer (.bin) and
all binary data live in separate files and are never touched.

Two checks specific to glTF that the OBJ/FBX/.blend paths don't need:

  * The spec allows PNG and JPEG only. A .gltf referencing .exr, .tga, .tif or
    .psd is non-conformant and most viewers will refuse it, even though the file
    parses fine.
  * Each image may declare a mimeType. If it says image/jpeg and the uri ends
    .png, the two disagree and loaders behave inconsistently.

glTF uris are PERCENT-ENCODED, so 'my%20texture.png' names the file
'my texture.png'. Resolution therefore happens on the decoded 'path', while the
stored 'uri' is left exactly as written; repointing encodes the new value again
so the file keeps conforming.

    from gltfread import read_gltf, repoint_gltf
"""
import json, os, shutil
from urllib.parse import unquote, quote

SPEC_OK = ('.png', '.jpg', '.jpeg')
MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}


def read_gltf(path):
    """-> {'ok','version','generator','images':[...],'buffers':[...],'error'}

    images: {'index','uri','path','mimeType','ext','spec_ok','mime_matches','embedded'}

    'uri' is the raw string as stored; 'path' is that percent-decoded, and is
    the one to resolve against the filesystem.
    """
    out = {'ok': False, 'version': '', 'generator': '', 'images': [],
           'buffers': [], 'counts': {}, 'error': None}
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
        return out
    try:
        a = d.get('asset', {})
        out['version'] = str(a.get('version', ''))
        out['generator'] = str(a.get('generator', ''))
        out['counts'] = {k: len(d.get(k, [])) for k in
                         ('meshes', 'materials', 'nodes', 'textures', 'images')}
        for b in d.get('buffers', []):
            uri = b.get('uri')
            emb = uri is None or str(uri).startswith('data:')
            out['buffers'].append({'uri': uri, 'embedded': emb,
                                   'path': '' if emb else unquote(str(uri))})
        for i, im in enumerate(d.get('images', [])):
            uri = im.get('uri')
            if uri is None or str(uri).startswith('data:'):
                out['images'].append({'index': i, 'uri': uri, 'path': '',
                                      'mimeType': im.get('mimeType', ''),
                                      'ext': '', 'spec_ok': True, 'mime_matches': True,
                                      'embedded': True})
                continue
            path = unquote(str(uri))
            ext = os.path.splitext(path)[1].lower()
            mt = im.get('mimeType', '')
            out['images'].append({
                'index': i, 'uri': uri, 'path': path, 'mimeType': mt, 'ext': ext,
                'spec_ok': ext in SPEC_OK,
                'mime_matches': (not mt) or MIME.get(ext) == mt,
                'embedded': False})
        out['ok'] = True
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def repoint_gltf(path, mapping, backup=True, fix_mime=True):
    """Rewrite image uris. mapping: {old: new}, keyed by the stored uri or by its
    decoded form; values are ordinary paths. Returns (changed, notes).

    The new value is percent-encoded on the way in, so a replacement containing a
    space is written as my%20texture.png and the file stays spec-conformant.

    Also corrects mimeType to match the new extension when fix_mime is set,
    otherwise a repoint from .png to .jpg leaves a mimeType that contradicts it.
    """
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    changed, notes = 0, []
    for im in d.get('images', []):
        uri = im.get('uri')
        # data: uris and bufferView images are embedded - there is no file to repoint
        if uri is None or str(uri).startswith('data:'):
            continue
        dec = unquote(str(uri))
        key = uri if uri in mapping else (dec if dec in mapping else None)
        if key is not None and mapping[key] not in (uri, dec):
            new = mapping[key]
            im['uri'] = quote(new, safe='/')
            changed += 1
            ext = os.path.splitext(new)[1].lower()
            if fix_mime and 'mimeType' in im and ext in MIME and im['mimeType'] != MIME[ext]:
                notes.append(f"mimeType {im['mimeType']} -> {MIME[ext]} for {new}")
                im['mimeType'] = MIME[ext]
            if ext not in SPEC_OK:
                notes.append(f'WARNING {new} is not PNG or JPEG; glTF does not allow it')
    if changed:
        if backup and not os.path.exists(path + '.bak'):
            shutil.copy2(path, path + '.bak')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=4)
        os.replace(tmp, path)
    return changed, notes


if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        i = read_gltf(p)
        print(f"\n{os.path.basename(p)}  glTF {i['version']}  {i['generator']}"
              + (f"  ERROR: {i['error']}" if i['error'] else ''))
        for b in i['buffers']:
            print(f"   buffer  {'(embedded)' if b['embedded'] else b['uri']}")
        for im in i['images']:
            flags = []
            if not im['spec_ok']: flags.append('NOT PNG/JPEG - invalid in glTF')
            if not im['mime_matches']: flags.append(f"mimeType says {im['mimeType']}")
            shown = im['uri'] if im['embedded'] or im['path'] == im['uri'] else \
                f"{im['uri']}  (= {im['path']})"
            print(f"   image {im['index']}  {shown}" + ('   << ' + '; '.join(flags) if flags else ''))
