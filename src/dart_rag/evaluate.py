"""평가.

1) retrieval (키 필요 없음): 검색 방식별(BM25 / 벡터 / 하이브리드) Hit@1, Hit@5, MRR@10
   python -m dart_rag.evaluate retrieval eval/set_a_keyword.jsonl --weights 0.5 0.8

   질문 파일 한 줄 = {"id", "question", "company", "keywords": [...]}
   정답 판정: 결과 문단이 `company`의 사업보고서이고 `keywords` 중 하나라도 들어 있으면 정답.
   회사 이름이 질문에 들어 있지 않은 상태(회사 필터 없음)로 검색한다.

2) agent (ANTHROPIC_API_KEY 필요): DB에서 만든 숫자 질문에 에이전트가 맞는 값을 답하는지
   python -m dart_rag.evaluate agent --year 2025 --n 30
   정답 판정: 답변 속 숫자(조원·억원·%) 중 하나가 정답과 ±1% 안이면 정답.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

from .config import ROOT
from .tools import METRICS, FinTools, ToolInputError

DOCS = ROOT / "docs"


# ----------------------------------------------------------------- retrieval
def load_questions(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def is_relevant(chunk: dict[str, Any], corp_code: str, keywords: list[str]) -> bool:
    text = chunk["text"].lower()
    return chunk["corp_code"] == corp_code and any(k.lower() in text for k in keywords)


def _configs(weights: list[float]) -> list[tuple[str, str, float | None]]:
    """(보고서 이름, 검색 mode, BM25 가중치). 하이브리드는 가중치마다 따로 잰다."""
    out: list[tuple[str, str, float | None]] = [("bm25", "bm25", None), ("vector", "vector", None)]
    for w in weights:
        out.append(("hybrid" if w == 0.5 else f"hybrid_bm25w{w:g}", "hybrid", w))
    return out


def eval_retrieval(tools: FinTools, questions: list[dict[str, Any]], k: int = 10,
                   weights: list[float] | None = None) -> dict[str, Any]:
    assert tools.index is not None
    report: dict[str, Any] = {"n_questions": len(questions), "k": k, "modes": {}, "per_question": []}
    per_q: dict[str, dict[str, int | None]] = {q["id"]: {} for q in questions}
    for name, mode, w in _configs(weights or [0.5]):
        hit1 = hit5 = 0
        rr = 0.0
        for q in questions:
            corp = tools.resolve(q["company"])
            hits = tools.index.search(q["question"], top_k=k, mode=mode, bm25_weight=w)  # type: ignore[arg-type]
            chunks = tools.store.chunks_by_ids([h.chunk_id for h in hits])
            rank = next(
                (i for i, h in enumerate(hits, 1) if is_relevant(chunks[h.chunk_id], corp["corp_code"], q["keywords"])),
                None,
            )
            per_q[q["id"]][name] = rank
            hit1 += rank == 1
            hit5 += rank is not None and rank <= 5
            rr += 1.0 / rank if rank else 0.0
        n = len(questions) or 1
        report["modes"][name] = {"hit@1": round(hit1 / n, 3), "hit@5": round(hit5 / n, 3), f"mrr@{k}": round(rr / n, 3)}
    report["per_question"] = [{"id": q["id"], "question": q["question"], "rank": per_q[q["id"]]} for q in questions]
    return report


# ----------------------------------------------------------------- agent
_NUM = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(조\s*원|조|억\s*원|억|%|원)?")
_UNIT = {"조원": 1e12, "조": 1e12, "억원": 1e8, "억": 1e8, "원": 1.0, "%": 1.0, "": 1.0}


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        raw, unit = m.group(1).replace(",", ""), (m.group(2) or "").replace(" ", "")
        try:
            out.append(float(raw) * _UNIT.get(unit, 1.0))
        except ValueError:
            continue
    return out


def matches(answer: str, gold: float, tol: float = 0.01) -> bool:
    return any(abs(x - gold) <= abs(gold) * tol for x in numbers_in(answer)) if gold else False


def make_numeric_questions(tools: FinTools, year: int, n: int, seed: int = 7) -> list[dict[str, Any]]:
    """DB 값으로 정답이 정해진 질문을 만든다 (금액 질문 + 비율 질문)."""
    rng = random.Random(seed)
    qs: list[dict[str, Any]] = []
    for c in tools.store.companies():
        name = c.get("alias") or c["corp_name"]
        try:
            fin = tools.get_financials(c["corp_name"], year)
            rat = tools.financial_ratios(c["corp_name"], year)
        except ToolInputError:
            continue
        for key in ("revenue", "operating_income", "net_income"):
            v = fin["items"][key]["value_krw"]
            if v:
                qs.append({"question": f"{name}의 {year}년 연결 기준 {METRICS[key].label}은 얼마야?",
                           "gold": v, "kind": key, "company": c["corp_name"]})
        for key, label in (("operating_margin", "영업이익률"), ("debt_ratio", "부채비율")):
            v = rat["ratios"][key]["value"]
            if v is not None:
                qs.append({"question": f"{name}의 {year}년 {label}은 몇 %야?", "gold": v, "kind": key,
                           "company": c["corp_name"]})
    rng.shuffle(qs)
    return qs[:n]


def eval_agent(tools: FinTools, year: int, n: int, model: str, effort: str) -> dict[str, Any]:
    import anyio

    from .agent import run_agent

    qs = make_numeric_questions(tools, year, n)
    rows = []
    for i, q in enumerate(qs, 1):
        res = anyio.run(lambda: run_agent(q["question"], tools, model=model, effort=effort))
        ok = matches(res.answer, float(q["gold"]))
        rows.append({**q, "correct": ok, "answer": res.answer, "tools": [t.name for t in res.tool_calls],
                     "tool_errors": sum(t.is_error for t in res.tool_calls),
                     "input_tokens": res.input_tokens, "output_tokens": res.output_tokens})
        print(f"[{i}/{len(qs)}] {'O' if ok else 'X'} {q['question']}", file=sys.stderr)
    m = len(rows) or 1
    return {
        "model": model, "effort": effort, "year": year, "n": len(rows),
        "accuracy": round(sum(r["correct"] for r in rows) / m, 3),
        "avg_tool_calls": round(sum(len(r["tools"]) for r in rows) / m, 2),
        "tool_error_rate": round(sum(r["tool_errors"] for r in rows) / max(1, sum(len(r["tools"]) for r in rows)), 3),
        "total_input_tokens": sum(r["input_tokens"] for r in rows),
        "total_output_tokens": sum(r["output_tokens"] for r in rows),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .mcp_server import load_tools

    p = argparse.ArgumentParser(prog="dart-rag-eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retrieval")
    r.add_argument("questions", type=Path)
    r.add_argument("--weights", type=float, nargs="+", default=[0.5],
                   help="하이브리드 BM25 가중치 후보 (예: 0.3 0.5 0.7)")
    a = sub.add_parser("agent")
    a.add_argument("--year", type=int, required=True)
    a.add_argument("--n", type=int, default=30)
    a.add_argument("--model", default="claude-opus-5-5")
    a.add_argument("--effort", default="medium")
    args = p.parse_args(argv)

    s = get_settings()
    tools = load_tools()
    DOCS.mkdir(exist_ok=True)
    meta_path = s.qdrant_path.parent / f"{s.qdrant_path.name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if args.cmd == "retrieval":
        rep = eval_retrieval(tools, load_questions(args.questions), weights=args.weights)
        rep["questions_file"] = args.questions.as_posix()
        rep["embed_model"] = meta.get("embed_model", s.embed_model)
        rep["chunks"] = meta.get("chunks")
        out = DOCS / f"retrieval__{args.questions.stem}__{s.qdrant_path.name}.json"
    else:
        rep = eval_agent(tools, args.year, args.n, args.model, args.effort)
        out = DOCS / "agent_eval.json"
    rep["run_at"] = dt.datetime.now().isoformat(timespec="seconds")
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = rep.get("modes") or {k: rep[k] for k in ("accuracy", "avg_tool_calls", "tool_error_rate")}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
