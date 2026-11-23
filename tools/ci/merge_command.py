#!/usr/bin/env python3
"""Merge reviewers add other people's pull requests to the merge queue by comment (docs/release-flow.md "Merge
reviewers").

    merge_command.py command     one `/merge` comment (merge-command.yml): checks who asked and what the pull request
                                 changes, adds it to the merge queue, answers in the thread
    merge_command.py live        prints `live=true` when the comment's author has a live line, for the workflow:
                                 only then does it make the app's token
    merge_command.py --verify-merge-group   rechecks delegation against the queue's actual squash commits

A merge reviewer holds no repository role. A line of .github/merge-reviewers.txt names the account (by numeric ID,
never by login), the day the line ends and the folders it covers. On the first line of a comment on a pull request:

    /merge

It is refused with a one-line answer unless all of this holds: the line is live (not past its day, and that day at
most MAX_DAYS away); the pull request is open, to main, not a draft, not the reviewer's own and holds no commit of
theirs, co-written ones included; every file in its contributed and applied diffs is in the reviewer's folders
and none is one CODEOWNERS guards; the reviewer's own approving review is on the head commit; nobody's change
request is open; `verify` and every other check of the head passed; it carries
neither `needs-proof` nor `release-now`; the reviewer merged fewer than MAX_PER_DAY in 24 hours. The queue then
runs `verify` on it like on any other. The script uses the comment and GitHub's metadata and file lists.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_set as ns  # noqa: E402  (accounts by ID, CODEOWNERS' patterns, the gh call)

Refused = ns.Refused
GRANTS = ".github/merge-reviewers.txt"
# A line never reaches further ahead than this: staying a merge reviewer takes a new date every month.
MAX_DAYS = 31
MAX_PER_DAY = 60  # a soft cap on what one account can send out: two runs at once can both pass it
PREFIX = "🤖 Merge: "
# The Team's probation rule (GOVERNANCE.md): proof-waiting pull requests, and ones that release at once, are the Team's.
HELD = ("needs-proof", "release-now")
MAX_COMMITS = 100  # one page of a pull request's commits
MAX_AUTHORS = 10  # of one commit
MAX_FILES = 300  # GitHub compares no more
REQUIRED = "verify"  # the check main's protection requires (tools/ci/protect_pipeline.sh)
ACTIONS_APP = 15368  # GitHub Actions: only its `verify` counts, as in main's protection
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OID = re.compile(r"[0-9a-f]{40}")
ACTIONS_BOT = 41898282  # github-actions[bot], which writes the command's answers
WRITE_TIMEOUT = 10
WRITE_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)
# The enqueue and comment attempts have a 43 second timeout and backoff budget. Allow time for the read to catch up.
RECEIPT_READ_DELAYS = (0, 2, 4, 8, 16, 30)
MARK = re.compile(r"<!-- merge-by:([1-9][0-9]{0,11}) head:([0-9a-f]{40}) queue:([\w+/=-]+) -->\Z", re.ASCII)
QUERY = """query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $n) {
    id state isDraft baseRefName headRefOid isInMergeQueue mergeable changedFiles
    baseRef { target { oid } }
    potentialMergeCommit { oid parents(first: 3) { nodes { oid } } }
    author { ... on User { databaseId } }
    labels(first: 100) { nodes { name } }
    head: commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    commits(first: 100) { totalCount nodes { commit {
      oid
      committer { user { databaseId } }
      authors(first: 10) { totalCount nodes { user { databaseId } } }
    } } }
    latestOpinionatedReviews(first: 100) {
      pageInfo { hasNextPage }
      nodes { state commit { oid } author { login ... on User { databaseId } } }
    }
  }
} }"""
ENQUEUE = """mutation($id: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) {
    mergeQueueEntry { id enqueuer { ... on Bot { databaseId } } }
  }
}"""
DEQUEUE = """mutation($id: ID!) {
  dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } }
}"""
QUEUE = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  ref(qualifiedName: "refs/heads/main") { target { oid } }
  mergeQueue(branch: "main") { entries(first: 100) { nodes {
    id enqueuedAt headCommit { oid } baseCommit { oid } enqueuer { ... on Bot { databaseId } }
    pullRequest { number headRefOid }
  } } }
} }"""
COMMENT_PAGE = "{ pageInfo { hasPreviousPage startCursor } nodes { createdAt body author { ... on Bot { databaseId } } } }"


