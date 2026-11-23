import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.ci import merge_command as mc
from tools.ci import nightly_set as ns

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
URL = "https://github.com/horizon-rust/horizon/pull/5#issuecomment-1"
HEAD, BASE, MERGE = "a" * 40, "c" * 40, "d" * 40
ENTRY = "MQE_test"
APP_BOT_ID = 14673881
ENQUEUED = "2026-11-23T12:00:00Z"
REVIEWER, AUTHOR, OTHER = ns.Actor("duzzl", 301), ns.Actor("dev", 302), ns.Actor("kacpherek", 101)
GUARDED = ns.guarded_patterns("/.github/ @horizon-rust/council\n/launcher/src/backend.rs @horizon-rust/council\n")
LIST = mc.read_grants("301 duzzl until 2026-12-20 /crates/horizon-client/ /tools/test_*_parity.py\n")


def approval(by=REVIEWER, state="APPROVED", commit=HEAD):
    return mc.Review(by.id, by.login, state, commit)


def queued_answer(actor=REVIEWER, head=HEAD, entry=ENTRY):
    return (f"🤖 Merge: added to the merge queue at `{head[:7]}`, on behalf of @{actor.login} "
            f"([their comment]({URL})). <!-- merge-by:{actor.id} head:{head} queue:{entry} -->")


def pull(**changes):
    """A pull request `/merge` accepts: someone else's, open, to main, green, approved by the reviewer at its head."""
    pr = mc.Pull(node="PR_1", state="OPEN", draft=False, base="main", base_oid=BASE, head=HEAD, merge_oid=MERGE,
                 author_id=AUTHOR.id, queued=False, conflicts=False, checks="SUCCESS", labels=["bug"], reviews=[approval()],
                 reviews_cut=False, changed_files=1, people={AUTHOR.id}, people_cut=False)
    return replace(pr, **changes)


class Fake:
    def __init__(self, pr=None, files=(("crates/horizon-client/src/game.rs", None),), verify=("success",), past=(),
                 queue_error=None, later=None, applied=None):
        self.pr = pull() if pr is None else pr
        self.later = later  # the pull request as a second read finds it; None: unchanged
        self.changed, self.verify, self.past, self.queue_error = list(files), list(verify), list(past), queue_error
        self.applied = self.changed if applied is None else list(applied)
        self.queued, self.comments, self.compared, self.reads = [], [], [], 0

    def pull(self, n):
        self.reads += 1
        return self.later if self.later is not None and self.reads > 1 else self.pr

    def required_check(self, head):
        return self.verify

    def main_oid(self):
        return self.pr.base_oid

    def files(self, base, head):
        self.compared.append((base, head))
        return self.applied if head == MERGE else self.changed

    def enqueue(self, node, head):
        if self.queue_error:
            raise mc.Refused(self.queue_error)
        self.queued.append((node, head))
        return ENTRY

    def comment(self, n, text):
        self.comments.append(text)

    def dequeue(self, number, node, entry):
        self.queued.remove((node, self.pr.head))

    def recent_answers(self, since):
        return self.past


def do(api=None, actor=REVIEWER, grants=LIST, now=NOW, switched_on=True, later=None, policy_oid=BASE):
    api = api or Fake()
    clock = mock.Mock(side_effect=[now, now if later is None else later])
    with mock.patch.object(mc, "datetime", mock.Mock(now=clock)):
        return mc.run(api, actor, 5, URL, grants, GUARDED, policy_oid, switched_on), api


