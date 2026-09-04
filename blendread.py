#!/usr/bin/env python3
"""
blendread.py - read (and safely repoint) image paths inside a .blend, stdlib only.

Blender stores each image's path in a FIXED-SIZE char buffer inside the Image struct.
That means a replacement of the same length or shorter can be written straight back
in place: no block offsets move, no DNA changes, nothing to corrupt. Extension swaps
(.png -> .jpg) are exactly that case, which is the common Poly Haven / texture-pack
mismatch.

    from blendread import read_blend, repoint_blend
    info = read_blend('scene.blend')
    repoint_blend('scene.blend', {'//textures/a_2k.png': '//textures/a_2k.jpg'})
"""
import os, gzip, shutil, struct, tempfile

def _decompress(path):
    """Return (raw_bytes, kind). kind is 'raw', 'gzip', or 'zstd' (unsupported)."""
    with open(path, 'rb') as f: head = f.read(4)
    if head[:7] == b'BLENDER'[:4] and open(path,'rb').read(7) == b'BLENDER':
        with open(path,'rb') as fh:
            return fh.read(), 'raw'
    if head[:2] == b'\x1f\x8b':
        with gzip.open(path,'rb') as f: return f.read(), 'gzip'
    if head == b'\x28\xb5\x2f\xfd':
        try:
            import zstandard as zstd
            with open(path,'rb') as f:
                return zstd.ZstdDecompressor().stream_reader(f).read(), 'zstd'
        except ImportError:
            return None, 'zstd'
    with open(path,'rb') as fh:
        return fh.read(), 'raw'

class _Blend:
    """Minimal .blend reader: block table + DNA, enough to locate Image paths."""
    def __init__(self, data):
        self.d = data
        assert self.d[:7] == b'BLENDER', 'not a .blend'
        self.ptr = 8 if self.d[7:8] == b'-' else 4
        self.end = '<' if self.d[8:9] == b'v' else '>'
        self.blocks, p = [], 12
        while p < len(self.d):
            code = self.d[p:p+4]; p += 4
            size = struct.unpack(self.end+'I', self.d[p:p+4])[0]; p += 4
            p += self.ptr
            sdna, count = struct.unpack(self.end+'II', self.d[p:p+8]); p += 8
            self.blocks.append(dict(code=code, size=size, sdna=sdna, off=p))
            if code == b'ENDB': break
            p += size
        self._dna()
    def _dna(self):
        blk = [b for b in self.blocks if b['code'] == b'DNA1'][0]
        d, base = self.d, blk['off']; p = base
        al = lambda q: base + ((q - base + 3) & ~3)
        assert d[p:p+4] == b'SDNA'; p += 4
        assert d[p:p+4] == b'NAME'; p += 4
        n = struct.unpack(self.end+'I', d[p:p+4])[0]; p += 4
        names = []
        for _ in range(n):
            e = d.index(b'\0', p); names.append(d[p:e].decode()); p = e+1
        p = al(p); assert d[p:p+4] == b'TYPE'; p += 4
        n = struct.unpack(self.end+'I', d[p:p+4])[0]; p += 4
        types = []
        for _ in range(n):
            e = d.index(b'\0', p); types.append(d[p:e].decode()); p = e+1
        p = al(p); assert d[p:p+4] == b'TLEN'; p += 4
        tlen = list(struct.unpack(self.end+'%dH' % len(types), d[p:p+2*len(types)])); p += 2*len(types)
        p = al(p); assert d[p:p+4] == b'STRC'; p += 4
        ns = struct.unpack(self.end+'I', d[p:p+4])[0]; p += 4
        structs = []
        for _ in range(ns):
            t, nf = struct.unpack(self.end+'HH', d[p:p+4]); p += 4
            fs = struct.unpack(self.end+'%dH' % (nf*2), d[p:p+4*nf]); p += 4*nf
            structs.append((t, [(fs[i*2], fs[i*2+1]) for i in range(nf)]))
        self.names, self.types, self.tlen, self.structs = names, types, tlen, structs
        self.sname = {types[s[0]]: i for i, s in enumerate(structs)}
    def _fsize(self, name, tidx):
        base = self.ptr if name.startswith('*') else self.tlen[tidx]
        n, s = 1, name
        while '[' in s:
            i, j = s.index('['), s.index(']')
            n *= int(s[i+1:j]); s = s[:i] + s[j+1:]
        return base * n
    def field(self, struct_name, field_name):
        """Return (offset, byte_size) of a field inside a struct."""
        si = self.sname[struct_name]; off = 0
        for ti, ni in self.structs[si][1]:
            nm = self.names[ni]; sz = self._fsize(nm, ti)
            if nm.lstrip('*').split('[')[0] == field_name: return off, sz
            off += sz
        raise KeyError(field_name)

def read_blend(path):
    """-> {'ok', 'compression', 'version', 'images':[{'name','path','offset','cap'}], 'error'}"""
    out = {'ok': False, 'compression': '?', 'version': '', 'images': [], 'error': None}
    try:
        data, kind = _decompress(path)
        out['compression'] = kind
        if data is None:
            out['error'] = ('this .blend is zstd-compressed (Blender 3.0+). '
                            'Re-save it from Blender with compression off, or '
                            'pip install zstandard')
            return out
        b = _Blend(data)
        out['version'] = data[9:12].decode('ascii', 'replace')
        noff, ncap = b.field('Image', 'name')
        idoff, idcap = b.field('ID', 'name')
        for blk in b.blocks:
            if blk['code'] != b'IM\0\0': continue
            def rd(o, cap):
                raw = data[o:o+cap]; z = raw.find(b'\0')
                return raw[:z if z >= 0 else cap].decode('utf8', 'replace')
            out['images'].append({'name': rd(blk['off']+idoff, idcap)[2:],
                                  'path': rd(blk['off']+noff, ncap),
                                  'offset': blk['off']+noff, 'cap': ncap})
        out['ok'] = True
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out

def repoint_blend(path, mapping, backup=True):
    """Rewrite image paths in place. Only same-length-or-shorter values are written,
    so nothing in the file moves. Returns (changed, skipped_too_long)."""
    data, kind = _decompress(path)
    if data is None: raise RuntimeError('zstd-compressed .blend; cannot edit')
    data = bytearray(data)
    info = read_blend(path)
    changed, toolong = 0, []
    for im in info['images']:
        new = mapping.get(im['path'])
        if not new or new == im['path']: continue
        enc = new.encode('utf8')
        if len(enc) + 1 > im['cap']:
            toolong.append(new); continue
        data[im['offset']:im['offset']+im['cap']] = enc + b'\0' * (im['cap'] - len(enc))
        changed += 1
    if changed:
        if backup and not os.path.exists(path + '.bak'):
            shutil.copy2(path, path + '.bak')
        tmp = path + '.tmp'
        if kind == 'gzip':
            with gzip.open(tmp, 'wb') as f: f.write(bytes(data))
        else:
            with open(tmp, 'wb') as fh:
                fh.write(bytes(data))
        os.replace(tmp, path)
    return changed, toolong

if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        i = read_blend(p)
        print(f"\n{os.path.basename(p)}  [{i['compression']} v{i['version']}]"
              + (f"  ERROR: {i['error']}" if i['error'] else ''))
        for im in i['images'][:200]:
            print(f"   {im['name'][:38]:38s} {im['path']}")
        print(f"   ({len(i['images'])} image datablocks)")