@dataclass(frozen=True)
class Grant:
    """One line of the list: this account merges pull requests that stay in these folders, until that day."""
    id: int
    login: str
    until: date
    paths: tuple[str, ...]


@dataclass
class Grants:
    lines: list[Grant]
    paused: bool
    skipped: list[str] = field(default_factory=list)  # lines not understood, ignored (fail closed)


def read_grants(text: str) -> Grants:
    """`<id> <login> until <YYYY-MM-DD> </folder/>...` per line, `#` comments, and a `paused` line that stops
    everyone. Any other line grants nothing and is reported on stderr, rather than being half-read."""
    lines, paused, skipped = [], False, []
    for n, raw in enumerate(text.splitlines(), 1):
        words = raw.split("#", 1)[0].split()
        if not words:
            continue
        if len(words) == 1 and words[0].lower() == "paused":
            paused = True
        elif (grant := _grant(words)):
            lines.append(grant)
        else:
            skipped.append(f"line {n}: {' '.join(words)[:60]!r}")
    if skipped:
        print(f"{GRANTS}: ignored (not `<id> <login> until <YYYY-MM-DD> </folder/>...` or `paused`): "
              + "; ".join(skipped), file=sys.stderr)
    return Grants(lines, paused, skipped)


def _grant(words: list[str]) -> Grant | None:
    if len(words) < 5 or not ns.USER_ID.match(words[0]) or not ns.LOGIN.match(words[1]):
        return None
    if words[2].lower() != "until" or not DAY.match(words[3]):
        return None
    try:
        until = date.fromisoformat(words[3])
    except ValueError:
        return None
    # From the repository's root only (a bare name would cover that name anywhere), and no `**`: see covers().
    if not all(path.startswith("/") and "**" not in path for path in words[4:]):
        return None
    return Grant(int(words[0]), words[1], until, tuple(words[4:]))


def covers(path: str, patterns: list[str]) -> bool:
    """Whether a line's folders include `path`. Stricter than CODEOWNERS' matching (ns.guarded), where covering too
    much is the safe side; here it is the unsafe one. `/a/b/` is that folder and all below it; `/a/b.rs` is that
    one file; `*` and `?` stay inside one name, and a pattern for a file never reaches below a folder of that name."""
    p = "/" + path.lstrip("/")
    for pattern in patterns:
        regex = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in pattern)
        if re.match(regex if pattern.endswith("/") else regex + r"\Z", p, re.DOTALL):
            return True
    return False


def scope(actor: ns.Actor, grants: Grants, today: date) -> list[str]:
    """The folders `actor` merges in today. Raises Refused when no line of theirs is live."""
    mine = [g for g in grants.lines if g.id == actor.id]
    if not mine:
        raise Refused(f"Only the merge reviewers listed in `{GRANTS}` can merge by comment.")
    if grants.paused:
        raise Refused(f"`/merge` is paused: the council stopped it in `{GRANTS}`.")
    live = [g for g in mine if today <= g.until <= today + timedelta(days=MAX_DAYS)]
    if live:
        return sorted({path for g in live for path in g.paths})
    last = max(g.until for g in mine)
    if last < today:
        raise Refused(f"Your line in `{GRANTS}` ended on {last}: the council extends it with a new date.")
    raise Refused(f"Your line in `{GRANTS}` ends more than {MAX_DAYS} days from now, so it grants nothing yet.")


def parse(body: str) -> bool:
    """Whether a comment's first line is `/merge`. Raises Refused when it is, with words after it."""
    line = body.partition("\n")[0].rstrip()
    # Agents start every comment with "🤖 Agent:" (AGENTS.md), so their `/merge` follows it, as in claim_command.
    line = re.sub(r"^🤖 Agent: ", "", line, flags=re.IGNORECASE)
    m = re.match(r"^/(\w+)(?:\s+(.*))?$", line)
    if not m or m.group(1).lower() != "merge":
        return False
    if (m.group(2) or "").strip():
        raise Refused("`/merge` takes no arguments: comment it on the pull request to merge.")
    return True


def shown(path: str) -> str:
    """A changed file's name, fit for the answer: a name is the author's text, so no markdown, mention or line break."""
    return "`" + (re.sub(r"[^\w./+-]", "?", path)[:120] or "?") + "`"


