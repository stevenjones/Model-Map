#!/usr/bin/env python3
"""
texstudio.py - a small local web app for wiring up OBJ/MTL texture paths.

    python3 texstudio.py                 # uses ./texstudio_workspace
    python3 texstudio.py /path/to/model  # uses that folder

Then open http://127.0.0.1:8765 . Drag your .obj, .mtl and texture files onto the page
(or just put them in the folder), review what's broken, repoint anything by dropdown,
and save. Originals are kept as .bak. Download a tidy zip when you're done.

Pure standard library. No pip install. Binds to localhost only.
"""
import os, re, io, sys, json, shutil, zipfile, mimetypes, webbrowser, argparse
try:
    from fbxread import read_fbx
except Exception:
    read_fbx = None
try:
    from blendread import read_blend, repoint_blend
except Exception:
    read_blend = repoint_blend = None
try:
    from gltfread import read_gltf, repoint_gltf
except Exception:
    read_gltf = repoint_gltf = None
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

IMG_EXT = ('.png','.jpg','.jpeg','.tga','.tif','.tiff','.bmp','.exr','.psd','.dds','.webp')
VIEWABLE = ('.png','.jpg','.jpeg','.gif','.webp','.bmp')
# the glTF spec permits PNG and JPEG only; anything else parses but will not load
GLTF_OK = ('.png','.jpg','.jpeg')
MAP_KEYS = ('map_kd','map_ka','map_ks','map_ke','map_ns','map_d','map_bump','bump','disp',
            'decal','refl','map_pr','map_pm','map_ao','norm','map_refl')
OPTS_N = {'-bm':1,'-s':3,'-o':3,'-t':3,'-mm':2,'-texres':1,'-clamp':1,'-blendu':1,
          '-blendv':1,'-boost':1,'-imfchan':1,'-type':1,'-cc':1}
# slots people commonly mis-wire, and where the value actually belongs
SLOT_FIX = {'map_ks':('map_Pr','holds a roughness map; map_Ks is specular colour'),
            'map_refl':('map_Pm','holds a metalness map; map_refl is an environment map'),
            'map_ns':('map_Pr','holds a roughness map; map_Ns is specular exponent')}
# FBX slots that commonly hold the wrong kind of map
FBX_SLOT_FIX = {'specularcolor':('roughness','SpecularColor is a specular COLOUR slot'),
                'reflectioncolor':('metalness','ReflectionColor is an environment slot'),
                'reflectionfactor':('metalness','ReflectionFactor is a scalar reflectivity slot'),
                'shininessexponent':('roughness','ShininessExponent is not roughness')}
ROOT = None

def split_map_line(rest):
    toks = rest.split(); i = 0; opts = []
    while i < len(toks) and toks[i].startswith('-'):
        n = OPTS_N.get(toks[i], 1); opts += toks[i:i+1+n]; i += 1+n
    return ' '.join(opts), ' '.join(toks[i:]).strip().strip('"')

def base_any(p):
    return re.split(r'[\\/]', p.strip().strip('"'))[-1]

def images():
    out = []
    for r, _, fs in os.walk(ROOT):
        if '__MACOSX' in r: continue
        for f in fs:
            if f.lower().endswith(IMG_EXT):
                out.append(os.path.relpath(os.path.join(r, f), ROOT).replace(os.sep,'/'))
    return sorted(out)

def find_files(ext):
    out = []
    for r, _, fs in os.walk(ROOT):
        if '__MACOSX' in r: continue
        for f in fs:
            if f.lower().endswith(ext):
                out.append(os.path.relpath(os.path.join(r, f), ROOT).replace(os.sep,'/'))
    return sorted(out)

def guess(raw, mtl_rel, imgs):
    """Resolve a map path: as written, then by basename, then by stem."""
    d = os.path.dirname(mtl_rel)
    cand = os.path.normpath(os.path.join(d, raw.replace('\\','/'))).replace(os.sep,'/')
    if cand in imgs: return cand, 'ok'
    b = base_any(raw).lower()
    for i in imgs:
        if os.path.basename(i).lower() == b: return i, 'relinked by name'
    stem = os.path.splitext(b)[0]
    for i in imgs:
        if os.path.splitext(os.path.basename(i))[0].lower() == stem:
            return i, 'relinked (different extension)'
    return None, 'missing'

def audit_fbx(imgs):
    out = []
    for f in find_files('.fbx') + find_files('.FBX'):
        full = os.path.join(ROOT, f)
        if read_fbx is None:
            out.append({'file':f,'error':'fbxread.py not found beside texstudio.py',
                        'format':'?','version':0,'rows':[],'materials':[]}); continue
        info = read_fbx(full)
        rows = []
        for t in info['textures']:
            new, status = guess(t['file'], f, imgs)
            hint = FBX_SLOT_FIX.get((t['slot'] or '').lower())
            rows.append({'slot':t['slot'] or '(unassigned)','mat':t['material'] or '-',
                         'raw':t['file'],'resolved':new,'status':status,
                         'slot_hint':hint,
                         'expects':base_any(t['file'])})
        out.append({'file':f,'format':info['format'],'version':info['version'],
                    'materials':info['materials'],'rows':rows,'error':info['error']})
    return out