class ParseTests(unittest.TestCase):
    def test_the_first_line_decides(self):
        for body in ("/merge", "/MERGE  ", "/merge\nthanks", "🤖 Agent: /merge\nReviewed the diff."):
            self.assertTrue(mc.parse(body), body)
        for body in ("", "merge", "please /merge", "\n/merge", "  /merge", "🤖 Agent:  /merge", "/merged yesterday",
                     "/merge-queue is slow", "/close"):
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
                     "301 duzzl until 2026-12-20 /crates/**", "301 duzzl until 2026-12-20 /crates/**/x.rs",
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
        self.assertEqual(text, queued_answer())

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

    def test_a_grant_expiring_during_the_check_is_refused(self):
        last_day = datetime(2026, 12, 20, 23, 59, tzinfo=timezone.utc)
        api = Fake()
        self.refused("ended on 2026-12-20", api=api, now=last_day, later=last_day + timedelta(minutes=1))
        self.assertEqual(api.reads, 2)

    def test_a_second_live_grant_does_not_keep_the_expired_folders(self):
        grants = mc.read_grants("301 duzzl until 2026-12-20 /crates/horizon-client/\n"
                                "301 duzzl until 2026-12-21 /crates/horizon-world/\n")
        last_day = datetime(2026, 12, 20, 23, 59, tzinfo=timezone.utc)
        api = Fake(files=[("crates/horizon-world/src/nbt.rs", None)],
                   applied=[("crates/horizon-client/src/game.rs", None)])
        self.refused("outside the folders", api=api, grants=grants,
                     now=last_day, later=last_day + timedelta(minutes=1))

    def test_a_checkout_with_an_older_policy_is_refused(self):
        api = Fake()
        self.refused("Main moved since the workflow checked out", api=api, policy_oid="b" * 40)
        self.assertEqual(api.compared, [])

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
        done = queued_answer()
        # Someone else's merges, and a refusal that quotes a file named like the mark, are not this reviewer's.
        others = [done.replace("merge-by:301 ", "merge-by:3011 "), done.replace("merge-by:301 ", "merge-by:30 "),
                  "🤖 Merge: refused, It changes `x<!-- merge-by:301 -->` (asked by @duzzl)"]
        at_limit = [queued_answer(entry=f"MQE_{i}") for i in range(mc.MAX_PER_DAY)]
        # A retried POST that created two receipts for one entry still counts once.
        self.assertEqual(len(do(Fake(past=at_limit[:-1] * 2 + others * 5))[1].queued), 1)
        self.refused("in the last 24 hours", api=Fake(past=at_limit))


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
        self.refused("a commit of yours", Fake(pull(people={AUTHOR.id, REVIEWER.id})))
        self.refused("too many commits or authors", Fake(pull(people_cut=True)))

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
        self.refused("`verify` hasn't run", Fake(verify=[]))
        for runs in (["failure"], [None], ["success", "failure"]):
            self.refused("`verify` hasn't passed", Fake(verify=runs))

    def test_what_changed_while_it_was_checked_holds_it(self):
        self.refused("needs-proof", Fake(later=pull(labels=["needs-proof"])))
        self.refused("change request", Fake(later=pull(reviews=[approval(), approval(OTHER, "CHANGES_REQUESTED")])))
        self.refused("new commit", Fake(later=pull(head="b" * 40, reviews=[approval(commit="b" * 40)])))

    def test_main_moving_during_the_check_holds_it(self):
        self.refused("Main moved while this was checking it", Fake(later=pull(base_oid="b" * 40, merge_oid=None)))

    def test_main_moving_during_the_verify_lookup_holds_it(self):
        api = Fake()
        with mock.patch.object(api, "required_check", wraps=api.required_check) as verify:
            def main_oid():
                verify.assert_called_once_with(HEAD)
                return "b" * 40

            with mock.patch.object(api, "main_oid", side_effect=main_oid):
                self.refused("Main moved while this was checking it", api)

    def test_verify_can_fail_during_the_quota_scan(self):
        api = Fake(verify=("success",))

        def scan(since):
            api.verify = ["failure"]
            return []

        with mock.patch.object(api, "recent_answers", side_effect=scan):
            self.refused("`verify` hasn't passed", api)

    def test_a_test_merge_is_required_and_must_still_match_on_the_final_read(self):
        self.refused("doesn't have a test merge", Fake(pull(merge_oid=None)))
        for merge_oid in (None, "b" * 40):
            self.refused("test merge changed", Fake(later=pull(merge_oid=merge_oid)))

    def test_proof_and_release_labels_are_the_teams(self):
        self.refused("needs-proof", Fake(pull(labels=["bug", "needs-proof"])))
        self.refused("Release-Now", Fake(pull(labels=["Release-Now"])))

    def test_githubs_own_refusal_is_the_answer(self):
        api = Fake(queue_error="At least 1 approving review is required")
        self.refused("approving review is required", api)
        self.assertEqual(api.comments, [])

    def test_a_timed_out_receipt_post_is_retried_before_queue_removal(self):
        api = Fake()

        def retry(n, receipt):
            self.assertEqual(api.queued, [("PR_1", HEAD)])
            api.comments.append(receipt)
            if len(api.comments) == 1:
                raise subprocess.TimeoutExpired("gh", mc.WRITE_TIMEOUT)

        with mock.patch.object(api, "comment", side_effect=retry), mock.patch.object(mc, "sleep"):
            self.assertEqual(do(api)[0], queued_answer())
        self.assertEqual(api.queued, [("PR_1", HEAD)])
        self.assertEqual(api.comments, [queued_answer(), queued_answer()])

    def test_persistent_receipt_post_failure_removes_the_original_entry(self):
        api = Fake()
        with mock.patch.object(api, "comment", side_effect=RuntimeError("GitHub unavailable")), \
                mock.patch.object(mc, "sleep"):
            self.refused("original entry is no longer queued", api)
        self.assertEqual(api.comments, [])

    def test_unconfirmed_queue_removal_does_not_publish_a_success_receipt(self):
        api = Fake()
        with mock.patch.object(api, "comment", side_effect=RuntimeError("GitHub unavailable")), \
                mock.patch.object(api, "dequeue", side_effect=RuntimeError("GitHub unavailable")), \
                mock.patch.object(mc, "sleep"):
            with self.assertRaisesRegex(mc.Refused, "couldn't publish the receipt or confirm queue removal"):
                do(api)
        self.assertEqual(api.comments, [])


