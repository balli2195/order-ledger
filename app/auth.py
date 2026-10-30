"""Logins: password hashing, session cookies, invites, roles."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import db

SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(32)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ENGINE_KEY = os.environ.get("ENGINE_KEY", "")
COOKIE = "ol_session"
SESSION_DAYS = 30
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "1") == "1"

_signer = URLSafeTimedSerializer(SECRET_KEY, salt="ol-session")

ROLES = ("owner", "editor", "viewer")
ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}

# Tabs a person can be given. Owners always see every tab; everyone else sees only the tabs ticked for them.
TABS = {
    "leads": ("Leads", "/leads"),
    "orders": ("Orders", "/"),
    "stores": ("Stores & brands", "/stores"),
    "prepos": ("PrepOS", "/"),
    "inventory_health": ("Inventory health", "/inventory-health"),
}
PAGE_TABS = ("leads", "orders", "stores", "inventory_health")   # tabs that are their own page, in menu order


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    tabs: tuple = ()

    @property
    def label(self) -> str:
        return self.name or self.email

    def can(self, need: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[need]

    def can_tab(self, tab: str) -> bool:
        return self.role == "owner" or tab in (self.tabs or ())

    def allowed_tabs(self) -> list[str]:
        return [t for t in TABS if self.can_tab(t)]

    def home(self) -> str:
        """First page this person can open."""
        for t in PAGE_TABS:
            if self.can_tab(t):
                return TABS[t][1]
        return "/no-access"

    def public(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name, "role": self.role, "tabs": self.allowed_tabs()}


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(pw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def make_cookie(user_id: int, session_ver: int) -> str:
    return _signer.dumps({"u": user_id, "v": session_ver})


async def user_by_id(user_id: int) -> tuple[User, int] | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, name, role, tabs, session_ver FROM users WHERE id=%s AND active",
            (user_id,),
        )
        r = await cur.fetchone()
    if not r:
        return None
    return _user(r), r["session_ver"]


def _user(r) -> User:
    return User(r["id"], r["email"], r["name"], r["role"], tuple(r.get("tabs") or ()))


async def current_user(request: Request) -> User | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        data = _signer.loads(raw, max_age=SESSION_DAYS * 86400)
    except BadSignature:
        return None
    found = await user_by_id(int(data.get("u", 0)))
    if not found:
        return None
    user, ver = found
    if ver != data.get("v"):
        return None
    return user


async def require(request: Request, role: str = "viewer", tab: str | tuple | None = None) -> User:
    """tab: the tab this data belongs to (or a tuple: any one of them is enough)."""
    user = await current_user(request)
    if not user:
        raise HTTPException(401, "Sign in required")
    if not user.can(role):
        raise HTTPException(403, f"Needs {role} access")
    if tab and not any(user.can_tab(t) for t in ((tab,) if isinstance(tab, str) else tab)):
        raise HTTPException(403, "You don't have access to this tab; ask the owner")
    # Basic CSRF guard: writes must come from our own page's fetch() calls.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "ledger":
        raise HTTPException(403, "Bad request origin")
    return user


def require_admin_key(request: Request) -> None:
    given = request.headers.get("x-admin-key", "")
    if not ADMIN_KEY or not hmac.compare_digest(given, ADMIN_KEY):
        raise HTTPException(401, "Bad admin key")


def require_engine_key(request: Request) -> None:
    """Key for the lead scoring / buy qty jobs (header X-Engine-Key, env ENGINE_KEY)."""
    given = request.headers.get("x-engine-key", "")
    if not ENGINE_KEY or not hmac.compare_digest(given, ENGINE_KEY):
        raise HTTPException(401, "Bad engine key")


async def authenticate(email: str, password: str) -> tuple[User, int] | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, name, role, tabs, session_ver, password_hash FROM users "
            "WHERE lower(email)=lower(%s) AND active",
            (email.strip(),),
        )
        r = await cur.fetchone()
    if not r or not check_password(password, r["password_hash"]):
        return None
    return _user(r), r["session_ver"]


async def create_invite(conn, user_id: int, days: int = 7) -> str:
    token = secrets.token_urlsafe(24)
    await conn.execute(
        "INSERT INTO invites (token_hash, user_id, expires_at) VALUES (%s,%s,%s)",
        (sha(token), user_id, datetime.now(timezone.utc) + timedelta(days=days)),
    )
    return token


def clean_tabs(tabs) -> list[str]:
    return [t for t in TABS if t in set(tabs or [])]


async def invite_user(email: str, name: str, role: str, tabs: list[str] | None = None) -> str:
    """Create (or re-invite) a user and return a one-time link token to set a password."""
    if role not in ROLES:
        raise HTTPException(400, "Role must be owner, editor or viewer")
    email = email.strip().lower()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO users (email, name, role, tabs) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (email) DO UPDATE SET name=COALESCE(NULLIF(EXCLUDED.name,''), users.name), "
                "role=EXCLUDED.role, tabs=EXCLUDED.tabs, active=true RETURNING id",
                (email, name.strip(), role, clean_tabs(tabs)),
            )
            uid = (await cur.fetchone())["id"]
            return await create_invite(conn, uid)


async def redeem_invite(token: str, password: str) -> tuple[User, int]:
    if len(password) < 10:
        raise HTTPException(400, "Use at least 10 characters")
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT user_id FROM invites WHERE token_hash=%s AND used_at IS NULL AND expires_at>now() FOR UPDATE",
                (sha(token),),
            )
            r = await cur.fetchone()
            if not r:
                raise HTTPException(400, "This link has expired or was already used")
            await conn.execute("UPDATE invites SET used_at=now() WHERE token_hash=%s", (sha(token),))
            cur = await conn.execute(
                "UPDATE users SET password_hash=%s, session_ver=session_ver+1 WHERE id=%s "
                "RETURNING id, email, name, role, tabs, session_ver",
                (hash_password(password), r["user_id"]),
            )
            u = await cur.fetchone()
    return _user(u), u["session_ver"]


async def invite_valid(token: str) -> dict | None:
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT u.email, u.name FROM invites i JOIN users u ON u.id=i.user_id "
            "WHERE i.token_hash=%s AND i.used_at IS NULL AND i.expires_at>now()",
            (sha(token),),
        )
        return await cur.fetchone()
