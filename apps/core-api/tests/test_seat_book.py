"""席位观察名单（P0 A02 / P2 C03）夹具测试——**全程不联网**。

名单是静态 JSON，但它决定「谁算游资命中」这个**结论性判断**，所以校验必须进测试：
条目查不到、桶被当成营业部、代码重复，都要在测试里直接失败。
"""

from __future__ import annotations

import json

import pytest
from investment_steward_core import seat_book as sb


@pytest.fixture()
def watchlist() -> sb.Watchlist:
    return sb.load_watchlist()


# —— 加载与结构 ——


def test_default_watchlist_loads(watchlist: sb.Watchlist):
    assert watchlist.version
    assert len(watchlist.seats) >= 8
    assert watchlist.disclaimer
    assert watchlist.bucket_name_suffixes


def test_watchlist_primary_key_is_operatedept_code(watchlist: sb.Watchlist):
    """主键必须是代码：营业部更名/合并会让名字失效。"""
    codes = [item.operatedept_code for item in watchlist.seats]
    assert all(codes), "每条席位都必须有 operatedept_code"
    assert len(codes) == len(set(codes)), "operatedept_code 不得重复"
    for seat in watchlist.seats:
        assert seat.name.strip(), f"{seat.operatedept_code} 缺少 name"


def test_every_seat_name_differs_from_its_code(watchlist: sb.Watchlist):
    """名字是展示与核验用，不能退化成代码的副本。"""
    for seat in watchlist.seats:
        assert seat.name != seat.operatedept_code


def test_missing_file_raises_not_silently_empty(tmp_path):
    """名单缺失必须报错。静默返回空名单会让「今日无命中」与「名单没加载」无法区分。"""
    with pytest.raises(sb.WatchlistError, match="不存在"):
        sb.load_watchlist(tmp_path / "nope.json")


def test_malformed_payload_raises():
    with pytest.raises(sb.WatchlistError):
        sb.parse_watchlist({"seats": []})
    with pytest.raises(sb.WatchlistError):
        sb.parse_watchlist({"seats": [{"name": "缺代码营业部"}]})
    with pytest.raises(sb.WatchlistError):
        sb.parse_watchlist({"seats": [{"operatedept_code": "1", "name": ""}]})
    with pytest.raises(sb.WatchlistError):
        sb.parse_watchlist(
            {
                "seats": [
                    {"operatedept_code": "1", "name": "甲营业部"},
                    {"operatedept_code": "1", "name": "乙营业部"},
                ]
            }
        )


def test_bucket_cannot_be_smuggled_into_seats():
    """桶放进 seats 必须被拒绝，否则它会被当成游资命中。"""
    with pytest.raises(sb.WatchlistError, match="kind 必须是 seat"):
        sb.parse_watchlist(
            {"seats": [{"operatedept_code": "1", "name": "机构专用", "kind": "bucket"}]}
        )


# —— 观察名单里那两条「方案写错了」的席位（逐字核验的落点） ——


def test_ziyang_donglu_is_the_wuhan_seat(watchlist: sb.Watchlist):
    """方案原文「国盛证券宁波紫阳东路」查无此名；真实在榜的是国泰海通武汉紫阳东路。"""
    seat = watchlist.by_code("10026937")
    assert seat is not None
    assert "武汉紫阳东路" in seat.name
    assert "国泰海通" in seat.name


def test_ningbo_sangtianlu_exists_as_its_own_seat(watchlist: sb.Watchlist):
    """「国盛 + 宁波」的真实席位是桑田路，必须单独存在。"""
    seat = watchlist.by_code("10456710")
    assert seat is not None
    assert "桑田路" in seat.name


def test_plan_document_wrong_name_is_absent(watchlist: sb.Watchlist):
    """把方案里那个拼接出来的名字钉在测试里：它不应出现在名单中。"""
    assert watchlist.by_name("国盛证券有限责任公司宁波紫阳东路证券营业部") is None


