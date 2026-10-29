"""OAuth discovery metadata builders shared by the Flask app (serves the
reserved /.well-known paths on Vercel, which cannot be rewritten) and the
gateway function (serves the same documents under /mcp_gateway/* for local
use and as a fallback).

Pure functions — no imports of app code, no filesystem access, no secrets.
Keep in sync via tests/test_gateway.py::test_metadata_sources_agree.
"""

GATEWAY_PREFIX = "/mcp_gateway"
SCOPES = ["mcp:read"]
GRANT_TYPES = ["authorization_code", "refresh_token"]
CODE_CHALLENGE_METHODS = ["S256"]
TOKEN_ENDPOINT_AUTH_METHODS = ["none"]


def normalize_origin(request_url_root: str) -> str:
    """Flask's url_root ('https://host/') -> 'https://host'."""
    return request_url_root.rstrip("/")


def protected_resource_metadata(origin: str) -> dict:
    """RFC 9728 protected-resource metadata (resource = primary MCP URL)."""
    return {
        "resource": f"{origin}{GATEWAY_PREFIX}",
        "resource_name": "NexusAI Store (read-only demo gateway)",
        "authorization_servers": [origin],
        "scopes_supported": SCOPES,
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(origin: str) -> dict:
    """RFC 8414 authorization-server metadata (minimal single-user AS)."""
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}{GATEWAY_PREFIX}/authorize",
        "token_endpoint": f"{origin}{GATEWAY_PREFIX}/token",
        "registration_endpoint": f"{origin}{GATEWAY_PREFIX}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": GRANT_TYPES,
        "code_challenge_methods_supported": CODE_CHALLENGE_METHODS,
        "token_endpoint_auth_methods_supported": TOKEN_ENDPOINT_AUTH_METHODS,
        "scopes_supported": SCOPES,
    }