def blend_map(bl_rel):
    """Work out which .blend image paths can be repointed to files we actually have."""
    full = os.path.join(ROOT, bl_rel); d = os.path.dirname(full)
    info = read_blend(full)
    mapping, ok, miss = {}, 0, []
    if info.get('error') or not info['ok']: return info, mapping, ok, miss
    imgs = images()
    for im in info['images']:
        rel = im['path'][2:] if im['path'].startswith('//') else im['path']
        if os.path.isfile(os.path.join(d, rel)): ok += 1; continue
        base = base_any(rel); hit = None
        for i in imgs:
            if os.path.basename(i).lower() == base.lower(): hit = i; break
        if not hit:
            stem = os.path.splitext(base)[0].lower()
            for i in imgs:
                if os.path.splitext(os.path.basename(i))[0].lower() == stem: hit = i; break
        if hit:
            mapping[im['path']] = '//' + os.path.relpath(os.path.join(ROOT, hit), d).replace(os.sep,'/')
        else: miss.append(base)
    return info, mapping, ok, miss

def audit_blend_all():
    out = []
    for f in find_files('.blend'):
        if read_blend is None:
            out.append({'file':f,'error':'blendread.py not found beside texstudio.py',
                        'compression':'?','version':'','n':0,'ok':0,'relink':0,'missing':[],'sample':None})
            continue
        info, mapping, ok, miss = blend_map(f)
        sample = None
        if mapping:
            k = next(iter(mapping)); sample = [k, mapping[k]]
        out.append({'file':f,'error':info.get('error'),'compression':info.get('compression','?'),
                    'version':info.get('version',''),'n':len(info.get('images',[])),
                    'ok':ok,'relink':len(mapping),'missing':sorted(set(miss))[:6],'sample':sample})
    return out

def _within_root(rel):
    """True if rel stays inside ROOT. safe() is the same guard the upload and the
    file-serving paths use, so a uri that fails here is also unservable."""
    try:
        safe(rel); return True
    except ValueError:
        return False

def gltf_map(gl_rel, imgs):
    """Resolve every image uri in one .gltf against the workspace.

    Returns (info, rows, mapping). Rows are DEDUPED by uri - a real glTF names the
    same texture once per material slot, so the Poly Haven picture frame has 8
    image entries for 5 distinct files. imgs is passed in so audit() walks the
    workspace once rather than once per glTF.

    Image uris come from an untrusted file, so every resolved path goes through
    safe(); one that escapes ROOT is refused rather than resolved or served.
    """
    full = os.path.join(ROOT, gl_rel)
    info = read_gltf(full)
    rows, mapping = [], {}
    if info.get('error'): return info, rows, mapping
    d = os.path.dirname(gl_rel)

    def resolve(p):
        cand = os.path.normpath(os.path.join(d, p.replace('\\','/'))).replace(os.sep,'/')
        return cand if _within_root(cand) else None

    # the .bin holds the geometry, so it gets its own row rather than being
    # lumped in with the textures. Embedded buffers have no file to report.
    for b in info['buffers']:
        if b['embedded']: continue
        cand = resolve(b['path'])
        if cand is None:
            rows.append({'kind':'buffer','uri':b['uri'],'resolved':None,'status':'unsafe',
                         'note':'path escapes the workspace - refused'}); continue
        ok = os.path.isfile(os.path.join(ROOT, cand))
        rows.append({'kind':'buffer','uri':b['uri'],'resolved':None,
                     'status':'ok' if ok else 'missing',
                     'note':'' if ok else 'the geometry lives here; without it there is no model'})

    seen = set()
    for im in info['images']:
        # data: uris and bufferView images are embedded - not missing files
        if im['embedded']: continue
        uri = im['uri']
        if uri in seen: continue
        seen.add(uri)
        flags = []
        if not im['spec_ok']: flags.append('NOT PNG/JPEG - invalid in glTF')
        if not im['mime_matches']: flags.append(f"mimeType says {im['mimeType']}")
        cand = resolve(im['path'])
        if cand is None:
            rows.append({'kind':'image','uri':uri,'resolved':None,'status':'unsafe',
                         'note':'path escapes the workspace - refused'}); continue
        if cand in imgs:
            rows.append({'kind':'image','uri':uri,'resolved':cand,'status':'ok',
                         'note':'; '.join(flags)}); continue
        base = base_any(im['path']).lower(); hit = None
        for i in imgs:
            if os.path.basename(i).lower() == base: hit = i; break
        if not hit:
            stem = os.path.splitext(base)[0]
            for i in imgs:
                if os.path.splitext(os.path.basename(i))[0].lower() == stem: hit = i; break
        if not hit:
            rows.append({'kind':'image','uri':uri,'resolved':None,'status':'missing',
                         'note':'; '.join(flags)}); continue
        ext = os.path.splitext(hit)[1].lower()
        if ext not in GLTF_OK:
            # wiring this in would leave a file no glTF viewer can load, so leave it
            flags.append(f'only a {ext} exists, which glTF cannot use')
            rows.append({'kind':'image','uri':uri,'resolved':None,'status':'format',
                         'note':'; '.join(flags)}); continue
        mapping[uri] = os.path.relpath(os.path.join(ROOT, hit),
                                       os.path.dirname(full)).replace(os.sep,'/')
        rows.append({'kind':'image','uri':uri,'resolved':hit,'status':'relink',
                     'note':'; '.join(flags)})
    return info, rows, mapping

