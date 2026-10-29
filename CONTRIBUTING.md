# 开发与验证

本项目使用根 package.json 固定的 pnpm、Node 支持范围和 pyproject.toml 的 Python 范围；CI 使用 Node 24、Python 3.12。安装依赖：

```sh
pnpm install --frozen-lockfile
uv sync --locked --all-packages --inexact
```

本地使用 `--inexact` 保留已有桌面打包或 relay 依赖；CI 在全新环境中使用精确同步。

模块入口：`apps/web-shell` 为 React 界面，`apps/desktop-host` 为 Electron，`apps/core-api` 为 FastAPI，`apps/agent-worker` 为后台任务，`apps/plugin-runner` 为插件进程。`apps/relay` 不属于根 uv workspace，修改它时按自身 pyproject.toml 验证。

## 按影响范围验证

| 改动 | 必做检查 |
|---|---|
| 文档 / 规则 | 链接、命令、git diff --check |
| Web 行为 | pnpm typecheck；pnpm test:web；pnpm build:web |
| Core 行为 | pnpm core:lint；相关 pytest；提交前 pnpm core:test |
| 公共模型 / Schema | pnpm check:contracts；pnpm typecheck；相关两端测试 |
| API 路由 / IPC 桥 | pnpm check:bridge；相关鉴权测试；Windows 桌面链路实测 |
| 存储 / 插件 / 后台任务 | 对应模块回归，覆盖重启、失败或权限边界中实际受影响项 |

PowerShell 下运行 Core 测试（避免本机第三方 pytest 插件干扰）：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PYTHONPATH = 'src'
pnpm core:test
```

这些环境变量仅用于当前终端；CI 对 Python job 设置同样的变量。契约漂移时使用 `uv run --directory apps/core-api python ../../scripts/export_contracts.py`，桥清单使用 `pnpm gen:bridge`，检查生成差异后重跑校验。不得为了过检查盲目接受生成结果。

CI 保留类型、构建、pytest、ruff，并运行 Web 测试、契约与桥检查。CI 不覆盖 Windows 打包、真实模型和网络数据源验收。`core:format-check` 可用于格式审计，本次不新增全库格式门禁。

## 提交与技能维护

每次 PR 说明问题、改动、测试和未验证项；重要改动保留本地决策笔记。不要提交 `.env`、令牌、数据库、日志或内部过程资料。现有 docs Markdown 仍只在本地保存。

新增 skill 前确认具体缺口、来源、许可证及附带脚本；定向更新前备份现有副本，记录版本并验证。项目已有工具能完成时不额外引入一套开发框架。
