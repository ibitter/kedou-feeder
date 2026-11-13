# 04 · Photon 协议实现与 VENDOR 命令参考

> 本文以 OpenPnP `org.openpnp.machine.photon` 与 photonfirmware/photon 为兼容目标，
> 记录本飞达固件实际实现的字节级行为。主机侧对应实现见 `host/photon_protocol.py`，
> 参考模拟器见 `host/feeder_sim.py`。

## 1. 物理层

| 项 | 值 |
|---|---|
| 电气 | RS485 半双工（A/B），DE//RE 由 MCU **P3.5** 控制（P3.4 是按键），默认接收 |
| 串口 | 57600，8 数据位，无校验，1 停止位 |
| 帧缓冲 | 64 字节（与官方 `RS485_BUS_BUFFER_SIZE` 一致） |
| 字节间超时 | 3ms：帧内字节间隔超时视为残帧，丢弃并重新同步 |

## 2. 帧格式

```
偏移  字段            说明
0     toAddress      目的地址：0x00=控制器，0xFF=广播，0x01..0xFE=飞达槽位
1     fromAddress    源地址：主机发命令恒 0x00；飞达应答为自己的地址（未编址时为 0xFF）
2     packetId       事务号，0..255，飞达应答原样回显
3     payloadLen     载荷长度（0..21）
4     crc8           CRC-8 覆盖字节 0..3 与全部载荷
5..   payload        payload[0] = 命令 ID（请求）或 status（应答）
```

- CRC-8：多项式 0x07、初值 0x00、不反射、不异或输出。
- 校验样例（OpenPnP 单元测试向量）：`2B 13 47 01 0A 03` → `to=0x2B, from=0x13, pid=0x47, len=1, payload=[0x03]`。
- 本仓库 `host/photon_protocol.py` 的 `crc8(b"\x2B\x13\x47\x01\x03") == 0x0A` 已由测试锁定。

## 3. 地址与 UUID

- UUID = 12 字节。本固件：`STC8H 唯一 ID 7 字节 (0xFDE0..0xFDE6) + 产线序列号 3 字节 + 版本 2 字节 (0x01,0x00)`。
- 上电地址：IAP 存储里为 0x01..0xFE 则用之，否则 0xFF（未编址，只响应广播）。
- `initialized` 标志**只在 RAM**：每次上电都是未初始化，主机每次连接/每次 Job 前都会重新发 0x02。
- 未初始化时：0x04/0x05/0xBF 一律回 `0x03 UNINITIALIZED_FEEDER + UUID`。
- 0x01 `GET_FEEDER_ID` 任何时候都应答（即使未初始化）。

## 4. 命令表

| 命令 | ID | 方向 | 请求载荷 | 应答载荷 |
|---|---|---|---|---|
| GET_FEEDER_ID | 0x01 | 单播/广播 | `[0x01]` | `[status][uuid×12]` |
| INITIALIZE_FEEDER | 0x02 | 单播/广播 | `[0x02][uuid×12]` | `[status][uuid×12]` |
| GET_VERSION | 0x03 | 单播/广播 | `[0x03]` | `[status][version]`（version=1） |
| MOVE_FEED_FORWARD | 0x04 | 单播 | `[0x04][tenths_mm]` | `[status][ETA_MSB][ETA_LSB]` |
| MOVE_FEED_BACKWARD | 0x05 | 单播 | `[0x05][tenths_mm]` | 同 0x04 |
| MOVE_FEED_STATUS | 0x06 | 单播 | `[0x06]` | `[last_status]` |
| VENDOR_OPTIONS | 0xBF | 单播 | `[0xBF][20B options]` | `[status][20B response]` |
| GET_FEEDER_ADDRESS | 0xC0 | **广播** | `[0xC0][uuid×12]` | `[0x00]`，地址在帧头 `fromAddress` |
| IDENTIFY_FEEDER | 0xC1 | **广播** | `[0xC1][uuid×12]` | `[0x00]`，随后闪灯 3 次 |
| PROGRAM_FEEDER_FLOOR | 0xC2 | **广播** | `[0xC2][uuid×12][newAddr]` | `[0x00]` 或 `[0x05]` |
| UNINITIALIZED_FEEDERS_RESPOND | 0xC3 | **广播** | `[0xC3]` | `[0x00][uuid×12]`（仅未初始化飞达） |