@dataclass(frozen=True)
class Review:
    """A person's latest approval or change request on a pull request, and the commit it looked at."""
    author_id: int | None
    login: str
    state: str  # APPROVED, CHANGES_REQUESTED, DISMISSED
    commit: str | None


@dataclass
class Pull:
    node: str  # GraphQL ID, for the queue
    state: str  # OPEN, CLOSED, MERGED
    draft: bool
    base: str
    base_oid: str
    head: str
    merge_oid: str | None  # GitHub's test merge commit, if its parents are the current base and head
    author_id: int | None
    queued: bool
    conflicts: bool
    checks: str | None  # the head's checks together: SUCCESS, FAILURE, PENDING, ERROR, EXPECTED; None when none ran
    labels: list[str]
    reviews: list[Review]
    reviews_cut: bool  # more reviewers than one page holds
    changed_files: int
    people: set[int]  # who wrote, co-wrote or committed its commits
    people_cut: bool  # more commits, or more authors of one, than were read; or the commits read don't end at head


@dataclass(frozen=True)
class QueueEntry:
    id: str
    number: int
    head: str  # reviewed PR head
    base: str | None  # parent of the squash commit
    squash: str
    bot_id: int | None
    enqueued_at: str


def pull_refusal(pr: Pull, actor: ns.Actor) -> str | None:
    """Why `actor` can't send this pull request to the queue (None when they can), before its files are read."""
    if pr.state != "OPEN":
        return f"This pull request is {pr.state.lower()}."
    if pr.base != "main":
        return f"This pull request targets `{ns.safe(pr.base)}`, not main."
    if pr.draft:
        return "This pull request is a draft."
    if pr.queued:
        return "This pull request is already in the merge queue."
    if pr.author_id == actor.id:
        return "This is your own pull request: someone else reviews and merges it."
    if pr.people_cut:
        return "It has too many commits or authors to read them all: the Horizon Team merges it."
    if actor.id in pr.people:
        return "It holds a commit of yours: someone else reviews and merges it."
    if (why := hold_refusal(pr.labels)):
        return why
    if pr.reviews_cut:
        return "This pull request has more reviewers than this can read: the Horizon Team merges it."
    asking = [r.login for r in pr.reviews if r.state == "CHANGES_REQUESTED"]
    if asking:
        return f"{ns.safe(asking[0])}'s change request is still open."
    mine = next((r for r in pr.reviews if r.author_id == actor.id), None)
    if mine is None or mine.state != "APPROVED":
        return "Approve it first: `/merge` needs your own approving review."
    if mine.commit != pr.head:
        return f"Your approval is of an earlier commit: review `{pr.head[:7]}` and approve again."
    if pr.conflicts:
        return "This pull request conflicts with main."
    if pr.checks != "SUCCESS":
        return f"The checks of `{pr.head[:7]}` are {(pr.checks or 'missing').lower()}: `/merge` again once all passed."
    return None


def hold_refusal(labels: list[str]) -> str | None:
    for name in labels:
        if name.lower() in HELD:
            return f"This pull request is labelled `{ns.safe(name)}`: the Horizon Team merges those."
    return None


def required_refusal(conclusions: list[str | None], head: str) -> str | None:
    """Why the required check holds it. The checks together can pass without it: a fork's runs wait for a Team
    member's approval while the bots' own checks pass."""
    if conclusions and all(c == "success" for c in conclusions):
        return None
    state = "hasn't run" if not conclusions else "hasn't passed"
    return f"`{REQUIRED}` {state} on `{head[:7]}`: `/merge` again once every check passed."


def files_refusal(files: list[tuple[str, str | None]], count: int, paths: list[str],
                  guarded: list[str]) -> str | None:
    """Why these changed files can't be merged by someone covering `paths`. Each file comes with the name it was
    renamed from, which counts too. A list GitHub cut short hides the rest. `count` is the pull request's file count
    for the contributed diff, or zero for the applied diff, whose file count can differ."""
    if not files or len(files) < count or len(files) >= MAX_FILES:
        return "GitHub didn't list every file this pull request changes: the Horizon Team merges it."
    return paths_refusal(files, paths, guarded)


