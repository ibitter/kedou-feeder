# 青蛙贴片机 RS485 智能飞达（硬件开源）

面向 [青蛙贴片机 / LumenPnP](../../) 的 8mm 编带智能飞达：RS485 总线、兼容 Photon 协议、OpenPnP 原生驱动，支持 SG90 / MG90S 舵机送料。

> **本仓库只包含硬件设计与文档。**
> 固件源码不公开，仅提供编译好的 `feeder.hex`（见 [Releases](../../releases)）。

---

## 特性

| 项目 | 说明 |
|---|---|
| 总线 | RS485 半双工，57600 8N1，与主板一线直连（A↔A、B↔B） |
| 协议 | Photon / M485 兼容，OpenPnP `PhotonFeeder` 插件直接可用 |
| 主控 | STC8H8K64U-45I-SOP16，40MHz 内部 IRC，免晶振 |
| 收发器 | MAX3078E（3.3V，真失效保护，无需偏置电阻） |
| 驱动 | SG90 / MG90S 舵机，50Hz PWM，2mm 基本步 |
| 撕带 | 机械单向棘轮，回程撕 1 齿 = 4mm 盖带 |
| 电源 | 5V 单电源（槽位取电），板载 AMS1117-3.3 给 MCU |
| 结构 | 3D 打印件，偏心轮零位三点一线 |

## 目录

```
hardware/
  蝌蚪RS485飞达控制板.epro2                     立创EDA 工程（原理图 + PCB）
  BOM_蝌蚪RS485飞达控制板_电路板单板.xlsx        物料清单
  蝌蚪飞达3D打印件.3mf                          结构件
docs/
  01-OpenPnP集成.md       接入与编址流程
  02-烧录与调试.md        ISP 烧录、IRC 校准、故障树
  03-装配与验收测试.md    打样物料、焊接装配、验收项
  04-协议与VENDOR命令.md  协议帧格式与私有命令
host/
  feeder_cli.py           RS485 CLI 工具（scan/init/feed/diag）
  photon_protocol.py      PhotonBus 协议实现
  feeder_sim.py           固件行为模拟器（离线调协议）
  stress_test.py          总线压力测试
  tests/test_protocol.py 协议单元测试
LICENSE                   CERN-OHL-P v2（硬件）
docs/LICENSE              CC BY-SA 4.0（文档）
```

> 注：`host/` 仅为与固件通信的 Python 工具，**不含固件源码**。`feeder_sim.py` 是协议层模拟器，不能直接烧录。

## 固件

固件源码不公开，编译产物随 [Releases](../../releases) 发布：

| 文件 | 用途 |
|---|---|
| `feeder.hex` | 直接用于 STC-ISP / stcgal 烧录 |
| `SHA256SUMS.txt` | 校验值 |

烧录前请务必按 [docs/02-烧录与调试.md](docs/02-烧录与调试.md) 执行：

1. 目标板 **3.3V 供电**；
2. STC-ISP 勾选 **下载时自动校准 IRC**，频率 40MHz；
3. 烧录后先确认串口能返回版本号，再上总线。

> 固件仅授权用于本硬件，**禁止反向工程、修改与二次分发**。详见下方许可。

## 快速上手

1. 按 [docs/03-装配与验收测试.md](docs/03-装配与验收测试.md) 焊接、装舵机、装结构件；
   - 舵机装好后先上电按两下送料键，让舵机回 0°，再把偏心轮按"三点一线"装上（§2.1）。
2. 烧录 `feeder.hex`。
3. 插入青蛙贴片机槽位 → OpenPnP 用 Program Feeder Slots 编址 → 设 Part Pitch（0402 填 2.0mm）。
4. 详见 [docs/01-OpenPnP集成.md](docs/01-OpenPnP集成.md)。

## 已知限制

- 开环驱动，无堵转/卡带检测；`0x06` 仅能反映 `tenths=0` 的无效请求。
- 机构单向，`0x05` 只做舵机回零，不实际退带。
- 首版未加 TVS 与保险丝，工业环境建议自行加固。
- 舵机寿命按 2 万次送料评估，请定期检查棘轮磨损。

## 许可

| 对象 | 许可 |
|---|---|
| 硬件设计（原理图、PCB、BOM、3D 件） | **CERN-OHL-P v2**，见 [LICENSE](LICENSE) |
| 文档 | **CC BY-SA 4.0**，见 [docs/LICENSE](docs/LICENSE) |
| 固件 `feeder.hex` | 保留所有权利。仅授权用于本飞达硬件，禁止反向工程、修改与二次分发 |

硬件设计基于 CERN-OHL-P v2 分发，允许制造、修改与商业使用；修改后的硬件设计请保留原始版权声明。

## 致谢

- 协议与生态：[Photon](https://github.com/photonfirmware/photon)（MPL-2.0）、[OpenPnP](https://openpnp.org)
- 结构参考：[opulo-inc/feeder](https://github.com/opulo-inc/feeder)
