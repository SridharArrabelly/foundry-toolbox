"""
Consume the Foundry Toolbox MCP endpoint from a Microsoft Agent Framework agent.

The agent connects to the toolbox as a single MCP tool and dynamically
discovers every underlying tool (AI Search, etc.). This is the exact
integration pattern we'd port into mtn-execu-copilot.

Run:
    uv run python main.py "what did we decide about the Q3 roadmap?"
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from agent_framework import MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv


# Toolboxes are in public preview behind this feature header.
_TOOLBOX_FEATURES = "Toolboxes=V1Preview"

SYSTEM_PROMPT = (
    "You are the MTN executive assistant. You have access to a Foundry "
    "toolbox with two grounding tools:\n"
    "  - meeting-mins-ai-search: INTERNAL board meeting minutes, decisions, "
    "action items and owners. Use this for anything about MTN's internal "
    "strategy, prior decisions, ownership, or historical context.\n"
    "  - bing-grounding: EXTERNAL public web. Use this for current news, "
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


class _ToolboxAuth(httpx.Auth):
    """Inject a fresh Entra bearer token on every request to the toolbox."""

    def __init__(self, token_provider):
        self._get_token = token_provider

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {self._get_token()}"
        yield request


async def run(question: str) -> int:
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

        # Own the MCP session in this task so its cleanup task-affinity matches
        # the httpx client; otherwise we get a noisy "exit cancel scope in a
        # different task" error from the mcp client lib on shutdown.
        async with mcp_tool:
            agent = chat_client.as_agent(
                name=agent_name,
                instructions=SYSTEM_PROMPT,
                tools=[mcp_tool],
            )

            print(f"-> user: {question}\n")
            result = await agent.run(question)
            print(result)
    return 0


def main() -> int:
    load_dotenv()
    question = " ".join(sys.argv[1:]).strip() or (
        "Summarize the most recent meeting minutes and list action items with owners."
    )
    return asyncio.run(run(question))


if __name__ == "__main__":
    raise SystemExit(main())