def paths_refusal(files: list[tuple[str, str | None]], paths: list[str], guarded: list[str]) -> str | None:
    names = [name for pair in files for name in pair if name is not None]
    council = [f for f in names if ns.guarded(f, guarded)]
    if council:
        more = f" and {len(council) - 1} more" if len(council) > 1 else ""
        return f"It changes {shown(council[0])}{more}, which only the council merges (CODEOWNERS)."
    outside = [f for f in names if not covers(f, paths)]
    if outside:
        more = f" and {len(outside) - 1} more" if len(outside) > 1 else ""
        return f"It changes {shown(outside[0])}{more}, outside the folders your line in `{GRANTS}` covers."
    return None


# --- GitHub ------------------------------------------------------------------------------------------------------


def read_pull(response: dict) -> Pull | None:
    """The pull request of QUERY's answer; None when there is none. Raises RuntimeError on an answer with errors:
    a field GitHub left out would otherwise read as a check that never ran, or a review that isn't there."""
    p = ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
    if p is None:
        if (response.get("data") or {}).get("repository") is None:
            raise RuntimeError(f"GraphQL: {json.dumps(response.get('errors'))[:300]}")
        return None
    if response.get("errors"):
        raise RuntimeError(f"GraphQL: {json.dumps(response['errors'])[:300]}")
    commits = [node["commit"] for node in p["commits"]["nodes"]]
    people = {who["user"]["databaseId"] for c in commits
              for who in [c["committer"] or {}, *c["authors"]["nodes"]] if who.get("user")}
    cut = (p["commits"]["totalCount"] > len(commits) or not commits or commits[-1]["oid"] != p["headRefOid"]
           or any(c["authors"]["totalCount"] > len(c["authors"]["nodes"]) for c in commits))
    head = (p["head"]["nodes"] or [{}])[0].get("commit") or {}
    reviews = p["latestOpinionatedReviews"]
    # GitHub can return baseRefOid and a test merge commit from an older tip of the base branch.
    base_oid = ((p["baseRef"] or {}).get("target") or {}).get("oid") or ""
    merge = p["potentialMergeCommit"]
    merge_oid = None
    if merge and [parent["oid"] for parent in merge["parents"]["nodes"]] == [base_oid, p["headRefOid"]]:
        merge_oid = merge["oid"]
    return Pull(
        node=p["id"], state=p["state"], draft=p["isDraft"], base=p["baseRefName"], base_oid=base_oid,
        head=p["headRefOid"], merge_oid=merge_oid, author_id=(p["author"] or {}).get("databaseId"),
        queued=p["isInMergeQueue"],
        conflicts=p["mergeable"] == "CONFLICTING", checks=(head.get("statusCheckRollup") or {}).get("state"),
        labels=[node["name"] for node in p["labels"]["nodes"]],
        reviews=[Review((node["author"] or {}).get("databaseId"), (node["author"] or {}).get("login", "?"),
                        node["state"], (node["commit"] or {}).get("oid")) for node in reviews["nodes"]],
        reviews_cut=reviews["pageInfo"]["hasNextPage"], changed_files=p["changedFiles"], people=people,
        people_cut=cut)


