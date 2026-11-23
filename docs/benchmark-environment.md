# 性能测试环境与测量口径

本文件适用于 [Python 热路径优化](performance.md) 和 [Rust 与调度优化](native-performance.md) 两轮报告。测试及环境补录日期均为 2026-11-23。Python、Windows build、MongoDB 版本、负载及各轮样本来自原始结果 JSON；机器配置与 PyMongo 版本在同一台测试机器上补录核实，原始 JSON 未采集这些字段，原始性能样本保持不变。

## 测试机器

| 项目 | 配置 | 依据 |
| --- | --- | --- |
| 整机 | HP Elite Tower 880 G9 Desktop PC | Windows CIM `Win32_ComputerSystem` |
| CPU | Intel Core i7-14700，20 个物理核、28 个逻辑处理器 | `Win32_Processor`；脚本未固定 CPU 亲和性或频率 |
| 内存 | 32 GiB，2 × 16 GiB DDR5；系统报告总物理内存约 31.68 GiB | `Win32_PhysicalMemory` / `Win32_ComputerSystem` |
| 内存配置速率 | 固件报告 4400 MT/s，内存条标称速率 5600 MT/s | `ConfiguredClockSpeed` / `Speed`；同机补录值 |
| 存储 | SK hynix PC801 HFS001TEJ9X101N，1 TB NVMe SSD（约 953.86 GiB） | `Win32_DiskDrive` 及 C: 分区对应的 `Get-Disk` |
| 数据位置 | 临时 MongoDB 数据目录位于项目所在的 C: NVMe SSD | 隔离服务启动脚本的 `--dbpath`；客户端和数据库在同一台机器 |
| 系统 | Windows 11 专业版，x64，build 26300（10.0.26300） | 原始 JSON 的 platform 字段及 `Win32_OperatingSystem` |
| 电源方案 | 补录时为 Windows「平衡」 | `powercfg /getactivescheme`；未记录各轮测试时的方案或实际频率 |

未采集逐轮 CPU 温度、频率、后台进程负载、磁盘缓存命中或整个进程 RSS；这些状态不能由机器规格推断。独立 mongod 保证比较使用同一数据库服务，不表示整台电脑没有其他活动。

## 软件与数据库

| 项目 | 版本或设置 |
| --- | --- |
| Python | CPython 3.13.15，64 位 x86-64，标准启用 GIL 的解释器 |
| PyMongo | 4.20.1，同机项目环境补录值 |
| MongoDB | 8.0.4，独立临时 mongod，绑定 `127.0.0.1`，客户端经本机 TCP 连接 |
| 数据库写确认 | `w=1`；默认 journaling，未显式要求每笔写入 `j=True` 或 `fsync=True` |
| 数据库配置 | 使用默认存储引擎和缓存配置；未设置额外调优参数；测试文档仅使用默认 `_id` 索引 |
| Rust，仅原生版本 | rustc / cargo 1.99.1，`stable-x86_64-pc-windows-msvc` 工具链，PyO3 0.30.1 |
| 原生构建 | release，thin LTO，`codegen-units=1`，链接 Windows MSVC；构建配置未声明 `target-cpu=native` |
| 后端选择 | Rust 比较设置 `PYORM_NATIVE=required`，当前 Python 比较设置 `PYORM_NATIVE=off` |

`w=1` 的测量口径不等同于每笔写入都等待日志持久化；使用更强确认、远程网络或其他索引配置时，应重新测量。原生验证范围是本机 Windows / CPython 3.13。

## 比较负载

| 参数 | Python 热路径优化 | Rust 与调度：常规负载 | Rust 与调度：小容量队列 |
| --- | --- | --- | --- |
| 基线 | `975e865` | `f660b45` | `f660b45` |
| 比较版本 | 基线 / `f660b45` | 基线 / 当前 Python / 当前 Rust | 基线 / 当前 Python / 当前 Rust |
| 轮数及统计 | 两版本交替各 5 轮，中位数 | 三版本轮换各 5 轮，中位数 | 三版本轮换各 5 轮，中位数 |
| 标量文档数 | 1,000 | 1,000 | 3,000 |
| 每文档赋值次数 | 100 | 100 | 10 |
| 缓存命中及单线程赋值次数 | 每项 100,000 | 每项 100,000 | 每项 30,000 |
| 并发赋值业务线程 | 8，文档分区互不重叠 | 8，文档分区互不重叠 | 8，文档分区互不重叠 |
| 标量文档写库线程 / 任务容量 | 8 / 32 | 8 / 32 | 8 / 1 |
| 标量调度参数 | `flush_interval=600`，`batch_size=1001` | `flush_interval=600`，`batch_size=1001` | `flush_interval=600`，`batch_size=3001` |
| 列表 / 字典 / 集合大小 | 100,000 / 10,000 / 10,000 | 100,000 / 10,000 / 10,000 | 1,000 / 100 / 100 |
| 同一脏状态每轮捕获次数 | 5 | 5 | 1 |
| 容器文档写库线程 / 任务容量 | 4 / 1024 | 4 / 1024 | 4 / 1024 |
| 容器调度参数 | `flush_interval=600`，`batch_size=100000` | 相同 | 相同 |

`--workers` 与 `--max-pending` 控制脚本中的标量文档 Manager；容器诊断使用另一个 Manager，采用默认的 4 个写库线程和容量 1024。小容量队列报告的排空结果仅对应标量文档，不应将容量 1 套用于容器行。

每组比较使用同一套测量脚本和同一独立服务，测试数据库使用随机名称并在结束后清理。两轮报告是不同测量批次，不能把第一轮的优化前值与第二轮的 Rust 值直接相除，作为一次同环境交替比较的加速比。

## 计时与内存口径

- 初次加载计时包括 MongoDB 读取、BSON 还原及缓存绑定；后续缓存命中不重读数据库。
- 快照捕获计时使用 `perf_counter`，包括脏字段转换、BSON 校验、脱离缓存的复制和增量差异生成，排除网络 I/O。
- 排空计时从 `wait_idle` 开始，包括捕获、调度、MongoDB 操作和写入确认；结束后独立读取数据库核对内容及版本。
- 容器分别追加 2 个列表元素、修改一个字典值、新增一个集合成员；操作符由脚本断言验证。并发赋值重复已有赋值序列，最终值与首次排空后的值相同，验证没有新增文档写入。
- tracemalloc 峰值单独测量，排除计时影响；它只反映捕获期间的 Python 临时分配，不计 Rust 分配器、测量前缓存和数据库进程内存。

每份报告链接完整 JSON 样本；中位数和改善比例从各自同轮样本计算。数据库吞吐、服务器 CPU、单次操作符服务时间与 ORM 排空耗时是不同指标，不能相互替代。
