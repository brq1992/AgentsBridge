import socket
import unittest
from unittest import mock

from bridge import adapters
from bridge.store import BridgeError


class FakeSock:
    def __init__(self):
        self.sent = b''
        self.closed = False

    def settimeout(self, t): pass
    def connect(self, path): pass
    def setblocking(self, b): pass
    def recv(self, n): return b''
    def sendall(self, data): self.sent += data
    def close(self): self.closed = True


class MacPeerCheck(unittest.TestCase):
    def run_write(self, peer, start='Wed Nov 18 03:00:00 2026'):
        fake = FakeSock()
        with mock.patch.object(socket, 'AF_UNIX', 1, create=True), \
                mock.patch.object(socket, 'socket', lambda *a: fake), \
                mock.patch.object(adapters, 'peer_pid', lambda s: peer), \
                mock.patch.object(adapters, 'process_start', lambda pid: start):
            try:
                result = adapters.write_unix('/tmp/x.sock', 't' * 32, 'hello', 4242,
                                             'Wed Nov 18 03:00:00 2026', receipt_wait=0)
            except BridgeError:
                result = None
        return fake, result

    def test_matching_peer_is_written(self):
        fake, result = self.run_write(4242)
        self.assertEqual(result['transport'], 'unix_socket')
        self.assertIn(b'"auth"', fake.sent)

    def test_wrong_pid_sends_nothing(self):
        fake, result = self.run_write(9999)
        self.assertIsNone(result)
        self.assertEqual(fake.sent, b'')
        self.assertTrue(fake.closed)

    def test_reused_pid_sends_nothing(self):
        fake, result = self.run_write(4242, start='Tue Nov 17 01:00:00 2026')
        self.assertIsNone(result)
        self.assertEqual(fake.sent, b'')


class MacPeerRequired(unittest.TestCase):
    def test_missing_peer_identity_sends_nothing(self):
        fake = FakeSock()
        with mock.patch.object(socket, 'AF_UNIX', 1, create=True), \
                mock.patch.object(socket, 'socket', lambda *a: fake), \
                mock.patch.object(adapters, 'peer_pid', lambda s: 4242), \
                mock.patch.object(adapters, 'process_start', lambda pid: 'x'):
            with self.assertRaises(BridgeError):
                adapters.write_unix('/tmp/x.sock', 't' * 32, 'hello', None, None, receipt_wait=0)
        self.assertEqual(fake.sent, b'')

    def test_deliver_rejects_target_without_peer(self):
        with mock.patch.object(adapters.os, 'name', 'posix'):
            with self.assertRaises(BridgeError):
                adapters.deliver(('claude', ('/tmp/x.sock', 't' * 32), 'hello'))


if __name__ == '__main__':
    unittest.main()
