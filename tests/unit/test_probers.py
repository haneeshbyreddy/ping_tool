import asyncio
import os
import queue
import socket
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "src"))

from wisp.config import Config
from wisp.ingress.probers import (
    IcmpProber, SingleSocketIcmpProber, build_echo_request, build_prober,
    icmp_checksum, parse_echo_reply,
)

_FAKE_IP_HEADER = bytes([0x45]) + bytes(19)

def _reply_for(request: bytes, *, ident: int | None = None,
               kind: int = 0) -> bytes:
    req_ident, seq = struct.unpack("!HH", request[4:8])
    if ident is None:
        ident = req_ident
    payload = request[8:]
    header = struct.pack("!BBHHH", kind, 0, 0, ident, seq)
    csum = icmp_checksum(header + payload)
    icmp = struct.pack("!BBHHH", kind, 0, csum, ident, seq) + payload
    return _FAKE_IP_HEADER + icmp

class FakeIcmpSocket:

    def __init__(self, reply_with=None):
        self._q: queue.Queue = queue.Queue()
        self._timeout = 1.0
        self.sent: list[tuple[bytes, tuple]] = []
        self.reply_with = reply_with if reply_with is not None else (
            lambda req, ip: (_reply_for(req), (ip, 0)))

    def settimeout(self, t):
        self._timeout = t

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        item = self.reply_with(data, addr[0])
        if item is not None:
            self._q.put(item)

    def recvfrom(self, bufsize):
        try:
            item = self._q.get(timeout=self._timeout)
        except queue.Empty:
            raise socket.timeout()
        if item is None:
            raise OSError("socket closed")
        return item

    def close(self):
        self._q.put(None)

def _run(coro):
    return asyncio.run(coro)

def _prober(sock, **kw):
    kw.setdefault("interval", 0.01)
    kw.setdefault("timeout", 0.25)
    return SingleSocketIcmpProber(sock_factory=lambda: sock, **kw)

class ChecksumAndPacketTest(unittest.TestCase):
    def test_checksum_of_checksummed_packet_folds_to_zero(self):
        pkt = build_echo_request(0x1234, 7)
        self.assertEqual(icmp_checksum(pkt), 0)

    def test_checksum_odd_length(self):
        self.assertEqual(icmp_checksum(b"\x01"), ~0x0100 & 0xFFFF)

    def test_build_parse_round_trip(self):
        req = build_echo_request(0xBEEF, 42)
        self.assertEqual(parse_echo_reply(_reply_for(req)), (0xBEEF, 42))

    def test_parse_rejects_echo_request_and_errors(self):
        req = build_echo_request(1, 1)
        self.assertIsNone(parse_echo_reply(_reply_for(req, kind=8)))
        self.assertIsNone(parse_echo_reply(_reply_for(req, kind=3)))
        self.assertIsNone(parse_echo_reply(b"\x45" + bytes(10)))
        self.assertIsNone(parse_echo_reply(b"\x60" + bytes(40)))

class SingleSocketProberTest(unittest.TestCase):
    def test_all_replies_zero_loss(self):
        sock = FakeIcmpSocket()
        p = _prober(sock)
        try:
            res = _run(p.ping("10.0.0.1", 3))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 0.0)
        self.assertIsNotNone(res.latency_ms)
        self.assertGreater(res.latency_ms, 0.0)
        self.assertIsNotNone(res.jitter_ms)
        self.assertEqual(len(sock.sent), 3)

    def test_single_reply_jitter_is_zero(self):
        p = _prober(FakeIcmpSocket())
        try:
            res = _run(p.ping("10.0.0.1", 1))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 0.0)
        self.assertEqual(res.jitter_ms, 0.0)

    def test_no_replies_is_total_loss(self):
        p = _prober(FakeIcmpSocket(reply_with=lambda req, ip: None))
        try:
            res = _run(p.ping("10.0.0.2", 2))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 100.0)
        self.assertIsNone(res.latency_ms)
        self.assertIsNone(res.jitter_ms)

    def test_partial_loss(self):
        dropped = iter([True, False, True, False])
        p = _prober(FakeIcmpSocket(
            reply_with=lambda req, ip:
                None if next(dropped) else (_reply_for(req), (ip, 0))))
        try:
            res = _run(p.ping("10.0.0.3", 4))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 50.0)
        self.assertIsNotNone(res.latency_ms)

    def test_foreign_ident_ignored(self):
        p = _prober(FakeIcmpSocket(
            reply_with=lambda req, ip: (_reply_for(req, ident=0xDEAD), (ip, 0))))
        try:
            res = _run(p.ping("10.0.0.4", 1))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 100.0)

    def test_mismatched_source_ignored(self):
        p = _prober(FakeIcmpSocket(
            reply_with=lambda req, ip: (_reply_for(req), ("192.0.2.99", 0))))
        try:
            res = _run(p.ping("10.0.0.5", 1))
        finally:
            p.close()
        self.assertEqual(res.packet_loss, 100.0)

    def test_concurrent_pings_match_by_sequence(self):
        sock = FakeIcmpSocket()
        p = _prober(sock)

        async def both():
            return await asyncio.gather(
                p.ping("10.0.0.6", 2), p.ping("10.0.0.7", 2))
        try:
            a, b = _run(both())
        finally:
            p.close()
        self.assertEqual((a.ip, a.packet_loss), ("10.0.0.6", 0.0))
        self.assertEqual((b.ip, b.packet_loss), ("10.0.0.7", 0.0))
        seqs = [struct.unpack("!HH", d[4:8])[1] for d, _ in sock.sent]
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_permission_error_becomes_runtime_error(self):
        def denied():
            raise PermissionError("raw sockets need privilege")
        p = SingleSocketIcmpProber(sock_factory=denied)
        with self.assertRaises(RuntimeError):
            _run(p.ping("10.0.0.8", 1))

    def test_preflight_pings_loopback(self):
        sock = FakeIcmpSocket()
        p = _prober(sock)
        try:
            _run(p.preflight())
        finally:
            p.close()
        self.assertEqual(sock.sent[0][1][0], "127.0.0.1")

class BuildProberTest(unittest.TestCase):
    def test_forced_singlesock(self):
        self.assertIsInstance(
            build_prober(Config(prober="singlesock")), SingleSocketIcmpProber)

    def test_forced_icmplib(self):
        self.assertIsInstance(build_prober(Config(prober="icmplib")), IcmpProber)

    def test_default_is_platform_picked(self):
        expected = (SingleSocketIcmpProber if sys.platform.startswith("win")
                    else IcmpProber)
        self.assertIsInstance(build_prober(Config(prober="icmp")), expected)

if __name__ == "__main__":
    unittest.main()
