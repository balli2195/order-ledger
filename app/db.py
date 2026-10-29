"""Database pool and schema migrations."""
from __future__ import annotations

import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/orders")

pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    global pool
    pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_MAX", "8")),
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    await migrate()
    return pool


async def close_pool() -> None:
    if pool is not None:
        await pool.close()


# Each entry runs once, in order. Never edit a shipped entry; append a new one.
MIGRATIONS: list[str] = [
    # 1: core tables
    """
    CREATE EXTENSION IF NOT EXISTS pgcrypto;

    CREATE TABLE users (
        id            serial PRIMARY KEY,
        email         text NOT NULL UNIQUE,
        name          text NOT NULL DEFAULT '',
        password_hash text,
        role          text NOT NULL CHECK (role IN ('owner','editor','viewer')),
        active        boolean NOT NULL DEFAULT true,
        session_ver   integer NOT NULL DEFAULT 1,
        created_at    timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE orders (
        id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        order_date  date NOT NULL,
        store       text NOT NULL,
        email       text,
        order_total numeric(12,2),
        order_ref   text,
        notes       text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now(),
        created_by  integer REFERENCES users(id) ON DELETE SET NULL,
        updated_by  integer REFERENCES users(id) ON DELETE SET NULL,
        legacy_id   uuid UNIQUE
    );
    CREATE INDEX orders_date_idx  ON orders (order_date DESC);
    CREATE INDEX orders_store_idx ON orders (lower(store));
    CREATE INDEX orders_ref_idx   ON orders (lower(order_ref));

    CREATE TABLE order_items (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        order_id      uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        asin          text,
        product_title text,
        shade         text,
        set_type      text,
        qty_ordered   integer NOT NULL DEFAULT 1,
        cost_per_unit numeric(12,4),
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        legacy_id     uuid UNIQUE
    );
    CREATE INDEX order_items_order_idx ON order_items (order_id);
    CREATE INDEX order_items_asin_idx  ON order_items (upper(asin));

    CREATE TABLE set_types (
        name text PRIMARY KEY
    );

    CREATE TABLE settings (
        key   text PRIMARY KEY,
        value jsonb NOT NULL
    );
    INSERT INTO settings (key, value) VALUES ('daily_target', '6800');

    CREATE TABLE audit_log (
        id        bigserial PRIMARY KEY,
        at        timestamptz NOT NULL DEFAULT now(),
        user_id   integer REFERENCES users(id) ON DELETE SET NULL,
        source    text NOT NULL,
        action    text NOT NULL,
        entity    text NOT NULL,
        entity_id uuid,
        order_id  uuid,
        before    jsonb,
        after     jsonb
    );
    CREATE INDEX audit_log_order_idx ON audit_log (order_id, at DESC);
    CREATE INDEX audit_log_at_idx    ON audit_log (at DESC);

    CREATE TABLE oauth_clients (
        client_id  text PRIMARY KEY,
        info       jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE oauth_codes (
        code       text PRIMARY KEY,
        data       jsonb NOT NULL,
        expires_at timestamptz NOT NULL
    );
    CREATE TABLE oauth_tokens (
        token      text PRIMARY KEY,
        kind       text NOT NULL CHECK (kind IN ('access','refresh')),
        client_id  text NOT NULL,
        user_id    integer REFERENCES users(id) ON DELETE CASCADE,
        scopes     text[] NOT NULL DEFAULT '{}',
        resource   text,
        pair       text,
        expires_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE oauth_pending (
        id         text PRIMARY KEY,
        client_id  text NOT NULL,
        params     jsonb NOT NULL,
        expires_at timestamptz NOT NULL
    );

    CREATE TABLE invites (
        token_hash text PRIMARY KEY,
        user_id    integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL,
        used_at    timestamptz
    );
    """,
]


async def migrate() -> None:
    assert pool is not None
    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version integer NOT NULL)"
            )
            # One app instance migrates at a time.
            await conn.execute("SELECT pg_advisory_xact_lock(724113)")
            cur = await conn.execute("SELECT max(version) AS v FROM schema_version")
            row = await cur.fetchone()
            current = (row or {}).get("v") or 0
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i <= current:
                    continue
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (i,))
