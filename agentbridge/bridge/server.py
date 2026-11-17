import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from . import adapters
from .store import BridgeError, Store


def write_client_config(path, data):
    temporary = path.with_suffix('.tmp')
    if os.name == 'nt':
        temporary.write_text(json.dumps(data), encoding='utf8')
    else:
        # Token file is created owner-only; never rely on the process umask.
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as stream:
            stream.write(json.dumps(data))
    temporary.replace(path)


def git_worktree(directory):
    """The enclosing git working tree root, or None."""
    for parent in [directory, *directory.parents]:
        if (parent / '.git').exists():
            return parent
    return None


def check_client_directory(directory):
    """Refuse token folders that a routine `git add -A` would publish."""
    root = git_worktree(directory)
    if root is None:
        return
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        # Ignore rules never apply to files already in the index: a tracked token keeps
        # being committed even after the folder is excluded. Refuse any indexed file there.
        tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '--', str(directory)],
                                 capture_output=True, text=True, timeout=15, creationflags=flags)
        if tracked.returncode != 0:
            raise BridgeError('cannot verify the git index for the client directory')
        if tracked.stdout.strip():
            raise BridgeError('client directory has files tracked by git (' + str(root) + '); run '
                              '"git rm --cached -r" on it, treat any committed token as exposed, then grant again')
        # Without --no-index, check-ignore reports only untracked paths that a rule ignores.
        ignored = subprocess.run(['git', '-C', str(root), 'check-ignore', '-q',
                                  str(directory / 'client.json')], capture_output=True, timeout=15,
                                 creationflags=flags).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ignored = False
    if not ignored:
        raise BridgeError('client directory is inside the git working tree ' + str(root) +
                          ' and not ignored; choose a folder outside the repository or add it to .git/info/exclude')


def try_lock(path):
    """Exclusive non-blocking lock on the state directory; the open stream or None."""
    lock = Path(path).open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock
    except OSError:
        lock.close()
        return None


