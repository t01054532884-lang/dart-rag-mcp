"""MCP 클라이언트 + Claude 도구 호출 루프 (선택 기능, ANTHROPIC_API_KEY 필요).

이 에이전트는 도구를 직접 import하지 않고 MCP 서버에 붙어 list_tools로 받은 도구 정의를
Claude API tool 형식으로 바꿔 쓴다. 즉 Claude Desktop이 MCP 서버를 쓰는 방식과 같은 경로를 탄다.

    python -m dart_rag.agent "삼성전자와 SK하이닉스의 2025년 영업이익률을 비교하고 HBM 관련 사업 설명을 찾아줘"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import Client

from .mcp_server import build_server
from .tools import FinTools

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = """\
너는 한국 상장사 공시를 근거로 답하는 금융 리서치 보조다.
- 숫자(매출, 이익, 비율, 주가, PER/PBR)는 반드시 도구 결과에서 가져온다. 도구 결과에 없는 숫자를 만들거나 직접 추정하지 않는다.
- 서술형 내용은 search_filings로 원문을 찾아 그 내용만 요약한다. 찾지 못하면 찾지 못했다고 말한다.
- 독립적인 조회는 한 번에 여러 도구를 같이 호출한다.
- 답변 끝에 '출처' 줄을 두고, 사용한 결과의 source.url(DART 원문 링크)을 적는다.
- 투자 권유나 매수·매도 의견은 내지 않는다.
- 한국어로, 짧고 정확하게 답한다.
"""


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    is_error: bool
    seconds: float


@dataclass
class AgentResult:
    answer: str
    stop_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _result_text(res: Any) -> str:
    if getattr(res, "structured_content", None) is not None:
        return json.dumps(res.structured_content, ensure_ascii=False)
    return "\n".join(getattr(c, "text", "") for c in res.content)


async def run_agent(
    question: str,
    tools: FinTools,
    model: str = DEFAULT_MODEL,
    effort: str = "medium",
    max_turns: int = 12,
    verbose: bool = False,
) -> AgentResult:
    import anthropic

    client = anthropic.AsyncAnthropic()
    server = build_server(tools)
    async with Client(server) as mcp:
        listed = await mcp.list_tools()
        claude_tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed.tools
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        result = AgentResult(answer="", stop_reason="")
        for _ in range(max_turns):
            resp = await client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=SYSTEM,
                tools=claude_tools,
                messages=messages,
                output_config={"effort": effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            result.turns += 1
            result.input_tokens += resp.usage.input_tokens
            result.output_tokens += resp.usage.output_tokens
            result.stop_reason = resp.stop_reason or ""
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "refusal":
                result.answer = "(모델이 이 요청에 답하지 않았습니다)"
                return result
            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                result.answer = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return result

            async def call(block: Any) -> dict[str, Any]:
                t0 = time.perf_counter()
                res = await mcp.call_tool(block.name, dict(block.input))
                is_error = bool(getattr(res, "is_error", False))
                result.tool_calls.append(ToolCall(block.name, dict(block.input), is_error, time.perf_counter() - t0))
                if verbose:
                    print(f"  → {block.name}({json.dumps(block.input, ensure_ascii=False)})"
                          f"{' [오류]' if is_error else ''}", file=sys.stderr)
                return {"type": "tool_result", "tool_use_id": block.id, "content": _result_text(res),
                        "is_error": is_error}

            # 병렬 호출 결과는 한 user 메시지에 모아서 돌려준다
            results = [await call(b) for b in uses]
            messages.append({"role": "user", "content": results})
        result.answer = "(도구 호출 횟수 한도에 도달했습니다)"
        result.stop_reason = "max_turns"
        return result


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .mcp_server import load_tools

    p = argparse.ArgumentParser(prog="dart-rag-agent")
    p.add_argument("question")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = p.parse_args(argv)
    get_settings()  # .env 읽기
    res = anyio.run(lambda: run_agent(args.question, load_tools(), args.model, args.effort, verbose=True))
    print(res.answer)
    print(f"\n[도구 호출 {len(res.tool_calls)}회, 턴 {res.turns}, 토큰 입력 {res.input_tokens:,} / 출력 {res.output_tokens:,}]",
          file=sys.stderr)


if __name__ == "__main__":
    main()
