"""R4 fixes: abandoned-claim recovery, endpoint matching/refresh, client folder safety,
detached start, retention and certificate expiry."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from bridge import adapters
from bridge.hub import HubStore, create_tls
from bridge.network import RemoteAgent
from bridge.provision import certificate_expiry
from bridge.server import check_client_directory
from bridge.spawn import spawn_detached
from bridge.store import BridgeError, Store, uid


class ClaimFixture:
    """Agent + store with one bound Claude session and one delivery job (no tests)."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        create_tls(root, ['127.0.0.1'])
        config = root / 'network.json'
        config.write_text(json.dumps({'hub_url': 'wss://127.0.0.1:1', 'node_id': 'n', 'label': 'win-x', 'token': 't',
                                      'projects': ['echo'], 'hub_certificate': (root / 'hub-cert.pem').read_text()}))
        self.store = Store(root / 'ledger.sqlite')
        self.session = self.store.bind('claude', str(uuid.uuid4()), 'claude2', 'echo', {})
        self.agent = RemoteAgent(self.store, root, config, threading.Event(), {'machine_id': str(uuid.uuid4()), 'os_user_id': 'u'})
        self.agent.connected = True
        self.args = {'message_id': 'm_abandoned', 'actor': 'n:' + self.session['id'], 'generation': self.session['generation']}
        with self.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state) VALUES('m_abandoned','{}','submitted')")

    def tearDown(self):
        self.temp.cleanup()

    def insert_ack(self, state, result=None, created=None):
        rid = uid('rpc')
        with self.store.connect() as c:
            c.execute('INSERT INTO net_ops(id,op,args,state,result,created,deadline) VALUES(?,?,?,?,?,?,?)',
                      (rid, 'ack', json.dumps(self.args), state, json.dumps(result) if result else None,
                       created or time.time(), time.time() + 10))
        return rid

    def ack(self):
        return self.agent.forward('/ack', {'actor': self.session['id'], 'message_id': 'm_abandoned'})


class AbandonedClaimTests(ClaimFixture, unittest.TestCase):
    def test_claim_reached_hub_after_caller_gave_up_is_returned_once(self):
        claim = {'should_execute': True, 'kind': 'request', 'body': 'x', 'message_id': 'm_abandoned'}
        rid = self.insert_ack('done', claim)
        self.assertEqual(self.ack(), claim)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT handed FROM net_ops WHERE id=?', (rid,)).fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT state FROM net_jobs WHERE id='m_abandoned'").fetchone()[0], 'received')
            # No second claim is fabricated: only that one ack operation exists.
            self.assertEqual(c.execute("SELECT count(*) FROM net_ops WHERE op='ack'").fetchone()[0], 1)

    def test_in_flight_claim_is_awaited_not_duplicated(self):
        rid = self.insert_ack('sending')
        def answer():
            time.sleep(0.4)
            with self.store.connect() as c:
                c.execute("UPDATE net_ops SET state='done',result=? WHERE id=?",
                          (json.dumps({'should_execute': True, 'kind': 'request'}), rid))
        threading.Thread(target=answer).start()
        self.assertTrue(self.ack()['should_execute'])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM net_ops WHERE op='ack'").fetchone()[0], 1)

    def test_prune_bounds_unreceived_claims(self):
        # An unreceived claim stays retrievable while its message could still be valid
        # (TTL <= 24 h) and is dropped after 2 days; other finished rows after 7 days.
        now = time.time()
        recent = self.insert_ack('done', {'should_execute': True}, created=now - 86400)
        self.insert_ack('done', {'should_execute': True}, created=now - 3 * 86400)
        with self.store.connect() as c:
            c.execute("INSERT INTO net_ops(id,op,args,state,created) VALUES('rpc_old','report','{}','done',?)", (now - 8 * 86400,))
            c.execute("INSERT INTO net_ops(id,op,args,state,created) VALUES('rpc_new','report','{}','done',?)", (now,))
        self.assertEqual(self.agent.prune(), 2)
        with self.store.connect() as c:
            ids = {r[0] for r in c.execute('SELECT id FROM net_ops')}
        self.assertEqual(ids, {recent, 'rpc_new'})


class ClaudeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        adapters.configure(None)
        self.temp.cleanup()

    def registry(self, host, session, version='2.1.325'):
        pid = os.getpid()
        start = adapters.process_start(pid)
        entry = {'sessionId': session, 'hostSessionId': host, 'pid': pid, 'procStart': start, 'version': version,
                 'pidDomain': 'test', 'peerProtocol': 1, 'messagingSocketPath': '\\\\.\\pipe\\LOCAL\\cc-msg-' + 'a' * 32}
        (self.root / f'{pid}.json').write_text(json.dumps(entry))
        (self.root / f'{pid}.test.key').write_text(json.dumps({'procStartFt': start, 'pidDomain': 'test', 'peerToken': 'k'}))
        return entry

    @unittest.skipUnless(os.name == 'nt', 'Windows process identity')
    def test_binding_matches_either_uuid_field(self):
        host, session = str(uuid.uuid4()), str(uuid.uuid4())
        self.registry(host, session)
        original = adapters.discover_claude
        with patch('bridge.adapters.discover_claude', lambda: original(self.root)):
            for native in (host, session):
                meta = adapters.claude_metadata(native)
                pipe, token = adapters.claude_preflight({'native_id': native, 'metadata': json.dumps(meta)})
                self.assertEqual(token, 'k')
            with self.assertRaises(BridgeError):
                adapters.claude_metadata(str(uuid.uuid4()))

    @unittest.skipUnless(os.name == 'nt', 'Windows process identity')
    def test_locally_qualified_version_and_structure_check(self):
        session = str(uuid.uuid4())
        self.registry('local_' + session, session, version='9.9.9')
        meta = {'registry': str(self.root / f'{os.getpid()}.json'), 'pid': os.getpid(),
                'procStart': adapters.process_start(os.getpid()), 'version': '9.9.9',
                'messagingSocketPath': '\\\\.\\pipe\\LOCAL\\cc-msg-' + 'a' * 32}
        with self.assertRaises(adapters.EndpointError) as caught:
            adapters.claude_preflight({'native_id': session, 'metadata': json.dumps(meta)})
        self.assertEqual(caught.exception.code, 'claude_version_unqualified')
        self.assertEqual(adapters.claude_structure(meta['registry']), '9.9.9')
        adapters.configure(self.root)
        (self.root / adapters.QUALIFIED_FILE).write_text(json.dumps({'versions': ['9.9.9']}))
        adapters.claude_preflight({'native_id': session, 'metadata': json.dumps(meta)})


class CodexRefreshTests(unittest.TestCase):
    def test_same_generation_cli_refresh_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / 'ledger.sqlite')
            s = store.bind('codex', str(uuid.uuid4()), 'codex', 'echo', {'cli': 'C:/old/codex.exe', 'codex_home': 'C:/h'})
            store.add_client(s['id'], 'client-dir', 'tok')
            refreshed = store.refresh_endpoint(s['id'], s['generation'], {'cli': 'C:/new/codex.exe', 'codex_home': 'C:/h'})
            self.assertEqual(refreshed['generation'], s['generation'])
            self.assertEqual(store.authenticate_client('tok')['id'], s['id'])
            for bad in ({'cli': 'x', 'codex_home': 'C:/other'}, {'cli': 'x', 'codex_home': 'C:/h', 'extra': 1}):
                with self.assertRaises(BridgeError):
                    store.refresh_endpoint(s['id'], s['generation'], bad)

    def test_candidates_beside_deleted_versioned_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / 'bin'
            (bin_dir / 'new').mkdir(parents=True)
            (bin_dir / 'new' / 'codex.exe').write_bytes(b'')
            self.assertEqual(adapters.codex_candidates(str(bin_dir / 'old' / 'codex.exe')), [str(bin_dir / 'new' / 'codex.exe')])

    def test_missing_cli_reports_code(self):
        session = {'native_id': str(uuid.uuid4()), 'metadata': json.dumps({'cli': 'C:/gone/codex.exe', 'codex_home': 'h'})}
        with self.assertRaises(adapters.EndpointError) as caught:
            adapters.codex_args(session, '')
        self.assertEqual(caught.exception.code, 'codex_cli_missing')


class ClientDirectoryTests(unittest.TestCase):
    def test_token_folder_in_git_tree_must_be_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / 'repo'
            repo.mkdir()
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, creationflags=flags)
            client = repo / 'agentbridge-client'
            with self.assertRaises(BridgeError):
                check_client_directory(client)
            (repo / '.git' / 'info').mkdir(exist_ok=True)
            (repo / '.git' / 'info' / 'exclude').write_text('/agentbridge-client/\n')
            check_client_directory(client)
            check_client_directory(Path(temporary) / 'outside' / 'agentbridge-client')


class SpawnTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows job objects')
    def test_refused_breakaway_refuses_unless_dependence_accepted(self):
        calls = []
        def popen(args, creationflags=0, **kw):
            calls.append(creationflags)
            if creationflags & 0x01000000:
                raise OSError(13, 'denied', None, 5)
            return 'process'
        with patch('bridge.spawn.in_job', return_value=True):
            with self.assertRaises(BridgeError):
                spawn_detached(['x'], '.', None, None, popen)
            self.assertEqual(len(calls), 1, 'nothing may be started after a refused breakaway')
            self.assertEqual(spawn_detached(['x'], '.', None, None, popen, allow_attached=True), ('process', False))
        self.assertEqual(len(calls), 3)
        with patch('bridge.spawn.in_job', return_value=False):
            self.assertEqual(spawn_detached(['x'], '.', None, None, popen), ('process', True))


class RetentionAndCertificateTests(unittest.TestCase):
    def test_hub_prunes_old_replay_results_and_reports_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_tls(root, ['127.0.0.1'])
            expiry = certificate_expiry((root / 'hub-cert.pem').read_text())
            self.assertIn(expiry['days_left'], (89, 90))
            hub = HubStore(root / 'hub.sqlite')
            with hub.connect() as c:
                c.execute("INSERT INTO rpc_results VALUES('n','old','f','{}',?)", (time.time() - 8 * 86400,))
                c.execute("INSERT INTO rpc_results VALUES('n','new','f','{}',?)", (time.time(),))
            self.assertEqual(hub.prune(), 1)
            with hub.connect() as c:
                self.assertEqual([r[0] for r in c.execute('SELECT request_id FROM rpc_results')], ['new'])


if __name__ == '__main__':
    unittest.main()
