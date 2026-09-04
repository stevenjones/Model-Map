#!/usr/bin/env python3
"""
fbxread.py - read texture references out of an FBX (binary or ASCII), stdlib only.

    from fbxread import read_fbx
    info = read_fbx('model.fbx')

Returns {'format': 'binary'|'ascii', 'version': int, 'textures': [...], 'materials': [...]}
Each texture: {'node', 'file', 'relative', 'slot', 'material'}
"""
import os, re, struct, zlib

# ---------------------------------------------------------------- binary FBX
def _rd_prop(d, p):
    t = chr(d[p]); p += 1
    if t in 'CB': return t, d[p], p+1
    if t == 'Y': return t, struct.unpack('<h', d[p:p+2])[0], p+2
    if t == 'I': return t, struct.unpack('<i', d[p:p+4])[0], p+4
    if t == 'F': return t, struct.unpack('<f', d[p:p+4])[0], p+4
    if t == 'D': return t, struct.unpack('<d', d[p:p+8])[0], p+8
    if t == 'L': return t, struct.unpack('<q', d[p:p+8])[0], p+8
    if t in 'SR':
        n = struct.unpack('<I', d[p:p+4])[0]; p += 4
        return t, d[p:p+n], p+n
    if t in 'fdlib':
        cnt, enc, cl = struct.unpack('<III', d[p:p+12]); p += 12
        raw = d[p:p+cl]; p += cl
        if enc == 1:
            try: raw = zlib.decompress(raw)
            except Exception: raw = b''
        return t, None, p          # array payloads are not needed here
    if t == 'c':
        cnt, enc, cl = struct.unpack('<III', d[p:p+12]); p += 12
        return t, None, p + cl
    raise ValueError(f'unknown property type {t!r}')

def _walk_binary(d):
    ver = struct.unpack('<I', d[23:27])[0]
    nodes, stack = [], []
    def walk(pos, end, depth):
        while pos < end:
            if ver >= 7500:
                if pos + 25 > len(d): return
                eo, npr, pl = struct.unpack('<QQQ', d[pos:pos+24]); pos += 24
                nl = d[pos]; pos += 1
            else:
                if pos + 13 > len(d): return
                eo, npr, pl = struct.unpack('<III', d[pos:pos+12]); pos += 12
                nl = d[pos]; pos += 1
            if eo == 0: return
            name = d[pos:pos+nl].decode('utf8', 'replace'); pos += nl
            pp, vals = pos, []
            for _ in range(npr):
                try: t, v, pp = _rd_prop(d, pp)
                except Exception: break
                vals.append((t, v))
            nodes.append((depth, name, vals))
            hdr = 25 if ver >= 7500 else 13
            if pp < eo - hdr:
                walk(pp, eo - hdr, depth + 1)
            pos = eo
    walk(27, len(d), 0)
    return ver, nodes

def _read_binary(path):
    with open(path, 'rb') as fh:
        d = fh.read()
    ver, nodes = _walk_binary(d)
    objs, conns, files, cur = {}, [], {}, None
    for depth, name, vals in nodes:
        if name in ('Texture', 'Video', 'Material', 'Model'):
            ids = [v for t, v in vals if t == 'L']
            nms = [v for t, v in vals if t == 'S']
            oid = ids[0] if ids else None
            nm = nms[0].decode('utf8', 'replace').split('\x00')[0] if nms else '?'
            objs[oid] = (name, nm); cur = oid
        elif cur is not None and name in ('RelativeFilename', 'FileName'):
            for t, v in vals:
                if t == 'S':
                    s = v.decode('utf8', 'replace').split('\x00')[0]
                    if s: files.setdefault(cur, {})[name] = s
        elif name == 'C':
            conns.append([v for t, v in vals])
    return ver, objs, conns, files

