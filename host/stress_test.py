"""stress_test.py - 总线压力测试（验收测试 T3）

用法：
    python host/stress_test.py --port COM7 --addr 0x05 --count 100000
    python host/stress_test.py --sim --count 20000

判定：所有应答都必须 status=0x00 且 packetId 回显正确；统计错帧率。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import photon_protocol as pp
from feeder_cli import SerialTransport, SimTransport


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port")
    g.add_argument("--sim", action="store_true")
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0x05)
    ap.add_argument("--count", type=int, default=100000)
    ap.add_argument("--timeout", type=float, default=0.02)
    args = ap.parse_args()

    t = SimTransport(address=args.addr) if args.sim else SerialTransport(args.port)
    if args.sim:
        t.feeder.initialized = True

    bad = 0
    t0 = time.time()
    try:
        for i in range(args.count):
            pid = i & 0xFF
            r = t.transact(pp.move_feed_status(args.addr, pid), timeout=args.timeout)
            if len(r) != 1 or r[0].payload[0] != pp.STATUS_OK or r[0].packet_id != pid:
                bad += 1
            if (i + 1) % 10000 == 0:
                print(f"  {i + 1}/{args.count}  bad={bad}")
    finally:
        t.close()

    dt = time.time() - t0
    rate = bad / args.count * 100
    print(f"完成：{args.count} 帧，错帧 {bad}（{rate:.4f}%），用时 {dt:.1f}s，"
          f"{args.count / dt:.0f} 帧/秒")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
