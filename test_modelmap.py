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
import gc, io, os, gzip, json, shutil, struct, sys, tempfile, unittest, warnings, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texcheck, fbxread, blendread, gltfread


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

    def test_relative_subfolder_path_is_not_a_false_positive(self):
        """An FBX storing textures\\foo.png with the file really there is CORRECT.
        It must report OK, not FIX - the earlier version only looked beside the
        FBX and flagged working paths as broken."""
        os.makedirs(os.path.join(self.d, 'textures'), exist_ok=True)
        tiny_png(os.path.join(self.d, 'textures', 'foo.png'))
        p = make_fbx(os.path.join(self.d, 'r.fbx'), 7400,
                     [('M1', 'textures\\foo.png', 'DiffuseColor', 'Mat')])
        idx = texcheck.build_index([self.d])
        import io as _io, contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            missing = texcheck.audit_fbx(p, idx, fix=False)
        out = buf.getvalue()
        self.assertEqual(missing, 0)
        self.assertIn('[OK  ]', out)
        self.assertNotIn('[FIX ]', out)

    def test_reflectionfactor_flagged_as_wrong_slot(self):
        self.assertEqual(texcheck.FBX_SLOT_FIX.get('reflectionfactor'), 'metalness')
        self.assertEqual(texcheck.FBX_SLOT_FIX.get('shininessexponent'), 'roughness')

    def test_corrupt_file_returns_error_not_exception(self):
        p = os.path.join(self.d, 'bad.fbx')
        with open(p, 'wb') as f:
            f.write(b'Kaydara FBX Binary  \x00\x1a\x00' + struct.pack('<I', 7400) + b'\xff' * 40)
        info = fbxread.read_fbx(p)          # must not raise
        self.assertIsInstance(info, dict)


