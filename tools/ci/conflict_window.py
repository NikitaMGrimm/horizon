#!/usr/bin/env python3
"""The conflict window: pull requests that conflict with main are closed a week after a notice (CONTRIBUTING.md
"Pull requests").

    conflict_window.py sweep               every open pull request once (conflict-window.yml, daily)
    conflict_window.py create [--apply]    creates the two labels the repository lacks

The sweep labels, comments and closes only when CONFLICT_WINDOW_ENFORCE is `true`; otherwise it prints what it would
do. A pull request against main that GitHub reports as conflicting gets the `conflicts` label and one notice; still
conflicting 7 days after the label went on, it is closed with a comment, and the label comes off so a reopened one
starts again. One that stops conflicting loses the label. Drafts, `keep-open` and the Horizon Team's own (write
access) are left alone, and an exempt one loses the label.

GitHub computes `mergeable` lazily and forgets it whenever main moves: UNKNOWN is never acted on. The list is read
again once after a wait, and a pull request is read again by itself right before it is closed.

The workflow runs on a schedule from main: this script reads the API only, never a pull request's code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

WINDOW_DAYS = 7  # T3 Code's conflict window
CONFLICT_LABEL = "conflicts"
KEEP_OPEN_LABEL = "keep-open"
BASE = "main"
# The job's token: its label events start the clock. A label someone added by hand starts nothing.
BOT = "github-actions"
TEAM_PERMISSIONS = {"admin", "maintain", "write"}
# A first enforced run meets the whole backlog. T3 Code's caps keep one run from flooding watchers or GitHub's limit
# on how fast a token may comment; what is left waits for the next day.
MAX_NOTICES_PER_RUN = 100
MAX_CLOSES_PER_RUN = 50
WRITE_PAUSE_S = 1
# T3 Code measured about two and a half minutes until every open pull request has an answer.
MERGEABLE_WAIT_S = 150
READ_ATTEMPTS = 4
READ_RETRY_S = 5
PAGE = 50
GUIDE = "https://github.com/{repo}/blob/main/CONTRIBUTING.md#pull-requests"

FIELDS = """
  number state isDraft mergeable baseRefName
  author { login }
  labels(first: 100) { nodes { name } }
  timelineItems(itemTypes: [LABELED_EVENT], last: 20) {
    nodes { ... on LabeledEvent { createdAt actor { login } label { name } } }
  }"""
LIST = """query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: %d, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}""" % (PAGE, FIELDS)
ONE = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) { %s } }
}""" % FIELDS


def notice(repo: str) -> str:
    return (f"This pull request conflicts with `{BASE}`. Please merge `{BASE}` into your branch within "
            f"{WINDOW_DAYS} days; after that it is closed automatically. Nothing is lost: once it is updated you "
            "can reopen it, or ask here and we will.\n\n"
            f"Want it kept open while you are away? Ask a maintainer for `{KEEP_OPEN_LABEL}`. "
            f"[Why]({GUIDE.format(repo=repo)})")


def closing(repo: str) -> str:
    return (f"Closing: this still conflicts with `{BASE}` {WINDOW_DAYS} days after the notice above. Thank you for "
            f"the work! Merge `{BASE}` into your branch and reopen it, or ask here and we will. "
            f"[Why]({GUIDE.format(repo=repo)})")


def labelled_at(pr: dict) -> datetime | None:
    """When this job's token last put the label on; None if it never did (a person added it by hand)."""
    events = [e for e in pr["timelineItems"]["nodes"] if (e.get("label") or {}).get("name") == CONFLICT_LABEL]
    if not events or (events[-1].get("actor") or {}).get("login") != BOT:
        return None
    return datetime.fromisoformat(events[-1]["createdAt"].replace("Z", "+00:00"))


def decide(pr: dict, team, now: datetime) -> str:
    """What the sweep does with one open pull request: skip, notice, wait, close, clear (take the label off), team
    (a team member's, left alone) or unclear (labelled without a clock, left alone). `team(login)` is asked only
    for a conflicting one."""
    labels = {l["name"] for l in pr["labels"]["nodes"]}
    labelled = CONFLICT_LABEL in labels
    if pr["isDraft"] or KEEP_OPEN_LABEL in labels or pr["baseRefName"] != BASE:
        return "clear" if labelled else "skip"
    if pr["mergeable"] == "UNKNOWN":
        return "skip"
    if pr["mergeable"] != "CONFLICTING":
        return "clear" if labelled else "skip"
    if team((pr.get("author") or {}).get("login") or ""):
        return "clear" if labelled else "team"
    if not labelled:
        return "notice"
    since = labelled_at(pr)
    if since is None:
        return "unclear"
    return "close" if (now - since).days >= WINDOW_DAYS else "wait"


# --- GitHub ------------------------------------------------------------------------------------------------------


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


