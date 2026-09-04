#!/usr/bin/env python3
"""
texcheck.py - find and fix broken texture links in OBJ/MTL files.

    python3 texcheck.py MODEL.obj                    # report only, changes nothing
    python3 texcheck.py MODEL.obj --fix              # rewrite the MTL to working paths
    python3 texcheck.py MODEL.obj --fix --collect    # also copy textures into ./maps/
    python3 texcheck.py FOLDER --search ~/Textures   # scan a whole folder, search elsewhere too

Pure standard library. No installs. Works on Windows, macOS and Linux.
"""
import os, re, sys, shutil, argparse
try:
    from fbxread import read_fbx
except Exception:
    read_fbx = None
try:
    from blendread import read_blend, repoint_blend
except Exception:
    read_blend = repoint_blend = None

MAP_KEYS = ('map_kd','map_ka','map_ks','map_ke','map_ns','map_d','map_bump','bump',
            'disp','decal','refl','map_pr','map_pm','map_ao','norm','map_refl')
IMG_EXT  = ('.png','.jpg','.jpeg','.tga','.tif','.tiff','.bmp','.exr','.psd','.dds','.gif','.webp')
# MTL numeric options that take arguments, so we can tell an option from a filename
OPTS_N   = {'-bm':1,'-s':3,'-o':3,'-t':3,'-mm':2,'-texres':1,'-clamp':1,'-blendu':1,
            '-blendv':1,'-boost':1,'-imfchan':1,'-type':1,'-cc':1}

def split_map_line(rest):
    """Return (options_string, filename) from the part after 'map_Kd'.
    Handles '-bm 0.3 C:\\path with spaces\\t.png' and bare filenames alike."""
    toks = rest.split()
    i, opts = 0, []
    while i < len(toks) and toks[i].startswith('-'):
        n = OPTS_N.get(toks[i], 1)
        opts += toks[i:i+1+n]; i += 1+n
    fname = ' '.join(toks[i:])                       # filenames may contain spaces
    return ' '.join(opts), fname.strip().strip('"')

def basename_any(p):
    """Basename for a path that may use Windows separators on a POSIX host."""
    return re.split(r'[\\/]', p.strip().strip('"'))[-1]

def build_index(dirs):
    """basename(lowercased) -> full path, for every image found under dirs."""
    idx = {}
    for d in dirs:
        if not d or not os.path.isdir(d): continue
        for root, _, files in os.walk(d):
            if '__MACOSX' in root: continue
            for f in files:
                if f.lower().endswith(IMG_EXT):
                    idx.setdefault(f.lower(), os.path.join(root, f))
    return idx

def find_replacement(raw, mtl_dir, idx):
    """Try, in order: as-written -> same basename -> same stem, any extension."""
    cand = os.path.join(mtl_dir, raw.replace('\\', os.sep))
    if os.path.isfile(cand): return cand, 'ok'
    base = basename_any(raw)
    hit = idx.get(base.lower())
    if hit: return hit, 'relinked by name'
    stem = os.path.splitext(base)[0].lower()
    for k, v in idx.items():
        if os.path.splitext(k)[0] == stem:
            return v, f'relinked by name, extension {os.path.splitext(k)[1]}'
    return None, 'MISSING'

def find_mtls(obj_path):
    """MTLs named by the OBJ, plus any sitting beside it (covers name mismatches)."""
    d = os.path.dirname(os.path.abspath(obj_path)) or '.'
    named, out = [], []
    try:
        with open(obj_path, errors='replace') as fh:
            _ls = fh.readlines()
        for line in _ls:
            if line.lower().startswith('mtllib'):
                rest = line.split(None, 1)[1].strip()
                # the whole remainder may be ONE name containing spaces; prefer that
                # if such a file exists, else fall back to whitespace-separated names
                named += [rest] if os.path.isfile(os.path.join(d, rest)) else rest.split()
    except Exception: pass
    for n in named:
        p = os.path.join(d, n)
        out.append((n, p, os.path.isfile(p)))
    for f in sorted(os.listdir(d)):
        if f.lower().endswith('.mtl') and all(os.path.abspath(os.path.join(d,f)) !=
                                              os.path.abspath(p) for _,p,_ in out):
            out.append((f, os.path.join(d, f), True))
    return out