# --------------------------------------------------------------- wiring
class TestGltf(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'textures'))
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, images, buffers=None):
        doc = {'asset': {'version': '2.0', 'generator': 'test'},
               'meshes': [{}], 'materials': [{}, {}],
               'buffers': buffers if buffers is not None else [{'uri': 'm.bin'}],
               'images': images}
        p = os.path.join(self.d, 'm.gltf')
        with open(p, 'w') as f: json.dump(doc, f)
        return p

    def test_reads_uris_and_counts(self):
        p = self.write([{'uri': 'textures/a.jpg', 'mimeType': 'image/jpeg'}])
        i = gltfread.read_gltf(p)
        self.assertTrue(i['ok'])
        self.assertEqual(i['version'], '2.0')
        self.assertEqual(i['images'][0]['uri'], 'textures/a.jpg')
        self.assertEqual(i['counts']['materials'], 2)

    def test_exr_flagged_as_invalid_for_gltf(self):
        """glTF permits PNG and JPEG only - an .exr parses but no viewer will load it."""
        p = self.write([{'uri': 'textures/n.exr'}, {'uri': 'textures/ok.png'}])
        i = gltfread.read_gltf(p)
        self.assertFalse(i['images'][0]['spec_ok'])
        self.assertTrue(i['images'][1]['spec_ok'])

    def test_mimetype_mismatch_detected(self):
        p = self.write([{'uri': 'textures/a.png', 'mimeType': 'image/jpeg'}])
        i = gltfread.read_gltf(p)
        self.assertFalse(i['images'][0]['mime_matches'])

    def test_embedded_data_uri_not_treated_as_a_file(self):
        p = self.write([{'uri': 'data:image/png;base64,iVBORw0KGgo=', 'mimeType': 'image/png'}])
        i = gltfread.read_gltf(p)
        self.assertTrue(i['images'][0]['embedded'])
        self.assertTrue(i['images'][0]['spec_ok'])

    def test_glb_style_bufferview_image_is_embedded(self):
        p = self.write([{'bufferView': 0, 'mimeType': 'image/png'}])
        i = gltfread.read_gltf(p)
        self.assertTrue(i['images'][0]['embedded'])

    def test_repoint_rewrites_uri_and_fixes_mimetype(self):
        p = self.write([{'uri': 'textures/a.png', 'mimeType': 'image/png'}])
        ch, notes = gltfread.repoint_gltf(p, {'textures/a.png': 'textures/a.jpg'})
        self.assertEqual(ch, 1)
        i = gltfread.read_gltf(p)
        self.assertEqual(i['images'][0]['uri'], 'textures/a.jpg')
        self.assertEqual(i['images'][0]['mimeType'], 'image/jpeg',
                         'mimeType must follow the new extension')
        self.assertTrue(i['images'][0]['mime_matches'])

    def test_repoint_keeps_backup_and_valid_json(self):
        p = self.write([{'uri': 'a.png'}])
        gltfread.repoint_gltf(p, {'a.png': 'b.png'})
        self.assertTrue(os.path.exists(p + '.bak'))
        with open(p) as f: json.load(f)          # must still parse

    def test_repoint_preserves_everything_else(self):
        """Only image uris may change - meshes, buffers and materials stay put."""
        p = self.write([{'uri': 'a.png'}])
        with open(p) as f: before = json.load(f)
        gltfread.repoint_gltf(p, {'a.png': 'b.png'})
        with open(p) as f: after = json.load(f)
        self.assertEqual(before['meshes'], after['meshes'])
        self.assertEqual(before['buffers'], after['buffers'])
        self.assertEqual(before['materials'], after['materials'])
        self.assertEqual(before['asset'], after['asset'])

    def test_repoint_to_non_spec_format_warns(self):
        p = self.write([{'uri': 'a.png'}])
        ch, notes = gltfread.repoint_gltf(p, {'a.png': 'a.exr'})
        self.assertEqual(ch, 1)
        self.assertTrue(any('does not allow' in n for n in notes))

    def test_percent_encoded_uri_is_decoded_for_resolution(self):
        """glTF uris are percent-encoded, so my%20texture.png names 'my texture.png'.
        Without decoding, every texture with a space in its name looks missing."""
        p = self.write([{'uri': 'textures/my%20texture.png'}])
        i = gltfread.read_gltf(p)
        self.assertEqual(i['images'][0]['uri'], 'textures/my%20texture.png',
                         'the stored uri must be left exactly as written')
        self.assertEqual(i['images'][0]['path'], 'textures/my texture.png',
                         'the resolvable path must be decoded')
        self.assertEqual(i['images'][0]['ext'], '.png')
        self.assertTrue(i['images'][0]['spec_ok'])

    def test_repoint_writes_an_encoded_uri_back(self):
        """A replacement containing a space must be written encoded, or the file
        stops conforming to the spec."""
        p = self.write([{'uri': 'a.png', 'mimeType': 'image/png'}])
        ch, notes = gltfread.repoint_gltf(p, {'a.png': 'textures/my texture.jpg'})
        self.assertEqual(ch, 1)
        with open(p) as f: raw = json.load(f)
        self.assertEqual(raw['images'][0]['uri'], 'textures/my%20texture.jpg')
        self.assertEqual(raw['images'][0]['mimeType'], 'image/jpeg')
        self.assertEqual(gltfread.read_gltf(p)['images'][0]['path'],
                         'textures/my texture.jpg', 'must round-trip')

    def test_repoint_matches_on_the_decoded_key_too(self):
        p = self.write([{'uri': 'textures/my%20texture.png'}])
        ch, _ = gltfread.repoint_gltf(p, {'textures/my texture.png': 'textures/ok.png'})
        self.assertEqual(ch, 1)
        self.assertEqual(gltfread.read_gltf(p)['images'][0]['uri'], 'textures/ok.png')

    def test_embedded_images_are_never_repointed(self):
        p = self.write([{'uri': 'data:image/png;base64,iVBORw0KGgo=', 'mimeType': 'image/png'},
                        {'bufferView': 0, 'mimeType': 'image/png'}])
        with open(p) as f: before = json.load(f)
        ch, _ = gltfread.repoint_gltf(p, {'data:image/png;base64,iVBORw0KGgo=': 'x.png'})
        self.assertEqual(ch, 0)
        with open(p) as f: after = json.load(f)
        self.assertEqual(before['images'], after['images'])

    def test_malformed_json_returns_error_not_exception(self):
        p = os.path.join(self.d, 'bad.gltf')
        with open(p, 'w') as f: f.write('{ not json')
        i = gltfread.read_gltf(p)
        self.assertFalse(i['ok'])
        self.assertIsNotNone(i['error'])


class TestModuleWiring(unittest.TestCase):
    def test_all_four_modules_import(self):
        """The check that would have caught fbxread/blendread missing from the repo."""
        import importlib
        for m in ('texcheck', 'texstudio', 'fbxread', 'blendread', 'gltfread'):
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


