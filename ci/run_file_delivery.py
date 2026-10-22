import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "org.nous.phonebridge"
CLASS = PACKAGE + ".FileDeliveryInstrumentationTest"
METHOD = "installedCliUploadsReachNativeMediaStoreWithoutAccessibilityOrDebug"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PUBLIC_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com")
CASE_IDS = ("binary", "image_flag", "image_alias", "empty", "same_a", "same_b")
FIELDS = {"uri", "name", "size", "sha256", "mime", "kind"}
REASONS = {
    "CI_ONLY", "CONFIG", "EMULATOR", "ADB", "DEADLINE", "GRADLE", "CLI", "CLI_RESULT",
    "HANDSHAKE", "READBACK", "READINESS", "HTTP", "BOUNDS", "INTERRUPTED", "INTERNAL",
    "CLEANUP", "REPORTS", "PYTHON_TESTS", "NOT_STARTED", "PASSED",
}
NATIVE_REASONS = {
    "FD_CLEANUP", "FD_DEADLINE", "FD_ACCESSIBILITY", "FD_DEBUG", "FD_HTTP", "FD_HTTP_BOUND",
    "FD_HANDSHAKE_BOUND", "FD_HANDSHAKE_WRITE", "FD_RECEIPT", "FD_URI", "FD_MEDIASTORE",
    "FD_NAME", "FD_SIZE", "FD_MIME", "FD_PATH", "FD_PENDING", "FD_OWNER", "FD_BYTES_BOUND",
    "FD_BYTES", "FD_SHA256", "FD_BITMAP", "FD_CI", "FD_NONCE", "FD_SERVER", "FD_TOKEN",
    "FD_STALE", "FD_PAIRING", "FD_WSS", "FD_CREDENTIALS", "FD_COMMAND", "FD_REQUEST",
    "FD_RESULT", "FD_OVERWRITE", "FD_COMPLETE", "FD_HOST",
}


class Failure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in REASONS else "INTERNAL"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, code):
    if not condition:
        raise Failure(code)


