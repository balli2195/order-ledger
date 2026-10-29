"""The Claude connector: MCP tools over the same order logic the website uses."""
from __future__ import annotations

import os
from typing import Annotated, Any, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from . import leads as L
from . import orders as o
from .auth import User, user_by_id
from .oauth import LedgerOAuthProvider

BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")

provider = LedgerOAuthProvider(BASE_URL)

INSTRUCTIONS = """Nick's Amazon order ledger (retail purchases that become FBA inventory).
Each order = one store checkout: order_date, store, email (account used), order_total (what was charged),
order_ref (the store's order number), notes, plus line items: asin, product_title, shade, set_type,
qty_ordered, cost_per_unit.
Discrepancy = sum(qty*cost_per_unit) differs from order_total by more than 1 cent per unit.
Missing info = an item lacks ASIN, title, cost, set type, shade or qty, or the order has no items.
Every change you make appears live on the ledger website and is logged with your user's name.
Look orders up before editing (search_orders / get_order), confirm destructive changes with the user,
and use run_sql for totals and analysis instead of paging through search results.

It also holds Nick's sourcing leads (formerly the Airtable "VA Leads" base): one row per product lead with asin,
lead_date, product_name, sourced_by (VA name), stores[], brands[], store_link, cost (per unit), promo_code, va_notes,
buyer_notes, feedback, buy_qty (planned), qty_ordered (actually bought), lead_score_band (1-10) / lead_score_roi
(predicted ROI) / lead_score_notes from the Lead Score server, claude_buy_qty / claude_buy_note, replen.
expected_spend = cost*buy_qty, actual_spend = cost*qty_ordered, left_to_buy = buy_qty-qty_ordered.
The VA bonus is 1.2% of actual spend on leads sourced by the bonus sourcer (see get_lead_choices).
Stores and brands have a can_source flag. While leads mode is "mirror", leads are copied from Airtable every minute
(this site never writes to Airtable); leads added or edited here stay here and edited fields aren't overwritten by
the copy. After the owner's cutover ("home") the copy stops."""

