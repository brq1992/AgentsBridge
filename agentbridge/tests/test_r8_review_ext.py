import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bridge import adapters

class DiscoveryRegression(unittest.TestCase):
    def test_invalid_pid_does_not_abort_valid_session_discovery(self):
        native = '11111111-1111-4111-8111-111111111111'
        with tempfile.TemporaryDirectory() as root:
            for name, pid in [('1.json', 'not-a-pid'), ('2.json', 2)]:
                Path(root, name).write_text(json.dumps({
                    'sessionId': native, 'pid': pid, 'procStart': '123'}))
            def process_start(pid):
                int(pid)  # Windows and macOS both convert registry PID to int.
                return '123'
            with patch.object(adapters, 'process_start', process_start):
                entries = adapters.discover_claude(root)
            self.assertEqual([entry['pid'] for entry in entries], [2])
