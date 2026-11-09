from __future__ import annotations

from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from dart_rag.chunking import chunk_text
from dart_rag.embedding import HashEmbedder
from dart_rag.parsing import extract_sections
from dart_rag.retrieval import HybridIndex
from dart_rag.store import Store
from dart_rag.tools import FinTools

FIXTURES = Path(__file__).parent / "fixtures"


def fin_rows(rcept_no, revenue, op, ni, ni_parent, assets, liab, equity, eq_parent, prev_scale=0.8):
    def row(sj, aid, nm, v):
        return {"sj_div": sj, "account_id": aid, "account_nm": nm, "ord": "1",
                "thstrm_amount": f"{v:,}", "frmtrm_amount": f"{int(v * prev_scale):,}",
                "currency": "KRW", "rcept_no": rcept_no}
    return [
        row("BS", "ifrs-full_Assets", "자산총계", assets),
        row("BS", "ifrs-full_Liabilities", "부채총계", liab),
        row("BS", "ifrs-full_Equity", "자본총계", equity),
        row("BS", "ifrs-full_EquityAttributableToOwnersOfParent", "지배기업 소유주지분", eq_parent),
        # 자본변동표의 당기순이익이 손익계산서보다 앞에 있어도 골라지면 안 된다
        row("SCE", "ifrs-full_ProfitLoss", "당기순이익", 1),
        row("CIS", "ifrs-full_Revenue", "매출액", revenue),
        row("CIS", "dart_OperatingIncomeLoss", "영업이익", op),
        row("CIS", "ifrs-full_ProfitLoss", "당기순이익", ni),
        row("CIS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", ni_parent),
    ]


@pytest.fixture()
def store():
    s = Store(":memory:")
    s.upsert_company("00000001", "가나반도체", "100001", "가나")
    s.upsert_company("00000002", "다라은행", "100002", None)
    s.upsert_filing("20260301000001", "00000001", "사업보고서 (2025.12)", "20260301", 2025)
    s.upsert_filing("20260301000002", "00000002", "사업보고서 (2025.12)", "20260301", 2025)

    xml = (FIXTURES / "sample_report.xml").read_text(encoding="utf-8")
    items = []
    for sec in extract_sections(xml):
        for i, t in enumerate(chunk_text(sec.text, 300, 50)):
            items.append((sec.section, sec.title, sec.subtitle, i, t))
    s.replace_chunks("20260301000001", "00000001", items)
    s.replace_chunks("20260301000002", "00000002", [
        ("business", "II. 사업의 내용", "1. 사업의 개요", 0, "당행은 기업 대출과 가계 대출, 예금 상품을 운영합니다."),
        ("mdna", "IV. 이사의 경영진단 및 분석의견", "2. 개요", 0, "순이자마진은 금리 하락으로 줄었습니다."),
    ])

    s.replace_financials("00000001", 2025, "CFS",
                         fin_rows("20260301000001", 1_000_000_000_000, 200_000_000_000, 150_000_000_000,
                                  140_000_000_000, 3_000_000_000_000, 1_000_000_000_000,
                                  2_000_000_000_000, 1_900_000_000_000))
    # 은행: 매출액·영업이익 계정이 없는 경우
    bank = [r for r in fin_rows("20260301000002", 1, 1, 50_000_000_000, 48_000_000_000,
                                10_000_000_000_000, 9_000_000_000_000, 1_000_000_000_000, 950_000_000_000)
            if r["account_id"] not in ("ifrs-full_Revenue", "dart_OperatingIncomeLoss")]
    s.replace_financials("00000002", 2025, "CFS", bank)
    s.replace_share_counts("00000001", 2025, [
        {"se": "보통주", "istc_totqy": "11,000,000", "tesstk_co": "1,000,000", "distb_stock_co": "10,000,000"},
        {"se": "합계", "istc_totqy": "11,000,000", "tesstk_co": "1,000,000", "distb_stock_co": "10,000,000"},
    ])
    s.upsert_prices("100001", [
        ("2026-01-02", 100_000, 101_000, 99_000, 100_000, 1000),
        ("2026-01-05", 100_000, 112_000, 100_000, 110_000, 3000),
        ("2026-01-06", 110_000, 121_000, 108_000, 120_000, 2000),
    ])
    yield s
    s.close()


@pytest.fixture()
def tools(store):
    index = HybridIndex(store, HashEmbedder(), QdrantClient(":memory:"))
    index.build()
    return FinTools(store, index)
