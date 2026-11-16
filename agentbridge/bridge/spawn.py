"""Start the Agent so its lifetime does not depend on the calling app.

An Agent started from an AI tool's shell (Claude, Codex) inherits that app's Windows job
object and is killed when the app exits. Windows can only break away when the job allows it,
so this module tries breakaway first and reports honestly when it was refused.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .store import BridgeError

CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def in_job(pid=None):
    """True/False on Windows; None where job objects do not exist."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    k.IsProcessInJob.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.OpenProcess(0x1000, False, int(pid or os.getpid()))
    if not handle:
        return None
    try:
        value = wintypes.BOOL()
        if not k.IsProcessInJob(handle, None, ctypes.byref(value)):
            return None
        return bool(value.value)
    finally:
        k.CloseHandle(handle)


def interpreter():
    """The real interpreter plus its import path (a venv python.exe is a redirector)."""
    return getattr(sys, '_base_executable', None) or sys.executable, list(sys.path)


def agent_command(install, state, hub_config):
    exe, path = interpreter()
    path = [str(install)] + [p for p in path if p != str(install)]
    bootstrap = ('import sys,json,runpy; sys.path[:]=json.loads(sys.argv.pop(1)); '
                 'sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")')
    return [exe, '-c', bootstrap, json.dumps(path), str(Path(install) / 'agentbridge.py'),
            '--state', str(state), 'agent', '--hub-config', str(hub_config)]


def spawn_detached(args, cwd, stdout, stderr, popen=subprocess.Popen):
    """Returns (process, breakaway). breakaway False means the child shares the caller's job."""
    common = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True)
    if os.name != 'nt':
        return popen(args, start_new_session=True, **common), None
    base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if in_job():
        try:
            return popen(args, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **common), True
        except OSError as e:
            if getattr(e, 'winerror', None) != 5:
                raise
            # The caller's job forbids breakaway: start anyway, but never claim independence.
            return popen(args, creationflags=base, **common), False
    return popen(args, creationflags=base, **common), True


def agent_start(state, hub_config=None, install=None, health=None, popen=subprocess.Popen, timeout=20):
    """Start the Agent unless one is already healthy for this state directory."""
    from .cli import call
    state = Path(state).resolve()
    install = Path(install or Path(__file__).resolve().parents[1])
    hub_config = Path(hub_config or state / 'network.json').resolve()
    if not hub_config.is_file():
        raise BridgeError('enroll the Agent first (network.json missing)')

    def probe():
        try:
            return (health or (lambda: call(state, '/health', {})))()
        except Exception:
            return None

    status = probe()
    if status:
        return {'action': 'already_running', 'pid': status['pid'], 'hub_connected': status.get('hub_connected'),
                'windows_job_member': status.get('windows_job_member')}
    lock = state / 'agent.lock'
    if lock.exists():
        from .server import try_lock
        held = try_lock(lock)
        if held is None:
            raise BridgeError('another Agent owns the state lock but does not answer; inspect its logs')
        held.close()
    with (state / 'Agent.stdout.log').open('ab') as out, (state / 'Agent.stderr.log').open('ab') as err:
        process, breakaway = spawn_detached(agent_command(install, state, hub_config), install, out, err, popen)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = probe()
        if status:
            result = {'action': 'started', 'pid': status['pid'], 'hub_connected': status.get('hub_connected'),
                      'windows_job_member': status.get('windows_job_member'), 'breakaway': breakaway}
            if breakaway is False:
                result['warning'] = ('Windows refused breakaway from the calling app; this Agent stops when that '
                                     'app exits. Restart it from File Explorer or a Start-menu PowerShell.')
            return result
        if process.poll() is not None:
            raise BridgeError('Agent exited during startup; see Agent.stderr.log')
        time.sleep(0.2)
    raise BridgeError('Agent did not answer within the startup timeout; see logs, do not start a second copy')
