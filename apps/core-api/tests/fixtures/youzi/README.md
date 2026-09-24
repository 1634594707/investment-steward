# 席位证据夹具（2026-09-16 真实响应原文）

本目录下的文件是**真实抓取的响应字节**，不是手写样例。目的是让 P1/P2 的解析测试**在断网环境**也能跑，
并且能挡住「脏数据被当干净数据用」。

抓取时间：2026-09-16（交易日 2026-09-16 收盘后）。
抓取方式：`apps/core-api/tests/conftest.py` 的 `load_youzi_fixture` / `load_youzi_json` 按 UTF-8 严格解码。
**夹具一律不修改**：要表达「数据源变了」，就换一份新夹具，不要编辑旧夹具去迁就代码。

## 文件清单

| 文件 | 来源 | 关键用途 |
| --- | --- | --- |
| `cffex_index_20260916.xml` | `http://www.cffex.com.cn/sj/hqsj/rtj/202609/16/index.xml`（493,864 B） | 日统计。**含反例**：期权 `productid=MO` 与国债 `T`/`TF`/`TL`/`TS` |
| `cffex_ccpm_IF_20260916.xml` | `http://www.cffex.com.cn/sj/ccpm/202609/16/IF.xml`（89,933 B） | 会员排名：80 条 × `datatypeid` 0/1/2 各 80 条 |
| `cffex_ccpm_IH_20260916.xml` | `http://www.cffex.com.cn/sj/ccpm/202609/16/IH.xml`（89,717 B） | 第二个品种，验证按品种解析不串行 |
| `em_lhb_details_20260916.json` | 东财 `RPT_DAILYBILLBOARD_DETAILS`（2026-09-16） | 当日上榜列表（71 行）。**含反例**：`EXPLAIN` 夹带「成功率 28.87%」 |
| `em_lhb_detailbuy_20260916.json` | 东财 `RPT_BILLBOARD_DAILYDETAILSBUY`（2026-09-16） | 单票买入席位（365 行 / 118 家）。**含反例**：`机构专用`、`沪股通专用`、`深股通专用`、`*总部` |
| `em_lhb_detailsell_20260916.json` | 东财 `RPT_BILLBOARD_DAILYDETAILSSELL`（2026-09-16） | 单票卖出席位。字段集与 BUY 完全一致（已核对） |
| `em_operatedept_trade_10026937.json` | 东财 `RPT_OPERATEDEPT_TRADE_DETAILS`，精确过滤 `OPERATEDEPT_CODE=10026937` | 席位档案：某营业部近 N 日出现过的股票/方向/原因 |
| `em_like_unsupported.json` | 同上，`filter=(OPERATEDEPT_NAME like '%紫阳东路%')` | **反例**：`code=9501`、`msg="不支持like查询"` |
| `em_bucket_name_empty.json` | 同上，`filter=(OPERATEDEPT_NAME="机构专用")` | **反例**：`code=9201`、`msg="返回数据为空"`（桶只存在于明细行） |

## 连续交易日窗口（2026-09-18 补，Y0-10）

2026-09-18 补齐**五个连续交易日**的真实响应：**2026-09-11(五) / 09-14(一) / 09-15(二) / 09-16(三) / 09-17(四)**，
每日三份报告（`RPT_DAILYBILLBOARD_DETAILS` details、`RPT_BILLBOARD_DAILYDETAILSBUY` buy、
`RPT_BILLBOARD_DAILYDETAILSSELL` sell），共 15 个文件。逐文件 sha256 / bytes / declared_count / pages /
rows / has_D1 见 **`em_lhb_multi_manifest.json`**（captured_at 2026-09-18）。

| 交易日 | details | buy | sell | 星期 |
| --- | --- | --- | --- | --- |
| 2026-09-11 | 68 | 340 | 340 | 五 |
| 2026-09-14 | 84 | 420 | 420 | 一 |
| 2026-09-15 | 73 | 365 | 365 | 二 |
| 2026-09-16 | 71 | 365 | 365 | 三 |
| 2026-09-17 | 62 | 310 | 310 | 四 |

**这组夹具的用途**：09-11 与 09-14 之间**只隔周末**，且 09-11..09-17 恰为五个连续交易日 →
正对路线图 §10 验收矩阵首行「**周五买榜、周一卖榜；中间仅休市**」，也是 `adjacent_buy_sell` /
`consecutive_buy` / `repeated_seat_presence` 三类事实端到端核验的唯一真实输入。
`has_D1` 显示 09-11/14/15/16 的 details 已带 D1 值，**09-17 为 0**（披露当日 D1 尚未生成）——这是
「未到期 ≠ 缺失 ≠ 0」的现成真实样本。

## 交易日历核验夹具（2026-09-18 补，Y0-07）

| 文件 | 来源 | 用途 |
| --- | --- | --- |
| `em_valuedays_000001_20240102_20260917.json` | `RPT_VALUEANALYSIS_DET`，`filter=(SECURITY_CODE="000001")(TRADE_DATE>=...)(TRADE_DATE<=...)` | 全区间 `TRADE_DATE` 序列 = 真实交易日序列 |
| `em_valuedays_600519_20240102_20260917.json` | 同上，600519 | 三只证券**交集**即可信日历（规避停牌） |
| `em_valuedays_601398_20240102_20260917.json` | 同上，601398 | 各 64,611 B；三份一致 → 2024-01-02..2026-09-17 共 **658 个交易日** |

