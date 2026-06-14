"""Self-update via git — shared by the CLI (`trading_agent.py update`) and
the web UI's Update page. Pulls the latest commit on the current branch and
hard-resets the working tree to match (mirrors the StockArena update flow)."""

import os
import subprocess

REPO_DIR = os.path.dirname(os.path.abspath(__file__))


class UpdateError(Exception):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise UpdateError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def is_git_repo() -> bool:
    return os.path.isdir(os.path.join(REPO_DIR, ".git"))


def check_for_update() -> dict:
    """Fetch from origin and report whether HEAD is behind."""
    if not is_git_repo():
        raise UpdateError("Not a git repository — auto-update unavailable")

    _git("fetch", "origin", "--quiet")
    branch  = _git("rev-parse", "--abbrev-ref", "HEAD")
    current = _git("rev-parse", "HEAD")
    remote  = _git("rev-parse", f"origin/{branch}")
    up_to_date = current == remote

    changelog = []
    if not up_to_date:
        log = _git("log", "--oneline", f"HEAD..origin/{branch}")
        changelog = [line for line in log.splitlines() if line.strip()][:30]

    return {
        "up_to_date":    up_to_date,
        "branch":        branch,
        "current_short": current[:7],
        "remote_short":  remote[:7],
        "changelog":     changelog,
    }


def apply_update():
    """Generator that yields progress strings while applying the update.

    The final item is a ("done", success: bool, message: str) tuple — every
    other item is a plain log line string.
    """
    if not is_git_repo():
        yield ("done", False, "Not a git repository — cannot update")
        return

    try:
        branch  = _git("rev-parse", "--abbrev-ref", "HEAD")
        current = _git("rev-parse", "HEAD")
        yield f"Current version : {current[:7]}  (branch: {branch})"

        yield "Fetching latest code from GitHub…"
        _git("fetch", "origin", "--quiet")

        remote = _git("rev-parse", f"origin/{branch}")
        if current == remote:
            yield ("done", True, "Already up to date — no restart needed.")
            return

        log = _git("log", "--oneline", f"HEAD..origin/{branch}")
        commits = [line for line in log.splitlines() if line.strip()]
        yield f"{len(commits)} new commit{'s' if len(commits) != 1 else ''}:"
        for line in commits:
            yield f"  {line}"

        yield "Applying update…"
        _git("reset", "--hard", f"origin/{branch}")
        new_sha = _git("rev-parse", "--short", "HEAD")
        yield f"Updated to {new_sha}"

        yield ("done", True, f"Updated to {new_sha}.")
    except UpdateError as e:
        yield ("done", False, f"Update failed: {e}")
