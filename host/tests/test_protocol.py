"""test_protocol.py - Photon 协议 + 模拟器的自动化测试

运行：在 lumenpnp-feeder 目录下
    python -m unittest discover -s host/tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOST_DIR)

import photon_protocol as pp                     # noqa: E402
from feeder_sim import Bus, FeederParams, FeederSim, calc_eta  # noqa: E402


class TestFrameCodec(unittest.TestCase):
    def test_official_test_vector(self):
        """OpenPnP PhotonFeederTest 的样例帧：29 13 47 01 0A 03。"""
        pkt = pp.Packet(0x2B, 0x13, 0x47, b"\x03")
        self.assertEqual(pkt.encode(), bytes([0x2B, 0x13, 0x47, 0x01, 0x0A, 0x03]))
        self.assertEqual(pkt.to_hex, "2B1347010A03")

    def test_crc8_known_values(self):
        self.assertEqual(pp.crc8(b""), 0x00)
        self.assertEqual(pp.crc8(b"\x2B\x13\x47\x01\x03"), 0x0A)   # 官方样例帧头 4B + 载荷
        self.assertEqual(pp.crc8(b"123456789"), 0xF4)   # CRC-8/ATM 标准校验值

    def test_roundtrip(self):
        pkt = pp.Packet(0x01, 0x00, 0x5A, bytes([0x04, 0x28]))
        self.assertEqual(pp.Packet.decode(pkt.encode()), pkt)

    def test_reject_bad_crc(self):
        raw = bytearray(pp.Packet(0x01, 0x00, 0x10, b"\x03").encode())
        raw[4] ^= 0xFF
        self.assertIsNone(pp.Packet.decode(bytes(raw)))

    def test_reject_bad_length(self):
        raw = pp.Packet(0x01, 0x00, 0x10, b"\x03").encode()
        self.assertIsNone(pp.Packet.decode(raw[:-1]))
        self.assertIsNone(pp.Packet.decode(raw + b"\x00"))
        self.assertIsNone(pp.Packet.decode(b"\x01\x00\x10"))

    def test_command_builders(self):
        self.assertEqual(pp.move_feed_forward(0x03, 0x11, 40).payload, bytes([0x04, 0x28]))
        self.assertEqual(pp.move_feed_backward(0x03, 0x11, 40).payload, bytes([0x05, 0x28]))
        self.assertEqual(pp.move_feed_status(0x03, 0x11).payload, bytes([0x06]))
        self.assertEqual(pp.get_version(0x03, 0x11).payload, bytes([0x03]))

        uuid = bytes(range(12))
        self.assertEqual(pp.initialize_feeder(0x03, 0x11, uuid).payload, bytes([0x02]) + uuid)
        p = pp.program_feeder_floor(uuid, 0x07, 0x11)
        self.assertEqual(p.to, pp.BROADCAST)
        self.assertEqual(p.payload, bytes([0xC2]) + uuid + b"\x07")
        self.assertEqual(pp.uninitialized_feeders_respond(0x11).payload, bytes([0xC3]))

    def test_vendor_payload_padded(self):
        p = pp.vendor_options(0x03, 0x11, bytes([0x02]))
        self.assertEqual(len(p.payload), 1 + pp.VENDOR_OPTIONS_LEN)


class TestFeederLifecycle(unittest.TestCase):
    def setUp(self):
        self.f = FeederSim()
        self.bus = Bus([self.f])

    def test_power_on_unaddressed(self):
        self.assertEqual(self.f.local_addr, pp.BROADCAST)
        self.assertFalse(self.f.initialized)

    def test_get_feeder_id(self):
        r = self.bus.send(pp.get_feeder_id(pp.BROADCAST, 0x01))
        self.assertEqual(len(r), 1)
        pkt = r[0][1]
        self.assertEqual(pkt.to, pp.CONTROLLER)
        self.assertEqual(pkt.frm, self.f.local_addr)
        self.assertEqual(pkt.packet_id, 0x01)
        self.assertEqual(pkt.payload[0], pp.STATUS_OK)
        self.assertEqual(pkt.payload[1:], self.f.uuid)

    def test_broadcast_scan_then_program_then_initialize(self):
        """OpenPnP 的标准扫描流程：0xC3→0xC2→0x02→0x04→0x06。"""
        # 1) 扫描
        r = self.bus.send(pp.uninitialized_feeders_respond(0x21))
        self.assertEqual(len(r), 1)
        uuid = r[0][1].payload[1:]
        self.assertEqual(uuid, self.f.uuid)

        # 2) 编址到槽位 0x07
        r = self.bus.send(pp.program_feeder_floor(uuid, 0x07, 0x22))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_OK)
        self.assertEqual(self.f.local_addr, 0x07)

        # 3) 初始化（绑定 UUID）
        r = self.bus.send(pp.initialize_feeder(0x07, 0x23, uuid))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_OK)
        self.assertTrue(self.f.initialized)

        # 4) 送料 4.0mm
        r = self.bus.send(pp.move_feed_forward(0x07, 0x24, 40))
        self.assertEqual(len(r[0][1].payload), 3)
        self.assertEqual(r[0][1].payload, bytes([0x00, 0x03, 0x34]))   # 820ms 大端（40×8+500）

        # 5) 查询结果
        r = self.bus.send(pp.move_feed_status(0x07, 0x25))
        self.assertEqual(r[0][1].payload, bytes([pp.STATUS_OK]))

    def test_initialize_wrong_uuid(self):
        r = self.bus.send(pp.initialize_feeder(pp.BROADCAST, 0x01, bytes(12)))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_WRONG_FEEDER_ID)
        self.assertFalse(self.f.initialized)

    def test_initialize_short_payload(self):
        r = self.bus.send(pp.Packet(pp.BROADCAST, 0, 1, b"\x02"))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_WRONG_FEEDER_ID)

    def test_feed_before_initialize_returns_uninitialized(self):
        r = self.bus.send(pp.move_feed_forward(pp.BROADCAST, 0x01, 40))
        pkt = r[0][1]
        self.assertEqual(pkt.payload[0], pp.STATUS_UNINITIALIZED_FEEDER)
        self.assertEqual(len(pkt.payload), 1 + 12)
        self.assertEqual(pkt.payload[1:], self.f.uuid)

    def test_feed_backward_eta(self):
        self.f.initialized = True
        r = self.bus.send(pp.move_feed_backward(pp.BROADCAST, 0x01, 40))
        self.assertEqual(r[0][1].payload, bytes([0x00, 0x0A, 0xF0]))   # 2800ms

    def test_eta_boundaries(self):
        self.assertEqual(calc_eta(1, True), 508)
        self.assertEqual(calc_eta(1, False), 460)
        self.assertEqual(calc_eta(255, True), 255 * 8 + 500)
        self.assertTrue(calc_eta(255, True) < 65536)

    def test_feed_zero_distance_fails(self):
        self.f.initialized = True
        r = self.bus.send(pp.move_feed_forward(pp.BROADCAST, 0x01, 0))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_OK)   # 先应答
        r = self.bus.send(pp.move_feed_status(pp.BROADCAST, 0x02))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_COULDNT_REACH)

    def test_status_initial_value(self):
        r = self.bus.send(pp.move_feed_status(pp.BROADCAST, 0x01))
        self.assertEqual(r[0][1].payload, bytes([pp.STATUS_OK]))

    def test_address_filter(self):
        self.f.local_addr = 0x05
        self.assertEqual(self.bus.send(pp.get_feeder_id(0x06, 0x01)), [])
        self.assertEqual(len(self.bus.send(pp.get_feeder_id(0x05, 0x01))), 1)

    def test_c0_returns_address_in_header(self):
        self.f.local_addr = 0x0A
        r = self.bus.send(pp.get_feeder_address(self.f.uuid, 0x31))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0][1].frm, 0x0A)
        self.assertEqual(r[0][1].to, pp.CONTROLLER)
        self.assertEqual(r[0][1].payload, bytes([pp.STATUS_OK]))
        self.assertEqual(self.bus.send(pp.get_feeder_address(bytes(12), 0x32)), [])

    def test_c1_identify(self):
        self.f.local_addr = 0x0A
        r = self.bus.send(pp.identify_feeder(self.f.uuid, 0x33))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_OK)
        self.assertEqual(self.f.identify_events, 1)
        self.assertEqual(self.bus.send(pp.identify_feeder(bytes(12), 0x34)), [])

    def test_c3_ignored_when_initialized(self):
        self.f.initialized = True
        self.assertEqual(self.bus.send(pp.uninitialized_feeders_respond(0x01)), [])

    def test_c3_backoff_range(self):
        self.bus.send(pp.uninitialized_feeders_respond(0x01))
        self.assertLess(self.f.backoff_ms, 8)

    def test_c2_rejects_reserved_addresses(self):
        for bad in (0x00, 0xFF):
            r = self.bus.send(pp.program_feeder_floor(self.f.uuid, bad, 0x01))
            self.assertEqual(r[0][1].payload[0], pp.STATUS_FAIL)
        self.assertEqual(self.f.local_addr, pp.BROADCAST)

    def test_c2_storage_failure_keeps_address(self):
        self.f.storage_ok = False
        r = self.bus.send(pp.program_feeder_floor(self.f.uuid, 0x09, 0x01))
        self.assertEqual(r[0][1].payload[0], pp.STATUS_FAIL)
        self.assertEqual(self.f.local_addr, pp.BROADCAST)
        self.assertEqual(self.f.params.address, 0x09)   # 固件同样先写 RAM 再存

    def test_address_persists_across_power_cycle(self):
        self.bus.send(pp.program_feeder_floor(self.f.uuid, 0x0C, 0x01))
        self.f.power_cycle()
        self.assertEqual(self.f.local_addr, 0x0C)
        self.assertFalse(self.f.initialized)          # initialized 是 RAM 态

    def test_protocol_version(self):
        r = self.bus.send(pp.get_version(pp.BROADCAST, 0x01))
        self.assertEqual(r[0][1].payload, bytes([pp.STATUS_OK, 0x01]))


class TestVendorOptions(unittest.TestCase):
    def setUp(self):
        self.f = FeederSim()
        self.f.initialized = True
        self.bus = Bus([self.f])

    def _send(self, sub: int, extra: bytes = b"", pid: int = 0x10):
        return self.bus.send(pp.vendor_options(pp.BROADCAST, pid, bytes([sub]) + extra))

    def test_vendor_requires_initialize(self):
        self.f.initialized = False
        r = self._send(0x01)
        self.assertEqual(r[0][1].payload[0], pp.STATUS_UNINITIALIZED_FEEDER)

    def test_tape_present(self):
        r = self._send(0x01)
        self.assertEqual(r[0][1].payload[0], pp.STATUS_OK)
        body = r[0][1].payload[1:]
        self.assertEqual(len(body), 20)
        self.assertEqual(body[0], 0x01)
        self.assertEqual(body[1], 0x01)
        self.assertEqual(body[2], 0xFF)

    def test_counters(self):
        self.bus.send(pp.move_feed_forward(pp.BROADCAST, 0x11, 40))
        self.bus.send(pp.move_feed_forward(pp.BROADCAST, 0x12, 40))
        body = self._send(0x02, pid=0x13)[0][1].payload[1:]
        self.assertEqual(body[3:7], bytes([2, 0, 0, 0]))

    def test_ticks_calibration(self):
        body = self._send(0x03)[0][1].payload[1:]
        self.assertEqual(body[1:3], bytes([400 >> 8, 400 & 0xFF]))

        body = self._send(0x03, bytes([0x01, 0x2C]))[0][1].payload[1:]   # 300
        self.assertEqual(self.f.params.ticks_per_mm, 300)
        self.assertEqual(body[1:3], bytes([0x01, 0x2C]))

        body = self._send(0x03, bytes([0x00, 0x0A]))[0][1].payload[1:]   # 10 越界
        self.assertEqual(body[1], 0xFF)
        self.assertEqual(self.f.params.ticks_per_mm, 300)

    def test_diagnostics(self):
        self.bus.send(pp.move_feed_forward(pp.BROADCAST, 0x11, 40))
        body = self._send(0x04, pid=0x14)[0][1].payload[1:]
        self.assertEqual(body[1], 0x01)          # 协议版本
        self.assertEqual(body[2], pp.STATUS_OK)  # 最近送料结果
        self.assertEqual(body[7], self.f.params.flags)

    def test_flags_switch(self):
        body = self._send(0x05, bytes([0x00]))[0][1].payload[1:]
        self.assertEqual(self.f.params.flags & 0x01, 0)
        self.assertEqual(body[1], self.f.params.flags)
        self._send(0x05, bytes([0x01]))
        self.assertEqual(self.f.params.flags & 0x01, 1)

    def test_unknown_subcommand(self):
        body = self._send(0x77)[0][1].payload[1:]
        self.assertEqual(body[0], 0xFF)


class TestBus(unittest.TestCase):
    def test_broadcast_hits_all_feeders(self):
        f1 = FeederSim(uuid=bytes([1] * 12))
        f2 = FeederSim(uuid=bytes([2] * 12))
        bus = Bus([f1, f2])
        r = bus.send(pp.uninitialized_feeders_respond(0x01))
        self.assertEqual(len(r), 2)                    # 两台都会应答 → 真实总线会冲突
        self.assertEqual({x[1].frm for x in r}, {pp.BROADCAST})

        # 编址后只应答自己的 UUID，不会互相串台
        bus.send(pp.program_feeder_floor(f1.uuid, 0x04, 0x02))
        bus.send(pp.program_feeder_floor(f2.uuid, 0x06, 0x03))
        r = bus.send(pp.get_feeder_id(0x04, 0x04))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0][0], f1)
        self.assertEqual(f2.local_addr, 0x06)

    def test_unicast_only_hits_matching(self):
        f1 = FeederSim()
        f2 = FeederSim()
        f1.local_addr = 0x02
        f2.local_addr = 0x03
        f1.initialized = f2.initialized = True
        bus = Bus([f1, f2])
        r = bus.send(pp.move_feed_forward(0x02, 0x01, 40))
        self.assertEqual(len(r), 1)
        self.assertEqual(f1.params.feed_count, 1)
        self.assertEqual(f2.params.feed_count, 0)

    def test_corrupt_frame_ignored(self):
        f = FeederSim()
        bus = Bus([f])
        raw = bytearray(pp.get_feeder_id(pp.BROADCAST, 0x01).encode())
        raw[4] ^= 0x5A
        self.assertEqual(bus.send_raw(bytes(raw)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
