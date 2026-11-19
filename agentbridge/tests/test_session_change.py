import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bridge import adapters

U = '11111111-1111-4111-8111-111111111111'
NEW = '22222222-2222-4222-8222-222222222222'
OLD = {'pid': 100, 'procStart': 'Wed Nov 18 03:00:00 2026', 'registry': '/x/100.json'}


def entry(uuid_, pid=100, start='Wed Nov 18 03:00:00 2026'):
    return {'sessionId': uuid_, 'sessionIds': [uuid_], 'pid': pid, 'procStart': start}


class Diagnose(unittest.TestCase):
    def code(self, entries):
        p = adapters.claude_session_problem(U, OLD, entries)
        return p.code if p else None

    def test_live_exact_uuid_is_fine(self):
        self.assertIsNone(self.code([entry(U, pid=200)]))

    def test_same_process_new_uuid_is_replaced(self):
        self.assertEqual(self.code([entry(NEW)]), 'claude_session_replaced')

    def test_process_gone_is_not_running(self):
        self.assertEqual(self.code([entry(NEW, pid=300)]), 'claude_session_not_running')

    def test_pid_reused_by_other_process_is_not_running(self):
        self.assertEqual(self.code([entry(NEW, start='Thu Nov 19 01:00:00 2026')]), 'claude_session_not_running')

    def test_duplicate_holders_are_ambiguous(self):
        self.assertEqual(self.code([entry(U, pid=1), entry(U, pid=2)]), 'claude_session_ambiguous')

    def test_never_substitutes_a_session(self):
        with mock.patch.object(adapters, 'discover_claude', return_value=[entry(NEW)]):
            with self.assertRaises(adapters.EndpointError):
                adapters.claude_metadata(U, OLD)

    def test_hint_formats_alias_and_project(self):
        p = adapters.claude_session_problem(U, OLD, [entry(NEW)])
        text = p.hint.format(alias='claude3', project='agentbridge-m0')
        self.assertIn('bind --alias claude3 --project agentbridge-m0', text)


    def test_unreadable_registry_is_not_not_running(self):
        p = adapters.claude_session_problem(U, OLD, [], ['100.json'])
        self.assertEqual(p.code, 'claude_registry_unreadable')

    def test_fill_hint_ignores_other_braces(self):
        self.assertEqual(adapters.fill_hint('v 99.{build} {alias}', 'a', 'p'), 'v 99.{build} a')


class PreflightMissingRegistry(unittest.TestCase):
    def test_missing_registry_reports_reason_not_path(self):
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        meta = dict(OLD, registry=str(Path(d.name) / '100.json'), version='9.9.9', messagingSocketPath='x')
        session = {'native_id': U, 'metadata': json.dumps(meta)}
        with mock.patch.object(adapters, 'qualified_versions', return_value={'9.9.9'}),                 mock.patch.object(adapters, 'discover_claude', return_value=[]):
            with self.assertRaises(adapters.EndpointError) as e:
                adapters.claude_preflight(session)
        self.assertEqual(e.exception.code, 'claude_session_not_running')
        self.assertNotIn(d.name, str(e.exception))


if __name__ == '__main__':
    unittest.main()