class FilesTests(unittest.TestCase):
    def refused(self, pattern, files, count=None, grants=LIST):
        api = Fake(pull(changed_files=len(files) if count is None else count), files=files)
        with self.assertRaisesRegex(mc.Refused, pattern):
            do(api, grants=grants)
        self.assertEqual(api.queued, [])

    def test_every_file_is_in_the_reviewers_folders(self):
        inside = [("crates/horizon-client/src/game/mobs/wither.rs", None), ("tools/test_mob_ai_parity.py", None)]
        api = do(Fake(pull(changed_files=2), files=inside))[1]
        self.assertEqual(len(api.queued), 1)
        # Read between the two commits the queue takes, not from the branch, which can move meanwhile.
        self.assertEqual(api.compared, [(BASE, HEAD), (BASE, MERGE)])
        self.refused("`crates/horizon-world/src/nbt.rs`, outside the folders",
                     inside + [("crates/horizon-world/src/nbt.rs", None)])
        self.refused("outside the folders", [("crates/horizon-client-render/src/gfx.rs", None)])
        self.refused("outside the folders", [("tools/parity.py", None)])

    def test_a_folder_or_file_pattern_covers_no_more_than_it_names(self):
        paths = ["/crates/horizon-client/", "/tools/test_*_parity.py", "/docs/a.md", "/crates/horizon-w*/"]
        for path in ("crates/horizon-client/src/x.rs", "tools/test_mob_ai_parity.py", "docs/a.md",
                     "crates/horizon-world/src/nbt.rs"):
            self.assertTrue(mc.covers(path, paths), path)
        for path in ("crates/horizon-client-render/src/x.rs", "crates/horizon-client", "Crates/horizon-client/x.rs",
                     "tools/test_a_parity.py/evil.py", "tools/test_a/b_parity.py", "docs/a.md/x", "docs/a.mdx",
                     "tools/ci/test_x_parity.py", "x/crates/horizon-client/y.rs"):
            self.assertFalse(mc.covers(path, paths), path)

    def test_a_files_name_cannot_write_into_the_answer(self):
        with self.assertRaises(mc.Refused) as refused:
            do(Fake(files=[("docs/a`\n@everyone <!-- merge-by:301 -->", None)]))
        self.assertIn("`docs/a???everyone???--?merge-by?301?--?`", str(refused.exception))

    def test_a_file_moved_out_of_another_folder_counts_there_too(self):
        self.refused("`launcher/src/main.rs`, outside the folders",
                     [("crates/horizon-client/src/moved.rs", "launcher/src/main.rs")])

    def test_main_renaming_a_file_out_of_scope_holds_the_applied_diff(self):
        api = Fake(files=[("crates/horizon-client/src/lan/conn.rs", None)],
                   applied=[("crates/horizon-world/src/lan/conn.rs", None)])
        with self.assertRaisesRegex(mc.Refused, "`crates/horizon-world/src/lan/conn.rs`, outside the folders"):
            do(api)
        self.assertEqual(api.queued, [])

    def test_a_guarded_path_in_the_applied_diff_holds_it_even_with_a_grant(self):
        api = Fake(applied=[(".github/workflows/ci.yml", None)])
        everything = mc.read_grants("301 duzzl until 2026-12-20 /\n")
        with self.assertRaisesRegex(mc.Refused, "`\\.github/workflows/ci.yml`, which only the council merges"):
            do(api, grants=everything)
        self.assertEqual(api.queued, [])

    def test_what_codeowners_guards_stays_the_councils_whatever_the_line_says(self):
        everything = mc.read_grants("301 duzzl until 2026-12-20 /\n")
        self.assertEqual(len(do(Fake(files=[("docs/x.md", None)]), grants=everything)[1].queued), 1)
        self.refused("only the council merges", [(".github/workflows/ci.yml", None)], grants=everything)
        self.refused("`launcher/src/backend.rs` and 1 more, which only the council merges",
                     [("docs/x.md", None), ("launcher/src/backend.rs", None), (".github/CODEOWNERS", None)],
                     grants=everything)

    def test_a_list_github_cut_short_is_refused(self):
        self.refused("didn't list every file", [("crates/horizon-client/src/game.rs", None)], count=2)
        self.refused("didn't list every file", [("crates/horizon-client/src/game.rs", None)] * mc.MAX_FILES)
        self.refused("didn't list every file", [], count=0)


