"""
Build (or update) a Foundry Toolbox containing the meeting-mins AI Search tool.

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

    client = AIProjectClient(
        endpoint=project_endpoint,
        credential=DefaultAzureCredential(),
    )

    print(f"-> resolving search connection: {search_conn_name}")
    connection = client.connections.get(name=search_conn_name)
    print(f"   connection id: {connection.id}")

    search_tool = AzureAISearchTool(
        name="meeting-mins-ai-search",
        description="Search meeting minutes for decisions and action items.",
        azure_ai_search=AzureAISearchToolResource(
            indexes=[
                AISearchIndexResource(
                    project_connection_id=connection.id,
                    index_name=search_index,
                    query_type=AzureAISearchQueryType.SIMPLE,
                    top_k=5,
                ),
            ],
        ),
    )

    print(f"-> creating toolbox version: {toolbox_name}")
    toolbox_version = client.beta.toolboxes.create_version(
        name=toolbox_name,
        description=(
            "Spike toolbox for evaluating Foundry Toolboxes against the "
            "meeting-mins-index AI Search index."
        ),
        tools=[search_tool],
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
