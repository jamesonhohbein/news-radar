"""Archive before prune (specs/archive.md).

Rows past a table's hot window are written to Parquet under
NEWSRADAR_ARCHIVE_DIR, one file per table per UTC day, and only then deleted
from Postgres. Layout: <table>/<yyyy>/<yyyy-mm-dd>.<n>.parquet; a day that
gets more rows after it was archived gets the next n, so no file is ever
rewritten (R60).

Each (table, day) slice runs in its own REPEATABLE READ transaction: the
export and the delete see the same snapshot, so a row inserted after the
export started is invisible to the delete and stays for the next run, and a
row changed under us makes the delete fail rather than drop an unarchived
version. The delete's row count must equal the count read back from the file,
or the transaction rolls back (R58).

Only whole UTC days that are entirely older than the cutoff are archived, so
the hourly run is one indexed min() per table when nothing is due.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Table:
    name: str
    ts: str                      # the column the window applies to
    days: int                    # hot window
    is_date: bool = False        # ts is a DATE, not a TIMESTAMPTZ
    # Extra predicate ANDed into both export and delete. Rows it excludes are
    # neither archived nor deleted.
    extra: str = ""
    children: tuple[str, ...] = field(default=())   # exported with the slice, keyed by article_id


# Feed sources whose added_at is the upstream start date and which keep
# serving an item until the episode ends (a months-long GDACS drought, a
# volcano elevated since 2021). Pruning them by added_at deletes rows the next
# fetch puts straight back, which made the map flicker hourly and would write a
# new archive file every hour. They stay in Postgres (spec amendment, T68).
EVENT_KEEP_SOURCES = ("gdacs-alerts", "usgs-volcanoes")


def tables(windows: dict[str, int]) -> list[Table]:
    return [
        Table("event", "added_at", windows["event"],
              extra="source_id NOT IN (SELECT id FROM source WHERE name = ANY(%(keep)s))"),
        Table("theme_hourly", "hour", windows["theme_hourly"]),
        Table("mention_hourly", "hour", windows["mention_hourly"]),
        Table("attention_hourly", "hour", windows["attention_hourly"]),
        Table("wiki_activity", "hour", windows["wiki_activity"]),
        Table("chatter_hourly", "hour", windows["chatter_hourly"]),
        Table("gkg_article", "added_at", windows["gkg_article"], children=("gkg_location",)),
        Table("firms_detection", "acq_at", windows["firms_detection"]),
        Table("theme_daily", "day", windows["theme_daily"], is_date=True),
    ]


ARCHIVED = ("event", "theme_hourly", "mention_hourly", "attention_hourly", "wiki_activity",
            "chatter_hourly", "gkg_article", "gkg_location", "firms_detection", "theme_daily")


class ArchiveError(RuntimeError):
    pass


# --- column mapping -----------------------------------------------------------

_SIMPLE = {
    "smallint": pa.int16(), "integer": pa.int32(), "bigint": pa.int64(),
    # float4 is widened: psycopg hands back the decimal text value as a double,
    # and storing that keeps the round trip exact (R65).
    "real": pa.float64(), "double precision": pa.float64(),
    "text": pa.string(), "character varying": pa.string(), "boolean": pa.bool_(),
    "date": pa.date32(), "timestamp with time zone": pa.timestamp("us", tz="UTC"),
}


def columns(conn, table: str) -> list[tuple[str, str, pa.DataType]]:
    """(output name, SQL expression, arrow type) per column, in table order.
    geometry becomes lon/lat doubles, json text, text[] a list of strings."""
    rows = conn.execute("""SELECT column_name, data_type, udt_name FROM information_schema.columns
                           WHERE table_schema = current_schema() AND table_name = %s
                           ORDER BY ordinal_position""", (table,)).fetchall()
    if not rows:
        raise ArchiveError(f"{table}: no such table")
    names = {r[0] for r in rows}
    out = []
    for name, dtype, udt in rows:
        q = f'"{name}"'
        if dtype in _SIMPLE:
            out.append((name, q, _SIMPLE[dtype]))
        elif dtype in ("jsonb", "json"):
            out.append((name, f"{q}::text", pa.string()))
        elif dtype == "ARRAY" and udt in ("_text", "_varchar"):
            out.append((name, f"{q}::text[]", pa.list_(pa.string())))
        elif dtype == "USER-DEFINED" and udt == "geometry":
            lon, lat = ("lon", "lat") if not {"lon", "lat"} & names else (f"{name}_lon", f"{name}_lat")
            out.append((lon, f"ST_X({q})", pa.float64()))
            out.append((lat, f"ST_Y({q})", pa.float64()))
        else:
            raise ArchiveError(f"{table}.{name}: no archive mapping for {dtype}/{udt}")
    return out


# --- files -------------------------------------------------------------------

def _to_arrow(batch: list[tuple], schema: pa.Schema) -> pa.Table:
    cols = list(zip(*batch)) if batch else [[] for _ in schema]
    return pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, schema)], schema=schema)


def _export(conn, table: str, where: str, params: dict, path: Path) -> int:
    cols = columns(conn, table)
    schema = pa.schema([(n, t) for n, _, t in cols])
    n = 0
    with pq.ParquetWriter(path, schema, compression="zstd") as w, conn.cursor(name=f"archive_{table}") as cur:
        cur.execute(f"SELECT {', '.join(e for _, e, _ in cols)} FROM {table} WHERE {where}", params)
        while batch := cur.fetchmany(100_000):
            w.write_table(_to_arrow(batch, schema))
            n += len(batch)
    return n


def _readback(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows


def _next_path(archive: Path, table: str, day: date) -> Path:
    d = archive / table / f"{day.year:04d}"
    taken = [int(p.name.split(".")[1]) for p in d.glob(f"{day.isoformat()}.*.parquet")] if d.exists() else []
    return d / f"{day.isoformat()}.{max(taken) + 1 if taken else 0}.parquet"


def _move_into(src: Path, dst: Path) -> None:
    """One sequential copy to a hidden name beside the target, then a no-clobber
    link into place, so the archive never shows a partial file and never
    replaces an existing one (R60, R61)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.part")
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 16 << 20)
            fo.flush()
            os.fsync(fo.fileno())
        os.link(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(dst.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    src.unlink()


# --- the slice loop ------------------------------------------------------------

def _bound(t: Table, now: datetime):
    cut = (now - timedelta(days=t.days)).astimezone(timezone.utc)
    start = datetime(cut.year, cut.month, cut.day, tzinfo=timezone.utc)
    return start.date() if t.is_date else start


def _day_of(t: Table, v) -> date:
    return v if t.is_date else v.astimezone(timezone.utc).date()


def _day_range(t: Table, day: date):
    if t.is_date:
        return day, day + timedelta(days=1)
    lo = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return lo, lo + timedelta(days=1)


def archive_table(conn, t: Table, archive: Path, scratch: Path, now: datetime) -> int:
    """Archive and delete every whole due day of one table. Returns rows deleted
    from the table itself. Raises on the first slice that fails; slices before
    it are already committed and archived."""
    bound = _bound(t, now)
    base = {"keep": list(EVENT_KEEP_SOURCES), "bound": bound}
    extra = f" AND {t.extra}" if t.extra else ""
    total = 0
    while True:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            first = conn.execute(f"SELECT min({t.ts}) FROM {t.name} WHERE {t.ts} < %(bound)s{extra}", base).fetchone()[0]
            if first is None:
                return total
            day = _day_of(t, first)
            lo, hi = _day_range(t, day)
            p = {**base, "lo": lo, "hi": hi}
            where = f"{t.ts} >= %(lo)s AND {t.ts} < %(hi)s{extra}"
            parts = [(t.name, where)] + [
                (c, f"article_id IN (SELECT id FROM {t.name} WHERE {where})") for c in t.children]
            staged = []
            for name, w in parts:
                f = scratch / f"{name}.{day.isoformat()}.parquet"
                staged.append((name, w, f))
                n = _export(conn, name, w, p, f)
                back = _readback(f)
                if back != n:
                    raise ArchiveError(f"{name} {day}: file holds {back} rows, exported {n}")
            counts = {}
            for name, w, f in staged:
                counts[name] = _readback(f)
                _move_into(f, _next_path(archive, name, day))
            # Children first, explicitly, so their count is checked too rather
            # than vanishing in the cascade.
            for name, w, _ in reversed(staged):
                got = conn.execute(f"DELETE FROM {name} WHERE {w}", p).rowcount
                if got != counts[name]:
                    raise ArchiveError(f"{name} {day}: delete would remove {got} rows, archive holds {counts[name]}")
            total += counts[t.name]


def prune_archived(conn, windows: dict[str, int], archive_dir: str | os.PathLike,
                   scratch_dir: str | os.PathLike | None = None,
                   now: datetime | None = None) -> tuple[dict[str, int], dict[str, str]]:
    """Archive then prune each table in turn. Returns (rows deleted per table,
    error per failed table). One table failing does not stop the others (R59)."""
    now = now or datetime.now(timezone.utc)
    archive = Path(archive_dir)
    if not archive.is_dir():
        raise ArchiveError(f"archive dir {archive} does not exist")
    base = Path(scratch_dir or tempfile.gettempdir())
    scratch = Path(tempfile.mkdtemp(prefix="newsradar-archive-", dir=base))
    done: dict[str, int] = {}
    failed: dict[str, str] = {}
    conn.commit()   # each slice must open its own top-level transaction
    try:
        for t in tables(windows):
            try:
                done[t.name] = archive_table(conn, t, archive, scratch, now)
            except Exception as e:  # noqa: BLE001 - any failure skips this table only
                if conn.info.transaction_status != 0:   # not IDLE: drop whatever is open
                    conn.rollback()
                failed[t.name] = f"{type(e).__name__}: {e}"
            for f in scratch.iterdir():
                f.unlink()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return done, failed


# --- read path -----------------------------------------------------------------

def duck(archive_dir: str | os.PathLike):
    """A DuckDB connection with one view per archived table, named like the
    Postgres table, over its Parquet glob. Tables with no files yet get no
    view."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    root = Path(archive_dir)
    for name in ARCHIVED:
        if any((root / name).glob("*/*.parquet")):
            glob = str(root / name / "*" / "*.parquet").replace("'", "''")
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{glob}', union_by_name = true)")
    return con


def query(sql: str, archive_dir: str | os.PathLike) -> tuple[list[str], list[tuple]]:
    con = duck(archive_dir)
    try:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        con.close()
