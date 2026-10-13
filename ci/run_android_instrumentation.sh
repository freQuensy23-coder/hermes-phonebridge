set -euo pipefail
adb install -r "$RUNNER_TEMP/clock-fixture/google-8.2.apk"
(
  cd android
  chmod +x gradlew
  ./gradlew --no-daemon assembleDebug assembleDebugAndroidTest
)
umask 077
mkdir "$RUNNER_TEMP/qr-fixture"
PHONEBRIDGE_URL=http://127.0.0.1:18940 PHONEBRIDGE_TOKEN=ci-phonebridge-test-admin-token-000000000000000000 \
  phonebridge pair --name QR-CI --ttl 900 --qr "$RUNNER_TEMP/qr-fixture/pairing.png" > "$RUNNER_TEMP/qr-fixture/pairing.json"
python3 -m http.server 18941 --bind 0.0.0.0 --directory "$RUNNER_TEMP/qr-fixture" > "$RUNNER_TEMP/qr-fixture-http.log" 2>&1 &
fixture_pid=$!
trap 'kill "$fixture_pid" 2>/dev/null || true; wait "$fixture_pid" 2>/dev/null || true' EXIT
status=0
(
  cd android
  ./gradlew --no-daemon connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
    -Pandroid.testInstrumentationRunnerArguments.adminToken=ci-phonebridge-test-admin-token-000000000000000000 \
    -Pandroid.testInstrumentationRunnerArguments.googleClockFixture=true \
    -Pandroid.testInstrumentationRunnerArguments.qrFixture=true \
    -Pandroid.testInstrumentationRunnerArguments.qrFixtureUrl=http://10.0.2.2:18941/pairing.png
) || status=$?
mkdir -p "$RUNNER_TEMP/google-clock-evidence"
adb exec-out run-as org.nous.phonebridge tar -cf - -C files google-clock-ci > "$RUNNER_TEMP/google-clock-evidence/instrumentation-frames.tar" 2> "$RUNNER_TEMP/google-clock-evidence/export-status.log" || true
exit "$status"
