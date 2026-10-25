import pytest

from pipeline.dashboard import bind_host, leads_where, page_bounds


def test_dashboard_binds_loopback_only():
    assert bind_host("127.0.0.1") == "127.0.0.1"
    assert bind_host("localhost") == "127.0.0.1"
    with pytest.raises(SystemExit):
        bind_host("0.0.0.0")


def test_leads_filter_rejects_unknown_verdict():
    with pytest.raises(ValueError, match="verdict"):
        leads_where({"verdict": "publish"})


def test_unreviewed_filter_is_a_null_check():
    where, params = leads_where({"verdict": "unreviewed", "city": "Calgary"})
    assert "verdict IS NULL" in where
    assert "city = %s" in where
    assert params == ["Calgary"]


def test_page_bounds_are_capped():
    assert page_bounds({"limit": "5000", "offset": "-4"}) == (200, 0)
