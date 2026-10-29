# Investment Steward 开发规则

## 开始任务

先读 [CONTRIBUTING.md](CONTRIBUTING.md)、相关模块及直接调用方，检查 `git status --short`。说明目标、改动范围和验证方式；保留已有未提交修改。

- 文案、格式、纯样式：直接修改并检查 diff 或渲染，不强制 PRD、拆单或笔记。
- 行为或 bug：明确预期，先复现或建立失败测试，再最小实现和相关回归。
- 跨模块、接口、存储、工具链：先记录方案与验收条件，按可验证切片推进。
- 不确定标识符从源码查证；业务含义、破坏性操作不明确时询问。不要反复请求已经给出的授权。

## 项目边界

主链路：web-shell → host-bridge / Electron → core-api → 本地 SQLite。沿用当前 pnpm + uv 工作区，不另加包管理器。

- 路由变化同步显式暴露声明和生成的桥清单；公共模型变化重新生成契约。使用已有生成器，不手改生成文件。
- 渲染层不得获取 Core 会话令牌；保持 loopback、默认拒绝及显式桥暴露边界。
- 不新增静默 fallback；保留既有授权行为，删除旧分支前先核实主路径。
- 不触碰真实数据库、密钥、备份；测试使用隔离数据。真实模型调用、部署和发布不属于普通验证。
- 研究结论保留来源、时间和不确定性；不能把构建或 mock 测试通过当作真实数据验证通过。

## 协作与交付

技能按需加载，同一任务只采用一套主流程。已有 `.teamai/skills/` 为团队技能来源，不批量覆盖本机工具目录。

重要决策记录于本地 `.agents/notes/{proposed,implemented,rejected,archived}/{feature,bug-fix,simplification,architecture,process,testing}/`，只创建用到的目录。先检索同主题旧笔记；纯机械改动不立笔记。遵循 write-notes-like-deepseek 格式与校验。内部笔记不入库；公共变更理由写 PR 描述。

交付列出改动、实际验证命令与结果、未验证项。CI 配置通过静态审查不等于远端 CI 已通过；Windows 桌面行为需要单独实测。
