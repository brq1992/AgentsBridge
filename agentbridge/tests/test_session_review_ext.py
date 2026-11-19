import contextlib
import io
import json
import unittest
import threading
from unittest import mock
import test_recovery
from bridge import adapters


class Review(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    registry_entry = test_recovery.RecoveryTests.registry_entry
    current = test_recovery.RecoveryTests.current

    def test_concurrent_discovery_keeps_own_unreadable_result(self):
        path = self.registry_entry()
        path.write_text('{partial')
        other = self.root / 'other'
        other.mkdir()
        scanned, resume = threading.Event(), threading.Event()
        result = []
        original = self.discovery.temp_original
        def paused_scan():
            entries = original(self.registry)
            scanned.set()
            if not resume.wait(3):
                raise RuntimeError('test synchronization timed out')
            return entries
        def worker():
            try:
                adapters.claude_metadata(self.native)
            except Exception as error:
                result.append(getattr(error, 'code', type(error).__name__))
        with mock.patch.object(adapters, 'discover_claude', paused_scan):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(scanned.wait(3))
                original(other)
            finally:
                resume.set()
                thread.join(3)
        self.assertEqual(result, ['claude_registry_unreadable'])
    def test_braces_in_unqualified_version_do_not_abort_recovery(self):
        self.registry_entry(version='99.{build}')
        self.agent.recover_endpoints()
        self.assertEqual(self.agent.endpoint_health[self.session['id']]['reason'],
                         'claude_version_unqualified')

    def test_unreadable_live_registry_is_not_proof_process_stopped(self):
        path = self.registry_entry()
        self.agent.recover_endpoints()
        current = self.current()
        original = type(path).read_text
        def read(p, *args, **kwargs):
            if p == path:
                raise PermissionError('private registry path')
            return original(p, *args, **kwargs)
        with mock.patch.object(type(path), 'read_text', read):
            with self.assertRaises(adapters.EndpointError) as caught:
                adapters.claude_preflight(current)
        self.assertNotEqual(caught.exception.code, 'claude_session_not_running')

    def test_reason_logged_once_until_state_changes(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.agent.recover_endpoints()
            self.agent.recover_endpoints()
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_duplicate_recovery_keeps_binding_and_token(self):
        self.registry_entry(pid=2)
        self.registry_entry(pid=3)
        self.agent.recover_endpoints()
        self.assertEqual(self.current()['metadata'], self.session['metadata'])
        self.assertEqual(self.current()['generation'], self.session['generation'])
        self.assertIsNotNone(self.store.authenticate_client('test-token'))