def test_seats_are_labelled_as_channels_not_accounts(watchlist: sb.Watchlist):
    """每条席位都要说明「通道名，非实名账户」。"""
    for seat in watchlist.seats:
        assert "非实名" in seat.note, f"{seat.operatedept_code} 未标注非实名"


def test_seats_record_last_seen_date(watchlist: sb.Watchlist):
    """活跃度差异极大，首版每条都要带最近一次上榜日期。"""
    for seat in watchlist.seats:
        assert seat.last_seen_trade_date, f"{seat.operatedept_code} 缺少最近一次上榜日期"


# —— 桶排除：本模块存在的首要理由 ——


@pytest.mark.parametrize("bucket_name", ["机构专用", "沪股通专用", "深股通专用"])
def test_buckets_never_match(watchlist: sb.Watchlist, bucket_name: str):
    result = sb.match_seat(watchlist, operatedept_name=bucket_name)
    assert result.matched is False
    assert "桶" in result.reason


@pytest.mark.parametrize(
    "headquarters",
    ["国泰海通证券股份有限公司总部", "华泰证券股份有限公司总部"],
)
def test_headquarters_suffix_never_matches(watchlist: sb.Watchlist, headquarters: str):
    result = sb.match_seat(watchlist, operatedept_name=headquarters)
    assert result.matched is False


def test_bucket_exclusion_wins_over_code_match(watchlist: sb.Watchlist):
    """即使有人给桶填了代码，也不得命中。"""
    result = sb.match_seat(watchlist, operatedept_code="10026937", operatedept_name="机构专用")
    assert result.matched is False


# —— 命中判定 ——


def test_hit_by_code(watchlist: sb.Watchlist):
    result = sb.match_seat(watchlist, operatedept_code="10026937")
    assert result.matched is True
    assert result.seat is not None
    assert "代码命中" in result.reason


def test_hit_by_exact_name_after_rename(watchlist: sb.Watchlist):
    """名字精确命中（代码缺失时兜底）。"""
    result = sb.match_seat(
        watchlist, operatedept_name="华鑫证券有限责任公司上海红宝石路证券营业部"
    )
    assert result.matched is True


def test_hit_by_alias(watchlist: sb.Watchlist):
    result = sb.match_seat(watchlist, operatedept_name="武汉紫阳东路")
    assert result.matched is True


def test_unknown_seat_does_not_match(watchlist: sb.Watchlist):
    result = sb.match_seat(watchlist, operatedept_name="某某证券某地某路证券营业部")
    assert result.matched is False
    assert result.reason == "不在观察名单"


def test_name_match_is_exact_not_fuzzy(watchlist: sb.Watchlist):
    """东财不支持 like 查询，自造模糊匹配会让「命中」变成猜测。"""
    result = sb.match_seat(watchlist, operatedept_name="武汉紫阳东路证券营业部")
    assert result.matched is False


def test_empty_inputs_do_not_match(watchlist: sb.Watchlist):
    assert sb.match_seat(watchlist, operatedept_code="", operatedept_name="").matched is False


# —— 对真实榜单夹具做整批判定 ——


def test_real_buy_rows_hit_only_real_seats(youzi_fixture_text):
    payload = json.loads(youzi_fixture_text("em_lhb_detailbuy_20260916.json"))
    rows = payload["result"]["data"]
    assert len(rows) == 365

    watchlist = sb.load_watchlist()
    hits = sb.hits_for_rows(watchlist, rows)
    assert hits, "2026-09-16 买榜应至少命中一条观察席位"

    for row, match in hits:
        name = row["OPERATEDEPT_NAME"]
        assert match.seat is not None
        assert not watchlist.is_bucket_name(name), f"桶被误判为命中：{name}"
        assert match.seat.operatedept_code == row["OPERATEDEPT_CODE"]


