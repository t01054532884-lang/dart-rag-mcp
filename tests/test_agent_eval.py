"""API 키 없이 에이전트 루프를 가짜 Claude 응답으로 검사한다."""

from types import SimpleNamespace

import anyio
import pytest

from dart_rag import agent as agent_mod
from dart_rag.evaluate import eval_retrieval, make_numeric_questions, matches, numbers_in


def _usage():
    return SimpleNamespace(input_tokens=100, output_tokens=20)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.beta = SimpleNamespace(messages=FakeMessages(script))


def test_agent_loop_calls_mcp_tools_and_returns_answer(tools, monkeypatch):
    script = [
        SimpleNamespace(stop_reason="tool_use", usage=_usage(), content=[
            SimpleNamespace(type="tool_use", id="t1", name="financial_ratios",
                            input={"company": "가나", "year": 2025}),
            SimpleNamespace(type="tool_use", id="t2", name="get_financials",
                            input={"company": "없는회사", "year": 2025}),
        ]),
        SimpleNamespace(stop_reason="end_turn", usage=_usage(),
                        content=[SimpleNamespace(type="text", text="영업이익률은 20.0%입니다.")]),
    ]
    fake = FakeClient(script)
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda: fake)

    res = anyio.run(lambda: agent_mod.run_agent("가나 영업이익률?", tools))
    assert res.answer == "영업이익률은 20.0%입니다."
    assert [t.name for t in res.tool_calls] == ["financial_ratios", "get_financials"]
    assert [t.is_error for t in res.tool_calls] == [False, True]
    # messages = [질문, 1차 응답(tool_use), 도구 결과, 최종 응답]
    msgs = fake.beta.messages.calls[1]["messages"]
    assert msgs[2]["role"] == "user"
    tool_results = msgs[2]["content"]
    assert [r["tool_use_id"] for r in tool_results] == ["t1", "t2"]  # 한 메시지에 모아서
    assert '"operating_margin"' in tool_results[0]["content"]
    assert fake.beta.messages.calls[0]["fallbacks"] == "default"
    assert res.input_tokens == 200


def test_agent_stops_on_refusal(tools, monkeypatch):
    fake = FakeClient([SimpleNamespace(stop_reason="refusal", usage=_usage(), content=[])])
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda: fake)
    res = anyio.run(lambda: agent_mod.run_agent("?", tools))
    assert res.stop_reason == "refusal"


@pytest.mark.parametrize("text,gold,ok", [
    ("매출액은 1.00조원입니다", 1_000_000_000_000, True),
    ("영업이익 2,000억원", 200_000_000_000, True),
    ("영업이익률 20.0%", 20.0, True),
    ("영업이익률 25%", 20.0, False),
])
def test_number_matching(text, gold, ok):
    assert matches(text, gold) is ok


def test_numbers_in_units():
    assert numbers_in("1,234억 원과 3.5조") == [123_400_000_000.0, 3_500_000_000_000.0]


def test_numeric_questions_from_db(tools):
    qs = make_numeric_questions(tools, 2025, 50)
    kinds = {q["kind"] for q in qs}
    assert {"revenue", "operating_margin"} <= kinds
    assert all(q["company"] != "다라은행" or q["kind"] not in ("revenue", "operating_margin") for q in qs)


def test_eval_retrieval_runs(tools):
    rep = eval_retrieval(tools, [{"id": "q1", "question": "고대역폭 메모리 판매", "company": "가나",
                                  "keywords": ["HBM", "고대역폭"]}], k=5)
    assert rep["modes"]["bm25"]["hit@1"] == 1.0
    assert set(rep["modes"]) == {"bm25", "vector", "hybrid"}


def test_hybrid_weight_extremes_follow_single_mode(tools):
    idx = tools.index
    q = "고대역폭 메모리 판매"
    bm25 = [h.chunk_id for h in idx.search(q, top_k=3, mode="bm25")]
    vec = [h.chunk_id for h in idx.search(q, top_k=3, mode="vector")]
    assert [h.chunk_id for h in idx.search(q, top_k=3, mode="hybrid", bm25_weight=1.0)][:len(bm25)] == bm25
    assert [h.chunk_id for h in idx.search(q, top_k=3, mode="hybrid", bm25_weight=0.0)][:len(vec)] == vec


def test_eval_retrieval_reports_each_weight(tools):
    rep = eval_retrieval(tools, [{"id": "q1", "question": "고대역폭 메모리 판매", "company": "가나",
                                  "keywords": ["HBM"]}], k=5, weights=[0.5, 0.8])
    assert set(rep["modes"]) == {"bm25", "vector", "hybrid", "hybrid_bm25w0.8"}
