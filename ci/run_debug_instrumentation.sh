set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ]
{ [ "${GITHUB_EVENT_NAME:-}" = push ] || [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; }
[ "${GITHUB_RUN_ATTEMPT:-}" = 1 ]
umask 077
serial=
adb_coordinator_pid=
private_log="$RUNNER_TEMP/debug-control-gradle-private.log"
cleanup() {
  primary_status=$?
  trap - EXIT INT TERM
  set +e
  cleanup_status=0
  if [ -n "$adb_coordinator_pid" ]; then
    kill -TERM "$adb_coordinator_pid" 2>/dev/null || :
    for ((attempt=0; attempt<150; attempt++)); do
      kill -0 "$adb_coordinator_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$adb_coordinator_pid" 2>/dev/null || :
    wait "$adb_coordinator_pid" || cleanup_status=1
  fi
  if [ -n "$serial" ]; then
    timeout 20s adb -s "$serial" shell am force-stop org.nous.phonebridge >> "$private_log" 2>&1 || cleanup_status=1
    timeout 20s adb -s "$serial" shell pm clear com.google.android.deskclock >> "$private_log" 2>&1 || cleanup_status=1
    timeout 20s adb -s "$serial" uninstall org.nous.phonebridge.debugfixture >> "$private_log" 2>&1 || cleanup_status=1
    if timeout 20s adb -s "$serial" shell pm list packages org.nous.phonebridge.debugfixture 2>/dev/null | python3 -c 'import sys; raise SystemExit("package:org.nous.phonebridge.debugfixture" in sys.stdin.read().splitlines())'; then
      :
    else
      cleanup_status=1
    fi
  fi
  timeout 40s python3 ci/prepare_emulator_server.py stop >> "$private_log" 2>&1 || cleanup_status=1
  DEBUG_CONTROL_PRIMARY_STATUS="$primary_status" DEBUG_CONTROL_CLEANUP_STATUS="$cleanup_status" timeout --signal=TERM --kill-after=5s 30s python3 ci/debug_evidence.py
  evidence_status=$?
  rm -f -- "$private_log"
  if [ "$primary_status" -eq 0 ]; then
    if [ "$cleanup_status" -ne 0 ]; then primary_status=$cleanup_status; else primary_status=$evidence_status; fi
  fi
  exit "$primary_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
: "${PHONEBRIDGE_CI_PUBLIC_URL:?Automatic CI must supply public HTTPS}"
: "${PHONEBRIDGE_CI_ADMIN_TOKEN:?Automatic CI must supply synthetic token}"
: "${PHONEBRIDGE_CI_ADMIN_TOKEN_FILE:?Automatic CI must supply private token file}"
IFS= read -r admin_token < "$PHONEBRIDGE_CI_ADMIN_TOKEN_FILE"
[ "$admin_token" = "$PHONEBRIDGE_CI_ADMIN_TOKEN" ]
[[ "$admin_token" =~ ^[A-Za-z0-9_-]{64}$ ]]
[[ "$PHONEBRIDGE_CI_PUBLIC_URL" =~ ^https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com$ ]]
serial=$(timeout 20s adb devices | python3 -c 'import re,sys; devices=[line.split()[0] for line in sys.stdin.read().splitlines()[1:] if len(line.split()) == 2 and line.split()[1] == "device"]; assert len(devices) == 1 and re.fullmatch(r"emulator-[0-9]+", devices[0]); print(devices[0])')
[ "$(timeout 20s adb -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r\n')" = 1 ]
[ "$(timeout 20s adb -s "$serial" shell getprop ro.product.cpu.abi | tr -d '\r\n')" = x86_64 ]
case "$(timeout 20s adb -s "$serial" shell getprop ro.build.version.sdk | tr -d '\r\n')" in 31|35) ;; *) exit 1 ;; esac
export ANDROID_SERIAL="$serial"
python3 - <<'PY'
from pathlib import Path
import shutil

root = Path("android/app/build/outputs/androidTest-results/connected")
if root.exists():
    shutil.rmtree(root)
PY
timeout 60s adb -s "$serial" install -r android/debugfixture/build/outputs/apk/debug/debugfixture-debug.apk > "$private_log" 2>&1
timeout 60s adb -s "$serial" install -r "$RUNNER_TEMP/clock-fixture/google-8.2.apk" >> "$private_log" 2>&1
python3 - <<'ADB_COORDINATOR' >> "$private_log" 2>&1 &
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

serial = os.environ["ANDROID_SERIAL"]
if re.fullmatch(r"emulator-[0-9]+", serial) is None:
    raise RuntimeError("Only the verified disposable emulator is allowed")
package = "org.nous.phonebridge"
clock = "com.google.android.deskclock"
receipt_path = Path(os.environ["RUNNER_TEMP"]) / "debug-wholeflow-host.json"
receipt = {"phases": [], "host_adb_taps": 0, "coordinator_error": False}
allowed_phases = {
    "clock_ready", "baseline_listed", "target_created", "survivor_created", "created_restarted",
    "adb_requested", "adb_observed", "adb_restarted", "enabled_restarted", "disabled_restarted",
    "reenabled_restarted", "debug_detail", "debug_deleted", "deleted_restarted", "completed", "failed",
    "promotion_refused", "promotion_dismissed",
}

stopping = False


def adb(*arguments, payload=None, required=True):
    if stopping:
        raise SystemExit(0)
    result = subprocess.run(["adb", "-s", serial, *arguments], input=payload, capture_output=True, timeout=10)
    if required and result.returncode != 0:
        raise RuntimeError("Bounded host ADB command failed")
    return result