mcp = FastMCP(
    "Myrddin Hub",
    instructions=INSTRUCTIONS,
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=BASE_URL,
        resource_server_url=f"{BASE_URL}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class ItemIn(BaseModel):
    asin: str | None = Field(None, description="10-character Amazon ASIN")
    product_title: str | None = None
    shade: str | None = Field(None, description="Shade/variant/size; use 'N/A' if none")
    set_type: str | None = Field(None, description="e.g. Single, Set, Bundle — see get_choices")
    qty_ordered: int = 1
    cost_per_unit: float | None = Field(None, description="Price paid per unit after discounts")


class ItemChange(BaseModel):
    id: str = Field(description="Line item id from get_order")
    asin: str | None = None
    product_title: str | None = None
    shade: str | None = None
    set_type: str | None = None
    qty_ordered: int | None = None
    cost_per_unit: float | None = None


async def _actor(need: str = "viewer") -> o.Actor:
    tok = get_access_token()
    if not tok or not tok.subject:
        raise o.LedgerError("Not signed in")
    found = await user_by_id(int(tok.subject))
    if not found:
        raise o.LedgerError("Your ledger login was removed or disabled")
    user: User = found[0]
    if not user.can(need):
        raise o.LedgerError(f"Your login ({user.role}) can't make changes; ask the owner for editor access")
    return o.Actor(user.id, f"{user.label} via Claude", "chat")


def _compact(row: dict) -> dict:
    keep = ("id", "order_date", "store", "email", "order_total", "order_ref", "notes",
            "items_total", "units", "item_count", "discrepancy", "missing", "asins")
    return {k: row.get(k) for k in keep}


@mcp.tool()
async def search_orders(
    query: Annotated[str | None, Field(description="Free text across store, email, order ID, notes, ASIN, title, shade")] = None,
    store: str | None = None,
    date_from: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    asin: str | None = None,
    order_ref: Annotated[str | None, Field(description="Store's order number (partial match)")] = None,
    problem: Annotated[Literal["discrepancy", "missing", "any"] | None, Field(description="Only orders with this flag")] = None,
    limit: int = 50,
) -> dict:
    """Find orders. Returns summaries newest first (no line items; use get_order for those)."""
    await _actor()
    rows = await o.list_orders(query=query, store=store, date_from=date_from, date_to=date_to,
                               asin=asin, order_ref=order_ref, problem=problem)
    limit = max(1, min(limit, 500))
    return {"total_matches": len(rows), "orders": [_compact(r) for r in rows[:limit]]}


@mcp.tool()
async def get_order(
    order_id: Annotated[str | None, Field(description="Internal id from search_orders")] = None,
    order_ref: Annotated[str | None, Field(description="Or the store's order number (exact)")] = None,
) -> dict:
    """Get one order with all its line items (item ids are needed to edit items)."""
    await _actor()
    if not order_id and not order_ref:
        raise o.LedgerError("Give order_id or order_ref")
    found = await o.get_order(order_id, order_ref)
    if not found:
        raise o.LedgerError("No such order")
    return found


@mcp.tool()
async def create_order(
    order_date: Annotated[str, Field(description="YYYY-MM-DD")],
    store: str,
    order_total: float | None = None,
    order_ref: Annotated[str | None, Field(description="Store's order number")] = None,
    email: Annotated[str | None, Field(description="Account email used for the purchase")] = None,
    notes: str | None = None,
    items: list[ItemIn] | None = None,
) -> dict:
    """Add a new order, optionally with its line items. Check first that the order_ref isn't already logged."""
    actor = await _actor("editor")
    data = {"order_date": order_date, "store": store, "order_total": order_total,
            "order_ref": order_ref, "email": email, "notes": notes}
    return await o.create_order(actor, data, [i.model_dump(exclude_unset=True) for i in (items or [])])


@mcp.tool()
async def update_order(
    order_id: str,
    order_date: str | None = None,
    store: str | None = None,
    order_total: float | None = None,
    order_ref: str | None = None,
    email: str | None = None,
    notes: str | None = None,
    clear_fields: Annotated[list[Literal["order_total", "order_ref", "email", "notes"]] | None,
                            Field(description="Fields to blank out")] = None,
) -> dict:
    """Change order-level fields. Only the fields you pass are changed."""
    actor = await _actor("editor")
    data: dict[str, Any] = {k: v for k, v in {
        "order_date": order_date, "store": store, "order_total": order_total,
        "order_ref": order_ref, "email": email, "notes": notes}.items() if v is not None}
    for f in clear_fields or []:
        data[f] = None
    return await o.update_order(actor, order_id, data)


@mcp.tool()
async def delete_order(order_id: str, confirm: Annotated[bool, Field(description="Must be true; ask the user first")] = False) -> dict:
    """Delete an order and its items. Ask the user before calling with confirm=true."""
    actor = await _actor("editor")
    if not confirm:
        raise o.LedgerError("Not deleted: confirm with the user, then call again with confirm=true")
    return await o.delete_order(actor, order_id)


@mcp.tool()
async def add_items(order_id: str, items: list[ItemIn]) -> dict:
    """Add line items to an existing order."""
    actor = await _actor("editor")
    return await o.add_items(actor, order_id, [i.model_dump(exclude_unset=True) for i in items])


@mcp.tool()
async def update_items(changes: list[ItemChange]) -> dict:
    """Edit one or more line items (can span orders). Only fields you pass change."""
    actor = await _actor("editor")
    payload = [c.model_dump(exclude_none=True) for c in changes]
    touched = await o.update_items(actor, payload)
    return {"updated_items": len(payload), "orders": [await o.get_order(t) for t in touched][:20]}


@mcp.tool()
async def delete_items(item_ids: list[str], confirm: bool = False) -> dict:
    """Remove line items. Ask the user before calling with confirm=true."""
    actor = await _actor("editor")
    if not confirm:
        raise o.LedgerError("Not deleted: confirm with the user, then call again with confirm=true")
    touched = await o.delete_items(actor, item_ids)
    return {"deleted_items": len(item_ids), "orders_affected": touched}


@mcp.tool()
async def monthly_spend(months: int = 6) -> dict:
    """Spent vs the daily purchasing target, per month (same as the tiles on the site)."""
    await _actor()
    return {"months": await o.monthly_spend(months)}


@mcp.tool()
async def set_daily_target(amount: float) -> dict:
    """Change the daily spend target used for 'Need to spend' (owner only)."""
    actor = await _actor("owner")
    await o.set_daily_target(actor, amount)
    return {"daily_target": amount}


@mcp.tool()
async def get_choices() -> dict:
    """Known stores, purchase emails and set types, to keep new entries consistent."""
    await _actor()
    return await o.suggestions()


@mcp.tool()
async def recent_changes(limit: int = 30, order_id: str | None = None) -> dict:
    """The change log: who changed what, from the site or from chat."""
    await _actor()
    return {"changes": await o.recent_changes(min(limit, 200), order_id)}


@mcp.tool()
async def run_sql(sql: Annotated[str, Field(description="One read-only SELECT/WITH statement")]) -> dict:
    """Read-only SQL for analysis. Tables: orders(id, order_date, store, email, order_total, order_ref,
    notes, created_at, updated_at), order_items(id, order_id, asin, product_title, shade, set_type,
    qty_ordered, cost_per_unit), leads(id, airtable_id, asin, lead_date, product_name, sourced_by, stores text[],
    brands text[], store_link, cost, promo_code, va_notes, feedback, buy_qty, qty_ordered, buyer_notes,
    lead_score_band, lead_score_roi, lead_score_notes, claude_buy_qty, claude_buy_note, replen, extra jsonb,
    expected_spend, actual_spend, left_to_buy, created_at, updated_at), stores(name, can_source),
    brands(name, can_source), audit_log(at, user_id, source, action, entity, entity_id, order_id, before, after),
    users(id, email, name, role), settings(key, value). Use unnest(stores) to group by store. Max 500 rows, 15s."""
    await _actor()
    return await o.run_readonly_sql(sql)


# ---------------------------------------------------------------- leads

class LeadIn(BaseModel):
    asin: str | None = None
    lead_date: str | None = Field(None, description="YYYY-MM-DD; defaults to today")
    product_name: str | None = None
    sourced_by: str | None = Field(None, description="Who found it (see get_lead_choices sourcers)")
    stores: list[str] | None = None
    brands: list[str] | None = None
    store_link: str | None = None
    cost: float | None = Field(None, description="Cost per unit at the store")
    promo_code: str | None = None
    va_notes: str | None = None
    feedback: str | None = None
    buy_qty: int | None = Field(None, description="Planned units to buy")
    qty_ordered: int | None = Field(None, description="Units actually ordered")
    buyer_notes: str | None = None
    claude_buy_qty: int | None = None
    claude_buy_note: str | None = None
    replen: bool | None = None


class LeadChange(LeadIn):
    id: str = Field(description="Lead id from search_leads")
    lead_score_band: int | None = None
    lead_score_roi: float | None = None
    lead_score_notes: str | None = None


def _lead_compact(r: dict) -> dict:
    keep = ("id", "lead_date", "asin", "product_name", "sourced_by", "stores", "brands", "cost", "buy_qty", "qty_ordered",
            "left_to_buy", "actual_spend", "lead_score_band", "lead_score_roi", "claude_buy_qty", "promo_code",
            "va_notes", "buyer_notes", "store_link")
    return {k: r.get(k) for k in keep}


@mcp.tool()
async def search_leads(
    query: Annotated[str | None, Field(description="Free text across ASIN, product, store, brand, notes, promo, link")] = None,
    asin: str | None = None,
    store: str | None = None,
    brand: str | None = None,
    date_from: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    sourced_by: str | None = None,
    ordered_only: bool = False,
    left_to_buy_only: bool = False,
    unscored_only: bool = False,
    band_min: int | None = None,
    sort: Literal["date", "cost", "band", "roi", "spend", "left", "claude_qty", "updated"] = "date",
    limit: int = 50,
) -> dict:
    """Find leads, newest first by default. Returns totals for everything matching (count, expected/actual spend, bonus)."""
    await _actor()
    f = {"q": query, "asin": asin, "store": store, "brand": brand, "date_from": date_from, "date_to": date_to,
         "sourced_by": sourced_by, "ordered": ordered_only, "left": left_to_buy_only, "unscored": unscored_only,
         "band_min": band_min}
    f = {k: v for k, v in f.items() if v not in (None, False, "")}
    res = await L.list_leads(f, sort, True, min(max(limit, 1), 500), 0)
    return {"totals": res["totals"], "leads": [_lead_compact(r) for r in res["leads"]]}


@mcp.tool()
async def get_lead(lead_id: str) -> dict:
    """One lead with every field, Lead Score notes, and other leads of the same ASIN."""
    await _actor()
    found = await L.get_lead(lead_id)
    if not found:
        raise o.LedgerError("No such lead")
    return found


@mcp.tool()
async def create_lead(lead: LeadIn) -> dict:
    """Add a lead (stays on this site; Airtable isn't changed). It is scored automatically once it has an ASIN, cost and store link."""
    actor = await _actor("editor")
    return await L.create_lead(actor, lead.model_dump(exclude_none=True))


@mcp.tool()
async def update_leads(changes: list[LeadChange]) -> dict:
    """Edit one or more leads on this site (Airtable isn't changed). Only the fields you pass change."""
    actor = await _actor("editor")
    ids = await L.update_leads(actor, [c.model_dump(exclude_none=True) for c in changes])
    return {"updated": len(ids), "ids": ids}


@mcp.tool()
async def delete_leads(lead_ids: list[str], confirm: bool = False) -> dict:
    """Delete leads from the site (Airtable isn't changed; deleted Airtable leads aren't copied back). Ask the user first, then call with confirm=true."""
    actor = await _actor("editor")
    if not confirm:
        raise o.LedgerError("Not deleted: confirm with the user, then call again with confirm=true")
    return {"deleted": await L.delete_leads(actor, lead_ids)}


@mcp.tool()
async def score_lead(lead_id: str) -> dict:
    """(Re)score a lead with the Lead Score server and save the band, predicted ROI and notes on this site."""
    await _actor("editor")
    return await L.score_lead(lead_id)


@mcp.tool()
async def list_stores_or_brands(
    kind: Literal["stores", "brands"] = "stores",
    query: str | None = None,
    limit: int = 100,
) -> dict:
    """Stores or brands with can_source flag, lead count, total actual spend, units and days since last lead."""
    await _actor()
    rows = await L.store_stats(kind, query)
    return {"total": len(rows), "rows": rows[: max(1, min(limit, 1000))]}


@mcp.tool()
async def set_can_source(kind: Literal["stores", "brands"], name: str, can_source: bool) -> dict:
    """Mark a store or brand as OK (or not) to source from (after cutover)."""
    actor = await _actor("editor")
    await L.set_can_source(actor, kind, name, can_source)
    return {"kind": kind, "name": name, "can_source": can_source}


@mcp.tool()
async def get_lead_choices() -> dict:
    """Known stores, brands, sourcers, bonus settings, and whether leads are still mirrored from Airtable."""
    await _actor()
    c = await L.lead_choices()
    c["stores"] = [s["name"] for s in c["stores"]]
    c["brands"] = [b["name"] for b in c["brands"]]
    return c
