"""Windows user-launched Agent entry point. No service or privilege changes."""
import argparse
import ctypes
from ctypes import wintypes
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


class LaunchError(Exception):
    pass


def in_job(pid):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    k.OpenProcess.restype=wintypes.HANDLE
    k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
    k.IsProcessInJob.restype=wintypes.BOOL
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.OpenProcess(0x1000,False,pid)
    if not handle:
        raise LaunchError('Cannot verify Agent process identity/lifetime.')
    try:
        value=wintypes.BOOL()
        if not k.IsProcessInJob(handle,None,ctypes.byref(value)):
            raise LaunchError('Cannot verify Windows job membership.')
        return bool(value.value)
    finally:
        k.CloseHandle(handle)


def health(state):
    try:
        config=json.loads((state/'client.json').read_text(encoding='utf-8-sig'))
        from urllib.parse import urlsplit
        parsed=urlsplit(config['url'])
        if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port:
            return None
        request=urllib.request.Request(config['url']+'/health',b'{}',
                 {'Authorization':'Bearer '+config['token'],'Content-Type':'application/json'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=2) as response:
            result=json.load(response)
        if result.get('ok') and result['result'].get('mode')=='windows-m1-network':
            return result['result']
    except Exception:
        pass
    return None


def take_lock(path,timeout=0):
    stream=path.open('a+b')
    # Reading a byte already locked by another process raises PermissionError.
    # File size is safe to inspect while a competing launcher owns the byte lock.
    if os.fstat(stream.fileno()).st_size==0:
        stream.write(b'0'); stream.flush()
    deadline=time.monotonic()+timeout
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            return stream
        except OSError:
            if time.monotonic()>=deadline:
                stream.close()
                return None
            time.sleep(.1)


def unlock(stream):
    stream.seek(0)
    msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    stream.close()


def load_config(path):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    result={name:Path(os.path.expandvars(data[name])).resolve() for name in ('install','state','python')}
    for name in ('install','state'):
        if not result[name].is_dir():
            raise LaunchError('Configured '+name+' directory does not exist; edit the selected profile.')
    for path in (result['python'],result['install']/'agentbridge.py',result['state']/'network.json'):
        if not path.is_file():
            raise LaunchError('Required runtime/installation/enrollment file is missing; edit the selected profile.')
    return result


def describe(status,action):
    pid=int(status['pid'])
    member=in_job(pid)
    return {'action':action,'pid':pid,'hub_connected':status.get('hub_connected',False),
            'windows_job_member':member,
            'independent_lifetime_verified':not member,
            'endpoint_health':status.get('endpoint_health',{}),
            'note':('Agent is in a Windows job; membership alone does not establish who owns it. '
                    'Verify app-close survival with this PID; no independence claim is made.' if member else
                    'Agent has no Windows job membership; verify application close/reopen separately.')}


def launch(config,status_only=False):
    state=config['state']
    if status_only:
        status=health(state)
        if not status:
            raise LaunchError('Agent is not responding. Status check did not start or stop anything.')
        return describe(status,'status')
    gate=take_lock(state/'independent-launcher.lock',timeout=35)
    if gate is None:
        raise LaunchError('Another launcher is busy; no additional Agent was started.')
    try:
        status=health(state)
        if status:
            return describe(status,'already_running')
        owner=take_lock(state/'agent.lock')
        if owner is None:
            raise LaunchError('Agent owns its state lock but health is unavailable. No duplicate was started; inspect its logs.')
        unlock(owner)
        check=subprocess.run([str(config['python']),'-c',
                 'import websockets.sync.client, cryptography, sys, json; print(json.dumps({"exe":sys._base_executable,"path":sys.path}))'],
                 stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=10,
                 creationflags=subprocess.CREATE_NO_WINDOW)
        if check.returncode:
            raise LaunchError('Selected Python lacks Agent network dependencies; nothing was started.')
        runtime=json.loads(check.stdout)
        # Windows venv python.exe is a redirector that may create its own job.
        # Start the verified base interpreter with that runtime's import paths,
        # avoiding the redirector while retaining its dependency environment.
        bootstrap='import sys,json,runpy; sys.path[:]=json.loads(sys.argv.pop(1)); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__")'
        runtime['path'].insert(0,str(config['install']))
        args=[runtime['exe'],'-c',bootstrap,json.dumps(runtime['path']),str(config['install']/'agentbridge.py'),'--state',str(state),
              'agent','--hub-config',str(state/'network.json')]
        # Breakaway must succeed, never fall back to an app-owned job. Detached
        # console means closing this launcher window cannot send Agent Ctrl+C.
        flags=0x01000000 | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            with (state/'Agent.stdout.log').open('ab') as stdout, (state/'Agent.stderr.log').open('ab') as stderr:
                child=subprocess.Popen(args,cwd=config['install'],stdin=subprocess.DEVNULL,
                       stdout=stdout,stderr=stderr,close_fds=True,creationflags=flags)
        except OSError as exc:
            raise LaunchError('Windows refused independent creation (error '+str(exc.winerror)+
                              '). Launch from File Explorer or a Start-menu PowerShell; no fallback was used.') from None
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            status=health(state)
            if status:
                # Another legacy launcher could win the Agent's own lock. Never
                # kill an existing process or claim its lifetime has been repaired.
                result=describe(status,'started' if child.poll() is None else 'already_running')
                return result
            if child.poll() is not None:
                raise LaunchError('Agent exited during startup; inspect Agent.stderr.log. No retry or second Agent was started.')
            time.sleep(.2)
        raise LaunchError('Agent startup health timed out; inspect logs. Do not start a second copy.')
    finally:
        unlock(gate)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--status',action='store_true')
    args=parser.parse_args()
    try:
        result=launch(load_config(args.config),args.status)
        print(json.dumps({'ok':True,'result':result},ensure_ascii=False),flush=True)
    except Exception as exc:
        message=str(exc) if isinstance(exc,LaunchError) else 'Launcher failed: '+type(exc).__name__
        print(json.dumps({'ok':False,'error':message}),flush=True)
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