class MergeGroupTests(unittest.TestCase):
    INSIDE = Path("crates/horizon-client/src/game/blocks/block.rs")
    OUTSIDE = Path("unowned/block.rs")

    def setUp(self):
        self.enterContext(chdir(self.enterContext(tempfile.TemporaryDirectory())))
        mc.git("init", "-q")
        mc.git("config", "user.name", "Fixture")
        mc.git("config", "user.email", "fixture@example.invalid")
        mc.git("config", "commit.gpgSign", "false")
        self.write(mc.GRANTS, "301 duzzl until 2026-12-20 /crates/horizon-client/src/game/blocks/\n")
        self.write(ns.CODEOWNERS, "/.github/ @horizon-rust/council\n")
        self.write(self.INSIDE, "contents\n")
        self.base = self.commit()
        self.api = mock.Mock()
        self.api.delegation_answers.return_value = {5: [queued_answer()]}
        self.api.queue_labels.return_value = {5: ["bug"]}
        self.enterContext(mock.patch.object(mc, "datetime", mock.Mock(now=lambda tz: NOW)))
        self.enterContext(mock.patch.object(mc, "sleep"))

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        mc.git("add", "--", ".")
        mc.git("commit", "-qm", "Fixture squash")
        return mc.git("rev-parse", "HEAD").decode().strip()

    def entry(self, squash, parent=None, **changes):
        entry = mc.QueueEntry(ENTRY, 5, HEAD, parent or self.base, squash, APP_BOT_ID, ENQUEUED)
        return replace(entry, **changes)

    def check(self, *entries):
        self.api.queue_entries.return_value = (self.base, list(entries))
        mc.verify_group(self.api, self.base, entries[-1].squash, APP_BOT_ID)

    def test_matching_app_bot_id_checks_the_receipt_and_in_scope_squash(self):
        self.write(self.INSIDE, "updated\n")
        entry = self.entry(self.commit())
        self.check(entry)
        self.api.delegation_answers.assert_called_once_with([entry])

    def test_a_receipt_that_arrives_after_verify_starts_can_pass(self):
        self.write(self.INSIDE, "updated\n")
        self.api.delegation_answers.side_effect = [{5: []}, {5: [queued_answer()]}]
        self.check(self.entry(self.commit()))

    def test_hold_labels_added_while_waiting_for_the_receipt_fail_the_group(self):
        self.write(self.INSIDE, "updated\n")
        entry = self.entry(self.commit())
        for label in ("needs-proof", "Release-Now"):
            with self.subTest(label=label):
                def appears(entries):
                    self.api.queue_labels.return_value = {5: ["bug", label]}
                    return {5: [queued_answer()]}

                self.api.queue_labels.return_value = {5: ["bug"]}
                self.api.delegation_answers.side_effect = appears
                with self.assertRaisesRegex(mc.Refused, f"#5: This pull request is labelled `{label}`"):
                    self.check(entry)

    def test_an_unconfigured_app_leaves_other_bot_enqueues_alone(self):
        self.write(self.OUTSIDE, "ordinary bot edit\n")
        entry = self.entry(self.commit(), bot_id=APP_BOT_ID + 1)
        self.api.queue_entries.return_value = (self.base, [entry])
        env = {"REPO": "horizon-rust/horizon", "BASE_SHA": self.base, "HEAD_SHA": entry.squash}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mc, "Api", return_value=self.api), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(mc.verify_merge_group(), 0)
        self.api.queue_entries.assert_not_called()

    def test_another_bot_id_skips_delegation_with_the_app_configured(self):
        self.write(self.OUTSIDE, "ordinary bot edit\n")
        self.check(self.entry(self.commit(), bot_id=APP_BOT_ID + 1))
        self.api.delegation_answers.assert_not_called()

    def test_rename_requires_both_paths_to_be_in_scope(self):
        self.OUTSIDE.parent.mkdir()
        mc.git("mv", str(self.INSIDE), str(self.OUTSIDE))
        moved_out = self.commit()
        with self.assertRaisesRegex(mc.Refused, "unowned/block.rs.*outside the folders"):
            self.check(self.entry(moved_out))

        self.base = moved_out
        mc.git("mv", str(self.OUTSIDE), str(self.INSIDE))
        with self.assertRaisesRegex(mc.Refused, "unowned/block.rs.*outside the folders"):
            self.check(self.entry(self.commit()))

    def test_expired_grant_fails_the_group_subcommand(self):
        self.write(mc.GRANTS, "301 duzzl until 2026-11-22 /crates/horizon-client/src/game/blocks/\n")
        self.base = self.commit()
        self.write(self.INSIDE, "updated\n")
        head = self.commit()
        self.api.queue_entries.return_value = (self.base, [self.entry(head)])
        env = {"REPO": "horizon-rust/horizon", "BASE_SHA": self.base, "HEAD_SHA": head,
               "MERGE_APP_BOT_ID": str(APP_BOT_ID)}
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "Api", return_value=self.api), \
                mock.patch.object(mc.sys, "stderr", new_callable=io.StringIO) as err:
            self.assertEqual(mc.verify_merge_group(), 1)
        self.assertIn("Your line in `.github/merge-reviewers.txt` ended on 2026-11-22", err.getvalue())

    def test_maintainer_passes_even_with_an_old_app_answer(self):
        self.write(".github/workflows/ci.yml", "maintainer update\n")
        self.check(self.entry(self.commit(), bot_id=None))
        self.api.delegation_answers.assert_not_called()

    def test_batch_checks_each_delegated_squash_against_its_parent(self):
        self.OUTSIDE.parent.mkdir()
        mc.git("mv", str(self.INSIDE), str(self.OUTSIDE))
        moved = self.commit()
        first = self.entry(moved, number=4, bot_id=None)
        self.write(self.INSIDE.parent / "other.rs", "another block\n")
        self.check(first, self.entry(self.commit(), parent=moved))

        mc.git("reset", "--hard", moved)
        self.write(self.OUTSIDE, "same original block, now updated\n")
        with self.assertRaisesRegex(mc.Refused, "#5: It changes `unowned/block.rs`, outside the folders"):
            self.check(first, self.entry(self.commit(), parent=moved))

    def test_terminal_maintainer_entry_does_not_hide_an_earlier_delegated_violation(self):
        self.write(self.OUTSIDE, "outside\n")
        outside = self.commit()
        self.write(self.INSIDE, "ordinary maintainer edit\n")
        terminal = self.entry(self.commit(), parent=outside, number=6, bot_id=None)
        with self.assertRaisesRegex(mc.Refused, "#5: It changes `unowned/block.rs`, outside the folders"):
            self.check(self.entry(outside), terminal)

    def test_guarded_paths_are_refused_even_with_a_repository_wide_grant(self):
        self.write(mc.GRANTS, "301 duzzl until 2026-12-20 /\n")
        self.base = self.commit()
        self.write("tools/ci/example.py", "guarded update\n")
        with self.assertRaisesRegex(mc.Refused, "#5: It changes `tools/ci/example.py`.*CODEOWNERS"):
            self.check(self.entry(self.commit()))

    def test_prior_entry_can_revoke_the_grant(self):
        self.write(mc.GRANTS, Path(mc.GRANTS).read_text() + "paused\n")
        paused = self.commit()
        self.write(self.INSIDE, "updated\n")
        first = self.entry(paused, number=4, bot_id=None)
        with self.assertRaisesRegex(mc.Refused, "#5: `/merge` is paused"):
            self.check(first, self.entry(self.commit(), parent=paused))

    def test_app_answer_has_to_match_the_entry_and_reviewed_head(self):
        self.write(self.INSIDE, "updated\n")
        entry = self.entry(self.commit())
        self.api.delegation_answers.return_value = {5: [queued_answer(entry="MQE_earlier"),
                                                        queued_answer(head="b" * 40)]}
        with self.assertRaisesRegex(mc.Refused, "no `/merge` receipt for this queue entry and head"):
            self.check(entry)


