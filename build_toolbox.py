"""
Build (or update) a Foundry Toolbox that bundles:
  - Azure AI Search over the meeting-mins-index (internal grounding)
  - Bing Grounding (external news / share prices / industry highlights)

Run once to create a toolbox version, then copy the printed MCP endpoint
into .env as FOUNDRY_TOOLBOX_ENDPOINT. The agent in main.py consumes it.

Usage:
    uv run python build_toolbox.py
"""

from __future__ import annotations

import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AISearchIndexResource,
    AzureAISearchQueryType,
    AzureAISearchTool,
    AzureAISearchToolResource,
    SearchContextSize,
    WebSearchTool,
)
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required env var: {name}")
    return value


def main() -> int:
    load_dotenv()

    project_endpoint = _require("FOUNDRY_PROJECT_ENDPOINT")
    toolbox_name = _require("TOOLBOX_NAME")
    search_conn_name = _require("SEARCH_CONNECTION_NAME")
    search_index = _require("SEARCH_INDEX_NAME")
    enable_web_search = os.environ.get("ENABLE_WEB_SEARCH", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )

    client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )

    print(f"-> resolving search connection: {search_conn_name}")
    search_conn = client.connections.get(name=search_conn_name)
    print(f"   connection id: {search_conn.id}")

    tools = [
        AzureAISearchTool(
            name="meeting-mins-ai-search",
            description=(
                "Search internal MTN executive board meeting minutes for "
                "decisions, action items, owners, and historical context."
            ),
            azure_ai_search=AzureAISearchToolResource(
                indexes=[
                    AISearchIndexResource(
                        project_connection_id=search_conn.id,
                        index_name=search_index,
                        query_type=AzureAISearchQueryType.SIMPLE,
                        top_k=5,
                    ),
                ],
            ),
        ),
    ]

    if enable_web_search:
        # Foundry routes `web_search` to the project's Grounding-with-Bing
        # connection automatically — no explicit project_connection_id needed
        # here. Toolboxes do NOT accept tool type `bing_grounding`; allowed
        # types are mcp, web_search, azure_ai_search, openapi, etc.
        tools.append(
            WebSearchTool(
                name="bing-web-search",
                description=(
                    "Grounded public web search via Bing. Use for external "
                    "context: current news, share prices, competitive "
                    "intelligence, telco industry highlights, regulatory "
                    "updates — anything not in internal meeting minutes."
                ),
                search_context_size=SearchContextSize.MEDIUM,
            )
        )
    else:
        print("-> ENABLE_WEB_SEARCH=false, skipping web search tool")

    print(f"-> creating toolbox version: {toolbox_name} ({len(tools)} tool(s))")
    toolbox_version = client.beta.toolboxes.create_version(
        name=toolbox_name,
        description=(
            "MTN executive copilot toolbox: internal meeting minutes (AI "
            "Search) + external grounding (Bing)."
        ),
        tools=tools,
    )

    print(f"\nCreated toolbox: {toolbox_version.name}, version: {toolbox_version.version}")
    mcp_endpoint = (
        f"{project_endpoint.rstrip('/')}/toolboxes/{toolbox_version.name}"
        f"/versions/{toolbox_version.version}/mcp?api-version=v1"
    )
    print(f"MCP endpoint:\n  {mcp_endpoint}")
    print("\nNext: paste that URL into .env as FOUNDRY_TOOLBOX_ENDPOINT, then run `uv run python main.py`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
