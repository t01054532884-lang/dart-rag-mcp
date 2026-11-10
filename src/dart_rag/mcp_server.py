"""MCP 서버: FinTools를 MCP 도구로 연다 (stdio).

Claude Desktop / Claude Code 등 MCP 클라이언트에 붙이면 API 키 없이 Claude가 이 도구들을 불러
공시 원문 검색 + 재무 수치 조회를 조합해 답한다.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .briefing import check_numbers
from .briefing import make_briefing as make_briefing_fn
from .tools import METRICS, RATIOS, FinTools, ToolInputError

INSTRUCTIONS = """\
한국 상장사 30곳의 DART 사업보고서 원문(사업의 내용, 경영진단 및 분석의견), 재무제표, 주가를 조회하는 도구 모음.
- 숫자(매출, 이익, 비율, PER/PBR)는 반드시 get_financials / financial_ratios / valuation / compare_companies 결과를 쓰고 직접 계산하거나 추정하지 않는다.
- 사업 내용·전략·위험 같은 서술형 질문은 search_filings로 원문을 찾고, 필요하면 read_chunk로 앞뒤 문맥을 읽는다.
- 답에는 결과의 source.url(DART 원문 링크)을 출처로 붙인다. 도구 결과에 없는 내용은 모른다고 말한다.
"""

Company = Annotated[str, Field(description="회사명, 짧은 이름 또는 6자리 종목코드 (예: '삼성전자', '현대차', '005930')")]
Year = Annotated[int, Field(description="사업연도 (예: 2025)", ge=2015, le=2100)]


def _wrap(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except ToolInputError as e:
            raise ToolError(str(e)) from e

    return inner


def build_server(tools: FinTools) -> MCPServer:
    server = MCPServer("dart-rag", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool()
    @_wrap
    def list_companies() -> dict[str, Any]:
        """수집된 회사 목록과 각 회사의 재무제표 연도·사업보고서 이름을 돌려준다. 어떤 회사를 물을 수 있는지 모를 때 먼저 부른다."""
        return tools.list_companies()

    @server.tool()
    @_wrap
    def search_filings(
        query: Annotated[str, Field(description="찾을 내용 (자연어 질문이나 키워드)")],
        companies: Annotated[list[str] | None, Field(description="검색할 회사 목록. 비우면 전체 회사")] = None,
        section: Annotated[
            Literal["business", "mdna"] | None,
            Field(description="business=사업의 내용, mdna=이사의 경영진단 및 분석의견. 비우면 둘 다"),
        ] = None,
        top_k: Annotated[int, Field(ge=1, le=20, description="돌려받을 문단 수")] = 5,
    ) -> dict[str, Any]:
        """사업보고서 원문에서 질문과 관련된 문단을 하이브리드 검색(벡터 + BM25 키워드, RRF 결합)으로 찾는다.
        사업 구조, 주요 제품, 시장 전망, 경영진 분석, 위험 요인 같은 서술형 질문에 쓴다. 숫자는 재무 도구로 확인한다."""
        return tools.search_filings(query, companies, section, top_k)

    @server.tool()
    @_wrap
    def read_chunk(
        chunk_id: Annotated[int, Field(description="search_filings 결과의 chunk_id")],
        neighbors: Annotated[int, Field(ge=0, le=3, description="앞뒤로 더 읽을 문단 수")] = 1,
    ) -> dict[str, Any]:
        """검색으로 찾은 문단의 앞뒤 문맥을 같은 소제목 안에서 더 읽는다."""
        return tools.read_chunk(chunk_id, neighbors)

    @server.tool()
    @_wrap
    def get_financials(
        company: Company,
        year: Year,
        fs_div: Annotated[Literal["CFS", "OFS"], Field(description="CFS 연결(기본), OFS 별도")] = "CFS",
    ) -> dict[str, Any]:
        """사업보고서 재무제표의 핵심 계정(매출액, 영업이익, 당기순이익, 지배주주 순이익, 자산·부채·자본총계,
        영업활동현금흐름)을 원 단위로 돌려준다. 전년도 값도 함께 준다."""
        return tools.get_financials(company, year, fs_div)

    @server.tool()
    @_wrap
    def financial_ratios(company: Company, year: Year) -> dict[str, Any]:
        """영업이익률, 순이익률, 부채비율, ROE, ROA, 매출·영업이익 증가율을 재무제표 값으로 계산해 돌려준다(공식 포함)."""
        return tools.financial_ratios(company, year)

    @server.tool()
    @_wrap
    def compare_companies(
        companies: Annotated[list[str], Field(min_length=2, max_length=10, description="비교할 회사 2~10곳")],
        year: Year,
        metrics: Annotated[
            list[str] | None,
            Field(description=f"비교 지표. 쓸 수 있는 값: {', '.join(list(METRICS) + list(RATIOS))}"),
        ] = None,
    ) -> dict[str, Any]:
        """여러 회사의 재무 지표를 한 표로 비교한다."""
        return tools.compare_companies(companies, year, metrics)

    @server.tool()
    @_wrap
    def price_history(
        company: Company,
        start: Annotated[str, Field(description="시작일 YYYY-MM-DD")],
        end: Annotated[str, Field(description="종료일 YYYY-MM-DD")],
    ) -> dict[str, Any]:
        """기간 수익률, 최고·최저가, 평균 거래량, 마지막 10거래일 종가를 돌려준다."""
        return tools.price_history(company, start, end)

    @server.tool()
    @_wrap
    def valuation(
        company: Company,
        date: Annotated[str, Field(description="기준일 YYYY-MM-DD (그날 또는 직전 거래일 종가 사용)")],
        year: Annotated[int | None, Field(description="PER·PBR에 쓸 사업연도. 비우면 기준일 직전 연도")] = None,
    ) -> dict[str, Any]:
        """기준일 종가와 사업보고서 주식수로 시가총액, 후행 PER, PBR을 계산한다."""
        return tools.valuation(company, date, year)

    @server.tool()
    @_wrap
    def make_briefing(
        company: Company,
        year: Year,
        date: Annotated[str, Field(description="시가총액·PER 기준일 YYYY-MM-DD")],
    ) -> dict[str, Any]:
        """사업보고서 숫자로 30초 안팎 숏폼 브리핑 대본을 만든다. 장면별 자막(숫자는 도구 값만), SRT,
        영상 생성용 프롬프트(글자·숫자·로고 없는 배경 영상), 근거 facts와 DART 링크를 돌려준다.
        영상 생성 도구에는 scenes[].visual_prompt만 넘기고, 숫자는 자막으로만 넣는다."""
        return make_briefing_fn(tools, company, year, date)

    @server.tool()
    @_wrap
    def check_briefing_numbers(
        company: Company,
        year: Year,
        date: Annotated[str, Field(description="make_briefing에 쓴 기준일")],
        script: Annotated[str, Field(description="고쳐 쓴 자막·내레이션 전체")],
    ) -> dict[str, Any]:
        """대본에 들어간 숫자가 모두 make_briefing의 facts(도구 값)나 연도·기준일에 있는지 검사한다.
        unknown이 비어 있지 않으면 그 숫자를 지우거나 facts 값으로 바꾼다."""
        b = make_briefing_fn(tools, company, year, date)
        return check_numbers(script, b["facts"], b["allowed_numbers"])

    return server


def load_tools() -> FinTools:
    from qdrant_client import QdrantClient

    from .config import get_settings
    from .embedding import FastEmbedder
    from .retrieval import HybridIndex
    from .store import Store

    s = get_settings()
    if not s.db_path.exists():
        raise SystemExit(f"{s.db_path}가 없습니다. 먼저 `python -m dart_rag.ingest all --year 2025`를 실행하세요.")
    store = Store(s.db_path)
    index = None
    if s.qdrant_path.exists():
        index = HybridIndex(store, FastEmbedder(s.embed_model), QdrantClient(path=str(s.qdrant_path)),
                            bm25_weight=s.hybrid_bm25_weight)
    return FinTools(store, index)


def main() -> None:
    build_server(load_tools()).run()


if __name__ == "__main__":
    main()
