"""The version every output names.

The release comes from the package metadata (pyproject.toml). Run from a git
checkout, the release alone does not say which code ran, so the commit is
added as a PEP 440 local label: 0.1.0+g976b9ae, 0.1.0+g976b9ae.dirty when
tracked files differ from it (as `git describe --dirty`).
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


def git_state(root=ROOT, run=subprocess.run):
    """-> (commit, dirty) of the checkout at `root`, or None: not a checkout,
    no git, or git failed. Never raises: the version must not stop a start."""
    # Only the checkout's own root: an installed copy may sit in some other
    # work tree (a .venv inside a clone) whose commit says nothing about it.
    if not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        p = run(
            ["git", "-C", root, "status", "--porcelain=v2", "--branch", "--untracked-files=no"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    commit, dirty = None, False
    for line in p.stdout.splitlines():
        if line.startswith("# branch.oid "):
            commit = line.split()[2]
        elif not line.startswith("#"):
            dirty = True
    if commit is None or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return None  # "(initial)": no commit yet
    return commit, dirty


def describe(rel, git):
    """-> {"version", "release", "commit", "dirty"}; commit and dirty are None
    outside a checkout."""
    if git is None:
        return {"version": rel, "release": rel, "commit": None, "dirty": None}
    commit, dirty = git
    local = "g" + commit[:7] + (".dirty" if dirty else "")
    return {
        "version": rel + ("." if "+" in rel else "+") + local,
        "release": rel,
        "commit": commit,
        "dirty": dirty,
    }


@functools.cache
def build():
    return describe(release(), git_state())


def version():
    return build()["version"]
