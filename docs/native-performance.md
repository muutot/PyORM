# Rust 加速与调度扫描优化

本轮基线为上一轮已提交的 `f660b45`。优化保留 Python Manager 管理的身份缓存、文档根锁、代次、写入包、版本过滤、增量操作符及丢失确认重试。Rust 处理容器转换、脱离缓存的复制和 BSON 字节比较；调度优化减少任务选择期间的完整队列扫描。

## 测试环境

| 项目 | 配置 |
| --- | --- |
| 机器 / CPU | HP Elite Tower 880 G9；Intel Core i7-14700，20 核 / 28 线程 |
| 内存 / 数据盘 | 32 GiB DDR5；SK hynix PC801 1 TB NVMe SSD，临时 MongoDB 数据位于 C: |
| 系统 / 解释器 | Windows 11 专业版 x64，build 26300；CPython 3.13.15，启用 GIL |
| 数据库 / 驱动 | MongoDB 8.0.4；PyMongo 4.20.1；客户端及独立数据库进程同机，通过回环 TCP 连接 |
| 原生构建 | Rust 1.99.1，PyO3 0.30.1；Windows MSVC 工具链，release / thin LTO / 单个 codegen unit |
| 写确认 / 统计 | `w=1`，默认 journaling；三版本轮换各 5 轮，取中位数 |

机器配置和驱动版本于 2026-11-23 在同机补录，原始 JSON 记录系统、Python、MongoDB、后端和负载。补录时电源方案为「平衡」，未记录各轮 CPU 频率、温度或后台负载。完整机器信息、参数、信息来源及计时口径见 [测试环境说明](benchmark-environment.md)。

## 同服务交替实测

2026-11-23，Windows 11 / CPython 3.13.15 / MongoDB 8.0.4，本机独立回环服务，`w=1`，默认 journaling。Rust 1.99.1 / PyO3 0.30.1，release、thin LTO、单个 codegen unit。基线、当前强制 Python、当前 Rust 三个版本轮换运行各 5 轮，以下为中位数。初次加载、快照捕获、真实数据库排空分别计时。

容器负载与上一轮一致：列表 100,000 个整数、字典 10,000 个键、集合 10,000 个整数；分别追加 2 个整数、修改一个键值、新增一个成员。每轮独立加载，捕获相同脏状态 5 次，再实际落库并读取 MongoDB 验证内容、版本、增量操作符和缓存状态。

| 测量项 | `f660b45` Python | 当前 Python | 当前 Rust |
| --- | ---: | ---: | ---: |
| 列表快照 ms | 16.84 | 16.25 | 11.48 |
| 字典快照 ms | 8.46 | 8.24 | 3.65 |
| 集合快照 ms | 16.00 | 15.53 | 3.97 |
| 列表加载 ms | 20.94 | 20.11 | 14.48 |
| 字典加载 ms | 4.99 | 4.72 | 3.64 |
| 集合加载 ms | 2.44 | 2.51 | 1.95 |
| 列表排空，含捕获及写入确认 ms | 31.27 | 30.33 | 25.43 |
| 字典排空，含捕获及写入确认 ms | 9.81 | 9.80 | 5.31 |
| 集合排空，含捕获及写入确认 ms | 18.34 | 18.13 | 6.55 |
| 1,000 个标量文档排空，容量 32 ms | 276.65 | 248.28 | 238.74 |

相对同轮当前 Python，Rust 的列表、字典、集合捕获分别快约 1.42×、2.26×、3.91×。相对已提交基线，真实容器排空耗时分别降低约 19%、46%、64%。这是客户端捕获、调度和数据库确认的合计，不是服务器 CPU 或纯数据库操作符性能。

单次捕获的 Python 分配峰值：列表 3,780,278 → 2,980,492 B，字典 733,731 → 734,011 B，集合 2,101,154 → 284,658 B。tracemalloc 不计入 Rust 分配器中的向量和哈希表，也不计入预先存在的缓存，不能将集合的约 86% Python 分配下降解释为整个进程内存下降 86%。

原始样本：[基线](benchmarks/performance-native-baseline.json)、[当前 Python](benchmarks/performance-native-python.json)、[当前 Rust](benchmarks/performance-native-rust.json)。标量赋值三组中位数约 134、134、132 ms；该负载没有可归因于 Rust 的明显收益。并发赋值受线程调度影响明显，保留全部样本但不作为稳定收益承诺。

## 小容量大队列

另一个独立服务上的相同三版本轮换测试：3,000 个文档，每个赋值 10 次，8 个工作线程，`max_pending=1`，各 5 轮。初次加载完成后计时排空，最终检查全部文档值及版本。

