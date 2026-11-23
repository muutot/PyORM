# PyORM Mongo — 实例属性与自动存盘 Manager

Python **3.13+**，运行时仅依赖 PyMongo。业务代码直接写 `self.xxx = value`，Manager 负责首次加载、唯一实例缓存、脏字段合并、多线程写库和退出时排空。

## 直接使用

```python
from pyorm import Document, init, shutdown


class Player(Document[int]):
    __collection__ = "players"
    _autosave = ("name", "coins", "level")
    name = ""
    coins = 0
    level = 1

    def reward(self, amount: int) -> None:
        self.coins += amount
        self.level = 2


init("mongodb://localhost:27017", database="game")  # 应用启动时初始化一次
try:
    player = Player.load(1001)
    player.name = "Alice"
    player.reward(10)
    assert Player.load(1001) is player
finally:
    shutdown()  # 应用退出时统一排空剩余变更
```

默认 Manager 在应用启动时调用 `init(...)` 初始化完成。业务模块直接 `Player.load(id)` 获取缓存实例，然后用 `self.xxx = value` 修改属性；无需传入 Manager。Manager 达到待存盘文档数量阈值或定时间隔后自动提交，应用退出时由 `shutdown()` 统一排空剩余变更。

`init(...)` 默认检查数据库连通性，失败会清理尚未发布的 Manager 并报告启动错误。多线程使用相同配置重复初始化会返回同一个 Manager；用不同配置再次初始化会报错，防止切换数据库或替换缓存。模型在初始化前调用 `load()` 会提示先执行启动初始化，不会偷偷创建新 Manager。

`Model.load(id)` 对同一集合、同一 ID 只创建一个缓存对象。首次访问全量读取一次；如果不存在，则根据类默认值初始化并安排首次写入。后续访问直接返回缓存对象，后续修改用 MongoDB `$set` / `$unset` 更新。首次创建提交初始字段，已有文档不会因为普通字段赋值而全量替换。

新建模型也可以定义构造函数：

```python
class Player(Document[int]):
    _autosave = ("name", "coins")

    def __init__(self, id: int):
        super().__init__(id=id)
        self.name = ""
        self.coins = 0
```

已有文档恢复时不执行业务构造函数，直接恢复持久化字段。需要默认值补全时，推荐声明类默认值，或使用 Field 的 default/default_factory。

## Pet 集合与嵌套对象

```python
from pyorm import Document, EmbeddedDocument, EmbeddedField


class PetStats(EmbeddedDocument):
    _autosave = ("level", "exp", "hp")
    level = 1
    exp = 0
    hp = 100


class Pet(Document[int]):
    __collection__ = "pet"
    _autosave = ("owner_id", "name", "species")
    owner_id: int | None = None
    name = ""
    species = "cat"
    stats = EmbeddedField(PetStats)

    def train(self, exp: int) -> None:
        self.stats.exp += exp

    def level_up(self) -> None:
        self.stats.level += 1
        self.stats.exp = 0
        self.stats.hp += 20


# 应用入口已经执行 init(...)，业务模块直接使用。
pet = Pet.load(2001)
pet.owner_id = 1001
pet.name = "Mimi"
pet.level_up()
pet.train(50)
assert Pet.load(2001) is pet
```

`Pet` 保存在独立的 `pet` 集合中，`owner_id` 记录所属玩家 ID；`PetStats` 保存在该宠物文档的 `stats` 内，不单独建集合。

`EmbeddedDocument` 与 `Document` 一样支持 `_autosave`，列出的属性会自动跟踪并持久化。`EmbeddedField(PetStats)` 默认以 `PetStats` 为工厂，保证每只宠物有自己的属性对象，首次从 MongoDB 恢复时仍得到 `PetStats` 实例。需要定制初始值时可传 `default_factory=lambda: PetStats(hp=200)`；`default=None` 或 `default_factory=None` 会关闭自动创建。嵌套类型的构造函数若要求额外参数，应显式提供工厂。

