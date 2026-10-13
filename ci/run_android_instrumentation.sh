set -euo pipefail
adb install -r "$RUNNER_TEMP/clock-fixture/google-8.2.apk"
status=0
(
  cd android
  chmod +x gradlew
  ./gradlew --no-daemon connectedDebugAndroidTest \
    -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
    -Pandroid.testInstrumentationRunnerArguments.adminToken=ci-phonebridge-test-admin-token-000000000000000000 \
    -Pandroid.testInstrumentationRunnerArguments.googleClockFixture=true
) || status=$?
mkdir -p "$RUNNER_TEMP/google-clock-evidence"
adb exec-out run-as org.nous.phonebridge tar -cf - -C files google-clock-ci > "$RUNNER_TEMP/google-clock-evidence/instrumentation-frames.tar" 2> "$RUNNER_TEMP/google-clock-evidence/export-status.log" || true
exit "$status"
