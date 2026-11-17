"""R6: legacy (no-nonce) and receipt (nonce) callers mixed on one claim; exactly one
execution permission in every order, directly and through the real CLI over HTTP."""
import threading
import time
import unittest
from unittest.mock import patch

from bridge import cli
from bridge.store import BridgeError

try:
    import test_r5 as r5
except ImportError:
    from tests import test_r5 as r5
DONE = r5.DONE


class MixedConsumerTests(r5.LostResponseTests):
    # Inherit the HTTP harness only; do not re-run the R5 tests here.
    test_ack_response_lost_then_next_ack_receives_once = None
    test_receipt_response_lost_is_retried_idempotently = None
    test_cli_waits_longer_than_agent = None

    NONCE = 'mixednonce0001'

    def offer(self, nonce=NONCE):
        return self.agent.forward('/ack', {'actor': self.session['id'], 'message_id': 'm_abandoned', 'claim_nonce': nonce})

    def receipt(self, rid, nonce=NONCE):
        return self.agent.receipt({'actor': self.session['id'], 'operation_id': rid, 'claim_nonce': nonce})

    def consumer(self, rid):
        with self.store.connect() as c:
            return tuple(c.execute('SELECT handed,consumed_by FROM net_ops WHERE id=?', (rid,)).fetchone())

    def test_offer_then_legacy_then_receipt(self):
        rid = self.insert_ack('done', DONE)
        self.assertEqual(self.offer()['claim_receipt'], rid)
        self.assertTrue(self.ack()['should_execute'])                 # legacy consumes first
        with self.assertRaises(BridgeError):
            self.receipt(rid)                                          # nonce must not also succeed
        with self.assertRaises(BridgeError):
            self.receipt(rid)                                          # and stays refused
        self.assertEqual(self.consumer(rid), (1, 'legacy'))

    def test_offer_then_receipt_then_legacy(self):
        rid = self.insert_ack('done', DONE)
        self.offer()
        self.assertTrue(self.receipt(rid)['receipt'])
        self.assertTrue(self.receipt(rid).get('replayed'))            # own success replays
        self.assertFalse(self.ack()['should_execute'])                 # legacy gets nothing
        with self.assertRaises(BridgeError):
            self.receipt(rid, 'othernonce0002')                        # another nonce gets nothing
        self.assertEqual(self.consumer(rid), (1, self.NONCE))

    def test_legacy_before_any_offer_blocks_later_offers(self):
        rid = self.insert_ack('done', DONE)
        self.assertTrue(self.ack()['should_execute'])
        self.assertFalse(self.offer()['should_execute'])
        self.assertEqual(self.consumer(rid), (1, 'legacy'))

    def test_real_cli_with_legacy_consumer_interleaved_before_receipt(self):
        client = self.serve()
        self.insert_ack('done', DONE)
        original, legacy = cli.call, []
        def interleave(state, route, body, **kw):
            if route == '/ack-receipt':
                legacy.append(self.ack())
            return original(state, route, body, **kw)
        with patch('bridge.cli.call', side_effect=interleave):
            modern = cli.claim(client, self.body())
        self.assertTrue(legacy[0]['should_execute'])
        self.assertFalse(modern['should_execute'])
        self.assertEqual(modern['reason'], 'claim_receipt_rejected')

    def test_real_cli_first_then_legacy(self):
        client = self.serve()
        rid = self.insert_ack('done', DONE)
        self.assertTrue(cli.claim(client, self.body())['should_execute'])
        self.assertFalse(self.ack()['should_execute'])
        self.assertEqual(self.consumer(rid)[0], 1)

    def test_concurrent_mixed_callers_get_exactly_one_permission(self):
        for _ in range(5):
            with self.store.connect() as c:
                c.execute('DELETE FROM net_ops')
            client = self.serve()
            rid = self.insert_ack('sending')
            results = []
            calls = [lambda: results.append(self.ack()) for _ in range(3)]
            calls += [lambda: results.append(cli.claim(client, self.body())) for _ in range(3)]
            threads = [threading.Thread(target=f) for f in calls]
            for t in threads:
                t.start()
            time.sleep(0.3)
            with self.store.connect() as c:
                c.execute("UPDATE net_ops SET state='done',result=? WHERE id=?", (__import__('json').dumps(DONE), rid))
            for t in threads:
                t.join(30)
            self.assertEqual(len(results), 6)
            self.assertEqual(sum(bool(r.get('should_execute')) for r in results), 1, results)


if __name__ == '__main__':
    unittest.main()