def test_real_buy_rows_contain_buckets_but_they_never_hit(youzi_fixture_text):
    """夹具里确实有 104 行桶；它们全部不得命中——否则上一个测试是空转。"""
    payload = json.loads(youzi_fixture_text("em_lhb_detailbuy_20260916.json"))
    rows = payload["result"]["data"]
    watchlist = sb.load_watchlist()

    bucket_rows = [r for r in rows if watchlist.is_bucket_name(r["OPERATEDEPT_NAME"])]
    assert len(bucket_rows) >= 100
    for row in bucket_rows:
        result = sb.match_seat(
            watchlist,
            operatedept_code=row["OPERATEDEPT_CODE"],
            operatedept_name=row["OPERATEDEPT_NAME"],
        )
        assert result.matched is False


def test_describe_watchlist_mentions_channel_and_activity(watchlist: sb.Watchlist):
    text = sb.describe_watchlist(watchlist)
    assert "通道名" in text
    assert "最近一次上榜" in text
    assert "不构成投资建议" in text


# ---------------------------------------------------------------------------
# P2 C04：个股研报 S6 摘要
# ---------------------------------------------------------------------------


@pytest.fixture()
def lhb_fixtures(youzi_fixture_text):
    """真实龙虎榜夹具：上榜列表 + 买卖两侧席位。"""
    import json as _json

    from investment_steward_core import lhb_feed as lf

    billboard = lf.parse_billboard_rows(
        _json.loads(youzi_fixture_text("em_lhb_details_20260916.json"))
    )
    seats = lf.parse_seat_rows(
        _json.loads(youzi_fixture_text("em_lhb_detailbuy_20260916.json")), lf.DIRECTION_BUY
    ) + lf.parse_seat_rows(
        _json.loads(youzi_fixture_text("em_lhb_detailsell_20260916.json")), lf.DIRECTION_SELL
    )
    return billboard, seats


def test_s6_summary_none_when_not_listed(watchlist: sb.Watchlist, lhb_fixtures):
    """未上榜必须返回 None：不生成空表，也不写「游资没进」这类负向编造。"""
    billboard, seats = lhb_fixtures
    assert sb.build_billboard_summary("999999", billboard, seats, watchlist) is None


def test_s6_summary_none_for_empty_code(watchlist: sb.Watchlist, lhb_fixtures):
    billboard, seats = lhb_fixtures
    assert sb.build_billboard_summary("", billboard, seats, watchlist) is None


def test_s6_summary_contains_reason_amounts_and_seats(watchlist: sb.Watchlist, lhb_fixtures):
    billboard, seats = lhb_fixtures
    text = sb.build_billboard_summary("000592", billboard, seats, watchlist)
    assert text is not None
    assert "平潭发展" in text
    assert "上榜原因" in text
    assert "日涨幅偏离值达到7%的前5只证券" in text
    assert "买入前 3 席位" in text
    assert "卖出前 3 席位" in text
    assert "武汉紫阳东路" in text
    assert "观察名单命中" in text
    assert sb.S6_LIMITATION in text


def test_s6_summary_marks_buckets_but_does_not_count_them_as_hits(
    watchlist: sb.Watchlist, lhb_fixtures
):
    """桶可以展示，但必须是【匿名汇总桶】且不计入命中数。"""
    billboard, seats = lhb_fixtures
    text = sb.build_billboard_summary("000592", billboard, seats, watchlist)
    assert text is not None
    assert "【匿名汇总桶】" in text
    # 000592 里只有紫阳东路一条真席位命中
    assert "观察名单命中 1 条" in text


def test_s6_summary_exposes_vendor_note_as_non_authoritative(watchlist: sb.Watchlist, lhb_fixtures):
    """数据商「成功率」只能作为原文标注出现，并明确写「非本产品口径」。"""
    billboard, seats = lhb_fixtures
    text = sb.build_billboard_summary("000592", billboard, seats, watchlist)
    assert text is not None
    assert "成功率" in text
    assert "非本产品口径" in text