class TestTexstudioGltf(unittest.TestCase):
    """The app side of glTF. texcheck already handled .gltf while texstudio did
    not, so a .gltf dropped on the page uploaded and was then silently ignored."""

    def setUp(self):
        import texstudio
        self.ts = texstudio
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'textures'))
        self._saved_root = texstudio.ROOT
        texstudio.ROOT = os.path.abspath(self.d)

    def tearDown(self):
        self.ts.ROOT = self._saved_root
        shutil.rmtree(self.d, ignore_errors=True)

    def write_gltf(self, images, buffers=None, name='m.gltf'):
        doc = {'asset': {'version': '2.0', 'generator': 'test'},
               'meshes': [{}], 'materials': [{}, {}],
               'buffers': buffers if buffers is not None else [{'uri': 'm.bin'}],
               'images': images}
        p = os.path.join(self.d, name)
        with open(p, 'w') as f: json.dump(doc, f)
        return p

    def rows(self, **kw):
        g = self.ts.audit()['gltfs'][0]
        return g, {r['uri']: r for r in g['rows'] if r['kind'] == 'image'}

    # (a) SECURITY
    def test_traversal_uri_is_refused_and_not_resolved(self):
        """An image uri is attacker-controlled data. It must not resolve outside
        the workspace, and must not become something /api/file will serve."""
        self.write_gltf([{'uri': '../../../../etc/passwd'}])
        g, imgs = self.rows()
        row = imgs['../../../../etc/passwd']
        self.assertEqual(row['status'], 'unsafe')
        self.assertIsNone(row['resolved'], 'nothing outside ROOT may be offered up')
        self.assertEqual(g['relink'], 0, 'and it must never be repointed')

    def test_safe_refuses_a_sibling_directory_sharing_the_prefix(self):
        """ROOT='/w' must not accept '/w2/x' - a plain startswith says it does."""
        sib = self.d + '2'
        os.makedirs(sib, exist_ok=True)
        try:
            with self.assertRaises(ValueError):
                self.ts.safe('../' + os.path.basename(sib) + '/secret.png')
        finally:
            shutil.rmtree(sib, ignore_errors=True)

    def test_api_file_cannot_serve_outside_the_workspace(self):
        with self.assertRaises(ValueError):
            self.ts.safe('../../../../etc/passwd')

    # (b) never wire in something glTF cannot load
    def test_wrong_format_is_reported_not_wired_in(self):
        """Reference is .jpg and only an .exr exists: glTF permits PNG/JPEG only,
        so wiring the .exr in would produce a file no viewer can open."""
        with open(os.path.join(self.d, 'textures', 'rough.exr'), 'wb') as f:
            f.write(b'\x76\x2f\x31\x01')
        self.write_gltf([{'uri': 'textures/rough.jpg'}])
        g, imgs = self.rows()
        self.assertEqual(imgs['textures/rough.jpg']['status'], 'format')
        self.assertIsNone(imgs['textures/rough.jpg']['resolved'])
        self.assertEqual(g['relink'], 0)

    def test_a_png_match_is_still_relinked(self):
        tiny_png(os.path.join(self.d, 'textures', 'albedo.jpg'))
        self.write_gltf([{'uri': 'textures/albedo.png', 'mimeType': 'image/png'}])
        g, imgs = self.rows()
        self.assertEqual(imgs['textures/albedo.png']['status'], 'relink')
        self.assertEqual(g['relink'], 1)

    # (c) embedded data is not a missing file
    def test_embedded_images_are_not_reported_as_missing(self):
        self.write_gltf([{'uri': 'data:image/png;base64,iVBORw0KGgo=', 'mimeType': 'image/png'},
                         {'bufferView': 0, 'mimeType': 'image/png'}],
                        buffers=[{'uri': 'data:application/octet-stream;base64,AAAA'}])
        g, imgs = self.rows()
        self.assertEqual(imgs, {}, 'embedded images are not files and get no row')
        self.assertEqual(g['rows'], [], 'nor does an embedded buffer')

    # (d) the buffer is worth its own row
    def test_buffer_is_reported_separately(self):
        self.write_gltf([])
        g = self.ts.audit()['gltfs'][0]
        bufs = [r for r in g['rows'] if r['kind'] == 'buffer']
        self.assertEqual(len(bufs), 1)
        self.assertEqual(bufs[0]['uri'], 'm.bin')
        self.assertEqual(bufs[0]['status'], 'missing')
        self.assertIn('geometry', bufs[0]['note'])
        with open(os.path.join(self.d, 'm.bin'), 'wb') as f:
            f.write(b'\0' * 8)
        g = self.ts.audit()['gltfs'][0]
        self.assertEqual([r for r in g['rows'] if r['kind'] == 'buffer'][0]['status'], 'ok')

    # (e) dedupe
    def test_repeated_uris_are_deduplicated(self):
        """The Poly Haven picture frame has 8 image entries for 5 distinct files."""
        tiny_png(os.path.join(self.d, 'textures', 'a.png'))
        self.write_gltf([{'uri': 'textures/a.png'}] * 4)
        g, imgs = self.rows()
        self.assertEqual(len(imgs), 1, 'one row per distinct uri')

    # (f) .glb is binary
    def test_glb_is_skipped_not_parsed_as_json(self):
        with open(os.path.join(self.d, 'binary.glb'), 'wb') as f:
            f.write(b'glTF' + struct.pack('<II', 2, 20) + b'\0' * 12)
        a = self.ts.audit()                      # must not raise
        self.assertEqual([g['file'] for g in a['gltfs']], [])

    # (g) mimeType must follow the extension
    def test_repoint_updates_mimetype(self):
        tiny_png(os.path.join(self.d, 'textures', 'albedo.jpg'))
        self.write_gltf([{'uri': 'textures/albedo.png', 'mimeType': 'image/png'}])
        done = self.ts.fix_gltfs()
        self.assertEqual(done[0]['changed'], 1)
        with open(os.path.join(self.d, 'm.gltf')) as f: doc = json.load(f)
        self.assertEqual(doc['images'][0]['uri'], 'textures/albedo.jpg')
        self.assertEqual(doc['images'][0]['mimeType'], 'image/jpeg')

    # percent-encoding, through the app
    def test_percent_encoded_uri_resolves_in_the_app(self):
        tiny_png(os.path.join(self.d, 'textures', 'my texture.png'))
        self.write_gltf([{'uri': 'textures/my%20texture.png'}])
        g, imgs = self.rows()
        self.assertEqual(imgs['textures/my%20texture.png']['status'], 'ok',
                         'a space in the filename is not a missing texture')
        self.assertEqual(imgs['textures/my%20texture.png']['resolved'],
                         'textures/my texture.png')

    # (h) performance
    def test_image_index_is_built_once_per_audit(self):
        """images() walks the whole workspace. Fine at 132 files, sluggish at 5000,
        so it must not run once per glTF."""
        tiny_png(os.path.join(self.d, 'textures', 'a.png'))
        for n in ('one.gltf', 'two.gltf', 'three.gltf'):
            self.write_gltf([{'uri': 'textures/a.png'}], name=n)
        real, calls = self.ts.images, []
        def counting():
            calls.append(1); return real()
        self.ts.images = counting
        try:
            a = self.ts.audit()
        finally:
            self.ts.images = real
        self.assertEqual(len(a['gltfs']), 3)
        self.assertEqual(len(calls), 1,
                         f'workspace walked {len(calls)} times for 3 glTFs; expected 1')


