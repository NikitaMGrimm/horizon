#!/usr/bin/env python3
"""Merge reviewers add other people's pull requests to the merge queue by comment (docs/release-flow.md "Merge
reviewers").

    merge_command.py command     one `/merge` comment (merge-command.yml): checks who asked and what the pull request
                                 changes, adds it to the merge queue, answers in the thread

A merge reviewer holds no repository role. A line of .github/merge-reviewers.txt names the account (by numeric ID,
never by login), the day the line ends and the folders it covers. On the first line of a comment on a pull request:

    /merge

It is refused with a one-line answer unless all of this holds: the line is live (not past its day, and that day at
most MAX_DAYS away); the pull request is open, to main, not a draft, not the reviewer's own and holds no commit of
theirs; every file it changes is in the reviewer's folders and none is one CODEOWNERS guards; the reviewer's own
approving review is on the head commit; nobody's change request is open; `verify` and every other check of the head
passed; it carries
neither `needs-proof` nor `release-now`; the reviewer merged fewer than MAX_PER_DAY in 24 hours. The queue then runs
`verify` on it like on any other. No code of any pull request is read or run: only the comment's text, its author
and the API's answers.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightly_set as ns  # noqa: E402  (accounts by ID, CODEOWNERS' patterns, the gh call)

Refused = ns.Refused
GRANTS = ".github/merge-reviewers.txt"
# A line never reaches further ahead than this: staying a merge reviewer takes a new date every month.
MAX_DAYS = 31
MAX_PER_DAY = 60
PREFIX = "🤖 Merge: "
# The Team's probation rule (GOVERNANCE.md): proof-waiting pull requests, and ones that release at once, are the Team's.
HELD = ("needs-proof", "release-now")
MAX_COMMITS = 250  # GitHub lists no more of a pull request's commits
REQUIRED = "verify"  # the check main's protection requires (tools/ci/protect_pipeline.sh)
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
QUERY = """query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $n) {
    id state isDraft baseRefName headRefOid isInMergeQueue mergeable changedFiles
    author { ... on User { databaseId } }
    labels(first: 100) { nodes { name } }
    commits(last: 1) { totalCount nodes { commit { statusCheckRollup {
      state
      contexts(first: 100) { nodes {
        ... on CheckRun { name conclusion }
        ... on StatusContext { name: context conclusion: state }
      } }
    } } } }
    latestOpinionatedReviews(first: 100) {
      pageInfo { hasNextPage }
      nodes { state commit { oid } author { login ... on User { databaseId } } }
    }
  }
} }"""
ENQUEUE = """mutation($id: ID!, $head: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head}) { clientMutationId }
}"""


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
    # From the repository's root only: a bare name would cover a folder of that name anywhere.
    if not all(path.startswith("/") for path in words[4:]):
        return None
    return Grant(int(words[0]), words[1], until, tuple(words[4:]))


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
    line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    # Agents start every comment with "🤖 Agent:" (AGENTS.md), so their `/merge` follows it, as in claim_command.
    line = re.sub(r"^🤖\s*Agent:\s*", "", line)
    m = re.match(r"^/(\w+)(?:\s+(.*))?$", line)
    if not m or m.group(1).lower() != "merge":
        return False
    if (m.group(2) or "").strip():
        raise Refused("`/merge` takes no arguments: comment it on the pull request to merge.")
    return True


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
    head: str
    author_id: int | None
    queued: bool
    conflicts: bool
    checks: str | None  # the head's checks together: SUCCESS, FAILURE, PENDING, ERROR, EXPECTED; None when none ran
    required: str | None  # the head's REQUIRED check: SUCCESS, FAILURE...; None when it never ran or hasn't ended
    labels: list[str]
    reviews: list[Review]
    reviews_cut: bool  # more reviewers than one page holds
    changed_files: int
    commits: int


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
    held = [name for name in pr.labels if name.lower() in HELD]
    if held:
        return f"This pull request is labelled `{ns.safe(held[0])}`: the Horizon Team merges those."
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
    # The checks together can pass without the required one: a fork's runs wait for a Team member's approval.
    if pr.required != "SUCCESS":
        state = "hasn't passed" if pr.required is None else f"is {pr.required.lower()}"
        return f"`{REQUIRED}` {state} on `{pr.head[:7]}`: `/merge` again once every check passed."
    if pr.checks != "SUCCESS":
        return f"The checks of `{pr.head[:7]}` are {(pr.checks or 'missing').lower()}: `/merge` again once all passed."
    return None


def files_refusal(files: list[tuple[str, str | None]], count: int, paths: list[str],
                  guarded: list[str]) -> str | None:
    """Why these changed files can't be merged by someone covering `paths`. Each file comes with the name it was
    renamed from, which counts too. `count` is how many files GitHub says the pull request changes: a list it cut
    short hides the rest."""
    if not files or len(files) < count:
        return "GitHub didn't list every file this pull request changes: the Horizon Team merges it."
    names = [name for pair in files for name in pair if name is not None]
    council = [f for f in names if ns.guarded(f, guarded)]
    if council:
        more = f" and {len(council) - 1} more" if len(council) > 1 else ""
        return f"It changes `{council[0]}`{more}, which only the council merges (CODEOWNERS)."
    outside = [f for f in names if not ns.guarded(f, paths)]
    if outside:
        more = f" and {len(outside) - 1} more" if len(outside) > 1 else ""
        return f"It changes `{outside[0]}`{more}, outside the folders your line in `{GRANTS}` covers."
    return None


# --- GitHub ------------------------------------------------------------------------------------------------------


class Api:
    """The few GitHub calls this needs; tests replace it. `merge_token` is the app's, used for the queue alone:
    a pull request the workflow's own token queued gets no `merge_group` run, so `verify` never starts."""

    def __init__(self, repo: str, merge_token: str = ""):
        self.repo, self.merge_token = repo, merge_token

    def pull(self, n: int) -> Pull | None:
        owner, name = self.repo.split("/", 1)
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f",
                            f"name={name}", "-F", f"n={n}"], capture_output=True, text=True)
        try:
            p = json.loads(r.stdout)["data"]["repository"]["pullRequest"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(f"gh api graphql: {r.stderr.strip()[:300]}") from None
        if p is None:
            return None
        last = (p["commits"]["nodes"] or [{}])[0].get("commit") or {}
        rollup = last.get("statusCheckRollup") or {}
        required = {node.get("conclusion") for node in (rollup.get("contexts") or {}).get("nodes", [])
                    if node.get("name") == REQUIRED}
        reviews = p["latestOpinionatedReviews"]
        return Pull(
            node=p["id"], state=p["state"], draft=p["isDraft"], base=p["baseRefName"], head=p["headRefOid"],
            author_id=(p["author"] or {}).get("databaseId"), queued=p["isInMergeQueue"],
            conflicts=p["mergeable"] == "CONFLICTING", checks=rollup.get("state"),
            required=required.pop() if len(required) == 1 else None,
            labels=[node["name"] for node in p["labels"]["nodes"]],
            reviews=[Review((node["author"] or {}).get("databaseId"), (node["author"] or {}).get("login", "?"),
                            node["state"], (node["commit"] or {}).get("oid")) for node in reviews["nodes"]],
            reviews_cut=reviews["pageInfo"]["hasNextPage"], changed_files=p["changedFiles"],
            commits=p["commits"]["totalCount"])

    def files(self, n: int) -> list[tuple[str, str | None]]:
        """Every file the pull request changes, with the name it was renamed from (None when it wasn't)."""
        raw = ns.gh("api", "--paginate", f"repos/{self.repo}/pulls/{n}/files?per_page=100", "--jq",
                    ".[] | [.filename, .previous_filename]")
        return [tuple(json.loads(line)) for line in raw.splitlines() if line.strip()]

    def commit_people(self, n: int) -> set[int]:
        """The accounts that wrote or committed the pull request's commits."""
        raw = ns.gh("api", "--paginate", f"repos/{self.repo}/pulls/{n}/commits?per_page=100", "--jq",
                    ".[] | [.author.id, .committer.id]")
        return {who for line in raw.splitlines() if line.strip() for who in json.loads(line) if who}

    def enqueue(self, node: str, head: str) -> None:
        """Adds it to the merge queue at this head; GitHub refuses when the head has moved since."""
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={ENQUEUE}", "-f", f"id={node}", "-f",
                            f"head={head}"], capture_output=True, text=True,
                           env={**os.environ, "GH_TOKEN": self.merge_token})
        if r.returncode != 0:
            print(r.stderr.strip()[:600], file=sys.stderr)
            why = re.sub(r"[^\w#:.,' -]", "", r.stderr.strip().removeprefix("gh: "))[:200].strip()
            raise Refused(f"GitHub didn't take it into the merge queue ({why or 'no reason given'}).")

    def comment(self, n: int, text: str) -> None:
        ns.gh("api", f"repos/{self.repo}/issues/{n}/comments", "-f", f"body={text}")

    def recent_comments(self, since: str) -> list[dict]:
        """Comments of the whole repository since a time (ISO 8601), as {user, body}."""
        raw = ns.gh("api", "--paginate", f"repos/{self.repo}/issues/comments?since={since}&per_page=100", "--jq",
                    '.[] | {user: .user.login, body: .body}')
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def merges_by(api, login: str, now: datetime) -> int:
    """Pull requests this workflow queued for `login` in the last 24 h, counted from its own comments."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag = re.compile(rf"on behalf of @{re.escape(login.lower())}(?![a-z0-9-])")
    return sum(1 for c in api.recent_comments(since)
               if c["user"] == "github-actions[bot]" and c["body"].startswith(PREFIX)
               and tag.search(c["body"].split("\n", 1)[0].lower()))


def run(api, actor: ns.Actor, number: int, url: str, grants: Grants, guarded: list[str], now: datetime,
        switched_on: bool = True) -> str:
    """Queues the pull request; returns the comment to post. Raises Refused for a refusal."""
    paths = scope(actor, grants, now.date())
    if not switched_on:
        raise Refused("`/merge` isn't switched on yet: the workflow has no app token for the merge queue.")
    pr = api.pull(number)
    if pr is None:
        raise Refused("This isn't a pull request.")
    if (why := pull_refusal(pr, actor)):
        raise Refused(why)
    if (why := files_refusal(api.files(number), pr.changed_files, paths, guarded)):
        raise Refused(why)
    if pr.commits > MAX_COMMITS:
        raise Refused(f"It has more than {MAX_COMMITS} commits, too many to read: the Horizon Team merges it.")
    if actor.id in api.commit_people(number):
        raise Refused("It holds a commit of yours: someone else reviews and merges it.")
    if merges_by(api, actor.login, now) >= MAX_PER_DAY:
        raise Refused(f"You merged {MAX_PER_DAY} pull requests in the last 24 hours. Try again later.")
    api.enqueue(pr.node, pr.head)
    return (f"{PREFIX}added to the merge queue at `{pr.head[:7]}`, on behalf of @{actor.login} "
            f"([their comment]({url})).")


def command() -> int:
    body, login = os.environ["BODY"], os.environ["AUTHOR"]
    number, url, repo = int(os.environ["ISSUE"]), os.environ.get("COMMENT_URL", ""), os.environ["REPO"]
    token = os.environ.get("MERGE_TOKEN", "")
    api: Api = Api(repo, token)
    try:
        if not parse(body):
            return 0
        actor = ns.actor_from_env()
        grants = read_grants(Path(GRANTS).read_text())
        guarded = ns.guarded_patterns(Path(ns.CODEOWNERS).read_text())
        text = run(api, actor, number, url, grants, guarded, datetime.now(timezone.utc), bool(token))
    except Refused as e:
        text = f"{PREFIX}refused, {e} (asked by @{ns.safe(login)})"
    except RuntimeError as e:
        print(e, file=sys.stderr)
        text = f"{PREFIX}GitHub refused that, nothing was done (asked by @{ns.safe(login)})."
    api.comment(number, text)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["command"]:
        sys.exit(__doc__)
    sys.exit(command())
