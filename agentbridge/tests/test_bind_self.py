import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from bridge import selfbind  # noqa: E402
from bridge.store import BridgeError  # noqa: E402
import install_skill  # noqa: E402

CLAUDE = '11111111-1111-4111-8111-111111111111'
CODEX = '22222222-2222-4222-8222-222222222222'


class Identify(unittest.TestCase):
    def test_codex_thread_wins(self):
        self.assertEqual(selfbind.identify({'CODEX_THREAD_ID': CODEX}, [1], []), ('codex', CODEX))

    def test_claude_found_through_parent_chain(self):
        entries = [{'pid': 50, 'sessionId': 'other'}, {'pid': 30, 'sessionId': CLAUDE}]
        self.assertEqual(selfbind.identify({}, [99, 40, 30, 1], entries), ('claude', CLAUDE))

    def test_nearest_ancestor_is_used(self):
        entries = [{'pid': 1, 'sessionId': 'outer'}, {'pid': 30, 'sessionId': CLAUDE}]
        self.assertEqual(selfbind.identify({}, [99, 30, 1], entries)[1], CLAUDE)

    def test_unrelated_claude_is_never_picked(self):
        with self.assertRaises(BridgeError):
            selfbind.identify({}, [99, 40, 1], [{'pid': 30, 'sessionId': CLAUDE}])

    def test_ancestors_of_this_process(self):
        chain = selfbind.ancestors()
        self.assertEqual(chain[0], __import__('os').getpid())
        self.assertGreater(len(chain), 1)


class BindSelf(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / 'state'
        self.state.mkdir()
        (self.state / 'network.json').write_text(json.dumps({'label': 'node-a'}), encoding='utf8')
        self.calls = []

    def call(self, state, route, body):
        self.calls.append((route, body))
        return {'generation': 'g1'} if route == '/bind' else {'scope': 'session-only'}

    def test_binds_then_grants_own_session(self):
        r = selfbind.bind_self(self.state, 'claude3', call=self.call, identity=('claude', CLAUDE))
        self.assertEqual([c[0] for c in self.calls], ['/bind', '/grant'])
        bind = self.calls[0][1]
        self.assertEqual((bind['tool'], bind['native_id'], bind['alias'], bind['actor']),
                         ('claude', CLAUDE, 'claude3', 'claude:' + CLAUDE))
        self.assertEqual(bind['project'], selfbind.default_project())
        client = Path(r['client_directory'])
        self.assertEqual(client.name, 'agentbridge-client')
        self.assertNotIn(self.state.resolve(), client.parents)
        self.assertEqual(self.calls[1][1]['directory'], str(client))
        self.assertEqual(r['address'], 'node-a/claude3')
        self.assertIn('--session ' + CLAUDE, r['command_prefix'])
        self.assertNotIn(str(self.state.resolve().as_posix()) + '"', r['command_prefix'])

    def test_explicit_project(self):
        selfbind.bind_self(self.state, 'c', project='p2', call=self.call, identity=('claude', CLAUDE))
        self.assertEqual(self.calls[0][1]['project'], 'p2')

    def test_bad_alias_rejected_before_any_call(self):
        for alias in ('', 'a/b', '-x', 'x' * 33, 'a b'):
            with self.assertRaises(BridgeError):
                selfbind.bind_self(self.state, alias, call=self.call, identity=('claude', CLAUDE))
        self.assertEqual(self.calls, [])

    def test_failed_bind_grants_nothing(self):
        def refuse(state, route, body):
            self.calls.append(route)
            raise BridgeError('alias already bound; unbind/rename explicitly')
        with self.assertRaises(BridgeError):
            selfbind.bind_self(self.state, 'taken', call=refuse, identity=('claude', CLAUDE))
        self.assertEqual(self.calls, ['/bind'])


class Skill(unittest.TestCase):
    def test_render_fills_every_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            text = install_skill.render(Path(d, 'state'), python='/x/python')
        self.assertNotIn('{{', text)
        self.assertIn('/x/python', text)
        self.assertTrue(text.startswith('---\nname: agentbridge\n'))

    def test_install_is_idempotent_and_backs_up_changes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(install_skill.install('one', [d])[0]['changed'])
            self.assertFalse(install_skill.install('one', [d])[0]['changed'])
            install_skill.install('two', [d])
            folder = Path(d, 'agentbridge')
            self.assertEqual((folder / 'SKILL.md').read_text(encoding='utf8'), 'two')
            self.assertEqual([p.read_text(encoding='utf8') for p in folder.glob('SKILL.md.bak-*')], ['one'])


if __name__ == '__main__':
    unittest.main()