def audit_mtl(mtl_path, idx, fix=False, collect=False):
    d = os.path.dirname(os.path.abspath(mtl_path))
    with open(mtl_path, errors='replace') as fh:
        lines = fh.read().splitlines()
    out, issues, changed, cur = [], [], 0, None
    for line in lines:
        s = line.strip()
        low = s.lower()
        key = low.split()[0] if s else ''
        if key == 'newmtl':
            cur = s.split(None,1)[1] if len(s.split())>1 else '?'
            out.append(line); continue
        if key in MAP_KEYS and len(s.split())>1:
            opts, raw = split_map_line(s.split(None,1)[1])
            new, status = find_replacement(raw, d, idx)
            issues.append((cur, key, raw, status, new))
            if fix and new:
                if collect:
                    os.makedirs(os.path.join(d,'maps'), exist_ok=True)
                    dst = os.path.join(d,'maps',os.path.basename(new))
                    if os.path.abspath(new) != os.path.abspath(dst): shutil.copy2(new, dst)
                    rel = os.path.join('maps', os.path.basename(new))
                else:
                    rel = os.path.relpath(new, d)
                rel = rel.replace(os.sep,'/')
                out.append(f"{s.split()[0]}{(' '+opts) if opts else ''} {rel}")
                if rel != raw: changed += 1
                continue
            out.append(line); continue
        # flag semantic traps
        if key == 'kd' and len(s.split())==4:
            try:
                v=[float(x) for x in s.split()[1:]]
                if max(v) < 0.99: issues.append((cur,'Kd',f"{v}",f"WARN darkens any texture by {100*(1-max(v)):.0f}%",None))
            except ValueError: pass
        out.append(line)
    if fix and changed:
        shutil.copy2(mtl_path, mtl_path + '.bak')
        with open(mtl_path,'w') as fh:
            fh.write('\n'.join(out) + '\n')
    return issues, changed

FBX_SLOT_FIX = {'specularcolor':'roughness','reflectioncolor':'metalness',
                'shininessexponent':'roughness'}

def audit_fbx(path, idx, fix=False):
    if read_fbx is None:
        print("   (fbxread.py not found beside texcheck.py - FBX skipped)"); return 0
    info = read_fbx(path)
    print(f"   [{info['format']} v{info['version']}]"
          + (f"  materials: {', '.join(info['materials'][:4])}" if info['materials'] else ''))
    if info['error']: print(f"   !! {info['error']}"); return 0
    if not info['textures']: print("   no texture references"); return 0
    dest_dir, placed, missing = os.path.dirname(os.path.abspath(path)), [], []
    for t in info['textures']:
        want = basename_any(t["file"])
        hit = idx.get(want.lower())
        if not hit:
            stem = os.path.splitext(want)[0].lower()
            for k, v in idx.items():
                if os.path.splitext(k)[0] == stem: hit = v; break
        short = t['file'] if len(t['file']) < 52 else '...' + t['file'][-49:]
        tag = 'OK  ' if hit and os.path.dirname(os.path.abspath(hit)) == dest_dir else ('FIX ' if hit else 'MISS')
        note = ''
        slot = (t['slot'] or '').lower()
        if slot in FBX_SLOT_FIX: note = f"   << likely wrong slot: holds a {FBX_SLOT_FIX[slot]} map"
        print(f"   [{tag}] {(t['material'] or '-')[:14]:14s} {(t['slot'] or '?'):22s} {short}{note}")
        if hit:
            dst = os.path.join(dest_dir, want)
            if fix and os.path.abspath(hit) != os.path.abspath(dst):
                shutil.copy2(hit, dst); placed.append(want)
        else:
            missing.append(want)
    if fix and placed:
        print(f"   placed {len(placed)} texture(s) beside the FBX under the names it asks for")
    if missing: print(f"   {len(missing)} not found: {', '.join(sorted(set(missing))[:4])}")
    return len(missing)

def audit_blend(path, idx, fix=False):
    if read_blend is None:
        print("   (blendread.py not found beside texcheck.py - .blend skipped)"); return 0
    info = read_blend(path)
    print(f"   [{info['compression']} v{info['version']}]  {len(info['images'])} image datablock(s)")
    if info['error']: print(f"   !! {info['error']}"); return 0
    d = os.path.dirname(os.path.abspath(path))
    mapping, ok, miss = {}, 0, []
    for im in info['images']:
        rel = im['path'][2:] if im['path'].startswith('//') else im['path']
        if os.path.isfile(os.path.join(d, rel)): ok += 1; continue
        base = basename_any(rel); hit = idx.get(base.lower())
        if not hit:
            stem = os.path.splitext(base)[0].lower()
            for k, v in idx.items():
                if os.path.splitext(k)[0] == stem: hit = v; break
        if hit:
            newrel = os.path.relpath(hit, d).replace(os.sep, '/')
            mapping[im['path']] = '//' + newrel
        else:
            miss.append(base)
    print(f"   resolve as written: {ok}   relinkable: {len(mapping)}   missing: {len(miss)}")
    if mapping and not fix:
        ex = list(mapping.items())[0]
        print(f"   e.g. {ex[0]}  ->  {ex[1]}")
    if fix and mapping:
        ch, toolong = repoint_blend(path, mapping)
        print(f"   rewrote {ch} path(s) in place (original kept as .bak)")
        if toolong: print(f"   {len(toolong)} too long for the fixed buffer, left alone")
    if miss: print(f"   not found: {', '.join(sorted(set(miss))[:4])}")
    return len(miss)