class Api:
    """The few GitHub calls this needs; tests replace it. `merge_token` is the app's, used for the queue alone:
    a pull request the workflow's own token queued gets no `merge_group` run, so `verify` never starts."""

    def __init__(self, repo: str, merge_token: str = "", app_bot_id: int | None = None):
        self.repo, self.merge_token, self.app_bot_id = repo, merge_token, app_bot_id

    def pull(self, n: int) -> Pull | None:
        owner, name = self.repo.split("/", 1)
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f",
                            f"name={name}", "-F", f"n={n}"], capture_output=True, text=True)
        try:
            response = json.loads(r.stdout)
        except ValueError:
            raise RuntimeError(f"gh api graphql: {r.stderr.strip()[:300]}") from None
        return read_pull(response)

    def required_check(self, head: str) -> list[str | None]:
        """How GitHub Actions' newest `verify` runs on this commit ended (None: still running)."""
        return json.loads(ns.gh("api", f"repos/{self.repo}/commits/{head}/check-runs?check_name={REQUIRED}"
                                f"&app_id={ACTIONS_APP}&filter=latest&per_page=100", "--jq",
                                "[.check_runs[].conclusion]"))

    def main_oid(self) -> str:
        return ns.gh("api", f"repos/{self.repo}/git/ref/heads/main", "--jq", ".object.sha")

    def files(self, base: str, head: str) -> list[tuple[str, str | None]]:
        """Files changed between the commits' common ancestor and `head`, with both names of each rename.
        For a test merge commit, `base` is its first parent, so this is the diff applied to the base branch."""
        raw = ns.gh("api", f"repos/{self.repo}/compare/{base}...{head}?per_page=1", "--jq",
                    ".files[] | [.filename, .previous_filename]")
        return [tuple(json.loads(line)) for line in raw.splitlines() if line.strip()]

    def queue_mutation(self, query: str, node: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"id={node}", *args],
                              capture_output=True, text=True, timeout=WRITE_TIMEOUT,
                              env={**os.environ, "GH_TOKEN": self.merge_token})

    def enqueue(self, node: str, head: str) -> str:
        """Adds it at this head, returning the queue entry ID GitHub assigns to this enqueue."""
        try:
            r = self.queue_mutation(ENQUEUE, node, "-f", f"head={head}")
        except subprocess.TimeoutExpired:
            raise Refused("GitHub didn't confirm the enqueue. The queue check requires a receipt.") from None
        if r.returncode != 0:
            print(r.stderr.strip()[:600], file=sys.stderr)
            why = re.sub(r"[^\w#:.,' -]", "", r.stderr.strip().removeprefix("gh: "))[:200].strip()
            raise Refused(f"GitHub didn't take it into the merge queue ({why or 'no reason given'}).")
        try:
            entry = json.loads(r.stdout)["data"]["enqueuePullRequest"]["mergeQueueEntry"]
            if self.app_bot_id and entry["enqueuer"].get("databaseId") == self.app_bot_id:
                if re.fullmatch(r"[\w+/=-]+", entry["id"], re.ASCII):
                    return entry["id"]
        except (ValueError, KeyError, TypeError):
            pass
        raise Refused("GitHub didn't confirm a queue entry from this app. The queue check requires a receipt.")

    def dequeue(self, number: int, node: str, entry_id: str) -> None:
        # GitHub's mutation targets the PR, so check for our entry immediately before using it.
        data = self.repository(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                               f"{{ pullRequest(number: {number}) "
                               "{ mergeQueueEntry { id enqueuer { ... on Bot { databaseId } } } } } }")
        current = data["pullRequest"]["mergeQueueEntry"]
        if not current or current["id"] != entry_id:
            return
        if not self.app_bot_id or current["enqueuer"].get("databaseId") != self.app_bot_id:
            raise RuntimeError("GitHub didn't confirm the original entry's enqueuer.")
        result = self.queue_mutation(DEQUEUE, node)
        if result.returncode:
            raise RuntimeError(f"GitHub refused to remove the unreceipted queue entry: {result.stderr.strip()[:300]}")
        removed = json.loads(result.stdout)["data"]["dequeuePullRequest"]["mergeQueueEntry"]
        if not removed or removed["id"] != entry_id:
            raise RuntimeError("GitHub did not confirm removal of the original queue entry.")

    def repository(self, query: str) -> dict:
        owner, name = self.repo.split("/", 1)
        data = json.loads(ns.gh("api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"))
        if data.get("errors"):
            raise RuntimeError("GitHub did not return all merge queue data.")
        return data["data"]["repository"]

    def queue_entries(self) -> tuple[str, list[QueueEntry]]:
        data = self.repository(QUEUE)
        entries = [QueueEntry(e["id"], e["pullRequest"]["number"], e["pullRequest"]["headRefOid"],
                              (e["baseCommit"] or {}).get("oid"), e["headCommit"]["oid"],
                              e["enqueuer"].get("databaseId"), e["enqueuedAt"])
                   for e in data["mergeQueue"]["entries"]["nodes"] if e["headCommit"]]
        return data["ref"]["target"]["oid"], entries

    def delegation_answers(self, entries: list[QueueEntry]) -> dict[int, list[str]]:
        """Workflow replies created at or after each enqueue, paging back across newer comments."""
        cutoffs = {e.number: datetime.fromisoformat(e.enqueued_at) for e in entries}
        answers = {n: [] for n in cutoffs}
        cursors = dict.fromkeys(cutoffs)
        while cursors:
            selections = []
            for n, cursor in cursors.items():
                before = f", before: {json.dumps(cursor)}" if cursor is not None else ""
                selections.append(f"pr{n}: pullRequest(number: {n}) {{ comments(last: 30{before}) {COMMENT_PAGE} }}")
            data = self.repository(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                                   f"{{ {' '.join(selections)} }} }}")
            for n, cursor in tuple(cursors.items()):
                page = data[f"pr{n}"]["comments"]
                recent = [c for c in page["nodes"] if datetime.fromisoformat(c["createdAt"]) >= cutoffs[n]]
                answers[n].extend(c["body"] for c in recent if (c["author"] or {}).get("databaseId") == ACTIONS_BOT)
                info = page["pageInfo"]
                if not info["hasPreviousPage"] or len(recent) < len(page["nodes"]):
                    del cursors[n]
                else:
                    if not info["startCursor"] or info["startCursor"] == cursor:
                        raise RuntimeError(f"GitHub's comment cursor did not advance for #{n}.")
                    cursors[n] = info["startCursor"]
        return answers

    def comment(self, n: int, text: str) -> None:
        subprocess.run(["gh", "api", f"repos/{self.repo}/issues/{n}/comments", "-f", f"body={text}"],
                       capture_output=True, check=True, timeout=WRITE_TIMEOUT)

    def queue_labels(self, entries: list[QueueEntry]) -> dict[int, list[str]]:
        selections = " ".join(f"pr{e.number}: pullRequest(number: {e.number}) "
                              "{ labels(first: 100) { pageInfo { hasNextPage } nodes { name } } }"
                              for e in entries)
        data = self.repository(f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                               f"{{ {selections} }} }}")
        labels = {}
        for entry in entries:
            page = data[f"pr{entry.number}"]["labels"]
            if page["pageInfo"]["hasNextPage"]:
                raise Refused(f"#{entry.number} has more labels than the queue check can read.")
            labels[entry.number] = [label["name"] for label in page["nodes"]]
        return labels

    def recent_answers(self, since: str) -> list[str]:
        """This workflow's own answers in the whole repository, written since a time (ISO 8601, UTC)."""
        raw = ns.gh("api", "--paginate", f"repos/{self.repo}/issues/comments?since={since}&per_page=100", "--jq",
                    '.[] | select(.user.login == "github-actions[bot]" and (.body | startswith("' + PREFIX
                    + '"))) | {body, created_at}')
        answers = [json.loads(line) for line in raw.splitlines() if line.strip()]
        # GitHub's `since` goes by the last edit, so an older comment someone edited comes back too.
        return [a["body"] for a in answers if a["created_at"] >= since]


