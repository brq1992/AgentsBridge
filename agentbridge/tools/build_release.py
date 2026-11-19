"""Build a complete, reproducible AgentBridge release layout from this git tree.

    python tools/build_release.py --runtime <R7 agentbridge/runtime dir> --out <empty dir> [--zip <file>] [--site <dir>]

Without --site the layout is the public release. --site <dir> overlays every file under <dir>/package/
(site.json, hub-public-cert.pem, internal scripts and notes) onto the package root to make an internal
deployment package; the code is byte-identical either way. Site files may only add files, never replace
one from git, and the site directory must not be inside this repository.

Layout: <out>/agentbridge/ = `git archive` of this package directory at HEAD + runtime/ copied verbatim, plus
SOURCE_SHA256.json listing every file (raw-byte SHA-256, keys 'agentbridge/<path>'),
which upgrade-recovery.ps1 verifies. The Windows test suite runs from <out>/agentbridge
with runtime/python.exe; nothing is read from an installed copy.
"""
import argparse, hashlib, io, json, shutil, subprocess, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    a = argparse.ArgumentParser()
    a.add_argument('--runtime', required=True)
    a.add_argument('--out', required=True)
    a.add_argument('--zip')
    a.add_argument('--site')
    args = a.parse_args()
    runtime, out = Path(args.runtime), Path(args.out)
    if not (runtime / 'python.exe').is_file():
        sys.exit('runtime must contain python.exe')
    if out.exists() and any(out.iterdir()):
        sys.exit('out must be empty')
    if subprocess.run(['git', 'status', '--porcelain', '.'], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        sys.exit('commit first: the release is built from HEAD only')
    head = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dest = out / 'agentbridge'
    # The package may live in a subdirectory of the repository (e.g. agentbridge/): archive only it.
    prefix = subprocess.run(['git', 'rev-parse', '--show-prefix'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    tree = 'HEAD:' + prefix.rstrip('/') if prefix else 'HEAD'
    top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    data = subprocess.run(['git', 'archive', '--format=zip', tree], cwd=top, capture_output=True, check=True).stdout
    if len(data) < 1024:
        sys.exit('git archive produced no files')
    zipfile.ZipFile(io.BytesIO(data)).extractall(dest)
    shutil.copytree(runtime, dest / 'runtime', ignore=shutil.ignore_patterns('__pycache__'))
    site_files = overlay_site(Path(args.site), dest, Path(top)) if args.site else []
    manifest = {}
    for p in sorted(dest.rglob('*')):
        if p.is_file():
            manifest['agentbridge/' + p.relative_to(dest).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    (dest / 'SOURCE_SHA256.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf8')
    if args.zip:
        with zipfile.ZipFile(args.zip, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(dest.rglob('*')):
                if p.is_file():
                    z.write(p, p.relative_to(out).as_posix())
    print(json.dumps({'head': head, 'files': len(manifest), 'site_files': site_files, 'out': str(dest)}))


def overlay_site(site, dest, top):
    site = site.resolve()
    if site == top or top in site.parents:
        sys.exit('site directory must be outside the repository')
    src = site / 'package'
    if not src.is_dir():
        sys.exit('site directory must contain package/')
    added = []
    for p in sorted(src.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts:
            rel = p.relative_to(src)
            target = dest / rel
            if target.exists():
                sys.exit('site file would replace a released file: ' + rel.as_posix())
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            added.append(rel.as_posix())
    return added


if __name__ == '__main__':
    main()
