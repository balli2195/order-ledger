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
    # 2: leads (from the Airtable "VA Leads" base), stores, brands
    """
    CREATE TABLE stores (
        name        text PRIMARY KEY,
        can_source  boolean NOT NULL DEFAULT false,
        airtable_id text UNIQUE,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE brands (
        name        text PRIMARY KEY,
        can_source  boolean NOT NULL DEFAULT false,
        airtable_id text UNIQUE,
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE leads (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        airtable_id      text UNIQUE,
        asin             text,
        lead_date        date,
        product_name     text,
        sourced_by       text,
        stores           text[] NOT NULL DEFAULT '{}',
        brands           text[] NOT NULL DEFAULT '{}',
        store_link       text,
        cost             numeric(12,2),
        promo_code       text,
        va_notes         text,
        feedback         text,
        buy_qty          integer,
        qty_ordered      integer,
        buyer_notes      text,
        lead_score_band  integer,
        lead_score_roi   numeric(8,4),
        lead_score_notes text,
        claude_buy_qty   integer,
        claude_buy_note  text,
        replen           boolean NOT NULL DEFAULT false,
        extra            jsonb NOT NULL DEFAULT '{}',
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        created_by       integer REFERENCES users(id) ON DELETE SET NULL,
        updated_by       integer REFERENCES users(id) ON DELETE SET NULL,
        expected_spend   numeric(14,2) GENERATED ALWAYS AS (cost * buy_qty) STORED,
        actual_spend     numeric(14,2) GENERATED ALWAYS AS (cost * qty_ordered) STORED,
        left_to_buy      integer GENERATED ALWAYS AS (buy_qty - qty_ordered) STORED
    );
    CREATE INDEX leads_date_idx   ON leads (lead_date DESC NULLS LAST, created_at DESC);
    CREATE INDEX leads_asin_idx   ON leads (upper(asin));
    CREATE INDEX leads_stores_idx ON leads USING gin (stores);
    CREATE INDEX leads_brands_idx ON leads USING gin (brands);
    CREATE INDEX audit_log_entity_idx ON audit_log (entity_id, at DESC);

    INSERT INTO settings (key, value) VALUES
      ('leads_mode', '"mirror"'),
      ('bonus_rate', '0.012'),
      ('bonus_sourcer', '"Ariane"'),
      ('sourcers', '["Ariane", "Nick"]');
    """,
    # 3: fields edited on the site are never overwritten by the Airtable copy
    """
    ALTER TABLE leads ADD COLUMN local_edits text[] NOT NULL DEFAULT '{}';
    """,
    # 4: leads deleted on the site that came from Airtable; the copy skips them
    """
    CREATE TABLE lead_tombstones (
        airtable_id text PRIMARY KEY,
        deleted_at  timestamptz NOT NULL DEFAULT now(),
        user_id     integer REFERENCES users(id) ON DELETE SET NULL
    );
    """,
    # 5: one-time cleanup of the completely blank item lines the first "+ Items" button saved (Oct 29, 2026 evening)
    """
    WITH gone AS (
        DELETE FROM order_items
        WHERE created_at >= '2026-10-30 02:40+00' AND created_at < '2026-10-30 05:00+00'
          AND COALESCE(asin, '') = '' AND COALESCE(product_title, '') = '' AND COALESCE(shade, '') = ''
          AND COALESCE(set_type, '') = '' AND cost_per_unit IS NULL
        RETURNING *
    ), logged AS (
        INSERT INTO audit_log (source, action, entity, entity_id, order_id, before)
        SELECT 'system', 'delete', 'item', id, order_id, to_jsonb(gone) - 'order_id' FROM gone
    )
    UPDATE orders SET updated_at = now() WHERE id IN (SELECT order_id FROM gone);
    """,
    # 6: fix the spelling of Ariane's user name (Nick, Oct 29, 2026)
    """
    UPDATE users SET name = 'Ariane' WHERE name = 'Araine';
    """,
    # 7: lines sent to the PrepOS (Network Prep) API, and which order items each line covered
    """
    CREATE TABLE prepos_sends (
        id          bigserial PRIMARY KEY,
        batch       uuid NOT NULL,
        sent_at     timestamptz NOT NULL DEFAULT now(),
        send_day    date NOT NULL,
        kind        text NOT NULL,
        user_id     integer REFERENCES users(id) ON DELETE SET NULL,
        asin        text NOT NULL,
        title       text,
        qty         integer NOT NULL,
        unit_cost   numeric(12,2),
        sell        numeric(12,2),
        supplier    text,
        order_refs  text,
        first_date  date,
        last_date   date,
        status      text NOT NULL,
        response    jsonb
    );
    CREATE INDEX prepos_sends_day_idx ON prepos_sends (send_day, asin);
    CREATE TABLE prepos_sent_items (
        item_id uuid PRIMARY KEY,
        send_id bigint NOT NULL REFERENCES prepos_sends(id) ON DELETE CASCADE
    );
    INSERT INTO settings (key, value) VALUES ('prepos_start', '"2026-10-29"'), ('prepos_lag_days', '2')
    ON CONFLICT (key) DO NOTHING;
    """,
    # 8: pack set types (sent to PrepOS as Multi-Pack) and the pack size on sent lines
    """
    INSERT INTO set_types (name) VALUES ('2-pack'), ('3-pack'), ('4-pack'), ('5-pack'), ('6-pack') ON CONFLICT DO NOTHING;
    ALTER TABLE prepos_sends ADD COLUMN multipack integer;
    """,
    # 9: per-person tabs (owners see everything). Existing users keep the tabs they could already see.
    """
    ALTER TABLE users ADD COLUMN tabs text[] NOT NULL DEFAULT '{}';
    UPDATE users SET tabs = ARRAY['leads','orders','stores','prepos'] WHERE role <> 'owner';
    """,
    # 10: documents written by the scheduled analysis runs (Inventory health etc.), same paths as the old Inventory Hub db
    """
    CREATE TABLE hub_docs (
        path       text PRIMARY KEY,
        data       jsonb NOT NULL,
        version    integer NOT NULL DEFAULT 1,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,    # 11: Left to buy = Buy qty until something is entered in Ordered (Nick, Oct 30 2026); was NULL while Ordered was blank
    """
    ALTER TABLE leads DROP COLUMN left_to_buy;
    ALTER TABLE leads ADD COLUMN left_to_buy integer GENERATED ALWAYS AS (buy_qty - COALESCE(qty_ordered, 0)) STORED;
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