def run(state, hub_config=None):
    state = Path(state).resolve()
    from .provision import certificate_expiry, private_directory, save_private
    from .spawn import in_job
    private_directory(state)
    lock = try_lock(state / 'agent.lock')
    if lock is None:
        raise BridgeError('another coordinator owns this state directory')
    adapters.configure(state)
    store = Store(state / 'ledger.sqlite')
    store.recover()
    token = secrets.token_urlsafe(32)
    stop = threading.Event()
    def lifecycle(event, **detail):
        print(json.dumps({'event': event, 'at': time.time(), 'pid': os.getpid(), **detail}),
              file=sys.stderr, flush=True)
    remote = None
    if hub_config:
        from .network import RemoteAgent
        remote = RemoteAgent(store, state, hub_config, stop)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            auth = self.headers.get('Authorization', '')
            admin = hmac.compare_digest(auth, 'Bearer ' + token)
            principal = None if admin else store.authenticate_client(auth.removeprefix('Bearer '))
            if self.headers.get('Origin') or (not admin and not principal):
                self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 256 * 1024:
                    raise BridgeError('invalid request size')
                body = json.loads(self.rfile.read(length))
                result = handle(self.path, body, principal)
                status, output = 200, {'ok': True, 'result': result}
            except (BridgeError, ValueError, KeyError, OSError) as e:
                status, output = 400, {'ok': False, 'error': str(e)}
            except Exception as e:
                # No raw payload, token or traceback is returned in errors.
                status, output = 500, {'ok': False, 'error': 'internal error: ' + type(e).__name__}
            data = json.dumps(output, ensure_ascii=False).encode('utf8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def handle(path, b, principal=None):
        if principal:
            if path not in ('/health', '/send', '/ack', '/ack-receipt', '/reply', '/status', '/sessions'):
                raise BridgeError('session capability cannot perform administrative operations')
            if path in ('/send', '/ack', '/ack-receipt', '/reply') or (remote and path == '/status' and b.get('message_id')):
                if b.get('actor') != principal['id']:
                    raise BridgeError('session capability cannot impersonate another binding')
            if path == '/sessions':
                if remote:
                    return remote.forward(path, b, principal)
                return [s for s in store.sessions() if s['project'] == principal['project']]
            if path == '/status':
                if remote:
                    if not b.get('message_id'):
                        return {'connected': remote.connected, 'scope': 'use --message for own message status'}
                    return remote.forward(path, b, principal)
                return [m for m in store.status(b.get('message_id'))
                        if principal['id'] in (m['sender'], m['receiver'])]
        if path == '/health':
            if remote:
                result = {'mode': 'windows-m1-network', 'pid': os.getpid(), 'protocol': '1.3',
                          'hub_connected': remote.connected, 'node': remote.label,
                          'recovery_interval_seconds': remote.recovery_interval,
                          'endpoint_health': ({principal['id']: remote.endpoint_health.get(principal['id'], {'state': 'checking'})}
                                              if principal else dict(remote.endpoint_health))}
                if not principal:
                    warnings = []
                    member = in_job()
                    result['windows_job_member'] = member
                    if member:
                        warnings.append('Agent runs inside a Windows job; if it was started from an AI tool shell '
                                        'it stops when that app exits')
                    try:
                        expiry = certificate_expiry(remote.config['hub_certificate'])
                        result['hub_certificate'] = expiry
                        if expiry['days_left'] <= 14:
                            warnings.append('Hub certificate expires in ' + str(expiry['days_left']) + ' days')
                    except Exception:
                        pass
                    try:
                        waiting = remote.unclaimed()
                        result['unclaimed_messages'] = waiting
                        if waiting:
                            warnings.append(str(len(waiting)) + ' delivered message(s) not yet claimed by the '
                                            'receiving session; they are re-notified when it is idle')
                    except Exception:
                        pass
                    result['warnings'] = warnings
                return result
            return {'mode': 'windows-m0-local-only', 'pid': os.getpid(), 'protocol': '1.2'}
        if path == '/ack-receipt':
            if not remote:
                raise BridgeError('claim receipts exist only in M1 network mode')
            return remote.receipt(b)
        if remote and path in ('/sessions', '/send', '/ack', '/reply', '/status'):
            return remote.forward(path, b, principal)
        if path == '/discover':
            return adapters.discover_claude()
        if path == '/refresh-claude':
            if not remote:
                raise BridgeError('explicit endpoint refresh is available in M1 mode')
            with store.connect() as c:
                original = store._session(c, b['identity'])
            if original['tool'] != 'claude':
                raise BridgeError('only Claude endpoints can be refreshed')
            metadata = adapters.claude_metadata(original['native_id'])
            verified = dict(original, metadata=json.dumps(metadata))
            adapters.claude_preflight(verified)  # Exact UUID, live process, qualified version and matching key.
            refreshed = store.refresh_endpoint(original['id'], original['generation'], metadata)
            retries = remote.retry_preflight(refreshed['id'])
            return {'session_id': refreshed['id'], 'generation': refreshed['generation'],
                    'pid': metadata['pid'], 'retry_queued': retries}
        if path == '/refresh-codex':
            with store.connect() as c:
                original = store._session(c, b['identity'])
            if original['tool'] != 'codex':
                raise BridgeError('only Codex bindings use refresh-codex')
            old = json.loads(original['metadata'])
            cli = b.get('cli')
            if not cli:
                candidates = adapters.codex_candidates(old['cli'])
                if len(candidates) != 1:
                    raise BridgeError('pass --cli explicitly; installed Codex CLIs found: ' + json.dumps(candidates))
                cli = candidates[0]
            version = adapters.verify_codex_cli(cli)
            metadata = {'cli': str(Path(cli).resolve()), 'codex_home': old['codex_home']}
            refreshed = store.refresh_endpoint(original['id'], original['generation'], metadata)
            retries = remote.retry_preflight(refreshed['id']) if remote else []
            return {'session_id': refreshed['id'], 'generation': refreshed['generation'], 'cli': metadata['cli'],
                    'cli_version': version, 'retry_queued': retries}
        if path == '/qualify-claude':
            version = b['version']
            if not isinstance(version, str) or not version or len(version) > 32:
                raise BridgeError('invalid version')
            live = [r for r in adapters.discover_claude() if r.get('version') == version]
            if not live:
                raise BridgeError('no live Claude session with that version to check')
            for entry in live:
                adapters.claude_structure(entry['registry'])
            target = state / adapters.QUALIFIED_FILE
            try:
                approved = json.loads(target.read_text(encoding='utf8')).get('versions', [])
            except (OSError, ValueError):
                approved = []
            approved = sorted(set(approved) | {version})
            save_private(target, {'versions': approved,
                                  'note': 'approved locally by qualify-claude after structural check'})
            return {'qualified': version, 'checked_sessions': len(live), 'local_versions': approved}
        if path == '/bind':
            tool = b['tool']
            if tool == 'claude':
                metadata = adapters.claude_metadata(b['native_id'])
            elif tool == 'codex':
                metadata = {'cli': b['cli'], 'codex_home': b['codex_home']}
                if not Path(metadata['cli']).is_file():
                    raise BridgeError('CLI does not exist')
            else:
                raise BridgeError('unsupported tool')
            return store.bind(tool, b['native_id'], b['alias'], b['project'], metadata)
        if path == '/sessions':
            return store.sessions()
        if path == '/unbind':
            return store.unbind(b['identity'])
        if path == '/grant':
            directory = Path(b['directory']).resolve()
            if directory == state or state in directory.parents:
                raise BridgeError('client directory must be separate from private coordinator state')
            check_client_directory(directory)
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != 'nt':
                directory.chmod(0o700)
            if os.name == 'nt':
                # Protect this newly designated client directory, never the workspace or user root.
                if directory.name != 'agentbridge-client':
                    raise BridgeError('client directory must be named agentbridge-client')
                import csv
                user = list(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv'], text=True).splitlines()))[-1][1]
                grants = ['icacls', str(directory), '/inheritance:r', '/grant:r',
                          f'*{user}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F']
                if b.get('reader_sid'):
                    sid = b['reader_sid']
                    if not sid.startswith('S-1-') or not all(part.isdigit() for part in sid[2:].split('-')):
                        raise BridgeError('invalid Windows SID')
                    # /grant:r replaces an existing ACE. Never replace the service
                    # owner's F grant with RX when Codex runs as that same user.
                    if sid not in (user, 'S-1-5-18'):
                        grants.append(f'*{sid}:(OI)(CI)RX')
                subprocess.run(grants, check=True, capture_output=True,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            client_token = secrets.token_urlsafe(32)
            sid = store.add_client(b['identity'], str(directory), client_token,
                publish=lambda: write_client_config(directory / 'client.json', {'url': base_url, 'token': client_token}))
            return {'session_id': sid, 'client_directory': str(directory), 'scope': 'session-only'}
        if path == '/send':
            return store.send(b['actor'], b['to'], b['body'], b.get('message_id'), b.get('ttl', 86400))
        if path == '/ack':
            return store.ack(b['message_id'], b['actor'], b.get('context'))
        if path == '/reply':
            return store.reply(b['message_id'], b['actor'], b['body'], b.get('outcome', 'completed'))
        if path == '/status':
            return store.status(b.get('message_id'))
        if path == '/stop':
            lifecycle('stop_requested')
            stop.set()
            return {'stopping': True}
        raise BridgeError('unknown endpoint')

    def worker():
        while not stop.is_set():
            try:
                item = store.next_delivery()
                if item:
                    m, s = item
                    try:
                        prepared = adapters.prepare(s, adapters.envelope(m, s, store.client_directory(s['id'], state)))
                    except Exception as e:
                        store.finish_submission(m['id'], 'blocked', {'reason': str(e)})
                        continue
                    if not store.begin_submission(m['id']):
                        continue
                    try:
                        detail = adapters.deliver(prepared)
                        store.finish_submission(m['id'], 'submitted', detail)
                    except Exception as e:
                        store.finish_submission(m['id'], 'delivery_uncertain', {'reason': str(e)})
                else:
                    stop.wait(0.3)
            except Exception as e:
                with (state / 'errors.log').open('a', encoding='utf8') as log:
                    log.write(f'{time.time()} worker {type(e).__name__}\n')
                stop.wait(1)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 0.3
    server.daemon_threads = True
    base_url = f'http://127.0.0.1:{server.server_port}'
    client_path = state / 'client.json'
    temporary = state / 'client.tmp'
    temporary.write_text(json.dumps({'url': base_url, 'token': token}), encoding='utf8')
    temporary.replace(client_path)
    for client in store.clients():
        path = Path(client['directory']) / 'client.json'
        if path.exists():
            config = json.loads(path.read_text(encoding='utf8'))
            config['url'] = base_url
            write_client_config(path, config)
    def guarded_worker(target):
        try:
            target()
        except BaseException as exc:
            lifecycle('worker_failed', worker=target.__name__, error_type=type(exc).__name__)
            stop.set()
        else:
            if not stop.is_set():
                lifecycle('worker_exited_unexpectedly', worker=target.__name__)
                stop.set()

    workers = [threading.Thread(target=guarded_worker, args=(target,), daemon=True) for target in
               ([remote.connection_loop, remote.delivery_loop] if remote else [worker])]
    for worker_thread in workers:
        worker_thread.start()
    print(json.dumps({'ready': True, 'pid': os.getpid(), 'port': server.server_port}), flush=True)
    lifecycle('agent_started')
    heartbeat_at = 0.0
    try:
        while not stop.is_set():
            server.handle_request()
            if time.monotonic() >= heartbeat_at:
                lifecycle('agent_heartbeat', hub_connected=remote.connected if remote else None,
                          workers_alive=sum(t.is_alive() for t in workers))
                heartbeat_at = time.monotonic() + 30
    except BaseException as exc:
        lifecycle('main_failed', error_type=type(exc).__name__)
        raise
    finally:
        lifecycle('agent_stopping')
        stop.set()
        for worker_thread in workers:
            worker_thread.join(timeout=30)
        server.server_close()
        lock.close()
        lifecycle('agent_stopped')
