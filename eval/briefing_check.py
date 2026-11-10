"""숏폼 브리핑 대본 검사 (API 키 필요 없음).

1) 30개 회사 브리핑을 만들고, 자막 속 숫자가 모두 재무 도구 값(facts)에 있는지 본다.
2) 자막 숫자 하나를 일부러 바꿨을 때 check_numbers가 잡는지 본다 (회사마다 1건씩).
3) 영상 프롬프트에 숫자가 들어가지 않았는지 본다.
결과: docs/briefing_check.json, 예시 SRT: docs/briefing_samples/*.srt
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from dart_rag.briefing import check_numbers, make_briefing
from dart_rag.mcp_server import load_tools

ROOT = Path(__file__).resolve().parents[1]
YEAR = 2025
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-11-09"
SAMPLES = ["삼성전자", "KB금융", "크래프톤"]


def mutate(caption: str) -> str | None:
    """첫 번째 숫자 덩어리의 마지막 숫자를 하나 바꾼다."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", caption)
    if not m:
        return None
    tok = m.group()
    i = max(j for j, ch in enumerate(tok) if ch.isdigit())
    new = tok[:i] + str((int(tok[i]) + 1) % 10) + tok[i + 1:]
    return caption[: m.start()] + new + caption[m.end():]


def main() -> None:
    tools = load_tools()
    rows = []
    out_dir = ROOT / "docs" / "briefing_samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in tools.store.companies():
        b = make_briefing(tools, c["corp_name"], YEAR, DATE)
        captions = [s["caption"] for s in b["scenes"]]
        mutated_caught = None
        for k, cap in enumerate(captions):
            if b["scenes"][k]["id"] in ("intro", "source"):
                continue
            new = mutate(cap)
            if new:
                script = "\n".join(captions[:k] + [new] + captions[k + 1:])
                mutated_caught = not check_numbers(script, b["facts"], b["allowed_numbers"])["ok"]
                break
        rows.append({
            "company": b["company"],
            "scenes": [s["id"] for s in b["scenes"]],
            "seconds": b["total_seconds"],
            "facts": len(b["facts"]),
            "caption_numbers": b["number_check"]["numbers_found"],
            "caption_ok": b["number_check"]["ok"],
            "unknown": b["number_check"]["unknown"],
            "prompt_has_digit": any(re.search(r"\d", s["visual_prompt"]) for s in b["scenes"]),
            "mutation_caught": mutated_caught,
            "snippet": bool(b["business_snippet"]),
        })
        if b["company"] in SAMPLES:
            (out_dir / f"{b['company']}.srt").write_text(b["srt"], encoding="utf-8")
            (out_dir / f"{b['company']}.json").write_text(
                json.dumps({k: b[k] for k in ("company", "year", "price_date", "facts", "scenes")},
                           ensure_ascii=False, indent=2), encoding="utf-8")

    tested = [r for r in rows if r["mutation_caught"] is not None]
    summary = {
        "date": DATE,
        "year": YEAR,
        "companies": len(rows),
        "caption_numbers_total": sum(r["caption_numbers"] for r in rows),
        "caption_ok": sum(r["caption_ok"] for r in rows),
        "prompts_with_digits": sum(r["prompt_has_digit"] for r in rows),
        "mutations_tested": len(tested),
        "mutations_caught": sum(r["mutation_caught"] for r in tested),
        "without_revenue_scene": [r["company"] for r in rows if "revenue" not in r["scenes"]],
        "without_valuation_scene": [r["company"] for r in rows if "valuation" not in r["scenes"]],
        "seconds_min_max": [min(r["seconds"] for r in rows), max(r["seconds"] for r in rows)],
    }
    (ROOT / "docs" / "briefing_check.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
