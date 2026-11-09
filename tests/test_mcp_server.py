import anyio
from mcp import Client

from dart_rag.mcp_server import build_server


def test_tools_listed_and_callable(tools):
    server = build_server(tools)

    async def run():
        async with Client(server) as client:
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert {"search_filings", "get_financials", "financial_ratios", "valuation",
                    "compare_companies", "price_history", "read_chunk", "list_companies"} <= names
            res = await client.call_tool("get_financials", {"company": "가나", "year": 2025})
            assert not res.is_error
            assert res.structured_content["company"] == "가나반도체"
            bad = await client.call_tool("get_financials", {"company": "없는회사", "year": 2025})
            assert bad.is_error
            assert "수집 대상에 없습니다" in bad.content[0].text

    anyio.run(run)
