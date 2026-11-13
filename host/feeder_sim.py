"""feeder_sim.py - 飞达协议模拟器（逐条镜像 firmware/src/photon.c 的分发逻辑）

用途：
1. 没有硬件时跑通 OpenPnP 侧的完整交互流程（M3 联调点的替代品）；
2. 作为 host/tests 的参考实现，验证帧格式、CRC、状态码、ETA 字节序；
3. feeder_cli.py 的 --sim 模式。

与固件一致的行为（改动时请同步 firmware/src/photon.c）：
  * 上电地址：storage 里有合法地址则用之，否则 0xFF（未编址，只响应广播）
  * _initialized 只在 RAM 中，掉电清零，主机每次连接都要重新发 0x02
  * 0x04/0x05 先应答 ETA（大端 uint16，ms）再"运动"；运动期间不响应别的帧
  * 0x06 只返回最近一次送料结果（0x00 OK / 0x02 COULDNT_REACH）
  * 0xC0 的应答地址在帧头 fromAddress；0xC2 写存储成功后才切地址
  * 0xC3 只由未初始化飞达应答，并按 UUID 错峰 0..7ms
"""
from __future__ import annotations

from dataclasses import dataclass, field

from photon_protocol import (
    BROADCAST,
    CONTROLLER,
    CMD_GET_FEEDER_ADDRESS,
    CMD_GET_FEEDER_ID,
    CMD_GET_VERSION,
    CMD_IDENTIFY_FEEDER,
    CMD_INITIALIZE_FEEDER,
    CMD_MOVE_FEED_BACKWARD,
    CMD_MOVE_FEED_FORWARD,
    CMD_MOVE_FEED_STATUS,
    CMD_PROGRAM_FEEDER_FLOOR,
    CMD_UNINITIALIZED_RESPOND,
    CMD_VENDOR_OPTIONS,
    Packet,
    STATUS_COULDNT_REACH,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_UNINITIALIZED_FEEDER,
    STATUS_WRONG_FEEDER_ID,
    UUID_LEN,
    VENDOR_OPTIONS_LEN,
    crc8,
)

# 与 firmware/include/config.h 保持一致的常量
FEED_MS_PER_TENTH_MM = 8
FEED_MS_FIXED = 500
FEED_BACK_MS_PER_TENTH = 60
FEED_BACK_MS_FIXED = 400
PHOTON_PROTOCOL_VERSION = 0x01
PHOTON_MAX_PAYLOAD = 21
PHOTON_C3_BACKOFF_MAX_MS = 8
ENC_TICKS_PER_MM_DEFAULT = 400
VENDOR_RESP_LEN = 20


def calc_eta(tenths: int, forward: bool) -> int:
    """与 photon.c calc_eta() 相同的整数算法。"""
    if forward:
        return tenths * FEED_MS_PER_TENTH_MM + FEED_MS_FIXED
    return tenths * FEED_BACK_MS_PER_TENTH + FEED_BACK_MS_FIXED


@dataclass
class FeederParams:
    """与 storage.h 的 fe_params_t 对应（模拟器里直接存 JSON 即可）。"""

    magic: int = 0x5AA5
    address: int = 0x00
    flags: int = 0x01          # bit0：剥膜边沿自检（出厂默认开启）
    serial: list[int] = field(default_factory=lambda: [0x00, 0x00, 0x00])
    ticks_per_mm: int = ENC_TICKS_PER_MM_DEFAULT
    feed_count: int = 0
    stall_count: int = 0


