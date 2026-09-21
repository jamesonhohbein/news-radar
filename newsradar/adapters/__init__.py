"""Source adapters. Each module exposes:

    KIND: str                       -- matches source.kind
    list_files(since) -> list[str]  -- what exists upstream since a time
    latest() -> str                 -- the newest file
    parse(file, blob) -> Iterable[Event]

and ingest.py does the rest: skipping files already in fetch_log, inserting
events idempotently, recording the fetch. An adapter never touches the
database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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
