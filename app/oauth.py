"""OAuth 2.1 authorization server for the Claude connector.

Claude registers itself (dynamic client registration), sends the person to our
/oauth/login page, and gets tokens tied to that person's ledger login and role.
Tokens are stored hashed.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import db
from .auth import sha

ACCESS_TTL = 8 * 3600
REFRESH_TTL = 180 * 86400
CODE_TTL = 300
PENDING_TTL = 900


def _ts(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class LedgerOAuthProvider:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    # -- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute("SELECT info FROM oauth_clients WHERE client_id=%s", (client_id,))
            r = await cur.fetchone()
        return OAuthClientInformationFull.model_validate(r["info"]) if r else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with db.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO oauth_clients (client_id, info) VALUES (%s,%s) "
                "ON CONFLICT (client_id) DO UPDATE SET info=EXCLUDED.info",
                (client_info.client_id, client_info.model_dump_json()),
            )
            await conn.commit()

    # -- authorize: park the request and send the person to our login page
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        pid = secrets.token_urlsafe(18)
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM oauth_pending WHERE expires_at < now()")
            await conn.execute(
                "INSERT INTO oauth_pending (id, client_id, params, expires_at) VALUES (%s,%s,%s,%s)",
                (pid, client.client_id, params.model_dump_json(), _ts(PENDING_TTL)),
            )
            await conn.commit()
        return f"{self.base_url}/oauth/login?req={pid}"

    async def pending(self, pid: str) -> tuple[str, AuthorizationParams] | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT client_id, params FROM oauth_pending WHERE id=%s AND expires_at > now()", (pid,)
            )
            r = await cur.fetchone()
        if not r:
            return None
        return r["client_id"], AuthorizationParams.model_validate(r["params"])

    async def approve(self, pid: str, user_id: int) -> str:
        """Called by our login page once the person signs in. Returns the redirect back to Claude."""
        found = await self.pending(pid)
        if not found:
            raise ValueError("This sign-in request expired. Go back to Claude and click Connect again.")
        client_id, params = found
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(user_id),
        )
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM oauth_pending WHERE id=%s", (pid,))
            await conn.execute(
                "INSERT INTO oauth_codes (code, data, expires_at) VALUES (%s,%s,%s)",
                (sha(code), ac.model_dump_json(), _ts(CODE_TTL)),
            )
            await conn.commit()
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def deny(self, pid: str) -> str | None:
        found = await self.pending(pid)
        if not found:
            return None
        _, params = found
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM oauth_pending WHERE id=%s", (pid,))
            await conn.commit()
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # -- codes
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT data FROM oauth_codes WHERE code=%s AND expires_at > now()", (sha(authorization_code),)
            )
            r = await cur.fetchone()
        if not r:
            return None
        ac = AuthorizationCode.model_validate(r["data"])
        # The SDK compares against the raw code it was given.
        ac.code = authorization_code
        return ac if ac.client_id == client.client_id else None

    async def _issue(self, conn, client_id: str, user_id: int, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        await conn.execute(
            "INSERT INTO oauth_tokens (token, kind, client_id, user_id, scopes, resource, pair, expires_at) "
            "VALUES (%s,'access',%s,%s,%s,%s,%s,%s), (%s,'refresh',%s,%s,%s,%s,%s,%s)",
            (
                sha(access), client_id, user_id, scopes, resource, sha(refresh), _ts(ACCESS_TTL),
                sha(refresh), client_id, user_id, scopes, resource, sha(access), _ts(REFRESH_TTL),
            ),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes) if scopes else None,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "DELETE FROM oauth_codes WHERE code=%s RETURNING data", (sha(authorization_code.code),)
                )
                if not await cur.fetchone():
                    raise TokenError("invalid_grant", "authorization code already used")
                await conn.execute("DELETE FROM oauth_tokens WHERE expires_at < now()")
                return await self._issue(
                    conn, client.client_id, int(authorization_code.subject),
                    authorization_code.scopes, authorization_code.resource,
                )

    # -- refresh
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT t.client_id, t.user_id, t.scopes, t.resource, t.expires_at FROM oauth_tokens t "
                "JOIN users u ON u.id=t.user_id AND u.active "
                "WHERE t.token=%s AND t.kind='refresh' AND t.expires_at > now()",
                (sha(refresh_token),),
            )
            r = await cur.fetchone()
        if not r or r["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=r["client_id"], scopes=list(r["scopes"]),
            expires_at=int(r["expires_at"].timestamp()), resource=r["resource"], subject=str(r["user_id"]),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]) -> OAuthToken:
        async with db.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "DELETE FROM oauth_tokens WHERE token=%s AND kind='refresh' RETURNING pair",
                    (sha(refresh_token.token),),
                )
                r = await cur.fetchone()
                if not r:
                    raise TokenError("invalid_grant", "refresh token already used")
                await conn.execute("DELETE FROM oauth_tokens WHERE token=%s", (r["pair"],))
                return await self._issue(
                    conn, client.client_id, int(refresh_token.subject),
                    scopes or refresh_token.scopes, refresh_token.resource,
                )

    # -- access
    async def load_access_token(self, token: str) -> AccessToken | None:
        async with db.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT t.client_id, t.user_id, t.scopes, t.resource, t.expires_at FROM oauth_tokens t "
                "JOIN users u ON u.id=t.user_id AND u.active "
                "WHERE t.token=%s AND t.kind='access' AND t.expires_at > now()",
                (sha(token),),
            )
            r = await cur.fetchone()
        if not r:
            return None
        return AccessToken(
            token=token, client_id=r["client_id"], scopes=list(r["scopes"]),
            expires_at=int(r["expires_at"].timestamp()), resource=r["resource"], subject=str(r["user_id"]),
        )

    async def revoke_token(self, token) -> None:
        async with db.pool.connection() as conn:
            cur = await conn.execute("DELETE FROM oauth_tokens WHERE token=%s RETURNING pair", (sha(token.token),))
            r = await cur.fetchone()
            if r and r["pair"]:
                await conn.execute("DELETE FROM oauth_tokens WHERE token=%s", (r["pair"],))
            await conn.commit()