| 排空 3,000 个标量文档 | 中位秒 | 相对基线耗时下降 |
| --- | ---: | ---: |
| `f660b45` | 2.259 | — |
| 当前 Python | 1.445 | 36% |
| 当前 Rust | 1.599 | 29% |

本负载主要受调度和逐笔数据库确认影响。Python 排空改善表明无需 Rust 就能消除一部分调度扫描开销；Rust 没有额外改善该标量负载。原始样本：[基线](benchmarks/performance-queue-baseline.json)、[当前 Python](benchmarks/performance-queue-python.json)、[当前 Rust](benchmarks/performance-queue-rust.json)。

强制排空只选择当前空位需要的合格文档。自动调度只计数到 batch 阈值，达到阈值即选择有限任务；未达阈值保留原有截止时间和重试筛选。全部状态仍由原有 pending、inflight、blocked、retry 映射管理。大量阻塞/未来重试条目或未达 batch 阈值时仍可能遍历待写队列，本轮没有改变这些异常路径的数据结构。

## 稳定性与构建

真实 MongoDB 下 Rust 模式 134 项测试通过；强制 Python 模式 127 项通过，7 项 Rust 专项按预期跳过。Rust 格式、Clippy 全目标检查、2 项 Rust 单元测试及 Python Ruff/Pyright 检查通过。行为测试包括缓存身份、并发在途修改、固定写入包、失败可见性、增量追加、丢失确认、重试计时、自动调度、容量限制和关闭。

新增差异测试覆盖 BSON 驱动值、类型区分、字段顺序、集合排序的 BSON 小端长度前缀、重复成员、深层嵌套、别名与循环、可变 Code scope、自定义转换、键碰撞、回调顺序、转换期间容器修改及孤立代理字符的路径处理。Rust 解析器检查 BSON envelope 和值边界，并用截断、随机及变异缓冲区验证不会 panic；它只处理 PyMongo 已编码的内部输入，不承担完整 BSON 语义校验器职责。

原生递归最多 32 层，再转入 Python；自定义子类、转换器和驱动对象保留 Python 回退。仅在操作不可变 BSON 字节和 Rust 自有元数据时释放 GIL，所有 Python 容器和回调使用安全 PyO3 API，项目禁止 unsafe 代码。

构建和运行选择独立：`PYORM_BUILD_NATIVE=auto|off|required` 控制源码构建；`PYORM_NATIVE=auto|off|required` 在进程导入时选择运行后端。auto 允许扩展缺失；动态库加载错误记录警告后回退，required 模式直接失败，其他缺失依赖不会被隐藏。Cargo.lock 随源码包分发并使用 `--locked` 构建，最低 Rust 1.85。

原生 wheel、纯 Python wheel、缺少 Rust 编译器的 auto 构建、required 构建失败和源码包重建分别验证；独立安装检查包源码、元数据、py.typed、模型默认值及类型推断。原生验证范围为本机 Windows CPython 3.13；其他平台的原生构建尚未实测，纯 Python 包仍可用。

安装后的原生及纯 Python wheel 再分别运行完整真实数据库测试，结果同为 134 项通过和 127 项通过 / 7 项跳过。验证禁用项目 editable 导入钩子，避免源码扩展掩盖纯 Python 包中的缺失模块。源码包重建使用全新 Cargo target 目录，并核对安装二进制与 wheel 字节一致，防止恢复的源文件时间戳复用旧快照的构建缓存。

## 复现

先安装对应后端，使用独立 MongoDB 服务，从项目根目录执行：

```powershell
$env:MONGODB_URI = 'mongodb://127.0.0.1:27017'
$env:PYORM_NATIVE = 'required'
.\.venv\Scripts\python.exe examples\benchmark_hot_paths.py --rounds 5 --output docs/benchmarks/native-current.json
$env:PYORM_NATIVE = 'off'
.\.venv\Scripts\python.exe examples\benchmark_hot_paths.py --rounds 5 --output docs/benchmarks/python-current.json
.\.venv\Scripts\python.exe examples\benchmark_hot_paths.py --documents 3000 --mutations 10 --max-pending 1 --size 1000 --captures 1 --rounds 5 --output docs/benchmarks/queue-current.json

$env:PYORM_TEST_URI = $env:MONGODB_URI
.\.venv\Scripts\python.exe -m pytest -q
$env:PYORM_NATIVE = 'required'
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:PYORM_NATIVE
```

比较时应使用相同测量脚本和服务器，轮换版本，核对 JSON 中 implementation_path、backend 和 workload。脚本仅清理自己创建的随机数据库。性能收益受数据结构、网络、写确认、调度和磁盘影响；已经由数据库服务时间主导的负载，需要按实际生产流量测量。
