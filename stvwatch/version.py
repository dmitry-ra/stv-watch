"""The version every output names.

The release comes from the package metadata (pyproject.toml). Run from a git
checkout, the release alone does not say which code ran, so the commit is
added as a PEP 440 local label: 0.1.0+g976b9ae, 0.1.0+g976b9ae.dirty when
tracked files differ from it (as `git describe --dirty`). At the release's own
tag (v0.1.0) with no changes the release does say it, and stands alone.
"""

import functools
import os
import re
import subprocess
from importlib import metadata

DIST = "stv-watch"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def release():
    try:
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "0+unknown"


def _git(root, run, *args):
    """git's stdout, or None if it could not run or failed."""
    try:
        p = run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def git_state(root=ROOT, run=subprocess.run, release=None):
    """-> (commit, dirty, tagged) of the checkout at `root`, or None: not a
    checkout, no git, or git failed. `tagged`: tag v`release` points at the
    commit. Never raises: the version must not stop a start."""
    # Only the checkout's own root: an installed copy may sit in some other
    # work tree (a .venv inside a clone) whose commit says nothing about it.
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    out = _git(root, run, "status", "--porcelain=v2", "--branch", "--untracked-files=no")
    if out is None:
        return None
    commit, dirty = None, False
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            commit = line.split()[2]
        elif not line.startswith("#"):
            dirty = True
    if commit is None or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return None  # "(initial)": no commit yet
    # A second call, and only for a clean tree: status names no tags, and
    # `git describe --dirty` would take the index lock from under the user.
    tag = f"v{release}"
    tagged = (
        release is not None
        and not dirty
        and _git(root, run, "tag", "--points-at", commit, "--list", tag) == tag + "\n"
    )
    return commit, dirty, tagged


def describe(rel, git):
    """-> {"version", "release", "commit", "dirty"}; commit and dirty are None
    outside a checkout."""
    if git is None:
        return {"version": rel, "release": rel, "commit": None, "dirty": None}
    commit, dirty, tagged = git
    local = "g" + commit[:7] + (".dirty" if dirty else "")
    return {
        "version": rel if tagged else rel + ("." if "+" in rel else "+") + local,
        "release": rel,
        "commit": commit,
        "dirty": dirty,
    }


@functools.cache
def build():
    rel = release()
    return describe(rel, git_state(release=rel))


def version():
    return build()["version"]