def answer_mark(body: str) -> re.Match | None:
    """Read a successful command's marker. The entry ID distinguishes separate enqueues at the same head."""
    return MARK.search(body.rstrip()) if body.startswith(f"{PREFIX}added to the merge queue at `") else None


def merges_by(api, actor: ns.Actor, now: datetime) -> int:
    """Pull requests this workflow queued for `actor` in the last 24 h, counted from its own answers."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A POST can land even if its response times out. Each queue entry counts once if the retry also lands.
    return len({m[3] for body in api.recent_answers(since) if (m := answer_mark(body)) and int(m[1]) == actor.id})


def retry_write(write, *args) -> None:
    for attempt in range(3):
        try:
            write(*args)
            return
        except WRITE_ERRORS as e:
            print(str(e)[:350], file=sys.stderr)
            if attempt == 2:
                raise
            sleep(attempt + 1)


def ready(api, actor: ns.Actor, number: int) -> Pull:
    """The pull request as it is now, if `actor` may queue it. Raises Refused."""
    pr = api.pull(number)
    if pr is None:
        raise Refused("This isn't a pull request.")
    if (why := pull_refusal(pr, actor)):
        raise Refused(why)
    return pr


def run(api, actor: ns.Actor, number: int, url: str, grants: Grants, guarded: list[str], policy_oid: str,
        switched_on: bool = True) -> str:
    """Queues the pull request and publishes its bound receipt. Raises Refused for a refusal."""
    now = datetime.now(timezone.utc)
    paths = scope(actor, grants, now.date())
    if not switched_on:
        raise Refused("`/merge` isn't switched on yet: the workflow has no app token for the merge queue.")
    pr = ready(api, actor, number)
    if pr.base_oid != policy_oid:
        raise Refused("Main moved since the workflow checked out the merge policy: try `/merge` again.")
    if not pr.merge_oid:
        raise Refused("GitHub doesn't have a test merge for the current main and head commits: try `/merge` again.")
    # A rename on main can make the merge touch a path absent from the contributed diff.
    diffs = []
    for target, count in ((pr.head, pr.changed_files), (pr.merge_oid, 0)):
        files = api.files(pr.base_oid, target)
        if (why := files_refusal(files, count, paths, guarded)):
            raise Refused(why)
        diffs.append((files, count))
    if merges_by(api, actor, now) >= MAX_PER_DAY:
        raise Refused(f"You merged {MAX_PER_DAY} pull requests in the last 24 hours. Try again later.")
    # The quota scan can take many API calls. Re-read eligibility and require the commits we already checked.
    current = ready(api, actor, number)
    if current.head != pr.head:
        raise Refused("It got a new commit while this was checking it: review it and `/merge` again.")
    if current.base_oid != pr.base_oid:
        raise Refused("Main moved while this was checking it: try `/merge` again.")
    if current.merge_oid != pr.merge_oid:
        raise Refused("GitHub's test merge changed while this was checking it: try `/merge` again.")
    if (why := required_refusal(api.required_check(current.head), current.head)):
        raise Refused(why)
    # The enqueue mutation pins only the head. Read main again in case the policy changed during the check lookup.
    if api.main_oid() != policy_oid:
        raise Refused("Main moved while this was checking it: try `/merge` again.")
    current_paths = scope(actor, grants, datetime.now(timezone.utc).date())
    if current_paths != paths:
        for files, count in diffs:
            if (why := files_refusal(files, count, current_paths, guarded)):
                raise Refused(why)
    entry = api.enqueue(pr.node, pr.head)
    receipt = (f"{PREFIX}added to the merge queue at `{pr.head[:7]}`, on behalf of @{actor.login} "
               f"([their comment]({url})). <!-- merge-by:{actor.id} head:{pr.head} queue:{entry} -->")
    try:
        retry_write(api.comment, number, receipt)
    except WRITE_ERRORS:
        try:
            retry_write(api.dequeue, number, pr.node, entry)
        except (*WRITE_ERRORS, ValueError, KeyError, TypeError) as e:
            print(repr(e), file=sys.stderr)
            raise Refused("GitHub couldn't publish the receipt or confirm queue removal. `verify` requires the receipt.") from None
        raise Refused("GitHub couldn't publish the receipt. The original entry is no longer queued.") from None
    return receipt


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def squash_files(base: str, head: str) -> list[tuple[str, str | None]]:
    raw = git("diff", "--no-ext-diff", "--no-textconv", "--name-status", "-M", "-z", base, head, "--")
    fields = iter(raw.split(b"\0")[:-1])
    files = []
    for status in fields:
        name = os.fsdecode(next(fields))
        files.append((os.fsdecode(next(fields)), name) if status.startswith(b"R") else (name, None))
    return files


def receipt_reviewers(api, entries: list[QueueEntry]) -> dict[int, int]:
    """The exact receipt can appear after the merge-group event starts its checks."""
    for delay in RECEIPT_READ_DELAYS:
        if delay:
            sleep(delay)
        answers = api.delegation_answers(entries)
        reviewers = {}
        for entry in entries:
            matches = {int(m[1]) for body in answers[entry.number] if (m := answer_mark(body))
                       and (m[2], m[3]) == (entry.head, entry.id)}
            if len(matches) > 1:
                raise Refused(f"#{entry.number} has conflicting `/merge` receipts for this queue entry and head.")
            if matches:
                reviewers[entry.number] = matches.pop()
        if len(reviewers) == len(entries):
            return reviewers
    missing = next(e for e in entries if e.number not in reviewers)
    raise Refused(f"#{missing.number} has no `/merge` receipt for this queue entry and head.")


def verify_group(api, base: str, head: str, app_bot_id: int | None) -> None:
    """Verify each unmerged squash commit using GitHub's queue entry for that exact commit and parent."""
    if app_bot_id is None:
        return
    if not OID.fullmatch(base) or not OID.fullmatch(head):
        raise Refused("The merge group must name its base and head commit IDs.")
    chain = git("rev-list", "--first-parent", "--reverse", "--parents", f"{base}..{head}").decode().splitlines()
    if not chain:
        raise Refused("The merge group has no squash commits.")
    main, entries = api.queue_entries()
    by_squash = {entry.squash: entry for entry in entries}
    delegated = []
    parent = base
    for line in chain:
        commit, *parents = line.split()
        if parents != [parent]:
            raise Refused("The merge group is not a contiguous chain of GitHub squash commits.")
        entry = by_squash.get(commit)
        if entry is None:
            # An earlier entry may already have landed while this group's other checks were running.
            if not OID.fullmatch(main) or subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, main], capture_output=True,
            ).returncode:
                raise Refused(f"GitHub has no queue entry for squash commit {commit[:12]}.")
        elif entry.base != parent:
            raise Refused(f"GitHub's queue entry for #{entry.number} has a different squash parent.")
        elif entry.bot_id == app_bot_id:
            delegated.append(entry)
        parent = commit
    if not delegated:
        return
    reviewers = receipt_reviewers(api, delegated)
    today = datetime.now(timezone.utc).date()
    for entry in delegated:
        actor = ns.Actor("", reviewers[entry.number])
        files = squash_files(entry.base, entry.squash)
        # The group base supplies the policy. An earlier entry can also revoke or narrow the grant.
        for commit in dict.fromkeys((base, entry.base)):
            grants = read_grants(git("show", f"{commit}:{GRANTS}").decode())
            guarded = ns.guarded_patterns(git("show", f"{commit}:{ns.CODEOWNERS}").decode())
            try:
                paths = scope(actor, grants, today)
            except Refused as e:
                raise Refused(f"#{entry.number}: {e}") from None
            if (why := paths_refusal(files, paths, guarded)):
                raise Refused(f"#{entry.number}: {why}")
    # Receipt visibility can take time. Check hold labels after those reads and the path checks.
    labels = api.queue_labels(delegated)
    for entry in delegated:
        if (why := hold_refusal(labels[entry.number])):
            raise Refused(f"#{entry.number}: {why}")


