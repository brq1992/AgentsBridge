"""Build a complete, reproducible AgentBridge release layout from this git tree.

    python tools/build_release.py --runtime <R7 agentbridge/runtime dir> --out <empty dir> [--zip <file>]

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
    print(json.dumps({'head': head, 'files': len(manifest), 'out': str(dest)}))


if __name__ == '__main__':
    main()
