# 固定证据检索基线 v1

10条合成证据、12个问题。覆盖英文词边界、跨标的干扰、来源精确匹配、发布日期闭区间、缺日期、相反证据、撤回、有效期及零命中；基准时间固定2026-10-26T00:00:00Z。valid_until等于基准时刻仍有效，晚于它才过期。发布日期不是财报统计期。

运行：`.venv/Scripts/python.exe scripts/evaluate-retrieval-baseline.py --output <result.json>`。调用生产select_relevant_evidence，固定now与输入哈希，不使用模型、不联网。逐题显示预期/实际证据集合及TP/FP/FN，汇总micro precision/recall；无可计算分母时为null，不把未知当满分。

集合匹配不测排名或语义关系识别。supporting/contradicting是证据本身的标注，本基线只测是否保留。题库含英语合成材料，不能推断真实中英文文档整体召回率，更不能代替完整研究质量验收。修改输入或预期必须升版并保留新哈希；不得覆盖预期来迁就实现。
