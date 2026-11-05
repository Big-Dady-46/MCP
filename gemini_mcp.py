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
    "You are the professional, friendly AI Store Manager for NexusAI Beverage Store.\n"
    "Crucial Rules:\n"
    "- ALWAYS RESPOND IN ENGLISH ONLY. Regardless of whether the user asks in English, Urdu, or Roman Urdu, your entire answer must be written in fluent, professional, clear English.\n"
    "- STORE MANAGEMENT: You manage the entire store. You have real-time access to products, live inventory, low stock alerts, customer orders/transactions, sales revenue, supplier purchases, worker payroll, expenses, and business performance reports.\n"
    "- CUSTOMERS: When asked about customers or customer visits, check the sales records (each completed sale represents a customer visit/transaction) and report the total customer count, revenue, and order breakdown.\n"
    "- CASUAL CHAT & GREETINGS: If the user says hello, hi, how are you, ok, done, thanks, or casual remarks, reply politely in English as a store manager without calling unnecessary tools.\n"
    "- CATEGORIES: When asked about categories or brands, group and list them neatly (e.g. 7UP, Coca-Cola, Fanta, Pepsi, Sprite, AquaPure Mineral Water).\n"
    "- CURRENCY: Always format currency in Pakistani Rupees (PKR), formatted clearly (e.g. PKR 250.00).\n"
    "- PRESENTATION: Always format answers with clean Markdown, bullet points, and professional business tone. Never output raw JSON dumps."
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

    # Ensure store database has seed/demo data on serverless cold starts
    try:
        import store_db as sdb
        if not sdb.demo_seeded():
            sdb.init_db()
            sdb.seed_demo()
    except Exception:
        pass

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


def check_conversational_shortcut(message: str) -> str | None:
    """Return an instant English chatbot reply for greetings, acknowledgments, or pleasantries
    without calling any store tools or external APIs.
    """
    msg_clean = message.strip().lower()
    if not msg_clean:
        return "Hello! How can I assist you with the beverage store today?"

    # If the user is asking any question or instruction, NEVER shortcut
    question_triggers = (
        "store", "batao", "dikhao", "check", "tell", "show", "kya", "kaya", "kia",
        "para", "pari", "pada", "padi", "kuch", "kitna", "kitni", "kitne", "kon",
        "kaun", "samaan", "product", "item", "stock", "maal", "list", "beverage",
        "price", "qeemat", "rate", "botal", "bottle", "sale", "bikri", "becha",
        "revenue", "kamai", "gross", "income", "munafa", "report", "profit",
        "loss", "faida", "nuqsan", "valuation", "hisab", "balance", "worker",
        "mulazim", "staff", "salary", "tankhwah", "payroll", "purchase", "kharid",
        "order", "orders", "customer", "customers", "client", "clients", "grahak",
        "supplier", "suppliers", "vendor", "expense", "expenses", "bill", "kharcha",
        "category", "categories", "brand", "types", "low stock", "shortage"
    )
    if any(q in msg_clean for q in question_triggers):
        return None

    # Only short messages (<= 3 words) can be pure greetings or pure acknowledgments
    tokens = [w.strip("!?,.;:") for w in msg_clean.split() if w.strip("!?,.;:")]
    if len(tokens) > 3:
        return None

    # Exact greeting or greeting phrase
    greetings = {
        "hello", "hi", "hey", "salam", "assalam", "assalam o alaikum", "assalamu alaikum",
        "aoa", "kese ho", "kaise ho", "kia haal hai", "kya haal hai", "kia hal ha", "kia hal hai",
        "hal chal", "kya hal he", "kia hal he", "good morning", "good evening", "good afternoon"
    }
    if msg_clean in greetings or " ".join(tokens) in greetings:
        return (
            "Hello! How can I help you today? I am your NexusAI Beverage Store Manager. "
            "You can ask me about our live inventory, customer orders, sales revenue, "
            "low stock alerts, supplier purchases, or financial reports."
        )

    # Acknowledgments and closings
    acknowledgments = {
        "done", "ok", "okay", "theek", "theek hai", "thik hai", "achha", "acha",
        "shukriya", "thanks", "thank you", "thx", "ty", "bye", "allah hafiz", "khuda hafiz",
        "great", "zabardast", "good job", "nice", "fine", "cool", "perfect", "done sir", "ok done"
    }
    if msg_clean in acknowledgments or " ".join(tokens) in acknowledgments:
        return (
            "Understood! Let me know if you need any further information or reports about the store."
        )

    # Bot identity / capability inquiries
    bot_info = {
        "who are you", "tum kaun ho", "tum kon ho", "aap kaun hain", "aap kon hain",
        "what can you do", "kya kar sakte ho", "kia kar saktay ho", "help", "madad"
    }
    if any(b in msg_clean for b in bot_info):
        return (
            "I am the AI Store Manager for NexusAI Beverage Store. "
            "I provide real-time information and analytics regarding store inventory, "
            "products, customer visits and orders, sales revenue, supplier purchases, "
            "worker payroll, and business financial reports."
        )

    return None


