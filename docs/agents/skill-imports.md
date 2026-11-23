# 项目 skills 与 AGENTS

本项目配置从 `B:\Program\Project\Open\Clipboard` 的 `.agents/skills` 与 AGENTS 规则引入，
仅维护 PyORM 内的副本。没有修改源项目或全局 skills；引入配置不会启动审计、发布或删除操作。

| 来源 | PyORM 中的处理 |
| --- | --- |
| clipboard-dev | 改为 pyorm-dev，重写为 Python ORM 的架构、并发、缓存、验证规则 |
| code-auditor | 保留通用审计资源，新增 PyORM profile，排除环境、MongoDB 工具和构建产物 |
| version-release | 改为 pyproject.toml 单一版本源、uv 构建与 Python 验证流程 |
| delete-remote-release | 保留按需使用的 GitHub 清理能力，移除源项目凭据路径和强推发布假设 |
| tdd、diagnosing-bugs、codebase-design、code-review、domain-modeling、grilling | 保留共享技能原文件与支持资源 |

六个共享技能的上游为 [mattpocock/skills](https://github.com/mattpocock/skills)，
源项目记录的版本为 `4ae2317ddb22b046e26455e4f9c9b3aaaf62212d`。
保留原有元数据、许可证文件（如有）与隐式调用策略。更新时先比较上游差异，再应用 PyORM 适配，
不要覆盖本项目配置或全局安装。

根 AGENTS 与 pyorm-dev 规定：已确认的 ORM 接口无需重复确认测试边界；没有 Git 时使用会话需求
和用户提供的比较文件；不安装缺失的上游 setup wizard。实际授权与任务范围优先于共享技能的
提问或提交流程。仅在用户或实际调用的适用工作流要求时使用子代理。

本项目已初始化 Git，初始分支为 `main`，`origin` 为
`https://github.com/muutot/PyORM.git`。沿用一次提交一个已验证改动、gitmoji 格式及提交前
检查文档是否同步的约定。远程发布流程尚未配置，应以实际工作流和用户授权为准。
Python 环境与依赖使用 uv，目标版本为 3.13。

入口：[根 AGENTS](../../AGENTS.md)、
[开发 skill](../../.agents/skills/pyorm-dev/SKILL.md)、
[审计 skill](../../.agents/skills/code-auditor/SKILL.md)、
[发布 skill](../../.agents/skills/version-release/SKILL.md)。
