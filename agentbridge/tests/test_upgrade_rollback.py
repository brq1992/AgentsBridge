"""The real PowerShell updater restores the previous files and Agent when the upgraded
Agent never becomes healthy (here: the Hub disappears after the pre-check)."""
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


@unittest.skipUnless(os.name == 'nt', 'Windows installer')
class UpgradeRollbackTests(unittest.TestCase):
    def test_failed_start_restores_previous_files_and_agent(self):
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / 'installed'
            install.mkdir()
            for name in ('bridge', 'runtime'):
                shutil.copytree(source / name, install / name, ignore=shutil.ignore_patterns('__pycache__'))
            for name in ('agentbridge.py', 'start-m1.ps1'):
                shutil.copy2(source / name, install / name)
            marker = install / 'bridge' / 'store.py'
            marker.write_text(marker.read_text() + '\n# previous-release-marker\n')
            previous = marker.read_bytes()
            state = root / 'state'
            state.mkdir()
            create_tls(root, ['127.0.0.1'])
            hub = HubStore(root / 'hub.sqlite')
            ready = threading.Event()
            servers = []
            def on_ready(server):
                servers.append(server)
                ready.set()
            thread = threading.Thread(target=serve_hub, args=(hub, '127.0.0.1', 0, root / 'hub-cert.pem', root / 'hub-key.pem', on_ready), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            node = hub.add_node('rollback-test', ['echo'])
            (state / 'network.json').write_text(json.dumps(node | {
                'hub_url': f'wss://127.0.0.1:{servers[0].socket.getsockname()[1]}',
                'hub_certificate': (root / 'hub-cert.pem').read_text()}))
            flags = subprocess.CREATE_NO_WINDOW
            python = install / 'runtime/python.exe'
            log = (root / 'agent.stderr').open('w')
            process = subprocess.Popen([str(python), str(install / 'agentbridge.py'), '--state', str(state), 'agent',
                                        '--hub-config', str(state / 'network.json')],
                                       stdout=subprocess.DEVNULL, stderr=log, creationflags=flags)
            try:
                deadline = time.monotonic() + 12
                while not self._healthy(state, connected=True):
                    if time.monotonic() > deadline:
                        self.fail('Agent failed to start')
                    time.sleep(.1)
                servers[0].shutdown()
                thread.join(5)
                output = root / 'upgrade.txt'
                with output.open('w') as stream:
                    code = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                           str(source / 'upgrade-recovery.ps1'), '-InstallDirectory', str(install),
                                           '-State', str(state)], stdout=stream, stderr=stream, timeout=180,
                                          creationflags=flags).returncode
                text = output.read_text()
                self.assertNotEqual(code, 0, text)
                self.assertIn('rolled back', text)
                self.assertEqual(marker.read_bytes(), previous)
                self.assertFalse((install / 'configure-m1-session.py').exists(), 'files new in the release must be removed')
                deadline = time.monotonic() + 20
                while not self._healthy(state):
                    if time.monotonic() > deadline:
                        self.fail('previous Agent was not restarted after rollback: ' + text)
                    time.sleep(.2)
            finally:
                try:
                    call(state, '/stop', {})
                except Exception:
                    pass
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
                log.close()
                deadline = time.monotonic() + 10
                while self._healthy(state) and time.monotonic() < deadline:
                    time.sleep(.2)

    @staticmethod
    def _healthy(state, connected=False):
        try:
            health = call(state, '/health', {})
        except Exception:
            return False
        return bool(health.get('hub_connected')) if connected else health.get('mode') == 'windows-m1-network'


if __name__ == '__main__':
    unittest.main()
