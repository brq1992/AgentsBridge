import json
import time
import unittest
import test_review_v2_ext as fixture


class AutoAckOutcomes(unittest.TestCase):
    setUp = fixture.Boundaries.setUp
    save_parent = fixture.Boundaries.save_parent

    def resolve(self, response):
        with self.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state,auto_ack) VALUES(?,?,'submitted','queued')",
                      ('reply', json.dumps(self.m)))
            rid = self.a._enqueue(c, 'ack', dict(message_id='reply', actor=self.m['receiver'],
                                                 generation=self.s['generation'], context={'auto_ack': 'agent'}))
            op = dict(c.execute('SELECT * FROM net_ops WHERE id=?', (rid,)).fetchone())
            self.a._resolve_auto_ack(c, op, response)
            return tuple(c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone())

    def test_fresh_receipt(self):
        self.assertEqual(self.resolve({'ok': True, 'result': {'should_execute': False, 'kind': 'reply', 'body': 'ok'}}),
                         ('received', 'agent_received'))

    def test_session_acked_first(self):
        self.assertEqual(self.resolve({'ok': True, 'result': {'should_execute': False, 'reason': 'already_received', 'kind': 'reply'}}),
                         ('received', 'already_received'))

    def test_expired_denial_is_terminal_expired(self):
        self.assertEqual(self.resolve({'ok': True, 'result': {'should_execute': False, 'reason': 'expired'}}),
                         ('expired', 'denied:expired'))

    def test_revoked_denial_is_terminal_blocked(self):
        self.assertEqual(self.resolve({'ok': True, 'result': {'should_execute': False, 'reason': 'binding_revoked'}}),
                         ('blocked', 'denied:binding_revoked'))

    def test_already_received_without_reply_kind_is_not_received(self):
        self.assertEqual(self.resolve({'ok': True, 'result': {'should_execute': False, 'reason': 'already_received'}})[0],
                         'blocked')

    def test_transport_error_reinjects(self):
        self.assertEqual(self.resolve({'ok': False, 'error': 'boom'}), ('accepted', 'failed:boom'))

    def test_unsent_expiry_is_terminal_and_not_requeued(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state,auto_ack) VALUES(?,?,'submitted','queued')",
                      ('reply', json.dumps(self.m)))
            rid = self.a._enqueue(c, 'ack', dict(message_id='reply', actor=self.m['receiver'],
                                                 generation=self.s['generation'], context={'auto_ack': 'agent'}))
            c.execute("UPDATE net_ops SET state='failed',error='operation expired before confirmed transmission; do not execute' WHERE id=?", (rid,))
        self.a.expire_auto_acks()
        with self.store.connect() as c:
            row = tuple(c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone())
        self.assertEqual(row, ('expired', 'expired_unsent'))
        job = next(j for j in self.a.status()['jobs'] if j['id'] == 'reply')
        self.assertEqual(job['auto_ack'], 'expired_unsent')

    def test_quiet_keeps_auto_ack_fields(self):
        from bridge import cli
        q = cli.quiet({'id': 'p', 'body': 'x', 'reply_delivery': 'received', 'reply_auto_ack': 'agent_received'})
        self.assertEqual(q, {'id': 'p', 'reply_delivery': 'received', 'reply_auto_ack': 'agent_received'})


class RealHubAnswers(unittest.TestCase):
    """Feed the Agent the exact dicts the real Hub Store.ack returns."""

    def test_store_ack_denials_map_to_terminal_states(self):
        import tempfile
        from pathlib import Path
        from bridge.store import Store
        d = tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup)
        hub = Store(Path(d.name) / 'hub.sqlite')
        a = hub.bind('codex', '22222222-2222-4222-8222-222222222222', 'a', 'echo', {})
        b = hub.bind('codex', '33333333-3333-4333-8333-333333333333', 'b', 'echo', {})
        req = hub.send(a['id'], 'b', json.dumps({'op': 'echo', 'text': 'x'}), ttl=60)
        hub.ack(req['id'], b['id'])
        rep = hub.reply(req['id'], b['id'], 'x')
        fresh = hub.ack(rep['id'], a['id'])
        again = hub.ack(rep['id'], a['id'])
        self.assertEqual((fresh.get('kind'), fresh.get('reason')), ('reply', None))
        self.assertEqual((again.get('kind'), again.get('reason')), ('reply', 'already_received'))


if __name__ == '__main__':
    unittest.main()
