# 研究题库 v3

完整保留v2原20题，新增same_basis_complete。新增题是显式构造的合成正例：三项均属2026-01-01至2026-06-30、累计、CNY百万元、相同合并范围与修订版本；发布日期2026-08-20。这些是测试材料声明，不是适配器从2026H1推断出的真实财务事实。

原same_basis仍只有期间标签，不能按生产规则要求它无视缺失元数据计算。新增正例预期覆盖倍数1.25、资本开支后余额40百万元；expected与rubric不发送给模型。原20题保持不变也意味着其既有输入缺陷没有自动消失。

使用生产生成+闸门入口：`.venv/Scripts/python.exe scripts/evaluate-research-generation.py --corpus evals/research/v3/cases.json --base-url <gateway> --model <model> --ids same_basis_complete --timeout 60 --output <result.json>`。凭据仅通过进程环境RESEARCH_EVAL_API_KEY提供。cash-flow-2只把financial_source_metadata中明确提供的published_at/financial_period_end/financial_period_basis映射到Evidence；统计起点及其他原始字段保留在证据摘要，不扩展生产数据模型。

此入口绕过检索、降级、持久化和协同，不构成完整研究验收。不能将新版输入与旧短问答或缺元数据样本拼成质量提升率；不能用正例通过覆盖既有跨期间自由文本推断失败。

第78批单次正例实测及人工复核：`docs/assets/research-eval-20261027/generation-complete-v3{,-review}.json`，1请求、4284 tokens、37296ms。模型核对元数据后输出1.25倍、40百万元，本次给定字段算术及可比性核验符合要求；模型同时保留net_income未展开归母定义的限定，不据此宣称严格财务定义核验或研究质量全面通过。原跨期间无依据推断问题仍未修复。

第79批生产研究提示词升1.2，明确证据约束覆盖时效/局限等所有字段，禁止从H1/FY推断unknown口径或无来源断言更新/过期。`generation-p12-regression{,-review}.json`两题单次复测：跨期间未重现口径推断，元数据完整正例仍给出1.25/40，两题本次符合要求。验证两题各自与旧实测的user消息完全相同、system变更。2请求、7314 tokens、累计64406ms；不能据此声称稳定修复或推广至个股研报/四阶段，未计算提升率。

第80批适配器evidence-snapshot-3支持全部21题，非现金流题使用analysis证据类型，不附财务来源元数据；candidate_citations属于任务输入，会原样送入，expected/oracle/rubric仍排除。来源内部编号仍是快照内待核验的任务数据，生产UUID闸门只核验快照引用存在，不能据此声称内部S编号语义已通过。相反证据保留原文，不依据评分标准预先划分支持/反对关系。

整批原始结果路径为`docs/assets/research-eval-20261028/generation-full-v3.json`，逐题保存、complete为假时尚未结束，不重复启动。全21题跑完也不是全21题质量通过：旧财务题元数据缺口仍在，boundary_quote七天健康阈值未送入，不能按隐藏oracle硬评。此批不静默补齐旧题输入、不把生成+闸门冒充完整检索或四阶段验收。
