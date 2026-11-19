"""On-device macOS transport check for write_unix (run by hand on the Mac, not part of unittest).

Uses a temporary AF_UNIX server in a separate child process and a dummy token; it never
touches ~/.claude, the Agent, the Hub or any real credential. Prints only booleans and sizes.

Usage:  <venv python3> tests/mac_device_check.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import adapters  # noqa: E402
from bridge.store import BridgeError  # noqa: E402

SERVER = r'''
import socket, sys, os, time
path, out, mode = sys.argv[1], sys.argv[2], sys.argv[3]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(path); os.chmod(path, 0o600); s.listen(1)
open(out + '.ready', 'w').close()
c, _ = s.accept()
data = b''
if mode == 'drop':
    c.close()                      # peer disconnects before reading anything
else:
    c.settimeout(3)
    try:
        while not data.endswith(b'\n') or data.count(b'\n') < 2:
            chunk = c.recv(65536)
            if not chunk: break
            data += chunk
    except Exception:
        pass
    if mode == 'receipt':
        c.sendall(b'{"type":"ack","ok":true}\n')
    time.sleep(0.3); c.close()
open(out, 'wb').write(data)
'''

DUMMY = 't' * 32
TEXT = 'mac device check 中文✓ 🙂'


def run_case(mode, peer):
    """peer: 'right' expects the real server pid, 'wrong' expects this process's pid."""
    d = tempfile.mkdtemp(prefix='abm1-')
    os.chmod(d, 0o700)
    sock, out = os.path.join(d, 's.sock'), os.path.join(d, 'got.bin')
    child = subprocess.Popen([sys.executable, '-c', SERVER, sock, out, mode])
    try:
        for _ in range(100):
            if os.path.exists(out + '.ready'):
                break
            time.sleep(0.05)
        pid = child.pid if peer == 'right' else os.getpid()
        start = adapters.process_start(pid)
        t0 = time.monotonic()
        try:
            r = adapters.write_unix(sock, DUMMY, TEXT, pid, start, receipt_wait=1.0)
            outcome = 'written:' + str(r.get('receipt'))[:40]
        except BridgeError as e:
            outcome = 'BridgeError:' + str(e)[:50]
        except OSError as e:
            outcome = 'OSError:' + type(e).__name__
        elapsed = round(time.monotonic() - t0, 2)
        child.wait(timeout=10)
        got = Path(out).read_bytes() if os.path.exists(out) else b''
        lines = [l for l in got.split(b'\n') if l]
        user_ok = False
        if len(lines) >= 2:
            try:
                user_ok = json.loads(lines[1])['message']['content'] == TEXT
            except Exception:
                user_ok = False
        return {'mode': mode, 'peer': peer, 'outcome': outcome, 'elapsed_s': elapsed,
                'server_bytes': len(got), 'unicode_roundtrip': user_ok}
    finally:
        if child.poll() is None:
            child.kill()
        for f in (sock, out, out + '.ready'):
            try:
                os.unlink(f)
            except OSError:
                pass
        os.rmdir(d)


if __name__ == '__main__':
    results = [run_case('read', 'right'), run_case('receipt', 'right'),
               run_case('read', 'wrong'), run_case('drop', 'right')]
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    expect = [
        results[0]['outcome'].startswith('written') and results[0]['unicode_roundtrip'],
        results[1]['outcome'].startswith('written'),
        results[2]['outcome'].startswith('BridgeError') and results[2]['server_bytes'] == 0,
        results[3]['elapsed_s'] < 5,   # disconnect must not hang; written or OSError both acceptable
    ]
    print('PASS' if all(expect) else 'FAIL', expect)