class TestTexstudioFormatCoverage(unittest.TestCase):
    """The app must handle every format the CLI does.

    texcheck grew .fbx, .blend and .gltf support while texstudio silently lagged
    behind - twice. This class exists so that gap cannot open again unnoticed.
    """
    FORMATS = ('.obj', '.mtl', '.fbx', '.blend', '.gltf')
    READERS = ('fbxread', 'blendread', 'gltfread')

    def test_texstudio_imports_every_reader(self):
        import texstudio
        src = _read(texstudio.__file__)
        for mod in self.READERS:
            self.assertIn(f'from {mod} import', src,
                          f'texstudio.py never imports {mod}, so its format is ignored')

    def test_every_reader_is_optional_not_fatal(self):
        """A missing reader must degrade to a note, never crash the app."""
        import texstudio
        src = _read(texstudio.__file__)
        for name in ('read_fbx', 'read_blend', 'read_gltf'):
            self.assertIn(f'{name} = ', src, f'{name} needs a None fallback')

    def test_audit_returns_a_key_for_every_format(self):
        import texstudio
        d = tempfile.mkdtemp()
        saved = texstudio.ROOT
        texstudio.ROOT = os.path.abspath(d)
        try:
            a = texstudio.audit()
        finally:
            texstudio.ROOT = saved
            shutil.rmtree(d, ignore_errors=True)
        for key in ('objs', 'mtls', 'fbxs', 'blends', 'gltfs'):
            self.assertIn(key, a, f"audit() has no '{key}' key, so the UI cannot show it")

    def test_drop_zone_names_every_format(self):
        import texstudio
        drop = [ln for ln in texstudio.PAGE.splitlines() if 'id="drop"' in ln]
        self.assertEqual(len(drop), 1)
        for ext in self.FORMATS:
            self.assertIn(ext, drop[0],
                          f'the drop zone does not mention {ext}, so users will not know it works')

    def test_the_cli_and_the_app_cover_the_same_formats(self):
        """Whatever texcheck learns to audit, texstudio must learn too."""
        import texstudio
        cli = _read(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'texcheck.py'))
        app = _read(texstudio.__file__)
        for fmt, reader in (('.fbx', 'read_fbx'), ('.blend', 'read_blend'), ('.gltf', 'read_gltf')):
            self.assertIn(fmt, cli)
            self.assertIn(reader, app,
                          f'texcheck audits {fmt} but texstudio has no {reader}')


