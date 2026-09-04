#!/usr/bin/env python3
"""
test_modelmap.py - tests for the Model-Map toolkit.

    python3 test_modelmap.py            # run everything
    python3 -m unittest test_modelmap   # same thing

Standard library only, and every fixture is BUILT BY THE TEST. No sample models,
no textures, nothing checked in — which matters because .gitignore deliberately
excludes every model and image format from the repo.

The important tests are the ones covering in-place writes: blendread rewrites
paths inside a real .blend, and if it gets the buffer arithmetic wrong it
corrupts the file. Those tests assert the file's structure is untouched, not
just that the paths look right.
"""
import io, os, gzip, json, shutil, struct, sys, tempfile, unittest, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texcheck, fbxread, blendread


def _read(path, mode='r'):
    with open(path, mode) as f:
        return f.read()


# --------------------------------------------------------------- fixtures
def tiny_png(path, w=4, h=4, rgb=(120, 160, 60)):
    """Smallest valid PNG, written with zlib+struct so no Pillow is needed."""
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
           + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    with open(path, 'wb') as f: f.write(png)
    return path


def make_blend(path, paths, version=b'293', compress=False, cap=1024):
    """Build a minimal but structurally valid .blend containing Image datablocks.

    Real Blender files are far richer, but blendread only needs: the 12-byte
    header, a walkable block table, a DNA1 block describing ID and Image, and
    IM blocks. Crucially the Image.name field is a FIXED-SIZE buffer here exactly
    as it is in Blender, which is the property the repointer depends on.
    """
    ID_LEN = 66
    types = ['char', 'short', 'int', 'ID', 'Image']
    tlen  = [1, 2, 4, ID_LEN, ID_LEN + cap]
    names = ['name[%d]' % ID_LEN, 'id', 'name[%d]' % cap]
    # struct 0 = ID {char name[66]}; struct 1 = Image {ID id; char name[cap]}
    structs = [(3, [(0, 0)]), (4, [(3, 1), (0, 2)])]

    def dna_block():
        b = bytearray(b'SDNA')
        b += b'NAME' + struct.pack('<I', len(names))
        for n in names: b += n.encode() + b'\0'
        while len(b) % 4: b += b'\0'
        b += b'TYPE' + struct.pack('<I', len(types))
        for t in types: b += t.encode() + b'\0'
        while len(b) % 4: b += b'\0'
        b += b'TLEN' + struct.pack('<%dH' % len(tlen), *tlen)
        while len(b) % 4: b += b'\0'
        b += b'STRC' + struct.pack('<I', len(structs))
        for t, fs in structs:
            b += struct.pack('<HH', t, len(fs))
            for ti, ni in fs: b += struct.pack('<HH', ti, ni)
        return bytes(b)

    out = bytearray(b'BLENDER-v' + version)
    def block(code, sdna, payload):
        out.extend(code + struct.pack('<I', len(payload)) + struct.pack('<Q', 0)
                   + struct.pack('<II', sdna, 1) + payload)
    for i, p in enumerate(paths):
        nm = ('IMimg%03d' % i).encode()
        payload = nm + b'\0' * (ID_LEN - len(nm))
        enc = p.encode()
        payload += enc + b'\0' * (cap - len(enc))
        block(b'IM\0\0', 1, payload)
    block(b'DNA1', 0, dna_block())
    block(b'ENDB', 0, b'')
    data = bytes(out)
    if compress:
        with gzip.open(path, 'wb') as f: f.write(data)
    else:
        with open(path, 'wb') as f: f.write(data)
    return path