### 4.1 送料距离单位（最容易踩的坑）

`MOVE_FEED_FORWARD/BACKWARD` 的载荷只有 **1 字节，单位 0.1mm**。
OpenPnP 的调用是 `new MoveFeedForward(slotAddress, distance_mm * 10)`，所以 4mm = 40 = `0x28`。
最大 25.5mm。

> 舵机固件按 **2mm 基本步**执行：`steps = ceil(tenths / 20)`，所以 1~20（0.1~2.0mm）都只走一步 2mm，
> 3mm 会走两步（4mm）。要精确对齐元件，partPitch 应设成 2mm 的整数倍。

### 4.2 ETA 与轮询

| 方向 | ETA 公式（本固件） | 示例 4mm |
|---|---|---|
| 前进 | `8 × tenths + 500` ms | 820ms |
| 后退 | `60 × tenths + 400` ms | 2800ms |

> 后退虽然 ETA 仍按上面公式算，但舵机只能回 0°（单向拨杆），实际耗时只有一次 `SERVO_MOVE_MS`。

ETA 是**大端 uint16**（`[MSB][LSB]`）。主机以 50ms 间隔轮询 0x06，最长等 `3 × ETA`。
固件先发应答、再阻塞执行运动；运动期间**不响应任何帧**，所以前几次轮询会超时，属预期。

### 4.3 0x06 的语义

只返回**最近一次送料结果**：`0x00 OK` / `0x02 COULDNT_REACH`。
官方固件同样没有 RUNNING/COMPLETE 枚举，不要按"状态机"去解析。
手动按键送料也会更新这个值。

> 舵机方案是开环：没有堵转/到位检测，正常情况下 **0x02 只会在 `tenths=0` 时返回**；
> 卡带、打滑这类机械问题不会自动上报失败。

### 4.4 0xC0 / 0xC2 / 0xC3 的细节

- **0xC0**：应答帧头 `fromAddress` = 飞达地址，载荷只有 status。未编址飞达的 from 为 0xFF。
- **0xC2**：先写 IAP，写成功才切换运行地址并回 0x00；写失败回 0x05 且保持原地址。
  拒绝 0x00 / 0xFF 作为新地址（协议保留）。
- **0xC3**：只由未初始化飞达应答。本固件在应答前按 `(uuid[0] ^ uuid[9]) & 7` 错峰 0–7ms，
  降低多台同时插入的冲突；官方固件无此退避。

### 4.5 OpenPnP 的 Search 与编址

- **Search** 是对地址 1..`maxFeederAddress` 逐个发 `GET_FEEDER_ID(0x01)`，**不广播**，
  因此只能发现已编址飞达。
- 新飞达编址走 **Program Feeder Slots** 向导（内部用 0xC3 找 UUID、0xC2 写地址），
  或本仓库 CLI 的 `scan --program`。

## 5. 状态码

| 值 | 名称 | 含义 |
|---|---|---|
| 0x00 | OK | 成功 |
| 0x01 | WRONG_FEEDER_ID | UUID 不匹配 |
| 0x02 | COULDNT_REACH | 送料失败（舵机方案仅 `tenths=0` 会触发） |
| 0x03 | UNINITIALIZED_FEEDER | 尚未 0x02 初始化 |
| 0x04 | FEEDING_IN_PROGRESS | 官方保留（本固件运动期间不应答） |
| 0x05 | FAIL | 通用失败（如 IAP 写入失败） |
| 0xFE | TIMEOUT | 主机侧超时（固件不主动发送） |
| 0xFF | UNKNOWN_ERROR | 未知 |