class TestNoFileHandleLeaks(unittest.TestCase):
    """Catch leaked file handles for real.

    `python -W error::ResourceWarning` does NOT fail the build: the warning is
    raised inside the file object's finalizer, where an exception cannot
    propagate, so Python prints "Exception ignored in:" and the exit code stays
    0. Recording the warning around an explicit gc.collect() does work, and is
    cross-platform (an fd count would need /proc).
    """
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, 'textures'))
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def assertNoLeak(self, fn, label):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            fn()
            gc.collect()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
        detail = '; '.join(f'{w.filename}:{w.lineno}' for w in leaks)
        self.assertEqual(leaks, [], f'{label} leaked {len(leaks)} file handle(s): {detail}')

    def test_blendread_closes_handles(self):
        p = make_blend(os.path.join(self.d, 'a.blend'), ['//textures/a.png'])
        self.assertNoLeak(lambda: blendread.read_blend(p), 'read_blend (raw)')

    def test_blendread_gzip_closes_handles(self):
        p = make_blend(os.path.join(self.d, 'g.blend'), ['//textures/a.png'], compress=True)
        self.assertNoLeak(lambda: blendread.read_blend(p), 'read_blend (gzip)')

    def test_blend_repoint_closes_handles(self):
        p = make_blend(os.path.join(self.d, 'r.blend'), ['//textures/a.png'])
        self.assertNoLeak(lambda: blendread.repoint_blend(p, {'//textures/a.png': '//textures/a.jpg'}),
                          'repoint_blend')

    def test_fbxread_closes_handles(self):
        p = make_fbx(os.path.join(self.d, 'a.fbx'), 7400,
                     [('M1', 'tex/a.png', 'DiffuseColor', 'Mat')])
        self.assertNoLeak(lambda: fbxread.read_fbx(p), 'read_fbx')

    def test_gltfread_closes_handles(self):
        p = os.path.join(self.d, 'm.gltf')
        with open(p, 'w') as f:
            json.dump({'asset': {'version': '2.0'}, 'images': [{'uri': 'a.png'}]}, f)
        self.assertNoLeak(lambda: gltfread.read_gltf(p), 'read_gltf')
        self.assertNoLeak(lambda: gltfread.repoint_gltf(p, {'a.png': 'b.png'}), 'repoint_gltf')

    def test_texcheck_closes_handles(self):
        tiny_png(os.path.join(self.d, 'textures', 'w.png'))
        with open(os.path.join(self.d, 'm.mtl'), 'w') as f:
            f.write('newmtl A\nmap_Kd N:\\dead\\w.png\n')
        with open(os.path.join(self.d, 'm.obj'), 'w') as f:
            f.write('mtllib m.mtl\n')
        idx = texcheck.build_index([self.d])
        def run():
            texcheck.find_mtls(os.path.join(self.d, 'm.obj'))
            texcheck.audit_mtl(os.path.join(self.d, 'm.mtl'), idx, fix=True)
        self.assertNoLeak(run, 'texcheck')


if __name__ == '__main__':
    unittest.main(verbosity=2)
