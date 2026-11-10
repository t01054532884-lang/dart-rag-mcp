"""공시 → 숏폼 브리핑 대본.

영상 생성 모델(예: Higgsfield MCP)은 화면에 숫자·글자를 그리면 틀리기 쉽다.
그래서 역할을 나눈다.
- 숫자: 이 모듈이 재무 도구 값으로 자막(SRT)을 만든다. LLM이 숫자를 쓰지 않는다.
- 영상: 장면별 프롬프트에는 "글자·숫자·로고 없음"을 넣어 배경 영상만 만들게 한다.
- 검사: LLM이 고쳐 쓴 대본이 있으면 check_numbers로 모든 숫자가 도구 결과에 있는지 확인한다.
"""

from __future__ import annotations

import re
from typing import Any

from .tools import FinTools, ToolInputError

NO_TEXT = "no text, no numbers, no letters, no logos, no brand names, no charts with labels"

# 업종별 배경 영상 분위기. 실제 회사 로고·건물을 흉내 내지 않도록 일반적인 장면만 쓴다.
VISUAL_HINT = {
    "semiconductor": "clean room with silicon wafers under soft blue light",
    "battery": "abstract lithium battery cells glowing, industrial lab",
    "bio": "laboratory with pipettes and cell culture plates, calm lighting",
    "auto": "car assembly line with robotic arms, wide shot",
    "finance": "modern city financial district at dusk, glass towers",
    "insurance": "family silhouettes in a park at golden hour",
    "platform": "people using smartphones in a cafe, shallow depth of field",
    "steel": "molten steel pouring in a mill, sparks",
    "chemical": "chemical plant pipes at night with steam",
    "energy": "power transmission towers over hills at sunrise",
    "telecom": "cellular antenna towers against a clear sky",
    "electronics": "home appliances in a bright modern living room",
    "game": "dark room lit by a gaming monitor glow, abstract",
    "shipbuilding": "giant ship hull in a dry dock, aerial shot",
    "aerospace": "jet engine turbine close-up, metallic",
    "cosmetics": "skincare bottles on marble with soft light",
    "food": "food processing line with fresh ingredients",
    "retail": "supermarket aisles with shoppers, motion blur",
    "construction": "construction cranes over a city skyline",
    "default": "abstract flowing lines over a city skyline at night",
}

SECTOR = {
    "005930": "semiconductor", "000660": "semiconductor", "373220": "battery", "006400": "battery",
    "207940": "bio", "068270": "bio", "005380": "auto", "000270": "auto", "012330": "auto",
    "105560": "finance", "055550": "finance", "086790": "finance", "032830": "insurance",
    "000810": "insurance", "028260": "construction", "035420": "platform", "035720": "platform",
    "005490": "steel", "051910": "chemical", "015760": "energy", "017670": "telecom",
    "030200": "telecom", "066570": "electronics", "259960": "game", "036570": "game",
    "329180": "shipbuilding", "012450": "aerospace", "090430": "cosmetics", "097950": "food",
    "139480": "retail",
}


def _fmt_pct(v: float | None) -> str | None:
    return None if v is None else f"{v:,.1f}%"


def _srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(scenes: list[dict[str, Any]]) -> str:
    t = 0.0
    out = []
    for i, sc in enumerate(scenes, 1):
        out.append(f"{i}\n{_srt_time(t)} --> {_srt_time(t + sc['seconds'])}\n{sc['caption']}\n")
        t += sc["seconds"]
    return "\n".join(out)


# ------------------------------------------------------------------ 숫자 검사
# 자막·대본에서 숫자 덩어리를 뽑는다: "1,234억원", "12.3%", "2.50조원", "2025년", "1.2배"
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*(?:조원|억원|원|%|배|년|월|일)?")


def _canon(tok: str) -> str:
    return re.sub(r"[\s,]", "", tok)


def extract_numbers(text: str) -> list[str]:
    return [_canon(m.group()) for m in _NUM.finditer(text) if re.search(r"\d", m.group())]


def check_numbers(script: str, facts: list[dict[str, Any]], allowed: list[str] | None = None) -> dict[str, Any]:
    """대본 속 숫자가 모두 facts의 display 값(또는 허용 목록: 연도·날짜)에 있는지 본다."""
    ok_set = {_canon(f["display"]) for f in facts if f.get("display")}
    for a in allowed or []:
        ok_set.update(extract_numbers(a))
    found = extract_numbers(script)
    unknown = [n for n in found if n not in ok_set]
    return {"numbers_found": len(found), "unknown": unknown, "ok": not unknown}


