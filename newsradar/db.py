"""Connection helper. Reads .env directly rather than through the environment
so a credential never sits in a process table or a transcript."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # Process environment wins, so tests and CI can point elsewhere.
    env.update({k: v for k, v in os.environ.items() if k.startswith(("POSTGRES_", "NEWSRADAR_", "ACUTE_"))})
    return env


def dsn(env: dict[str, str] | None = None) -> str:
    env = env or load_env()
    if env.get("NEWSRADAR_DSN"):
        return env["NEWSRADAR_DSN"]
    return (f"host={env.get('POSTGRES_HOST', '127.0.0.1')} port={env.get('POSTGRES_PORT', '5443')} "
            f"dbname={env.get('POSTGRES_DB', 'newsradar')} user={env.get('POSTGRES_USER', 'newsradar')} "
            f"password={env['POSTGRES_PASSWORD']}")


def connect(env: dict[str, str] | None = None) -> psycopg.Connection:
    return psycopg.connect(dsn(env))
