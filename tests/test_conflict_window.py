import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_ci_concurrency import yaml
from tests.test_ci_runners import jobs, runs_on
from tools.ci import conflict_window as cw

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def pr(n, mergeable="CONFLICTING", labels=(), draft=False, base="main", author="someone", labelled=None,
       by="github-actions", state="OPEN"):
    events = [] if labelled is None else [{"createdAt": (NOW - labelled).isoformat().replace("+00:00", "Z"),
                                           "actor": {"login": by}, "label": {"name": cw.CONFLICT_LABEL}}]
    return {"number": n, "state": state, "isDraft": draft, "mergeable": mergeable, "baseRefName": base,
            "author": {"login": author}, "labels": {"nodes": [{"name": l} for l in labels]},
            "timelineItems": {"nodes": events}}


class FakeApi:
    """The open pull requests as GitHub reports them; writes change them the way GitHub would."""
    repo = "o/r"

    def __init__(self, *pulls, team=(), later=None):
        self.pulls = {p["number"]: p for p in pulls}
        self.later = later  # what a second listing returns (GitHub has computed what was UNKNOWN)
        self.members = set(team)
        self.calls = []

    def open_pulls(self):
        if self.later is not None and any(c[0] == "list" for c in self.calls):
            self.pulls = {p["number"]: p for p in self.later}
        self.calls.append(("list",))
        return [p for p in self.pulls.values() if p["state"] == "OPEN"]

    def pull(self, n):
        return self.pulls[n]

    def team(self, login):
        return login in self.members

    def comment(self, n, text):
        self.calls.append(("comment", n, text))

    def add(self, n, name):
        self.calls.append(("add", n, name))
        p = self.pulls[n]
        p["labels"]["nodes"].append({"name": name})
        p["timelineItems"]["nodes"].append({"createdAt": NOW.isoformat().replace("+00:00", "Z"),
                                            "actor": {"login": "github-actions"}, "label": {"name": name}})

    def remove(self, n, name):
        self.calls.append(("remove", n, name))
        self.pulls[n]["labels"]["nodes"] = [l for l in self.pulls[n]["labels"]["nodes"] if l["name"] != name]

    def close(self, n):
        self.calls.append(("close", n))
        self.pulls[n]["state"] = "CLOSED"

    def writes(self):
        return [c[:2] if c[0] == "comment" else c for c in self.calls if c[0] != "list"]


def run(api, enforce=True, now=NOW):
    waits = []
    done = cw.sweep(api, enforce, now=now, wait=waits.append, log=lambda _: None)
    return done, waits


