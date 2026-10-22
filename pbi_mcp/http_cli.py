"""Command line for the Microsoft 365 Copilot build (`powerbi-desktop-mcp-http`): serves the Power BI MCP
tools over Streamable HTTP on 127.0.0.1 so a tunnel can publish them.

The Claude plugin (run_server.py, stdio) is unaffected.

    powerbi-desktop-mcp-http --tenant-id <tenant-guid> \\
        --audience api://<app-guid> --audience <app-guid> \\
        --allowed-user first.last@yourdomain.org \\
        --public-host <tunnel-host>

Every option can also come from an environment variable (PBI_MCP_TENANT_ID,
PBI_MCP_AUDIENCES, PBI_MCP_ALLOWED_USERS, PBI_MCP_PUBLIC_HOSTS,
PBI_MCP_REQUIRED_SCOPE, PBI_MCP_PORT); lists are comma-separated.
"""
import argparse
import logging
import os
import sys

from .http_auth import AuthConfig, EntraTokenVerifier, parse_user_list


def _csv(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Power BI Desktop MCP server for Microsoft 365 Copilot (HTTP, 127.0.0.1 only).")
    p.add_argument("--tenant-id", default=os.environ.get("PBI_MCP_TENANT_ID", ""), help="Microsoft Entra tenant (directory) id.")
    p.add_argument("--audience", action="append", default=_csv("PBI_MCP_AUDIENCES"),
                   help="Accepted token audience: the Application ID URI and/or client id of the Entra app that secures this server. Repeatable.")
    p.add_argument("--allowed-user", action="append", default=_csv("PBI_MCP_ALLOWED_USERS"),
                   help="UPN, email or object id allowed to call the server. Repeatable. Required unless --allow-any-tenant-user.")
    p.add_argument("--allow-any-tenant-user", action="store_true", help="Accept every user in the tenant (not recommended: they can read this PC's Power BI data).")
    p.add_argument("--required-scope", default=os.environ.get("PBI_MCP_REQUIRED_SCOPE", "access_as_user"),
                   help="Delegated scope the token must carry ('' to skip). Default: access_as_user.")
    p.add_argument("--public-host", action="append", default=_csv("PBI_MCP_PUBLIC_HOSTS"),
                   help="Host header the tunnel presents, e.g. abc123-8765.usw2.devtunnels.ms. Repeatable.")
    p.add_argument("--port", type=int, default=int(os.environ.get("PBI_MCP_PORT", "8765")))
    p.add_argument("--allow-anonymous", action="store_true",
                   help="DEVELOPMENT ONLY: no authentication. Never combine with a tunnel and real data.")
    return p.parse_args(argv)


def build_from_args(args: argparse.Namespace):
    from .http_app import build_app

    if args.allow_anonymous:
        logging.getLogger("pbi_mcp.http").warning("AUTHENTICATION IS OFF (--allow-anonymous). Do not expose this port.")
        return build_app(None, args.public_host)
    config = AuthConfig(
        tenant_id=args.tenant_id,
        audiences=tuple(args.audience),
        allowed_users=parse_user_list(",".join(args.allowed_user)),
        allow_any_tenant_user=args.allow_any_tenant_user,
        required_scope=args.required_scope,
    )
    return build_app(EntraTokenVerifier(config), args.public_host)


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = parse_args(argv)
    try:
        app = build_from_args(args)
    except ValueError as exc:
        sys.exit(f"error: {exc}")
    log = logging.getLogger("pbi_mcp.http")
    log.info("Serving MCP at http://127.0.0.1:%d/mcp (bind: loopback only; publish it with a tunnel)", args.port)
    if not args.public_host:
        log.warning("No --public-host given: requests arriving through a tunnel will be refused (421) until you add the tunnel's host name.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