def bounded_json(payload, maximum=8192):
    require(0 < len(payload) <= maximum, "BOUNDS")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "HANDSHAKE")
                result[key] = value
            return result
        value = json.loads(payload, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise Failure("HANDSHAKE") from None
    require(type(value) is dict, "HANDSHAKE")
    return value


def private_write(path, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    require(len(payload) <= 65536, "BOUNDS")
    pending = path.with_suffix(".pending")
    pending.write_bytes(payload)
    pending.chmod(0o600)
    pending.replace(path)


def terminate(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def execute(command, private, timeout, payload=None, maximum=65536):
    process = None
    with tempfile.TemporaryFile(dir=private) as output, tempfile.TemporaryFile(dir=private) as errors:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                       stdout=output, stderr=errors, start_new_session=True)
            process.communicate(input=payload, timeout=timeout)
            require(output.tell() <= maximum and errors.tell() <= maximum, "BOUNDS")
            output.seek(0)
            errors.seek(0)
            return process.returncode, output.read(maximum + 1), errors.read(maximum + 1)
        finally:
            terminate(process)


def png():
    def chunk(kind, payload):
        return struct.pack("!I", len(payload)) + kind + payload + struct.pack("!I", zlib.crc32(kind + payload))
    scanline = bytes((0, 12, 34, 56, 255))
    stored = bytes((120, 1, 1, 5, 0, 250, 255)) + scanline + struct.pack("!I", zlib.adler32(scanline))
    return (bytes((137, 80, 78, 71, 13, 10, 26, 10))
            + chunk(b"IHDR", struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", stored) + chunk(b"IEND", b""))


def fixtures(nonce):
    prefix = "fd-" + nonce[:8]
    cases = [
        ("binary", prefix + "-binary.bin", "application/octet-stream", "file", bytes((index * 73 + 19) & 255 for index in range(1024 * 1024 + 37))),
        ("image_flag", prefix + "-image-flag.png", "image/png", "image", png()),
        ("image_alias", prefix + "-image-alias.png", "image/png", "image", png()),
        ("empty", prefix + "-empty.bin", "application/octet-stream", "file", b""),
        ("same_a", prefix + "-shared.bin", "application/octet-stream", "file", b"PhoneBridge same name alpha\n"),
        ("same_b", prefix + "-shared.bin", "application/octet-stream", "file", b"PhoneBridge same name beta different\n"),
    ]
    return [{"case_id": identifier, "bytes": payload, "request": {
        "name": name, "mime": mime, "kind": kind, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
    }} for identifier, name, mime, kind, payload in cases]


class Coordinator:
    def __init__(self, private, state):
        self.private = private
        self.state = state
        self.serial = None
        self.verified_emulator = False
        self.gradle = None
        self.device = None
        self.deadline = time.monotonic() + 460
        self.nonce = secrets.token_hex(16)
        self.origin = os.environ.get("PHONEBRIDGE_CI_PUBLIC_URL", "")
        self.token = os.environ.get("PHONEBRIDGE_CI_ADMIN_TOKEN", "")
        self.environment = {key: value for key, value in os.environ.items()
                            if key not in {"PYTHONPATH", "PHONEBRIDGE_URL", "PHONEBRIDGE_TOKEN"}}
        self.environment.update({"PHONEBRIDGE_URL": self.origin, "PHONEBRIDGE_TOKEN": self.token})
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def save(self):
        private_write(self.private / "state.json", self.state)

    def remaining(self, maximum=12):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "DEADLINE")
        return min(maximum, remaining)

    def adb(self, *arguments, payload=None, required=True):
        require(self.serial is not None, "EMULATOR")
        status, stdout, _ = execute(["adb", "-s", self.serial, *arguments], self.private,
                                          self.remaining(), payload=payload)
        if required:
            require(status == 0, "ADB")
        return status, stdout

    def read(self, name):
        require(re.fullmatch(r"(?:device|complete|readback-[0-5])\.json", name) is not None, "HANDSHAKE")
        status, payload = self.adb("shell", "-T", "run-as", PACKAGE, "head", "-c", "8193", "files/file-delivery/" + name, required=False)
        if status != 0:
            return None
        return bounded_json(payload)

    def write(self, name, value):
        require(re.fullmatch(r"(?:receipt-[0-5]|host-complete|abort)\.json", name) is not None, "HANDSHAKE")
        payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        require(len(payload) <= 8192, "BOUNDS")
        command = "cat > files/file-delivery/" + name + ".pending && mv files/file-delivery/" + name + ".pending files/file-delivery/" + name
        self.adb("shell", "-T", "run-as", PACKAGE, "sh", "-c", "'" + command + "'", payload=payload)

    def poll(self, predicate):
        while True:
            self.remaining()
            require(self.gradle is None or self.gradle.poll() is None, "GRADLE")
            value = predicate()
            if value is not None and value is not False:
                return value
            time.sleep(min(0.2, self.remaining()))

    def get(self, path, expected=200):
        require(path.startswith("/v1/"), "HTTP")
        request = urllib.request.Request(self.origin + path, headers={"Authorization": "Bearer " + self.token})
        try:
            with self.opener.open(request, timeout=self.remaining(10)) as response:
                require(response.status == expected and response.url == self.origin + path, "HTTP")
                return bounded_json(response.read(65537), 65536)
        except urllib.error.HTTPError as error:
            if error.code == expected:
                return {}
            raise Failure("HTTP") from None
        except (OSError, urllib.error.URLError):
            raise Failure("HTTP") from None

    def discover(self):
        status, output, _ = execute(["adb", "devices"], self.private, self.remaining())
        require(status == 0, "EMULATOR")
        devices = [line.split()[0] for line in output.decode("ascii").splitlines()[1:]
                   if len(line.split()) == 2 and line.split()[1] == "device"]
        require(len(devices) == 1 and re.fullmatch(r"emulator-[0-9]+", devices[0]) is not None, "EMULATOR")
        self.serial = devices[0]
        for key, expected in (("ro.kernel.qemu", "1"), ("ro.product.cpu.abi", "x86_64"), ("sys.boot_completed", "1")):
            require(self.adb("shell", "getprop", key)[1].decode().strip() == expected, "EMULATOR")
        api = self.adb("shell", "getprop", "ro.build.version.sdk")[1].decode().strip()
        require(api in {"31", "35"} and api == os.environ.get("FILE_DELIVERY_API"), "EMULATOR")
        self.state["api"] = int(api)
        self.save()
        for package in (PACKAGE, PACKAGE + ".test"):
            packages = self.adb("shell", "pm", "list", "packages", package)[1].decode().splitlines()
            require("package:" + package not in packages, "EMULATOR")
        require(self.adb("shell", "settings", "get", "secure", "accessibility_enabled")[1].decode().strip() in {"0", "null"}, "EMULATOR")
        self.verified_emulator = True

    def start_gradle(self):
        reports = ROOT / "android/app/build/outputs/androidTest-results/connected"
        if reports.exists():
            shutil.rmtree(reports)
        command = [str(ROOT / "android/gradlew"), "--no-daemon", "--console=plain", ":app:connectedDebugAndroidTest",
                   "-Pandroid.testInstrumentationRunnerArguments.class=" + CLASS,
                   "-Pandroid.testInstrumentationRunnerArguments.automaticFileDeliveryCi=true",
                   "-Pandroid.testInstrumentationRunnerArguments.ciRunAttempt=1",
                   "-Pandroid.testInstrumentationRunnerArguments.fileDeliveryNonce=" + self.nonce,
                   "-Pandroid.testInstrumentationRunnerArguments.expectedPublicServerUrl=" + self.origin,
                   "-Pandroid.testInstrumentationRunnerArguments.adminToken=" + self.token]
        environment = self.environment | {"ANDROID_SERIAL": self.serial}
        with (self.private / "gradle.log").open("wb") as output:
            self.gradle = subprocess.Popen(command, cwd=ROOT / "android", env=environment, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

    def handshake(self):
        value = self.read("device.json")
        if value is None:
            return None
        require(set(value) == {"version", "nonce", "device_id"}, "HANDSHAKE")
        require(type(value["version"]) is int and value["version"] == 1 and value["nonce"] == self.nonce, "HANDSHAKE")
        require(isinstance(value["device_id"], str) and UUID.fullmatch(value["device_id"]) is not None, "HANDSHAKE")
        return value

    def ready(self):
        device = self.get("/v1/devices/" + self.device)
        require(device.get("id") == self.device, "READINESS")
        capabilities = device.get("capabilities")
        permissions = device.get("permissions")
        if device.get("online") is not True or not isinstance(capabilities, list) or not isinstance(permissions, dict):
            return False
        require(permissions.get("accessibility") is False, "READINESS")
        require(not any(isinstance(op, str) and op.startswith("screen.") for op in capabilities), "READINESS")
        return "files.receive" in capabilities and permissions.get("foreground") is False

    def cli(self, case):
        identifier = case["case_id"]
        filepath = self.private / (identifier + (".png" if case["request"]["kind"] == "image" else ".bin"))
        filepath.write_bytes(case["bytes"])
        filepath.chmod(0o600)
        executable = shutil.which("phonebridge")
        require(executable is not None, "CONFIG")
        group = "images" if identifier == "image_alias" else "files"
        command = [executable, "--request-timeout", "30", group, "send", self.device, str(filepath),
                   "--name", case["request"]["name"], "--mime", case["request"]["mime"]]
        if identifier == "image_flag":
            command.append("--image")
        command.extend(["--wait", "--timeout", "120", "--ttl", "300"])
        process = None
        with tempfile.TemporaryFile(dir=self.private) as output, tempfile.TemporaryFile(dir=self.private) as errors:
            try:
                process = subprocess.Popen(command, cwd=self.private, env=self.environment, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=errors, start_new_session=True)
                end = time.monotonic() + self.remaining(130)
                while process.poll() is None:
                    require(time.monotonic() < end, "DEADLINE")
                    require(self.gradle.poll() is None, "GRADLE")
                    require(output.tell() <= 65536 and errors.tell() <= 65536, "BOUNDS")
                    time.sleep(min(0.2, self.remaining()))
                require(process.returncode == 0, "CLI")
                require(output.tell() <= 65536 and errors.tell() == 0, "CLI_RESULT")
                output.seek(0)
                value = bounded_json(output.read(65537), 65536)
            finally:
                terminate(process)
        require(value.get("status") == "succeeded" and value.get("op") == "files.receive" and value.get("device_id") == self.device, "CLI_RESULT")
        require(isinstance(value.get("id"), str) and UUID.fullmatch(value["id"]) is not None, "CLI_RESULT")
        data = value.get("data")
        require(isinstance(data, dict) and FIELDS <= set(data), "CLI_RESULT")
        data = {key: data[key] for key in sorted(FIELDS)}
        require(type(data["size"]) is int and data["size"] == case["request"]["size"], "CLI_RESULT")
        for key in ("sha256", "mime", "kind"):
            require(data[key] == case["request"][key], "CLI_RESULT")
        name = data["name"]
        require(isinstance(name, str) and 0 < len(name) <= 240 and name not in {".", ".."}
                and not any(ord(char) < 32 or char in "/\\" for char in name), "CLI_RESULT")
        if identifier != "same_b":
            require(name == case["request"]["name"], "CLI_RESULT")
        require(isinstance(data["uri"], str) and len(data["uri"]) <= 512, "CLI_RESULT")
        uri = urllib.parse.urlsplit(data["uri"])
        collection = r"images/media" if case["request"]["kind"] == "image" else r"downloads"
        require(uri.scheme == "content" and uri.netloc == "media" and not uri.query and not uri.fragment
                and re.fullmatch(r"/(?:external|external_primary)/" + collection + r"/[0-9]+", uri.path) is not None, "CLI_RESULT")
        observed = self.get("/v1/commands/" + value["id"])
        require(all(observed.get(key) == value.get(key) for key in ("id", "device_id", "op", "status")), "CLI_RESULT")
        require(isinstance(observed.get("data"), dict) and all(observed["data"].get(key) == data[key] for key in FIELDS), "CLI_RESULT")
        return value["id"], data

    def readback(self, index, case, command_id, data):
        value = self.read("readback-" + str(index) + ".json")
        if value is None:
            return None
        expected_keys = FIELDS | {"version", "nonce", "device_id", "index", "case_id", "command_id",
                                  "relative_path", "is_pending", "accessibility_enabled", "debug_enabled"}
        require(set(value) == expected_keys, "READBACK")
        require(type(value["version"]) is int and value["version"] == 1 and value["nonce"] == self.nonce
                and value["device_id"] == self.device and type(value["index"]) is int and value["index"] == index
                and value["case_id"] == case["case_id"] and value["command_id"] == command_id, "READBACK")
        require(all(value[key] == data[key] for key in FIELDS), "READBACK")
        require(type(value["size"]) is int and type(value["is_pending"]) is int and value["is_pending"] == 0
                and value["accessibility_enabled"] is False and value["debug_enabled"] is False, "READBACK")
        expected_path = ("Pictures" if case["request"]["kind"] == "image" else "Download") + "/PhoneBridge/"
        require(value["relative_path"] == expected_path, "READBACK")
        return {key: value[key] for key in ("case_id", "size", "sha256", "mime", "kind", "relative_path", "is_pending", "accessibility_enabled", "debug_enabled")}

    def run(self):
        require(PUBLIC_ORIGIN.fullmatch(self.origin) is not None and re.fullmatch(r"[A-Za-z0-9_-]{64}", self.token) is not None, "CONFIG")
        token_path = Path(os.environ.get("PHONEBRIDGE_CI_ADMIN_TOKEN_FILE", ""))
        require(token_path.is_file() and token_path.read_text().strip() == self.token, "CONFIG")
        self.discover()
        self.start_gradle()
        handshake = self.poll(self.handshake)
        self.device = handshake["device_id"]
        self.poll(self.ready)
        uris = set()
        commands = set()
        cases = fixtures(self.nonce)
        for index, case in enumerate(cases):
            require(self.handshake() == handshake, "HANDSHAKE")
            require(self.ready(), "READINESS")
            command_id, data = self.cli(case)
            require(command_id not in commands and data["uri"] not in uris, "CLI_RESULT")
            commands.add(command_id)
            uris.add(data["uri"])
            self.state["host_cli_completed"].append(case["case_id"])
            self.save()
            receipt = {"version": 1, "nonce": self.nonce, "device_id": self.device, "index": index,
                       "case_id": case["case_id"], "command_id": command_id, "request": case["request"], "data": data}
            self.write("receipt-" + str(index) + ".json", receipt)
            self.state["native_readbacks"].append(self.poll(lambda: self.readback(index, case, command_id, data)))
            self.save()
        complete = self.poll(lambda: self.read("complete.json"))
        require(complete == {"version": 1, "nonce": self.nonce, "device_id": self.device,
                             "verified": len(cases), "uris_distinct": len(uris), "case_ids": list(CASE_IDS)}, "READBACK")
        self.state["native_final_reread_observed"] = True
        self.save()
        self.write("host-complete.json", {"version": 1, "nonce": self.nonce, "device_id": self.device, "verified": len(cases)})
        self.gradle.wait(timeout=self.remaining(30))
        require(self.gradle.returncode == 0, "GRADLE")
        self.state["gradle_exit"] = 0
        self.get("/v1/devices/" + self.device, expected=404)

    def cleanup(self):
        errors = False
        if self.device is not None and self.gradle is not None and self.gradle.poll() is None:
            try:
                self.write("abort.json", {"version": 1, "nonce": self.nonce, "device_id": self.device})
                self.gradle.wait(timeout=15)
            except (Failure, OSError, subprocess.TimeoutExpired):
                pass
        try:
            if self.gradle is not None:
                terminate(self.gradle)
                self.state["gradle_exit"] = self.gradle.returncode
        except (OSError, subprocess.TimeoutExpired):
            errors = True
        self.deadline = time.monotonic() + 60
        if self.verified_emulator:
            for package in (PACKAGE + ".test", PACKAGE):
                try:
                    self.adb("shell", "am", "force-stop", package, required=False)
                    self.adb("uninstall", package, required=False)
                    packages = self.adb("shell", "pm", "list", "packages", package)[1].decode().splitlines()
                    require("package:" + package not in packages, "CLEANUP")
                except (Failure, OSError, subprocess.TimeoutExpired):
                    errors = True
        self.state["cleanup_failed"] = errors
        self.save()


def observed_reports(paths, python=False):
    clean = ET.Element("testsuite", {"name": "Observed file delivery Python tests" if python else "Observed native file delivery"})
    counts = {"observed": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "invalid_reports": 0}
    seen = set()
    require(len(paths) <= 32, "REPORTS")
    for path in paths:
        try:
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024, "REPORTS")
            report = ET.fromstring(path.read_bytes())
            cases = list(report.iter("testcase"))
            require(len(cases) <= 2048, "REPORTS")
            for case in cases:
                classname = case.get("classname", "")
                name = case.get("name", "")
                if python:
                    require(re.fullmatch(r"(?:server|cli)\.tests\.test_file_delivery(?:_cli)?(?:\.[A-Za-z_][A-Za-z_0-9]*)*", classname) is not None
                            or re.fullmatch(r"test_file_delivery(?:_cli)?(?:\.[A-Za-z_][A-Za-z_0-9]*)*", classname) is not None, "REPORTS")
                    base = name.split("[", 1)[0]
                    require(re.fullmatch(r"test_[A-Za-z_0-9]+", base) is not None and len(name) <= 4096, "REPORTS")
                    if name != base:
                        require(name.endswith("]"), "REPORTS")
                        name = base + "[case-" + hashlib.sha256(name.encode()).hexdigest()[:24] + "]"
                else:
                    require(classname == CLASS and name == METHOD, "REPORTS")
                require((classname, name) not in seen, "REPORTS")
                seen.add((classname, name))
                outcomes = [tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None]
                require(len(outcomes) <= 1, "REPORTS")
                row = ET.SubElement(clean, "testcase", {"classname": classname, "name": name})
                counts["observed"] += 1
                if outcomes:
                    outcome = outcomes[0]
                    counts[{"failure": "failed", "error": "errors", "skipped": "skipped"}[outcome]] += 1
                    raw = ET.tostring(case.find(outcome), encoding="unicode")
                    reasons = sorted({match for match in re.findall(r"\bFD_[A-Z_0-9]+\b", raw) if match in NATIVE_REASONS})
                    ET.SubElement(row, outcome, {"message": ",".join(reasons) if reasons else "Observed " + outcome})
                else:
                    counts["passed"] += 1
        except (Failure, OSError, ET.ParseError, ValueError):
            counts["invalid_reports"] += 1
    clean.set("tests", str(counts["observed"]))
    clean.set("failures", str(counts["failed"]))
    clean.set("errors", str(counts["errors"]))
    clean.set("skipped", str(counts["skipped"]))
    return clean, counts


def report(private, python=False, test_exit=None):
    evidence = Path(os.environ["RUNNER_TEMP"]) / ("file-delivery-python-evidence" if python else "file-delivery-evidence")
    evidence.mkdir(mode=0o700, exist_ok=True)
    if python:
        path = private / "python-results.xml"
        paths = [path] if path.exists() else []
    else:
        paths = sorted((ROOT / "android/app/build/outputs/androidTest-results/connected").glob("**/*.xml"))
    clean, counts = observed_reports(paths, python)
    summary = {"junit": counts}
    successful = counts["observed"] > 0 and counts["passed"] == counts["observed"] and not counts["invalid_reports"]
    if python:
        summary["pytest_exit"] = test_exit
        successful = successful and test_exit == 0
    else:
        state_path = private / "state.json"
        state = bounded_json(state_path.read_bytes(), 65536) if state_path.exists() else {}
        reason = state.get("reason", "NOT_STARTED")
        require(reason in REASONS, "REPORTS")
        summary.update({"api": state.get("api"), "reason": reason, "gradle_exit": state.get("gradle_exit"),
                        "cleanup_failed": state.get("cleanup_failed", True),
                        "host_cli_completed": state.get("host_cli_completed", []),
                        "native_readbacks": state.get("native_readbacks", []),
                        "native_final_reread_observed": state.get("native_final_reread_observed", False)})
        require(type(summary["cleanup_failed"]) is bool and type(summary["native_final_reread_observed"]) is bool, "REPORTS")
        require(isinstance(summary["host_cli_completed"], list)
                and summary["host_cli_completed"] == list(CASE_IDS)[:len(summary["host_cli_completed"])], "REPORTS")
        require(isinstance(summary["native_readbacks"], list) and len(summary["native_readbacks"]) <= len(CASE_IDS), "REPORTS")
        for index, proof in enumerate(summary["native_readbacks"]):
            require(set(proof) == {"case_id", "size", "sha256", "mime", "kind", "relative_path", "is_pending", "accessibility_enabled", "debug_enabled"}, "REPORTS")
            require(proof["case_id"] == CASE_IDS[index] and type(proof["size"]) is int and 0 <= proof["size"] <= 2 * 1024 * 1024
                    and isinstance(proof["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", proof["sha256"]) is not None
                    and proof["mime"] in {"image/png", "application/octet-stream"} and proof["kind"] in {"file", "image"}
                    and proof["relative_path"] in {"Download/PhoneBridge/", "Pictures/PhoneBridge/"}
                    and proof["is_pending"] == 0 and proof["accessibility_enabled"] is False and proof["debug_enabled"] is False, "REPORTS")
        require(summary["api"] in {None, 31, 35}, "REPORTS")
        require(summary["gradle_exit"] is None or (type(summary["gradle_exit"]) is int and -128 <= summary["gradle_exit"] <= 255), "REPORTS")
        successful = (successful and counts["observed"] == 1 and reason == "PASSED" and summary["gradle_exit"] == 0
                      and summary["cleanup_failed"] is False and summary["host_cli_completed"] == list(CASE_IDS)
                      and len(summary["native_readbacks"]) == len(CASE_IDS) and summary["native_final_reread_observed"] is True)
    summary["evidence_complete"] = successful
    ET.ElementTree(clean).write(evidence / "observed-junit.xml", encoding="utf-8", xml_declaration=True)
    private_write(evidence / "summary.json", summary)
    rendered = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    require(len(rendered) <= 16384, "BOUNDS")
    print(rendered)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a") as output:
            output.write("\n### File delivery " + ("Python" if python else "native") + " evidence\n```json\n" + rendered + "\n```\n")
    return 0 if successful else 1


def interrupted(signum, frame):
    raise Failure("INTERRUPTED")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "report", "python-tests"))
    arguments = parser.parse_args()
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_EVENT_NAME") in {"push", "pull_request"}
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "CI_ONLY")
    os.umask(0o077)
    private = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / "file-delivery-private"
    private.mkdir(mode=0o700, exist_ok=True)
    if arguments.action == "report":
        try:
            return report(private)
        finally:
            shutil.rmtree(private, ignore_errors=True)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if arguments.action == "python-tests":
        status = None
        try:
            status, _, _ = execute([sys.executable, "-m", "pytest", str(ROOT / "server/tests/test_file_delivery.py"),
                                             str(ROOT / "cli/tests/test_file_delivery_cli.py"), "-x", "-q",
                                             "--junitxml=" + str(private / "python-results.xml")], private, 450, maximum=4 * 1024 * 1024)
        finally:
            try:
                result = report(private, python=True, test_exit=status)
            finally:
                shutil.rmtree(private, ignore_errors=True)
        return result
    state = {"api": None, "reason": "NOT_STARTED", "gradle_exit": None, "cleanup_failed": True,
             "host_cli_completed": [], "native_readbacks": [], "native_final_reread_observed": False}
    coordinator = Coordinator(private, state)
    coordinator.save()
    try:
        coordinator.run()
        state["reason"] = "PASSED"
    except Failure as error:
        state["reason"] = error.code
    except subprocess.TimeoutExpired:
        state["reason"] = "DEADLINE"
    except Exception:
        state["reason"] = "INTERNAL"
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        coordinator.cleanup()
    return 0 if state["reason"] == "PASSED" and not state["cleanup_failed"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as error:
        print(json.dumps({"harness_error": error.code}))
        sys.exit(1)
    except Exception:
        print(json.dumps({"harness_error": "INTERNAL"}))
        sys.exit(1)