`EmbeddedField` 支持泛型，但通常只需写 `EmbeddedField(PetStats)`，编辑器便会把 `pet.stats` 推断为 `PetStats`，提供 `level / exp / hp` 补全；无需重复写泛型参数和默认工厂。需要时仍可写 `EmbeddedField[PetStats](PetStats)`。`Field[int]` 和 `ListField[PetStats]` 同样支持类型提示，发布包包含 `py.typed` 标记。

显式声明的 `Field`（包括 `EmbeddedField`、`ListField`）都会自动注册为持久化字段，无需重复加入 `_autosave`。`_autosave` 用于把普通属性及其默认值转换成字段；所以上面的 `stats` 不在 `_autosave` 中，仍会自动存盘。

模型用 `Document[int]`、`Document[str]` 声明 ID 类型；省略时默认为 `ObjectId`。例如 quickstart 的 `Player` 和 `Pet` 都继承 `Document[str]`，所以 `Player.load("player-1")` 返回 `Player`，`player.id` 是 `str`，可直接赋给 `pet.owner_id: str | None`。`load`、`from_mongo`、`save`、`reload` 用 `Self` 保留子类类型；Manager、Repository、QuerySet 和 Future 也保留具体模型类型，可能不存在的查询返回 `Pet | None`。字符串/整数 ID 的模型直接构造时应传入相应的 `id`。

类型提示服务于方法接口和编辑器补全，不额外引入运行时转换、`cast()` 或类型检查。空容器应声明元素类型，例如 `skills: list[str] = []`；动态字段关键字参数保持原有写法。

已有宠物执行 `self.stats.level = 5` 会自动标记所属文档的 stats 字段，并生成 `stats.level` 增量更新。嵌套对象没有独立的提交队列，由所属 `Pet` 的 Manager 统一调度，无需字典赋值或手动保存。

也支持 dict/list/set 的原地修改；Manager 在绑定对象时包装容器以捕获变化。数组纯尾部追加、集合纯新增时，单个新增用索引点路径 `$set`，多个新增用 `$push` / `$each`；其他数组变化替换对应数组，字典和嵌套对象使用点路径更新。赋入普通容器时会复制容器结构，请通过实例字段继续修改，不要修改赋值前保留的原始容器。已绑定的嵌套对象不能共享给另一个字段或文档。

Field 可用于类型校验、必填字段、默认工厂、枚举和数据库字段别名。`_autosave` 列表中的字段也会变成可跟踪的实例属性；其他业务属性不自动存盘。`None` 保存为 BSON null；`del self.optional_field` 生成 `$unset`。

## 列表、字典、元组和集合字段

四种容器都可以作为一个 `_autosave` 字段，业务使用正常的 Python 操作：

```python
class Pet(Document):
    __collection__ = "pet"
    _autosave = ("skills", "attributes", "position", "tags")
    skills: list[str] = []
    attributes: dict[str, int] = {}
    position: tuple[int, int] = (0, 0)
    tags: set[str] = set()

    def update_state(self) -> None:
        self.skills.append("scratch")
        self.attributes["attack"] = 25
        self.position = (10, 20)
        self.tags.add("rare")
```

| 字段类型 | 可跟踪的操作示例 | 后续 MongoDB 更新 | 加载后的 Python 类型 |
| --- | --- | --- | --- |
| `list` | `append`、`extend`、`pop`、索引/切片赋值、`+=` | 纯追加：单个索引 `$set`，多个 `$push` / `$each`；其他变化整体 `$set` | `list` |
| `dict` | 键赋值、`update`、`del`、`pop`、`\|=`，含嵌套修改 | 点路径 `$set` / `$unset` | `dict` |
| `tuple` | 整体赋值、`+=`；内部可变对象的修改也跟踪 | 纯尾部扩展：单个索引 `$set`，多个 `$push` / `$each`；其他变化整体 `$set` | `tuple` |
| `set` | `add`、`discard`、`update`、`\|=`、`&=`、`-=`、`^=` | 缓存去重后，单个索引 `$set`，多个 `$push` / `$each`；含移除整体 `$set` | `set` |