def audit_gltf_all(imgs):
    """.glb is binary, not JSON - find_files('.gltf') does not match it, so it is
    skipped rather than parsed."""
    out = []
    for f in find_files('.gltf'):
        if read_gltf is None:
            out.append({'file':f,'error':'gltfread.py not found beside texstudio.py',
                        'version':'','generator':'','counts':{},'rows':[],'relink':0})
            continue
        info, rows, mapping = gltf_map(f, imgs)
        out.append({'file':f,'error':info.get('error'),'version':info.get('version',''),
                    'generator':info.get('generator',''),'counts':info.get('counts',{}),
                    'rows':rows,'relink':len(mapping)})
    return out

def audit():
    imgs = images()
    objs = []
    for o in find_files('.obj'):
        named = []
        try:
            with open(os.path.join(ROOT,o), errors='replace') as fh:
                lines_o = fh.readlines()
            for line in lines_o:
                if line.lower().startswith('mtllib'):
                    rest = line.split(None,1)[1].strip()
                    d = os.path.dirname(os.path.join(ROOT,o))
                    named += [rest] if os.path.isfile(os.path.join(d,rest)) else rest.split()
        except Exception: pass
        notes = []
        for n in named:
            p = os.path.normpath(os.path.join(os.path.dirname(o), n)).replace(os.sep,'/')
            if not os.path.isfile(os.path.join(ROOT,p)):
                notes.append({'lvl':'bad','msg':f"mtllib '{n}' does not exist"})
            elif ' ' in n:
                notes.append({'lvl':'warn','msg':f"mtllib '{n}' contains spaces (breaks many parsers)"})
        if not named: notes.append({'lvl':'bad','msg':'no mtllib line at all'})
        objs.append({'file':o,'notes':notes})

    mtls = []
    for m in find_files('.mtl'):
        rows, cur, matmaps = [], None, {}
        with open(os.path.join(ROOT,m), errors='replace') as fh:
            _src = fh.read().splitlines()
        for ln, line in enumerate(_src):
            s = line.strip()
            if not s or s.startswith('#'): continue
            key = s.split()[0].lower()
            if key == 'newmtl':
                cur = s.split(None,1)[1] if len(s.split())>1 else '?'
                matmaps[cur] = []
            elif key in MAP_KEYS and len(s.split())>1:
                opts, raw = split_map_line(s.split(None,1)[1])
                new, status = guess(raw, m, imgs)
                slot = SLOT_FIX.get(key)
                rows.append({'line':ln,'mat':cur,'key':s.split()[0],'raw':raw,'opts':opts,
                             'resolved':new,'status':status,
                             'slot_hint':(slot[0], slot[1]) if slot else None})
                matmaps.setdefault(cur,[]).append(key)
            elif key == 'kd' and len(s.split())==4:
                try:
                    v = [float(x) for x in s.split()[1:]]
                    if max(v) < 0.99:
                        rows.append({'line':ln,'mat':cur,'key':'Kd','raw':' '.join(f'{x:g}' for x in v),
                                     'opts':'','resolved':None,
                                     'status':f'darkens any texture by {100*(1-max(v)):.0f}%',
                                     'slot_hint':None,'is_kd':True})
                except ValueError: pass
        mtls.append({'file':m,'rows':rows})
    fbxs = audit_fbx(imgs)
    blends = audit_blend_all()
    gltfs = audit_gltf_all(imgs)
    used = {r.get('resolved') for mm in mtls for r in mm['rows']}
    used |= {r.get('resolved') for fb in fbxs for r in fb['rows']}
    used |= {r.get('resolved') for g in gltfs for r in g['rows']}
    unused = [i for i in imgs if i not in used]
    return {'root':ROOT,'images':imgs,'objs':objs,'mtls':mtls,'fbxs':fbxs,
            'blends':blends,'gltfs':gltfs,'unused':unused}

