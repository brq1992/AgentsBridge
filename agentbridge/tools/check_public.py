"""Refuse to publish environment-specific data. Scans tracked files (or the given paths).

    python tools/check_public.py [--deny-file <site.json>] [paths...]

Built-in rules: private IPv4 ranges (RFC 1918), e-mail addresses other than noreply/example,
Windows account SIDs, real user profile paths. --deny-file (or AGENTBRIDGE_SITE) reads the
site.json "deny" list for site words (machine names, internal domains) that must not appear.
Exit 1 with file:line for every hit; prints nothing else of the matched text.
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

RULES = {
    'private_ip': re.compile(r'(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])'),
    'email': re.compile(r'[\w.+-]+@(?!(?:users\.)?noreply\.|example\.|anthropic\.com)[\w-]+\.[\w.]+'),
    'account_sid': re.compile(r'S-1-5-21-\d+-\d+-\d+-\d+'),
    'user_path': re.compile(r'(?i)[A-Z]:\\?Users\\?(?!<|USER|Public|\.\.\.)[\w.]+'),
}


def tracked(top):
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=top, capture_output=True, check=True).stdout
    return [Path(top, p) for p in out.decode('utf8').split('\0') if p]


def main(argv=None):
    a = argparse.ArgumentParser()
    a.add_argument('--deny-file', default=os.environ.get('AGENTBRIDGE_SITE'))
    a.add_argument('paths', nargs='*')
    args = a.parse_args(argv)
    rules = dict(RULES)
    if args.deny_file:
        words = [w for w in json.loads(Path(args.deny_file).read_text(encoding='utf8')).get('deny', []) if w]
        if words:
            rules['site_word'] = re.compile('|'.join(re.escape(w) for w in words), re.I)
    top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip()
    files = [Path(p) for p in args.paths] if args.paths else tracked(top)
    hits = 0
    for f in files:
        try:
            text = f.read_bytes().decode('utf8')
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in rules.items():
                if rx.search(line):
                    print(f'{f}:{n}: {name}')
                    hits += 1
    if hits:
        print(f'{hits} environment-specific value(s); move them to the site directory', file=sys.stderr)
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