def answer(**changes):
    """GitHub's answer to QUERY for a pull request `/merge` accepts, as read_pull() takes it."""
    commit = {"oid": HEAD, "committer": {"user": {"databaseId": AUTHOR.id}},
              "authors": {"totalCount": 1, "nodes": [{"user": {"databaseId": AUTHOR.id}}]}}
    pr = {"id": "PR_1", "state": "OPEN", "isDraft": False, "baseRefName": "main",
          "baseRef": {"target": {"oid": BASE}},
          "potentialMergeCommit": {"oid": MERGE, "parents": {"nodes": [{"oid": BASE}, {"oid": HEAD}]}},
          "headRefOid": HEAD, "isInMergeQueue": False, "mergeable": "MERGEABLE", "changedFiles": 1,
          "author": {"databaseId": AUTHOR.id}, "labels": {"nodes": [{"name": "bug"}]},
          "head": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
          "commits": {"totalCount": 1, "nodes": [{"commit": commit}]},
          "latestOpinionatedReviews": {"pageInfo": {"hasNextPage": False}, "nodes": [
              {"state": "APPROVED", "commit": {"oid": HEAD}, "author": {"login": "duzzl", "databaseId": 301}}]}}
    pr.update(changes)
    return {"data": {"repository": {"pullRequest": pr}}}


