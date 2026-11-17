"""Every action from outside the repository is pinned to a commit (docs/security.md).

A tag can be moved to other code; a commit can't. Dependabot (.github/dependabot.yml) updates the pins.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^\s'\"#]+)", re.M)
PINNED = re.compile(r"@[0-9a-f]{40}$")


def remote_uses():
    for path in sorted((ROOT / ".github").rglob("*")):
        if path.suffix not in (".yml", ".yaml"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in USES.finditer(text):
            ref = match.group(1)
            if not ref.startswith("./"):
                line = text.count("\n", 0, match.start(1)) + 1
                yield f"{path.relative_to(ROOT).as_posix()}:{line}", ref


class PinnedActionsTests(unittest.TestCase):
    def test_remote_actions_are_pinned_to_a_commit(self):
        uses = list(remote_uses())
        self.assertTrue(uses, "no remote actions found: the pattern no longer matches the workflows")
        unpinned = [f"{where}: {ref}" for where, ref in uses if not PINNED.search(ref)]
        self.assertEqual(unpinned, [], "pin these to a full commit SHA, with the version in a comment")

    def test_pattern_rejects_tags_and_short_shas(self):
        for ref in ["actions/checkout@v7", "a/b@stable", "a/b@d1527c8", "a/b@" + "A" * 40]:
            with self.subTest(ref=ref):
                self.assertIsNone(PINNED.search(ref))
        self.assertIsNotNone(PINNED.search("a/b/path@" + "0f" * 20))


if __name__ == "__main__":
    unittest.main()