`_autosave` 根据类默认值记录容器类型，解决 MongoDB 把 list、tuple、set 都保存为数组后无法区分的问题；恢复的 list/dict/set 由 ORM 内部包装，但仍支持相应 Python 容器 API。每个实例独立复制可变默认值。容器字段赋值会校验声明的类型，`set` 字段应赋集合，`tuple` 字段应赋元组；无需原地修改时，也可以直接整体赋值。

如果默认值只在业务构造函数里赋，加载时不会执行该构造函数，应使用类默认值，或显式声明 `Field(set, default_factory=set)` / `Field(tuple, default=(0, 0))`。

Manager 对比最近已确认快照与当前值：已有数组的原有前缀保持 BSON 类型和值不变、仅尾部增长时，只发送新增元素。单个新增生成 `skills.原长度` 这样的索引点路径 `$set`；多个新增合并为一次 `$push` / `$each`。已有集合在缓存端计算新增成员，采用相同的追加策略；重复 `add` 不会重复存储，数据库无需用 `$addToSet` 再扫描数组去重。首次创建或字段原来不存在时用 `$set` 初始化；同时修改原有数组元素、删除、排序，或集合包含移除时，用 `$set` 写最终结果。

```python
# 已有宠物的 skills、tags 字段已落库。
pet.skills.append("scratch")
pet.skills.extend(["jump", "run"])
pet.tags.add("rare")
pet.tags.update({"trained", "rare"})
# Manager 自动合并并提交，无需手动操作数据库：
# $push: {skills: {$each: ["scratch", "jump", "run"]}}
# $push: {tags: {$each: ["rare", "trained"]}}
```

字典里的字典变化用点路径更新；列表或元组里修改原有嵌套元素会更新整个对应数组。字典键需要满足 MongoDB 字段名约束，整数键保存为字符串；集合成员需要可哈希且能编码为 BSON，集合顺序不作为业务语义。元组自身不可原地改元素，需要 `self.position = (...)`；元组中的 list/dict 仍可原地修改并自动存盘。

自动类型恢复保证已声明字段的顶层类型。任意混合容器内部的 tuple/set 与 list 在 BSON 中同为数组，不能凭数据自动辨别；需要保留元素类型时可用 `ListField(Field(tuple))` 等显式元素声明，复杂结构可拆成有字段声明的 `EmbeddedDocument`。

## Manager 调度与缓存保证

```python
init(
    "mongodb://localhost:27017",
    database="game",
    flush_interval=1.0,  # 第一笔未提交变更约 1 秒后开始调度
    batch_size=100,  # 100 个脏文档时提前调度
    workers=8,
    max_pending=1024,  # 运行中 + 已排队的写库任务上限
    max_pool_size=32,  # 每个 MongoDB 节点的连接池上限
    retry_interval=1.0,
)
```

同一对象连续赋值合并到同一脏文档，最终提交最后的字段值，避免每次赋值都生成数据库任务。`batch_size` 按脏文档数量计数，`max_pending` 限制工作线程的任务数量；任务满时变更仍保留在缓存，Manager 等有空位再提交。MongoDB 耗时或重试可能延长实际落库时间。

每次赋值有自己的变更代次。Manager 只序列化脏字段，将发送内容复制成固定的写入快照，并在数据库操作期间释放实例锁。因此：

1. 赋值先改变缓存，再异步写入数据库。
2. 写库期间的新赋值不被旧写入结果覆盖。
3. 成功确认只清除与本次发送代次相同的脏标记。
4. 新代次仍留在缓存，自动进入下一批。
5. 失败保留缓存与脏标记，数据库结果不回灌缓存。

这就是“cache >= 数据库”：数据库跟随缓存的修改进度，不是字段数值必须大于数据库，也不是缓存与数据库始终同步。正常运行期间，Manager 不全量重读文档，不靠 reload 对齐状态。