判据：该报表**每个交易日**返回全市场估值横截面，**非交易日**返回 `success=false` + 返回数据为空。
2026-09-18 联网探针 `.runtime-local/y0-probes/y0_07_calendar_verify.py` 对 2025/2026 六大假期做
29 项对照 → **mismatches=0**。实现见 `src/investment_steward_core/trade_calendar.py`（77 例测试）。

## 文件清单与完整性（sha256，2026-09-17 由脚本对当前字节计算）

| 文件 | sha256 | 行数/页数（payload 内声明） |
| --- | --- | --- |
| `cffex_ccpm_IF_20260916.xml` | `08e10361…ed7ec` | 80 条 × datatypeid 0/1/2 |
| `cffex_ccpm_IH_20260916.xml` | `30b98545…62faea` | 同上 |
| `cffex_index_20260916.xml` | `f464214a…bd2bc` | — |
| `em_lhb_details_20260916.json` | `75cc9573…2544fc` | rows=71, count=71, pages=1 |
| `em_lhb_detailbuy_20260916.json` | `16b8f670…ebf6b6` | rows=365, count=365, pages=1 |
| `em_lhb_detailsell_20260916.json` | `4c1da11c…c95833` | rows=365, count=365, pages=1 |
| `em_operatedept_trade_10026937.json` | `b99b2f5f…e3039f4` | rows=50, count=1116, **pages=23**（单页 50 行分页证据，Y2-07 缺口旁证） |
| `em_like_unsupported.json` | `31e88e0d…0497b55` | 反例：code=9501 |
| `em_bucket_name_empty.json` | `ea6528a2…9bbfb19` | 反例：code=9201 |

（表中哈希为截断显示，便于排版；完整哈希见下方折叠列表，两处均由同一脚本对当前字节计算。）

<details>
<summary>完整 sha256</summary>

```
cffex_ccpm_IF_20260916.xml          08e10361449c3308cff435f97aed5428ce03714e6acfe22b7289ed539b5ed7ec
cffex_ccpm_IH_20260916.xml          30b98545114861cf1d92759056a4c7b7daa126937e52b36c73bc63f42062faea
cffex_index_20260916.xml            f464214ac6a62d70e4d0bd174e8a82b9e0b791b3a26a281b607e24e9fecbd2bc
em_bucket_name_empty.json           ea6528a2204b61a7ee34683a73ca00f8afdc86aadf3f765d4dae39bbfb19dbe7
em_lhb_detailbuy_20260916.json      16b8f6707be49fac27356b5c3a54d5e032d94362cc77f792cec661c756ebf6b6
em_lhb_details_20260916.json        75cc9573759924e34bef6b3733271341296845dcebaaffca4fc62b27882544fc
em_lhb_detailsell_20260916.json     4c1da11c0588aaa4ed9a07d24fa91b0ecd355f1864d4a4ec2e1b3c3569c95833
em_like_unsupported.json            31e88e0d7b0c4413f3e65b401cf321227ab134a7d9f8b71853bd8fc104497b55
em_operatedept_trade_10026937.json  b99b2f5fa55cbf4ad6485b5252c9ae77088a8a1986b079f680c839db6e3039f4
```

</details>

> 分页事实（Y2-07 旁证）：`em_operatedept_trade_10026937.json` 声明 `count=1116, pages=23`，而当前 `lhb_feed._fetch_rows` 仅取单页 → 确有缺页风险，分页实现列入 Y2-07。
> 连续交易日真实样本：**已补（2026-09-18）**，见下节「连续交易日窗口」——2026-09-11..09-17 五个连续交易日、每日三报告共 15 文件。~~未补（离线无法联网抓取多日响应）~~

## 为什么这些反例必须在夹具里

1. **`index.xml` 不是只有股指。** 里面同时有 MO 期权和国债期货。不做 `productid` 白名单过滤，
   就会把期权行当股指写进方向研判证据——这是「看起来有数据、其实口径错了」的典型。
2. **`机构专用` / `*总部` 是桶，不是营业部。** 它们出现在明细行，但按名单独查营业部维度表返回空
   （`em_bucket_name_empty.json` 就是证据）。游资命中必须排除它们。
3. **`like` 查询被数据源拒绝。** 席位档案只能走代码或精确名，「不做全市场搜索」不是取舍而是被强制的。
4. **`EXPLAIN`/`RISE_PROBABILITY_3DAY` 夹带数据商自己的统计。** 如
   `"3家机构买入，成功率28.87%"`。这些**可以留原文说明，绝不解析成我方字段、绝不进排序键**。

## 规则

- 联网只作手动验收；`pytest` 必须能在断网下跑完全部 P1/P2 用例。
- 夹具缺失或解码失败时测试应当**失败**，不允许静默跳过——不然「没数据」会被误读成「没问题」。
