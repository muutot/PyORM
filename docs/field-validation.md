# 字段校验与 0.2.3 升级

`_autosave` 将普通类属性转换为字段，并在构造、赋值、BSON 恢复和序列化边界校验类型。嵌套 EmbeddedDocument 使用相同规则。显式 `Field`、`EmbeddedField`、`ListField` 自动注册，其参数优先于类注解。

## 声明规则

| 声明 | 非空值的运行时类型 | 是否允许 None |
| --- | --- | --- |
| `coins = 0` | int，拒绝 bool 和 float | 是，保留未注解字段的原有规则 |
| `rate = 0.0` | float，拒绝 int 和 bool | 是 |
| `enabled = False` | bool，拒绝 int | 是 |
| `coins: int = 0` | int，拒绝 bool | 否 |
| `coins: int \| None = None` | int，拒绝 bool | 是 |
| `amount: int \| float = 0` | int 或 float，拒绝 bool | 否 |
| `flag: int \| bool = 0` | int 或 bool | 否 |
| `value = None`，或只列入 `_autosave` | 不约束 Python 类型，仍需能编码为 BSON | 是 |
| `value: Any = 0`，或 `value: object = 0` | 不约束 Python 类型，仍需能编码为 BSON | 是 |
| `tags: set[str] \| None = None` | 顶层 set，BSON 数组恢复为 set | 是 |
| `stats = Stats()` | Stats；恢复时得到 Stats 实例 | 是 |
| `stats: Stats \| None = None` | Stats；恢复时得到 Stats 实例 | 是 |

不做数值转换。默认 `0.0` 的 float 字段接收小数；需要同时接收整数和小数时显式声明联合类型。布尔值只有在允许类型中包含 bool 或更宽泛的类型时才被接收，显式 `Field((int, float))` 也拒绝 bool。

注解支持具体运行时类型、Union/Optional、`Annotated` 的基础类型和容器的顶层类型。Python 3.13 的延迟注解及函数内定义的嵌套类型在模型声明时解析；类型名必须在当时可解析。无法解析或不支持的注解抛出 TypeError，可改用显式 Field。嵌套字段需要一个具体的 EmbeddedDocument 类型，多个嵌套类型的联合需要另行定义判别及恢复方式。

set、frozenset、tuple 和 Enum 的自动恢复需要一个具体的非空类型，支持与 None 联合。它们与其他非空类型的联合（例如 `set | tuple`、`set | int` 或 `Color | int`）需要显式自定义 Field 的 from_mongo 规则；模型声明会拒绝这种自动字段，避免写入后无法恢复。多个数组或枚举类型共享 BSON 表示时还需要业务判别信息。

`dict[str, int]` 和 `list[str]` 的元素参数用于编辑器推断，不新增逐元素运行时校验。需要元素校验和类型恢复时使用 `ListField(Field(...))` 或 `ListField(EmbeddedField(...))`。任意混合容器内部的 tuple/set 与 list 在 BSON 中同为数组，继续遵守 [README 的容器恢复边界](../README.md#列表字典元组和集合字段)。

## 默认值

可变默认值独立复制。嵌套默认实例复制已声明字段和保留的额外 BSON 数据，使用新锁及新跟踪绑定，不复制所属 Manager，也不重新执行业务构造函数和字段默认工厂。复制保留默认值中的 Python 容器结构。根 Document 具有独立的缓存身份，不能作为可复制的嵌套模板。

```python
from pyorm import Document, EmbeddedDocument, EmbeddedField


class Stats(EmbeddedDocument):
    _autosave = ("coins",)
    coins = 0


class Player(Document[str]):
    _autosave = ("optional_coins", "stats")
    optional_coins: int | None = None
    stats = Stats(coins=3)
    custom_stats = EmbeddedField(Stats, default=Stats(coins=7))
```

若业务方法依赖构造函数设置的非字段属性，使用 `default_factory=lambda: Stats(...)` 创建默认对象。数据库加载继续不执行业务构造函数。

## 兼容性决定

已发布 0.2.3 仅为 `_autosave` 的容器默认值记录类型，标量字段曾接受异型值。这次修复明确收紧行为：`coins = 0` 的历史字符串值、bool 值或小数值会在 `get` / `load` 时抛出 ValidationError，加载失败的对象不会进入缓存。无法从该缺陷推导出发生过数据丢失；直接证据是错误类型曾被接受。

此前仅作为编辑器提示的类注解现在也决定字段类型和 nullable。已有非 Optional 注解的字段（包括容器）会拒绝存储的 null；需要保留 null 时声明 `T | None`，或用显式 Field 指定 nullable。未注解字段及显式 Field 的原有 nullable 规则保留。nullable 控制 null 值，字段能否删除仍由 required 单独控制。

升级时应选择符合业务实际的一种方式：

1. 修正已有数据，使字段符合类默认值或明确注解。
2. 若多种类型合法，声明准确的联合类型，例如 `coins: int | str = 0`。
3. 迁移期间需要保留宽松行为时，显式使用 `coins = Field(default=0)`；这只关闭 Python 类型约束，BSON 校验仍保留。显式 Field 不受 `_autosave` 注解推断影响。

这项兼容性说明记录源码行为变化；版本号仍由 pyproject.toml 管理，发布工作遵守 [发布流程](releases.md)。

## 验证入口

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_autosave_validation.py tests/test_models.py tests/test_saved_containers.py tests/test_runtime.py
.\.venv\Scripts\python.exe -m pyright
```

用例覆盖赋值及坏数据加载、bool/int/float 区分、Optional/Union、局部延迟注解、枚举和容器恢复、显式宽松字段、嵌套模板的独立性、默认工厂不重跑及已绑定容器的复制。