def make_fbx(path, version=7400, textures=()):
    """Build a minimal binary FBX with Material / Texture / Video nodes and the
    connections between them. textures: [(texname, filename, slot, matname)]."""
    def prop_s(v): return b'S' + struct.pack('<I', len(v)) + v
    def prop_l(v): return b'L' + struct.pack('<q', v)

    def node(name, props=b'', children=b''):
        """<7500 header: EndOffset u32, NumProps u32, PropListLen u32, NameLen u8."""
        nprops = 0; i = 0
        while i < len(props):
            t = chr(props[i]); nprops += 1
            if t in 'SR':
                n = struct.unpack('<I', props[i+1:i+5])[0]; i += 5 + n
            elif t == 'L': i += 9
            else: raise ValueError(t)
        body = props + children + (b'\0' * 13 if children else b'')
        hdr_len = 13 + len(name)
        end = None
        # EndOffset is absolute; patched by the caller via _fix
        return (hdr_len + len(body), name, props, len(props), nprops, body)

    def emit(nodes, start):
        """Serialise a list of node tuples, resolving absolute EndOffsets."""
        buf = bytearray(); pos = start
        for size, name, props, plen, nprops, body in nodes:
            end = pos + size
            buf += struct.pack('<III', end, nprops, plen) + bytes([len(name)]) + name + body
            pos = end
        return bytes(buf)

    mats, texs, vids, conns = {}, {}, {}, []
    nid = 1000
    for tname, fname, slot, mat in textures:
        if mat not in mats: nid += 1; mats[mat] = nid
        nid += 1; texs[tname] = nid
        nid += 1; vids[tname] = nid
        conns.append((b'OP', texs[tname], mats[mat], slot.encode()))
        conns.append((b'OO', vids[tname], texs[tname], None))

    objs = []
    for m, oid in mats.items():
        objs.append(node(b'Material', prop_l(oid) + prop_s(m.encode() + b'\x00\x01Material')))
    for tname, oid in texs.items():
        fn = [t for t in textures if t[0] == tname][0][1]
        kids = emit([node(b'RelativeFilename', prop_s(fn.encode()))], 0)
        # children need absolute offsets, so rebuild with a correct base below
        objs.append(('TEX', oid, tname, fn))
    for tname, oid in vids.items():
        fn = [t for t in textures if t[0] == tname][0][1]
        objs.append(('VID', oid, tname, fn))

    # Build Objects node content with correct absolute offsets by two passes.
    def build_objects(base):
        items = []
        for m, oid in mats.items():
            items.append(node(b'Material', prop_l(oid) + prop_s(m.encode() + b'\x00\x01Material')))
        for tname, oid in texs.items():
            fn = [t for t in textures if t[0] == tname][0][1]
            child = node(b'RelativeFilename', prop_s(fn.encode()))
            inner = emit([child], 0)  # placeholder, fixed in second pass
            items.append((None, b'Texture',
                          prop_l(oid) + prop_s(tname.encode() + b'\x00\x01Texture'), fn))
        for tname, oid in vids.items():
            fn = [t for t in textures if t[0] == tname][0][1]
            items.append((None, b'Video',
                          prop_l(oid) + prop_s(tname.encode() + b'\x00\x01Video'), fn))
        buf = bytearray(); pos = base
        for it in items:
            if it[0] is not None:
                size, name, props, plen, nprops, body = it
                end = pos + size
                buf += struct.pack('<III', end, nprops, plen) + bytes([len(name)]) + name + body
                pos = end
            else:
                _, name, props, fn = it
                cprops = prop_s(fn.encode())
                cname = b'RelativeFilename'
                csize = 13 + len(cname) + len(cprops)
                nprops = props.count(b'L'[0:1]) if False else 2
                hdr = 13 + len(name)
                size = hdr + len(props) + csize + 13
                end = pos + size
                buf += struct.pack('<III', end, 2, len(props)) + bytes([len(name)]) + name + props
                cend = pos + hdr + len(props) + csize
                buf += struct.pack('<III', cend, 1, len(cprops)) + bytes([len(cname)]) + cname + cprops
                buf += b'\0' * 13
                pos = end
        return bytes(buf)

    def build_conns(base):
        buf = bytearray(); pos = base
        for kind, a, b_, prop in conns:
            props = prop_s(kind) + prop_l(a) + prop_l(b_) + (prop_s(prop) if prop else b'')
            n = 4 if prop else 3
            name = b'C'
            size = 13 + len(name) + len(props)
            end = pos + size
            buf += struct.pack('<III', end, n, len(props)) + bytes([len(name)]) + name + props
            pos = end
        return bytes(buf)

    head = b'Kaydara FBX Binary  \x00' + b'\x1a\x00' + struct.pack('<I', version)
    # Objects wrapper
    inner_objs = build_objects(0)
    obj_hdr = 13 + len(b'Objects')
    objs_start = len(head)
    inner_objs = build_objects(objs_start + obj_hdr)
    objs_size = obj_hdr + len(inner_objs) + 13
    objs_node = (struct.pack('<III', objs_start + objs_size, 0, 0) + bytes([7]) + b'Objects'
                 + inner_objs + b'\0' * 13)
    conn_start = objs_start + objs_size
    conn_hdr = 13 + len(b'Connections')
    inner_conns = build_conns(conn_start + conn_hdr)
    conns_size = conn_hdr + len(inner_conns) + 13
    conns_node = (struct.pack('<III', conn_start + conns_size, 0, 0) + bytes([11]) + b'Connections'
                  + inner_conns + b'\0' * 13)
    with open(path, 'wb') as f:
        f.write(head + objs_node + conns_node + b'\0' * 13)
    return path


