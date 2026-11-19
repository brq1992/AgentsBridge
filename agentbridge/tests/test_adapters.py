import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge import adapters
from bridge.store import BridgeError

UUID = '2295c6e6-3bed-4a9a-b85c-6cc14620d530'
PIPE = r'\\.\pipe\LOCAL\cc-msg-' + 'a' * 32


class ClaudePreflightTests(unittest.TestCase):
    def make(self, version):
        d = Path(tempfile.mkdtemp())
        reg = {'sessionId': UUID, 'pid': 4242, 'procStart': '99', 'messagingSocketPath': PIPE,
               'version': version, 'pidDomain': 'win32:test'}
        (d / '4242.json').write_text(json.dumps(reg), encoding='utf8')
        (d / ('4242.' + 'b' * 64 + '.key')).write_text(json.dumps(
            {'peerToken': 'c' * 32, 'procStartFt': '99', 'pidDomain': 'win32:test'}), encoding='utf8')
        meta = {'registry': str(d / '4242.json'), 'pid': 4242, 'procStart': '99',
                'version': version, 'messagingSocketPath': PIPE}
        return {'native_id': UUID, 'metadata': json.dumps(meta)}

    def test_qualified_versions_pass(self):
        for version in ('2.1.321', '2.1.325'):
            with patch('bridge.adapters.process_start', return_value='99'):
                self.assertEqual(adapters.claude_preflight(self.make(version)), (PIPE, 'c' * 32))

    def test_unqualified_version_still_rejected(self):
        with patch('bridge.adapters.process_start', return_value='99'):
            with self.assertRaisesRegex(BridgeError, 'unqualified'):
                adapters.claude_preflight(self.make('2.1.999'))


if __name__ == '__main__':
    unittest.main()
