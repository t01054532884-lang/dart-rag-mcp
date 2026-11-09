"""에이전트·MCP 서버가 부르는 금융 도구. 모든 결과에 DART 접수번호와 원문 링크를 붙인다.

숫자는 저장된 DART 재무제표 값을 그대로 쓰고, 비율은 그 값으로만 계산한다.
LLM이 숫자를 지어내지 않게 하려고 계산은 전부 여기서 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .dart_client import dart_viewer_url
from .retrieval import HybridIndex, chunk_header
from .store import Store


class ToolInputError(ValueError):
    """도구 입력이 잘못됐을 때. 메시지는 모델이 읽고 고칠 수 있게 쓴다."""


@dataclass(frozen=True)
class Metric:
    label: str
    sj_divs: tuple[str, ...]
    account_ids: tuple[str, ...]
    names: tuple[str, ...]


METRICS: dict[str, Metric] = {
    "revenue": Metric("매출액", ("IS", "CIS"), ("ifrs-full_Revenue",), ("매출액", "수익(매출액)", "영업수익", "매출")),
    "operating_income": Metric(
        "영업이익", ("IS", "CIS"), ("dart_OperatingIncomeLoss",), ("영업이익", "영업이익(손실)")
    ),
    "net_income": Metric("당기순이익", ("IS", "CIS"), ("ifrs-full_ProfitLoss",), ("당기순이익", "당기순이익(손실)")),
    "net_income_parent": Metric(
        "지배주주 순이익", ("IS", "CIS"), ("ifrs-full_ProfitLossAttributableToOwnersOfParent",), ()
    ),
    "total_assets": Metric("자산총계", ("BS",), ("ifrs-full_Assets",), ("자산총계",)),
    "total_liabilities": Metric("부채총계", ("BS",), ("ifrs-full_Liabilities",), ("부채총계",)),
    "total_equity": Metric("자본총계", ("BS",), ("ifrs-full_Equity",), ("자본총계",)),
    "equity_parent": Metric("지배주주 자본", ("BS",), ("ifrs-full_EquityAttributableToOwnersOfParent",), ()),
    "operating_cash_flow": Metric(
        "영업활동현금흐름", ("CF",), ("ifrs-full_CashFlowsFromUsedInOperatingActivities",), ("영업활동현금흐름",)
    ),
}

RATIOS = {
    "operating_margin": "영업이익률(%)",
    "net_margin": "순이익률(%)",
    "debt_ratio": "부채비율(%)",
    "roe": "ROE(%)",
    "roa": "ROA(%)",
    "revenue_growth": "매출 증가율(%)",
    "operating_income_growth": "영업이익 증가율(%)",
}


def fmt_krw(value: int | float | None) -> str | None:
    """사람이 읽는 금액 표기 (조/억 원)."""
    if value is None:
        return None
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e12:
        return f"{sign}{v / 1e12:,.2f}조원"
    if v >= 1e8:
        return f"{sign}{v / 1e8:,.0f}억원"
    return f"{sign}{v:,.0f}원"


def _pct(num: float | None, den: float | None) -> float | None:
    if num is None or den in (None, 0):
        return None
    return round(num / den * 100, 2)


def _norm(name: str) -> str:
    name = re.sub(r"\(주\)|주식회사|㈜", "", name)
    return re.sub(r"\s+", "", name).lower()


class FinTools:
    def __init__(self, store: Store, index: HybridIndex | None):
        self.store = store
        self.index = index

    # ------------------------------------------------------------ 회사 찾기
    def resolve(self, company: str) -> dict[str, Any]:
        q = _norm(company)
        rows = self.store.companies()
        if not rows:
            raise ToolInputError("저장된 회사가 없습니다. 먼저 수집(ingest)을 실행해야 합니다.")
        for key in ("stock_code", "corp_code"):
            for r in rows:
                if r[key] == company.strip():
                    return r
        exact = [r for r in rows if q in (_norm(r["corp_name"]), _norm(r.get("alias") or ""))]
        if len(exact) == 1:
            return exact[0]
        partial = [r for r in rows if q and (q in _norm(r["corp_name"]) or q in _norm(r.get("alias") or ""))]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(r["corp_name"] for r in (partial or rows)[:40])
        if partial:
            raise ToolInputError(f"'{company}'에 맞는 회사가 여러 곳입니다: {names}. 하나를 골라 다시 호출하세요.")
        raise ToolInputError(f"'{company}'는 수집 대상에 없습니다. 가능한 회사: {names}")

    def list_companies(self) -> dict[str, Any]:
        out = []
        for r in self.store.companies():
            out.append(
                {
                    "corp_name": r["corp_name"],
                    "stock_code": r["stock_code"],
                    "financial_years": self.store.available_years(r["corp_code"]),
                    "annual_reports": [f["report_nm"] for f in self.store.filings(r["corp_code"])],
                }
            )
        return {"count": len(out), "companies": out}

    # ------------------------------------------------------------ 재무제표
    def _pick(self, rows: list[dict[str, Any]], metric: Metric) -> dict[str, Any] | None:
        for sj in metric.sj_divs:
            for r in rows:
                if r["sj_div"] == sj and r["account_id"] in metric.account_ids:
                    return r
        for sj in metric.sj_divs:
            for r in rows:
                if r["sj_div"] == sj and r["account_nm"].replace(" ", "") in metric.names:
                    return r
        return None

    def _statement(self, corp_code: str, year: int, fs_div: str) -> tuple[list[dict[str, Any]], str]:
        rows = self.store.financial_rows(corp_code, year, fs_div)
        if rows:
            return rows, fs_div
        other = "OFS" if fs_div == "CFS" else "CFS"
        rows = self.store.financial_rows(corp_code, year, other)
        return rows, other

    def _values(self, company: str, year: int, fs_div: str) -> tuple[dict[str, Any], dict[str, Any]]:
        corp = self.resolve(company)
        rows, used = self._statement(corp["corp_code"], year, fs_div)
        if not rows:
            years = self.store.available_years(corp["corp_code"])
            raise ToolInputError(f"{corp['corp_name']} {year}년 재무제표가 없습니다. 있는 연도: {years}")
        vals: dict[str, Any] = {"_fs_div": used}
        for key, metric in METRICS.items():
            r = self._pick(rows, metric)
            vals[key] = None if r is None else r["thstrm_amount"]
            vals[f"{key}_prev"] = None if r is None else r["frmtrm_amount"]
            if r is not None and r.get("rcept_no"):
                vals["_rcept_no"] = r["rcept_no"]
        return corp, vals

    def _source(self, rcept_no: str | None) -> dict[str, Any] | None:
        if not rcept_no:
            return None
        return {"rcept_no": rcept_no, "url": dart_viewer_url(rcept_no)}

    def get_financials(self, company: str, year: int, fs_div: str = "CFS") -> dict[str, Any]:
        corp, vals = self._values(company, year, fs_div)
        items = {
            key: {"label": m.label, "value_krw": vals[key], "display": fmt_krw(vals[key]),
                  "prev_year_krw": vals[f"{key}_prev"]}
            for key, m in METRICS.items()
        }
        return {
            "company": corp["corp_name"],
            "stock_code": corp["stock_code"],
            "year": year,
            "statement": "연결" if vals["_fs_div"] == "CFS" else "별도",
            "items": items,
            "note": "값이 null이면 해당 계정이 이 회사 재무제표에 없음(예: 금융회사는 매출액 대신 영업수익 구조).",
            "source": self._source(vals.get("_rcept_no")),
        }

    def financial_ratios(self, company: str, year: int, fs_div: str = "CFS") -> dict[str, Any]:
        corp, v = self._values(company, year, fs_div)
        ni_parent = v["net_income_parent"] if v["net_income_parent"] is not None else v["net_income"]
        eq_now = v["equity_parent"] if v["equity_parent"] is not None else v["total_equity"]
        eq_prev = v["equity_parent_prev"] if v["equity_parent"] is not None else v["total_equity_prev"]
        avg_eq = (eq_now + eq_prev) / 2 if eq_now is not None and eq_prev is not None else eq_now
        avg_assets = (
            (v["total_assets"] + v["total_assets_prev"]) / 2
            if v["total_assets"] is not None and v["total_assets_prev"] is not None
            else v["total_assets"]
        )

        def growth(key: str) -> float | None:
            now, prev = v[key], v[f"{key}_prev"]
            if now is None or prev in (None, 0):
                return None
            return round((now - prev) / abs(prev) * 100, 2)

        ratios = {
            "operating_margin": _pct(v["operating_income"], v["revenue"]),
            "net_margin": _pct(v["net_income"], v["revenue"]),
            "debt_ratio": _pct(v["total_liabilities"], v["total_equity"]),
            "roe": _pct(ni_parent, avg_eq),
            "roa": _pct(v["net_income"], avg_assets),
            "revenue_growth": growth("revenue"),
            "operating_income_growth": growth("operating_income"),
        }
        return {
            "company": corp["corp_name"],
            "year": year,
            "statement": "연결" if v["_fs_div"] == "CFS" else "별도",
            "ratios": {k: {"label": RATIOS[k], "value": val} for k, val in ratios.items()},
            "formulas": {
                "roe": "지배주주 순이익 / 지배주주 자본(기초·기말 평균)",
                "roa": "당기순이익 / 자산총계(기초·기말 평균)",
                "debt_ratio": "부채총계 / 자본총계",
            },
            "source": self._source(v.get("_rcept_no")),
        }

    def compare_companies(self, companies: list[str], year: int, metrics: list[str] | None = None) -> dict[str, Any]:
        metrics = metrics or ["revenue", "operating_income", "operating_margin", "roe", "debt_ratio"]
        unknown = [m for m in metrics if m not in METRICS and m not in RATIOS]
        if unknown:
            raise ToolInputError(f"모르는 지표: {unknown}. 쓸 수 있는 지표: {list(METRICS) + list(RATIOS)}")
        table = []
        for name in companies:
            try:
                fin = self.get_financials(name, year)
                rat = self.financial_ratios(name, year)
            except ToolInputError as e:
                table.append({"company": name, "error": str(e)})
                continue
            row: dict[str, Any] = {"company": fin["company"], "source": fin["source"]}
            for m in metrics:
                if m in METRICS:
                    row[m] = {"label": METRICS[m].label, "value_krw": fin["items"][m]["value_krw"],
                              "display": fin["items"][m]["display"]}
                else:
                    row[m] = rat["ratios"][m]
            table.append(row)
        return {"year": year, "metrics": metrics, "rows": table}

    # ------------------------------------------------------------ 주가·밸류에이션
    def price_history(self, company: str, start: str, end: str) -> dict[str, Any]:
        corp = self.resolve(company)
        rows = self.store.prices(corp["stock_code"], start, end)
        if not rows:
            raise ToolInputError(f"{corp['corp_name']}의 {start}~{end} 주가가 없습니다.")
        first, last = rows[0], rows[-1]
        return {
            "company": corp["corp_name"],
            "stock_code": corp["stock_code"],
            "period": {"start": first["date"], "end": last["date"], "trading_days": len(rows)},
            "close_start": first["close"],
            "close_end": last["close"],
            "return_pct": _pct(last["close"] - first["close"], first["close"]),
            "high": max(r["high"] for r in rows),
            "low": min(r["low"] for r in rows),
            "avg_volume": round(sum(r["volume"] for r in rows) / len(rows)),
            "last_10_days": [{"date": r["date"], "close": r["close"], "volume": r["volume"]} for r in rows[-10:]],
            "source": "KRX 일별 시세 (FinanceDataReader로 수집)",
        }

    def valuation(self, company: str, date: str, year: int | None = None) -> dict[str, Any]:
        corp = self.resolve(company)
        years = self.store.available_years(corp["corp_code"])
        if not years:
            raise ToolInputError(f"{corp['corp_name']} 재무제표가 없습니다.")
        if year is None:
            year = max((y for y in years if y < int(date[:4])), default=max(years))
        price = self.store.price_on_or_before(corp["stock_code"], date)
        if price is None:
            raise ToolInputError(f"{corp['corp_name']}의 {date} 이전 주가가 없습니다.")
        shares = {r["se"].replace(" ", ""): r for r in self.store.share_counts(corp["corp_code"], year)}
        common = shares.get("보통주")
        if not common or not common.get("floating"):
            raise ToolInputError(f"{corp['corp_name']} {year}년 보통주 유통주식수가 없습니다.")
        _, v = self._values(corp["corp_name"], year, "CFS")
        mcap = price["close"] * common["floating"]
        ni = v["net_income_parent"] if v["net_income_parent"] is not None else v["net_income"]
        eq = v["equity_parent"] if v["equity_parent"] is not None else v["total_equity"]
        return {
            "company": corp["corp_name"],
            "price_date": price["date"],
            "close": price["close"],
            "financial_year": year,
            "common_shares_floating": common["floating"],
            "market_cap_krw": mcap,
            "market_cap_display": fmt_krw(mcap),
            "per": round(mcap / ni, 2) if ni and ni > 0 else None,
            "pbr": round(mcap / eq, 2) if eq and eq > 0 else None,
            "note": (
                "시가총액 = 종가 × 보통주 유통주식수(자기주식 제외, 우선주 제외). "
                f"PER·PBR은 {year}년 사업보고서의 지배주주 순이익·자본 기준(후행). 순이익이 음수면 PER은 null."
            ),
            "source": self._source(v.get("_rcept_no")),
        }

    # ------------------------------------------------------------ 공시 원문 검색
    def search_filings(
        self,
        query: str,
        companies: list[str] | None = None,
        section: str | None = None,
        top_k: int = 5,
        mode: str = "hybrid",
    ) -> dict[str, Any]:
        if self.index is None:
            raise ToolInputError("벡터 색인이 없습니다. 먼저 `dart-rag-ingest index`를 실행해야 합니다.")
        if section not in (None, "business", "mdna"):
            raise ToolInputError("section은 business(사업의 내용) 또는 mdna(경영진단 및 분석의견)만 됩니다.")
        if mode not in ("hybrid", "vector", "bm25"):
            raise ToolInputError("mode는 hybrid, vector, bm25 중 하나입니다.")
        top_k = max(1, min(int(top_k), 20))
        corp_codes = [self.resolve(c)["corp_code"] for c in companies] if companies else None
        hits = self.index.search(query, corp_codes=corp_codes, top_k=top_k, mode=mode, section=section)  # type: ignore[arg-type]
        chunks = self.store.chunks_by_ids([h.chunk_id for h in hits])
        filings = {f["rcept_no"]: f for f in self.store.filings()}
        names = {c["corp_code"]: c["corp_name"] for c in self.store.companies()}
        results = []
        for h in hits:
            c = chunks[h.chunk_id]
            f = filings.get(c["rcept_no"], {})
            results.append(
                {
                    "chunk_id": c["chunk_id"],
                    "company": names.get(c["corp_code"]),
                    "location": chunk_header(names.get(c["corp_code"], ""), c),
                    "report": f.get("report_nm"),
                    "text": c["text"],
                    "score": round(h.score, 5),
                    "ranks": h.ranks,
                    "source": self._source(c["rcept_no"]),
                }
            )
        return {"query": query, "mode": mode, "results": results}

    def read_chunk(self, chunk_id: int, neighbors: int = 1) -> dict[str, Any]:
        base = self.store.chunks_by_ids([int(chunk_id)]).get(int(chunk_id))
        if base is None:
            raise ToolInputError(f"chunk_id {chunk_id}가 없습니다.")
        neighbors = max(0, min(int(neighbors), 3))
        ids = list(range(int(chunk_id) - neighbors, int(chunk_id) + neighbors + 1))
        rows = self.store.chunks_by_ids(ids)
        same = [rows[i] for i in ids if i in rows and rows[i]["rcept_no"] == base["rcept_no"]
                and rows[i]["subtitle"] == base["subtitle"]]
        corp = self.store.company_by_code(base["corp_code"]) or {}
        return {
            "company": corp.get("corp_name"),
            "location": chunk_header(corp.get("corp_name", ""), base),
            "chunks": [{"chunk_id": r["chunk_id"], "text": r["text"]} for r in same],
            "source": self._source(base["rcept_no"]),
        }
