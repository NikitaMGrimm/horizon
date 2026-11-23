#!/usr/bin/env python3
"""Merge reviewers add other people's pull requests to the merge queue by comment (docs/release-flow.md "Merge
reviewers").

    merge_command.py command     one `/merge` comment (merge-command.yml): checks who asked and what the pull request
                                 changes, adds it to the merge queue, answers in the thread
    merge_command.py live        prints `live=true` when the comment's author has a live line, for the workflow:
                                 only then does it make the app's token

A merge reviewer holds no repository role. A line of .github/merge-reviewers.txt names the account (by numeric ID,
never by login), the day the line ends and the folders it covers. On the first line of a comment on a pull request:

    /merge

It is refused with a one-line answer unless all of this holds: the line is live (not past its day, and that day at
most MAX_DAYS away); the pull request is open, to main, not a draft, not the reviewer's own and holds no commit of
theirs, co-written ones included; every file it changes is in the reviewer's folders and none is one CODEOWNERS
guards; the reviewer's own approving review is on the head commit; nobody's change request is open; `verify` and
every other check of the head passed; it carries
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
MAX_COMMITS = 100  # one page of a pull request's commits
MAX_AUTHORS = 10  # of one commit
MAX_FILES = 300  # GitHub compares no more
REQUIRED = "verify"  # the check main's protection requires (tools/ci/protect_pipeline.sh)
ACTIONS_APP = 15368  # GitHub Actions: only its `verify` counts, as in main's protection
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
QUERY = """query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $n) {
    id state isDraft baseRefName baseRefOid headRefOid isInMergeQueue mergeable changedFiles
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
    line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    # Agents start every comment with "🤖 Agent:" (AGENTS.md), so their `/merge` follows it, as in claim_command.
    line = re.sub(r"^🤖\s*Agent:\s*", "", line)
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
    if pr.checks != "SUCCESS":
        return f"The checks of `{pr.head[:7]}` are {(pr.checks or 'missing').lower()}: `/merge` again once all passed."
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
    renamed from, which counts too. `count` is how many files GitHub says the pull request changes: a list it cut
    short hides the rest."""
    if not files or len(files) < count or len(files) >= MAX_FILES:
        return "GitHub didn't list every file this pull request changes: the Horizon Team merges it."
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
    return Pull(
        node=p["id"], state=p["state"], draft=p["isDraft"], base=p["baseRefName"], base_oid=p["baseRefOid"],
        head=p["headRefOid"], author_id=(p["author"] or {}).get("databaseId"), queued=p["isInMergeQueue"],
        conflicts=p["mergeable"] == "CONFLICTING", checks=(head.get("statusCheckRollup") or {}).get("state"),
        labels=[node["name"] for node in p["labels"]["nodes"]],
        reviews=[Review((node["author"] or {}).get("databaseId"), (node["author"] or {}).get("login", "?"),
                        node["state"], (node["commit"] or {}).get("oid")) for node in reviews["nodes"]],
        reviews_cut=reviews["pageInfo"]["hasNextPage"], changed_files=p["changedFiles"], people=people,
        people_cut=cut)


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
            response = json.loads(r.stdout)
        except ValueError:
            raise RuntimeError(f"gh api graphql: {r.stderr.strip()[:300]}") from None
        return read_pull(response)

    def required_check(self, head: str) -> list[str | None]:
        """How GitHub Actions' newest `verify` runs on this commit ended (None: still running)."""
        return json.loads(ns.gh("api", f"repos/{self.repo}/commits/{head}/check-runs?check_name={REQUIRED}"
                                f"&app_id={ACTIONS_APP}&filter=latest&per_page=100", "--jq",
                                "[.check_runs[].conclusion]"))

    def files(self, base: str, head: str) -> list[tuple[str, str | None]]:
        """Every file `head` changes since it left `base`, with the name it was renamed from (None when it wasn't).
        Between two commits, not "the pull request's files": that list follows the branch, which can move between
        this read and the others, while the queue takes exactly `head`."""
        raw = ns.gh("api", f"repos/{self.repo}/compare/{base}...{head}?per_page=1", "--jq",
                    ".files[] | [.filename, .previous_filename]")
        return [tuple(json.loads(line)) for line in raw.splitlines() if line.strip()]

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

    def recent_answers(self, since: str) -> list[str]:
        """This workflow's own answers in the whole repository since a time (ISO 8601)."""
        raw = ns.gh("api", "--paginate", f"repos/{self.repo}/issues/comments?since={since}&per_page=100", "--jq",
                    '.[] | select(.user.login == "github-actions[bot]" and (.body | startswith("' + PREFIX
                    + '"))) | .body')
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def mark(actor: ns.Actor) -> str:
    """Ends a merge's answer, unseen: what the day's count goes by. The ID, since a login can change hands."""
    return f"<!-- merge-by:{actor.id} -->"


def merges_by(api, actor: ns.Actor, now: datetime) -> int:
    """Pull requests this workflow queued for `actor` in the last 24 h, counted from its own answers."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sum(1 for body in api.recent_answers(since) if body.rstrip().endswith(mark(actor)))


def ready(api, actor: ns.Actor, number: int) -> Pull:
    """The pull request as it is now, if `actor` may queue it. Raises Refused."""
    pr = api.pull(number)
    if pr is None:
        raise Refused("This isn't a pull request.")
    if (why := pull_refusal(pr, actor)):
        raise Refused(why)
    return pr


def run(api, actor: ns.Actor, number: int, url: str, grants: Grants, guarded: list[str], now: datetime,
        switched_on: bool = True) -> str:
    """Queues the pull request; returns the comment to post. Raises Refused for a refusal."""
    paths = scope(actor, grants, now.date())
    if not switched_on:
        raise Refused("`/merge` isn't switched on yet: the workflow has no app token for the merge queue.")
    pr = ready(api, actor, number)
    if (why := required_refusal(api.required_check(pr.head), pr.head)):
        raise Refused(why)
    if (why := files_refusal(api.files(pr.base_oid, pr.head), pr.changed_files, paths, guarded)):
        raise Refused(why)
    if merges_by(api, actor, now) >= MAX_PER_DAY:
        raise Refused(f"You merged {MAX_PER_DAY} pull requests in the last 24 hours. Try again later.")
    # Counting the day's merges reads many pages: a label, a change request or a push may have come meanwhile.
    if ready(api, actor, number).head != pr.head:
        raise Refused("It got a new commit while this was checking it: review it and `/merge` again.")
    api.enqueue(pr.node, pr.head)
    return (f"{PREFIX}added to the merge queue at `{pr.head[:7]}`, on behalf of @{actor.login} "
            f"([their comment]({url})). {mark(actor)}")


def command() -> int:
    body, login = os.environ["BODY"], os.environ["AUTHOR"]
    number, url, repo = int(os.environ["ISSUE"]), os.environ.get("COMMENT_URL", ""), os.environ["REPO"]
    # Out of the environment: only the queue call gets the app's token, no other `gh` this starts.
    token = os.environ.pop("MERGE_TOKEN", "")
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
    except (RuntimeError, ValueError, KeyError, TypeError) as e:  # GitHub failed, or answered in another shape
        print(repr(e), file=sys.stderr)
        text = f"{PREFIX}GitHub refused that, nothing was done (asked by @{ns.safe(login)})."
    api.comment(number, text)
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
    sys.exit(__doc__)