# --------------------------------------------------------------- OBJ / MTL
class TestObjMtl(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'Textures'))
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, mtl_body, mtllib='m.mtl', objname='m.obj'):
        with open(os.path.join(self.d, objname), 'w') as f:
            f.write(f'mtllib {mtllib}\nusemtl A\nf 1 2 3\n')
        with open(os.path.join(self.d, 'm.mtl'), 'w') as f:
            f.write(mtl_body)

    def test_dead_absolute_path_is_relinked(self):
        tiny_png(os.path.join(self.d, 'Textures', 'wood.png'))
        self.write('newmtl A\nmap_Kd N:\\Some Author\\proj\\wood.png\n')
        idx = texcheck.build_index([self.d])
        issues, changed = texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertEqual(changed, 1)
        body = _read(os.path.join(self.d, 'm.mtl'))
        self.assertIn('Textures/wood.png', body)
        self.assertNotIn('N:\\', body)

    def test_backup_is_created_before_writing(self):
        tiny_png(os.path.join(self.d, 'Textures', 'wood.png'))
        self.write('newmtl A\nmap_Kd C:\\gone\\wood.png\n')
        idx = texcheck.build_index([self.d])
        texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertTrue(os.path.exists(os.path.join(self.d, 'm.mtl.bak')))
        self.assertIn('C:\\gone', _read(os.path.join(self.d, 'm.mtl.bak')))

    def test_extension_fallback(self):
        """A .png reference resolving to a shipped .jpg - the Poly Haven case."""
        tiny_png(os.path.join(self.d, 'Textures', 'cover_2k.jpg'))
        self.write('newmtl A\nmap_Kd textures/cover_2k.png\n')
        idx = texcheck.build_index([self.d])
        issues, changed = texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertEqual(changed, 1)
        self.assertIn('cover_2k.jpg', _read(os.path.join(self.d, 'm.mtl')))

    def test_map_options_survive_rewrite(self):
        """-bm 0.3000 carries the authored bump strength; losing it changes the look."""
        tiny_png(os.path.join(self.d, 'Textures', 'n.png'))
        self.write('newmtl A\nmap_bump -bm 0.3000 D:\\dead\\n.png\n')
        idx = texcheck.build_index([self.d])
        texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        body = _read(os.path.join(self.d, 'm.mtl'))
        self.assertIn('-bm 0.3000', body)
        self.assertIn('Textures/n.png', body)

    def test_filename_with_spaces_is_parsed(self):
        tiny_png(os.path.join(self.d, 'Textures', 'my wood.png'))
        self.write('newmtl A\nmap_Kd N:\\x\\my wood.png\n')
        idx = texcheck.build_index([self.d])
        issues, changed = texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertEqual(changed, 1)
        self.assertIn('my wood.png', _read(os.path.join(self.d, 'm.mtl')))

    def test_kd_darkening_is_flagged(self):
        self.write('newmtl A\nKd 0.083532 0.060475 0.037071\nmap_Kd x.png\n')
        idx = texcheck.build_index([self.d])
        issues, _ = texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx)
        kd = [i for i in issues if i[1] == 'Kd']
        self.assertEqual(len(kd), 1)
        self.assertIn('92%', kd[0][3])

    def test_missing_texture_reported_not_invented(self):
        self.write('newmtl A\nmap_Kd N:\\nope\\absent.png\n')
        idx = texcheck.build_index([self.d])
        issues, changed = texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertEqual(changed, 0)
        self.assertEqual(issues[0][3], 'MISSING')

    def test_mtllib_with_spaces_is_found(self):
        with open(os.path.join(self.d, 'Chair 2.mtl'), 'w') as f: f.write('newmtl A\n')
        with open(os.path.join(self.d, 'Chair 2.obj'), 'w') as f: f.write('mtllib Chair 2.mtl\n')
        found = texcheck.find_mtls(os.path.join(self.d, 'Chair 2.obj'))
        named = [n for n, p, e in found if e and n == 'Chair 2.mtl']
        self.assertEqual(len(named), 1, 'the whole remainder should be tried as one name')

    def test_split_map_line(self):
        self.assertEqual(texcheck.split_map_line('-bm 0.3 C:\\a b\\t.png'),
                         ('-bm 0.3', 'C:\\a b\\t.png'))
        self.assertEqual(texcheck.split_map_line('plain.png'), ('', 'plain.png'))
        self.assertEqual(texcheck.split_map_line('-s 1 1 1 t.png'), ('-s 1 1 1', 't.png'))


