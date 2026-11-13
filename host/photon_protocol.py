"""photon_protocol.py - Photon 飞达协议帧编解码（与固件/OpenPnP 一致的字节级实现）

帧结构（5 字节头 + 载荷）：
    0 toAddress   目的地址（控制器 0x00 / 广播 0xFF / 飞达 0x01..0xFE）
    1 fromAddress 源地址（主机发命令恒为 0x00）
    2 packetId    事务号，飞达应答时原样回显
    3 payloadLen  载荷字节数
    4 crc8        CRC-8（多项式 0x07，初值 0，不反射），覆盖头 4 字节 + 全部载荷
    5..           payload，payload[0] 为命令 ID（请求）或 status（应答）
"""
from __future__ import annotations

from dataclasses import dataclass, field

CONTROLLER = 0x00
BROADCAST = 0xFF

CMD_GET_FEEDER_ID = 0x01
CMD_INITIALIZE_FEEDER = 0x02
CMD_GET_VERSION = 0x03
CMD_MOVE_FEED_FORWARD = 0x04
CMD_MOVE_FEED_BACKWARD = 0x05
CMD_MOVE_FEED_STATUS = 0x06
CMD_VENDOR_OPTIONS = 0xBF
CMD_GET_FEEDER_ADDRESS = 0xC0
CMD_IDENTIFY_FEEDER = 0xC1
CMD_PROGRAM_FEEDER_FLOOR = 0xC2
CMD_UNINITIALIZED_RESPOND = 0xC3

STATUS_OK = 0x00
STATUS_WRONG_FEEDER_ID = 0x01
STATUS_COULDNT_REACH = 0x02
STATUS_UNINITIALIZED_FEEDER = 0x03
STATUS_FEEDING_IN_PROGRESS = 0x04
STATUS_FAIL = 0x05
STATUS_TIMEOUT = 0xFE
STATUS_UNKNOWN_ERROR = 0xFF

UUID_LEN = 12
VENDOR_OPTIONS_LEN = 20


def crc8(data: bytes) -> int:
    """CRC-8/ATM（poly 0x07, init 0, 不反射, 不异或输出）。"""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass
class Packet:
    to: int
    frm: int
    packet_id: int
    payload: bytes = field(default_factory=bytes)

    def encode(self) -> bytes:
        head = bytes([self.to, self.frm, self.packet_id, len(self.payload)])
        return head + bytes([crc8(head + self.payload)]) + bytes(self.payload)

    @property
    def to_hex(self) -> str:
        return self.encode().hex().upper()

    @classmethod
    def decode(cls, data: bytes) -> "Packet | None":
        """严格解析：长度必须精确匹配 5+payloadLen，CRC 必须正确。"""
        if len(data) < 5:
            return None
        plen = data[3]
        if len(data) != 5 + plen:
            return None
        if crc8(data[:4] + data[5:]) != data[4]:
            return None
        return cls(data[0], data[1], data[2], bytes(data[5:]))

    @property
    def status(self) -> int:
        return self.payload[0] if self.payload else STATUS_UNKNOWN_ERROR


# ---------------- 命令构造器（主机侧） ----------------
def get_feeder_id(addr: int, pid: int) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_GET_FEEDER_ID]))


def initialize_feeder(addr: int, pid: int, uuid: bytes) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_INITIALIZE_FEEDER]) + uuid)


def get_version(addr: int, pid: int) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_GET_VERSION]))


def move_feed_forward(addr: int, pid: int, tenths_mm: int) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_MOVE_FEED_FORWARD, tenths_mm & 0xFF]))


def move_feed_backward(addr: int, pid: int, tenths_mm: int) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_MOVE_FEED_BACKWARD, tenths_mm & 0xFF]))


def move_feed_status(addr: int, pid: int) -> Packet:
    return Packet(addr, CONTROLLER, pid, bytes([CMD_MOVE_FEED_STATUS]))


def vendor_options(addr: int, pid: int, options: bytes) -> Packet:
    opts = bytes(options[:VENDOR_OPTIONS_LEN]).ljust(VENDOR_OPTIONS_LEN, b"\x00")
    return Packet(addr, CONTROLLER, pid, bytes([CMD_VENDOR_OPTIONS]) + opts)


def get_feeder_address(uuid: bytes, pid: int) -> Packet:
    return Packet(BROADCAST, CONTROLLER, pid, bytes([CMD_GET_FEEDER_ADDRESS]) + uuid)


def identify_feeder(uuid: bytes, pid: int) -> Packet:
    return Packet(BROADCAST, CONTROLLER, pid, bytes([CMD_IDENTIFY_FEEDER]) + uuid)


def program_feeder_floor(uuid: bytes, new_addr: int, pid: int) -> Packet:
    return Packet(BROADCAST, CONTROLLER, pid,
                  bytes([CMD_PROGRAM_FEEDER_FLOOR]) + uuid + bytes([new_addr]))


def uninitialized_feeders_respond(pid: int) -> Packet:
    return Packet(BROADCAST, CONTROLLER, pid, bytes([CMD_UNINITIALIZED_RESPOND]))
