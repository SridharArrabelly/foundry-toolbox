"""
Consume the Foundry Toolbox MCP endpoint from a Microsoft Agent Framework agent,
with end-to-end tracing of which tools were invoked and how long each step took.

Two layers of observability:
  1. Always-on per-run trace via streaming: prints assistant text as it arrives,
     records each MCP tool call's name/args/duration, and prints a final
     summary table.
  2. Optional full OpenTelemetry trace to stdout (chat client + MCP HTTP +
     function spans), enabled via ENABLE_OTEL_CONSOLE=true.

Run:
    uv run python main.py "what was decided about ESG and executive compensation?"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv


_TOOLBOX_FEATURES = "Toolboxes=V1Preview"

SYSTEM_PROMPT = (
    "You are the MTN executive assistant. You have access to a Foundry "
    "toolbox with two grounding tools:\n"
    "  - meeting-mins-ai-search: INTERNAL board meeting minutes, decisions, "
    "action items and owners. Use this for anything about MTN's internal "
    "strategy, prior decisions, ownership, or historical context.\n"
    "  - bing-web-search: EXTERNAL public web. Use this for current news, "
    "share prices, competitive intelligence, telco industry trends, "
    "regulatory updates, or any question that needs information not "
    "covered by internal minutes.\n"
    "When a question spans both (e.g. 'what did the board decide about "
    "Project Zero and how is the market reacting?'), call both tools and "
    "synthesise. Always cite the sources you used."
)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on") or (
        default and name not in os.environ
    )


class _ToolboxAuth(httpx.Auth):
    """Inject a fresh Entra bearer token on every request to the toolbox."""

    def __init__(self, token_provider):
        self._get_token = token_provider

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


# ---------------------------------------------------------------------------
# Per-run tracing
# ---------------------------------------------------------------------------


@dataclass
class _ToolCall:
    name: str
    server: str | None
    arguments: Any
    started_at: float
    ended_at: float | None = None
    output_preview: str | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000.0


@dataclass
class _RunTrace:
    run_started_at: float
    first_token_at: float | None = None
    run_ended_at: float | None = None
    tool_calls: list[_ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    _by_call_id: dict[str, _ToolCall] = field(default_factory=dict, repr=False)

    def on_tool_call(self, *, call_id: str | None, name: str, server: str | None, arguments: Any) -> None:
        call = _ToolCall(
            name=name or "<unnamed>",
            server=server,
            arguments=arguments,
            started_at=time.perf_counter(),
        )
        self.tool_calls.append(call)
        if call_id:
            self._by_call_id[call_id] = call

    def on_tool_result(self, *, call_id: str | None, output: Any, error: str | None = None) -> None:
        call = self._by_call_id.get(call_id) if call_id else None
        if call is None and self.tool_calls:
            # fall back to the most recently started call still open
            for c in reversed(self.tool_calls):
                if c.ended_at is None:
                    call = c
                    break
        if call is None:
            return
        call.ended_at = time.perf_counter()
        call.error = error
        if output is not None:
            preview = output if isinstance(output, str) else json.dumps(output, default=str)
            call.output_preview = (preview[:160] + "…") if len(preview) > 160 else preview

    def add_usage(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0

    def mark_first_token(self) -> None:
        if self.first_token_at is None:
            self.first_token_at = time.perf_counter()

    def finish(self) -> None:
        self.run_ended_at = time.perf_counter()

    def print_summary(self) -> None:
        total_ms = (self.run_ended_at - self.run_started_at) * 1000.0 if self.run_ended_at else 0.0
        ttft_ms = (
            (self.first_token_at - self.run_started_at) * 1000.0 if self.first_token_at else None
        )
        tool_ms = sum((c.duration_ms or 0.0) for c in self.tool_calls)
        model_ms = max(total_ms - tool_ms, 0.0)

        print("\n" + "=" * 78)
        print("RUN TRACE")
        print("=" * 78)
        if self.tool_calls:
            print(f"{'#':>2}  {'tool':40s} {'duration':>10s}  status")
            print("-" * 78)
            for i, c in enumerate(self.tool_calls, 1):
                dur = f"{c.duration_ms:.0f} ms" if c.duration_ms is not None else "—"
                status = "ok" if not c.error else f"ERR {c.error[:30]}"
                label = c.name if not c.server else f"{c.server}::{c.name}"
                print(f"{i:>2}  {label[:40]:40s} {dur:>10s}  {status}")
        else:
            print("(no tool calls — model answered without grounding)")
        print("-" * 78)
        print(f"  tool calls:            {len(self.tool_calls)}")
        print(f"  time in tools:         {tool_ms:>8.0f} ms")
        print(f"  time in model/network: {model_ms:>8.0f} ms")
        if ttft_ms is not None:
            print(f"  time to first token:   {ttft_ms:>8.0f} ms")
        print(f"  total wall time:       {total_ms:>8.0f} ms")
        if self.input_tokens or self.output_tokens:
            print(f"  tokens (in/out):       {self.input_tokens} / {self.output_tokens}")
        print("=" * 78)


# ---------------------------------------------------------------------------
# Optional full OTel console tracing
# ---------------------------------------------------------------------------


def _maybe_enable_otel_console() -> None:
    if not _bool_env("ENABLE_OTEL_CONSOLE", default=False):
        return
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from agent_framework.observability import enable_instrumentation

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    enable_instrumentation(enable_sensitive_data=True)
    print("-> OTel console tracing enabled (ENABLE_OTEL_CONSOLE=true)")


# ---------------------------------------------------------------------------
# Streaming + tracing run loop
# ---------------------------------------------------------------------------


def _ingest_content(trace: _RunTrace, content: Any) -> None:
    """Extract tool calls / results / text / usage from a single Content item."""
    ctype = getattr(content, "type", None)

    if ctype == "text":
        text = getattr(content, "text", "") or ""
        if text:
            trace.mark_first_token()
            sys.stdout.write(text)
            sys.stdout.flush()

    elif ctype in ("function_call", "mcp_server_tool_call", "search_tool_call"):
        trace.on_tool_call(
            call_id=getattr(content, "call_id", None),
            name=getattr(content, "tool_name", None) or getattr(content, "name", None) or ctype,
            server=getattr(content, "server_name", None),
            arguments=getattr(content, "arguments", None),
        )

    elif ctype in ("function_result", "mcp_server_tool_result", "search_tool_result"):
        output = getattr(content, "output", None)
        if output is None:
            output = getattr(content, "result", None)
        trace.on_tool_result(
            call_id=getattr(content, "call_id", None),
            output=output,
            error=getattr(content, "exception", None),
        )

    elif ctype == "usage":
        details = getattr(content, "usage_details", None) or {}
        trace.add_usage(
            input_tokens=int(details.get("input_token_count") or details.get("input_tokens") or 0),
            output_tokens=int(details.get("output_token_count") or details.get("output_tokens") or 0),
        )


async def run(question: str) -> int:
    _maybe_enable_otel_console()

    toolbox_endpoint = _require("FOUNDRY_TOOLBOX_ENDPOINT")
    project_endpoint = _require("FOUNDRY_PROJECT_ENDPOINT")
    model = _require("FOUNDRY_MODEL")
    agent_name = os.environ.get("AGENT_NAME", "toolbox-test-agent")

    credential = DefaultAzureCredential()
    chat_client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=credential,
    )

    token_provider = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
    http_client = httpx.AsyncClient(
        auth=_ToolboxAuth(token_provider),
        headers={"Foundry-Features": _TOOLBOX_FEATURES},
        timeout=120.0,
    )

    print(f"-> connecting to toolbox: {toolbox_endpoint}")
    async with http_client:
        mcp_tool = MCPStreamableHTTPTool(
            name="toolbox",
            url=toolbox_endpoint,
            http_client=http_client,
            load_prompts=False,
        )

        async with mcp_tool:
            agent = chat_client.as_agent(
                name=agent_name,
                instructions=SYSTEM_PROMPT,
                tools=[mcp_tool],
            )

            print(f"-> user: {question}\n")
            trace = _RunTrace(run_started_at=time.perf_counter())
            async for update in agent.run(question, stream=True):
                for content in getattr(update, "contents", []) or []:
                    _ingest_content(trace, content)
            trace.finish()
            trace.print_summary()
    return 0


def main() -> int:
    load_dotenv()
    question = " ".join(sys.argv[1:]).strip() or (
        "Summarize the most recent meeting minutes and list action items with owners."
    )
    return asyncio.run(run(question))


if __name__ == "__main__":
    raise SystemExit(main())
