"""Phase 13: docs integrity — every relative markdown link in README.md and
docs/*.md must resolve to an existing file (broken entry links fail the suite).

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

REPO = Path(__file__).resolve().parent.parent.parent
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class DocsLinkTests(unittest.TestCase):
    def test_all_relative_links_resolve(self):
        broken = []
        docs = [REPO / "README.md"] + sorted((REPO / "docs").glob("*.md"))
        for doc in docs:
            for m in _LINK_RE.finditer(doc.read_text(encoding="utf-8")):
                target = m.group(1).split("#")[0]
                if not target or target.startswith(("http://", "https://")):
                    continue
                if not (doc.parent / target).resolve().exists():
                    broken.append("%s -> %s" % (doc.relative_to(REPO), target))
        self.assertEqual(broken, [], "broken doc links: %s" % broken)


if __name__ == "__main__":
    unittest.main()
