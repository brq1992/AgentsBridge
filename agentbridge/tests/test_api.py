import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from bridge.cli import call
from bridge.store import BridgeError


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.temp.name)
        root = Path(__file__).resolve().parents[1]
        cls.process = subprocess.Popen([sys.executable, str(root / 'agentbridge.py'), '--state', str(cls.state), 'agent'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 10
        while not (cls.state / 'client.json').exists():
            if cls.process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('test coordinator did not start')
            time.sleep(0.05)
        cls.config = json.loads((cls.state / 'client.json').read_text())

    @classmethod
    def tearDownClass(cls):
        call(cls.state, '/stop', {})
        cls.process.wait(timeout=10)
        cls.temp.cleanup()

    def test_health(self):
        self.assertEqual(call(self.state, '/health', {})['protocol'], '1.2')

    def test_missing_token_denied(self):
        req = urllib.request.Request(self.config['url'] + '/health', b'{}')
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()

    def test_browser_origin_denied_even_with_token(self):
        req = urllib.request.Request(self.config['url'] + '/health', b'{}',
                                     {'Authorization': 'Bearer ' + self.config['token'], 'Origin': 'https://example.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()

    def test_scoped_client_cannot_administer_or_impersonate(self):
        native = '33333333-3333-4333-8333-333333333333'
        call(self.state, '/bind', {'tool': 'codex', 'native_id': native, 'alias': 'test-client',
                                  'project': 'test', 'cli': sys.executable, 'codex_home': str(self.state)})
        directory = self.state.parent / (self.state.name + '-client') / 'agentbridge-client'
        try:
            call(self.state, '/grant', {'identity': 'test-client', 'directory': str(directory)})
            self.assertEqual(call(directory, '/health', {})['protocol'], '1.2')
            with self.assertRaises(BridgeError):
                call(directory, '/stop', {})
            with self.assertRaises(BridgeError):
                call(directory, '/send', {'actor': 'claude:not-me'})
            with self.assertRaises(BridgeError):
                call(directory, '/refresh-claude', {'identity': 'test-client'})
            call(self.state, '/unbind', {'identity': 'test-client'})
            with self.assertRaises(BridgeError):
                call(directory, '/health', {})
        finally:
            import shutil
            shutil.rmtree(directory.parent)

    def test_single_coordinator_lock(self):
        root = Path(__file__).resolve().parents[1]
        p = subprocess.run([sys.executable, str(root / 'agentbridge.py'), '--state', str(self.state), 'agent'],
                           capture_output=True, text=True, timeout=5,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertNotEqual(p.returncode, 0)

    @unittest.skipUnless(os.name == 'nt', 'requires Windows ACLs')
    def test_same_user_reader_keeps_writer_access_and_repairs_old_acl(self):
        import csv
        import shutil
        user = list(csv.reader(subprocess.check_output(['whoami', '/user', '/fo', 'csv'], text=True).splitlines()))[-1][1]
        native = '44444444-4444-4444-8444-444444444444'
        call(self.state, '/bind', {'tool': 'codex', 'native_id': native, 'alias': 'same-user',
                                  'project': 'test', 'cli': sys.executable, 'codex_home': str(self.state)})
        directory = self.state.parent / (self.state.name + '-same-user') / 'agentbridge-client'
        directory.mkdir(parents=True)
        try:
            # Reproduce the actual broken directory left by the previous grant.
            subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r',
                            f'*{user}:(OI)(CI)RX', '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True)
            with self.assertRaises(PermissionError):
                (directory / 'client.tmp').write_text('cannot write')
            for _ in range(2):
                call(self.state, '/grant', {'identity': 'same-user', 'directory': str(directory), 'reader_sid': user})
                self.assertEqual(call(directory, '/health', {})['protocol'], '1.2')
                (directory / 'write-check.txt').write_text('owner retains write access')
                with self.assertRaises(BridgeError):
                    call(directory, '/stop', {})
        finally:
            subprocess.run(['icacls', str(directory), '/grant:r', f'*{user}:(OI)(CI)F'], capture_output=True)
            shutil.rmtree(directory.parent)


if __name__ == '__main__':
    unittest.main()