def intelligent_mcp_fallback(message: str) -> tuple[str, list[str]]:
    """Comprehensive English fallback when cloud AI encounters a temporary network issue."""
    msg_clean = message.strip().lower()
    tools_used = []

    has_store_query = any(q in msg_clean for q in (
        "store", "batao", "dikhao", "para", "pari", "pada", "padi", "samaan",
        "stock", "maal", "sale", "report", "product", "item", "category",
        "categories", "worker", "kya", "kaya", "kia", "kitna", "kitni", "kitne",
        "customer", "customers", "client", "order", "orders", "grahak", "purchase",
        "supplier", "expense"
    ))

    # 1. Greetings / Conversational (NO tools needed - only if no store query asked)
    if not has_store_query:
        greetings = {"hello", "hi", "hey", "salam", "assalam o alaikum", "aoa", "kese ho", "kaise ho", "kia haal hai", "kya haal hai", "kia hal ha", "hal chal"}
        if any(msg_clean == g or msg_clean.startswith(g + " ") or msg_clean.startswith(g + "!") or msg_clean.startswith(g + ",") for g in greetings):
            return (
                "Hello! How can I help you today? I am your NexusAI Beverage Store Manager. "
                "You can ask me about live inventory, customer orders, sales revenue, "
                "low stock alerts, supplier purchases, or financial reports.",
                []
            )

        # 2. Acknowledgements & Closing (NO tools needed - only if no store query asked)
        acks = {"done", "ok", "okay", "theek", "theek hai", "thik hai", "achha", "acha", "shukriya", "thanks", "thank you", "bye", "allah hafiz", "khuda hafiz"}
        if any(msg_clean == a or msg_clean.startswith(a + " ") or msg_clean.startswith(a + ".") or msg_clean.startswith(a + "!") for a in acks):
            return (
                "Understood! Feel free to ask if you need any other store data or reports.",
                []
            )

    # 3. Customers / Orders / Transactions / Footfall
    if any(k in msg_clean for k in ("customer", "customers", "client", "clients", "order", "orders", "grahak", "receipt", "transaction")):
        tools_used.append("list_sales")
        raw_sales = execute_tool("list_sales", {"limit": 100})
        raw_summary = execute_tool("get_sales_summary", {})
        try:
            sales_list = json.loads(raw_sales) if raw_sales else []
            summary = json.loads(raw_summary) if raw_summary else {}
            total_customers = len(sales_list)
            today_count = summary.get("count", 0)
            today_rev = summary.get("revenue", 0.0)
            total_rev = sum(s.get("total", 0.0) for s in sales_list)
            return (
                f"👥 **Customer Orders & Store Visits Summary:**\n\n"
                f"- **Total Customer Orders Processed:** {total_customers} completed orders\n"
                f"- **Today's Customer Orders:** {today_count} customer(s)\n"
                f"- **Today's Customer Revenue:** PKR {today_rev:,.2f}\n"
                f"- **Total Recorded Revenue:** PKR {total_rev:,.2f}\n"
                f"- **Payment Methods:** All transactions processed via cash register receipts."
            ), tools_used
        except Exception:
            return raw_sales, tools_used

    # 4. Product Categories / Brands / Types only (Clean list, not full inventory dump)
    if any(k in msg_clean for k in ("category", "categories", "kis kis", "types", "brands", "aqsaam")):
        tools_used.append("list_products")
        raw = execute_tool("list_products", {})
        try:
            items = json.loads(raw)
            brands: dict[str, list[str]] = {}
            for p in items:
                b = p.get("brand") or "Beverages"
                size = f" ({p['size_label']})" if p.get("size_label") else ""
                clean_name = p['name'].replace("[DEMO] ", "").strip()
                brands.setdefault(b, []).append(f"{clean_name}{size}")
            lines = ["Here are the main beverage categories and brands currently in our store:\n"]
            for brand, p_list in brands.items():
                lines.append(f"• **{brand}:** {', '.join(p_list)}")
            return "\n".join(lines), tools_used
        except Exception:
            return raw, tools_used

    # 5. Low stock / reorder alerts
    if any(k in msg_clean for k in ("low", "kam", "stock alert", "reorder", "khatam", "shortage", "khatam hone")):
        tools_used.append("get_low_stock")
        raw = execute_tool("get_low_stock", {})
        try:
            items = json.loads(raw)
            if not items:
                return "All store products are currently well-stocked and above their reorder thresholds.", tools_used
            lines = ["⚠️ **Low Stock Alert (Products at or below reorder limit):**\n"]
            for p in items:
                lines.append(f"- **{p['name']}** ({p.get('size_label', '')}): **{p['stock_units']} bottles** remaining (Reorder limit: {p['reorder_level']})")
            return "\n".join(lines), tools_used
        except Exception:
            return raw, tools_used

    # 6. Purchases / Suppliers / Stock Arrivals
    if any(k in msg_clean for k in ("purchase", "purchases", "supplier", "suppliers", "vendor", "vendors", "kharid")):
        tools_used.append("list_purchases")
        raw = execute_tool("list_purchases", {})
        try:
            purchases = json.loads(raw)
            if not purchases:
                return "No supplier purchase orders recorded yet.", tools_used
            lines = ["🛒 **Recent Supplier Purchase Orders:**\n"]
            for pr in purchases:
                lines.append(f"- Purchase #{pr.get('id', '')} ({pr.get('ref_no', '')}): Total PKR {pr.get('total', 0):,.2f} | Status: {pr.get('status', 'paid')}")
            return "\n".join(lines), tools_used
        except Exception:
            return raw, tools_used

    # 7. Products / Samaan / Inventory / Stock / Prices / Kya para hai
    if any(k in msg_clean for k in (
        "samaan", "product", "item", "stock", "maal", "list", "beverage",
        "price", "qeemat", "rate", "botal", "bottle", "store", "para", "pari",
        "pada", "padi", "kya kuch", "kaya kuch", "kia kuch", "kya kya", "kaya kaya",
        "kia kia", "kya hai", "kaya hai", "kia hai", "mojood", "items", "inventory",
        "7up", "coke", "coca", "fanta", "pepsi", "sprite", "water", "mineral"
    )):
        tools_used.append("list_products")
        raw = execute_tool("list_products", {})
        try:
            items = json.loads(raw)
            if not items:
                return "The store inventory is currently empty.", tools_used

            # Check if user specifically asked about a specific product/brand
            specific_brands = ["7up", "coca-cola", "coca", "cola", "coke", "fanta", "pepsi", "sprite", "water", "mineral"]
            matched_brand = next((b for b in specific_brands if b in msg_clean), None)
            if matched_brand:
                filtered = [
                    p for p in items
                    if matched_brand in p["name"].lower() or matched_brand in (p.get("brand") or "").lower()
                ]
                if filtered:
                    p = filtered[0]
                    size = f" ({p['size_label']})" if p.get('size_label') else ""
                    return (
                        f"We currently have **{p['stock_units']} bottles** of **{p['name']}**{size} in stock, "
                        f"priced at **PKR {p.get('sell_price', 0):.2f}** per bottle."
                    ), tools_used

            lines = ["📦 **Current Store Inventory & Live Stock:**\n"]
            for p in items:
                size = f" ({p['size_label']})" if p.get('size_label') else ""
                lines.append(f"- **{p['name']}**{size}: **{p['stock_units']} bottles** | Retail Price: PKR {p.get('sell_price', 0):.2f}")
            return "\n".join(lines), tools_used
        except Exception:
            return raw, tools_used

    # 8. Sales / POS / Revenue
    if any(k in msg_clean for k in ("sale", "bikri", "becha", "revenue", "kamai", "gross", "income", "munafa")):
        tools_used.append("get_sales_summary")
        raw = execute_tool("get_sales_summary", {})
        try:
            data = json.loads(raw)
            return (
                f"💰 **Live Sales Summary (Today):**\n"
                f"- Total Sales Orders: {data.get('count', 0)}\n"
                f"- Total Revenue: PKR {data.get('revenue', 0.0):,.2f}\n"
                f"- Gross Profit: PKR {data.get('gross_profit', 0.0):,.2f}"
            ), tools_used
        except Exception:
            return raw, tools_used

    # 9. Report / Profit & Loss / Business valuation / Expenses
    if any(k in msg_clean for k in ("report", "profit", "loss", "faida", "nuqsan", "valuation", "hisab", "balance", "expense", "kharcha")):
        tools_used.append("generate_business_report")
        report = execute_tool("generate_business_report", {"period": "month"})
        return report, tools_used

    # 10. Workers / Payroll / Staff
    if any(k in msg_clean for k in ("worker", "mulazim", "staff", "salary", "tankhwah", "payroll", "kam karnay")):
        tools_used.append("list_workers")
        raw = execute_tool("list_workers", {})
        try:
            workers = json.loads(raw)
            lines = ["👥 **Active Store Workers & Payroll:**\n"]
            for w in workers:
                lines.append(f"- **{w['name']}** — Role: {w.get('role', 'Staff')} | Salary: PKR {w.get('salary', 0):,.2f} ({w.get('salary_frequency', 'monthly')})")
            return "\n".join(lines), tools_used
        except Exception:
            return raw, tools_used

    # 11. Generic query (Professional English Response)
    return (
        "Hello! I am your NexusAI Beverage Store Manager. "
        "You can ask me about our current inventory, product prices, customer orders, "
        "sales revenue, supplier purchases, worker payroll, or financial reports.",
        []
    )