同一文档只允许一笔写入在途；不同文档可并行写库，所有线程共享一个 MongoClient 连接池。缓存实例只有一份，后台写库线程只确认已发送的变更，不修改业务属性。普通同步实例方法在 ORM 内部自动按实例串行执行，方法体直接写属性操作：

```python
def reward(self, amount):
    self.coins += amount
    self.stats.exp += 10
```

锁由 ORM 内部维护，业务方法无需写 `with self.locked()`。如果业务由一条逻辑线程顺序执行，后台 MongoDB 工作线程不会与它抢着修改缓存；如果多个业务线程同时调用同一个实例的方法，ORM 会把完整方法调用串行执行，保护 `+=` 和一组读改写。方法内调用其他实例方法支持重入。Manager 只短暂加锁捕获和确认快照，不会持有此锁等待数据库网络操作。

同步方法保护的是实例方法的整个调用。在实例方法之外由多个线程拼接读改写、异步方法或生成器跨暂停点修改状态，需要由应用的业务调度统一顺序；它们不属于普通同步方法调用。

## 写入失败与生命周期

Manager 用 `_orm_version` 检测其他写入方，用 `_orm_write_token` 标识已发送批次。网络错误的重试发送同一快照、同一目标版本、同一 token；即使第一次写入成功但确认丢失，也不会重复增加版本或重放业务 `+=`，无需读取旧业务数据确认结果。

包含 `$push` 的批次只匹配发送前的版本，成功的批次不会再次追加。若重试未匹配旧版本，Manager 用该批次的版本和 token 做一次只涉及元数据的写入确认；不读取或覆盖缓存里的业务数据。固定索引 `$set` 重试会写相同位置，不会重复追加。集合增量追加依赖同一文档由一个 Manager 写入，以及外部写入方遵守版本约束。

同一文档应由一个 Manager 负责写入。外部进程若同步维护 `_orm_version`，会触发版本冲突；Manager 保留缓存，暂停该文档并报告错误。外部写入绕过版本字段时无法提供这项保证。多进程同时修改同一文档需要业务层决定归属或合并规则。

```python
def report_error(document, error):
    print(document.id, error)


init(database="game", on_error=report_error)  # 在应用入口初始化时配置
```

网络错误自动延时重试。校验错误、重复键、版本冲突不会无限重放，可通过 `manager.errors` 和 `manager.stats` 查看；默认会记录日志。非冲突失败后修改字段可触发新尝试，版本冲突需要处理写入方归属，Manager 不会读取数据库覆盖缓存。

应用退出统一调用 `shutdown(timeout=10)`。关闭会停止接受新赋值、等待已开始的赋值完成、立即提交剩余变更、等待确认，再关闭线程和自建连接。永久错误或超时会抛出异常并保留默认 Manager 和缓存，供应用继续处理，不会默默丢弃数据。直接调用托管对象的 save/save_async/reload/delete 会被拒绝。

`manager.wait_idle(timeout=...)` 是测试、运维和生命周期的等待接口，业务逻辑不需要它。注入 `client=` 默认不关闭外部客户端；`owns_client=True` 可让 Manager 管理其生命周期。

缓存是进程内状态，不提供进程崩溃后的持久恢复。默认一直持有已加载实例，避免重复加载或淘汰尚未确认的缓存；大量长期无用实例的释放应另行定义生命周期。`w=0` 无确认写入不受支持。

## 安装、示例和测试

项目 `.venv` 使用 Python 3.13：

```powershell
uv pip install --python .\.venv\Scripts\python.exe --link-mode copy -e '.[dev]'
.\.venv\Scripts\python.exe examples\quickstart.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pyright
```

`examples/quickstart.py` 同时演示 `players` 和 `pet` 两个独立集合，并在嵌套对象上使用 `_autosave`。示例在应用入口初始化 Manager，业务代码只使用 `Player.load(id)` / `Pet.load(id)` 和实例属性，MongoDB 后台工作线程负责落库。需运行 MongoDB，默认连接 `localhost:27017`，也可以设置 `MONGODB_URI`。

