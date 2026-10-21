set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ]
{ [ "${GITHUB_EVENT_NAME:-}" = push ] || [ "${GITHUB_EVENT_NAME:-}" = pull_request ]; }
[ "${GITHUB_RUN_ATTEMPT:-}" = 1 ]
umask 077
serial=
private_log="$RUNNER_TEMP/debug-control-gradle-private.log"
cleanup() {
  primary_status=$?
  trap - EXIT INT TERM
  set +e
  cleanup_status=0
  if [ -n "$serial" ]; then
    timeout 20s adb -s "$serial" shell am force-stop org.nous.phonebridge >> "$private_log" 2>&1 || cleanup_status=1
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
export ANDROID_SERIAL="$serial"
python3 - <<'PY'
from pathlib import Path
import shutil

root = Path("android/app/build/outputs/androidTest-results/connected")
if root.exists():
    shutil.rmtree(root)
PY
timeout 60s adb -s "$serial" install -r android/debugfixture/build/outputs/apk/debug/debugfixture-debug.apk > "$private_log" 2>&1
status=0
(
  cd android
  timeout --signal=TERM --kill-after=30s 18m ./gradlew --no-daemon --console=plain :app:connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.class=org.nous.phonebridge.DebugControlInstrumentationTest \
    -Pandroid.testInstrumentationRunnerArguments.automaticDebugCi=true \
    "-Pandroid.testInstrumentationRunnerArguments.ciRunAttempt=$GITHUB_RUN_ATTEMPT" \
    -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
    "-Pandroid.testInstrumentationRunnerArguments.expectedPublicServerUrl=$PHONEBRIDGE_CI_PUBLIC_URL" \
    "-Pandroid.testInstrumentationRunnerArguments.adminToken=$admin_token"
) >> "$private_log" 2>&1 || status=$?
exit "$status"
