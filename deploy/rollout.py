import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile
import time
import urllib.request

REPOSITORY = "freQuensy23-coder/hermes-phonebridge"
REQUIRED_CHECKS = {"python", "android-build", "android-e2e (31)", "android-e2e (35)"}
BASE = Path.home() / ".local/share/phonebridge"
SOURCE = Path.home() / "projects/hermes-phonebridge"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def github(path):
    return json.loads(run("gh", "api", path, capture_output=True, text=True).stdout)


def accepted_revision(revision):
    main = github(f"repos/{REPOSITORY}/commits/main")["sha"]
    if main != revision:
        raise RuntimeError("Revision is not current main")
    checks = github(f"repos/{REPOSITORY}/commits/{revision}/check-runs?per_page=100")
    latest = {}
    for check in sorted(checks["check_runs"], key=lambda item: item["id"]):
        latest[check["name"]] = check["conclusion"]
    if any(latest.get(name) != "success" for name in REQUIRED_CHECKS):
        raise RuntimeError("Required automatic checks have not succeeded")


def healthy(revision):
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:18940/healthz", timeout=2) as response:
                body = json.load(response)
            if body.get("status") == "ok" and body.get("revision") == revision:
                return body
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Deployed revision health check failed")


def switch(target):
    temporary = BASE / "current.next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(BASE / "current")


def deploy(revision):
    accepted_revision(revision)
    run("git", "fetch", "origin", "main", cwd=SOURCE)
    fetched = run("git", "rev-parse", "origin/main", cwd=SOURCE, capture_output=True, text=True).stdout.strip()
    if fetched != revision:
        raise RuntimeError("Fetched main differs from accepted revision")
    release = BASE / "releases" / revision
    release.mkdir(parents=True, exist_ok=True)
    archive = run("git", "archive", "--format=tar", revision, cwd=SOURCE, capture_output=True).stdout
    with tempfile.TemporaryFile() as source_archive:
        source_archive.write(archive)
        source_archive.seek(0)
        with tarfile.open(fileobj=source_archive, mode="r:") as source_tar:
            source_tar.extractall(release, filter="data")
    run("uv", "venv", str(release / ".venv"), "--python", "3.11")
    run("uv", "pip", "install", "--python", str(release / ".venv/bin/python"), str(release / "server"), str(release / "cli"))
    (release / "revision.env").write_text(f"PHONEBRIDGE_REVISION={revision}\n")
    os.chmod(release / "revision.env", 0o600)
    current = BASE / "current"
    previous = current.resolve() if current.is_symlink() else None
    accepted_revision(revision)
    switch(release)
    try:
        run("systemctl", "--user", "restart", "phonebridge.service")
        body = healthy(revision)
        run(str(release / ".venv/bin/python"), str(release / "deploy/provision_https.py"))
        run("systemctl", "--user", "enable", "phonebridge.service")
        print(json.dumps({"deployed": True, "health": body}))
    except Exception:
        if previous is not None:
            switch(previous)
            run("systemctl", "--user", "restart", "phonebridge.service")
        else:
            run("systemctl", "--user", "stop", "phonebridge.service")
            current.unlink(missing_ok=True)
        raise


def main():
    command = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if len(command) != 2 or command[0] != "deploy" or not re.fullmatch(r"[0-9a-f]{40}", command[1]):
        raise SystemExit("Only deploy <accepted-main-sha> is permitted")
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True)
    with (BASE / "deployment.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deploy(command[1])


if __name__ == "__main__":
    main()