def apply_edits(payload):
    """edits: [{file, line, key, path}]  kd: [{file, line, value}]"""
    byfile = {}
    for e in payload.get('edits',[]) + payload.get('kd',[]):
        byfile.setdefault(e['file'], []).append(e)
    changed = []
    for f, es in byfile.items():
        p = os.path.join(ROOT, f)
        with open(p, errors='replace') as fh:
            lines = fh.read().splitlines()
        for e in es:
            i = int(e['line'])
            if i >= len(lines): continue
            if 'value' in e:
                lines[i] = f"\tKd {e['value']}"
            else:
                cur = lines[i]
                indent = cur[:len(cur)-len(cur.lstrip())]
                opts = e.get('opts','')
                rel = os.path.relpath(os.path.join(ROOT, e['path']), os.path.dirname(p)).replace(os.sep,'/')
                lines[i] = f"{indent}{e['key']}{(' '+opts) if opts else ''} {rel}"
        if not os.path.exists(p + '.bak'): shutil.copy2(p, p + '.bak')
        with open(p,'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        changed.append(f)
    return changed

def collect_maps():
    """Copy every referenced texture into maps/ and repoint the MTLs there."""
    a = audit(); moved = 0
    os.makedirs(os.path.join(ROOT,'maps'), exist_ok=True)
    for mm in a['mtls']:
        edits = []
        for r in mm['rows']:
            if r.get('resolved'):
                src = os.path.join(ROOT, r['resolved'])
                dst = os.path.join(ROOT,'maps', os.path.basename(r['resolved']))
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst); moved += 1
                edits.append({'file':mm['file'],'line':r['line'],'key':r['key'],
                              'opts':r['opts'],'path':'maps/'+os.path.basename(r['resolved'])})
        if edits: apply_edits({'edits':edits})
    return moved

def stage_fbx(fbx_rel):
    """A binary FBX cannot be safely rewritten in place, but every importer falls back
    to looking for the texture's BASENAME next to the FBX. So copy the files we found
    to those exact names beside it - which is what actually fixes the import."""
    a = audit(); done, missing = [], []
    for fb in a['fbxs']:
        if fbx_rel and fb['file'] != fbx_rel: continue
        dest_dir = os.path.dirname(os.path.join(ROOT, fb['file'])) or ROOT
        for r in fb['rows']:
            want = r['expects']
            if not r['resolved']:
                missing.append(want); continue
            src = os.path.join(ROOT, r['resolved'])
            dst = os.path.join(dest_dir, want)
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst); done.append(want)
    return {'placed': sorted(set(done)), 'missing': sorted(set(missing))}

def fix_blends():
    done = []
    for f in find_files('.blend'):
        if read_blend is None: break
        info, mapping, ok, miss = blend_map(f)
        if mapping:
            ch, toolong = repoint_blend(os.path.join(ROOT, f), mapping)
            done.append({'file':f,'changed':ch,'skipped':len(toolong)})
    return done

def fix_gltfs():
    done = []
    if repoint_gltf is None: return done
    imgs = images()
    for f in find_files('.gltf'):
        info, rows, mapping = gltf_map(f, imgs)
        if mapping:
            ch, notes = repoint_gltf(os.path.join(ROOT, f), mapping)
            done.append({'file':f,'changed':ch,'notes':notes})
    return done

def make_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for r,_,fs in os.walk(ROOT):
            if '__MACOSX' in r: continue
            for f in fs:
                if f.endswith('.bak'): continue
                full = os.path.join(r,f)
                z.write(full, os.path.relpath(full, ROOT))
    return buf.getvalue()