def main():
    ap = argparse.ArgumentParser(description="Find and fix broken texture links in OBJ/MTL.")
    ap.add_argument('target', help='an .obj, an .mtl, or a folder')
    ap.add_argument('--fix', action='store_true', help='rewrite MTLs (a .bak is kept)')
    ap.add_argument('--collect', action='store_true', help='with --fix, copy textures into ./maps/')
    ap.add_argument('--search', action='append', default=[], help='extra folder to search (repeatable)')
    a = ap.parse_args()

    t = os.path.abspath(a.target)
    root = t if os.path.isdir(t) else os.path.dirname(t)
    objs, mtls = [], []
    if os.path.isdir(t):
        for r,_,fs in os.walk(t):
            for f in fs:
                if f.lower().endswith('.obj'): objs.append(os.path.join(r,f))
                elif f.lower().endswith('.mtl'): mtls.append(os.path.join(r,f))
    elif t.lower().endswith(('.fbx','.blend')): objs=[]
    elif t.lower().endswith('.obj'): objs=[t]
    elif t.lower().endswith('.mtl'): mtls=[t]
    else: sys.exit('give me an .obj, an .mtl, or a folder')

    fbxs, blends = [], []
    if os.path.isdir(t):
        for r,_,fs in os.walk(t):
            for f in fs:
                if f.lower().endswith('.fbx'): fbxs.append(os.path.join(r,f))
                elif f.lower().endswith('.blend'): blends.append(os.path.join(r,f))
    elif t.lower().endswith('.fbx'): fbxs=[t]
    elif t.lower().endswith('.blend'): blends=[t]

    idx = build_index([root] + a.search)
    print(f"indexed {len(idx)} image files under {root}" + (f" (+{len(a.search)} extra)" if a.search else ""))

    for o in objs:
        print(f"\nOBJ  {os.path.relpath(o, root)}")
        found = find_mtls(o)
        if not found: print("   !! no MTL named and none found beside it")
        for name, path, exists in found:
            if not exists:
                print(f"   mtllib '{name}'  -> MISSING")
                sib=[n for n,p,e in found if e]
                if sib: print(f"      but these MTLs sit beside the OBJ: {sib}  <- likely a name mismatch")
            elif ' ' in name:
                print(f"   mtllib '{name}'  -> found, but the NAME CONTAINS SPACES (breaks many parsers)")
            if exists and path not in mtls: mtls.append(path)

    for fb in fbxs:
        print(f"\nFBX  {os.path.relpath(fb, root)}")
        audit_fbx(fb, idx, a.fix)

    for bl in blends:
        print(f"\nBLEND  {os.path.relpath(bl, root)}")
        audit_blend(bl, idx, a.fix)

    total_missing = 0
    for m in sorted(set(mtls)):
        issues, changed = audit_mtl(m, idx, a.fix, a.collect)
        print(f"\nMTL  {os.path.relpath(m, root)}")
        if not issues: print("   no texture references and nothing to flag")
        for mat, key, raw, status, new in issues:
            short = raw if len(raw) < 58 else '...'+raw[-55:]
            tag = 'OK  ' if status=='ok' else ('WARN' if status.startswith('WARN') else 'FIX ')
            if status=='MISSING': tag='MISS'; total_missing += 1
            print(f"   [{tag}] {str(mat)[:14]:14s} {key:9s} {short}")
            if status not in ('ok',) and not status.startswith('WARN') and new:
                print(f"          -> {os.path.relpath(new, os.path.dirname(m))}   ({status})")
            elif status.startswith('WARN'): print(f"          -> {status[5:]}")
            elif status=='MISSING': print( "          -> not found anywhere searched")
        if a.fix and changed: print(f"   rewrote {changed} path(s); original saved as {os.path.basename(m)}.bak")
    if total_missing: print(f"\n{total_missing} texture(s) could not be found. Use --search to add folders.")
    elif a.fix: print("\nall texture paths now resolve.")
    else: print("\nrun again with --fix to rewrite the paths.")

if __name__ == '__main__':
    main()
