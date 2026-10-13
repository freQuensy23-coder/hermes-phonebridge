import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


CLOUDFLARED_VERSION = "2026.10.0"
CLOUDFLARED_SHA256 = "d33ff2d14475178d2012c2c56beba87389ac5ded27649519f198a7d3134a99db"
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}/cloudflared-linux-amd64"
)
PUBLIC_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com")
LOCAL_ORIGIN = "http://127.0.0.1:18940"


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise RuntimeError("Cloudflared download attempted an insecure redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def private_write(path, text):
    with path.open("w", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        output.write(text)


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except FileNotFoundError:
        return None


def save_processes(root, records):
    pending = root / "processes.pending.json"
    private_write(pending, json.dumps(records))
    pending.replace(root / "processes.json")


def launch(root, records, command, environment, log):
    pending_signals = []
    previous_handlers = {
        sig: signal.signal(sig, lambda signum, frame: pending_signals.append(signum))
        for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        with log.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                command, env=environment, cwd=root, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
        records.append({"pid": process.pid, "start": process_identity(process.pid)})
        save_processes(root, records)
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    if pending_signals:
        raise RuntimeError("CI server preparation interrupted while recording child processes")
    return process


def stop(root, records=None):
    if records is None:
        state = root / "processes.json"
        records = json.loads(state.read_text()) if state.exists() else []
    for sig, timeout in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        for record in reversed(records):
            pid = record["pid"]
            if record["start"] is not None and process_identity(pid) == record["start"]:
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(record["start"] is not None and process_identity(record["pid"]) == record["start"] for record in records):
                shutil.rmtree(root, ignore_errors=True)
                return
            time.sleep(0.1)
    raise RuntimeError("CI server or tunnel survived bounded process-group cleanup")


def isolated_environment(root):
    keys = ("PATH", "LANG", "LC_ALL", "RUNNER_TRACKING_ID", "SYSTEMROOT")
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    environment["HOME"] = str(root)
    environment["TMPDIR"] = str(root)
    return environment


def download_cloudflared(root):
    target = root / "cloudflared"
    opener = urllib.request.build_opener(HttpsRedirect(), urllib.request.ProxyHandler({}))
    digest = hashlib.sha256()
    total = 0
    deadline = time.monotonic() + 120
    with opener.open(CLOUDFLARED_URL, timeout=30) as response, target.open("wb") as output:
        if response.status != 200 or urllib.parse.urlsplit(response.url).scheme != "https":
            raise RuntimeError("Cloudflared download did not use successful HTTPS")
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 64 * 1024 * 1024 or time.monotonic() >= deadline:
                raise RuntimeError("Cloudflared download exceeded its size or time bound")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != CLOUDFLARED_SHA256:
        raise RuntimeError("Pinned cloudflared SHA-256 mismatch")
    target.chmod(0o700)
    return target


def discover_origin(tunnel, log):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if tunnel.poll() is not None:
            raise RuntimeError("Cloudflared exited before assigning a quick tunnel")
        text = log.read_text(encoding="utf-8")
        candidates = set(re.findall(r"https://[^\s\"|]+\.trycloudflare\.com[^\s\"|]*", text))
        if candidates:
            if len(candidates) != 1:
                raise RuntimeError("Cloudflared emitted ambiguous public origins")
            origin = candidates.pop()
            if PUBLIC_ORIGIN.fullmatch(origin) is None:
                raise RuntimeError("Cloudflared emitted an invalid quick tunnel origin")
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme != "https" or parsed.netloc != parsed.hostname or parsed.path or parsed.query or parsed.fragment:
                raise RuntimeError("Quick tunnel must be an exact HTTPS origin")
            return origin
        time.sleep(0.25)
    raise RuntimeError("Cloudflared did not assign a validated public hostname")


def health(origin, revision):
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(origin + "/healthz", timeout=5) as response:
        if response.status != 200 or response.url != origin + "/healthz":
            raise RuntimeError("CI health check redirected or failed")
        body = response.read(8193)
    if len(body) > 8192:
        raise RuntimeError("CI health response exceeded its size bound")
    value = json.loads(body)
    if value != {"status": "ok", "version": "0.1.0", "revision": revision}:
        raise RuntimeError("CI server health revision mismatch")


def wait_for_health(origin, revision, processes):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("CI server or tunnel exited before health readiness")
        try:
            health(origin, revision)
            return
        except (OSError, ValueError, RuntimeError, urllib.error.URLError):
            time.sleep(1)
    raise RuntimeError("CI server did not become healthy at its exact advertised origin")


def interrupted(signum, frame):
    raise RuntimeError(f"CI server preparation interrupted by signal {signum}")


def start(root, temporary):
    root.mkdir(mode=0o700)
    records = []
    try:
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        binary = download_cloudflared(root)
        environment = isolated_environment(root)
        tunnel_log = temporary / "phonebridge-tunnel.log"
        tunnel = launch(root, records, [
            str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2",
            "--url", LOCAL_ORIGIN, "--loglevel", "info",
        ], environment, tunnel_log)
        origin = discover_origin(tunnel, tunnel_log)
        token = secrets.token_urlsafe(48)
        print(f"::add-mask::{token}", flush=True)
        token_file = root / "admin-token"
        private_write(token_file, token + "\n")
        revision = os.environ["GITHUB_SHA"]
        server_environment = environment | {
            "PHONEBRIDGE_ADMIN_TOKEN": token,
            "PHONEBRIDGE_PUBLIC_URL": origin,
            "PHONEBRIDGE_ALLOW_INSECURE": "true",
            "PHONEBRIDGE_DB_PATH": str(root / "phonebridge.sqlite3"),
            "PHONEBRIDGE_REVISION": revision,
        }
        server = launch(root, records, [
            sys.executable, "-m", "uvicorn", "phonebridge_server.app:create_app",
            "--factory", "--host", "127.0.0.1", "--port", "18940", "--no-access-log",
        ], server_environment, temporary / "phonebridge-server.log")
        wait_for_health(LOCAL_ORIGIN, revision, (server, tunnel))
        wait_for_health(origin, revision, (server, tunnel))
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
            output.write(f"PHONEBRIDGE_CI_PUBLIC_URL={origin}\n")
            output.write(f"PHONEBRIDGE_CI_ADMIN_TOKEN={token}\n")
            output.write(f"PHONEBRIDGE_CI_ADMIN_TOKEN_FILE={token_file}\n")
        print(f"Synthetic CI server ready over verified public HTTPS: {origin}")
    except BaseException:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop(root, records)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop"))
    arguments = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Ephemeral synthetic servers may run only in automated GitHub CI")
    os.umask(0o077)
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    root = temporary / "phonebridge-emulator-private"
    if arguments.action == "start":
        start(root, temporary)
    elif root.exists():
        stop(root)


if __name__ == "__main__":
    main()
