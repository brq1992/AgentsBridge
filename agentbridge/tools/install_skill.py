"""Install the agentbridge skill for this user (Claude Code and Codex), filled with local paths.

    python tools/install_skill.py --state <Agent state dir> [--target <skills dir> ...]

Default targets: ~/.claude/skills and ~/.codex/skills. Writes <target>/agentbridge/SKILL.md only;
an existing different file is backed up next to it first. Contains paths, never credentials.
"""
import argparse
import json
from pathlib import Path
import sys
import time

PACKAGE = Path(__file__).resolve().parents[1]
SOURCE = PACKAGE / 'skills' / 'agentbridge' / 'SKILL.md'


def render(state, python=None):
    state = Path(state).resolve()
    values = {'{{PYTHON}}': Path(python or getattr(sys, '_base_executable', sys.executable)).as_posix(),
              '{{LAUNCHER}}': (PACKAGE / 'agentbridge.py').as_posix(),
              '{{STATE}}': state.as_posix(),
              '{{CLIENTS}}': (state.parent / 'clients').as_posix()}
    text = SOURCE.read_text(encoding='utf8')
    for key, value in values.items():
        text = text.replace(key, value)
    if '{{' in text:
        raise SystemExit('unfilled placeholder in skill template')
    return text


def install(text, targets):
    written = []
    for target in targets:
        path = Path(target).expanduser() / 'agentbridge' / 'SKILL.md'
        if path.is_file():
            if path.read_text(encoding='utf8') == text:
                written.append({'path': str(path), 'changed': False})
                continue
            path.with_name(f'SKILL.md.bak-{time.strftime("%Y%m%d%H%M%S")}').write_bytes(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf8')
        written.append({'path': str(path), 'changed': True})
    return written


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--state', required=True)
    p.add_argument('--python', help='Interpreter the skill runs (default: this one)')
    p.add_argument('--target', action='append', help='Skills directory; repeatable')
    a = p.parse_args(argv)
    targets = a.target or [Path.home() / '.claude' / 'skills', Path.home() / '.codex' / 'skills']
    print(json.dumps({'ok': True, 'installed': install(render(a.state, a.python), targets)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
