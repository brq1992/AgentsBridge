"""Real PowerShell updater failure paths against an isolated install:
- the upgraded Agent never becomes healthy (Hub disappears after the pre-check);
- a file copy fails in the middle of the install;
- an independent start is impossible here and -AllowAttached was not given.
Each must leave the previous files (hash-verified) and a newly started previous Agent,
or change nothing at all."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from bridge.cli import call
from bridge.hub import HubStore, create_tls, serve_hub
from bridge.spawn import breakaway_possible

SOURCE = Path(__file__).resolve().parents[1]
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


@unittest.skipUnless(os.name == 'nt', 'Windows installer')
class UpgradeFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / 'installed'
        self.install.mkdir()
        for name in ('bridge', 'runtime'):
            shutil.copytree(SOURCE / name, self.install / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('agentbridge.py', 'start-m1.ps1'):
            shutil.copy2(SOURCE / name, self.install / name)
        self.marker = self.install / 'bridge' / 'store.py'
        self.marker.write_text(self.marker.read_text() + '\n# previous-release-marker\n')
        self.previous = hashlib.sha256(self.marker.read_bytes()).hexdigest()
        self.state = self.root / 'state'
        self.state.mkdir()
        create_tls(self.root, ['127.0.0.1'])
        hub = HubStore(self.root / 'hub.sqlite')
        ready = threading.Event()
        self.servers = []
        def on_ready(server):
            self.servers.append(server)
            ready.set()
        self.hub_thread = threading.Thread(target=serve_hub, args=(hub, '127.0.0.1', 0, self.root / 'hub-cert.pem',
                                                                   self.root / 'hub-key.pem', on_ready), daemon=True)
        self.hub_thread.start()
        self.assertTrue(ready.wait(5))
        node = hub.add_node('rollback-test', ['echo'])
        (self.state / 'network.json').write_text(json.dumps(node | {
            'hub_url': f'wss://127.0.0.1:{self.servers[0].socket.getsockname()[1]}',
            'hub_certificate': (self.root / 'hub-cert.pem').read_text()}))
        self.log = (self.root / 'agent.stderr').open('w')
        self.process = subprocess.Popen([str(self.install / 'runtime/python.exe'), str(self.install / 'agentbridge.py'),
                                         '--state', str(self.state), 'agent', '--hub-config', str(self.state / 'network.json')],
                                        stdout=subprocess.DEVNULL, stderr=self.log, creationflags=FLAGS)
        deadline = time.monotonic() + 12
        while not self.health(connected=True):
            if time.monotonic() > deadline:
                self.fail('Agent failed to start')
            time.sleep(.1)
        self.original_pid = self.health()['pid']

    def tearDown(self):
        try:
            call(self.state, '/stop', {})
        except Exception:
            pass
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=10)
        deadline = time.monotonic() + 15
        while self.health() and time.monotonic() < deadline:
            time.sleep(.2)
        self.log.close()
        if self.hub_thread.is_alive():
            self.servers[0].shutdown()
            self.hub_thread.join(5)
        self.temporary.cleanup()

    def health(self, connected=False):
        try:
            result = call(self.state, '/health', {})
        except Exception:
            return None
        if connected and not result.get('hub_connected'):
            return None
        return result

    def upgrade(self, *extra):
        output = self.root / 'upgrade.txt'
        with output.open('w') as stream:
            code = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                   str(SOURCE / 'upgrade-recovery.ps1'), '-InstallDirectory', str(self.install),
                                   '-State', str(self.state), *extra], stdout=stream, stderr=stream, timeout=240,
                                  creationflags=FLAGS).returncode
        return code, output.read_text()

    def assert_rolled_back(self, code, text):
        self.assertNotEqual(code, 0, text)
        self.assertIn('rolled back', text)
        self.assertIn('hash-verified', text)
        self.assertEqual(hashlib.sha256(self.marker.read_bytes()).hexdigest(), self.previous)
        self.assertFalse((self.install / 'configure-m1-session.py').is_file(), 'files new in the release must be removed')
        restarted = self.health()
        self.assertIsNotNone(restarted, 'previous Agent was not restarted: ' + text)
        self.assertNotEqual(restarted['pid'], self.original_pid)
        self.assertIn('PID ' + str(restarted['pid']), text)

    def test_unhealthy_upgraded_agent_rolls_back(self):
        self.servers[0].shutdown()
        self.hub_thread.join(5)
        self.assert_rolled_back(*self.upgrade('-AllowAttached'))

    def test_folder_in_place_of_release_file_changes_nothing(self):
        blocker = self.install / 'tests'
        blocker.mkdir()
        (blocker / 'test_r5.py').mkdir()
        code, text = self.upgrade('-AllowAttached')
        self.assertNotEqual(code, 0, text)
        self.assertIn('Nothing was changed', text)
        self.assertEqual(hashlib.sha256(self.marker.read_bytes()).hexdigest(), self.previous)
        self.assertEqual(self.health()['pid'], self.original_pid)

    def test_copy_failure_after_stop_is_reported_not_hidden(self):
        # Another program holds store.py open read-only (like an editor or scanner): the
        # backup can read it, but the install copy fails after the stop, and restoring that
        # same file fails too. The updater must say the rollback is incomplete, leave the
        # Agent stopped, keep the previous content and remove files new in the release.
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        handle = k.CreateFileW(str(self.marker), 0x80000000, 0x1, None, 3, 0, None)   # GENERIC_READ, FILE_SHARE_READ
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            code, text = self.upgrade('-AllowAttached')
        finally:
            k.CloseHandle(handle)
        self.assertNotEqual(code, 0, text)
        self.assertIn('ROLLBACK IS INCOMPLETE', text)
        self.assertIn('bridge/store.py', text)
        self.assertEqual(hashlib.sha256(self.marker.read_bytes()).hexdigest(), self.previous)
        self.assertFalse((self.install / 'configure-m1-session.py').is_file())
        self.assertIsNone(self.health(), 'Agent must be left stopped when the rollback is incomplete')

    def test_without_independent_start_nothing_changes(self):
        if breakaway_possible():
            self.skipTest('this environment allows an independent start')
        code, text = self.upgrade()
        self.assertNotEqual(code, 0, text)
        self.assertIn('Nothing was changed', text)
        self.assertEqual(hashlib.sha256(self.marker.read_bytes()).hexdigest(), self.previous)
        self.assertEqual(self.health()['pid'], self.original_pid)


if __name__ == '__main__':
    unittest.main()
