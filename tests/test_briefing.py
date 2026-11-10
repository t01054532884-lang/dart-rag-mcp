import anyio
from mcp import Client

from dart_rag.briefing import check_numbers, extract_numbers, make_briefing, to_srt
from dart_rag.mcp_server import build_server


def test_extract_numbers():
    assert extract_numbers("매출액 1.00조원 · 전년 대비 25.0%, PER 7.1배, 2025년") == [
        "1.00조원", "25.0%", "7.1배", "2025년"]


def test_briefing_captions_use_only_tool_numbers(tools):
    b = make_briefing(tools, "가나", 2025, "2026-01-06")
    assert b["company"] == "가나반도체"
    assert b["number_check"]["ok"], b["number_check"]
    ids = [s["id"] for s in b["scenes"]]
    assert ids[0] == "intro" and ids[-1] == "source"
    assert {"revenue", "profit", "valuation"} <= set(ids)
    rev = next(s for s in b["scenes"] if s["id"] == "revenue")
    assert "1.00조원" in rev["caption"]
    for s in b["scenes"]:
        assert "no numbers" in s["visual_prompt"]
        assert not any(ch.isdigit() for ch in s["visual_prompt"])
    assert b["srt"].startswith("1\n00:00:00,000 --> 00:00:04,000\n")
    assert b["business_snippet"]["source"]["url"]


def test_bank_without_revenue_uses_net_income(tools):
    b = make_briefing(tools, "다라은행", 2025, "2026-01-06")
    ids = [s["id"] for s in b["scenes"]]
    assert "revenue" not in ids and "profit" in ids
    assert "당기순이익" in next(s for s in b["scenes"] if s["id"] == "profit")["caption"]
    assert b["number_check"]["ok"]


def test_changed_number_is_caught(tools):
    b = make_briefing(tools, "가나", 2025, "2026-01-06")
    script = "\n".join(s["caption"] for s in b["scenes"]).replace("1.00조원", "1.10조원")
    res = check_numbers(script, b["facts"], b["allowed_numbers"])
    assert not res["ok"] and res["unknown"] == ["1.10조원"]


def test_srt_times():
    srt = to_srt([{"caption": "a", "seconds": 1.5}, {"caption": "b", "seconds": 2.0}])
    assert "00:00:01,500 --> 00:00:03,500" in srt


def test_briefing_mcp_tools(tools):
    server = build_server(tools)

    async def run():
        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"make_briefing", "check_briefing_numbers"} <= names
            res = await client.call_tool("check_briefing_numbers", {
                "company": "가나", "year": 2025, "date": "2026-01-06", "script": "매출액 9.99조원"})
            assert not res.is_error
            assert res.structured_content["unknown"] == ["9.99조원"]

    anyio.run(run)