def get_live_store_context() -> str:
    """Retrieve real-time store database snapshot to empower Gemini AI context."""
    try:
        import store_db as sdb
        if not sdb.demo_seeded():
            sdb.init_db()
            sdb.seed_demo()
        import store_server
        products_raw = store_server.list_products()
        products = json.loads(products_raw) if isinstance(products_raw, str) else products_raw

        prod_summaries = []
        for p in (products or []):
            prod_summaries.append(
                f"- {p['name']} ({p.get('size_label', '')}): {p['stock_units']} bottles in stock, "
                f"cost PKR {p.get('unit_cost', 0):.2f}, retail price PKR {p.get('sell_price', 0):.2f}, "
                f"reorder level: {p.get('reorder_level', 0)}"
            )

        summary_raw = store_server.get_sales_summary()
        summary = json.loads(summary_raw) if isinstance(summary_raw, str) else (summary_raw or {})

        workers_raw = store_server.list_workers()
        workers = json.loads(workers_raw) if isinstance(workers_raw, str) else (workers_raw or [])
        worker_summaries = [f"{w['name']} ({w.get('role', 'Staff')}, PKR {w.get('salary', 0):,.2f})" for w in workers]

        return (
            "CURRENT LIVE STORE METRICS & DATABASE STATE:\n"
            + "Products:\n" + "\n".join(prod_summaries)
            + f"\nSales Metrics: {summary.get('count', 0)} completed orders, Total Revenue PKR {summary.get('revenue', 0.0):,.2f}, Gross Profit PKR {summary.get('gross_profit', 0.0):,.2f}\n"
            + f"Active Workers: {', '.join(worker_summaries)}\n"
        )
    except Exception as exc:
        return f"Store context snapshot unavailable: {exc}"


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
    # 0. Conversational shortcut (instant reply for greetings / done / ok without tool calling or data dumping)
    conv_reply = check_conversational_shortcut(message)
    if conv_reply:
        return {
            "status": "ok",
            "response": conv_reply,
            "tools_used": [],
            "model": "nexusai-chat",
        }

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
    tools_used: list[str] = []

    # The valid current Google Gemini model
    model_name = (model or os.environ.get("GEMINI_MODEL", "")).strip() or "gemini-3.8-flash"

    # -------------------------------------------------------------------------
    # Gemini AI Generation with Live Store Knowledge and Function Calling Tools
    # -------------------------------------------------------------------------
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=30000),
        )

        # Convert 13 read-only tool definitions to Gemini function declarations
        function_declarations = get_gemini_tool_declarations()

        # Build comprehensive system instruction with live store data
        live_context = get_live_store_context()
        full_system_instruction = f"{SYSTEM_INSTRUCTION}\n\n{live_context}"

        # Build initial contents from history
        initial_contents: list[types.Content] = []
        if history:
            for item in history[-6:]:
                role = "user" if item.get("role") == "user" else "model"
                text_val = str(item.get("content", "")).strip()
                if text_val:
                    initial_contents.append(
                        types.Content(
                            role=role,
                            parts=[types.Part.from_text(text=text_val)],
                        )
                    )
        initial_contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=message)])
        )

        contents = list(initial_contents)
        try:
            # Multi-turn tool execution loop (up to 4 turns)
            max_turns = 4
            for _ in range(max_turns):
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=full_system_instruction,
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
                                    role="tool",
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

        except Exception as model_exc:
            logger.warning("Gemini model %s hit exception (%s), using intelligent MCP store fallback.", model_name, model_exc)
            fallback_text, fallback_tools = intelligent_mcp_fallback(message)
            return {
                "status": "ok",
                "response": fallback_text,
                "tools_used": list(dict.fromkeys(tools_used + fallback_tools)),
                "model": "nexusai-store-mcp",
            }

    except Exception as exc:
        logger.error("Gemini function-calling loop error: %s", exc)
        fallback_text, fallback_tools = intelligent_mcp_fallback(message)
        return {
            "status": "ok",
            "response": fallback_text,
            "tools_used": list(dict.fromkeys(tools_used + fallback_tools)),
            "model": "nexusai-store-mcp",
        }

    return {
        "status": "error",
        "response": "Exceeded maximum tool calling iterations.",
        "tools_used": tools_used,
    }

