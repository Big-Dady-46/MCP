"""NexusAI — Google Gemini AI + Remote MCP Server Integration.

Connects Google Gemini AI to the deployed remote NexusAI MCP server
(Streamable HTTP transport) with Bearer token authentication.

Features:
- Gemini API with remote MCP Streamable HTTP tool calling.
- Server-side authentication using GEMINI_API_KEY and MCP_GATEWAY_TOKEN.
- Dual execution strategy:
  1. Google Gemini Interactions API with remote `mcp_server` Streamable HTTP support.
  2. Fallback function-calling loop over Streamable HTTP JSON-RPC tools/call.
- Read-only tools enforcement: permits inventory, stock, sales, purchases,
  workers, expenses, and business reports queries.
- Safe serverless/Vercel deployment: no secrets leaked to client.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("nexusai.gemini_mcp")

# ---------------------------------------------------------------------------
# Configuration (Environment Variables)
# ---------------------------------------------------------------------------

DEFAULT_REMOTE_MCP_URL = "https://mcp-control.vercel.app/mcp"
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

SYSTEM_INSTRUCTION = (
    "You are the NexusAI Beverage Store AI Assistant. You have access to real-time "
    "store data through the remote NexusAI MCP server tools. Always query the appropriate "
    "read-only tools to retrieve accurate, live data before answering questions regarding "
    "products, stock levels, reorder alerts, sales history, daily summaries, purchases, "
    "workers, salaries, expenses, stock valuations, or business reports.\n"
    "Store amounts and prices are in Pakistani Rupees (PKR).\n"
    "Be concise, helpful, and format financial numbers clearly."
)

ALLOWED_READ_ONLY_TOOLS = {
    "list_products",
    "get_product",
    "get_low_stock",
    "list_sales",
    "get_sale",
    "get_sales_summary",
    "list_purchases",
    "list_workers",
    "get_business_summary",
    "get_profit_loss",
    "get_stock_valuation",
    "get_dashboard_summary",
    "generate_business_report",
}


def get_gemini_api_key() -> str:
    """Return GEMINI_API_KEY from environment."""
    return os.environ.get("GEMINI_API_KEY", "").strip()


def get_mcp_gateway_token() -> str:
    """Return MCP_GATEWAY_TOKEN from environment."""
    return os.environ.get("MCP_GATEWAY_TOKEN", "").strip()


def get_remote_mcp_url() -> str:
    """Return remote MCP endpoint URL."""
    return os.environ.get("NEXUSAI_REMOTE_MCP_URL", DEFAULT_REMOTE_MCP_URL).strip() or DEFAULT_REMOTE_MCP_URL


# ---------------------------------------------------------------------------
# Remote MCP Streamable HTTP Client
# ---------------------------------------------------------------------------

def call_remote_mcp(
    method: str,
    params: dict[str, Any] | None = None,
    endpoint: str | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Dispatch a JSON-RPC 2.0 call to the remote MCP Streamable HTTP endpoint."""
    url = (endpoint or get_remote_mcp_url()).rstrip("/")
    bearer = token or get_mcp_gateway_token()

    if not bearer:
        raise ValueError("MCP_GATEWAY_TOKEN is not configured.")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {bearer}",
        "MCP-Protocol-Version": "2024-11-05",
    }

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")

            if "text/event-stream" in content_type:
                # Parse Server-Sent Events stream for JSON-RPC message
                for line in raw.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                return json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                return {"result": raw}

            return json.loads(raw) if raw.strip() else {}

    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace")
        logger.error("Remote MCP HTTPError %d: %s", err.code, err_body)
        raise RuntimeError(f"Remote MCP server returned HTTP {err.code}: {err_body}") from err
    except Exception as exc:
        logger.error("Remote MCP connection error: %s", exc)
        raise


