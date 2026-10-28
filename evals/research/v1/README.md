# 固定研究评测题库 v1

20 个合成案例，观测基准日固定为 2026-10-26。原始证据与预期分离；向模型提供 question 和 evidence，不提供 expected、oracle、rubric 或候选引用的标准答案。题库修改后须保留新哈希，不能把新旧结果当作同条件比较。

覆盖现金流期间/单位/币种冲突、缺币种、零分母、负资本开支后余额、陈旧/边界/缺失/未来日期、未取得来源、仅公告标题、相反证据、未经核验的用户转述及不可比同行样本。

运行离线基线：`.venv/Scripts/python.exe scripts/evaluate-research-baseline.py`。结果包含题库 SHA-256，16 项规则可核对，4 项明确 manual_pending。该命令不调用模型，不测生成质量；退出成功只表示已实现规则未偏离预期，不能称“20 题模型评测通过”。

后续单模型、四阶段和多模型比较必须使用同一 evidence 快照；记录提示词/流水线版本、网关声明模型、完整用量或缺失标记、耗时、尝试次数及失败率。语义项按 rubric 人工核对，未评或调用失败不计通过；不使用模型自评分或推测收益。

短问答实测入口：`scripts/evaluate-research-live.py --base-url <gateway> --model <model> --ids same_basis currency_mismatch counter_evidence --timeout 45 --output <result.json>`，使用 `.venv/Scripts/python.exe` 执行；密钥仅通过进程环境 `RESEARCH_EVAL_API_KEY` 提供。复用生产客户端的超时、最多两次尝试及用量记录；每题默认最多60秒，命令上限120秒，输出长度提示不是硬计费上限。此为独立进程工具，不供服务进程导入执行。

入口只发送 question/evidence/as_of，记录题库及输入哈希、探测提示词版本与诊断；成功一律 unreviewed，失败 call_failed，均不自动计通过。不是生产研报或四阶段协同评测。2026-10-26 三题原始结果见 docs/assets/research-eval-20261026/live-probe.json；其中 same_basis 暴露“现金流覆盖倍数”分母歧义：项目规则用净利润，回答选择资本开支。原题库保留，后续明确指标定义须升版及重新计算哈希，不得覆写旧结果或直接横比。