# --------------------------------------------------------------- .blend
class TestBlend(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'textures'))
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_reads_paths_uncompressed(self):
        p = make_blend(os.path.join(self.d, 'a.blend'),
                       ['//textures/one.png', '//textures/two.png'])
        info = blendread.read_blend(p)
        self.assertTrue(info['ok'])
        self.assertEqual(info['compression'], 'raw')
        self.assertEqual([i['path'] for i in info['images']],
                         ['//textures/one.png', '//textures/two.png'])

    def test_reads_paths_gzipped(self):
        p = make_blend(os.path.join(self.d, 'g.blend'), ['//textures/one.png'], compress=True)
        info = blendread.read_blend(p)
        self.assertEqual(info['compression'], 'gzip')
        self.assertEqual(info['images'][0]['path'], '//textures/one.png')

    def test_zstd_is_reported_not_crashed(self):
        p = os.path.join(self.d, 'z.blend')
        with open(p, 'wb') as f:
            f.write(b'\x28\xb5\x2f\xfd' + b'\0' * 64)
        info = blendread.read_blend(p)
        try:
            import zstandard  # noqa: F401
        except ImportError:
            self.assertFalse(info['ok'])
            self.assertIn('zstd', info['error'])

    def test_repoint_roundtrip_and_structure_preserved(self):
        """The one that matters: an in-place edit must not disturb the file."""
        paths = ['//textures/c%02d_2k.png' % i for i in range(12)]
        p = make_blend(os.path.join(self.d, 'r.blend'), paths)
        before = _read(p, 'rb')
        mapping = {q: q.replace('.png', '.jpg') for q in paths}
        changed, toolong = blendread.repoint_blend(p, mapping)
        after = _read(p, 'rb')

        self.assertEqual(changed, 12)
        self.assertEqual(toolong, [])
        self.assertEqual(len(before), len(after), 'file size must not change')
        self.assertEqual([i['path'] for i in blendread.read_blend(p)['images']],
                         list(mapping.values()))
        # structure: same block count, still ends with ENDB, DNA1 intact
        b1 = blendread._Blend(before).blocks
        b2 = blendread._Blend(after).blocks
        self.assertEqual(len(b1), len(b2))
        self.assertEqual(b2[-1]['code'], b'ENDB')
        self.assertTrue(any(x['code'] == b'DNA1' for x in b2))
        self.assertEqual([x['off'] for x in b1], [x['off'] for x in b2],
                         'no block may move')
        # only bytes inside name buffers changed
        diff = sum(1 for x, y in zip(before, after) if x != y)
        self.assertGreater(diff, 0)
        self.assertLess(diff, len(before) * 0.05)

    def test_repoint_gzip_roundtrip(self):
        p = make_blend(os.path.join(self.d, 'gz.blend'), ['//textures/a.png'], compress=True)
        changed, _ = blendread.repoint_blend(p, {'//textures/a.png': '//textures/a.jpg'})
        self.assertEqual(changed, 1)
        info = blendread.read_blend(p)
        self.assertEqual(info['compression'], 'gzip', 'must stay compressed')
        self.assertEqual(info['images'][0]['path'], '//textures/a.jpg')

    def test_backup_created(self):
        p = make_blend(os.path.join(self.d, 'b.blend'), ['//t/a.png'])
        blendread.repoint_blend(p, {'//t/a.png': '//t/a.jpg'})
        self.assertTrue(os.path.exists(p + '.bak'))

    def test_too_long_replacement_is_refused(self):
        """Longer than the fixed buffer must be skipped, never truncated or overflowed."""
        p = make_blend(os.path.join(self.d, 't.blend'), ['//a.png'], cap=16)
        long = '//' + 'x' * 40 + '.jpg'
        changed, toolong = blendread.repoint_blend(p, {'//a.png': long})
        self.assertEqual(changed, 0)
        self.assertEqual(toolong, [long])
        self.assertEqual(blendread.read_blend(p)['images'][0]['path'], '//a.png')

    def test_unmapped_paths_untouched(self):
        p = make_blend(os.path.join(self.d, 'u.blend'), ['//a.png', '//b.png'])
        blendread.repoint_blend(p, {'//a.png': '//a.jpg'})
        self.assertEqual([i['path'] for i in blendread.read_blend(p)['images']],
                         ['//a.jpg', '//b.png'])


