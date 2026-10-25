"""Full-text source links must belong to announcements whose body was actually used."""
from types import SimpleNamespace

from investment_steward_core.api.app import _announcement_body_segment
from investment_steward_core.news_evidence import AnnouncementBody


def test_body_link_fallback_uses_successful_notice_identity():
    items = [SimpleNamespace(art_code="failed", title="未取到正文", notice_date_raw="2026-09-01",
                             url="https://example.test/failed-notice"),
             SimpleNamespace(art_code="used", title="已取到正文", notice_date_raw="2026-09-02",
                             url="https://example.test/used-notice")]
    text, codes, url = _announcement_body_segment(items, {
        "used": AnnouncementBody(art_code="used", content="实际正文", attach_url=None),
    })
    assert codes == ["used"] and "实际正文" in text
    assert url == items[1].url, "The first title-only notice cannot supply the full-text source link"


def test_original_pdf_keeps_priority_over_notice_page():
    item = SimpleNamespace(art_code="used", title="正文", notice_date_raw="2026-09-02",
                           url="https://example.test/used-notice")
    _, _, url = _announcement_body_segment([item], {
        "used": AnnouncementBody(art_code="used", content="实际正文", attach_url="https://example.test/original.pdf"),
    })
    assert url == "https://example.test/original.pdf"
