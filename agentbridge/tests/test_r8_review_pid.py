import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from bridge import adapters

UUID = '11111111-2222-3333-4444-555555555555'


class MalformedPidTests(unittest.TestCase):
    """R8 review: a malformed pid in one registry must not abort discovery of the others."""

    def test_malformed_pid_is_unreadable_and_healthy_entry_survives(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, pid in (('good.json', 1234), ('bad1.json', 'not-a-pid'), ('bad2.json', True),
                              ('bad3.json', -5), ('bad4.json', 12.5)):
                (root / name).write_text(json.dumps({'pid': pid, 'procStart': 'S', 'sessionId': UUID,
                                                     'version': '1'}), encoding='utf8')
            with mock.patch.object(adapters, 'process_start', lambda pid: 'S'):
                entries = adapters.discover_claude(root)
            self.assertEqual([e['pid'] for e in entries], [1234])
            self.assertEqual(sorted(adapters.last_unreadable()),
                             ['bad1.json', 'bad2.json', 'bad3.json', 'bad4.json'])

    def test_unexpected_process_start_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a.json').write_text(json.dumps({'pid': 1, 'procStart': 'S', 'sessionId': UUID}),
                                            encoding='utf8')
            def boom(pid):
                raise ZeroDivisionError('unrelated bug')
            with mock.patch.object(adapters, 'process_start', boom):
                with self.assertRaises(ZeroDivisionError):
                    adapters.discover_claude(Path(d))