class FeederSim:
    """一台飞达。多台飞达放在一起就是一条 RS485 总线（见下方 Bus）。"""

    def __init__(self, uuid: bytes | None = None, params: FeederParams | None = None,
                 storage_ok: bool = True):
        self.uuid = bytes(uuid) if uuid else bytes(
            [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x08, 0x09, 0x0A, 0x01, 0x00])
        if len(self.uuid) != UUID_LEN:
            raise ValueError("UUID 必须是 12 字节")
        self.params = params or FeederParams()
        self.storage_ok = storage_ok      # False = 模拟 IAP 写失败（擦写寿命/校验失败）
        self.initialized = False          # RAM 态，power_cycle() 会清零
        self.last_feed_status = STATUS_OK
        self.identify_events = 0          # 闪灯次数，供测试断言
        self.led_on = False
        self.peel_blocked = True          # 模拟剥膜光电（True=被遮挡）
        self.tape_present = True          # 模拟料带在位
        self.runtime_ms = 0
        self.motion_log: list[tuple[int, bool]] = []   # (tenths, forward)
        self.power_cycle()

    # ---------------- 掉电/上电 ----------------
    def power_cycle(self) -> None:
        """上电：地址来自存储，initialized 清 0（与固件一致）。"""
        self.initialized = False
        self.local_addr = self.params.address if 0x01 <= self.params.address <= 0xFE else BROADCAST
        self.last_feed_status = STATUS_OK
        self.led_on = False
        self.runtime_ms = 0

    @property
    def initialized_address(self) -> int:
        """主机视角的"是否已编址"：地址合法即视为已编址。"""
        return 0x01 <= self.params.address <= 0xFE

    # ---------------- 收发 ----------------
    def receive(self, raw: bytes) -> list[Packet]:
        """收到一帧原始字节，返回需要发出的应答帧列表（可能为空）。"""
        pkt = Packet.decode(raw)
        if pkt is None:
            return []
        return self.handle(pkt)

    def handle(self, pkt: Packet) -> list[Packet]:
        if not pkt.payload:
            return []                                  # 固件：payloadLen=0 不处理
        if len(pkt.payload) > PHOTON_MAX_PAYLOAD:
            return []                                  # 固件：超长直接丢弃
        if pkt.to != self.local_addr and pkt.to != BROADCAST:
            return []                                  # 地址不匹配，静默丢弃

        cmd = pkt.payload[0]
        handler = {
            CMD_GET_FEEDER_ID: self._cmd_get_id,
            CMD_INITIALIZE_FEEDER: self._cmd_initialize,
            CMD_GET_VERSION: self._cmd_version,
            CMD_MOVE_FEED_FORWARD: self._cmd_move,
            CMD_MOVE_FEED_BACKWARD: self._cmd_move,
            CMD_MOVE_FEED_STATUS: self._cmd_status,
            CMD_VENDOR_OPTIONS: self._cmd_vendor,
            CMD_GET_FEEDER_ADDRESS: self._cmd_get_address,
            CMD_IDENTIFY_FEEDER: self._cmd_identify,
            CMD_PROGRAM_FEEDER_FLOOR: self._cmd_program_floor,
            CMD_UNINITIALIZED_RESPOND: self._cmd_uninitialized,
        }.get(cmd)
        if handler is None:
            return []
        return handler(pkt) or []

    def _reply(self, pkt: Packet, status: int, data: bytes = b"") -> list[Packet]:
        """应答帧：to=控制器、from=本机地址、packetId 回显。"""
        return [Packet(CONTROLLER, self.local_addr, pkt.packet_id, bytes([status]) + bytes(data))]

    def _reply_uuid(self, pkt: Packet, status: int) -> list[Packet]:
        return self._reply(pkt, status, self.uuid)

    # ---------------- 命令实现 ----------------
    def _cmd_get_id(self, pkt: Packet) -> list[Packet]:
        return self._reply_uuid(pkt, STATUS_OK)

    def _cmd_initialize(self, pkt: Packet) -> list[Packet]:
        if len(pkt.payload) < 1 + UUID_LEN:
            return self._reply_uuid(pkt, STATUS_WRONG_FEEDER_ID)
        if pkt.payload[1:1 + UUID_LEN] != self.uuid:
            return self._reply_uuid(pkt, STATUS_WRONG_FEEDER_ID)
        self.initialized = True
        self.led_on = True
        return self._reply_uuid(pkt, STATUS_OK)

    def _cmd_version(self, pkt: Packet) -> list[Packet]:
        return self._reply(pkt, STATUS_OK, bytes([PHOTON_PROTOCOL_VERSION]))

    def _cmd_move(self, pkt: Packet) -> list[Packet]:
        if len(pkt.payload) < 2:
            return []
        if not self.initialized:
            return self._reply_uuid(pkt, STATUS_UNINITIALIZED_FEEDER)
        forward = pkt.payload[0] == CMD_MOVE_FEED_FORWARD
        tenths = pkt.payload[1]
        eta = calc_eta(tenths, forward)
        # 先应答（大端 ETA），再执行"运动"——模拟器里同步完成
        out = self._reply(pkt, STATUS_OK, bytes([eta >> 8, eta & 0xFF]))
        self._simulate_motion(tenths, forward)
        return out

    def _simulate_motion(self, tenths: int, forward: bool) -> None:
        self.motion_log.append((tenths, forward))
        # 运动期间不响应任何帧（固件为阻塞调用），这里用 runtime 累加近似
        self.runtime_ms += calc_eta(tenths, forward)
        if tenths == 0:
            self.last_feed_status = STATUS_COULDNT_REACH
            return
        self.last_feed_status = STATUS_OK
        self.params.feed_count += 1
        self.led_on = False

    def _cmd_status(self, pkt: Packet) -> list[Packet]:
        return self._reply(pkt, self.last_feed_status)

    def _cmd_vendor(self, pkt: Packet) -> list[Packet]:
        if not self.initialized:
            return self._reply_uuid(pkt, STATUS_UNINITIALIZED_FEEDER)
        if len(pkt.payload) < 1 + VENDOR_RESP_LEN:
            return []
        req = pkt.payload[1:]
        rsp = [0x00] * VENDOR_RESP_LEN
        rsp[0] = req[0]
        sub = req[0]
        if sub == 0x01:
            rsp[1] = 0x01 if self.tape_present else 0x00
            rsp[2] = 0xFF
        elif sub == 0x02:
            rsp[1] = self.params.stall_count & 0xFF
            rsp[2] = (self.params.stall_count >> 8) & 0xFF
            rsp[3] = self.params.feed_count & 0xFF
            rsp[4] = (self.params.feed_count >> 8) & 0xFF
            rsp[5] = (self.params.feed_count >> 16) & 0xFF
            rsp[6] = (self.params.feed_count >> 24) & 0xFF
        elif sub == 0x03:
            v = (req[1] << 8) | req[2]
            if v == 0:
                rsp[1] = self.params.ticks_per_mm >> 8
                rsp[2] = self.params.ticks_per_mm & 0xFF
            elif v < 50 or v > 2000:
                rsp[1] = 0xFF
            else:
                self.params.ticks_per_mm = v
                rsp[1] = v >> 8
                rsp[2] = v & 0xFF
        elif sub == 0x04:
            rsp[1] = PHOTON_PROTOCOL_VERSION
            rsp[2] = self.last_feed_status
            rsp[3] = 0x01 if self.peel_blocked else 0x00
            rsp[4] = 0x01 if self.tape_present else 0x00
            secs = self.runtime_ms // 1000
            rsp[5] = (secs >> 8) & 0xFF
            rsp[6] = secs & 0xFF
            rsp[7] = self.params.flags
        elif sub == 0x05:
            if req[1] & 0x01:
                self.params.flags |= 0x01
            else:
                self.params.flags &= 0xFE
            rsp[1] = self.params.flags
        else:
            rsp[0] = 0xFF
        return self._reply(pkt, STATUS_OK, bytes(rsp))

    def _cmd_get_address(self, pkt: Packet) -> list[Packet]:
        if pkt.to != BROADCAST:
            return []
        if pkt.payload[1:1 + UUID_LEN] != self.uuid:
            return []
        return self._reply(pkt, STATUS_OK)

    def _cmd_identify(self, pkt: Packet) -> list[Packet]:
        if pkt.to != BROADCAST:
            return []
        if pkt.payload[1:1 + UUID_LEN] != self.uuid:
            return []
        self.identify_events += 1
        return self._reply(pkt, STATUS_OK)

    def _cmd_program_floor(self, pkt: Packet) -> list[Packet]:
        if pkt.to != BROADCAST:
            return []
        if len(pkt.payload) < 1 + UUID_LEN + 1:
            return []
        if pkt.payload[1:1 + UUID_LEN] != self.uuid:
            return []
        new_addr = pkt.payload[1 + UUID_LEN]
        if new_addr in (0x00, 0xFF):
            return self._reply(pkt, STATUS_FAIL)
        self.params.address = new_addr
        if not self.storage_ok:
            # 与固件一致：存储失败不改运行地址，回 FAIL
            return self._reply(pkt, STATUS_FAIL)
        self.local_addr = new_addr
        return self._reply(pkt, STATUS_OK)

    def _cmd_uninitialized(self, pkt: Packet) -> list[Packet]:
        if pkt.to != BROADCAST or self.initialized:
            return []
        self.backoff_ms = (self.uuid[0] ^ self.uuid[9]) & (PHOTON_C3_BACKOFF_MAX_MS - 1)
        return self._reply_uuid(pkt, STATUS_OK)

    backoff_ms = 0


class Bus:
    """一条半双工 RS485 总线：广播帧发给所有飞达，单播帧只有地址匹配者应答。"""

    def __init__(self, feeders: list[FeederSim] | None = None):
        self.feeders = feeders or []

    def add(self, feeder: FeederSim) -> FeederSim:
        self.feeders.append(feeder)
        return feeder

    def send(self, pkt: Packet) -> list[tuple[FeederSim, Packet]]:
        raw = pkt.encode()
        out: list[tuple[FeederSim, Packet]] = []
        for f in self.feeders:
            for r in f.receive(raw):
                out.append((f, r))
        return out

    def send_raw(self, raw: bytes) -> list[tuple[FeederSim, Packet]]:
        out: list[tuple[FeederSim, Packet]] = []
        for f in self.feeders:
            for r in f.receive(raw):
                out.append((f, r))
        return out