## 6. VENDOR_OPTIONS (0xBF) 私有扩展

请求载荷固定 21 字节：`[0xBF][20 字节 options]`（不足补 0）。
应答载荷固定 21 字节：`[status=0x00][20 字节 response]`。未初始化时回 `0x03 + UUID`。

| 子命令 | options[0] | 请求字段 | response 布局 |
|---|---|---|---|
| 读料位 | 0x01 | — | `[1]=料带在位` `[2]=剩余百分比（0xFF=未知）` |
| 读计数 | 0x02 | — | `[1..2]=堵转计数` `[3..6]=累计送料次数（小端 32 位）` |
| 标定 ticks/mm | 0x03 | `[1..2]=新值（大端，0=只读）` | `[1..2]=当前值大端`；越界（<50 或 >2000）时 `[1]=0xFF` |
| 诊断 | 0x04 | — | `[1]=协议版本` `[2]=最近结果` `[3]=剥膜光电` `[4]=料带在位` `[5..6]=运行秒数` `[7]=flags` |
| 功能开关 | 0x05 | `[1] bit0=剥膜边沿自检` | `[1]=当前 flags` |
| 未知子命令 | 其它 | — | `[0]=0xFF` |

> **舵机方案下这些字段只是兼容保留**：`[1]=料带在位` 恒为 1，诊断的 `[3]=剥膜光电` 恒为 0，
> `[4]` 恒为 1；0x03 `ticks_per_mm`、0x05 flags 可读可写并持久化，但不影响送料行为
> （撕带由机械棘轮保证）。保留它们是为了不改 host 工具与协议布局。

> `response[0]` 恒为子命令号（未知时 0xFF）。所有未用字节为 0。

## 7. 典型交互（扫描→编址→送料）

```
主机 → 0xFF  0xC3                                  (广播，找未编址飞达)
飞达 → 0x00  0x00 + uuid×12                         (from=0xFF)
主机 → 0xFF  0xC2 + uuid×12 + 0x05                  (编址到槽位 5)
飞达 → 0x00  0x00                                   (from=0x05)
主机 → 0x05  0x02 + uuid×12                         (初始化)
飞达 → 0x00  0x00 + uuid×12
主机 → 0x05  0x04 + 0x28                            (前进 4.0mm)
飞达 → 0x00  0x00 + 0x03 0x34                       (820ms，大端)
   … 运动约 0.84s，期间不应答 …
主机 → 0x05  0x06                                   (查询结果)
飞达 → 0x00  0x00                                   (OK)
```

## 8. 主机侧代码示例

```python
import sys
sys.path.insert(0, "host")
import photon_protocol as pp
from feeder_cli import SerialTransport

bus = SerialTransport("COM7")
r = bus.transact(pp.get_version(0x05, 0x10))
print(hex(r[0].payload[0]), r[0].payload[1])       # 0x0 1
```

## 9. 兼容性备注

| 项 | 本固件 | 官方 Photon |
|---|---|---|
| 0x04 载荷单位 | 0.1mm（1 字节） | 同 |
| ETA | 保守估计（见 §4.2） | `72×tenths+1200` / `80×tenths+1400`（更保守） |
| 0x06 | 只回最近结果 | 同 |
| 运动期间 | 不应答 | 同（单线程） |
| 0xC3 退避 | 0–7ms UUID 错峰 | 无 |
| 0xC3 应答条件 | 仅未初始化 | 同 |
| 0xC2 地址校验 | 拒绝 0x00/0xFF | 未校验 |
| initialized 掉电 | 清零 | 同 |

如需与官方飞达完全一致的 ETA，把 `firmware/include/config.h` 的
`FEED_MS_PER_TENTH_MM`/`FEED_MS_FIXED` 改为 72/1200、后退改为 80/1400 即可（不影响协议结构）。
