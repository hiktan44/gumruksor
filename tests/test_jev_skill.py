"""Jev yetenekleri depodaki koda bağlı kalır.

Proje yeteneği kod adlarına ve ortam değişkenlerine atıf yapar; kod değişip yetenek
eskirse bu test kırmızıya döner. Resmî TypeSafe yeteneği lisansıyla birlikte durur.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return {}
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in match.group(1).splitlines() if re.match(r"^\w[\w-]*:", line))
    }


class JevSkillTests(unittest.TestCase):
    def test_both_skills_have_a_name_and_description(self):
        for name in ("typesafe-ai", "gumruksor-jev"):
            meta = _frontmatter(SKILLS / name / "SKILL.md")
            self.assertEqual(meta.get("name"), name)
            self.assertIn("description", meta)

    def test_the_official_skill_keeps_its_mit_licence(self):
        licence = (SKILLS / "typesafe-ai" / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", licence)
        self.assertIn("Copyright (c) 2026 TypeSafe AI", licence)

    def test_the_project_skill_names_only_symbols_that_exist(self):
        skill = (SKILLS / "gumruksor-jev" / "SKILL.md").read_text(encoding="utf-8")
        code = (ROOT / "customs_advisor.py").read_text(encoding="utf-8") + (
            ROOT / "typesafe_client.py"
        ).read_text(encoding="utf-8")
        for symbol in (
            "_jev_narrow",
            "_jev_state",
            "_jev_min_confidence",
            "_JEV_NONE_OPTION",
            "JEV_NARROWING_ENABLED",
            "JEV_MIN_CONFIDENCE",
            "TYPESAFE_API_KEY",
            "TypeSafeClient",
            "redact_text",
            "validate_outbound_url",
            "api.typesafe.ai/v1/systemone",
        ):
            self.assertIn(symbol, skill, symbol)
            self.assertIn(symbol, code, symbol)
        self.assertIn('_JEV_NONE_OPTION = "hicbiri"', code)
        self.assertIn("`\"hicbiri\"`", skill)

    def test_no_secret_looking_value_is_committed_in_the_skills(self):
        for path in SKILLS.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?i)(sk-|ts_)[a-z0-9]{16,}", str(path))


if __name__ == "__main__":
    unittest.main()
