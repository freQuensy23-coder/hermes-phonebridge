from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_emulator_suite_parts_run_every_instrumentation_class_once():
    script = (ROOT / "ci/run_android_instrumentation.sh").read_text()
    parts = [re.search(rf"^part{index}=([^\s]+)$", script, re.MULTILINE) for index in (1, 2)]
    assert all(parts)
    selected = [name for part in parts for name in part.group(1).split(",")]
    classes = {path.stem for path in (ROOT / "android/app/src/androidTest/java/org/nous/phonebridge").glob("*Test.kt")}
    assert len(selected) == len(set(selected))
    assert set(selected) == {"org.nous.phonebridge." + name for name in classes}
    assert '"-Pandroid.testInstrumentationRunnerArguments.class=$classes"' in script


def test_ci_workflow_runs_the_emulator_suite_on_both_api_levels():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "api: [31, 35]" in workflow
    assert "part: [1, 2]" in workflow
    assert "PHONEBRIDGE_TEST_PART: ${{ matrix.part }}" in workflow
    assert "script: bash ci/run_android_instrumentation.sh" in workflow
    assert "    name: release\n    needs: [version, lint, typecheck, no-comments, security, python, android-build, image, offline-runtime, android-e2e]" in workflow
