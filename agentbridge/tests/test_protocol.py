import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.adapters import codex_args, native_command_length
from bridge.store import BridgeError, Store


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.temp.name) / 'test.sqlite')
        self.a = self.db.bind('codex', '11111111-1111-4111-8111-111111111111', 'a', 'm0', {})['id']
        self.b = self.db.bind('claude', '22222222-2222-4222-8222-222222222222', 'b', 'm0', {})['id']

    def tearDown(self):
        self.temp.cleanup()

    def send(self, **kwargs):
        return self.db.send(self.a, self.b, '{"op":"echo","text":"hello"}', **kwargs)

    def test_client_publish_failure_preserves_previous_capability(self):
        self.db.add_client(self.a, 'test-client-directory', 'old-test-token')
        def fail_publish():
            raise PermissionError('simulated client config write failure')
        with self.assertRaises(PermissionError):
            self.db.add_client(self.a, 'test-client-directory', 'new-test-token', publish=fail_publish)
        self.assertEqual(self.db.authenticate_client('old-test-token')['id'], self.a)
        self.assertIsNone(self.db.authenticate_client('new-test-token'))

    def test_explicit_endpoint_refresh_preserves_original_binding_and_capability(self):
        original=next(s for s in self.db.sessions() if s['id']==self.b)
        self.db.add_client(self.b,'client-directory','refresh-test-token')
        message=self.send()
        refreshed=self.db.refresh_endpoint(self.b,original['generation'],{'pid':12345,'verified':'test'})
        self.assertEqual(refreshed['generation'],original['generation'])
        self.assertEqual(refreshed['native_id'],original['native_id'])
        self.assertIsNotNone(self.db.authenticate_client('refresh-test-token'))
        self.assertTrue(self.db.ack(message['id'],self.b)['should_execute'])
        self.db.unbind(self.b)
        with self.assertRaises(BridgeError):
            self.db.refresh_endpoint(self.b,original['generation'],{'pid':999})

    def test_endpoint_refresh_rejects_stale_generation_and_non_claude(self):
        for identity in (self.a,self.b):
            with self.assertRaises(BridgeError):
                self.db.refresh_endpoint(identity,'g_stale',{})

    def test_roundtrip_requires_source_receipt(self):
        m = self.send()
        self.assertTrue(self.db.ack(m['id'], self.b)['should_execute'])
        r = self.db.reply(m['id'], self.b, 'hello')
        status = self.db.status(m['id'])[0]
        self.assertEqual(status['outcome'], 'completed')
        self.assertEqual(status['reply_delivery'], 'accepted')
        received = self.db.ack(r['id'], self.a)
        self.assertFalse(received['should_execute'])
        self.assertEqual(self.db.status(m['id'])[0]['reply_delivery'], 'received')

    def test_duplicate_send_and_payload_conflict(self):
        first = self.send(mid='m_same')
        self.assertEqual(first['id'], self.send(mid='m_same')['id'])
        with self.assertRaises(BridgeError):
            self.db.send(self.a, self.b, 'different', mid='m_same')

    def test_duplicate_ack_does_not_execute_twice(self):
        m = self.send()
        self.assertTrue(self.db.ack(m['id'], self.b)['should_execute'])
        self.assertFalse(self.db.ack(m['id'], self.b)['should_execute'])

    def test_wrong_receiver_cannot_ack_or_reply(self):
        m = self.send()
        with self.assertRaises(BridgeError):
            self.db.ack(m['id'], self.a)
        with self.assertRaises(BridgeError):
            self.db.reply(m['id'], self.a, 'bad')

    def test_reply_before_claim_rejected(self):
        with self.assertRaises(BridgeError):
            self.db.reply(self.send()['id'], self.b, 'bad')

    def test_duplicate_final_and_conflict(self):
        m = self.send()
        self.db.ack(m['id'], self.b)
        r = self.db.reply(m['id'], self.b, 'hello')
        self.assertEqual(self.db.reply(m['id'], self.b, 'hello')['id'], r['id'])
        with self.assertRaises(BridgeError):
            self.db.reply(m['id'], self.b, 'different')

    def test_reply_to_reply_rejected(self):
        m = self.send()
        self.db.ack(m['id'], self.b)
        r = self.db.reply(m['id'], self.b, 'hello')
        self.db.ack(r['id'], self.a)
        with self.assertRaises(BridgeError):
            self.db.reply(r['id'], self.a, 'loop')
        self.assertEqual(r['hop'], m['hop'] + 1)
        self.assertEqual(r['conversation_id'], m['conversation_id'])

    def test_native_queue_late_arrival_is_not_executed(self):
        with patch('bridge.store.time.time', return_value=1000):
            m = self.send(ttl=10)
            self.db.begin_submission(m['id'])
            self.db.finish_submission(m['id'], 'submitted')
        with patch('bridge.store.time.time', return_value=1011):
            self.assertFalse(self.db.ack(m['id'], self.b)['should_execute'])
            self.assertEqual(self.db.status(m['id'])[0]['delivery'], 'expired')

    def test_claimed_work_can_finish_after_expiry(self):
        with patch('bridge.store.time.time', return_value=1000):
            m = self.send(ttl=10)
            self.db.ack(m['id'], self.b)
        with patch('bridge.store.time.time', return_value=1011):
            r = self.db.reply(m['id'], self.b, 'late result')
            self.assertEqual(r['expires'], 1011 + 86400)

    def test_restart_never_reinjects_submission_intent(self):
        m = self.send()
        self.db.begin_submission(m['id'])
        reopened = Store(self.db.path)
        self.assertEqual(reopened.recover(), [m['id']])
        self.assertIsNone(reopened.next_delivery())
        self.assertEqual(reopened.status(m['id'])[0]['delivery'], 'delivery_uncertain')

    def test_restart_before_submission_preserves_pending(self):
        m = self.send()
        reopened = Store(self.db.path)
        self.assertEqual(reopened.recover(), [])
        self.assertEqual(reopened.next_delivery()[0]['id'], m['id'])

    def test_fast_ack_not_regressed_by_native_success(self):
        m = self.send()
        self.db.begin_submission(m['id'])
        self.db.ack(m['id'], self.b)
        self.db.reply(m['id'], self.b, 'fast')
        self.db.finish_submission(m['id'], 'submitted')
        self.assertEqual(self.db.status(m['id'])[0]['delivery'], 'received')
        self.assertEqual(self.db.status(m['id'])[0]['outcome'], 'completed')

    def test_unbind_stops_delivery(self):
        m = self.send()
        self.db.unbind(self.b)
        self.assertIsNone(self.db.next_delivery())
        self.assertEqual(self.db.status(m['id'])[0]['delivery'], 'blocked')

    def test_rebind_does_not_retarget_old_message(self):
        m = self.send()
        self.db.unbind(self.b)
        self.db.bind('claude', self.b.split(':')[1], 'b', 'm0', {})
        self.assertFalse(self.db.ack(m['id'], self.b)['should_execute'])
        self.assertIsNone(self.db.next_delivery())

    def test_alias_collision_rejected(self):
        with self.assertRaises(BridgeError):
            self.db.bind('codex', '33333333-3333-4333-8333-333333333333', 'b', 'm0', {})

    def test_cross_project_denied(self):
        s = self.db.bind('codex', '33333333-3333-4333-8333-333333333333', 'c', 'other', {})
        with self.assertRaises(BridgeError):
            self.db.send(self.a, s['id'], 'bad')

    def test_queue_limit(self):
        for _ in range(50):
            self.send()
        with self.assertRaises(BridgeError):
            self.send()

    def test_utf8_limit(self):
        with self.assertRaises(BridgeError):
            self.db.send(self.a, self.b, '中' * 11000)

    def test_untrusted_id_cannot_enter_shell_hint(self):
        with self.assertRaises(BridgeError):
            self.send(mid='m_test; arbitrary-command')

    def test_nan_expiry_rejected(self):
        with self.assertRaises(BridgeError):
            self.send(ttl=float('nan'))

    def test_native_windows_limit_catches_ascii_and_escaping(self):
        s = {'native_id': self.a.split(':')[1], 'metadata': json.dumps({'cli': __file__, 'codex_home': '.'})}
        with self.assertRaises(BridgeError):
            codex_args(s, 'A' * 32768)
        with self.assertRaises(BridgeError):
            codex_args(s, '"' * 17000)
        self.assertLess(native_command_length(codex_args(s, '小测试 😀')), 32767)


if __name__ == '__main__':
    unittest.main()
