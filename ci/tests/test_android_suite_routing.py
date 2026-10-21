from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
HARDWARE_CLASSES = {"PhoneOemInstrumentationTest", "SamsungClockInstrumentationTest"}
SEPARATELY_ROUTED_CLASSES = {"DebugControlInstrumentationTest"}


def test_emulator_suite_routes_every_nonhardware_nonisolated_instrumentation_class():
    script = (ROOT / "ci/run_android_instrumentation.sh").read_text()
    argument = re.search(r"-Pandroid\.testInstrumentationRunnerArguments\.class=([^\s]+)", script)
    assert argument is not None
    selected = argument.group(1).split(",")
    classes = {path.stem for path in (ROOT / "android/app/src/androidTest/java/org/nous/phonebridge").glob("*Test.kt")}
    assert len(selected) == len(set(selected))
    assert classes >= HARDWARE_CLASSES | SEPARATELY_ROUTED_CLASSES
    assert set(selected) == {"org.nous.phonebridge." + name for name in classes - HARDWARE_CLASSES - SEPARATELY_ROUTED_CLASSES}


def test_debug_suite_routes_only_its_dedicated_automatic_fixture_harness():
    workflow = (ROOT / ".github/workflows/debug-control.yml").read_text()
    script = (ROOT / "ci/run_debug_instrumentation.sh").read_text()
    selections = re.findall(r"-Pandroid\.testInstrumentationRunnerArguments\.class=([^\s]+)", script)
    assert selections == ["org.nous.phonebridge.DebugControlInstrumentationTest"]
    assert re.findall(r"          script: (.+)", workflow) == ["bash ci/run_debug_instrumentation.sh"]
    assert "run_android_instrumentation.sh" not in workflow
    assert "-Pandroid.testInstrumentationRunnerArguments.automaticDebugCi=true" in script
    assert '"-Pandroid.testInstrumentationRunnerArguments.ciRunAttempt=$GITHUB_RUN_ATTEMPT"' in script
    assert '[ "${GITHUB_RUN_ATTEMPT:-}" = 1 ]' in script
    assert '[ "${GITHUB_ACTIONS:-}" = true ]' in script
    assert '[ "${GITHUB_EVENT_NAME:-}" = push ]' in script
    assert '[ "${GITHUB_EVENT_NAME:-}" = pull_request ]' in script
    assert ":debugfixture:assembleDebug" in workflow
    assert 'adb -s "$serial" install -r android/debugfixture/build/outputs/apk/debug/debugfixture-debug.apk' in script
    test = (ROOT / "android/app/src/androidTest/java/org/nous/phonebridge/DebugControlInstrumentationTest.kt").read_text()
    assert 'assertEquals("true", supplied.getString("automaticDebugCi"))' in test
    assert 'assertEquals("1", supplied.getString("ciRunAttempt"))' in test
    assert 'private val fixture = "org.nous.phonebridge.debugfixture"' in test



def test_physical_positive_suite_is_separate_and_required_for_promotion():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "  android-physical:\n" in workflow
    assert "--app-apk \"$RUNNER_TEMP/physical-input/android/outputs/apk/debug/app-debug.apk\"" in workflow
    assert "--test-apk \"$RUNNER_TEMP/physical-input/android/outputs/apk/androidTest/debug/app-debug-androidTest.apk\"" in workflow
    assert "    name: release-input\n    needs: [security, python, android-build, android-e2e, android-physical]" in workflow
    assert "    name: deployment-artifact\n    needs: [security, python, android-build, android-e2e, android-physical]" in workflow
    assert "          version: '589.0.0'" in workflow
    assert "      id-token: write" in workflow
    assert "          create_credentials_file: true" in workflow
    assert "          audience: projects/606598667955/locations/global/workloadIdentityPools/phonebridge-github/providers/github-main" in workflow
