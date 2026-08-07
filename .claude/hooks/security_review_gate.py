"""PreToolUse gate: block writes to `main` that no security review covers.

Reads the hook JSON on stdin. If the shell command would move `main` — `gh pr create
--base main`, `gh pr merge` of a main-based PR, `git merge` while on main, or a
`git push` whose destination ref is main — it is denied unless
`.claude/.last-security-review` holds the sha of *the code being merged*. That stamp is
written after a clean /security-review run, and any new commit invalidates it.

The subject of the review is deliberately not "whatever HEAD happens to be": for a
`gh pr merge` the local checkout may be an unrelated branch, so the gate resolves the
PR's head commit and requires the stamp to match *that*. Anything it cannot resolve is
treated as unreviewed, so the gate fails closed.

Failing closed has two distinct causes and they get two distinct messages. Either the
command **does** target main and the subject sha is unknown, or the gate could not tell
**whether** it targets main at all — the latter happens when `gh pr view` fails, which is
transient and common. Both deny; conflating them in the message sends the reader looking
for a main-targeted merge that never existed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

MARKER_REL = Path(".claude") / ".last-security-review"

#: Returned instead of a sha when a command **does** target main but the subject cannot
#: be resolved (offline, no such PR, detached ref). Never equal to a real sha, so it
#: always fails the freshness check.
UNRESOLVED = ""

#: Returned when the gate cannot even establish *whether* the command targets main —
#: typically because `gh pr view` failed (gh unauthenticated, offline, rate-limited).
#: Kept distinct from UNRESOLVED only so the message can be honest: both deny.
#: Not a valid sha, and `review_covers` refuses it explicitly.
UNDETERMINED = "?"

DENY_REASON = (
    "BLOCKED by security-review gate: this command targets the `main` branch and no "
    "security review covers the code being merged. Run the full /security-review skill "
    "on that code now. When it finishes with no unresolved HIGH/MEDIUM findings, stamp "
    "it with: git rev-parse HEAD > .claude/.last-security-review — then retry this "
    "command. If findings were reported, fix them (or have Teo explicitly accept them) "
    "before stamping."
)

UNDETERMINED_REASON = (
    "BLOCKED by security-review gate: it could NOT determine whether this command "
    "targets `main`, so it failed closed. This is usually transient — resolving a PR's "
    "base shells out to `gh pr view`, which fails when gh is unauthenticated, offline "
    "or rate-limited. It does NOT mean the command targets main.\n"
    "Check with: gh pr view <number> --json baseRefName  (and `gh auth status`).\n"
    "If that reports a base other than `main`, simply retry — the gate will resolve it "
    "and allow the command. Only run /security-review if the base really is `main`."
)


def run(*args: str) -> str | None:
    """Run a command and return stripped stdout, or None on any failure."""
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=20, check=True
        ).stdout.strip()
        return out or None
    except Exception:
        return None


# A command can only start at the beginning of the string or after a shell
# separator — otherwise prose inside a commit/PR message ("denies gh pr merge…")
# would trip the gate, which is exactly how the first commit of this hook failed.
CMD_START = r"(?:^|[;&|(\n]\s*)"

_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"", re.S)


def sanitize(cmd: str) -> str:
    """Neutralize quoted prose while keeping single-word quoted args.

    `-m "long message about gh pr merge"` must not look like a merge, but
    `--base "main"` must still look like main. Single-token quoted content is
    unwrapped; anything with whitespace or shell metacharacters becomes a space.
    """
    def repl(m: re.Match[str]) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        return inner if re.fullmatch(r"[\w./=:+-]*", inner or "") else " "

    return _QUOTED.sub(repl, cmd)


def sha_of(ref: str) -> str:
    """Resolve a git ref to a full sha, or UNRESOLVED when it cannot be resolved."""
    return run("git", "rev-parse", ref) or UNRESOLVED


def pr_merge_selector(args: str) -> str | None:
    """Return the PR number/branch/url argument of `gh pr merge`, ignoring leading flags.

    `gh pr merge --merge 16` is as valid as `gh pr merge 16 --merge`, and missing the
    selector would silently resolve the PR of the *current* branch instead.
    """
    for token in args.split():
        if not token.startswith("-"):
            return token
    return None


def push_destinations(args: str) -> list[str]:
    """Return the destination refs of a `git push`, resolving the bare-push case.

    A push with no refspec sends the current branch to its upstream, so the current
    branch is the destination. Otherwise every refspec's right-hand side is one.
    """
    tokens = [t for t in args.split() if not t.startswith("-")]
    refspecs = tokens[1:]  # tokens[0] is the remote
    if not refspecs:
        current = run("git", "branch", "--show-current")
        return [current] if current else []
    return [spec.lstrip("+").split(":")[-1] for spec in refspecs]


def push_source(args: str) -> str:
    """Return the sha a `git push` would publish (the refspec's left-hand side)."""
    tokens = [t for t in args.split() if not t.startswith("-")]
    refspecs = tokens[1:]
    if not refspecs:
        return sha_of("HEAD")
    source = refspecs[0].lstrip("+").split(":")[0]
    return sha_of(source or "HEAD")


def gate_subject(cmd: str) -> str | None:
    """Return the sha a review must cover, or None when the command does not touch main.

    UNRESOLVED means "targets main, subject unknown" — the caller treats that as
    unreviewed rather than waving it through.
    """
    cmd = sanitize(cmd)

    if re.search(CMD_START + r"gh\s+pr\s+create\b", cmd) and re.search(
        r"(--base[= ]|-B )main\b", cmd
    ):
        return sha_of("HEAD")

    merge_pr = re.search(CMD_START + r"gh\s+pr\s+merge\b([^;&|\n]*)", cmd)
    if merge_pr:
        sel = pr_merge_selector(merge_pr.group(1))
        view = ["gh", "pr", "view"] + ([sel] if sel else [])
        base = run(*view, "--json", "baseRefName", "-q", ".baseRefName")
        if base is None:
            # Cannot tell what it merges into: fail closed, but say *that* rather than
            # asserting it targets main — the two are different claims, and reporting the
            # wrong one sends the reader hunting for a main-targeted merge that never was.
            return UNDETERMINED
        if base != "main":
            return None
        return run(*view, "--json", "headRefOid", "-q", ".headRefOid") or UNRESOLVED

    git_merge = re.search(CMD_START + r"git\s+merge\b([^;&|\n]*)", cmd)
    if git_merge:
        if run("git", "branch", "--show-current") != "main":
            return None
        # The subject is the branch being merged in, not main's current tip.
        sources = [t for t in git_merge.group(1).split() if not t.startswith("-")]
        return sha_of(sources[0]) if sources else UNRESOLVED

    git_push = re.search(CMD_START + r"git\s+push\b([^;&|\n]*)", cmd)
    if git_push:
        args = git_push.group(1)
        if "main" not in push_destinations(args):
            return None
        return push_source(args)

    return None


def review_covers(subject: str) -> bool:
    """True when the marker file holds exactly the sha of the code being merged."""
    root = run("git", "rev-parse", "--show-toplevel")
    # Both sentinels are refused explicitly. UNRESOLVED is falsy and would fail anyway;
    # UNDETERMINED is truthy, so without this line a marker file containing "?" would
    # satisfy the gate.
    if not root or not subject or subject == UNDETERMINED:
        return False
    try:
        return (Path(root) / MARKER_REL).read_text(encoding="utf-8").strip() == subject
    except OSError:
        return False


def main() -> None:
    """Entry point: emit a deny decision or stay silent (allow)."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd:
        return
    subject = gate_subject(cmd)
    if subject is None or review_covers(subject):
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            # Same decision either way; only the explanation differs.
            "permissionDecisionReason": (
                UNDETERMINED_REASON if subject == UNDETERMINED else DENY_REASON
            ),
        }
    }))


if __name__ == "__main__":
    main()
