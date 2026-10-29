"""Postgres LISTEN/NOTIFY -> Server-Sent Events fan-out."""
from __future__ import annotations

import asyncio
import json
import logging

import psycopg

from . import db

log = logging.getLogger("ledger.live")

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def _broadcast(payload: str) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # A stuck client: tell it to reload everything instead of queueing forever.
            try:
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(json.dumps({"op": "reload"}))
            except Exception:
                pass


async def listener() -> None:
    """Runs for the life of the app; reconnects if the database drops."""
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(db.DATABASE_URL, autocommit=True) as conn:
                await conn.execute("LISTEN ledger")
                log.info("listening for ledger changes")
                # After a reconnect we may have missed events.
                _broadcast(json.dumps({"op": "reload"}))
                async for note in conn.notifies():
                    _broadcast(note.payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - network hiccups
            log.warning("listener error %s; reconnecting in 3s", e)
            await asyncio.sleep(3)
