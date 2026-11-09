import pytest

from dart_rag.retrieval import tokenize
from dart_rag.tools import ToolInputError, fmt_krw


def test_resolve_by_name_alias_and_code(tools):
    assert tools.resolve("가나반도체")["corp_code"] == "00000001"
    assert tools.resolve("가나")["corp_code"] == "00000001"
    assert tools.resolve("100002")["corp_name"] == "다라은행"
    with pytest.raises(ToolInputError):
        tools.resolve("없는회사")


def test_financials_pick_income_statement_not_sce(tools):
    fin = tools.get_financials("가나반도체", 2025)
    assert fin["items"]["net_income"]["value_krw"] == 150_000_000_000
    assert fin["items"]["revenue"]["display"] == "1.00조원"
    assert fin["source"]["url"].endswith("rcpNo=20260301000001")


def test_ratios(tools):
    r = tools.financial_ratios("가나반도체", 2025)["ratios"]
    assert r["operating_margin"]["value"] == 20.0
    assert r["debt_ratio"]["value"] == 50.0
    # ROE = 지배주주 순이익 1,400억 / 지배주주 자본 평균((1.9조 + 1.52조)/2)
    assert r["roe"]["value"] == round(140_000_000_000 / ((1_900_000_000_000 + 1_520_000_000_000) / 2) * 100, 2)
    assert r["revenue_growth"]["value"] == 25.0


def test_bank_without_revenue_returns_null_not_error(tools):
    fin = tools.get_financials("다라은행", 2025)
    assert fin["items"]["revenue"]["value_krw"] is None
    assert tools.financial_ratios("다라은행", 2025)["ratios"]["operating_margin"]["value"] is None


def test_missing_year_message_lists_available(tools):
    with pytest.raises(ToolInputError, match=r"\[2025\]"):
        tools.get_financials("가나반도체", 2019)


def test_compare(tools):
    out = tools.compare_companies(["가나반도체", "다라은행"], 2025, ["net_income", "roe"])
    assert [r["company"] for r in out["rows"]] == ["가나반도체", "다라은행"]
    with pytest.raises(ToolInputError):
        tools.compare_companies(["가나"], 2025, ["eps"])


def test_price_history_and_valuation(tools):
    p = tools.price_history("가나", "2026-01-01", "2026-01-31")
    assert p["return_pct"] == 20.0 and p["high"] == 121_000 and p["period"]["trading_days"] == 3
    v = tools.valuation("가나", "2026-01-05")
    assert v["financial_year"] == 2025
    assert v["market_cap_krw"] == 110_000 * 10_000_000
    assert v["per"] == round(1_100_000_000_000 / 140_000_000_000, 2)


def test_search_filters_company_and_section(tools):
    out = tools.search_filings("고대역폭 메모리 HBM 판매", top_k=3)
    assert out["results"][0]["company"] == "가나반도체"
    assert out["results"][0]["source"]["rcept_no"] == "20260301000001"
    bank = tools.search_filings("메모리", companies=["다라은행"])
    assert {r["company"] for r in bank["results"]} <= {"다라은행"}
    mdna = tools.search_filings("금리", section="mdna")
    assert all("경영진단" in r["location"] for r in mdna["results"])


def test_search_modes_report_ranks(tools):
    out = tools.search_filings("HBM", mode="bm25", top_k=2)
    assert set(out["results"][0]["ranks"]) == {"bm25"}
    with pytest.raises(ToolInputError):
        tools.search_filings("x", mode="fancy")


def test_read_chunk(tools):
    cid = tools.search_filings("DRAM NAND 비중", top_k=1)["results"][0]["chunk_id"]
    out = tools.read_chunk(cid)
    assert cid in [c["chunk_id"] for c in out["chunks"]]


def test_tokenize_and_fmt():
    assert "반도" in tokenize("반도체") and "hbm" in tokenize("HBM")
    assert fmt_krw(-300_000_000) == "-3억원"
    assert fmt_krw(None) is None
