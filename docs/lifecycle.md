# 缓存释放与删除

一个 Manager 对同一集合和 BSON ID 只保留一个有效缓存对象。缓存默认长期持有，不做 LRU、TTL 或其他自动淘汰。显式释放或删除结束该实例的托管生命周期；旧引用继续可读，后续加载取得新的有效实例。

## 接口与选择

| 接口 | 尚未发送的变更 | 已在途或结果不确定的写入 | 数据库记录 |
| --- | --- | --- | --- |
| `manager.release(doc, timeout=10)` | 全部确认落库后释放 | 等待完成；沿用现有批次重试及确认 | 保留 |
| `manager.release(doc, discard=True, timeout=10)` | 明确丢弃 | 仍等待现有批次结果确认 | 保留 |
| `manager.delete(doc, timeout=10)` / `doc.delete(timeout=10)` | 删除成功后丢弃，不先写入这些变更 | 等待完成后按最新确认的版本删除 | 删除或确认不存在 |

`release` 成功返回 True，重复释放已经释放的实例返回 False。`delete` 实际删除数据库记录时返回 True；记录已经不存在、取消尚未持久化的新对象、重复删除已经删除的实例时返回 False。跨操作使用已失效实例（例如释放后再删除）抛出 DocumentInactiveError。传入另一个 Manager 的活动实例或非托管实例会报错。

删除同时匹配 `_orm_version` 和最近确认的 `_orm_write_token`，包括没有这些元数据的旧记录。其他写入方改变版本或重建同 ID 后改变 token 时，抛出 ConcurrentModificationError 并保留原缓存及未确认变更；不会强制删除。绕过版本和 token 的外部写入仍不具备缓存一致性保证。

记录已由外部删除时，删除的未匹配结果会通过只读取 `_id` 的存在性查询确认，随后清理缓存、错误和调度状态。它不重新加载业务字段，也不把数据库内容覆盖回旧对象。

## 应用用法

```python
from pyorm import Document, get_manager, init, shutdown


class Player(Document[str]):
    __collection__ = "players"
    _autosave = ("coins",)
    coins = 0

    def reward(self, amount: int) -> None:
        self.coins += amount


init(database="game")
try:
    player = Player.load("player-1")
    player.reward(10)
    get_manager().release(player)  # 应用明确结束此实例的使用，确认后释放缓存
    player = Player.load("player-1")
    player.delete()  # 通过所属 Manager 删除
finally:
    shutdown()
```

应用负责停止使用旧引用。释放/删除期间，该文档的新字段赋值、容器修改和嵌套修改抛出 DocumentInactiveError；成功后这些引用永久失效，save/save_async/reload 也抛出同一异常。只读属性保留最后缓存值，`is_dirty` 为 False，`pending_update()` 为空。

生命周期操作在普通同步业务方法和 `locked()` 块之外调用，也不能在 Manager 工作线程中启动。持有该文档锁时等待其正在进行的生命周期操作会被拒绝，防止与写入确认或逐出相互等待。业务方法仍只需要普通属性和容器操作。

## 并发与失败

操作先保留该 ID 的生命周期入口，再暂停新修改。数据库等待期间释放文档锁及 Manager 调度锁。并发 `get` / `load` 等待操作结束，成功后加入同一个首次加载 Future；失败后继续取得保留的原缓存。缓存不会在旧写入仍可能执行时重新接受一个有效替代对象。

默认 release 的校验错误、持久化冲突、超时会保留缓存和待处理状态。显式 `discard=True` 可用于应用已决定丢弃的变更，例如保留外部写入而放弃本地冲突值。它仍需完成已有不确定批次的确认，避免旧批次在释放后继续写入。

delete 的命令异常或冲突也保留缓存。网络异常可能发生在服务器已删除、确认却丢失之后；调用方重试 delete，可确认记录已不存在并结束该实例。删除不会在后台自动重放生命周期命令。timeout 限制进入操作、等待写入及删除命令的时间；删除确认后必须完成本地逐出，不能因最后的锁等待超时而重新激活已删除对象。

`wait_idle` 等待正在进行的生命周期操作，`close` 等待已开始的释放/删除后再排空。错误仍可见，默认 Manager 在 shutdown 失败时仍保留。每个文档释放后，集合到模型的注册继续保留到 Manager 生命周期结束；同一集合不能换成另一种模型。

## 外部删除后的恢复

```python
# 应用已收到该文档的 PersistenceError，并确认记录由外部删除。
manager.delete(document)  # 返回 False，确认不存在并清理该文档
manager.wait_idle(timeout=10)
manager.close(timeout=10)
```

这段是运维/生命周期处理示意。仍存在的版本冲突需要应用决定写入方归属；若决定放弃本地值，使用 `manager.release(document, discard=True)`，保留数据库现有记录。

## 容量观测与验证

`manager.stats` 返回 cached、pending、inflight、errors、loading、retiring 计数。retiring 对象在操作完成前仍计入 cached。没有新增内存字节数估算接口；需要内存数据时测量进程 RSS，Python tracemalloc 不包含 Rust 分配。

生命周期回归使用同一套公共行为用例，分别连接 mongomock 和真实 MongoDB。真实参数标记为 integration，并只创建、清理自己的随机 `pyorm_test_*` 数据库。

```powershell
$env:PYORM_TEST_URI = 'mongodb://localhost:27017'
.\.venv\Scripts\python.exe -m pytest -q tests/test_manager_lifecycle.py -m integration
.\.venv\Scripts\python.exe -m pytest -q
```

真库用例覆盖在途写入时释放/删除、并发 get 的单实例恢复、旧引用失效、未发送变更处理、外部删除与版本冲突、同 ID 同版本重建、丢失删除确认、丢失 `$push` 确认、生命周期超时和关闭期间的删除。

2026-11-24，本机 CPython 3.13.15 / MongoDB 8.0.4 的完整验证：Rust required 模式 200 项通过；Python off 模式 193 项通过，7 项原生专项按配置跳过。两种模式均执行了 28 项真库用例。Ruff 格式及 lint、Pyright、文档链接及 Python 示例语法检查通过；纯 Python wheel 的元数据、py.typed 和隔离安装后的字段/生命周期验证也通过。
