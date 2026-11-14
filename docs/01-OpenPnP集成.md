# 01 · OpenPnP 集成指南

> 目标：把自研飞达插进 LumenPnP 槽位，用 OpenPnP 原生的 PhotonFeeder 完成扫描、编址、初始化、送料，
> 上位机与主板固件**零改动**。

## 1. 版本与前提

| 项 | 要求 |
|---|---|
| OpenPnP | 2.0 及以上（内置 `org.openpnp.machine.photon`） |
| LumenPnP 主板固件 | 支持 `M485` G-code 桥接（官方 2.x 固件已带；老固件需先升级） |
| 飞达固件 | 本仓库 `firmware/build/feeder.hex` |
| 总线 | 57600 8N1，飞达地址 0x01–0xFE，未编址时为 0xFF |

## 2. 数据通路（理解这条链路，排错就快）

```
OpenPnP PhotonFeeder
   │  通过 Actuator「PhotonFeederData」发 ACTUATOR_READ
   ▼
GcodeDriver:  M485 {Value}            ← 这段是 PhotonBus 编好的 485 报文
   │  串口/USB
   ▼
LumenPnP 主板固件：解析 M485 → 把报文送上 RS485 总线 → 收应答
   │  应答以 "rs485-reply: ..." 形式回给 OpenPnP（正则 (?<Value>.*)）
   ▼
自研飞达（本固件）
```

要点：

- OpenPnP 在配置加载时若找不到名为 **`PhotonFeederData`** 的 actuator，会自动创建，
  并把 READ 命令设为 `M485 {Value}`、READ 正则设为 `rs485-reply: (?<Value>.*)`。
  你**不需要**手工加 actuator；如果机器里已有一个同名的，OpenPnP 直接复用。
- 因此飞达能否被驱动，只取决于：主板 `M485` 是否可用 + 飞达协议是否正确。

## 3. 首次接入步骤

### 3.1 机器使能

OpenPnP 左上角绿色电源按钮 → 连接机器。未使能时 Photon 的搜索/编址向导会提示
"Please connect to the machine before running this wizard."

### 3.2 扫描已编址飞达（Search）

1. 打开任意 PhotonFeeder 的属性面板 → **Global Config** 页；
2. 设置 **Maximum Feeder Address To Scan**（默认会扫描 1..N，按你的槽位数设，最大 254）；
3. 点 **Search**，进度条会逐地址显示 FOUND / MISSING。

> ⚠ 关键事实：OpenPnP 的 Search 是**逐地址发 `GET_FEEDER_ID(0x01)`**，不是广播 0xC3。
> 所以它只能发现**已经编址**的飞达；刚焊好、地址还是 0xFF 的新飞达不会出现。
> 新飞达必须先走 3.3 编址。

### 3.3 给新飞达编址（Program Feeder Slots）

1. Global Config 页 → **Program Feeder Slots** → **Start Wizard**；
2. 按向导提示，把新飞达插入目标槽位（此时飞达上电、地址 0xFF，只应答广播 `0xC3`）；
3. 向导会让飞达自报 UUID，并用 `PROGRAM_FEEDER_FLOOR(0xC2)` 把槽位号写成飞达地址；
4. 完成后飞达重启/重新上电都保留该地址（存在 IAP 数据区）。

> 本固件在 0xC3 应答前按 UUID 做了 0–7ms 错峰（官方固件没有），多台新飞达同时插入时
> 总线冲突概率更低；单台时对主机无影响（向导本身是重试循环）。

没有 OpenPnP 时，也可以用 CLI 完成同样的流程：

```powershell
python host\feeder_cli.py --port COM7 scan --program 0x05   # 扫描并编址到槽位 5
```

### 3.4 配置飞达参数（Feeders 页）

