"""Tests for Google Gemini AI + NexusAI Remote MCP Integration."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

import app
import gemini_mcp


@pytest.fixture
def client():
    app.app.config["TESTING"] = True
    with app.app.test_client() as client:
        yield client


def test_tool_surface_and_declarations():
    """Verify that all 13 read-only store tools are exposed and converted."""
    assert len(gemini_mcp.ALLOWED_READ_ONLY_TOOLS) == 13
    decls = gemini_mcp.get_gemini_tool_declarations()
    assert len(decls) == 13
    decl_names = {d["name"] for d in decls}
    assert decl_names == gemini_mcp.ALLOWED_READ_ONLY_TOOLS
    assert "create_sale" not in decl_names
    assert "add_product" not in decl_names


def test_chat_turn_without_gemini_key():
    """Missing GEMINI_API_KEY must return informative message without crashing."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
        res = gemini_mcp.chat_turn("What products do we have?")
        assert res["status"] == "error"
        assert "GEMINI_API_KEY is not configured" in res["response"]


def test_chat_turn_without_gateway_token():
    """Missing MCP_GATEWAY_TOKEN must return clear configuration guidance."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-gemini-key", "MCP_GATEWAY_TOKEN": ""}, clear=False):
        res = gemini_mcp.chat_turn("What products do we have?")
        assert res["status"] == "error"
        assert "MCP_GATEWAY_TOKEN is not configured" in res["response"]


def test_execute_tool_blocks_write_tools():
    """Write tools must never be executed via the read-only MCP wrapper."""
    res = gemini_mcp.execute_tool("create_sale", {"product_id": 1, "qty": 5})
    assert "not an authorized read-only tool" in res

    res2 = gemini_mcp.execute_tool("add_product", {"name": "Test Drink", "sell_price": 50})
    assert "not an authorized read-only tool" in res2


def test_execute_tool_local_fallback():
    """Read-only tools execute reliably via local fallback if remote is not reachable."""
    res = gemini_mcp.execute_tool("get_dashboard_summary", {})
    assert isinstance(res, str)
    assert len(res) > 0


def test_chat_endpoint_get(client):
    """GET /chat returns the AI assistant interface."""
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert b"Google Gemini AI" in resp.data
    assert b"Remote MCP" in resp.data
    assert b"Low Stock Alerts" in resp.data


def test_api_chat_empty_message(client):
    """POST /api/chat with empty input returns HTTP 400."""
    resp = client.post("/api/chat", json={"message": "   "})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "error"


def test_api_chat_flow(client):
    """POST /api/chat routes message through gemini_mcp and returns JSON."""
    with patch("gemini_mcp.chat_turn") as mock_turn:
        mock_turn.return_value = {
            "status": "ok",
            "response": "Here is the stock summary for Cola.",
            "tools_used": ["get_low_stock"],
            "model": "gemini-3.8-flash",
        }
        resp = client.post(
            "/api/chat",
            json={"message": "Show low stock products", "history": []},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "Cola" in data["response"]
        assert data["tools_used"] == ["get_low_stock"]

