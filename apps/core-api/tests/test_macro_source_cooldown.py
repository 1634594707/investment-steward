import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from investment_steward_core import macro_feed


def test_okx_failure_enters_cooldown_without_repeating_request(monkeypatch):
    calls = []

    def unavailable(request, timeout):
        calls.append(request)
        raise urllib.error.URLError("isolated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(macro_feed, "_source_fail_until", {})
    before = datetime.now(UTC)
    with pytest.raises(RuntimeError, match="OKX"):
        macro_feed.fetch_okx_series("XAUT-USDT")
    assert macro_feed._source_fail_until["XAUT-USDT"] > before
    with pytest.raises(RuntimeError, match="OKX"):
        macro_feed.fetch_okx_series("XAUT-USDT")
    assert len(calls) == 1
