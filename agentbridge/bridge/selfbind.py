"""bind-self: bind the calling conversation without the user looking up UUID, tool or project.

Run from inside the conversation's own shell by its AI, at the user's request. It needs the
Agent's private state (administrator capability), exactly like bind/grant-client; a
session-scoped client cannot run it. Identity is never guessed:
  - Codex: CODEX_THREAD_ID of the calling process.
  - Claude: the live registry entry (~/.claude/sessions/<pid>.json) whose pid is an ancestor of
    this process; discover_claude() has already checked the recorded process start time.
The only binding it can create is the caller's own. An alias held by another conversation is
refused by the store, never taken over.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from . import adapters
from .store import BridgeError

DEFAULT_PROJECT = 'agentbridge-m0'
ALIAS = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$')
PACKAGE = Path(__file__).resolve().parents[1]


def ancestors(pid=None):
    """PIDs from this process up to the root, nearest first (the process itself included)."""
    pid = os.getpid() if pid is None else pid
    parents = _parent_table()
    chain, seen = [], set()
    while pid and pid not in seen:
        chain.append(pid)
        seen.add(pid)
        pid = parents.get(pid)
    return chain


def _parent_table():
    if os.name == 'nt':
        return _windows_parents()
    out = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, check=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table


def _windows_parents():
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        raise BridgeError('cannot list processes to identify the calling conversation')
    table = {}
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            table[e.th32ProcessID] = e.th32ParentProcessID
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return table


def identify(environ=None, chain=None, entries=None):
    """(tool, native_uuid) of the calling conversation, or BridgeError. Never guesses."""
    environ = os.environ if environ is None else environ
    thread = environ.get('CODEX_THREAD_ID')
    if thread:
        return 'codex', thread
    chain = ancestors() if chain is None else chain
    entries = adapters.discover_claude() if entries is None else entries
    for pid in chain:
        found = [e for e in entries if e.get('pid') == pid]
        if found:
            return 'claude', found[0]['sessionId']
    raise BridgeError('cannot identify the calling conversation: run bind-self from the Claude or Codex '
                      'conversation itself (no CODEX_THREAD_ID and no live Claude registry entry for '
                      'any parent process)')


def default_project():
    site = PACKAGE / 'site.json'
    if site.is_file():
        value = json.loads(site.read_text(encoding='utf-8-sig')).get('project')
        if value:
            return value
    return DEFAULT_PROJECT


def default_client_directory(state, alias):
    """Outside the private state and outside any repository: <state parent>/clients/<alias>/agentbridge-client."""
    return Path(state).resolve().parent / 'clients' / alias / 'agentbridge-client'


def command_prefix(client, tool, native):
    python = Path(getattr(sys, '_base_executable', sys.executable)).as_posix()
    launcher = (PACKAGE / 'agentbridge.py').as_posix()
    return f'"{python}" "{launcher}" --state "{Path(client).as_posix()}" --tool {tool} --session {native}'


def bind_self(state, alias, project=None, client_directory=None, call=None, identity=None):
    if not ALIAS.match(alias or ''):
        raise BridgeError('alias must be 1-32 letters, digits, _ or -, starting with a letter or digit')
    if call is None:
        from .cli import call
    tool, native = identity or identify()
    project = project or default_project()
    client = Path(client_directory).resolve() if client_directory else default_client_directory(state, alias)
    body = {'actor': f'{tool}:{native}', 'context': {'cli_pid': os.getpid()},
            'tool': tool, 'native_id': native, 'alias': alias, 'project': project}
    if tool == 'codex':
        body.update(cli=shutil.which('codex'),
                    codex_home=os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        if not body['cli']:
            raise BridgeError('codex CLI not found on PATH')
    bound = call(state, '/bind', body)
    call(state, '/grant', {'actor': body['actor'], 'context': body['context'],
                           'identity': alias, 'directory': str(client), 'reader_sid': None})
    network = Path(state) / 'network.json'
    label = json.loads(network.read_text(encoding='utf8')).get('label') if network.is_file() else None
    return {'address': f'{label}/{alias}' if label else alias, 'tool': tool, 'session': native,
            'project': project, 'generation': bound.get('generation'), 'client_directory': str(client),
            'command_prefix': command_prefix(client, tool, native)}
