"""R5: fixes for the independent R4 review (concurrent claims, generation, lost responses,
tracked token files)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest

from bridge import cli
from bridge.server import check_client_directory
from bridge.store import BridgeError
try:
    from test_r4 import ClaimFixture
except ImportError:
    from tests.test_r4 import ClaimFixture

DONE = {'should_execute': True, 'kind': 'request', 'body': '{"op":"echo","text":"x"}'}


class ClaimRaceTests(ClaimFixture, unittest.TestCase):
    def finish_later(self, rid, delay, result=DONE):
        def finish():
            time.sleep(delay)
            with self.store.connect() as c:
                c.execute("UPDATE net_ops SET state='done',result=? WHERE id=?", (json.dumps(result), rid))
        thread = threading.Thread(target=finish)
        thread.start()
        return thread

    def parallel(self, call, count=3):
        results = []
        threads = [threading.Thread(target=lambda: results.append(call())) for _ in range(count)]
        for t in threads:
            t.start()
        return threads, results

    def test_concurrent_waiters_without_nonce_get_one_permission(self):
        rid = self.insert_ack('sending')
        threads, results = self.parallel(self.ack)
        self.finish_later(rid, 0.5).join()
        for t in threads:
            t.join(15)
        self.assertEqual(len(results), 3)
        self.assertEqual(sum(bool(r.get('should_execute')) for r in results), 1, results)

    def test_concurrent_nonce_offers_allow_one_receipt(self):
        rid = self.insert_ack('sending')
        def offer(n):
            return self.agent.forward('/ack', {'actor': self.session['id'], 'message_id': 'm_abandoned',
                                               'claim_nonce': 'nonce%04d' % n})
        offers = []
        threads = [threading.Thread(target=lambda n=n: offers.append((n, offer(n)))) for n in range(3)]
        for t in threads:
            t.start()
        self.finish_later(rid, 0.5).join()
        for t in threads:
            t.join(15)
        receipts = 0
        for n, result in offers:
            if result.get('claim_receipt'):
                try:
                    self.agent.receipt({'actor': self.session['id'], 'operation_id': rid, 'claim_nonce': 'nonce%04d' % n})
                    receipts += 1
                except BridgeError:
                    pass
        self.assertEqual(receipts, 1, offers)

    def test_old_generation_claim_is_never_reused(self):
        self.insert_ack('done', dict(DONE, body='old-generation'))
        old = self.session['generation']
        self.session = self.store.bind('claude', self.session['native_id'], 'claude2', 'echo', {'changed': True})
        self.assertNotEqual(old, self.session['generation'])
        self.agent.stop.set()
        self.assertFalse(self.ack().get('should_execute'))

    def test_rebind_between_offer_and_receipt_rejects_receipt(self):
        rid = self.insert_ack('done', DONE)
        offered = self.agent.forward('/ack', {'actor': self.session['id'], 'message_id': 'm_abandoned',
                                              'claim_nonce': 'abcdef012345'})
        self.assertEqual(offered['claim_receipt'], rid)
        self.store.bind('claude', self.session['native_id'], 'claude2', 'echo', {'changed': True})
        with self.assertRaises(BridgeError):
            self.agent.receipt({'actor': self.session['id'], 'operation_id': rid, 'claim_nonce': 'abcdef012345'})

    def test_expired_message_claim_is_not_handed_out(self):
        with self.store.connect() as c:
            c.execute("UPDATE net_jobs SET envelope=? WHERE id='m_abandoned'", (json.dumps({'expires': time.time() - 1}),))
        self.insert_ack('done', DONE)
        result = self.ack()
        self.assertFalse(result['should_execute'])
        self.assertEqual(result['reason'], 'expired')


class LostResponseTests(ClaimFixture, unittest.TestCase):
    """The real CLI claim() over HTTP against a handler that forwards to the Agent."""

    def serve(self, drop_first_receipt=False):
        agent = self.agent
        dropped = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    if self.path == '/ack':
                        result = agent.forward('/ack', body)
                    else:
                        result = agent.receipt(body)
                        if drop_first_receipt and not dropped:
                            dropped.append(True)
                            self.connection.close()     # applied, but the response is lost
                            return
                    data = {'ok': True, 'result': result}
                except BridgeError as e:
                    data = {'ok': False, 'error': str(e)}
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Path(self.temp.name) / 'client'
        client.mkdir(exist_ok=True)
        (client / 'client.json').write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'token': 't'}))
        return client

    def body(self):
        return {'actor': self.session['id'], 'message_id': 'm_abandoned'}

    def handed(self, rid):
        with self.store.connect() as c:
            return c.execute('SELECT handed FROM net_ops WHERE id=?', (rid,)).fetchone()[0]

    def test_ack_response_lost_then_next_ack_receives_once(self):
        client = self.serve()
        rid = self.insert_ack('sending')
        def finish():
            time.sleep(2)
            with self.store.connect() as c:
                c.execute("UPDATE net_ops SET state='done',result=? WHERE id=?", (json.dumps(DONE), rid))
        threading.Thread(target=finish).start()
        first = cli.claim(client, self.body(), ack_timeout=1)      # client gives up before the Hub answers
        self.assertFalse(first['should_execute'])
        self.assertTrue(first['reason'].startswith('ack_response_lost'))
        time.sleep(2.5)
        self.assertEqual(self.handed(rid), 0, 'a lost response must not consume the claim')
        second = cli.claim(client, self.body())
        self.assertTrue(second['should_execute'])
        self.assertNotIn('claim_receipt', second)
        self.assertEqual(self.handed(rid), 1)

    def test_receipt_response_lost_is_retried_idempotently(self):
        client = self.serve(drop_first_receipt=True)
        rid = self.insert_ack('done', DONE)
        result = cli.claim(client, self.body())
        self.assertTrue(result['should_execute'])
        self.assertEqual(self.handed(rid), 1)

    def test_cli_waits_longer_than_agent(self):
        from bridge.network import RemoteAgent
        self.assertGreaterEqual(cli.ACK_TIMEOUT, RemoteAgent.ACK_WAIT + 15)


class TrackedTokenTests(unittest.TestCase):
    def test_tracked_then_excluded_token_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo'
            repo.mkdir()
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            git = lambda *a: subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, creationflags=flags)
            git('init', '-q')
            folder = repo / 'client'
            folder.mkdir()
            (folder / 'client.json').write_text('{}')    # synthetic, not a real token
            git('add', 'client/client.json')
            (repo / '.git' / 'info' / 'exclude').write_text('/client/\n')
            with self.assertRaises(BridgeError) as caught:
                check_client_directory(folder)
            self.assertIn('tracked', str(caught.exception))
            git('rm', '-q', '--cached', 'client/client.json')
            check_client_directory(folder)      # untracked and excluded: accepted
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            import importlib.util
            spec = importlib.util.spec_from_file_location('configure', Path(__file__).resolve().parents[1] / 'configure-m1-session.py')
            configure = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(configure)
            git('add', '-f', 'client/client.json')
            with self.assertRaises(BridgeError):
                configure.exclude_from_git(folder)


if __name__ == '__main__':
    unittest.main()