class Api:
    def __init__(self, repo: str):
        self.repo = repo
        self.owner, self.name = repo.split("/")
        self._team: dict[str, bool] = {}

    def _read(self, *args: str) -> str:
        # GitHub answers a heavy list now and then with a 502. Only reads repeat: a repeated comment is a second one.
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return gh(*args)
            except RuntimeError as e:
                if attempt == READ_ATTEMPTS or not any(f"HTTP {c}" in str(e) for c in (500, 502, 503, 504)):
                    raise
                time.sleep(attempt * READ_RETRY_S)
        raise AssertionError

    def _graphql(self, query: str, **variables) -> dict:
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        for key, value in variables.items():
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        return json.loads(self._read(*args))["data"]["repository"]

    def open_pulls(self) -> list[dict]:
        out, cursor = [], None
        while True:
            page = self._graphql(LIST, **({"cursor": cursor} if cursor else {}))["pullRequests"]
            out += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return out
            cursor = page["pageInfo"]["endCursor"]

    def pull(self, n: int) -> dict:
        return self._graphql(ONE, number=n)["pullRequest"]

    def team(self, login: str) -> bool:
        """Write access to the repository: the Horizon Team. Any answer but a clear 404 stops the run, so a failed
        lookup never reads as "not on the team" and closes a maintainer's pull request."""
        if not login:
            return False
        if login not in self._team:
            try:
                perm = self._read("api", f"repos/{self.repo}/collaborators/{urllib.parse.quote(login)}/permission",
                                  "--jq", ".permission")
            except RuntimeError as e:
                if "HTTP 404" not in str(e):
                    raise
                perm = "none"
            self._team[login] = perm in TEAM_PERMISSIONS
        return self._team[login]

    def comment(self, n: int, text: str) -> None:
        gh("api", f"repos/{self.repo}/issues/{n}/comments", "-f", f"body={text}")

    def add(self, n: int, name: str) -> None:
        gh("api", f"repos/{self.repo}/issues/{n}/labels", "-f", f"labels[]={name}")

    def remove(self, n: int, name: str) -> None:
        try:
            gh("api", "-X", "DELETE", f"repos/{self.repo}/issues/{n}/labels/{urllib.parse.quote(name, safe='')}")
        except RuntimeError as e:
            if "HTTP 404" not in str(e):  # already gone
                raise

    def close(self, n: int) -> None:
        gh("api", "-X", "PATCH", f"repos/{self.repo}/pulls/{n}", "-f", "state=closed")

    def existing_labels(self) -> set[str]:
        return set(gh("label", "list", "-R", self.repo, "--limit", "1000", "--json", "name", "--jq", ".[].name")
                   .splitlines())

    def create_label(self, name: str, color: str, description: str) -> None:
        gh("label", "create", name, "-R", self.repo, "--color", color, "--description", description)


def sweep(api, enforce: bool, now: datetime | None = None, wait=time.sleep, log=print) -> dict[str, list[int]]:
    """Every open pull request once; returns the pull request numbers by action."""
    def candidates_unknown(prs):
        return any(p["mergeable"] == "UNKNOWN" and not p["isDraft"] and p["baseRefName"] == BASE for p in prs)

    prs = api.open_pulls()
    if candidates_unknown(prs):
        # Asking started GitHub's computation; ask once more. What is still unknown waits for the next run.
        wait(MERGEABLE_WAIT_S)
        prs = api.open_pulls()
    now = now or datetime.now(timezone.utc)
    done: dict[str, list[int]] = {}
    for pr in prs:
        n = pr["number"]
        action = decide(pr, api.team, now)
        if action == "skip" or (action == "notice" and len(done.get("notice", [])) >= MAX_NOTICES_PER_RUN):
            continue
        if action == "close":
            if len(done.get("close", [])) >= MAX_CLOSES_PER_RUN:
                continue
            # The list is minutes old, and a merge to main can resolve a conflict without touching the pull request.
            fresh = api.pull(n)
            if fresh["state"] != "OPEN" or (action := decide(fresh, api.team, now)) != "close":
                continue
        done.setdefault(action, []).append(n)
        if action == "unclear":
            log(f"#{n}: `{CONFLICT_LABEL}` was added by hand, so there is no notice to count from: left alone")
            continue
        log(f"#{n} {action}{'' if enforce else ' (dry run)'}")
        if not enforce or action in ("wait", "team"):
            continue
        if action == "notice":
            # The notice first: a label without it would start a clock nobody was told about.
            api.comment(n, notice(api.repo))
            api.add(n, CONFLICT_LABEL)
        elif action == "clear":
            api.remove(n, CONFLICT_LABEL)
        elif action == "close":
            api.comment(n, closing(api.repo))
            api.close(n)
            # Without this a reopened pull request would be closed again by the next run.
            api.remove(n, CONFLICT_LABEL)
        wait(WRITE_PAUSE_S)
    unknown = sum(1 for p in prs if p["mergeable"] == "UNKNOWN" and not p["isDraft"] and p["baseRefName"] == BASE)
    summary = ", ".join(f"{len(v)} {k}" for k, v in done.items()) or "nothing to do"
    log(f"{'' if enforce else 'Dry run. '}{len(prs)} open pull requests read, {unknown} still unknown: {summary}.")
    return done


LABELS = [
    (CONFLICT_LABEL, "e99695", f"Conflicts with main: closed {WINDOW_DAYS} days after the notice unless updated"),
    (KEEP_OPEN_LABEL, "0e8a16", "Not closed while it conflicts with main (conflict window)"),
]


def create(api, apply: bool) -> list[str]:
    have = {n.lower() for n in api.existing_labels()}
    todo = [label for label in LABELS if label[0] not in have]
    for label in todo if apply else []:
        api.create_label(*label)
    return [t[0] for t in todo]


if __name__ == "__main__":
    verb, rest = (sys.argv[1:2] or [""])[0], sys.argv[2:]
    api = Api(os.environ.get("REPO", "horizon-rust/horizon"))
    if verb == "sweep" and not rest:
        sweep(api, os.environ.get("CONFLICT_WINDOW_ENFORCE") == "true")
        sys.exit(0)
    if verb == "create" and rest in ([], ["--apply"]):
        for line in create(api, rest == ["--apply"]):
            print(line)
        sys.exit(0)
    sys.exit(__doc__)
