import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.ci import merge_command as mc
from tools.ci import nightly_set as ns

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
URL = "https://github.com/horizon-rust/horizon/pull/5#issuecomment-1"
HEAD = "a" * 40
REVIEWER, AUTHOR, OTHER = ns.Actor("duzzl", 301), ns.Actor("dev", 302), ns.Actor("kacpherek", 101)
GUARDED = ns.guarded_patterns("/.github/ @horizon-rust/council\n/launcher/src/backend.rs @horizon-rust/council\n")
LIST = mc.read_grants("301 duzzl until 2026-12-20 /crates/horizon-client/ /tools/test_*_parity.py\n")


def approval(by=REVIEWER, state="APPROVED", commit=HEAD):
    return mc.Review(by.id, by.login, state, commit)


def pull(**changes):
    """A pull request `/merge` accepts: someone else's, open, to main, green, approved by the reviewer at its head."""
    pr = mc.Pull(node="PR_1", state="OPEN", draft=False, base="main", head=HEAD, author_id=AUTHOR.id, queued=False,
                 conflicts=False, checks="SUCCESS", required="SUCCESS", labels=["bug"], reviews=[approval()],
                 reviews_cut=False,
                 changed_files=1, commits=1)
    return replace(pr, **changes)


class Fake:
    def __init__(self, pr=None, files=(("crates/horizon-client/src/game.rs", None),), people=(AUTHOR.id,), past=(),
                 queue_error=None):
        self.pr = pull() if pr is None else pr
        self.changed, self.people, self.past, self.queue_error = list(files), set(people), list(past), queue_error
        self.queued, self.comments = [], []

    def pull(self, n):
        return self.pr

    def files(self, n):
        return self.changed

    def commit_people(self, n):
        return self.people

    def enqueue(self, node, head):
        if self.queue_error:
            raise mc.Refused(self.queue_error)
        self.queued.append((node, head))

    def comment(self, n, text):
        self.comments.append(text)

    def recent_comments(self, since):
        return self.past


def do(api=None, actor=REVIEWER, grants=LIST, now=NOW, switched_on=True):
    api = api or Fake()
    return mc.run(api, actor, 5, URL, grants, GUARDED, now, switched_on), api


class ParseTests(unittest.TestCase):
    def test_the_first_line_decides(self):
        for body in ("/merge", "  /MERGE  ", "\n/merge\nthanks", "🤖 Agent: /merge\nReviewed the diff."):
            self.assertTrue(mc.parse(body), body)
        for body in ("", "merge", "please /merge", "/merged yesterday", "/merge-queue is slow", "/close"):
            self.assertFalse(mc.parse(body), body)

    def test_words_after_it_are_refused(self):
        with self.assertRaises(mc.Refused):
            mc.parse("/merge 123")


class ListTests(unittest.TestCase):
    def test_a_line_names_account_day_and_folders(self):
        grants = mc.read_grants("# trial\n301 duzzl until 2026-12-20 /crates/horizon-world/ /docs/  # added by x\n")
        self.assertEqual([(g.id, g.login, g.until.isoformat(), g.paths) for g in grants.lines],
                         [(301, "duzzl", "2026-12-20", ("/crates/horizon-world/", "/docs/"))])
        self.assertFalse(grants.paused)
        self.assertEqual(grants.skipped, [])

    def test_a_line_in_another_form_grants_nothing(self):
        for line in ("duzzl", "301 duzzl", "301 duzzl /crates/", "301 duzzl until 2026-12-20",
                     "301 duzzl until 2026-13-40 /crates/", "301 duzzl until 20261220 /crates/",
                     "301 duzzl until 2026-12-20 crates/", "301 duzzl until 2026-12-20 /crates/ *",
                     "duzzl 301 until 2026-12-20 /crates/", "301 duzzl since 2026-12-20 /crates/"):
            grants = mc.read_grants(line + "\n")
            self.assertEqual(grants.lines, [], line)
            self.assertEqual(len(grants.skipped), 1, line)

    def test_the_repos_list_reads_whole_and_no_line_reaches_too_far(self):
        grants = mc.read_grants((ROOT / mc.GRANTS).read_text())
        self.assertEqual(grants.skipped, [])
        # A date further away than MAX_DAYS grants nothing until it is that close: such a line would switch its
        # reviewer off, so it fails here, in the pull request that writes it.
        limit = datetime.now(timezone.utc).date() + timedelta(days=mc.MAX_DAYS)
        self.assertEqual([g.login for g in grants.lines if g.until > limit], [])


