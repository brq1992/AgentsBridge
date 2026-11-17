"""Re-notification of Claude messages that were written to the pipe but never claimed,
and pipe receipt summaries. Native delivery is simulated; no real pipe is written."""
import json
import time
import unittest
from unittest.mock import patch

from bridge import adapters

try:
    from test_r4 import ClaimFixture
except ImportError:
    from tests.test_r4 import ClaimFixture


class RenotifyTests(ClaimFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.envelope = {'id': 'm_abandoned', 'kind': 'request', 'sender': 'x:codex:1', 'parent_id': None, 'hop': 0,
                         'receiver': 'n:' + self.session['id'], 'receiver_gen': self.session['generation'],
                         'project': 'echo', 'body': '{"op":"echo","text":"hi"}', 'expires': time.time() + 3600,
                         'network': True}
        with self.store.connect() as c:
            c.execute("UPDATE net_jobs SET envelope=?,notified_at=? WHERE id='m_abandoned'",
                      (json.dumps(self.envelope), time.time() - 120))
        self.sent = []
        self.patches = [patch('bridge.network.adapters.prepare', side_effect=lambda s, t: (s, t)),
                        patch('bridge.network.adapters.deliver', side_effect=self.fake_deliver),
                        patch('bridge.network.adapters.claude_status', return_value='idle')]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def fake_deliver(self, prepared):
        self.sent.append(prepared[1])
        return {'transport': 'test', 'receipt': [{'type': 'receipt', 'status': 'held'}]}

    def job(self):
        with self.store.connect() as c:
            return dict(c.execute("SELECT * FROM net_jobs WHERE id='m_abandoned'").fetchone())

    def test_unclaimed_message_is_renotified_when_idle_with_backoff_and_cap(self):
        self.assertEqual(self.agent.renotify(), ['m_abandoned'])
        self.assertIn('re-notification 1/3', self.sent[0])
        self.assertIn('ack --message m_abandoned', self.sent[0])
        self.assertEqual(self.job()['notify_count'], 1)
        self.assertEqual(json.loads(self.job()['last_receipt']), [{'type': 'receipt', 'status': 'held'}])
        self.assertEqual(self.agent.renotify(), [], 'second re-notification waits 180 s')
        self.assertEqual(self.agent.renotify(now=time.time() + 181), ['m_abandoned'])
        self.assertEqual(self.agent.renotify(now=time.time() + 800), ['m_abandoned'])
        self.assertEqual(self.agent.renotify(now=time.time() + 5000), [], 'at most three re-notifications')
        self.assertEqual(self.job()['notify_count'], 3)

    def test_busy_session_is_not_renotified(self):
        with patch('bridge.network.adapters.claude_status', return_value='busy'):
            self.assertEqual(self.agent.renotify(), [])
        self.assertEqual(self.sent, [])

    def test_claimed_expired_or_rebound_messages_are_left_alone(self):
        with self.store.connect() as c:
            c.execute("UPDATE net_jobs SET state='received' WHERE id='m_abandoned'")
        self.assertEqual(self.agent.renotify(), [])
        with self.store.connect() as c:
            c.execute("UPDATE net_jobs SET state='submitted',envelope=? WHERE id='m_abandoned'",
                      (json.dumps(dict(self.envelope, expires=time.time() - 1)),))
        self.assertEqual(self.agent.renotify(), [])
        with self.store.connect() as c:
            c.execute("UPDATE net_jobs SET envelope=? WHERE id='m_abandoned'", (json.dumps(self.envelope),))
        self.store.bind('claude', self.session['native_id'], 'claude2', 'echo', {'rebound': True})
        self.assertEqual(self.agent.renotify(), [])
        self.assertEqual(self.sent, [])

    def test_unclaimed_report(self):
        report = self.agent.unclaimed()
        self.assertEqual([r['message_id'] for r in report], ['m_abandoned'])
        self.assertGreaterEqual(report[0]['age_seconds'], 119)


class ReceiptSummaryTests(unittest.TestCase):
    def test_only_structural_fields_are_kept(self):
        raw = (json.dumps({'type': 'receipt', 'status': 'held', 'text': 'secret body'}) + '\nnot json\n').encode()
        self.assertEqual(adapters._summarize_receipt(raw), [{'type': 'receipt', 'status': 'held'}, {'unparsed_bytes': 8}])


if __name__ == '__main__':
    unittest.main()