| 参数 | 说明 |
|---|---|
| Hardware ID | 12 字节 UUID（编址后自动填入，可用来区分同型号飞达） |
| Slot Address | 槽位号 = 协议地址（1..254），必须与物理槽位一一对应 |
| Part Pitch | 料距，默认 4mm（0402 填 2mm）；固件按 `pitch*10` 个 0.1mm 单位送料，且**向上取整到 2mm 整数倍** |
| Slot Location / Offset | 槽位坐标与飞达相对槽位的取料偏移；缺一项飞达会被判为未配置 |

每次 Job 开始前，OpenPnP 会重新发 `INITIALIZE_FEEDER(0x02)`（因为固件的 initialized 只是 RAM 态），
这是正常行为，不是重复编址。

### 3.5 试送料

在 Feeders 页选中飞达 → 右键 **Feed**（或工具栏送料按钮）。观察：

- 飞达状态灯不闪（Identify 只在显式 Identify 时闪 3 次，送料不闪）；
- 料带前进一个 partPitch；
- OpenPnP 控制台无 `FeedFailureException`。

## 4. OpenPnP 的送料时序（与固件对齐）

1. `MoveFeedForward(address, partPitch*10)` → 固件立即回 `[0x00][ETA_MSB][ETA_LSB]`；
2. OpenPnP 以 50ms 间隔轮询 `MOVE_FEED_STATUS(0x06)`，最长等 `3 × ETA`；
3. 固件在运动期间**不响应任何帧**（阻塞送料），所以前几次轮询会超时，属正常；
4. 运动结束后 0x06 返回 `0x00`（OK）或 `0x02`（COULDNT_REACH → 抛 FeedFailureException）；
5. 若 0x04 返回 `0x03`（UNINITIALIZED），OpenPnP 会清空地址并在重试中重新初始化。

> 因此固件的 ETA 必须 **≥ 实际运动时间**。本固件取 `8ms × 0.1mm + 500ms`（4mm = 820ms），
> 而实际 4mm 耗时约 840ms（180+60ms 两段推料 + 360ms 回程撕带），相差很小，够用；
> 若后续收紧舵机延时，ETA 宏要同步改小。

## 5. 多飞达与混插

- 每条总线地址必须唯一；同一槽位只能有一台飞达。
- 可与官方 Photon 飞达混插：双方都按地址应答，广播命令（0xC0/0xC1/0xC2/0xC3）按 UUID 过滤。
- 总线末端只保留主机/集线板侧一处 120Ω；**本版飞达板 BOM 里没有 120Ω 终端电阻**，不需要也不应该焊。
- 25 台以上混跑时建议把 Search 的 Maximum Feeder Address 设为实际最大槽位，减少扫描时间。

## 6. 故障树

| 现象 | 可能原因 | 处理 |
|---|---|---|
| Search 全部 MISSING | 飞达未编址（0xFF） | 先走 Program Feeder Slots 向导或 CLI scan |
| 全部 MISSING 且 CLI 也无应答 | A/B 接反、无终端、DE 一直为高、飞达没上电 | 交换 A/B；量 5V；`diag` 看是否上电 |
| 0x04 一直返回 0x03 | 未初始化（每次上电都要 0x02） | 正常现象；检查 OpenPnP 是否在 Job 前初始化 |
| 送料抛 COULDNT_REACH | 本固件仅在 tenths=0 时返回；其余情况是主机侧超时 | 查 partPitch 是否为 0；查总线与电源 |
| 送料超时（无应答） | 固件 ETA 太小 / 总线被别的飞达占用 / 地址冲突 | 增大 ETA；确认地址唯一；查总线终端 |
| Identify 不闪灯 | UUID 不匹配或灯坏 | `identify --uuid` 用扫描到的 UUID 再试 |
| 主板 M485 不工作 | 主板固件版本旧 | 升级 LumenPnP 固件；或用 USB-RS485 适配器直连总线验证飞达 |

## 7. 不依赖主板的验证路径

```powershell
# USB-RS485 适配器直接接飞达 A/B/GND（适配器自带收发切换）
python host\feeder_cli.py --port COM7 demo --mm 4
```

这条路径能在没有整机时验证：扫描→编址→初始化→送料→状态查询的完整链路，
也是本项目 M3 里程碑的验收方式。