def publish():
    pending = receipt_path.with_suffix(".pending")
    pending.write_text(json.dumps(receipt))
    pending.chmod(0o600)
    pending.replace(receipt_path)


def read_private(name):
    result = adb("shell", "-T", "run-as", package, "head", "-c", "4097", "files/debug-wholeflow/" + name, required=False)
    if result.returncode != 0:
        return None
    if len(result.stdout) > 4096:
        raise RuntimeError("App-private handshake exceeds bound")
    return json.loads(result.stdout)


def write_ack(name, value):
    commands = {
        "ack.json": "cat > files/debug-wholeflow/ack.pending && mv files/debug-wholeflow/ack.pending files/debug-wholeflow/ack.json",
        "progress-ack.json": "cat > files/debug-wholeflow/progress-ack.pending && mv files/debug-wholeflow/progress-ack.pending files/debug-wholeflow/progress-ack.json",
    }
    adb("exec-out", "run-as", package, "sh", "-c", "'" + commands[name] + "'", payload=json.dumps(value).encode())


def stop(signum, frame):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
publish()
consumed = None
deadline = time.monotonic() + 18 * 60
try:
    while not stopping and time.monotonic() < deadline:
        progress = read_private("progress.json")
        if progress is not None:
            if set(progress) != {"phases"} or not isinstance(progress["phases"], list) or not 1 <= len(progress["phases"]) <= 32 or any(not isinstance(phase, str) or phase not in allowed_phases for phase in progress["phases"]):
                raise RuntimeError("Invalid restricted native-flow progress")
            phases = progress["phases"]
            if phases[:len(receipt["phases"])] != receipt["phases"]:
                raise RuntimeError("Native-flow progress cannot rewrite observations")
            if phases != receipt["phases"]:
                receipt["phases"] = phases
                publish()
                write_ack("progress-ack.json", {"observed_phases": len(phases)})
        request = read_private("request.json")
        if request is not None:
            if consumed is not None:
                if request != consumed:
                    raise RuntimeError("Only one immutable host ADB tap request is allowed")
            else:
                if set(request) != {"version", "nonce", "debug_session", "expected_package", "x", "y", "expires_elapsed_ms"}:
                    raise RuntimeError("Host ADB request schema mismatch")
                if type(request["version"]) is not int or request["version"] != 1 or request["expected_package"] != clock:
                    raise RuntimeError("Host ADB target is not allowlisted")
                if any(not isinstance(request[key], str) or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request[key]) is None for key in ("nonce", "debug_session")):
                    raise RuntimeError("Host ADB nonce/session binding is invalid")
                if any(type(request[key]) is not int for key in ("x", "y", "expires_elapsed_ms")):
                    raise RuntimeError("Host ADB coordinates/deadline must be integers")
                if not receipt["phases"] or receipt["phases"][-1] != "adb_requested":
                    raise RuntimeError("Host ADB tap requested outside its native test phase")
                size = adb("shell", "wm", "size").stdout.decode()
                rectangles = re.findall(r"(?:Physical|Override) size: ([0-9]+)x([0-9]+)", size)
                if not rectangles:
                    raise RuntimeError("Host ADB display bounds unavailable")
                width, height = map(int, rectangles[-1])
                if not (0 <= request["x"] < width <= 4096 and 0 <= request["y"] < height <= 4096):
                    raise RuntimeError("Host ADB coordinates exceed actual display")
                uptime = float(adb("shell", "cat", "/proc/uptime").stdout.split()[0]) * 1000
                if not uptime < request["expires_elapsed_ms"] <= uptime + 20000:
                    raise RuntimeError("Host ADB tap request expired")
                focus = adb("shell", "dumpsys", "window", "windows").stdout.decode()
                if re.search(r"mCurrentFocus=Window\{[^\n]*\scom\.google\.android\.deskclock/[^\n]*\}", focus) is None:
                    raise RuntimeError("Genuine Clock is not the host-observed focused window")
                if read_private("request.json") != request:
                    raise RuntimeError("Host ADB request changed before dispatch")
                consumed = request
                adb("shell", "input", "tap", str(request["x"]), str(request["y"]))
                receipt["host_adb_taps"] = 1
                publish()
                write_ack("ack.json", {"nonce": request["nonce"], "debug_session": request["debug_session"], "dispatched": True, "transport": "host_adb_shell_input"})
        time.sleep(0.3)
    if not stopping:
        raise RuntimeError("Host ADB coordinator deadline exceeded")
except Exception:
    receipt["coordinator_error"] = True
    publish()
    raise
finally:
    publish()
ADB_COORDINATOR
adb_coordinator_pid=$!
status=0
(
  cd android
  timeout --signal=TERM --kill-after=30s 18m ./gradlew --no-daemon --console=plain :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.class=org.nous.phonebridge.DebugControlInstrumentationTest \
    -Pandroid.testInstrumentationRunnerArguments.automaticDebugCi=true \
    -Pandroid.testInstrumentationRunnerArguments.genuineClockWholeFlow=true \
    "-Pandroid.testInstrumentationRunnerArguments.ciRunAttempt=$GITHUB_RUN_ATTEMPT" \
    -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
    "-Pandroid.testInstrumentationRunnerArguments.expectedPublicServerUrl=$PHONEBRIDGE_CI_PUBLIC_URL" \
    "-Pandroid.testInstrumentationRunnerArguments.adminToken=$admin_token"
) >> "$private_log" 2>&1 || status=$?
exit "$status"
