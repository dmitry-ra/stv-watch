"""onnxruntime on Linux reports usage to Microsoft unless switched off before
it is imported; stv-watch switches it off."""

import os
import subprocess
import sys

# onnxruntime keeps quiet by itself under these (CI runners, its own tests), so
# the child must not inherit them, or the test passes without the switch
QUIET = (
    "ORT_DISABLE_TELEMETRY",
    "ORT_RUNNING_UNIT_TESTS",
    "CI",
    "TF_BUILD",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "CIRCLECI",
    "TRAVIS",
    "JENKINS_URL",
    "CODEBUILD_BUILD_ID",
    "BUILDKITE",
    "TEAMCITY_VERSION",
    "APPVEYOR",
    "BITBUCKET_BUILD_NUMBER",
    "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI",
)


def test_onnxruntime_leaves_no_device_id_and_no_upload_queue(tmp_path):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in QUIET}
    env.update(HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"), TMPDIR=str(tmp))
    code = "import stvwatch.asr, onnxruntime, sherpa_onnx"
    subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=60)
    # the device id and the event queue go to ~/.cache/Microsoft, the uploader's
    # log (mat-debug-PID.log) and session file (.ses) to TMPDIR
    assert not (home / ".cache").exists()
    assert sorted(p.name for p in tmp.iterdir()) == []
