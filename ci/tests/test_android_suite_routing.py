from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_emulator_suite_runs_every_instrumentation_class():
    script = (ROOT / "ci/run_android_instrumentation.sh").read_text()
    argument = re.search(r"-Pandroid\.testInstrumentationRunnerArguments\.class=([^\s]+)", script)
    assert argument is not None
    selected = argument.group(1).split(",")
    classes = {path.stem for path in (ROOT / "android/app/src/androidTest/java/org/nous/phonebridge").glob("*Test.kt")}
    assert len(selected) == len(set(selected))
    assert set(selected) == {"org.nous.phonebridge." + name for name in classes}


def test_ci_workflow_runs_the_emulator_suite_on_both_api_levels():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "api: [31, 35]" in workflow
    assert "script: bash ci/run_android_instrumentation.sh" in workflow
