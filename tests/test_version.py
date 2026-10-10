"""The version: pyproject's, plus the commit when run from a git checkout;
whatever git does, the start goes on."""

import os
import shutil
import subprocess
import tomllib

import pytest

from stvwatch import cli, version

OID = "976b9ae" + "0" * 33
CLEAN = f"# branch.oid {OID}\n# branch.head main\n"
DIRTY = CLEAN + "1 .M N... 100644 100644 100644 aa bb stvwatch/app.py\n"


def git(stdout="", rc=0, raises=None):
    calls = []

    def run(cmd, **_kw):
        calls.append(cmd)
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, rc, stdout, "")

    run.calls = calls
    return run


@pytest.mark.parametrize(
    "checkout, run, want",
    [
        (True, git(CLEAN), "0.1.0+g976b9ae"),
        (True, git(DIRTY), "0.1.0+g976b9ae.dirty"),
        (True, git(raises=FileNotFoundError("git")), "0.1.0"),
        (True, git(raises=subprocess.TimeoutExpired("git", 5)), "0.1.0"),
        (True, git(CLEAN, rc=128), "0.1.0"),
        (True, git("# branch.oid (initial)\n# branch.head main\n"), "0.1.0"),
        (False, git(CLEAN), "0.1.0"),
    ],
    ids=["clean", "dirty", "no-git", "timeout", "git-error", "no-commit", "not-a-checkout"],
)
def test_the_commit_is_added_only_when_git_names_one(tmp_path, checkout, run, want):
    if checkout:
        (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    b = version.describe("0.1.0", version.git_state(str(tmp_path), run=run))
    assert b["version"] == want
    assert (b["commit"] is None) == (want == "0.1.0")
    assert len(run.calls) == checkout


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
@pytest.mark.parametrize(
    "tag, edit, want",
    [
        ("v0.1.0", False, "0.1.0"),
        ("v0.1.0", True, "0.1.0+g{}.dirty"),
        ("v0.0.9", False, "0.1.0+g{}"),
    ],
    ids=["at-the-tag", "at-the-tag-edited", "another-release-tag"],
)
def test_at_the_release_tag_the_release_stands_alone(tmp_path, monkeypatch, tag, edit, want):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def sh(*args):
        cmd = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args]
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()

    sh("init", "-q")
    (tmp_path / "f").write_text("1\n")
    sh("add", "f")
    sh("commit", "-q", "-m", "c")
    sh("tag", "-a", tag, "-m", tag)
    if edit:
        (tmp_path / "f").write_text("2\n")
    head = sh("rev-parse", "HEAD")
    b = version.describe("0.1.0", version.git_state(str(tmp_path), release="0.1.0"))
    assert (b["version"], b["commit"]) == (want.format(head[:7]), head)


def test_version_prints_the_package_version_from_pyproject(capsys):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), "rb") as f:
        release = tomllib.load(f)["project"]["version"]
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    out = capsys.readouterr().out
    assert (e.value.code, out) == (0, f"stv-watch {version.version()}\n")
    assert version.version().split("+")[0] == release