def verify_merge_group() -> int:
    try:
        bot_id = os.environ.get("MERGE_APP_BOT_ID", "")
        if bot_id and not ns.USER_ID.fullmatch(bot_id):
            raise Refused("MERGE_APP_BOT_ID must be a numeric GitHub account ID.")
        verify_group(Api(os.environ["REPO"]), os.environ["BASE_SHA"], os.environ["HEAD_SHA"],
                     int(bot_id) if bot_id else None)
    except (Refused, RuntimeError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as e:
        print(f"Merge queue delegation check failed: {e}", file=sys.stderr)
        return 1
    print("Merge queue delegation check passed.")
    return 0


def command() -> int:
    body, login = os.environ["BODY"], os.environ["AUTHOR"]
    number, url, repo = int(os.environ["ISSUE"]), os.environ.get("COMMENT_URL", ""), os.environ["REPO"]
    # Out of the environment: only the queue call gets the app's token, no other `gh` this starts.
    token = os.environ.pop("MERGE_TOKEN", "")
    raw_bot_id = os.environ.get("MERGE_APP_BOT_ID", "")
    bot_id = int(raw_bot_id) if ns.USER_ID.fullmatch(raw_bot_id) else None
    api: Api = Api(repo, token, bot_id)
    try:
        if not parse(body):
            return 0
        actor = ns.actor_from_env()
        grants = read_grants(Path(GRANTS).read_text())
        guarded = ns.guarded_patterns(Path(ns.CODEOWNERS).read_text())
        policy_oid = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        run(api, actor, number, url, grants, guarded, policy_oid, bool(token and bot_id))
        return 0
    except Refused as e:
        text = f"{PREFIX}refused, {e} (asked by @{ns.safe(login)})"
    except (RuntimeError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as e:
        print(repr(e), file=sys.stderr)
        text = f"{PREFIX}GitHub refused that, nothing was done (asked by @{ns.safe(login)})."
    try:
        api.comment(number, text)
    except WRITE_ERRORS as e:
        print(f"Could not post the command's refusal: {e}", file=sys.stderr)
        return 1
    return 0


def live() -> int:
    """For the workflow: whether the comment's author could merge today, so that it makes the app's token for them
    alone. Decides nothing: `command` checks again."""
    try:
        scope(ns.actor_from_env(), read_grants(Path(GRANTS).read_text()), datetime.now(timezone.utc).date())
        print("live=true")
    except Refused:
        print("live=false")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["command"]:
        sys.exit(command())
    if sys.argv[1:] == ["live"]:
        sys.exit(live())
    if sys.argv[1:] == ["--verify-merge-group"]:
        sys.exit(verify_merge_group())
    sys.exit(__doc__)
