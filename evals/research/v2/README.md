# 固定研究评测题库 v2

第77批生产生成三题实测：`docs/assets/research-eval-20261027/generation-three-v1{,-review,-summary}.json`。3请求、10218 tokens、累计请求延迟77953ms，均通过格式/引用闸门。人工语义复核：跨币种停止计算符合要求；跨期间虽停止计算，却在时效说明里把unknown口径推定为半年累计，整题失败；同口径题只给2026H1标签，缺生产要求的统计起点/期间口径，不按旧1.25/40预期评分。不能将此三题与短问答做同条件提升率；后续正例应在新版显式补齐元数据，保留本次输入与失败原文。

生产生成与闸门评测：`scripts/evaluate-research-generation.py --base-url <gateway> --model <model> --ids same_basis period_mismatch --timeout 60 --output <result.json>`，通过Python执行，凭据仅由进程环境 RESEARCH_EVAL_API_KEY 提供。cash-flow-1适配器只接收现金流题，将原始任务证据和基准日作为未知关系的合成快照，不推断期间日期、不发送评分字段。调用真实research._try_model，保存实际消息、证据、原始回复、闸门输出和逐题检查点；不覆盖检索、本地降级、持久化和协同，也不等同完整run_research。model_answer_gated仍为质量未审阅，call_failed/gate_rejected单列，不自动补跑。第76批只做离线验证，尚无此入口联网结果。

2026-10-27评测输入修正：evidence-probe-1遗漏candidate_citations，四道引用题没有收到待核验编号，原实测保留但不能按引用oracle评分。evidence-probe-2仅补入待核验编号，仍不发送expected/oracle/rubric；不得将输入不同的两版本计算为同条件质量提升。旧样本full-probe-v2.json的题库v2与提示词版本1是两个独立版本号。

完整20题短问答实测及逐题复核见`docs/assets/research-eval-20261027/full-probe-v2{,-review,-summary}.json`：20次调用均返回、12156 tokens、累计请求延迟179009ms；按项目要求10题符合、5题财务边界失败、4题候选编号未送入和1题健康阈值未送入故不评分。保留原始回答，不计算全题库通过率。通用短问答未带完整生产财务策略，不能直接推断生产研报质量或把模型名字视作底层身份验证。

沿用 v1 的20例、证据、基准日、预期与评分标准；只修改8例现金流问题，明确覆盖倍数为 operating_cash_flow ÷ net_income，资本开支后余额为 operating_cash_flow − capex。定义与现有 cash_flow_quality 计算规则一致。此举不改变生产提示词、计算规则或历史报告。

v1 三题短问答暴露分母歧义后升版，保留原文件与实测结果。v2结果不能与v1当作完全相同输入比较，不计算跨版本质量提升率。

离线：`.venv/Scripts/python.exe scripts/evaluate-research-baseline.py --corpus evals/research/v2/cases.json`。

联网：`.venv/Scripts/python.exe scripts/evaluate-research-live.py --corpus evals/research/v2/cases.json --base-url <gateway> --model <model> --ids same_basis currency_mismatch counter_evidence --timeout 45 --output <result.json>`。密钥只通过进程环境 RESEARCH_EVAL_API_KEY 提供；仅合成证据短问答，调用成功仍是 unreviewed，不能代替生产研报/协同流程或人工语义验收。

首次三题结果位于 docs/assets/research-eval-20261026/live-probe-v2.json。同口径给出1.25倍、余额40；跨币种题先列机械计算再声明不可比，仍不满足项目停止计算规则。保留失败现象，不以调用成功充当质量通过；后续须验证生产输出链。该次结果不能与v1计算质量提升率。

真实协同引擎入口：`scripts/evaluate-research-collab.py --case currency_mismatch --base-url <gateway> --model <model> --timeout 60 --output <result.json>`，同样通过Python执行及进程环境提供凭据。仅支持cash_flow案例，复用真实角色模板、串行执行与质量闸门，关闭额外语义judge、不运行HTTP归档；逐阶段保存检查点，失败不自动续跑。首次结果collab-currency-v2.json：核对完成、初稿60秒超时、后两阶段未执行，超时用量未知。不得称为四阶段通过。

`--replay-prefix <artifact.json>` 可复用按序保存的完整stop回复，校验相同题库/证据/提示词/模型配置；重放仍执行真实后处理，只对未保存阶段联网。结果分别记录origin、replayed_requests及live_requests，重放耗时/未知用量不代表原成本。p215-tail组合样本四阶段done，但质量为needs_review，未做质量通过验收。
