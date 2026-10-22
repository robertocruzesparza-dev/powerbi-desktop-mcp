"""ASGI app for the Microsoft 365 Copilot transport: MCP over Streamable HTTP,
behind Entra bearer-token validation. See http_auth.py for why the auth layer
is mandatory."""
from __future__ import annotations

from mcp.server.transport_security import TransportSecuritySettings

from .copilot_server import mcp
from .http_auth import BearerAuthMiddleware, EntraTokenVerifier

MCP_PATH = "/mcp"
LOCAL_HOSTS = ["localhost:*", "127.0.0.1:*", "[::1]:*"]


def build_app(verifier: EntraTokenVerifier | None, public_hosts: list[str] | None = None):
    """Return the ASGI app. `verifier=None` builds it with NO authentication
    (only run_http_server.py's --allow-anonymous does that).

    `public_hosts` are the Host header values the tunnel presents (e.g.
    `abc123-8765.usw2.devtunnels.ms`); requests with any other Host are
    refused (DNS-rebinding protection). Stateless + plain-JSON responses keep
    the server working through tunnels and proxies that buffer event streams.
    """
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOCAL_HOSTS, *(public_hosts or [])],
        allowed_origins=[],
    )
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )
    return BearerAuthMiddleware(app, verifier) if verifier else app
