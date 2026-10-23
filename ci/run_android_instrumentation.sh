set -euo pipefail
part1=org.nous.phonebridge.AccessibilityInstrumentationTest,org.nous.phonebridge.DurabilityInstrumentationTest,org.nous.phonebridge.QrPipelineInstrumentationTest,org.nous.phonebridge.ScreenControlInstrumentationTest
part2=org.nous.phonebridge.ServerInstrumentationTest,org.nous.phonebridge.ScreenshotTourTest,org.nous.phonebridge.OwnerUiTourTest
case "${PHONEBRIDGE_TEST_PART:-all}" in
  1) classes=$part1 ;;
  2) classes=$part2 ;;
  all) classes=$part1,$part2 ;;
  *) echo "PHONEBRIDGE_TEST_PART must be 1, 2 or all" >&2; exit 2 ;;
esac
: "${PHONEBRIDGE_CI_PUBLIC_URL:?CI must supply the public HTTPS origin}"
: "${PHONEBRIDGE_CI_ADMIN_TOKEN_FILE:?CI must supply the admin token file}"
IFS= read -r admin_token < "$PHONEBRIDGE_CI_ADMIN_TOKEN_FILE"
fixture_pid=
cleanup() {
  if [ -n "$fixture_pid" ]; then
    kill "$fixture_pid" 2>/dev/null || true
    wait "$fixture_pid" 2>/dev/null || true
  fi
  mkdir -p "$RUNNER_TEMP/screenshots"
  adb pull /sdcard/Download/phonebridge-tour "$RUNNER_TEMP/screenshots" >/dev/null 2>&1 || true
  adb pull /sdcard/Download/phonebridge-redesign "$RUNNER_TEMP/screenshots" >/dev/null 2>&1 || true
  adb shell am force-stop org.nous.phonebridge >/dev/null 2>&1 || true
  adb uninstall org.nous.phonebridge.debugfixture >/dev/null 2>&1 || true
  python3 ci/prepare_emulator_server.py stop
}
trap cleanup EXIT
settle_emulator() {
  adb shell settings put global hide_error_dialogs 1 || true
  adb shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS >/dev/null 2>&1 || true
  adb shell input keyevent KEYCODE_WAKEUP || true
  adb shell wm dismiss-keyguard || true
  adb shell input keyevent KEYCODE_HOME || true
}
settle_emulator
if [ -n "${RUNNER_TEMP:-}" ] && [ -e "$RUNNER_TEMP/gradle-build.log" ]; then
  waited=0
  until [ -s "$RUNNER_TEMP/gradle-build.status" ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt 1500 ]; then tail -n 200 "$RUNNER_TEMP/gradle-build.log"; exit 1; fi
    sleep 1
  done
  if [ "$(cat "$RUNNER_TEMP/gradle-build.status")" != 0 ]; then
    tail -n 200 "$RUNNER_TEMP/gradle-build.log"
    exit 1
  fi
else
  (
    cd android
    chmod +x gradlew
    ./gradlew assembleDebug assembleDebugAndroidTest :debugfixture:assembleDebug
  )
fi
adb install -r android/debugfixture/build/outputs/apk/debug/debugfixture-debug.apk
curl --fail --silent --show-error --max-time 20 "$PHONEBRIDGE_CI_PUBLIC_URL/healthz"
umask 077
mkdir -p "$RUNNER_TEMP/qr-fixture"
PHONEBRIDGE_URL=http://127.0.0.1:18940 PHONEBRIDGE_TOKEN="$admin_token" \
  phonebridge pair --name QR-CI --ttl 900 --qr "$RUNNER_TEMP/qr-fixture/pairing.png" > "$RUNNER_TEMP/qr-fixture/pairing.json"
pairing_code=$(PHONEBRIDGE_URL=http://127.0.0.1:18940 PHONEBRIDGE_TOKEN="$admin_token" phonebridge pair --name Tour-CI --ttl 900 --code | python3 -c 'import json,sys; print(json.load(sys.stdin)["pairing_code"])')
python3 -m http.server 18941 --protocol HTTP/1.1 --bind 127.0.0.1 --directory "$RUNNER_TEMP/qr-fixture" > "$RUNNER_TEMP/qr-fixture-http.log" 2>&1 &
fixture_pid=$!
settle_emulator
(
cd android
./gradlew :app:connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.serverUrl=http://10.0.2.2:18940 \
  "-Pandroid.testInstrumentationRunnerArguments.expectedPublicServerUrl=$PHONEBRIDGE_CI_PUBLIC_URL" \
  "-Pandroid.testInstrumentationRunnerArguments.adminToken=$admin_token" \
  -Pandroid.testInstrumentationRunnerArguments.emulatorCi=true \
  "-Pandroid.testInstrumentationRunnerArguments.class=$classes" \
  -Pandroid.testInstrumentationRunnerArguments.screenshotTour=true \
  -Pandroid.testInstrumentationRunnerArguments.ownerUiTour=true \
  "-Pandroid.testInstrumentationRunnerArguments.pairingCode=$pairing_code" \
  -Pandroid.testInstrumentationRunnerArguments.qrFixture=true \
  -Pandroid.testInstrumentationRunnerArguments.qrFixtureUrl=http://10.0.2.2:18941/pairing.png
)
