"""feeder_cli.py - 飞达命令行工具（真机 / 模拟器双模式）

示例：
    # 没有硬件，先用模拟器熟悉命令
    python host/feeder_cli.py --sim scan
    python host/feeder_cli.py --sim --addr 0x05 init
    python host/feeder_cli.py --sim --addr 0x05 feed --mm 4
    python host/feeder_cli.py --sim --addr 0x05 status

    # 真机：USB-RS485 适配器接到飞达总线上（注意 A/B 极性与终端电阻）
    python host/feeder_cli.py --port COM7 scan
    python host/feeder_cli.py --port COM7 --addr 0x05 feed --mm 4 --wait

真机模式依赖 pyserial：pip install pyserial
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import photon_protocol as pp

DEFAULT_TIMEOUT = 0.12


# ---------------------------------------------------------------- 传输层
class SimTransport:
    """进程内模拟器：一台飞达挂在"总线"上。"""

    def __init__(self, uuid: bytes | None = None, address: int = 0x00):
        from feeder_sim import FeederParams, FeederSim
        params = FeederParams(address=address)
        self.feeder = FeederSim(uuid=uuid, params=params)

    def transact(self, pkt: pp.Packet, timeout: float = DEFAULT_TIMEOUT) -> list[pp.Packet]:
        if timeout < 0.008:
            time.sleep(timeout)
        return self.feeder.handle(pkt)

    def close(self) -> None:
        pass


class SerialTransport:
    """真实 RS485 总线（半双工，依赖 USB-RS485 适配器的自动收发）。"""

    def __init__(self, port: str, baud: int = 57600):
        try:
            import serial                                       # noqa: F401
        except ImportError as exc:                                # pragma: no cover
            raise SystemExit("缺少 pyserial：pip install pyserial") from exc
        import serial
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1,
                                 timeout=0.002, write_timeout=0.2)

    def _read_frame(self, deadline: float) -> pp.Packet | None:
        buf = bytearray()
        while time.time() < deadline:
            b = self.ser.read(1)
            if not b:
                continue
            buf.append(b[0])
            if len(buf) == 5:
                need = 5 + buf[3]
            elif len(buf) > 5 and len(buf) == 5 + buf[3]:
                pkt = pp.Packet.decode(bytes(buf))
                if pkt:
                    return pkt
                buf = buf[1:]                     # CRC 错，滑动重同步
        return None

    def transact(self, pkt: pp.Packet, timeout: float = DEFAULT_TIMEOUT) -> list[pp.Packet]:
        self.ser.reset_input_buffer()
        self.ser.write(pkt.encode())
        out = []
        deadline = time.time() + timeout
        while True:
            r = self._read_frame(deadline)
            if r is None:
                break
            if r.frm == pp.CONTROLLER or r.packet_id != pkt.packet_id:
                continue
            out.append(r)
        return out

    def close(self) -> None:
        self.ser.close()


# ---------------------------------------------------------------- 命令实现
def _uuid_text(u: bytes) -> str:
    return "-".join(f"{b:02X}" for b in u)


def _print_response(rsp: list[pp.Packet], show_payload: bool = True) -> bool:
    if not rsp:
        print("  <无应答>")
        return False
    for p in rsp:
        status = pp.STATUS_OK if p.payload[0] == 0 else p.payload[0]
        extra = ""
        if p.payload[0] == 0 and len(p.payload) >= 13:
            extra = f" uuid={_uuid_text(p.payload[1:13])}"
        print(f"  from=0x{p.frm:02X} pid=0x{p.packet_id:02X} status=0x{p.status:02X} "
              f"payload={p.payload.hex().upper()}{extra}")
        del status
    return all(p.payload[0] == pp.STATUS_OK for p in rsp)


def cmd_scan(t: SimTransport | SerialTransport, args) -> int:
    """0xC3 广播扫描未编址飞达；--program 时顺带按顺序编址。"""
    print(f"扫描 {args.seconds:.1f}s（发 0xC3 广播）...")
    seen: dict[bytes, int] = {}
    deadline = time.time() + args.seconds
    pid = 0x40
    while time.time() < deadline:
        for p in t.transact(pp.uninitialized_feeders_respond(pid), timeout=DEFAULT_TIMEOUT):
            if len(p.payload) >= 13:
                seen.setdefault(p.payload[1:13], p.frm)
        pid = (pid + 1) & 0xFF

    if not seen:
        print("未发现未编址飞达（可能已编址，或 A/B 接反 / 无终端电阻）")
    for i, (uuid, frm) in enumerate(seen.items()):
        print(f"  第 {i + 1} 台 uuid={_uuid_text(uuid)} from=0x{frm:02X}")

    if seen and args.program is not None:
        for i, uuid in enumerate(seen):
            slot = args.program + i
            r = t.transact(pp.program_feeder_floor(uuid, slot, 0x50 + i))
            ok = _print_response(r)
            print(f"  {'编址成功' if ok else '编址失败'} uuid={_uuid_text(uuid)} → 槽位 0x{slot:02X}")
    return 0 if seen else 1


def cmd_init(t, args) -> int:
    """先 0x01 取 UUID，再 0x02 绑定（每次上电都要做）。"""
    r = t.transact(pp.get_feeder_id(args.addr, 0x60))
    if not r or len(r[0].payload) < 13:
        print("读 UUID 失败")
        return 1
    uuid = r[0].payload[1:13]
    print(f"uuid={_uuid_text(uuid)}")
    r = t.transact(pp.initialize_feeder(args.addr, 0x61, uuid))
    _print_response(r)
    return 0 if r and r[0].payload[0] == pp.STATUS_OK else 1


def _mm_to_tenths(mm: float) -> int:
    tenths = int(round(mm * 10))
    if abs(tenths - mm * 10) > 0.5:
        raise SystemExit("步距必须是 0.1mm 的整数倍")
    return tenths & 0xFF


def cmd_feed(t, args, forward: bool) -> int:
    tenths = _mm_to_tenths(args.mm)
    builder = pp.move_feed_forward if forward else pp.move_feed_backward
    r = t.transact(builder(args.addr, 0x70, tenths))
    if not r:
        print("无应答（未初始化？地址错？）")
        return 1
    eta = (r[0].payload[1] << 8 | r[0].payload[2]) if len(r[0].payload) >= 3 else 0
    print(f"status=0x{r[0].payload[0]:02X} eta={eta}ms")
    if not args.wait or r[0].payload[0] != pp.STATUS_OK:
        return 0

    # 与 OpenPnP 一致：运动期间不响应，等 ETA 后按 50ms 轮询 0x06
    time.sleep(eta / 1000.0)
    deadline = time.time() + 3 * eta / 1000.0
    pid = 0x71
    while time.time() < deadline:
        r = t.transact(pp.move_feed_status(args.addr, pid), timeout=0.05)
        pid = (pid + 1) & 0xFF
        if r:
            st = r[0].payload[0]
            print(f"最终结果 status=0x{st:02X} ({'OK' if st == 0 else 'COULDNT_REACH'})")
            return 0 if st == pp.STATUS_OK else 2
        time.sleep(0.05)
    print("轮询超时")
    return 1


def cmd_status(t, args) -> int:
    r = t.transact(pp.move_feed_status(args.addr, 0x72))
    if not r:
        return 1
    st = r[0].payload[0]
    print(f"最近送料结果 status=0x{st:02X} ({'OK' if st == 0 else 'COULDNT_REACH'})")
    return 0


def cmd_identify(t, args) -> int:
    uuid = bytes.fromhex(args.uuid.replace("-", ""))
    r = t.transact(pp.identify_feeder(uuid, 0x73), timeout=0.3)
    _print_response(r)
    return 0 if r else 1


def cmd_version(t, args) -> int:
    r = t.transact(pp.get_version(args.addr, 0x74))
    if r and len(r[0].payload) >= 2:
        print(f"协议版本={r[0].payload[1]}")
        return 0
    print("无应答")
    return 1


def cmd_diag(t, args) -> int:
    r = t.transact(pp.vendor_options(args.addr, 0x75, bytes([0x04])))
    if not r or len(r[0].payload) < 1 + 20:
        print("无应答")
        return 1
    b = r[0].payload[1:]
    print(f"  协议版本   : {b[1]}")
    print(f"  最近送料   : 0x{b[2]:02X}")
    print(f"  剥膜光电   : {'遮挡' if b[3] else '透光'}")
    print(f"  料带在位   : {'是' if b[4] else '否'}")
    print(f"  运行秒数   : {(b[5] << 8) | b[6]}")
    print(f"  功能 flags : 0x{b[7]:02X}  (bit0 剥膜自检)")
    return 0


def cmd_cal(t, args) -> int:
    extra = bytes([args.ticks >> 8, args.ticks & 0xFF]) if args.ticks is not None else b""
    r = t.transact(pp.vendor_options(args.addr, 0x76, bytes([0x03]) + extra))
    if not r or len(r[0].payload) < 1 + 20:
        print("无应答")
        return 1
    b = r[0].payload[1:]
    if b[1] == 0xFF:
        print("参数越界（允许 50..2000）")
        return 2
    print(f"ticks_per_mm = {(b[1] << 8) | b[2]}")
    return 0


def cmd_flags(t, args) -> int:
    val = 0x01 if args.peel_check else 0x00
    r = t.transact(pp.vendor_options(args.addr, 0x77, bytes([0x05, val])))
    if not r or len(r[0].payload) < 1 + 20:
        print("无应答")
        return 1
    print(f"flags = 0x{r[0].payload[2]:02X}")
    return 0


def cmd_address(t, args) -> int:
    uuid = bytes.fromhex(args.uuid.replace("-", ""))
    r = t.transact(pp.get_feeder_address(uuid, 0x78), timeout=0.3)
    if r:
        print(f"槽位地址 = 0x{r[0].frm:02X}")
        return 0
    print("无应答")
    return 1


def cmd_program(t, args) -> int:
    uuid = bytes.fromhex(args.uuid.replace("-", ""))
    r = t.transact(pp.program_feeder_floor(uuid, args.slot, 0x79), timeout=0.3)
    _print_response(r)
    return 0 if r and r[0].payload[0] == 0 else 1


def cmd_raw(t, args) -> int:
    raw = bytes.fromhex(args.hex.replace(" ", ""))
    pkt = pp.Packet.decode(raw)
    if pkt is None:
        print("帧格式/CRC 无效")
        return 2
    _print_response(t.transact(pkt))
    return 0


# ---------------------------------------------------------------- 入口
def cmd_demo(t, args) -> int:
    """一条龙：扫描 → 编址 → 初始化 → 送料 → 查询结果（M3 联调用）。"""
    print("--- 1) 扫描未编址飞达 (0xC3) ---")
    uuid = None
    deadline = time.time() + 0.5
    pid = 0x01
    while time.time() < deadline and uuid is None:
        for p in t.transact(pp.uninitialized_feeders_respond(pid)):
            if len(p.payload) >= 13:
                uuid = p.payload[1:13]
                break
        pid = (pid + 1) & 0xFF
    if uuid is None:
        print("未发现未编址飞达")
        return 1
    print(f"uuid={_uuid_text(uuid)}")

    slot = args.slot if args.slot is not None else 0x05
    print(f"--- 2) 编址到槽位 0x{slot:02X} (0xC2) ---")
    if not _print_response(t.transact(pp.program_feeder_floor(uuid, slot, 0x02))):
        return 2

    print("--- 3) 初始化 (0x01 + 0x02) ---")
    r = t.transact(pp.get_feeder_id(slot, 0x03))
    if not r or len(r[0].payload) < 13:
        print("读 UUID 失败")
        return 3
    if not _print_response(t.transact(pp.initialize_feeder(slot, 0x04, r[0].payload[1:13]))):
        return 3

    print(f"--- 4) 送料 {args.mm}mm (0x04) ---")
    args.addr = slot
    args.wait = True
    return cmd_feed(t, args, True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="LumenPnP RS485 飞达工具")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="串口，如 COM7（USB-RS485 适配器）")
    g.add_argument("--sim", action="store_true", help="使用进程内模拟器")
    p.add_argument("--addr", type=lambda s: int(s, 0), default=pp.BROADCAST,
                   help="飞达槽位地址（默认广播 0xFF）")
    p.add_argument("--slot", type=lambda s: int(s, 0), default=None,
                   help="program 子命令要写入的槽位地址")
    p.add_argument("--uuid", help="12 字节 UUID，可带 - 分隔")
    p.add_argument("--sim-address", type=lambda s: int(s, 0), default=0x00,
                   help="模拟器上电时已存的地址（0=未编址）")

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="广播扫描未编址飞达")
    s.add_argument("--seconds", type=float, default=2.0)
    s.add_argument("--program", type=lambda x: int(x, 0), default=None,
                   help="从该槽位号开始依次编址")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("init", help="0x01+0x02：读取 UUID 并初始化")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("feed", help="向前送料")
    s.add_argument("--mm", type=float, required=True, help="送料距离，如 4 或 2")
    s.add_argument("--wait", action="store_true", help="等待并轮询 0x06")
    s.set_defaults(func=lambda t, a: cmd_feed(t, a, True))

    s = sub.add_parser("back", help="反向退带")
    s.add_argument("--mm", type=float, required=True)
    s.add_argument("--wait", action="store_true")
    s.set_defaults(func=lambda t, a: cmd_feed(t, a, False))

    s = sub.add_parser("status", help="查询最近送料结果")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("identify", help="按 UUID 闪灯定位")
    s.set_defaults(func=cmd_identify)

    s = sub.add_parser("version", help="读协议版本")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("diag", help="VENDOR 0x04 诊断")
    s.set_defaults(func=cmd_diag)

    s = sub.add_parser("cal", help="VENDOR 0x03 编码器标定")
    s.add_argument("--ticks", type=int, default=None, help="写入 ticks/mm（省略为只读）")
    s.set_defaults(func=cmd_cal)

    s = sub.add_parser("flags", help="VENDOR 0x05 剥膜自检开关")
    s.add_argument("--peel-check", action=argparse.BooleanOptionalAction, default=True)
    s.set_defaults(func=cmd_flags)

    s = sub.add_parser("address", help="0xC0 按 UUID 查询槽位地址")
    s.set_defaults(func=cmd_address)

    s = sub.add_parser("program", help="0xC2 按 UUID 写入槽位地址")
    s.set_defaults(func=cmd_program)

    s = sub.add_parser("raw", help="发送任意十六进制帧")
    s.add_argument("hex", help="如 2B1347010A03")
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("demo", help="一条龙联调：扫描→编址→初始化→送料")
    s.add_argument("--mm", type=float, default=4.0)
    s.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in ("identify", "address", "program") and not args.uuid:
        raise SystemExit("该命令需要 --uuid")
    if args.cmd == "program" and args.slot is None:
        raise SystemExit("program 需要 --slot")

    t = SimTransport(address=args.sim_address) if args.sim else SerialTransport(args.port)
    try:
        return args.func(t, args)
    finally:
        t.close()


if __name__ == "__main__":
    sys.exit(main())
