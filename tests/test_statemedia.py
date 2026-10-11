"""T36 to T42 (specs/state-media.md): the State Media Monitor parser, the
registry upsert, Wikidata resolution, manual identifiers, host matching, the
coverage report and the skill."""
import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from newsradar import statemedia as sm
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
REGIONAL = json.loads((FIX / "smm_regional.json").read_text())
PROFILE = json.loads((FIX / "smm_profile.json").read_text())
SKILL = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "state-media-monitor" / "SKILL.md"


def _profile_as(country: str, title: str) -> dict:
    p = json.loads(json.dumps(PROFILE[0]))
    p["content"]["rendered"] = p["content"]["rendered"].replace("Spain &middot; Quick Facts",
                                                                f"{country} &middot; Quick Facts")
    p["title"]["rendered"] = title
    return p


def _ent(qid, label, country=None, **props):
    claims = {}
    if country:
        claims["P17"] = [{"mainsnak": {"datavalue": {"value": {"id": country}}}, "rank": "normal"}]
    for prop, vals in props.items():
        claims[prop] = [{"mainsnak": {"datavalue": {"value": v}}, "rank": "normal"} for v in vals]
    return {"id": qid, "labels": {"en": {"value": label}}, "aliases": {}, "claims": claims}


# A tiny Wikidata: RT has a Spanish namesake the country filter drops, a
# Russian sub-channel the exact-name tie-break drops, and a subsidiary. TASS
# has two Russian items both labelled exactly "TASS", so it stays unmatched.
ENTS = {
    "Q159": _ent("Q159", "Russian Federation", P297=["RU"]),
    "Q29": _ent("Q29", "Spain"),
    "Q1": _ent("Q1", "RT", "Q159", P856=["https://www.RT.com/"], P3789=["@rt_russian"],
               P2397=["UCpwvZwUam-URkxB7g4USKpg"], P355=[{"id": "Q9"}]),
    "Q2": _ent("Q2", "RT", "Q29", P856=["https://rt.es/"]),
    "Q9": _ent("Q9", "Ruptly", "Q159", P856=["https://ruptly.tv"]),
    "Q3": _ent("Q3", "TASS", "Q159", P856=["https://tass.ru"]),
    "Q4": _ent("Q4", "TASS", "Q159"),
    "Q5": _ent("Q5", "RT Arabic", "Q159", P856=["https://arabic.rt.com"]),
}
SEARCH = {"RT": ["Q1", "Q2", "Q5"], "TASS": ["Q3", "Q4"]}


def fake_wikidata(url: str) -> bytes:
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if q["action"] == "wbsearchentities":
        return json.dumps({"search": [{"id": i} for i in SEARCH.get(q["search"], [])]}).encode()
    return json.dumps({"entities": {i: ENTS[i] for i in q["ids"].split("|") if i in ENTS}}).encode()


class T36Parse(unittest.TestCase):
    def test_regional_data_and_null_year(self):
        out = sm.parse_data(REGIONAL[0]["content"]["rendered"], REGIONAL[0]["link"])
        by = {o.name: o for o in out}
        self.assertEqual(set(by), {"RT", "TASS", "National Media Group (NMG)"})
        self.assertEqual(by["TASS"].trajectory, ["-", "SC", "SC", "SC", "SC"])
        self.assertEqual(by["National Media Group (NMG)"].trajectory, ["CaPu"] * 4 + ["SC"])
        self.assertEqual((by["RT"].region, by["RT"].typology), ("Russia", "SC"))
        self.assertIn("Ruptly", by["RT"].assets)

    def test_profile_quick_facts_and_trajectory(self):
        p = PROFILE[0]
        o = sm.parse_profile(p["content"]["rendered"], p["title"]["rendered"], p["link"])
        self.assertEqual((o.country, o.typology), ("Spain", "ISFM"))
        self.assertEqual(o.trajectory, ["SC", "SC", "SC", "SC", "ISFM"])
        self.assertIsNone(sm.parse_profile("<p>commentary</p>", "x", "u"))

    def test_profile_for_a_data_outlet_is_ignored(self):
        posts = REGIONAL + PROFILE + [_profile_as("Russia", "National Media Group")]
        out = sm.outlets_from_posts(posts)
        self.assertEqual(sorted(o.name for o in out),
                         ["Corporacion de Radio y Television Espanola (RTVE)", "National Media Group (NMG)", "RT", "TASS"])
        nmg = next(o for o in out if o.name.startswith("National"))
        self.assertEqual(nmg.smm_url, REGIONAL[0]["link"])


class StateDB(DBCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE state_outlet_id, state_outlet RESTART IDENTITY CASCADE")
        self.conn.commit()

    def load(self, posts, day=date(2026, 10, 1)):
        n = sm.upsert_outlets(self.conn, sm.outlets_from_posts(posts), day)
        self.conn.commit()
        return n

    def oid(self, name):
        return self.one("SELECT id FROM state_outlet WHERE name = %s", (name,))[0]


class T37Upsert(StateDB):
    def test_idempotent_and_never_deleted(self):
        self.assertEqual(self.load(REGIONAL + PROFILE), 4)
        self.assertEqual(self.load(REGIONAL + PROFILE), 0)
        self.assertEqual(self.one("SELECT count(*) FROM state_outlet")[0], 4)
        self.load(REGIONAL, date(2026, 11, 1))
        rows = dict(self.conn.execute("SELECT name, last_seen FROM state_outlet").fetchall())
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows["Corporacion de Radio y Television Espanola (RTVE)"], date(2026, 10, 1))
        self.assertEqual(rows["RT"], date(2026, 11, 1))