def test_s6_summary_states_net_is_signed_across_both_tables(watchlist: sb.Watchlist, lhb_fixtures):
    """`NET` 是买卖轧差净额，两榜同义——写错会把净买入讲成净卖出。"""
    billboard, seats = lhb_fixtures
    text = sb.build_billboard_summary("000592", billboard, seats, watchlist)
    assert text is not None
    assert "买卖轧差净额" in text
    assert "同一席位可能同时出现在买卖两侧" in text
    # 深股通专用净买入 1.02 亿，绝不能写成「卖出净额 1.02 亿」
    assert "卖出净额 1.02" not in text


def test_s6_summary_orders_sell_side_by_sell_amount(watchlist: sb.Watchlist, lhb_fixtures):
    """卖出席位按 SELL 金额排序，而不是按 NET（否则净买入席位会排到前面）。"""
    billboard, seats = lhb_fixtures
    text = sb.build_billboard_summary("000592", billboard, seats, watchlist)
    assert text is not None
    sell_block = text.split("卖出前 3 席位：", 1)[1].split("\n- 观察名单", 1)[0]
    assert "华泰证券股份有限公司天津东丽开发区二纬路证券营业部" in sell_block
    assert sell_block.index("华泰证券") < sell_block.index("深股通专用")


def test_s6_summary_includes_lookback_wording(watchlist: sb.Watchlist, lhb_fixtures):
    billboard, seats = lhb_fixtures
    one_day = sb.build_billboard_summary("000592", billboard, seats, watchlist, lookback_days=1)
    five_day = sb.build_billboard_summary("000592", billboard, seats, watchlist, lookback_days=5)
    assert one_day is not None and five_day is not None
    assert "当日" in one_day
    assert "近 5 个交易日" in five_day


# —— 名单路径解析（打包形态回归，2026-09-17 真机缺陷）——
#
# 缺陷：`DEFAULT_WATCHLIST_PATH` 由 `__file__.parents[4]` 推导。PyInstaller onefile
# 冻结后 `__file__` 位于解包临时目录，`parents[4]` 指向 `%LOCALAPPDATA%`，打包版
# 找的是 `C:\Users\<user>\AppData\plugins\official\youzi-radar\seat_watchlist.json`
# （真机实测的报错原文），于是 `/youzi/watchlist` 直接 500，而源码/测试全绿。
# 口径与 `api/app.py::_registry_entries` 对齐：先看 STEWARD_PLUGIN_REGISTRY_DIR。


def test_watchlist_path_follows_registry_dir_env(tmp_path, monkeypatch):
    """设了插件目录覆盖点就必须用它——这正是打包形态的路径来源。"""
    registry = tmp_path / "resources" / "plugins"
    target = registry / "official" / "youzi-radar" / "seat_watchlist.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(sb._REPO_WATCHLIST_PATH.read_bytes())
    monkeypatch.setenv(sb.PLUGIN_REGISTRY_DIR_ENV, str(registry))

    assert sb.default_watchlist_path() == target
    watchlist = sb.load_watchlist()
    assert watchlist.seats, "覆盖点下的名单必须真正被加载"


def test_watchlist_path_falls_back_to_repo_plugins(monkeypatch):
    """非打包形态（无覆盖点）沿用仓库内 plugins/。"""
    monkeypatch.delenv(sb.PLUGIN_REGISTRY_DIR_ENV, raising=False)
    assert sb.default_watchlist_path() == sb._REPO_WATCHLIST_PATH
    assert sb.load_watchlist().seats


def test_missing_watchlist_under_registry_dir_raises_with_that_path(tmp_path, monkeypatch):
    """覆盖点下缺文件必须报出覆盖点路径——否则又会退回「路径猜错」的不可诊断状态。"""
    registry = tmp_path / "resources" / "plugins"
    registry.mkdir(parents=True)
    monkeypatch.setenv(sb.PLUGIN_REGISTRY_DIR_ENV, str(registry))

    with pytest.raises(sb.WatchlistError) as excinfo:
        sb.load_watchlist()
    assert str(registry) in str(excinfo.value)

