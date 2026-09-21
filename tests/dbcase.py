"""A scratch database per test class, created on the running news-radar-db
server and dropped afterwards. schema.sql is applied as-is, so the tables the
tests hit are the ones production has, not a copy."""
from __future__ import annotations

import unittest
from pathlib import Path

import psycopg

from newsradar.db import dsn, load_env

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "schema.sql").read_text()


class DBCase(unittest.TestCase):
    dbname = "newsradar_test"

    @classmethod
    def setUpClass(cls):
        env = load_env()
        cls._admin = dsn(env)
        with psycopg.connect(cls._admin, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.dbname}")
            c.execute(f"CREATE DATABASE {cls.dbname}")
        cls.conn = psycopg.connect(dsn({**env, "POSTGRES_DB": cls.dbname}))
        cls.conn.execute(SCHEMA)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        with psycopg.connect(cls._admin, autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {cls.dbname}")

    def setUp(self):
        self.conn.execute("TRUNCATE event, fetch_log, attention_hourly, attention_daily, attention_anomaly, alert")
        self.conn.commit()

    def one(self, sql, params=None):
        return self.conn.execute(sql, params).fetchone()
