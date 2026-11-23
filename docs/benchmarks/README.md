# 基准结果与性能对比样本

本目录统一保存已记录的基准结果 JSON。测试机器、软件版本与测量口径见 [环境说明](../benchmark-environment.md)，性能结论见 [Python 优化报告](../performance.md) 和 [Rust / 调度报告](../native-performance.md)。

| 样本 | 内容 |
| --- | --- |
| [benchmark-results.json](benchmark-results.json) | 缓存赋值与自动排空基准 |
| [collection-benchmark-results.json](collection-benchmark-results.json) | 单线程数组更新操作符比较 |
| [collection-benchmark-concurrent-results.json](collection-benchmark-concurrent-results.json) | 多线程数组更新操作符比较 |
| [performance-before.json](performance-before.json) | 第一轮 Python 优化前样本 |
| [performance-after.json](performance-after.json) | 第一轮 Python 优化后样本 |
| [performance-native-baseline.json](performance-native-baseline.json) | 第二轮容器负载基线 |
| [performance-native-python.json](performance-native-python.json) | 当前 Python 模式容器负载 |
| [performance-native-rust.json](performance-native-rust.json) | 当前 Rust 模式容器负载 |
| [performance-queue-baseline.json](performance-queue-baseline.json) | 小容量标量队列基线 |
| [performance-queue-python.json](performance-queue-python.json) | 当前 Python 模式小容量队列 |
| [performance-queue-rust.json](performance-queue-rust.json) | 当前 Rust 模式小容量队列 |

新测量结果也输出到本目录，可通过各脚本的 `--output` 指定新的文件名。比较数据使用同一测量批次的样本；文件内的 implementation_path 记录当时加载的包位置，移动结果文件不改变该历史信息。
