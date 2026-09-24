"""Source adapters. Two shapes, both database-free:

File adapters (gdelt) publish a series of files:

    KIND: str                       -- matches source.kind
    list_files(since) -> list[str]  -- what exists upstream since a time
    latest() -> str                 -- the newest file
    fetch(file) -> bytes
    parse(file, blob) -> Iterable[(Event | None, kept: bool)]

Feed adapters (usgs, gdacs) publish one endpoint that is the current state:

    KIND: str
    SOURCE: str                     -- source.name
    fetch() -> bytes
    parse(blob) -> Iterable[Event]
    resolve(conn, events) -> list[Event]   -- optional: place events parse left without a point
    after_insert(conn, sid)               -- optional: link rows to each other after upsert

ingest.py does the rest: skipping files already in fetch_log, inserting
events idempotently (feed adapters upsert, because a quake's magnitude and an
alert's level get revised), recording the fetch."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    external_id: str
    occurred_on: date | None
    added_at: datetime
    cameo_code: str | None
    cameo_root: str | None
    quad_class: int | None
    goldstein: float | None
    tone: float | None
    actor1_name: str | None
    actor1_country: str | None
    actor2_name: str | None
    actor2_country: str | None
    geo_type: int | None
    geo_name: str | None
    country: str
    adm1: str | None
    lat: float
    lon: float
    num_mentions: int
    num_sources: int
    num_articles: int
    url: str | None
    props: dict[str, Any] | None = None