```powershell
$env:PYORM_TEST_URI = 'mongodb://localhost:27017'
.\.venv\Scripts\python.exe -m pytest -q -m integration

$env:MONGODB_URI = 'mongodb://localhost:27017'
.\.venv\Scripts\python.exe examples\benchmark.py --operations 1000 --threads 8 --mutations 100
```

基准先全量加载 1,000 个文档，再执行 100,000 次缓存赋值，退出时自动合并成 1,000 笔文档增量写入。初次加载、缓存赋值速度和落库速度分别计量；实际吞吐依赖网络、索引和 MongoDB 写确认方式。结果保存在 `benchmark-results.json`。

MongoORM / Repository / QuerySet 保留为底层显式数据库接口；托管业务对象使用 Manager，避免通过底层接口绕过缓存。

本次基准运行环境为 Python 3.13.15 / 本机 MongoDB 8.0.4 / 8 个写库线程。100,000 次缓存赋值耗时约 0.18 秒，自动排空 1,000 笔增量写入耗时约 0.23 秒；这是单次本机结果，生产环境请运行脚本测量。

### 数据库为瓶颈时的数组更新

`examples/benchmark_collections.py` 对比整数组 `$set`、索引 `$set`、`$push` / `$each`、`$addToSet` / `$each`，测量写入报文大小、客户端耗时、数据库写入服务时间和吞吐。它创建并清理独立的临时基准数据库；数据库服务时间来自全局 `opLatencies`，应在隔离、空闲的服务上运行。

```powershell
.\.venv\Scripts\python.exe examples\benchmark_collections.py
.\.venv\Scripts\python.exe examples\benchmark_collections.py --sizes 100000 --batches 32 --workers 8 --iterations 50 --output collection-benchmark-concurrent-results.json
```

Python 3.13.15 / 本机 MongoDB 8.0.4 / `w=1`，已有 100,000 个整数的数组，每种策略 100 次写入，单工作线程的实测：

| 更新策略 | 每次新增 1 个：数据库平均 ms | 每次新增 32 个：数据库平均 ms | 新增 32 个：平均更新报文 |
| --- | --- | --- | --- |
| 整数组 `$set` | 10.79 | 10.62 | 约 1.11 MB |
| 索引点路径 `$set` | 8.85 | 19.12 | 710 B |
| `$push` / `$each` | 12.15 | 11.17 | 385 B |
| `$addToSet` / `$each` | 14.15 | 82.56 | 389 B |

报文缩小不代表数据库工作量按同比例下降，`$push` 也可能需要处理较大的原文档。单个新增采用固定索引，批量新增采用 `$push`，集合去重移到缓存端，以减少数据库操作开销。Manager 合并未确认变化以减少写入次数；数据库已经饱和时，提高工作线程数不一定提高吞吐，需同时观察吞吐和延迟。持续增长的大数组应按业务拆成有界分桶或独立集合，避免整个大文档成为长期写入瓶颈。

具体结果见 `collection-benchmark-results.json`；这是单次本机测量，数组元素、索引、写确认、并发和磁盘条件都会影响结果。

相同服务下，8 个工作线程分别写独立文档，数组初始 100,000 个整数，每次新增 32 个，每线程 50 次写入：

| 更新策略 | 数据库平均服务时间 ms | 总吞吐：写入/秒 |
| --- | --- | --- |
| 整数组 `$set` | 24.44 | 193 |
| 索引点路径 `$set` | 41.65 | 184 |
| `$push` / `$each` | 26.46 | 289 |
| `$addToSet` / `$each` | 133.49 | 59 |

详情见 `collection-benchmark-concurrent-results.json`。并发下的数据库服务时间包含等待，不是 CPU 时间；本例 `$push` 的服务时间与整数组 `$set` 接近，吞吐收益更多来自更小的报文和更少的客户端 BSON 编码工作。这是操作符基准，预先生成更新内容，不包括 ORM 构造和对比快照的耗时。