# --------------------------------------------------------------- FBX
class TestFbx(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)

    def test_binary_texture_slots_and_materials(self):
        p = make_fbx(os.path.join(self.d, 'a.fbx'), 7400, [
            ('Map1', 'C:\\proj\\albedo.png', 'DiffuseColor', 'ChairMat'),
            ('Map2', 'C:\\proj\\rough.png', 'SpecularColor', 'ChairMat'),
        ])
        info = fbxread.read_fbx(p)
        self.assertIsNone(info['error'])
        self.assertEqual(info['format'], 'binary')
        self.assertIn('ChairMat', info['materials'])
        slots = {t['slot']: t['file'] for t in info['textures']}
        self.assertEqual(slots.get('DiffuseColor'), 'C:\\proj\\albedo.png')
        self.assertEqual(slots.get('SpecularColor'), 'C:\\proj\\rough.png')

    def test_no_textures_is_reported_not_an_error(self):
        """An FBX with materials but zero texture nodes is valid - and real."""
        p = make_fbx(os.path.join(self.d, 'b.fbx'), 7400, [])
        info = fbxread.read_fbx(p)
        self.assertIsNone(info['error'])
        self.assertEqual(info['textures'], [])

    def test_ascii_fbx(self):
        p = os.path.join(self.d, 'c.fbx')
        with open(p, 'w') as f: f.write('''; FBX 7.4.0 project file
FBXVersion: 7400
Texture: 1234, "Texture::Map1", "" {
    FileName: "C:\\\\proj\\\\albedo.png"
    RelativeFilename: "textures/albedo.png"
}
''')
        info = fbxread.read_fbx(p)
        self.assertEqual(info['format'], 'ascii')
        self.assertEqual(len(info['textures']), 1)
        self.assertEqual(info['textures'][0]['file'], 'textures/albedo.png')

    def test_corrupt_file_returns_error_not_exception(self):
        p = os.path.join(self.d, 'bad.fbx')
        with open(p, 'wb') as f:
            f.write(b'Kaydara FBX Binary  \x00\x1a\x00' + struct.pack('<I', 7400) + b'\xff' * 40)
        info = fbxread.read_fbx(p)          # must not raise
        self.assertIsInstance(info, dict)


# --------------------------------------------------------------- wiring
class TestModuleWiring(unittest.TestCase):
    def test_all_four_modules_import(self):
        """The check that would have caught fbxread/blendread missing from the repo."""
        import importlib
        for m in ('texcheck', 'texstudio', 'fbxread', 'blendread'):
            self.assertTrue(importlib.import_module(m))

    def test_texstudio_has_no_third_party_imports(self):
        import texstudio
        src = _read(texstudio.__file__)
        for bad in ('import numpy', 'import PIL', 'from PIL', 'import requests'):
            self.assertNotIn(bad, src, 'must stay standard-library only')

    def test_server_binds_localhost_only(self):
        src = _read(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'texstudio.py'))
        self.assertIn("'127.0.0.1'", src)
        self.assertNotIn("'0.0.0.0'", src, 'must never bind to all interfaces')

    def test_upload_path_traversal_is_blocked(self):
        import texstudio
        texstudio.ROOT = tempfile.mkdtemp()
        try:
            with self.assertRaises(ValueError):
                texstudio.safe('../../etc/passwd')
            with self.assertRaises(ValueError):
                texstudio.safe('a/../../../outside.txt')
            inside = texstudio.safe('sub/ok.png')
            self.assertTrue(inside.startswith(os.path.abspath(texstudio.ROOT)))
        finally:
            shutil.rmtree(texstudio.ROOT, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