class AnswerTests(unittest.TestCase):
    """What GitHub answers, read into the pull request the rules judge."""

    def test_enqueue_reply_must_identify_the_configured_app_bot(self):
        api = mc.Api("horizon-rust/horizon", "app-token", APP_BOT_ID)
        for bot_id in (APP_BOT_ID, APP_BOT_ID + 1):
            reply = {"data": {"enqueuePullRequest": {"mergeQueueEntry": {
                "id": ENTRY, "enqueuer": {"databaseId": bot_id}}}}}
            result = subprocess.CompletedProcess([], 0, json.dumps(reply), "")
            with self.subTest(bot_id=bot_id), mock.patch.object(api, "queue_mutation", return_value=result):
                if bot_id == APP_BOT_ID:
                    self.assertEqual(api.enqueue("PR_1", HEAD), ENTRY)
                else:
                    with self.assertRaisesRegex(mc.Refused, "didn't confirm a queue entry from this app"):
                        api.enqueue("PR_1", HEAD)

    def test_receipt_cleanup_leaves_replacement_queue_entries_alone(self):
        api = mc.Api("horizon-rust/horizon", "app-token", APP_BOT_ID)
        replacement = {"pullRequest": {"mergeQueueEntry": {"id": "MQE_replacement", "enqueuer": {}}}}
        with mock.patch.object(api, "repository", return_value=replacement), \
                mock.patch.object(api, "queue_mutation") as mutation:
            api.dequeue(5, "PR_1", ENTRY)
        mutation.assert_not_called()

    def test_receipt_cleanup_dequeues_the_confirmed_app_entry(self):
        api = mc.Api("horizon-rust/horizon", "app-token", APP_BOT_ID)
        active = {"pullRequest": {"mergeQueueEntry": {"id": ENTRY, "enqueuer": {"databaseId": APP_BOT_ID}}}}
        reply = {"data": {"dequeuePullRequest": {"mergeQueueEntry": {"id": ENTRY}}}}
        result = subprocess.CompletedProcess([], 0, json.dumps(reply), "")
        with mock.patch.object(api, "repository", return_value=active), \
                mock.patch.object(api, "queue_mutation", return_value=result) as mutation:
            api.dequeue(5, "PR_1", ENTRY)
        mutation.assert_called_once_with(mc.DEQUEUE, "PR_1")

    def test_queue_attribution_only_reads_the_workflow_bots_answers(self):
        comments = [{"author": {}, "body": queued_answer(actor=OTHER), "createdAt": ENQUEUED},
                    {"author": {"databaseId": 136622811}, "body": queued_answer(actor=OTHER), "createdAt": ENQUEUED},
                    {"author": {"databaseId": mc.ACTIONS_BOT}, "body": queued_answer(), "createdAt": ENQUEUED}]
        api = mc.Api("horizon-rust/horizon")
        entry = mc.QueueEntry(ENTRY, 5, HEAD, BASE, MERGE, APP_BOT_ID, ENQUEUED)
        page = {"nodes": comments, "pageInfo": {"hasPreviousPage": False}}
        with mock.patch.object(api, "repository", return_value={"pr5": {"comments": page}}):
            self.assertEqual(api.delegation_answers([entry]), {5: [queued_answer()]})

    def test_queue_receipt_survives_more_than_thirty_newer_comments(self):
        entry = mc.QueueEntry(ENTRY, 5, HEAD, BASE, MERGE, APP_BOT_ID, ENQUEUED)
        receipt = {"author": {"databaseId": mc.ACTIONS_BOT}, "body": queued_answer(), "createdAt": ENQUEUED}
        old_reply = {**receipt, "createdAt": "2026-11-23T11:59:59Z", "body": queued_answer(actor=OTHER)}
        newer = {"author": {}, "body": "ordinary comment", "createdAt": "2026-11-23T12:01:00Z"}
        newest_page = {"nodes": [newer] * 30, "pageInfo": {"hasPreviousPage": True, "startCursor": "page-2"}}
        older_page = {"nodes": [old_reply, receipt, newer],
                      "pageInfo": {"hasPreviousPage": True, "startCursor": "page-3"}}
        api = mc.Api("horizon-rust/horizon")
        with mock.patch.object(api, "repository", side_effect=[{"pr5": {"comments": p}}
                                                               for p in (newest_page, older_page)]) as read:
            self.assertEqual(api.delegation_answers([entry]), {5: [queued_answer()]})
        self.assertIn('comments(last: 30, before: "page-2")', read.call_args.args[0])

    def test_the_days_merges_are_read_from_the_workflows_own_comments(self):
        done = queued_answer()
        # As `gh api --jq '{body, created_at}'` prints them, one object a line. The last one is older than the day:
        # GitHub lists it because it was edited since.
        lines = "\n".join(['{"body":"%s","created_at":"2026-11-23T09:00:00Z"}' % done.replace('"', '\\"'),
                           '{"body":"🤖 Merge: refused, x (asked by @duzzl)","created_at":"2026-11-23T10:00:00Z"}',
                           '{"body":"%s","created_at":"2026-11-20T09:00:00Z"}' % done.replace('"', '\\"')])
        with mock.patch.object(mc.ns, "gh", return_value=lines):  # the script's own import of nightly_set
            self.assertEqual(mc.merges_by(mc.Api("horizon-rust/horizon"), REVIEWER, NOW), 1)

    def test_a_ready_pull_request_reads_as_one(self):
        self.assertEqual(mc.read_pull(answer()), pull())
        missing = {"data": {"repository": {"pullRequest": None}}, "errors": [{"type": "NOT_FOUND"}]}
        self.assertIsNone(mc.read_pull(missing))

    def test_the_test_merge_must_have_the_current_base_and_head_as_its_only_parents(self):
        other = "b" * 40
        for parents in ([other, HEAD], [BASE, other], [HEAD, BASE], [BASE], [BASE, HEAD, other]):
            with self.subTest(parents=parents):
                merge = {"oid": MERGE, "parents": {"nodes": [{"oid": oid} for oid in parents]}}
                self.assertIsNone(mc.read_pull(answer(potentialMergeCommit=merge)).merge_oid)
        self.assertIsNone(mc.read_pull(answer(potentialMergeCommit=None)).merge_oid)
        # The PR's cached baseRefOid can still agree with the stale test merge.
        pr = mc.read_pull(answer(baseRefOid=BASE, baseRef={"target": {"oid": other}}))
        self.assertEqual(pr.base_oid, other)
        self.assertIsNone(pr.merge_oid)

    def test_an_answer_with_errors_is_no_answer(self):
        # A field the token may not read comes back null beside an error: read alone, it is a check that passed
        # nowhere or a change request that isn't there.
        for response in ({**answer(), "errors": [{"message": "Resource not accessible by integration"}]},
                         {"data": {"repository": None}, "errors": [{"message": "x"}]}, {"errors": [{"message": "x"}]}):
            with self.assertRaises(RuntimeError):
                mc.read_pull(response)

    def test_a_co_author_is_one_of_its_people(self):
        commit = {"oid": HEAD, "committer": {"user": None},
                  "authors": {"totalCount": 3, "nodes": [{"user": {"databaseId": AUTHOR.id}}, {"user": None},
                                                         {"user": {"databaseId": REVIEWER.id}}]}}
        pr = mc.read_pull(answer(commits={"totalCount": 1, "nodes": [{"commit": commit}]}))
        self.assertEqual((pr.people, pr.people_cut), ({AUTHOR.id, REVIEWER.id}, False))
        self.assertIn("a commit of yours", mc.pull_refusal(pr, REVIEWER))

    def test_commits_or_authors_it_could_not_read_all_of_are_cut(self):
        commit = answer()["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]
        many_authors = {**commit, "authors": {"totalCount": 11, "nodes": commit["authors"]["nodes"]}}
        for commits in ({"totalCount": 101, "nodes": [{"commit": commit}]},
                        {"totalCount": 1, "nodes": [{"commit": {**commit, "oid": "b" * 40}}]},  # not the head's list
                        {"totalCount": 1, "nodes": [{"commit": many_authors}]}, {"totalCount": 0, "nodes": []}):
            self.assertTrue(mc.read_pull(answer(commits=commits)).people_cut, commits)


class CommandTests(unittest.TestCase):
    """command(), as merge-command.yml runs it: the comment in the environment, one answer posted back."""

    def run_command(self, body, author=REVIEWER, token="app-token"):
        api = Fake()
        env = {"REPO": "horizon-rust/horizon", "ISSUE": "5", "AUTHOR": author.login, "AUTHOR_ID": str(author.id),
               "BODY": body, "COMMENT_URL": URL, "GH_TOKEN": "token", "MERGE_TOKEN": token,
               "MERGE_APP_BOT_ID": str(APP_BOT_ID)}
        self.api_token = None
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mc, "Api", lambda repo, token, bot_id: setattr(self, "api_token", token) or api), \
                mock.patch.object(mc, "read_grants", lambda text: LIST), \
                mock.patch.object(mc, "datetime", mock.Mock(now=lambda tz: NOW)), \
                mock.patch.object(mc.subprocess, "check_output", return_value=BASE + "\n"), \
                mock.patch.object(Path, "read_text", lambda self: ""):
            self.assertEqual(mc.command(), 0)
            # Only the queue call gets the app's token: no other `gh` this starts finds it in its environment.
            self.assertNotIn("MERGE_TOKEN", os.environ)
        self.assertEqual(self.api_token, token)
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

    def test_githubs_answer_in_another_shape_still_gets_a_reply(self):
        api = Fake()
        api.pull = lambda n: mc.read_pull({"data": {"repository": {"pullRequest": {"id": "PR_1"}}}})
        env = {"REPO": "horizon-rust/horizon", "ISSUE": "5", "AUTHOR": "duzzl", "AUTHOR_ID": "301", "BODY": "/merge",
               "MERGE_TOKEN": "app-token", "MERGE_APP_BOT_ID": str(APP_BOT_ID)}
        with mock.patch.dict(os.environ, env), mock.patch.object(mc, "Api", lambda repo, token, bot_id: api), \
                mock.patch.object(mc, "read_grants", lambda text: LIST), \
                mock.patch.object(mc, "datetime", mock.Mock(now=lambda tz: NOW)), \
                mock.patch.object(mc.subprocess, "check_output", return_value=BASE + "\n"), \
                mock.patch.object(Path, "read_text", lambda self: ""):
            self.assertEqual(mc.command(), 0)
        self.assertEqual(api.queued, [])
        self.assertEqual(api.comments, ["🤖 Merge: GitHub refused that, nothing was done (asked by @duzzl)."])

    def test_the_workflow_learns_whether_the_authors_line_is_live(self):
        def live(author):
            out = io.StringIO()
            env = {"AUTHOR": author.login, "AUTHOR_ID": str(author.id)}
            with mock.patch.dict(os.environ, env), mock.patch.object(mc, "read_grants", lambda text: LIST), \
                    mock.patch.object(mc, "datetime", mock.Mock(now=lambda tz: NOW)), \
                    mock.patch.object(Path, "read_text", lambda self: ""), redirect_stdout(out):
                mc.live()
            return out.getvalue()
        self.assertEqual((live(REVIEWER), live(OTHER)), ("live=true\n", "live=false\n"))


class WorkflowTests(unittest.TestCase):
    TEXT = (ROOT / ".github/workflows/merge-command.yml").read_text()

    def test_it_fetches_every_file_the_script_reads(self):
        # It checks out main sparsely: a file left out reads as missing and every `/merge` fails.
        checkout = self.TEXT[self.TEXT.index("sparse-checkout: |"):self.TEXT.index("sparse-checkout-cone-mode")]
        for path in (mc.GRANTS, ns.CODEOWNERS, "tools/ci/nightly_set.py", "tools/ci/merge_command.py"):
            self.assertIn(path, checkout)

    def test_it_passes_both_tokens(self):
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.TEXT)
        self.assertIn("MERGE_TOKEN: ${{ steps.app-account.outputs.matches == 'true' && steps.app.outputs.token",
                      self.TEXT)


if __name__ == "__main__":
    unittest.main()