def execute_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    endpoint: str | None = None,
    token: str | None = None,
) -> str:
    """Execute a read-only store tool via store MCP tools.
    
    Uses direct local store_server execution for instant sub-millisecond response
    without serverless loop timeouts, and falls back to remote MCP Streamable HTTP.
    """
    if name not in ALLOWED_READ_ONLY_TOOLS:
        return f"Error: Tool '{name}' is not an authorized read-only tool on this gateway."

    args = arguments or {}

    # Fast path: direct store_server execution (instant, avoids serverless recursion)
    try:
        import store_server
        tool_fn = getattr(store_server, name, None)
        if tool_fn is not None:
            raw_res = tool_fn(**args)
            return raw_res if isinstance(raw_res, str) else json.dumps(raw_res, default=str)
    except Exception as local_exc:
        logger.info("Local store_server execution skipped: %s", local_exc)

    # Remote MCP Streamable HTTP call (with fast 3-second timeout)
    try:
        res = call_remote_mcp(
            method="tools/call",
            params={"name": name, "arguments": args},
            endpoint=endpoint,
            token=token,
            timeout=3.0,
        )
        if "error" in res:
            err_msg = res["error"].get("message", str(res["error"]))
            logger.warning("Remote MCP tool call error: %s", err_msg)
        elif "result" in res:
            result_obj = res["result"]
            if isinstance(result_obj, dict) and "content" in result_obj:
                texts = [
                    c.get("text", "")
                    for c in result_obj["content"]
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if texts:
                    return "\n".join(texts)
            return json.dumps(result_obj, ensure_ascii=False)
    except Exception as exc:
        logger.error("Remote MCP call failed: %s", exc)

    return f"Error executing tool '{name}': unable to retrieve data."


def get_gemini_tool_declarations() -> list[dict[str, Any]]:
    """Return Gemini function declarations for all 13 read-only tools."""
    try:
        import store_server
        tools_meta = store_server.get_tool_definitions()
    except Exception:
        tools_meta = []

    declarations = []
    for t in tools_meta:
        name = t.get("name", "")
        if name not in ALLOWED_READ_ONLY_TOOLS:
            continue
        schema = t.get("input_schema") or {"type": "object", "properties": {}}
        # Clean schema for Gemini function declaration compatibility
        cleaned_props = {}
        for prop_name, prop_spec in schema.get("properties", {}).items():
            cleaned_props[prop_name] = {
                "type": prop_spec.get("type", "string").upper(),
                "description": prop_spec.get("description", prop_spec.get("title", "")),
            }
        decl = {
            "name": name,
            "description": t.get("description", ""),
            "parameters": {
                "type": "OBJECT",
                "properties": cleaned_props,
            },
        }
        if "required" in schema:
            decl["parameters"]["required"] = schema["required"]
        declarations.append(decl)
    return declarations


# ---------------------------------------------------------------------------
# Main Chat Turn Integration
# ---------------------------------------------------------------------------

def chat_turn(
    message: str,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Execute a single conversational turn with Google Gemini AI and remote MCP.

    Returns:
        dict: {"status": "ok"|"error", "response": str, "tools_used": list[str]}
    """
    api_key = get_gemini_api_key()
    if not api_key:
        return {
            "status": "error",
            "response": (
                "⚠️ **GEMINI_API_KEY is not configured.**\n\n"
                "To enable Gemini AI responses, please set the `GEMINI_API_KEY` "
                "environment variable in your deployment environment or `.env` file."
            ),
            "tools_used": [],
        }

    gateway_token = get_mcp_gateway_token()
    if not gateway_token:
        return {
            "status": "error",
            "response": (
                "⚠️ **MCP_GATEWAY_TOKEN is not configured.**\n\n"
                "Please configure the `MCP_GATEWAY_TOKEN` environment variable "
                "to authenticate Bearer requests to the remote NexusAI MCP server."
            ),
            "tools_used": [],
        }

    remote_url = get_remote_mcp_url()
    model_name = model or DEFAULT_MODEL
    tools_used: list[str] = []

    # -------------------------------------------------------------------------
    # Gemini Function Calling with Store MCP Tools (Fast & Serverless Safe)
    # -------------------------------------------------------------------------
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Convert 13 read-only tool definitions to Gemini function declarations
        function_declarations = get_gemini_tool_declarations()

        # Build contents from history
        contents: list[types.Content] = []
        if history:
            for item in history[-6:]:
                role = "user" if item.get("role") == "user" else "model"
                text_val = str(item.get("content", "")).strip()
                if text_val:
                    contents.append(
                        types.Content(
                            role=role,
                            parts=[types.Part.from_text(text=text_val)],
                        )
                    )
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=message)])
        )

        # Multi-turn tool execution loop (up to 4 turns)
        max_turns = 4
        for _ in range(max_turns):
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=[types.Tool(function_declarations=function_declarations)],
                ),
            )

            # Check if model requested any function calls
            has_func_call = False
            candidate = response.candidates[0] if response.candidates else None
            if candidate and candidate.content and candidate.content.parts:
                for part in candidate.content.parts:
                    if part.function_call:
                        has_func_call = True
                        fn_name = part.function_call.name
                        fn_args = dict(part.function_call.args or {})
                        tools_used.append(fn_name)

                        # Execute tool immediately
                        tool_result = execute_tool(
                            name=fn_name,
                            arguments=fn_args,
                            endpoint=remote_url,
                            token=gateway_token,
                        )

                        # Append model function call and function response to contents
                        contents.append(candidate.content)
                        contents.append(
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_function_response(
                                        name=fn_name,
                                        response={"result": tool_result},
                                    )
                                ],
                            )
                        )
                        break

            if not has_func_call:
                reply = response.text or "Store tools processed your request."
                return {
                    "status": "ok",
                    "response": reply.strip(),
                    "tools_used": list(dict.fromkeys(tools_used)),
                    "model": model_name,
                }

    except Exception as exc:
        logger.error("Gemini function-calling loop error: %s", exc)
        return {
            "status": "error",
            "response": f"Failed to generate AI response: {exc}",
            "tools_used": tools_used,
        }

    return {
        "status": "error",
        "response": "Exceeded maximum tool calling iterations.",
        "tools_used": tools_used,
    }