class SweepTests(unittest.TestCase):
    def test_a_conflict_gets_one_notice_then_the_label(self):
        api = FakeApi(pr(1), pr(2, "MERGEABLE"))
        self.assertEqual(run(api)[0], {"notice": [1]})
        self.assertEqual(api.writes(), [("comment", 1), ("add", 1, "conflicts")])
        self.assertIn("within 7 days", api.calls[1][2])

    def test_no_second_notice(self):
        api = FakeApi(pr(1))
        run(api)
        api.calls.clear()
        self.assertEqual(run(api, now=NOW + timedelta(days=6, hours=23))[0], {"wait": [1]})
        self.assertEqual(api.writes(), [])

    def test_closed_7_days_after_the_notice(self):
        api = FakeApi(pr(1, labels=["conflicts"], labelled=timedelta(days=7)))
        self.assertEqual(run(api)[0], {"close": [1]})
        self.assertEqual(api.writes(), [("comment", 1), ("close", 1), ("remove", 1, "conflicts")])
        self.assertIn("reopen", api.calls[1][2])

    def test_not_closed_if_resolved_since_the_list(self):
        api = FakeApi(pr(1, labels=["conflicts"], labelled=timedelta(days=8)))
        listed = dict(api.pulls[1])
        api.open_pulls = lambda: [listed]  # the list still says CONFLICTING
        api.pulls[1]["mergeable"] = "MERGEABLE"  # main moved; the pull request's own read says otherwise
        self.assertEqual(run(api)[0], {})
        self.assertNotIn("close", [c[0] for c in api.calls])

    def test_label_removed_when_fixed(self):
        api = FakeApi(pr(1, "MERGEABLE", labels=["conflicts"], labelled=timedelta(days=3)))
        self.assertEqual(run(api)[0], {"clear": [1]})
        self.assertEqual(api.writes(), [("remove", 1, "conflicts")])

    def test_drafts_keep_open_team_and_other_bases_skipped(self):
        api = FakeApi(pr(1, draft=True), pr(2, labels=["keep-open"]), pr(3, author="kira"), pr(4, base="feature"),
                      team={"kira"})
        self.assertEqual(run(api)[0], {"team": [3]})
        self.assertEqual(api.writes(), [])

    def test_an_exempt_one_loses_the_label(self):
        # Made a draft or given keep-open after the notice: ready again later, it gets a new notice, not a close.
        api = FakeApi(pr(1, draft=True, labels=["conflicts"], labelled=timedelta(days=9)),
                      pr(2, labels=["conflicts", "keep-open"], labelled=timedelta(days=9)))
        self.assertEqual(run(api)[0], {"clear": [1, 2]})

    def test_unknown_is_never_acted_on(self):
        api = FakeApi(pr(1, "UNKNOWN"), pr(2, "UNKNOWN", labels=["conflicts"], labelled=timedelta(days=30)))
        done, waits = run(api)
        self.assertEqual(done, {})
        self.assertEqual(api.writes(), [])
        self.assertEqual(waits, [cw.MERGEABLE_WAIT_S])  # asked once more after a wait

    def test_unknown_listed_again_after_the_wait(self):
        api = FakeApi(pr(1, "UNKNOWN"), later=[pr(1)])
        self.assertEqual(run(api)[0], {"notice": [1]})

    def test_a_label_added_by_hand_starts_no_clock(self):
        api = FakeApi(pr(1, labels=["conflicts"], labelled=timedelta(days=30), by="a-maintainer"))
        self.assertEqual(run(api)[0], {"unclear": [1]})
        self.assertEqual(api.writes(), [])

    def test_dry_run_changes_nothing(self):
        api = FakeApi(pr(1), pr(2, labels=["conflicts"], labelled=timedelta(days=8)),
                      pr(3, "MERGEABLE", labels=["conflicts"], labelled=timedelta(days=1)))
        self.assertEqual(run(api, enforce=False)[0], {"notice": [1], "close": [2], "clear": [3]})
        self.assertEqual(api.writes(), [])

    def test_caps_per_run(self):
        api = FakeApi(*[pr(n) for n in range(cw.MAX_NOTICES_PER_RUN + 5)])
        done = run(api)[0]
        self.assertEqual((len(done["notice"]), len(done["notice next run"])), (cw.MAX_NOTICES_PER_RUN, 5))
        self.assertEqual(len([c for c in api.calls if c[0] == "comment"]), cw.MAX_NOTICES_PER_RUN)
        api = FakeApi(*[pr(n, labels=["conflicts"], labelled=timedelta(days=8))
                        for n in range(cw.MAX_CLOSES_PER_RUN + 5)])
        self.assertEqual(len(run(api)[0]["close"]), cw.MAX_CLOSES_PER_RUN)

    def test_create_only_what_is_missing(self):
        class Labels:
            created = []

            def existing_labels(self):
                return {"Conflicts", "bug"}

            def create_label(self, name, color, description):
                self.created.append(name)
        api = Labels()
        self.assertEqual(cw.create(api, apply=False), ["keep-open"])
        self.assertEqual(api.created, [])
        cw.create(api, apply=True)
        self.assertEqual(api.created, ["keep-open"])


class WorkflowTests(unittest.TestCase):
    def test_workflow_is_off_by_default_and_runs_no_pull_request_code(self):
        text = (ROOT / ".github/workflows/conflict-window.yml").read_text()
        workflow = yaml.safe_load(text)
        on = workflow[True]  # YAML reads a bare `on` as true
        self.assertEqual(set(on), {"schedule", "workflow_dispatch"})
        self.assertIs(on["workflow_dispatch"]["inputs"]["dry_run"]["default"], True)
        job = workflow["jobs"]["sweep"]
        run_steps = [s for s in job["steps"] if "run" in s]
        self.assertEqual([s["run"] for s in run_steps], ["python3 tools/ci/conflict_window.py sweep"])
        self.assertEqual(run_steps[0]["env"]["CONFLICT_WINDOW_ENFORCE"],
                         "${{ vars.CONFLICT_WINDOW_ENFORCE == 'true' && "
                         "!(github.event_name == 'workflow_dispatch' && inputs.dry_run) }}")
        checkout = job["steps"][0]["with"]
        self.assertEqual(checkout["ref"], "main")
        self.assertIs(checkout["persist-credentials"], False)
        self.assertEqual(checkout["sparse-checkout"], "tools/ci/conflict_window.py\n")
        # Its token closes pull requests: GitHub's runners only.
        self.assertEqual(runs_on(dict(jobs(text))["sweep"]), "ubuntu-24.04")
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "write"})


if __name__ == "__main__":
    unittest.main()
