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
        # JIT compiling made the Leads list ~30x slower (0.8 s vs 30 ms) once it got the Transparency brand columns
        kwargs={"row_factory": dict_row, "autocommit": False, "options": "-c jit=off"},
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
    """,    # 12: one "Single" (items had both "single" and "Single"), fixed order of set types, no 6-pack (Nick, Oct 30 2026)
    """
    ALTER TABLE set_types ADD COLUMN position integer NOT NULL DEFAULT 100;
    DELETE FROM set_types WHERE lower(name) IN ('single', '6-pack');
    INSERT INTO set_types (name, position) VALUES ('Single', 1), ('Split set', 2), ('Bundle', 7) ON CONFLICT (name) DO NOTHING;
    UPDATE set_types SET position = CASE lower(name) WHEN 'split set' THEN 2 WHEN '2-pack' THEN 3 WHEN '3-pack' THEN 4
        WHEN '4-pack' THEN 5 WHEN '5-pack' THEN 6 WHEN 'bundle' THEN 7 ELSE position END;
    UPDATE order_items i SET set_type = t.name FROM set_types t
        WHERE lower(btrim(i.set_type)) = lower(t.name) AND i.set_type IS DISTINCT FROM t.name;
    """,    # 13: which lead an order item came from (Create order on Leads); its qty is added to / taken off the lead's Ordered
    """
    ALTER TABLE order_items ADD COLUMN lead_id uuid REFERENCES leads(id) ON DELETE SET NULL;
    CREATE INDEX order_items_lead_idx ON order_items (lead_id) WHERE lead_id IS NOT NULL;
    """,    # 14: hazmat flags per ASIN from Keepa (see app/hazmat.py)
    """
    CREATE TABLE asin_flags (
        asin        text PRIMARY KEY,
        hazmat      boolean NOT NULL,
        hazmat_info jsonb NOT NULL DEFAULT '[]',
        fba_ever    boolean NOT NULL,
        amazon_ever boolean NOT NULL,
        level       text NOT NULL,
        title       text,
        checked_at  timestamptz NOT NULL DEFAULT now()
    );
    """,    # 15: Amazon's own FBA eligibility per ASIN (app/sellable.py); Keepa hazmat fields become optional
    """
    ALTER TABLE asin_flags ALTER COLUMN hazmat DROP NOT NULL, ALTER COLUMN fba_ever DROP NOT NULL,
        ALTER COLUMN amazon_ever DROP NOT NULL, ALTER COLUMN level DROP NOT NULL, ALTER COLUMN checked_at DROP NOT NULL;
    ALTER TABLE asin_flags ADD COLUMN fba_eligible boolean, ADD COLUMN fba_reasons text[] NOT NULL DEFAULT '{}',
        ADD COLUMN fba_checked_at timestamptz;
    """,    # 16: Amazon Transparency enrollment per ASIN, from Amazon's emails (app/transparency.py)
    """
    CREATE TABLE transparency_notices (
        asin           text NOT NULL,
        ref            text NOT NULL,
        kind           text NOT NULL CHECK (kind IN ('enrolled', 'rejected')),
        email_date     date,
        effective_date date,
        units_rejected integer,
        title          text,
        source         text NOT NULL DEFAULT 'email',
        added_at       timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (asin, ref)
    );
    """,    # 17: also "removed": Amazon removed the listing / set the inventory aside for missing Transparency codes
    """
    ALTER TABLE transparency_notices DROP CONSTRAINT transparency_notices_kind_check;
    ALTER TABLE transparency_notices ADD CONSTRAINT transparency_notices_kind_check CHECK (kind IN ('enrolled', 'rejected', 'removed'));
    """,    # 18: Keepa brand per ASIN, and the brands of Transparency ASINs (Hub lead brands + Keepa brand), so leads of the
            #     same brand get a warning (Nick, Oct 30 2026)
    r"""
    ALTER TABLE asin_flags ADD COLUMN brand text, ADD COLUMN brand_checked_at timestamptz;
    CREATE FUNCTION brand_key(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
        SELECT NULLIF(regexp_replace(regexp_replace(regexp_replace(lower(COALESCE(t, '')), '&', 'and', 'g'),
               '\m(the|beauty|cosmetics|skincare|new york)\M', ' ', 'g'), '[^a-z0-9]', '', 'g'), '')
    $$;
    CREATE VIEW transparency_brands AS
        SELECT DISTINCT t.asin, k.key, k.name FROM (SELECT DISTINCT asin FROM transparency_notices) t
        CROSS JOIN LATERAL (
            SELECT brand_key(b) AS key, b AS name FROM leads l, unnest(l.brands) b WHERE upper(btrim(l.asin)) = t.asin
            UNION SELECT brand_key(f.brand), f.brand FROM asin_flags f WHERE f.asin = t.asin
        ) k WHERE length(k.key) >= 3;
    """,    # 19: index for the ASIN lookups the Transparency brand view and warning popups use (240 ms -> 8 ms per request)
    """
    CREATE INDEX IF NOT EXISTS leads_asin_trim_idx ON leads (upper(btrim(asin)));
    ANALYZE leads;
    """,    # 20: can Nick's account list the ASIN at all (SP-API listing restrictions; app/sellable.py)
    """
    ALTER TABLE asin_flags ADD COLUMN list_ok boolean, ADD COLUMN list_reasons jsonb NOT NULL DEFAULT '[]',
        ADD COLUMN list_checked_at timestamptz;
    """,    # 21: lead sets (split sets): leads grouped by hand stay together when the list is sorted or filtered
    """
    CREATE TABLE lead_sets (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name       text,
        created_at timestamptz NOT NULL DEFAULT now(),
        created_by integer
    );
    ALTER TABLE leads ADD COLUMN set_id uuid REFERENCES lead_sets(id) ON DELETE SET NULL;
    CREATE INDEX leads_set_idx ON leads (set_id) WHERE set_id IS NOT NULL;
    """,    # 22: when a lead was last scored (shown next to its Lead Score). Set by Score now, the engine, and any score change.
    """
    ALTER TABLE leads ADD COLUMN lead_score_at timestamptz;
    UPDATE leads l SET lead_score_at = a.at
      FROM (SELECT entity_id, max(at) AS at FROM audit_log WHERE entity = 'lead' AND after ? 'lead_score_band' GROUP BY entity_id) a
     WHERE a.entity_id = l.id AND l.lead_score_band IS NOT NULL;
    CREATE FUNCTION leads_stamp_score() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.lead_score_band IS NOT NULL AND NEW.lead_score_at IS NULL THEN NEW.lead_score_at := now(); END IF;
      ELSIF (NEW.lead_score_band, NEW.lead_score_roi, NEW.lead_score_notes) IS DISTINCT FROM (OLD.lead_score_band, OLD.lead_score_roi, OLD.lead_score_notes)
            AND NEW.lead_score_at IS NOT DISTINCT FROM OLD.lead_score_at THEN
        NEW.lead_score_at := now();
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER leads_stamp_score BEFORE INSERT OR UPDATE ON leads FOR EACH ROW EXECUTE FUNCTION leads_stamp_score();
    """,    # 23: time clock (app/timeclock.py). People can be linked to a Hub login; one open entry per person.
            #     Everyone who isn't an owner gets the new Time clock tab so they can clock in (Nick, Nov 2 2026).
    """
    CREATE TABLE tc_people (
        id         serial PRIMARY KEY,
        name       text NOT NULL,
        user_id    integer UNIQUE REFERENCES users(id) ON DELETE SET NULL,
        rate       numeric(8,2),
        active     boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE UNIQUE INDEX tc_people_name_idx ON tc_people (lower(name));
    CREATE TABLE time_entries (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        person_id  integer NOT NULL REFERENCES tc_people(id) ON DELETE CASCADE,
        clock_in   timestamptz NOT NULL,
        clock_out  timestamptz,
        note       text NOT NULL DEFAULT '',
        source     text NOT NULL DEFAULT 'clock',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT time_entries_check CHECK (clock_out IS NULL OR clock_out >= clock_in)
    );
    CREATE UNIQUE INDEX time_entries_open_idx ON time_entries (person_id) WHERE clock_out IS NULL;
    CREATE INDEX time_entries_person_idx ON time_entries (person_id, clock_in);
    CREATE TABLE tc_adjustments (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        person_id  integer NOT NULL REFERENCES tc_people(id) ON DELETE CASCADE,
        week_start date NOT NULL,
        label      text NOT NULL,
        amount     numeric(10,2) NOT NULL,
        source     text NOT NULL DEFAULT 'manual',
        created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX tc_adjustments_idx ON tc_adjustments (person_id, week_start);
    UPDATE users SET tabs = array_append(tabs, 'timeclock') WHERE role <> 'owner' AND NOT ('timeclock' = ANY(tabs));
    """,    # 24: order confirmation emails, found by searching Nick's mailboxes for the order ID (a scheduled run posts the
            #     results through the engine API). Changing the order ID clears the result so it's checked again.
    """
    ALTER TABLE orders ADD COLUMN confirm_found boolean, ADD COLUMN confirm_checked_at timestamptz, ADD COLUMN confirm_info jsonb;
    CREATE FUNCTION orders_reset_confirm() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.order_ref IS DISTINCT FROM OLD.order_ref THEN
        NEW.confirm_found := NULL; NEW.confirm_checked_at := NULL; NEW.confirm_info := NULL;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER orders_reset_confirm BEFORE UPDATE OF order_ref ON orders FOR EACH ROW EXECUTE FUNCTION orders_reset_confirm();
    """,    # 25: canceled orders take their units back off the lead's Ordered (so Left to buy goes back up), with a note on
            #     the lead card. One row per order item, so it never happens twice (Nick, Nov 3 2026).
    """
    CREATE TABLE lead_cancellations (
        item_id    uuid PRIMARY KEY,
        order_id   uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        lead_id    uuid REFERENCES leads(id) ON DELETE CASCADE,
        asin       text,
        qty        integer NOT NULL DEFAULT 0,
        store      text,
        order_ref  text,
        order_date date,
        reason     text,
        at         timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX lead_cancellations_lead_idx ON lead_cancellations (lead_id);
    """,    # 26: Tasks tab: to-dos for Nick, typed in by hand or added by the Hub (e.g. "remove a canceled order's units
            #     from PrepOS"). key = dedupe key for tasks the Hub adds, so each one is created once and kept up to date.
    """
    CREATE TABLE tasks (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title      text NOT NULL,
        details    text NOT NULL DEFAULT '',
        status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
        kind       text NOT NULL DEFAULT 'manual',
        key        text UNIQUE,
        link       text,
        due        date,
        created_at timestamptz NOT NULL DEFAULT now(),
        created_by integer REFERENCES users(id) ON DELETE SET NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        done_at    timestamptz,
        done_by    integer REFERENCES users(id) ON DELETE SET NULL
    );
    CREATE INDEX tasks_status_idx ON tasks (status, created_at DESC);
    """,    # 27: daily summary of Nick's Amazon emails, written by a scheduled run (shown on the Inbox & tasks tab)
    """
    CREATE TABLE amazon_digests (
        day        date PRIMARY KEY,
        data       jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    );
    """,    # 28: recurring tasks (daily / weekdays / weekly / monthly). The Hub adds each one to the task list on the days
            #     it's due; an unfinished one carries over instead of piling up. Seeded with Nick's daily Amazon routine.
    """
    CREATE TABLE task_templates (
        id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        title      text NOT NULL,
        details    text NOT NULL DEFAULT '',
        freq       text NOT NULL DEFAULT 'daily' CHECK (freq IN ('daily', 'weekdays', 'weekly', 'monthly')),
        days       integer[] NOT NULL DEFAULT '{}',
        monthday   integer,
        position   integer NOT NULL DEFAULT 0,
        active     boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        created_by integer REFERENCES users(id) ON DELETE SET NULL
    );
    ALTER TABLE tasks ADD COLUMN template_id uuid REFERENCES task_templates(id) ON DELETE SET NULL,
                      ADD COLUMN for_date date, ADD COLUMN position integer;
    CREATE UNIQUE INDEX tasks_template_day_idx ON tasks (template_id, for_date) WHERE template_id IS NOT NULL;
    INSERT INTO task_templates (title, position) VALUES
        ('Address buyer messages', 1), ('Remove negative feedback', 2), ('Request Amazon disbursement', 3),
        ('Turn on repricer for new products', 4), ('Check account health', 5);
    """,    # 29: Nick's Monday tasks (Nov 3, 2026)
    """
    INSERT INTO task_templates (title, freq, days, position) VALUES
        ('Check stranded inventory', 'weekly', '{0}', 11), ('Pay credit cards', 'weekly', '{0}', 12),
        ('Pay out Chase Ink cashback', 'weekly', '{0}', 13), ('Pay out Fluz', 'weekly', '{0}', 14),
        ('Pay Abby', 'weekly', '{0}', 15), ('Pay Ma', 'weekly', '{0}', 16), ('Pay Huntington loan', 'weekly', '{0}', 17);
    """,    # 30: Nick doesn't want tasks made from his email (Nov 3, 2026): remove the ones the email summary added
    """
    DELETE FROM tasks WHERE kind = 'amazon_email';
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
