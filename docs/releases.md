# Wheel 构建与 GitHub Release

[`.github/workflows/release.yml`](../.github/workflows/release.yml) 负责构建安装包并发布到本仓库的 GitHub Releases。版本以 [`pyproject.toml`](../pyproject.toml) 的 `project.version` 为准。

## 触发方式

推送 `vX.Y.Z` 标签自动发布；标签与包版本不一致时立即失败。例如包版本为 `0.2.3` 时，标签必须为 `v0.2.3`。准备好版本与提交后执行：

```powershell
git push origin main
git tag v0.2.3
git push origin v0.2.3
```

需要升级版本时，先更新 `pyproject.toml`，完成验证并提交，再创建对应标签。发布工作流不会自动修改版本、创建标签或提交代码。

在 GitHub 的 Actions 页面选择 **Build wheels and release → Run workflow**，可以为选中的分支或标签构建产物。手动运行只上传 Actions artifacts，适合在打标签前检查构建结果；发布由标签推送触发。

## 构建产物

| 安装包 | Python / 平台 |
| --- | --- |
| 纯 Python wheel | Python 3.13+，`py3-none-any` |
| Rust wheel | CPython 3.13、3.14；Windows x64 |
| Rust wheel | CPython 3.13、3.14；Linux x64，glibc 2.28+ |
| Rust wheel | CPython 3.13、3.14；macOS ARM64 |
| 源码包 | 包含 Rust 源码、`Cargo.toml` 和 `Cargo.lock` |
| `SHA256SUMS` | 全部 wheel 与源码包的 SHA256 |

原生 wheel 使用 `PYORM_BUILD_NATIVE=required`，编译失败会让任务失败。Linux 通过 cibuildwheel 的 manylinux 2.28 容器构建并修复平台标签；Windows 与 macOS 使用对应架构的 GitHub 托管 runner。当前矩阵不包含 macOS Intel、Linux ARM64、musl 或自由线程 Python；这些环境可使用纯 Python wheel，或自行构建源码。

所有发布附件由 Actions 从标签源码构建，发布 job 只接收同一次运行的构建 artifacts。Rust 的 release profile 使用 `opt-level=3`、完整 LTO（`fat`）、单个 codegen unit、关闭增量编译与调试信息，并移除调试符号。保留 `panic=unwind`，让 PyO3 可以处理 Rust panic；使用架构的通用指令集，保持同架构机器之间的兼容性。这些是构建配置，既有性能报告仍记录其当时的构建环境，不代表已测量这次 profile 的性能收益。

从 Release 下载匹配的 wheel 后安装：

```powershell
uv pip install --python .\.venv\Scripts\python.exe --link-mode copy .\pyorm_mongo-0.2.3-py3-none-any.whl
```

使用 Rust 时下载匹配 Python、系统与架构的原生 wheel；可在导入前设置 `PYORM_NATIVE=required`，让缺少或加载失败的扩展明确报错。运行模式说明见 [README](../README.md)。

## 验证与失败处理

发布依赖所有检查与构建成功：

1. 校验包名、数字版本以及标签与版本的一致性。
2. 在 Linux 的 Python 3.13、3.14 上执行 Ruff、Pyright、Rust 检查，并对 `required` 与 `off` 两种运行模式执行完整测试，包含独立 MongoDB 8.0 服务。
3. 构建纯 Python wheel 与源码包，在源码目录外安装并测试纯 Python wheel，包含真实 MongoDB。
4. 每个原生 wheel 在源码目录外安装，验证 Rust 扩展与 `py.typed`，分别执行 Rust、Python 模式测试。这些平台安装测试不启动 MongoDB，数据库集成验证由前两项承担。
5. 合并产物，核对一个纯 Python wheel、六个原生 wheel、一个源码包的版本、平台和必要文件，生成校验和。
6. 创建草稿 Release，上传所有附件后再公开。

构建失败不会创建公开 Release，可在修复原因后重跑任务。附件上传或最后发布失败时，可能留下草稿；同一标签重跑会更新草稿附件再发布。已公开的同名 Release 会让发布步骤失败，避免覆盖既有发布；不要通过移动标签来替换已发布版本。

工作流使用 GitHub 提供的 `GITHUB_TOKEN`，仅发布 job 拥有 `contents: write`，构建 job 保持只读。Actions 引用固定提交。这里的发布目标是 GitHub Release；没有配置 PyPI 上传。