# ------------------------------------------------------------------ 브리핑
def make_briefing(tools: FinTools, company: str, year: int, date: str) -> dict[str, Any]:
    fin = tools.get_financials(company, year)
    rat = tools.financial_ratios(company, year)
    corp = tools.resolve(company)
    name = fin["company"]

    facts: list[dict[str, Any]] = []

    def add(fid: str, label: str, value: Any, display: str | None, source: Any) -> None:
        if display is not None:
            facts.append({"id": fid, "label": label, "value": value, "display": display, "source": source})

    items = fin["items"]
    for key in ("revenue", "operating_income", "net_income"):
        it = items[key]
        add(key, f"{year}년 {it['label']}", it["value_krw"], it["display"], fin["source"])
    for key in ("operating_margin", "revenue_growth", "operating_income_growth", "debt_ratio", "roe"):
        r = rat["ratios"][key]
        add(key, r["label"], r["value"], _fmt_pct(r["value"]), rat["source"])

    try:
        val = tools.valuation(company, date, year)
        add("market_cap", f"{val['price_date']} 시가총액", val["market_cap_krw"], val["market_cap_display"], val["source"])
        if val["per"] is not None:
            add("per", "후행 PER", val["per"], f"{val['per']:,.1f}배", val["source"])
        if val["pbr"] is not None:
            add("pbr", "PBR", val["pbr"], f"{val['pbr']:,.1f}배", val["source"])
        price_date = val["price_date"]
    except ToolInputError:
        price_date = None

    snippet = None
    if tools.index is not None:
        hits = tools.search_filings("주요 제품과 서비스, 사업의 개요", [name], "business", 1)["results"]
        if hits:
            h = hits[0]
            snippet = {"chunk_id": h["chunk_id"], "location": h["location"], "text": h["text"][:400],
                       "source": h["source"]}

    f = {x["id"]: x for x in facts}
    hint = VISUAL_HINT[SECTOR.get(corp["stock_code"], "default")]
    scenes: list[dict[str, Any]] = [
        {"id": "intro", "seconds": 4.0, "caption": f"{name} {year}년 사업보고서 브리핑",
         "visual_prompt": f"{hint}, cinematic slow push-in, {NO_TEXT}"},
    ]
    if "revenue" in f:
        cap = f"매출액 {f['revenue']['display']}"
        if "revenue_growth" in f:
            cap += f" · 전년 대비 {f['revenue_growth']['display']}"
        scenes.append({"id": "revenue", "seconds": 5.0, "caption": cap,
                       "visual_prompt": f"{hint}, busy and productive mood, {NO_TEXT}"})
    if "operating_income" in f:
        cap = f"영업이익 {f['operating_income']['display']}"
        if "operating_margin" in f:
            cap += f" · 영업이익률 {f['operating_margin']['display']}"
        scenes.append({"id": "profit", "seconds": 5.0, "caption": cap,
                       "visual_prompt": f"{hint}, focused workers, steady camera, {NO_TEXT}"})
    elif "net_income" in f:  # 금융사: 매출액·영업이익 계정 없음
        scenes.append({"id": "profit", "seconds": 5.0, "caption": f"당기순이익 {f['net_income']['display']}",
                       "visual_prompt": f"{hint}, calm and stable mood, {NO_TEXT}"})
    if "market_cap" in f:
        parts = [f"시가총액 {f['market_cap']['display']}"]
        if "per" in f:
            parts.append(f"PER {f['per']['display']}")
        if "pbr" in f:
            parts.append(f"PBR {f['pbr']['display']}")
        scenes.append({"id": "valuation", "seconds": 5.0, "caption": " · ".join(parts),
                       "visual_prompt": f"abstract stock market light trails, dark background, {NO_TEXT}"})
    scenes.append({"id": "source", "seconds": 3.0, "caption": "출처: DART 사업보고서 · 투자 권유 아님",
                   "visual_prompt": f"{hint}, slow fade out, {NO_TEXT}"})

    allowed = [f"{year}년"] + ([price_date] if price_date else [])
    check = check_numbers("\n".join(s["caption"] for s in scenes), facts, allowed)
    return {
        "company": name,
        "year": year,
        "price_date": price_date,
        "facts": facts,
        "business_snippet": snippet,
        "scenes": scenes,
        "total_seconds": sum(s["seconds"] for s in scenes),
        "srt": to_srt(scenes),
        "allowed_numbers": allowed,
        "number_check": check,
        "rules": [
            "숫자는 scenes[].caption(자막)으로만 넣는다. 영상 프롬프트에 숫자를 넣지 않는다.",
            "visual_prompt에는 글자·숫자·로고·실제 회사 브랜드가 나오지 않게 한다.",
            "자막을 고쳐 쓰면 check_briefing_numbers로 숫자를 다시 검사한다.",
            "business_snippet은 원문 발췌이고, 요약 문장을 만들면 숫자 없이 쓴다.",
        ],
    }
