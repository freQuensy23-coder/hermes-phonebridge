from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
HARDWARE_CLASSES = {"PhoneOemInstrumentationTest", "SamsungClockInstrumentationTest"}


def test_emulator_suite_routes_every_nonhardware_instrumentation_class():
    script = (ROOT / "ci/run_android_instrumentation.sh").read_text()
    argument = re.search(r"-Pandroid\.testInstrumentationRunnerArguments\.class=([^\s]+)", script)
    assert argument is not None
    selected = argument.group(1).split(",")
    classes = {path.stem for path in (ROOT / "android/app/src/androidTest/java/org/nous/phonebridge").glob("*Test.kt")}
    assert len(selected) == len(set(selected))
    assert set(selected) == {"org.nous.phonebridge." + name for name in classes - HARDWARE_CLASSES}


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
