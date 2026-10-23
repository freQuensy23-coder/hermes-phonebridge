import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import tempfile
import time

SOURCE = Path(__file__).resolve().parent
HOME = Path.home()
FILES = {"bootstrap.py": "bin/bootstrap.py", "rollout.py": "bin/deploy_entry.py", "provision_https.py": "bin/provision_https.py", "phonebridge.service": "unit"}


def run(*args):
    return subprocess.run(args, check=True, timeout=25, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def validate_key(value):
    fields = value.split()
    if len(fields) != 3 or fields[0] != "ssh-ed25519" or fields[2] != "phonebridge-github-deploy":
        raise RuntimeError("Unexpected deployment key")
    try:
        decoded = base64.b64decode(fields[1], validate=True)
        if len(decoded) != 51 or decoded[:4] != struct.pack(">I", 11) or decoded[4:15] != b"ssh-ed25519" or decoded[15:19] != struct.pack(">I", 32):
            raise ValueError()
    except ValueError:
        raise RuntimeError("Invalid Ed25519 public key encoding") from None
    return fields


def snapshot(path):
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise RuntimeError("Refusing nonregular bootstrap target")
    return (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None


def replace(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    base = HOME / ".local/share/phonebridge"
    entry = base / "bin/deploy_entry.py"
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(entry)):
        raise RuntimeError("Unsupported forced-command path")
    public_key = (HOME / ".hermes/credentials/phonebridge/deploy_ed25519.pub").read_text().strip()
    fields = validate_key(public_key)
    authorized = HOME / ".ssh/authorized_keys"
    old_key = snapshot(authorized)
    existing = old_key[0].decode() if old_key else ""
    expected = f'restrict,command="/usr/bin/python3 {entry}" {public_key}'
    matching = [line for line in existing.splitlines() if fields[1] in line.split()]
    if matching and matching != [expected]:
        raise RuntimeError("Deployment key exists with different restrictions")
    contents = {}
    trust = {}
    unit = HOME / ".config/systemd/user/phonebridge.service"
    for filename, location in FILES.items():
        source = SOURCE / filename
        if not stat.S_ISREG(source.lstat().st_mode):
            raise RuntimeError("Bootstrap source must be regular")
        data = source.read_bytes()
        trust[filename] = hashlib.sha256(data).hexdigest()
        target = unit if location == "unit" else base / location
        contents[target] = (data, 0o600 if location == "unit" else 0o700)
    contents[base / "trust.json"] = (json.dumps({"format": 1, "trust": trust}, sort_keys=True).encode(), 0o600)
    contents[authorized] = ((existing + ("" if not existing or existing.endswith("\n") else "\n") + expected + "\n").encode() if not matching else existing.encode(), 0o600)
    before = {path: snapshot(path) for path in contents}
    for path in contents:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (base / "state").mkdir(exist_ok=True, mode=0o700)
    with (base / "deployment.lock").open("a") as lock:
        end = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("Bootstrap lock deadline exceeded")
                time.sleep(0.1)
        if (base / "transaction.json").exists():
            raise RuntimeError("Recover pending application transaction before bootstrap update")
        if snapshot(authorized) != old_key or any(snapshot(path) != value for path, value in before.items()):
            raise RuntimeError("Bootstrap targets changed during preparation")
        changed = []
        saved = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        try:
            for path, (data, mode) in contents.items():
                replace(path, data, mode)
                changed.append(path)
            run("systemctl", "--user", "daemon-reload")
        except BaseException as original:
            failures = []
            for path in reversed(changed):
                try:
                    if before[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        replace(path, *before[path])
                except OSError as error:
                    failures.append(type(error).__name__)
            try:
                run("systemctl", "--user", "daemon-reload")
            except subprocess.SubprocessError as error:
                failures.append(type(error).__name__)
            if failures:
                raise RuntimeError(f"Bootstrap failed {type(original).__name__}; restore failed {failures}") from None
            raise
        finally:
            for number, handler in saved.items():
                signal.signal(number, handler)
    print(json.dumps({"bootstrap": "installed", "entry": str(entry), "trust": trust}, sort_keys=True))


if __name__ == "__main__":
    main()