# ---------------------------------------------------------------- ascii FBX
def _read_ascii(path):
    with open(path, errors='replace') as fh:
        txt = fh.read()
    ver = 0
    m = re.search(r'FBXVersion:\s*(\d+)', txt)
    if m: ver = int(m.group(1))
    files = {}
    # Texture / Video blocks carry FileName and RelativeFilename entries
    for i, m in enumerate(re.finditer(
            r'(?:Texture|Video):\s*\d*\s*,?\s*"([^"]*)"[^{]*\{(.*?)\n\}', txt, re.S)):
        nm, body = m.group(1), m.group(2)
        fn = re.search(r'FileName:\s*"([^"]*)"', body)
        rel = re.search(r'RelativeFilename:\s*"([^"]*)"', body)
        if fn or rel:
            files[('ascii', i)] = {'name': nm.split('\x00')[0],
                                   'FileName': fn.group(1) if fn else '',
                                   'RelativeFilename': rel.group(1) if rel else ''}
    return ver, files, txt

# ---------------------------------------------------------------- public
def read_fbx(path):
    with open(path, 'rb') as fh:
        head = fh.read(32)
    is_bin = head.startswith(b'Kaydara FBX Binary')
    out = {'format': 'binary' if is_bin else 'ascii', 'version': 0,
           'textures': [], 'materials': [], 'error': None}
    try:
        if is_bin:
            ver, objs, conns, files = _read_binary(path)
            out['version'] = ver
            # texture -> material slot, and video -> texture
            tex_slot, vid_of_tex = {}, {}
            for c in conns:
                if len(c) < 3: continue
                ch, pa = objs.get(c[1]), objs.get(c[2])
                prop = c[3].decode('utf8','replace') if len(c) > 3 and isinstance(c[3], bytes) else ''
                if ch and pa and ch[0] == 'Texture' and pa[0] == 'Material':
                    tex_slot[c[1]] = (prop.split('|')[-1], pa[1])
                if ch and pa and ch[0] == 'Video' and pa[0] == 'Texture':
                    vid_of_tex[c[2]] = c[1]
            out['materials'] = sorted({n for k, (t, n) in objs.items() if t == 'Material'})
            seen = set()
            # Texture nodes carry the slot + material; Video nodes are the raw file.
            # Emit Textures first, then only those Videos whose file nothing else covers.
            order = ([o for o in objs if objs[o][0] == 'Texture'] +
                     [o for o in objs if objs[o][0] == 'Video'])
            covered = set()
            for oid in order:
                typ, nm = objs[oid]
                f = files.get(oid, {})
                if typ == 'Texture' and oid in vid_of_tex:
                    f = files.get(vid_of_tex[oid], f) or f
                path_s = f.get('RelativeFilename') or f.get('FileName') or ''
                if not path_s: continue
                slot, mat = tex_slot.get(oid, ('', ''))
                if typ == 'Video' and path_s in covered: continue
                key = (path_s, slot, mat)
                if key in seen: continue
                seen.add(key); covered.add(path_s)
                out['textures'].append({'node': nm, 'file': path_s,
                                        'relative': f.get('RelativeFilename', ''),
                                        'absolute': f.get('FileName', ''),
                                        'slot': slot, 'material': mat})
        else:
            ver, files, _ = _read_ascii(path)
            out['version'] = ver
            for k, f in files.items():
                p = f.get('RelativeFilename') or f.get('FileName') or ''
                if p:
                    out['textures'].append({'node': f['name'], 'file': p,
                                            'relative': f.get('RelativeFilename',''),
                                            'absolute': f.get('FileName',''),
                                            'slot': '', 'material': ''})
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out

if __name__ == '__main__':
    import sys, json
    for p in sys.argv[1:]:
        info = read_fbx(p)
        print(f"\n{os.path.basename(p)}  [{info['format']} v{info['version']}]"
              + (f"  ERROR {info['error']}" if info['error'] else ''))
        if info['materials']: print("  materials:", ', '.join(info['materials'][:8]))
        for t in info['textures']:
            print(f"   {t['slot'] or '(slot?)':22s} {t['material'] or '-':16s} {t['file']}")
        if not info['textures']: print("   no texture references")
