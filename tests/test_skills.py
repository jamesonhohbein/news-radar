"""T31 (R34): every adapter has a skill, and the skill names the endpoint the
adapter actually calls, so the two cannot drift apart silently."""
import importlib
import pkgutil
import unittest
from pathlib import Path

import newsradar.adapters as adapters

SKILLS = Path(__file__).resolve().parent.parent / ".claude" / "skills"


def _adapters():
    for m in pkgutil.iter_modules(adapters.__path__):
        mod = importlib.import_module(f"newsradar.adapters.{m.name}")
        if hasattr(mod, "KIND"):
            yield mod


class T31Skills(unittest.TestCase):
    def test_every_adapter_has_a_skill_naming_its_endpoint(self):
        mods = list(_adapters())
        self.assertGreaterEqual(len(mods), 3)
        for mod in mods:
            with self.subTest(kind=mod.KIND):
                path = SKILLS / mod.KIND / "SKILL.md"
                self.assertTrue(path.exists(), f"missing {path}")
                text = path.read_text()
                self.assertTrue(text.startswith(f"---\nname: {mod.KIND}\n"))
                endpoint = getattr(mod, "FEED", None) or getattr(mod, "BASE")
                self.assertIn(endpoint, text)


if __name__ == "__main__":
    unittest.main()
