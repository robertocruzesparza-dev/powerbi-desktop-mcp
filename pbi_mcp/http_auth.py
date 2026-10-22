"""Bearer-token protection for the HTTP (Microsoft 365 Copilot) transport.

Why this exists: Copilot can only reach an MCP server over public HTTPS, so the
server is published through a tunnel that must allow anonymous connections
(Microsoft's dev-tunnel guidance says so). Anything that can reach the tunnel
can then reach a tool that reads this PC's Power BI data. The tunnel is not the
security boundary -- this module is.

Every request except /healthz must carry an Entra ID access token that:
  * is signed by the tenant's keys (RS256 only, keys fetched from the tenant's JWKS),
  * is unexpired and issued by that tenant (v1 or v2 issuer form, and `tid`),
  * has one of the configured audiences (the Application ID URI / client id),
  * is a delegated (user) token carrying the required scope, and
  * belongs to a user on the allow-list.

Copilot obtains that token through its Microsoft Entra SSO auth config
(runtime auth type `OAuthPluginVault`); see copilot/README.md.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import anyio.to_thread
import jwt

log = logging.getLogger("pbi_mcp.http")

HEALTH_PATH = "/healthz"
_IDENTITY_CLAIMS = ("oid", "preferred_username", "upn", "unique_name", "email")


class AuthError(Exception):
    """Raised for a request that must be refused. `status` is 401 (no/invalid
    token) or 403 (valid token, not allowed here)."""

    def __init__(self, status: int, code: str, description: str):
        super().__init__(description)
        self.status = status
        self.code = code
        self.description = description


@dataclass(frozen=True)
class AuthConfig:
    tenant_id: str
    audiences: tuple[str, ...]
    allowed_users: frozenset[str] = field(default_factory=frozenset)  # lower-case UPN / email / object id
    allow_any_tenant_user: bool = False
    required_scope: str = "access_as_user"  # "" disables the scope check
    leeway_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        if not self.audiences:
            raise ValueError("at least one audience (Application ID URI or client id) is required")
        if not self.allowed_users and not self.allow_any_tenant_user:
            raise ValueError(
                "allowed_users is empty. This server exposes the host PC's Power BI data, so name the "
                "users who may call it (or set allow_any_tenant_user to accept everyone in the tenant)."
            )


class EntraTokenVerifier:
    """Validates Entra ID access tokens. `jwks_client` is injectable for tests."""

    def __init__(self, config: AuthConfig, jwks_client: Any | None = None):
        self.config = config
        self._jwks = jwks_client or jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{config.tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )
        self._issuers = (
            f"https://login.microsoftonline.com/{config.tenant_id}/v2.0",
            f"https://sts.windows.net/{config.tenant_id}/",
        )

    def verify(self, token: str) -> dict[str, Any]:
        """Return the token's claims, or raise AuthError. Blocking (may fetch JWKS)."""
        cfg = self.config
        try:
            if jwt.get_unverified_header(token).get("alg") != "RS256":
                raise AuthError(401, "invalid_token", "unsupported signing algorithm")
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(cfg.audiences),
                leeway=cfg.leeway_seconds,
                options={"require": ["exp", "iss", "aud"], "verify_iss": False},
            )
        except AuthError:
            raise
        except jwt.PyJWTError as exc:
            # Never echo the token; the exception text names the failed check only.
            raise AuthError(401, "invalid_token", type(exc).__name__) from exc

        if claims.get("iss") not in self._issuers or claims.get("tid") != cfg.tenant_id:
            raise AuthError(401, "invalid_token", "token was not issued by the expected tenant")

        if cfg.required_scope and cfg.required_scope not in str(claims.get("scp", "")).split():
            raise AuthError(403, "insufficient_scope", f"token lacks the '{cfg.required_scope}' scope")

        if not cfg.allow_any_tenant_user:
            identities = {str(claims[c]).lower() for c in _IDENTITY_CLAIMS if claims.get(c)}
            if not identities & cfg.allowed_users:
                raise AuthError(403, "access_denied", "this user is not allowed to use this server")
        return claims


def _identity(claims: dict[str, Any]) -> str:
    return str(claims.get("preferred_username") or claims.get("upn") or claims.get("oid") or "unknown")


ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


class BearerAuthMiddleware:
    """ASGI wrapper: 401/403 before the MCP app ever sees an unauthorised request."""

    def __init__(self, app: ASGIApp, verifier: EntraTokenVerifier, realm: str = "powerbi-desktop-mcp"):
        self.app = app
        self.verifier = verifier
        self.realm = realm

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":  # lifespan (and anything else) goes straight through
            await self.app(scope, receive, send)
            return

        if scope["path"] == HEALTH_PATH:
            await self._json(send, 200, {"status": "ok"})
            return

        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            await self._refuse(send, AuthError(401, "", "missing bearer token"), include_error=False)
            return
        try:
            claims = await anyio.to_thread.run_sync(self.verifier.verify, token.strip())
        except AuthError as exc:
            log.warning("refused %s %s: %s (%s)", scope["method"], scope["path"], exc.code, exc.description)
            await self._refuse(send, exc)
            return
        except Exception:  # noqa: BLE001 -- e.g. JWKS endpoint unreachable: fail closed
            log.exception("token validation failed unexpectedly")
            await self._refuse(send, AuthError(401, "invalid_token", "token could not be validated"))
            return

        log.info("%s %s by %s", scope["method"], scope["path"], _identity(claims))
        await self.app(scope, receive, send)

    async def _refuse(self, send: Callable, exc: AuthError, include_error: bool = True) -> None:
        challenge = f'Bearer realm="{self.realm}"'
        if include_error and exc.code:
            challenge += f', error="{exc.code}"'
        await self._json(send, exc.status, {"error": exc.code or "unauthorized", "error_description": exc.description},
                         extra=[(b"www-authenticate", challenge.encode())])

    @staticmethod
    async def _json(send: Callable, status: int, body: dict, extra: list[tuple[bytes, bytes]] | None = None) -> None:
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()),
                   (b"cache-control", b"no-store"), *(extra or [])]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


def parse_user_list(raw: str) -> frozenset[str]:
    """`a@x.org, b@x.org;c` -> lower-cased set."""
    return frozenset(p.strip().lower() for p in raw.replace(";", ",").split(",") if p.strip())
