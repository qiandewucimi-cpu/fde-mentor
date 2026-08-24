from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]


class SkillPackageTests(unittest.TestCase):
    def test_required_package_files_exist(self) -> None:
        required = {
            "SKILL.md",
            "agents/openai.yaml",
            "references/curriculum.md",
            "references/daily-delivery.md",
            "references/quality-gates.md",
            "scripts/selfcheck.py",
            "LICENSE",
        }
        missing = sorted(path for path in required if not (REPO_ROOT / path).is_file())
        self.assertEqual(missing, [], f"missing package files: {missing}")

    def test_skill_frontmatter_is_discriminating_and_complete(self) -> None:
        content = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8-sig")
        self.assertTrue(content.startswith("---\n"), "SKILL.md must start with YAML")
        closing = content.find("\n---\n", 4)
        self.assertGreater(closing, 4, "SKILL.md frontmatter is not closed")
        frontmatter = content[4:closing]

        name_match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", frontmatter)
        description_match = re.search(
            r'(?m)^description:\s*"([^\n]+)"\s*$', frontmatter
        )
        self.assertIsNotNone(name_match, "frontmatter needs a valid name")
        self.assertIsNotNone(description_match, "description must be a quoted line")
        self.assertEqual(name_match.group(1), "fde-mentor")
        description = description_match.group(1)
        self.assertGreaterEqual(len(description), 30)
        self.assertLessEqual(len(description), 1024)
        self.assertIn("FDE", description)
        self.assertIn("不应", description)

    def test_openai_interface_metadata_matches_skill(self) -> None:
        content = (REPO_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8-sig"
        )
        display = re.search(r'(?m)^  display_name: "([^\n]+)"$', content)
        short = re.search(r'(?m)^  short_description: "([^\n]+)"$', content)
        prompt = re.search(r'(?m)^  default_prompt: "([^\n]+)"$', content)
        self.assertIsNotNone(display)
        self.assertIsNotNone(short)
        self.assertIsNotNone(prompt)
        self.assertEqual(display.group(1), "FDE Mentor")
        self.assertGreaterEqual(len(short.group(1)), 25)
        self.assertLessEqual(len(short.group(1)), 64)
        self.assertIn("$fde-mentor", prompt.group(1))

    def test_all_local_markdown_links_resolve(self) -> None:
        broken: list[str] = []
        link_pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
        for markdown in REPO_ROOT.rglob("*.md"):
            if ".git" in markdown.parts:
                continue
            content = markdown.read_text(encoding="utf-8-sig")
            for raw_target in link_pattern.findall(content):
                target = raw_target.strip().strip("<>")
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                path_text = unquote(target.split("#", 1)[0])
                resolved = (markdown.parent / path_text).resolve()
                if not resolved.exists():
                    broken.append(f"{markdown.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(broken, [], "broken local links: " + "; ".join(broken))

    def test_release_text_has_no_private_path_or_scaffold_placeholder(self) -> None:
        banned = (
            "[" + "你的名字" + "]",
            "C:" + "\\" + "Users" + "\\",
            "<" + "MODEL_NAME" + ">",
            "FIX" + "ME",
            "T" + "BD",
        )
        offenders: list[str] = []
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.suffix == ".pyc":
                continue
            if path.suffix.lower() not in {".md", ".yaml", ".yml", ".py", ""}:
                continue
            content = path.read_text(encoding="utf-8-sig")
            for marker in banned:
                if marker in content:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {marker}")
        self.assertEqual(offenders, [], "release placeholders found: " + "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
