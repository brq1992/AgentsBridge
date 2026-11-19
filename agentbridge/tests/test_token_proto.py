import json
import unittest
from unittest import mock

from bridge import adapters, cli


class EchoPolicy(unittest.TestCase):
    def test_json_echo_returns_text(self):
        self.assertEqual(cli.echo_text('{"op":"echo","text":"中文✓"}'), ('中文✓', 'completed'))

    def test_json_notice_is_blocked(self):
        self.assertEqual(cli.echo_text('{"op":"notice","text":"do x"}'), (cli.NOTICE_TEXT, 'blocked'))

    def test_plain_text_is_a_notice(self):
        self.assertEqual(cli.echo_text('echo: hi 🙂'), (cli.NOTICE_TEXT, 'blocked'))


class Handle(unittest.TestCase):
    def run_handle(self, claimed):
        calls = []
        with mock.patch.object(cli, 'claim', lambda s, b: claimed), \
                mock.patch.object(cli, 'call', lambda s, r, b: calls.append((r, b)) or {'id': 'r1', 'delivery': 'accepted'}):
            result = cli.handle_message('/x', {'actor': 'claude:u', 'message_id': 'm1', 'context': {}})
        return result, calls

    def test_echo_request_replies_once_with_stored_text(self):
        result, calls = self.run_handle({'should_execute': True, 'kind': 'request', 'expect_reply': True,
                                         'body': '{"op":"echo","text":"abc"}'})
        self.assertEqual(result['action'], 'echoed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['body'], 'abc')

    def test_not_authorised_never_replies(self):
        result, calls = self.run_handle({'should_execute': False, 'reason': 'claim_already_received'})
        self.assertEqual((result['action'], calls), ('already_received', []))

    def test_reply_kind_is_ack_only(self):
        result, calls = self.run_handle({'should_execute': True, 'kind': 'reply', 'body': 'x'})
        self.assertEqual((result['action'], calls), ('received', []))


class Quiet(unittest.TestCase):
    def test_drops_payload_and_fingerprint(self):
        q = cli.quiet({'id': 'm', 'delivery': 'accepted', 'body': 'long', 'fingerprint': 'f' * 64, 'error': None})
        self.assertEqual(q, {'id': 'm', 'delivery': 'accepted'})


class Envelope(unittest.TestCase):
    def test_compact_has_one_command_and_short_preview(self):
        m = {'id': 'm1', 'kind': 'request', 'sender': 'n:claude:u', 'parent_id': None, 'hop': 0,
             'body': 'x' * 1000, 'network': True}
        with mock.patch.object(adapters, 'COMPACT_ENVELOPE', True):
            text = adapters.envelope(m, {'tool': 'claude', 'native_id': 'u'}, '/tmp/c')
        self.assertEqual(text.count(' handle --message m1'), 1)
        self.assertNotIn(' reply --message', text)
        self.assertLess(len(text) - len(adapters.envelope.__code__.co_filename), 600)


if __name__ == '__main__':
    unittest.main()