def safe(rel):
    """Resolve rel inside ROOT or refuse it.

    The prefix test needs the separator: without it ROOT='/w' also accepts
    '/w2/secret.png', a sibling directory that merely starts with the same
    string. Uploads, /api/file and every uri read out of a model file go
    through here.
    """
    root = os.path.abspath(ROOT)
    p = os.path.abspath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep): raise ValueError('path escape')
    return p

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype='application/json'):
        if isinstance(body,str): body = body.encode()
        self.send_response(code); self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == '/': return self._send(200, PAGE, 'text/html; charset=utf-8')
        if u.path == '/api/audit': return self._send(200, json.dumps(audit()))
        if u.path == '/api/file':
            try: p = safe(unquote(q.get('p',[''])[0]))
            except ValueError: return self._send(400,'{}')
            if not os.path.isfile(p): return self._send(404,'{}')
            ctype = mimetypes.guess_type(p)[0] or 'application/octet-stream'
            with open(p,'rb') as fh:
                blob = fh.read()
            return self._send(200, blob, ctype)
        if u.path == '/api/zip':
            return self._send(200, make_zip(), 'application/zip')
        return self._send(404,'{}')
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get('Content-Length',0)); raw = self.rfile.read(n)
        if u.path == '/api/save':
            return self._send(200, json.dumps({'changed':apply_edits(json.loads(raw))}))
        if u.path == '/api/fixblend':
            return self._send(200, json.dumps({'done':fix_blends()}))
        if u.path == '/api/fixgltf':
            return self._send(200, json.dumps({'done':fix_gltfs()}))
        if u.path == '/api/stagefbx':
            d = json.loads(raw or b'{}')
            return self._send(200, json.dumps(stage_fbx(d.get('file'))))
        if u.path == '/api/collect':
            return self._send(200, json.dumps({'moved':collect_maps()}))
        if u.path == '/api/upload':
            d = json.loads(raw)
            import base64
            for f in d['files']:
                rel = f['name'].replace('\\','/').lstrip('/')
                dest = safe(rel); os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest,'wb') as fh:
                    fh.write(base64.b64decode(f['data']))
            return self._send(200, json.dumps({'saved':len(d['files'])}))
        return self._send(404,'{}')

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Texture Studio</title>
<style>
*{box-sizing:border-box} body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
margin:0;background:#14161a;color:#e6e8ec}
header{padding:14px 20px;background:#1b1e24;border-bottom:1px solid #2a2f38;
display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0;font-weight:600}
.path{color:#8b93a3;font-size:12px;font-family:ui-monospace,monospace}
button{background:#2d6cdf;color:#fff;border:0;padding:8px 14px;border-radius:6px;
cursor:pointer;font-size:13px} button:hover{background:#3b7bee}
button.ghost{background:#262b34} button.ghost:hover{background:#313846}
main{padding:20px;max-width:1200px;margin:0 auto}
#drop{border:2px dashed #39404d;border-radius:10px;padding:26px;text-align:center;
color:#8b93a3;margin-bottom:20px;transition:.15s}
#drop.hot{border-color:#2d6cdf;background:#1a2231;color:#cfe0ff}
.card{background:#1b1e24;border:1px solid #2a2f38;border-radius:10px;margin-bottom:16px;overflow:hidden}
.card h2{font-size:13px;margin:0;padding:11px 14px;background:#20242c;
border-bottom:1px solid #2a2f38;font-family:ui-monospace,monospace;font-weight:600}
table{width:100%;border-collapse:collapse} td,th{padding:9px 12px;text-align:left;
border-bottom:1px solid #23272f;vertical-align:middle;font-size:13px}
th{color:#8b93a3;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}
.tag{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
.ok{background:#12351f;color:#5fd48a} .fix{background:#3a2f10;color:#f0c057}
.bad{background:#3d1a1c;color:#ff8b8b} .warn{background:#33241a;color:#ffab6b}
select{background:#12151a;color:#e6e8ec;border:1px solid #333a45;border-radius:5px;
padding:5px 7px;max-width:340px;font-size:12px}
.raw{font-family:ui-monospace,monospace;font-size:11px;color:#8b93a3;
max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
img.th{width:38px;height:38px;object-fit:cover;border-radius:4px;background:#000;display:block}
.hint{color:#f0c057;font-size:11px;margin-top:3px}
.mat{color:#9ecbff;font-family:ui-monospace,monospace;font-size:12px}
.empty{padding:16px;color:#8b93a3} .chips{display:flex;gap:6px;flex-wrap:wrap;padding:12px 14px}
.chip{background:#262b34;padding:4px 9px;border-radius:12px;font-size:11px;color:#a9b2c1;
font-family:ui-monospace,monospace}
#msg{margin-left:auto;color:#5fd48a;font-size:12px}
input.kd{background:#12151a;color:#e6e8ec;border:1px solid #333a45;border-radius:5px;
padding:5px 7px;width:150px;font-family:ui-monospace,monospace;font-size:12px}
</style></head><body>
<header><h1>Texture Studio</h1><span class="path" id="root"></span>
<button onclick="save()">Save changes</button>
<button class="ghost" onclick="collect()">Collect into maps/</button>
<button class="ghost" onclick="stagefbx()">Place FBX textures</button>
<button class="ghost" onclick="fixblend()">Repoint .blend paths</button>
<button class="ghost" onclick="fixgltf()">Repoint .gltf paths</button>
<button class="ghost" onclick="location.href='/api/zip'">Download zip</button>
<button class="ghost" onclick="load()">Refresh</button><span id="msg"></span></header>
<main>
<div id="drop">Drop .obj, .mtl, .fbx, .blend, .gltf and texture files here (folders work too)</div>
<div id="out"></div></main>
<script>
let D=null;
const q=s=>document.createElement(s);
function tag(t,c){const s=q('span');s.className='tag '+c;s.textContent=t;return s}
async function load(){
  D=await (await fetch('/api/audit')).json();
  document.getElementById('root').textContent=D.root;
  const out=document.getElementById('out'); out.innerHTML='';
  for(const o of D.objs){
    const c=q('div');c.className='card';
    c.innerHTML='<h2>'+o.file+'</h2>';
    if(!o.notes.length){const d=q('div');d.className='empty';d.textContent='mtllib fine.';c.appendChild(d)}
    for(const n of o.notes){const d=q('div');d.className='empty';
      d.appendChild(tag(n.lvl==='bad'?'PROBLEM':'WARNING',n.lvl==='bad'?'bad':'warn'));
      d.append(' '+n.msg);c.appendChild(d)}
    out.appendChild(c);
  }
  for(const m of D.mtls){
    const c=q('div');c.className='card';c.innerHTML='<h2>'+m.file+'</h2>';
    if(!m.rows.length){const d=q('div');d.className='empty';d.textContent='no texture references.';c.appendChild(d);out.appendChild(c);continue}
    const t=q('table');
    t.innerHTML='<tr><th></th><th>Material</th><th>Slot</th><th>In the file</th><th>Points to</th><th>Status</th></tr>';
    for(const r of m.rows){
      const tr=q('tr');
      const td=()=>{const x=q('td');tr.appendChild(x);return x};
      const thumb=td();
      if(r.resolved&&/\.(png|jpe?g|gif|webp|bmp)$/i.test(r.resolved)){
        const im=q('img');im.className='th';im.src='/api/file?p='+encodeURIComponent(r.resolved);thumb.appendChild(im)}
      const mt=td();mt.innerHTML='<span class="mat">'+(r.mat||'?')+'</span>';
      td().textContent=r.key;
      const rw=td();rw.className='raw';rw.textContent=r.raw;rw.title=r.raw;
      const pick=td();
      if(r.is_kd){
        const i=q('input');i.className='kd';i.value=r.raw;i.dataset.file=m.file;i.dataset.line=r.line;i.dataset.kd='1';
        pick.appendChild(i);
        const b=q('button');b.className='ghost';b.style.marginLeft='6px';b.textContent='set 1 1 1';
        b.onclick=()=>{i.value='1.000000 1.000000 1.000000'};pick.appendChild(b);
      } else {
        const s=q('select');s.dataset.file=m.file;s.dataset.line=r.line;
        s.dataset.key=r.key;s.dataset.opts=r.opts||'';
        const none=q('option');none.value='';none.textContent='— not found —';s.appendChild(none);
        for(const img of D.images){const o=q('option');o.value=img;o.textContent=img;
          if(img===r.resolved)o.selected=true;s.appendChild(o)}
        s.onchange=()=>{const im=thumb.querySelector('img');
          if(s.value&&/\.(png|jpe?g|gif|webp|bmp)$/i.test(s.value)){
            if(im)im.src='/api/file?p='+encodeURIComponent(s.value);
            else{const n=q('img');n.className='th';n.src='/api/file?p='+encodeURIComponent(s.value);thumb.appendChild(n)}}};
        pick.appendChild(s);
      }
      const st=td();
      if(r.is_kd) st.appendChild(tag('CHECK','warn'));
      else if(r.status==='ok') st.appendChild(tag('OK','ok'));
      else if(r.status==='missing') st.appendChild(tag('MISSING','bad'));
      else st.appendChild(tag('RELINKED','fix'));
      if(r.is_kd){const h=q('div');h.className='hint';h.textContent=r.status;st.appendChild(h)}
      if(r.slot_hint){const h=q('div');h.className='hint';
        h.textContent='wrong slot: '+r.slot_hint[1]+' — should be '+r.slot_hint[0];st.appendChild(h)}
      t.appendChild(tr);
    }
    c.appendChild(t);out.appendChild(c);
  }
  for(const bl of (D.blends||[])){
    const c=q('div');c.className='card';
    c.innerHTML='<h2>'+bl.file+'  <span style="color:#8b93a3;font-weight:400">'
      +bl.compression+' v'+bl.version+' · '+bl.n+' image datablock(s)</span></h2>';
    const d=q('div');d.className='empty';
    if(bl.error){d.appendChild(tag('CANNOT READ','bad'));d.append(' '+bl.error)}
    else{
      if(bl.relink){d.appendChild(tag(bl.relink+' RELINKABLE','fix'));d.append(' ')}
      if(bl.ok){d.appendChild(tag(bl.ok+' OK','ok'));d.append(' ')}
      if(bl.missing.length){d.appendChild(tag(bl.missing.length+' MISSING','bad'));
        d.append(' '+bl.missing.join(', '))}
      if(bl.sample){const s2=q('div');s2.className='hint';s2.style.marginTop='6px';
        s2.textContent='e.g. '+bl.sample[0]+'  →  '+bl.sample[1];d.appendChild(s2)}
      if(bl.relink){const s3=q('div');s3.className='hint';s3.style.marginTop='6px';
        s3.style.color='#8b93a3';
        s3.textContent='Blender stores each path in a fixed-size buffer, so a same-length-or-shorter '
          +'replacement is written in place - nothing in the file moves. Use "Repoint .blend paths".';
        d.appendChild(s3)}
    }
    c.appendChild(d);out.appendChild(c);
  }
  for(const g of (D.gltfs||[])){
    const c=q('div');c.className='card';
    const cts=g.counts||{};
    c.innerHTML='<h2>'+g.file+'  <span style="color:#8b93a3;font-weight:400">glTF '
      +(g.version||'?')+(g.generator?' · '+g.generator:'')
      +' · '+(cts.materials||0)+' material(s) · '+(cts.images||0)+' image reference(s)</span></h2>';
    if(g.error){const d=q('div');d.className='empty';
      d.appendChild(tag('CANNOT READ','bad'));d.append(' '+g.error);
      c.appendChild(d);out.appendChild(c);continue}
    if(!g.rows.length){const d=q('div');d.className='empty';
      d.textContent='no external file references (everything is embedded).';
      c.appendChild(d);out.appendChild(c);continue}
    const t=q('table');
    t.innerHTML='<tr><th></th><th>Kind</th><th>Reference in the file</th>'
               +'<th>Found in workspace</th><th>Status</th></tr>';
    for(const r of g.rows){
      const tr=q('tr');const td=()=>{const x=q('td');tr.appendChild(x);return x};
      const th=td();
      if(r.resolved&&/\.(png|jpe?g|gif|webp|bmp)$/i.test(r.resolved)){
        const im=q('img');im.className='th';
        im.src='/api/file?p='+encodeURIComponent(r.resolved);th.appendChild(im)}
      td().textContent=r.kind;
      const rw=td();rw.className='raw';rw.textContent=r.uri;rw.title=r.uri;
      const fo=td();fo.textContent=r.resolved||'—';fo.style.fontSize='12px';
      const st=td();
      if(r.status==='ok')st.appendChild(tag('OK','ok'));
      else if(r.status==='relink')st.appendChild(tag('RELINKABLE','fix'));
      else if(r.status==='format')st.appendChild(tag('WRONG FORMAT','warn'));
      else if(r.status==='unsafe')st.appendChild(tag('REFUSED','bad'));
      else st.appendChild(tag('MISSING','bad'));
      if(r.note){const h=q('div');h.className='hint';h.textContent=r.note;st.appendChild(h)}
      t.appendChild(tr);
    }
    c.appendChild(t);
    const n=q('div');n.className='empty';n.style.fontSize='12px';
    n.innerHTML='A .gltf is JSON, so paths are rewritten in place and the .bin and all '
      +'binary data are left untouched. <b>Repoint .gltf paths</b> writes the working paths '
      +'(percent-encoded) and corrects mimeType to match. glTF permits PNG and JPEG only, so '
      +'a reference whose only match on disk is e.g. an .exr is reported and left alone.';
    c.appendChild(n);
    out.appendChild(c);
  }
  for(const fb of (D.fbxs||[])){
    const c=q('div');c.className='card';
    c.innerHTML='<h2>'+fb.file+'  <span style="color:#8b93a3;font-weight:400">'
      +fb.format+' v'+fb.version+(fb.materials.length?' · '+fb.materials.length+' material(s)':'')+'</span></h2>';
    if(fb.error){const d=q('div');d.className='empty';
      d.appendChild(tag('READ ERROR','bad'));d.append(' '+fb.error);c.appendChild(d);out.appendChild(c);continue}
    if(!fb.rows.length){const d=q('div');d.className='empty';
      d.textContent='no texture references inside this FBX.';c.appendChild(d);out.appendChild(c);continue}
    const t=q('table');
    t.innerHTML='<tr><th></th><th>Material</th><th>Slot</th><th>Path inside the FBX</th>'
               +'<th>Found in workspace</th><th>Status</th></tr>';
    for(const r of fb.rows){
      const tr=q('tr');const td=()=>{const x=q('td');tr.appendChild(x);return x};
      const th=td();
      if(r.resolved&&/\.(png|jpe?g|gif|webp|bmp)$/i.test(r.resolved)){
        const im=q('img');im.className='th';im.src='/api/file?p='+encodeURIComponent(r.resolved);th.appendChild(im)}
      td().innerHTML='<span class="mat">'+r.mat+'</span>';
      td().textContent=r.slot;
      const rw=td();rw.className='raw';rw.textContent=r.raw;rw.title=r.raw;
      const fo=td();fo.textContent=r.resolved||'—';fo.style.fontSize='12px';
      const st=td();
      if(r.status==='ok')st.appendChild(tag('OK','ok'));
      else if(r.status==='missing')st.appendChild(tag('MISSING','bad'));
      else st.appendChild(tag('FOUND','fix'));
      if(r.slot_hint){const h=q('div');h.className='hint';
        h.textContent='likely wrong slot: '+r.slot_hint[1]+' but holds a '+r.slot_hint[0]+' map';st.appendChild(h)}
      t.appendChild(tr);
    }
    c.appendChild(t);
    const n=q('div');n.className='empty';n.style.fontSize='12px';
    n.innerHTML='A binary FBX cannot be safely rewritten in place. '
      +'<b>Place FBX textures</b> copies each map next to the FBX under the exact filename it '
      +'asks for, which is what importers fall back to — that fixes the import without touching the FBX.';
    c.appendChild(n);
    out.appendChild(c);
  }
  if(D.unused.length){
    const c=q('div');c.className='card';c.innerHTML='<h2>images in the folder that nothing references</h2>';
    const d=q('div');d.className='chips';
    for(const u of D.unused){const s=q('span');s.className='chip';s.textContent=u;d.appendChild(s)}
    c.appendChild(d);out.appendChild(c);
  }
}
async function save(){
  const edits=[],kd=[];
  document.querySelectorAll('select[data-file]').forEach(s=>{
    if(s.value)edits.push({file:s.dataset.file,line:+s.dataset.line,key:s.dataset.key,
                           opts:s.dataset.opts,path:s.value})});
  document.querySelectorAll('input[data-kd]').forEach(i=>{
    kd.push({file:i.dataset.file,line:+i.dataset.line,value:i.value})});
  const r=await (await fetch('/api/save',{method:'POST',body:JSON.stringify({edits,kd})})).json();
  msg('saved — originals kept as .bak');load();
}
async function fixblend(){
  const r=await (await fetch('/api/fixblend',{method:'POST',body:'{}'})).json();
  const n=r.done.reduce((a,b)=>a+b.changed,0);
  msg('repointed '+n+' path(s) across '+r.done.length+' .blend file(s) — originals kept as .bak');
  load();
}
async function fixgltf(){
  const r=await (await fetch('/api/fixgltf',{method:'POST',body:'{}'})).json();
  const n=r.done.reduce((a,b)=>a+b.changed,0);
  msg('repointed '+n+' uri(s) across '+r.done.length+' .gltf file(s) — originals kept as .bak');
  load();
}
async function stagefbx(){
  const r=await (await fetch('/api/stagefbx',{method:'POST',body:'{}'})).json();
  let m='placed '+r.placed.length+' texture(s) beside the FBX';
  if(r.missing.length) m+=' — still missing: '+r.missing.join(', ');
  msg(m);load();
}
async function collect(){
  const r=await (await fetch('/api/collect',{method:'POST',body:'{}'})).json();
  msg('copied '+r.moved+' texture(s) into maps/');load();
}
function msg(t){const m=document.getElementById('msg');m.textContent=t;setTimeout(()=>m.textContent='',4000)}
const dz=document.getElementById('drop');
['dragenter','dragover'].forEach(e=>dz.addEventListener(e,ev=>{ev.preventDefault();dz.classList.add('hot')}));
['dragleave','drop'].forEach(e=>dz.addEventListener(e,ev=>{ev.preventDefault();dz.classList.remove('hot')}));
async function walk(entry,path,out){
  if(entry.isFile){await new Promise(r=>entry.file(f=>{out.push([path+f.name,f]);r()}))}
  else if(entry.isDirectory){
    const rd=entry.createReader();
    const ents=await new Promise(r=>rd.readEntries(r));
    for(const e of ents) await walk(e,path+entry.name+'/',out)}
}
dz.addEventListener('drop',async ev=>{
  const items=[...ev.dataTransfer.items].map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  const files=[];
  if(items.length){for(const it of items) await walk(it,'',files)}
  else{for(const f of ev.dataTransfer.files) files.push([f.name,f])}
  if(!files.length)return;
  msg('uploading '+files.length+' file(s)…');
  const payload=[];
  for(const [name,f] of files){
    const b=await f.arrayBuffer();
    let s='';const u=new Uint8Array(b);
    for(let i=0;i<u.length;i+=8192) s+=String.fromCharCode.apply(null,u.subarray(i,i+8192));
    payload.push({name,data:btoa(s)});
  }
  await fetch('/api/upload',{method:'POST',body:JSON.stringify({files:payload})});
  msg('added '+payload.length+' file(s)');load();
});
load();
</script></body></html>"""

def main():
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument('folder', nargs='?', default='texstudio_workspace')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    ROOT = os.path.abspath(a.folder)
    os.makedirs(ROOT, exist_ok=True)
    url = f'http://127.0.0.1:{a.port}'
    print(f"Texture Studio\n  workspace: {ROOT}\n  open:      {url}\n  (ctrl-c to stop)")
    if not a.no_browser:
        try: webbrowser.open(url)
        except Exception: pass
    ThreadingHTTPServer(('127.0.0.1', a.port), H).serve_forever()

if __name__ == '__main__':
    main()
