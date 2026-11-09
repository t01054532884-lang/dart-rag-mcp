"""재무 도구 값 ↔ 사업보고서 본문 숫자 대조.

사업보고서 '사업의 개요'에 회사가 직접 적은 2025년 연결 매출액(억원)을 손으로 옮겨 적고,
get_financials 도구가 재무제표 API 값으로 계산한 매출액과 비교한다.

    python eval/crosscheck_revenue.py   → docs/revenue_crosscheck.json
"""

import datetime as dt
import json

from dart_rag.config import ROOT, get_settings
from dart_rag.store import Store
from dart_rag.tools import FinTools

# 회사: 본문 표기 매출액(억원) — 각 회사 2025년 사업보고서 'II. 사업의 내용 > 1. 사업의 개요'
TEXT_REVENUE_EOK = {
    "SK하이닉스": 971468, "기아": 1141410, "셀트리온": 41625, "삼성바이오로직스": 45570,
    "LG에너지솔루션": 236718, "현대모비스": 611181, "크래프톤": 33266, "LG화학": 459322,
    "한국전력공사": 974294, "삼성SDI": 132667, "NC": 15069,
}


def main() -> None:
    tools = FinTools(Store(get_settings().db_path), None)
    rows = []
    for company, text_eok in TEXT_REVENUE_EOK.items():
        item = tools.get_financials(company, 2025)
        tool_eok = item["items"]["revenue"]["value_krw"] / 1e8
        rows.append({"company": company, "text_eok": text_eok, "tool_eok": round(tool_eok, 1),
                     "diff_pct": round((tool_eok - text_eok) / text_eok * 100, 4),
                     "source": item["source"]["url"]})
    out = {"n": len(rows), "max_abs_diff_pct": max(abs(r["diff_pct"]) for r in rows), "rows": rows,
           "run_at": dt.datetime.now().isoformat(timespec="seconds")}
    path = ROOT / "docs" / "revenue_crosscheck.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(rows)}개 회사, 최대 차이 {out['max_abs_diff_pct']}% → {path}")


if __name__ == "__main__":
    main()
