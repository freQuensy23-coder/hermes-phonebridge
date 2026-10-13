set -euo pipefail
[ "${GITHUB_ACTIONS:-}" = true ]
: "${PHONEBRIDGE_CI_PUBLIC_URL:?Automatic CI must supply the validated public HTTPS origin}"
: "${PHONEBRIDGE_CI_ADMIN_TOKEN:?Automatic CI must supply the synthetic admin token}"
: "${PHONEBRIDGE_CI_ADMIN_TOKEN_FILE:?Automatic CI must supply the private token file}"
IFS= read -r admin_token < "$PHONEBRIDGE_CI_ADMIN_TOKEN_FILE"
[ "$admin_token" = "$PHONEBRIDGE_CI_ADMIN_TOKEN" ]
[ "${#admin_token}" -ge 32 ]
fixture_pid=
cleanup() {
  if [ -n "$fixture_pid" ]; then
    kill "$fixture_pid" 2>/dev/null || true
    wait "$fixture_pid" 2>/dev/null || true
  fi
  python3 ci/prepare_emulator_server.py stop
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
adb install -r "$RUNNER_TEMP/clock-fixture/google-8.2.apk"
(
  cd android
  chmod +x gradlew
  ./gradlew --no-daemon --refresh-dependencies assembleDebug assembleDebugAndroidTest
)
curl --fail --silent --show-error --max-time 20 "$PHONEBRIDGE_CI_PUBLIC_URL/healthz"
umask 077
mkdir "$RUNNER_TEMP/qr-fixture"
PHONEBRIDGE_URL=http://127.0.0.1:18940 PHONEBRIDGE_TOKEN="$admin_token" \
  phonebridge pair --name QR-CI --ttl 900 --qr "$RUNNER_TEMP/qr-fixture/pairing.png" > "$RUNNER_TEMP/qr-fixture/pairing.json"
python3 -m http.server 18941 --protocol HTTP/1.1 --bind 127.0.0.1 --directory "$RUNNER_TEMP/qr-fixture" > "$RUNNER_TEMP/qr-fixture-http.log" 2>&1 &
fixture_pid=$!
status=0
(
  cd android
  ./gradlew --no-daemon connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
    "-Pandroid.testInstrumentationRunnerArguments.expectedPublicServerUrl=$PHONEBRIDGE_CI_PUBLIC_URL" \
    "-Pandroid.testInstrumentationRunnerArguments.adminToken=$admin_token" \
    -Pandroid.testInstrumentationRunnerArguments.googleClockFixture=true \
    -Pandroid.testInstrumentationRunnerArguments.class=org.nous.phonebridge.AccessibilityInstrumentationTest,org.nous.phonebridge.ClipboardInstrumentationTest,org.nous.phonebridge.ClockIntentInstrumentationTest,org.nous.phonebridge.DndInstrumentationTest,org.nous.phonebridge.DurabilityInstrumentationTest,org.nous.phonebridge.GoogleClockInstrumentationTest,org.nous.phonebridge.QrPipelineInstrumentationTest,org.nous.phonebridge.SamsungClockUnsupportedInstrumentationTest,org.nous.phonebridge.ServerInstrumentationTest \
    -Pandroid.testInstrumentationRunnerArguments.qrFixture=true \
    -Pandroid.testInstrumentationRunnerArguments.qrFixtureUrl=http://10.0.2.2:18941/pairing.png
) || status=$?
mkdir -p "$RUNNER_TEMP/google-clock-evidence"
adb exec-out run-as org.nous.phonebridge tar -cf - -C files google-clock-ci > "$RUNNER_TEMP/google-clock-evidence/instrumentation-frames.tar" 2> "$RUNNER_TEMP/google-clock-evidence/export-status.log" || true
mkdir -p "$RUNNER_TEMP/foreground-evidence"
adb exec-out run-as org.nous.phonebridge tar -cf - -C files foreground-ci > "$RUNNER_TEMP/foreground-evidence/instrumentation-frames.tar" 2> "$RUNNER_TEMP/foreground-evidence/export-status.log" || true
exit "$status"
