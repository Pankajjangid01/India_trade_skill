"""Repo-wide rules: the skill folders, the safety text, and no order code anywhere."""

import ast
import os
import re
import sys
import unittest

from _load import ROOT

SKILLS = ["in-setup", "in-scanner", "in-chain", "in-news", "in-entry", "in-exit", "in-timemachine",
          "in-budget", "in-risk", "in-ticket", "in-alerts", "in-analyst", "in-flows"]   # BRD section 5
MODULES = ["in-common"]    # shared code, not a skill: no SKILL.md
# order endpoints, spelled in pieces so this file does not trip its own check
ORDER_NAMES = ["place" + "Order", "modify" + "Order", "cancel" + "Order", "create" + "Rule", "modify" + "Rule",
               "cancel" + "Rule", "rule" + "List", "rule" + "Details", "gtt" + "/v1", "convert" + "Position"]
ORDER_PATH = re.compile("/" + "order" + r"/(?!v1/getLtpData)", re.I)


def text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".jsonl", ".txt")) or f in ("LICENSE", ".gitignore"):
                yield os.path.join(base, f)


def py_files():
    for path in text_files():
        if path.endswith(".py"):
            yield path


def read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class Repo(unittest.TestCase):
    def test_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))
        for m in MODULES:
            self.assertFalse(os.path.exists(os.path.join(ROOT, m, "SKILL.md")), m)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "in-common", "smart.py")))

    def test_frontmatter_and_the_safety_text(self):
        for s in SKILLS:
            text = read(os.path.join(ROOT, s, "SKILL.md"))
            m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            self.assertTrue(m, s)
            self.assertIn("name: %s\n" % s, m.group(1) + "\n", s)
            self.assertGreater(len(m.group(1)), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("never places", low, s)

    def test_no_order_endpoint_anywhere(self):
        for path in text_files():
            text = read(path)
            rel = os.path.relpath(path, ROOT)
            for name in ORDER_NAMES:
                self.assertNotIn(name.lower(), text.lower(), rel)
            self.assertIsNone(ORDER_PATH.search(text), rel)

    def test_no_em_dashes(self):
        for path in text_files():
            self.assertNotIn(chr(0x2014), read(path), os.path.relpath(path, ROOT))

    def test_gitignore_keeps_the_working_folder_and_secrets_out(self):
        lines = read(os.path.join(ROOT, ".gitignore")).split()
        for must in ("trader/", ".env"):
            self.assertIn(must, lines)

    def test_python_39_syntax_and_a_fixed_ist_offset(self):
        # IST is a fixed offset: the time-zone database module needs tzdata, which Windows does not ship
        for path in py_files():
            src = read(path)
            ast.parse(src, filename=path, feature_version=(3, 9))
            self.assertNotIn("zone" + "info", src, path)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "needs Python 3.10+ to list the standard library")
    def test_standard_library_only(self):
        ours = {"smart", "_load", "firstrun"}
        for path in py_files():
            for node in ast.walk(ast.parse(read(path))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
                for n in names:
                    top = n.split(".")[0]
                    self.assertTrue(top in sys.stdlib_module_names or top in ours,
                                    "%s imports %s" % (os.path.relpath(path, ROOT), n))

    def test_readme_names_every_skill_and_the_fine_print(self):
        readme = " ".join(read(os.path.join(ROOT, "README.md")).split())
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)
        self.assertIn("Not financial advice", readme)


if __name__ == "__main__":
    unittest.main()
