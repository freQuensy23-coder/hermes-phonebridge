import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token=None):
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=2) as response:
        if response.status != 200:
            raise RuntimeError("Unexpected HTTP status")
        body = response.read(65537)
    if len(body) > 65536:
        raise RuntimeError("Oversized smoke response")
    return json.loads(body)


def run(*arguments, environment=None, cwd=None):
    return subprocess.run(arguments, env=environment, cwd=cwd, check=True, capture_output=True, text=True, timeout=60)


def smoke(wheelhouse, revision):
    wheelhouse = Path(wheelhouse).resolve(strict=True)
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels or not (wheelhouse / "phonebridge_server-0.1.0-py3-none-any.whl").is_file() or not (wheelhouse / "phonebridge_cli-0.1.0-py3-none-any.whl").is_file():
        raise RuntimeError("Missing actual CI project wheels")
    import hashlib

    scratch = os.environ.get("RUNNER_TEMP")
    with tempfile.TemporaryDirectory(prefix="phonebridge-offline-smoke-", dir=scratch) as temporary:
        root = Path(temporary)
        virtualenv = root / "runtime"
        run(sys.executable, "-m", "venv", str(virtualenv))
        requirements = root / "requirements.txt"
        requirements.write_text("".join(str(path) + " --hash=sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() + "\n" for path in wheels))
        python = str(virtualenv / "bin/python")
        run(python, "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes", "-r", str(requirements), cwd=root)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        token = secrets.token_urlsafe(48)
        url = f"http://127.0.0.1:{port}"
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update({
            "PHONEBRIDGE_ADMIN_TOKEN": token,
            "PHONEBRIDGE_TOKEN": token,
            "PHONEBRIDGE_URL": url,
            "PHONEBRIDGE_PUBLIC_URL": "https://ci-offline-smoke.example",
            "PHONEBRIDGE_ALLOW_INSECURE": "false",
            "PHONEBRIDGE_DB_PATH": str(root / "state.sqlite3"),
            "PHONEBRIDGE_REVISION": revision,
            "HERMES_HOME": str(root / "hermes-test-home"),
        })
        with (root / "server.log").open("w") as output:
            server = subprocess.Popen([python, "-m", "uvicorn", "phonebridge_server.app:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], env=environment, cwd=root, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 20
                health = None
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        raise RuntimeError("Actual wheel-installed server exited before readiness")
                    try:
                        health = request(url + "/healthz")
                        break
                    except (OSError, ValueError):
                        time.sleep(0.1)
                if health is None or health.get("status") != "ok" or health.get("version") != "0.1.0" or health.get("revision") != revision:
                    raise RuntimeError("Actual wheel-installed revision health mismatch")
                cli = str(virtualenv / "bin/phonebridge")
                devices = json.loads(run(cli, "devices", "list", environment=environment, cwd=root).stdout)
                if devices != {"devices": []}:
                    raise RuntimeError("Unexpected isolated device collection")
                png = root / "pair.png"
                pairing = json.loads(run(cli, "pair", "--name", "CI offline wheel runtime", "--ttl", "300", "--qr", str(png), environment=environment, cwd=root).stdout)
                if png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("Actual wheel CLI did not generate a PNG")
                resource = request(url + "/v1/pairings/" + pairing["id"], token)
                if resource.get("status") != "pending":
                    raise RuntimeError("Pairing was not persisted by the actual server")
                run(cli, "install-skill", environment=environment, cwd=root)
                installed = root / "hermes-test-home/skills/phonebridge/SKILL.md"
                if not installed.is_file():
                    raise RuntimeError("Actual wheel did not include/install its skill")
                expected = Path(__file__).resolve().parents[1] / "skills/phonebridge/SKILL.md"
                if installed.read_bytes() != expected.read_bytes():
                    raise RuntimeError("Bundled wheel skill differs from canonical source")
                print(json.dumps({"offline_wheel_install": True, "health": health, "cli_devices": True, "cli_persisted_pairing_png": True, "bundled_skill_install": True, "public_origin": "CI-only reserved example fixture, not production HTTPS"}))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", required=True)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    smoke(arguments.wheelhouse, arguments.revision)


if __name__ == "__main__":
    main()