class WhoTests(unittest.TestCase):
    def test_a_listed_reviewer_queues_someone_elses_pull_request(self):
        text, api = do()
        self.assertEqual(api.queued, [("PR_1", HEAD)])
        self.assertEqual(text, f"🤖 Merge: added to the merge queue at `aaaaaaa`, on behalf of @duzzl "
                               f"([their comment]({URL})).")

    def refused(self, pattern, **how):
        api = how.pop("api", None) or Fake()
        with self.assertRaisesRegex(mc.Refused, pattern):
            do(api, **how)
        self.assertEqual(api.queued, [])

    def test_nobody_else_merges_this_way(self):
        self.refused("Only the merge reviewers", actor=OTHER)
        # By ID: the listed login on another account is nobody.
        self.refused("Only the merge reviewers", actor=ns.Actor("duzzl", 999))

    def test_the_line_works_through_its_last_day_and_not_after(self):
        last_day = datetime(2026, 12, 20, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(len(do(now=last_day)[1].queued), 1)
        self.refused("ended on 2026-12-20", now=last_day + timedelta(minutes=1))

    def test_a_day_more_than_a_month_away_grants_nothing_yet(self):
        far = mc.read_grants("301 duzzl until 2027-06-01 /crates/horizon-client/\n")
        self.refused("more than 31 days", grants=far)
        self.assertEqual(len(do(grants=far, now=datetime(2027, 5, 1, tzinfo=timezone.utc))[1].queued), 1)

    def test_a_new_line_extends_an_ended_one(self):
        both = mc.read_grants("301 duzzl until 2026-10-01 /docs/\n301 duzzl until 2026-12-20 /crates/horizon-client/\n")
        self.assertEqual(len(do(grants=both)[1].queued), 1)
        # The ended line's folders ended with it.
        self.refused("outside the folders", grants=both, api=Fake(files=[("docs/x.md", None)]))

    def test_paused_stops_every_reviewer(self):
        self.refused("paused", grants=mc.read_grants("301 duzzl until 2026-12-20 /crates/horizon-client/\npaused\n"))

    def test_without_the_apps_token_nothing_is_queued(self):
        self.refused("isn't switched on", switched_on=False)

    def test_more_than_the_days_merges_are_refused(self):
        done = {"user": "github-actions[bot]", "body": "🤖 Merge: added to the merge queue at `aaaaaaa`, on behalf "
                                                       "of @duzzl ([their comment](u))."}
        others = [{"user": "github-actions[bot]", "body": done["body"].replace("@duzzl", "@duzzl-two")},
                  {"user": "duzzl", "body": done["body"]},
                  {"user": "github-actions[bot]", "body": "🤖 Merge: refused, x (asked by @duzzl)"}]
        self.assertEqual(len(do(Fake(past=[done] * (mc.MAX_PER_DAY - 1) + others * 5))[1].queued), 1)
        self.refused("in the last 24 hours", api=Fake(past=[done] * mc.MAX_PER_DAY))


class PullRequestTests(unittest.TestCase):
    def refused(self, pattern, api):
        with self.assertRaisesRegex(mc.Refused, pattern):
            do(api)
        self.assertEqual(api.queued, [])

    def test_only_an_open_ready_pull_request_to_main(self):
        for changes, pattern in (({"state": "MERGED"}, "is merged"), ({"state": "CLOSED"}, "is closed"),
                                 ({"base": "release"}, "not main"), ({"draft": True}, "draft"),
                                 ({"queued": True}, "already in the merge queue"),
                                 ({"conflicts": True}, "conflicts with main")):
            self.refused(pattern, Fake(pull(**changes)))

    def test_never_their_own_work(self):
        self.refused("your own pull request", Fake(pull(author_id=REVIEWER.id)))
        self.refused("a commit of yours", Fake(people=(AUTHOR.id, REVIEWER.id)))
        self.refused("too many to read", Fake(pull(commits=251)))

    def test_their_own_approval_of_the_head_is_needed(self):
        self.refused("Approve it first", Fake(pull(reviews=[])))
        self.refused("Approve it first", Fake(pull(reviews=[approval(by=OTHER)])))
        self.refused("Approve it first", Fake(pull(reviews=[approval(state="DISMISSED")])))
        self.refused("earlier commit", Fake(pull(reviews=[approval(commit="b" * 40)])))

    def test_anyones_open_change_request_holds_it(self):
        self.refused("kacpherek's change request",
                     Fake(pull(reviews=[approval(), approval(by=OTHER, state="CHANGES_REQUESTED")])))
        self.refused("more reviewers than", Fake(pull(reviews_cut=True)))

    def test_every_check_of_the_head_passed(self):
        for state, pattern in (("FAILURE", "are failure"), ("PENDING", "are pending"), (None, "are missing")):
            self.refused(pattern, Fake(pull(checks=state)))

    def test_the_required_check_passed_itself(self):
        # A fork's runs wait for approval while the bots' checks pass: together they read SUCCESS without `verify`.
        self.refused("`verify` hasn't passed", Fake(pull(checks="SUCCESS", required=None)))
        self.refused("`verify` is failure", Fake(pull(required="FAILURE")))

    def test_proof_and_release_labels_are_the_teams(self):
        self.refused("needs-proof", Fake(pull(labels=["bug", "needs-proof"])))
        self.refused("Release-Now", Fake(pull(labels=["Release-Now"])))

    def test_githubs_own_refusal_is_the_answer(self):
        self.refused("approving review is required", Fake(queue_error="At least 1 approving review is required"))


class FilesTests(unittest.TestCase):
    def refused(self, pattern, files, count=None, grants=LIST):
        api = Fake(pull(changed_files=len(files) if count is None else count), files=files)
        with self.assertRaisesRegex(mc.Refused, pattern):
            do(api, grants=grants)
        self.assertEqual(api.queued, [])

    def test_every_file_is_in_the_reviewers_folders(self):
        inside = [("crates/horizon-client/src/game/mobs/wither.rs", None), ("tools/test_mob_ai_parity.py", None)]
        self.assertEqual(len(do(Fake(pull(changed_files=2), files=inside))[1].queued), 1)
        self.refused("`crates/horizon-world/src/nbt.rs`, outside the folders",
                     inside + [("crates/horizon-world/src/nbt.rs", None)])
        self.refused("outside the folders", [("crates/horizon-client-render/src/gfx.rs", None)])
        self.refused("outside the folders", [("tools/parity.py", None)])

    def test_a_file_moved_out_of_another_folder_counts_there_too(self):
        self.refused("`launcher/src/main.rs`, outside the folders",
                     [("crates/horizon-client/src/moved.rs", "launcher/src/main.rs")])

    def test_what_codeowners_guards_stays_the_councils_whatever_the_line_says(self):
        everything = mc.read_grants("301 duzzl until 2026-12-20 /\n")
        self.assertEqual(len(do(Fake(files=[("docs/x.md", None)]), grants=everything)[1].queued), 1)
        self.refused("only the council merges", [(".github/workflows/ci.yml", None)], grants=everything)
        self.refused("`launcher/src/backend.rs` and 1 more, which only the council merges",
                     [("docs/x.md", None), ("launcher/src/backend.rs", None), (".github/CODEOWNERS", None)],
                     grants=everything)

    def test_a_list_github_cut_short_is_refused(self):
        self.refused("didn't list every file", [("crates/horizon-client/src/game.rs", None)], count=3001)
        self.refused("didn't list every file", [], count=0)


class CommandTests(unittest.TestCase):
    """command(), as merge-command.yml runs it: the comment in the environment, one answer posted back."""

    def run_command(self, body, author=REVIEWER, token="app-token"):
        api = Fake()
        env = {"REPO": "horizon-rust/horizon", "ISSUE": "5", "AUTHOR": author.login, "AUTHOR_ID": str(author.id),
               "BODY": body, "COMMENT_URL": URL, "GH_TOKEN": "token", "MERGE_TOKEN": token}
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "Api", lambda repo, token: api), \
                mock.patch.object(mc, "read_grants", lambda text: LIST), \
                mock.patch.object(mc, "datetime", mock.Mock(now=lambda tz: NOW)), \
                mock.patch.object(Path, "read_text", lambda self: ""):
            self.assertEqual(mc.command(), 0)
        return api

    def test_a_merge_answers_once(self):
        api = self.run_command("/merge")
        self.assertEqual(api.queued, [("PR_1", HEAD)])
        self.assertEqual(len(api.comments), 1)
        self.assertIn("added to the merge queue", api.comments[0])

    def test_a_refusal_answers_and_queues_nothing(self):
        for api in (self.run_command("/merge", author=OTHER), self.run_command("/merge", token="")):
            self.assertEqual(api.queued, [])
            self.assertEqual(len(api.comments), 1)
            self.assertTrue(api.comments[0].startswith("🤖 Merge: refused, "), api.comments[0])

    def test_any_other_comment_is_left_alone(self):
        api = self.run_command("Looks good to me")
        self.assertEqual((api.queued, api.comments), ([], []))


class WorkflowTests(unittest.TestCase):
    TEXT = (ROOT / ".github/workflows/merge-command.yml").read_text()

    def test_it_fetches_every_file_the_script_reads(self):
        # It checks out main sparsely: a file left out reads as missing and every `/merge` fails.
        checkout = self.TEXT[self.TEXT.index("sparse-checkout: |"):self.TEXT.index("sparse-checkout-cone-mode")]
        for path in (mc.GRANTS, ns.CODEOWNERS, "tools/ci/nightly_set.py", "tools/ci/merge_command.py"):
            self.assertIn(path, checkout)

    def test_it_passes_the_authors_id_and_both_tokens(self):
        self.assertIn("AUTHOR_ID: ${{ github.event.comment.user.id }}", self.TEXT)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.TEXT)
        self.assertIn("MERGE_TOKEN: ${{ steps.app.outputs.token }}", self.TEXT)


if __name__ == "__main__":
    unittest.main()