class T38Wikidata(StateDB):
    def test_single_in_country_item_and_ambiguity(self):
        self.load(REGIONAL)
        wd = sm.Wikidata(fetch=fake_wikidata, pause=0)
        rt = sm.Outlet("Russia", "RT", "SC", [], "u")
        qid, ids = sm.resolve(wd, rt, "RU")
        self.assertEqual(qid, "Q1")
        self.assertEqual(set(ids), {("host", "rt.com"), ("telegram", "rt_russian"),
                                    ("youtube", "UCpwvZwUam-URkxB7g4USKpg"), ("host", "ruptly.tv")})
        self.assertEqual(sm.resolve(wd, sm.Outlet("Russia", "TASS", "SC", [], "u"), "RU"), (None, []))
        # Without the ISO code, "Russia" is not a name of Q159 here: no match.
        self.assertEqual(sm.resolve(wd, rt), (None, []))

        sm.store_wikidata(self.conn, {self.oid("RT"): (qid, ids)})
        self.conn.commit()
        rows = self.conn.execute("SELECT kind, value, origin, qid FROM state_outlet_id ORDER BY 1, 2").fetchall()
        self.assertIn(("host", "rt.com", "wikidata", "Q1"), rows)
        self.assertEqual(len(rows), 4)
        tass = self.one("SELECT count(*) FROM state_outlet_id WHERE outlet_id = %s", (self.oid("TASS"),))[0]
        self.assertEqual(tass, 0)


class T39Manual(StateDB):
    def test_manual_rows_survive_a_refresh(self):
        self.load(REGIONAL)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("# comment\ncountry,name,kind,value\nRussia,RT,host,www.actualidad.rt.com\nRussia,TASS,host,tass.com\n")
        path = Path(f.name)
        self.assertEqual(sm.load_manual(self.conn, path), 2)
        # Wikidata now claims tass.com for RT and drops everything else: the
        # manual row keeps its outlet and origin, and nothing manual is deleted.
        sm.store_wikidata(self.conn, {self.oid("RT"): ("Q1", [("host", "tass.com"), ("host", "rt.com")])})
        self.conn.commit()
        rows = dict(((k, v), (o, src)) for k, v, o, src in
                    self.conn.execute("SELECT kind, value, outlet_id, origin FROM state_outlet_id").fetchall())
        self.assertEqual(rows[("host", "tass.com")], (self.oid("TASS"), "manual"))
        self.assertEqual(rows[("host", "actualidad.rt.com")], (self.oid("RT"), "manual"))
        self.assertEqual(rows[("host", "rt.com")], (self.oid("RT"), "wikidata"))
        sm.store_wikidata(self.conn, {})
        self.assertEqual(self.one("SELECT count(*) FROM state_outlet_id")[0], 2)

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("country,name,kind,value\nRussia,Rt Typo,host,x.ru\n")
        with self.assertRaises(ValueError):
            sm.load_manual(self.conn, Path(f.name))


class StateArticles(StateDB):
    def article(self, site, age=timedelta(hours=1)):
        self.conn.execute("INSERT INTO gkg_article (gkg_id, added_at, url, site, themes) VALUES (%s, %s, %s, %s, '{}')",
                          (f"g-{site}-{age}", datetime.now(timezone.utc) - age, f"https://{site}/a", site))

    def register(self, name, host):
        self.conn.execute("INSERT INTO state_outlet_id (outlet_id, kind, value, origin) VALUES (%s, 'host', %s, 'manual')",
                          (self.oid(name), host))


class T40HostMatch(StateArticles):
    def test_suffix_on_label_boundaries(self):
        self.load(REGIONAL)
        self.register("TASS", "news.cn")
        for s in ("english.news.cn", "news.cn", "fakenews.cn", "news.cn.example.org"):
            self.article(s)
        self.conn.commit()
        rows = self.conn.execute("SELECT site, outlet, typology FROM state_media_article ORDER BY 1").fetchall()
        self.assertEqual(rows, [("english.news.cn", "TASS", "SC"), ("news.cn", "TASS", "SC")])


class T41Report(StateArticles):
    def test_report_lists_gaps(self):
        self.load(REGIONAL)
        self.register("RT", "rt.com")
        self.register("TASS", "tass.ru")
        self.article("www.rt.com")
        self.article("tass.ru", timedelta(days=9))
        self.conn.commit()
        r = sm.report(self.conn)
        self.assertEqual([n for _, _, n in r["no_host"]], ["National Media Group (NMG)"])
        self.assertEqual([h for *_, h in r["silent_host"]], ["tass.ru"])


class T42Skill(unittest.TestCase):
    def test_skill_names_both_endpoints(self):
        text = SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: state-media-monitor\n"))
        self.assertIn(sm.SMM, text)
        self.assertIn(sm.WIKIDATA, text)


if __name__ == "__main__":
    unittest.main()
