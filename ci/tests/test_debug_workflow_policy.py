import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from ci.debug_evidence import INSTRUMENTATION, MAX_REPORT_BYTES, UNIT, WHOLEFLOW_PHASES, evidence_summary, read_report, restricted_coordinator_failure, restricted_wholeflow, sanitize_reports


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/debug-control.yml"
SCRIPT = ROOT / "ci/run_debug_instrumentation.sh"
TEST = ROOT / "android/app/src/androidTest/java/org/nous/phonebridge/DebugControlInstrumentationTest.kt"
FIXTURE = ROOT / "android/debugfixture"
EXPECTED_TESTS = {
    "realHttpWsTreeCoordinateTapInputAndSingleSwipeWithIndependentReadback",
    "realPlatformScreenshotApi35OrExplicitApi31Unsupported",
    "localWarningCancellationAndAllFourDurationsRequireConfirmation",
    "localStopAndFreshEnableRejectOldGeneration",
    "ownConsentControlsCannotBeRemotelyReadOrOperated",
    "accessibilitySettingLossRevokesConsentAndPreventsEffects",
    "secureAndPasswordWindowsRefuseCaptureAndEffects",
    "serviceTeardownDoesNotRestoreIndefiniteConsent",
    "realWholeFlowHttpWssGenuineClockHostAdbToggleAndRestartPreserveSurvivor",
}
PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/setup-java": "cf277c60eb25467037889841efdb72551f06f6c3",
    "gradle/actions/setup-gradle": "ed408507eac070d1f99cc633dbcf757c94c7933a",
    "reactivecircus/android-emulator-runner": "a421e43855164a8197daf9d8d40fe71c6996bb0d",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
}


def test_automatic_read_only_matrix_uses_only_existing_pinned_actions():
    source = WORKFLOW.read_text()
    assert "  push:\n" in source and "  pull_request:\n" in source
    assert "'feat/**'" in source
    assert "permissions:\n  contents: read\n" in source
    assert "api: [31, 35]" in source
    assert "runs-on: ubuntu-24.04" in source
    assert "persist-credentials: false" in source
    references = re.findall(r"uses: ([^\s]+)", source)
    assert references
    old = (ROOT / ".github/workflows/ci.yml").read_text()
    for reference in references:
        name, sha = reference.split("@")
        assert PINS[name] == sha
        assert reference in old
    for forbidden in (
        "workflow_dispatch", "pull_request_target", "workflow_run", "self-hosted",
        "secrets.", "id-token:", "contents: write",
        "continue-on-error", "testlab", "gcloud", "google-github-actions",
    ):
        assert forbidden not in source


def test_required_unit_build_python_and_exact_instrumentation_route_are_not_optional():
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    assert "python -m pytest ci/tests/test_debug_workflow_policy.py" in workflow
    assert ":app:testDebugUnitTest" in workflow
    assert ":app:assembleDebug :app:assembleDebugAndroidTest :debugfixture:assembleDebug" in workflow
    assert re.findall(r"--tests (org\.nous\.phonebridge\.\w+)", workflow) == [
        "org.nous.phonebridge.DebugControlTest",
        "org.nous.phonebridge.DebugPayloadBoundsTest",
        "org.nous.phonebridge.DebugExecutionGuardTest",
        "org.nous.phonebridge.RemoteWindowPolicyTest",
    ]
    assert set(UNIT) == {
        "org.nous.phonebridge.DebugControlTest",
        "org.nous.phonebridge.DebugPayloadBoundsTest",
        "org.nous.phonebridge.DebugExecutionGuardTest",
        "org.nous.phonebridge.RemoteWindowPolicyTest",
    }
    assert '"org.nous.phonebridge.DebugExecutionGuardTest": None' in workflow
    assert '"org.nous.phonebridge.RemoteWindowPolicyTest": None' in workflow
    graph_unit = ROOT / "android/app/src/test/java/org/nous/phonebridge/RemoteWindowPolicyTest.kt"
    assert "class RemoteWindowPolicyTest" in graph_unit.read_text()
    assert 'assert cases, "Required debug unit suite must execute"' in workflow
    assert "deadlineRejectsAtExactBoundaryWithoutTimer" in workflow
    assert "bash ci/run_debug_instrumentation.sh" in workflow
    assert "[ \"${GITHUB_ACTIONS:-}\" = true ]" in script
    assert "[ \"${GITHUB_EVENT_NAME:-}\" = push ]" in script
    assert "[ \"${GITHUB_EVENT_NAME:-}\" = pull_request ]" in script
    assert "[ \"${GITHUB_RUN_ATTEMPT:-}\" = 1 ]" in script
    assert "emulator-[0-9]+" in script
    assert "adb -s \"$serial\" install -r android/debugfixture/build/outputs/apk/debug/debugfixture-debug.apk" in script
    assert ":app:connectedDebugAndroidTest" in script
    assert "-Pandroid.testInstrumentationRunnerArguments.class=org.nous.phonebridge.DebugControlInstrumentationTest" in script
    assert "ci/prepare_emulator_server.py start" in workflow
    assert "ci/prepare_emulator_server.py stop" in workflow
    assert "ci/prepare_emulator_server.py stop" in script
    for forbidden in ("run_android_instrumentation.sh", "phonebridge pair", "logcat", "bugreport"):
        assert forbidden not in workflow + script


def test_artifacts_are_allowlisted_sanitized_junit_and_restricted_summary_only():
    workflow = WORKFLOW.read_text()
    uploads = re.findall(r"          path: (.+)", workflow)
    assert uploads == ["${{ runner.temp }}/debug-control-evidence/"]
    assert "if-no-files-found: error" in workflow
    script = SCRIPT.read_text()
    evidence = (ROOT / "ci/debug_evidence.py").read_text()
    assert "python3 ci/debug_evidence.py" in script
    for required in (
        'os.chmod(output, 0o700)', 'os.chmod(destination, 0o600)',
        '"debug-control-junit.xml"', '"debug-control-unit-junit.xml"',
        '"debug-control-summary.log"', '"physical_device_claim": False',
        '"expiry_e2e": "not_exercised_no_production_clock_backdoor"',
    ):
        assert required in evidence
    for forbidden in ("phonebridge-server.log", "phonebridge-tunnel.log", "build/reports/", "cp -r", "tee "):
        assert forbidden not in workflow + script


def test_cleanup_preserves_primary_failure_and_bounds_every_external_action():
    source = SCRIPT.read_text()
    assert 'primary_status=$?' in source
    assert 'trap cleanup EXIT' in source
    assert 'trap - EXIT INT TERM' in source
    assert 'timeout 40s python3 ci/prepare_emulator_server.py stop' in source
    assert 'DEBUG_CONTROL_CLEANUP_STATUS="$cleanup_status" timeout --signal=TERM --kill-after=5s 30s python3 ci/debug_evidence.py' in source
    assert 'evidence_status=$?' in source
    assert 'else primary_status=$evidence_status; fi' in source
    assert 'timeout 20s adb -s "$serial" uninstall org.nous.phonebridge.debugfixture' in source
    assert 'if [ "$primary_status" -eq 0 ]' in source
    assert 'exit "$primary_status"' in source
    test = TEST.read_text()
    assert 'http("DELETE", "/v1/devices/$it", expected = 204)' in test
    assert 'expected = 404' in test
    assert 'Credentials(context).load() == null' in test
    assert 'failure.addSuppressed(error)' in test
    assert 'Settings.Secure.getString(context.contentResolver, key)' in test
    assert 'if (value == null) "settings delete secure $key"' in test
    assert 'originalServices = Settings.Secure.getString' in test
    assert 'originalEnabled = Settings.Secure.getString' in test
    assert 'getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)' in test


def test_independent_automation_enables_interactive_windows_and_restores_original_flags():
    source = TEST.read_text()
    assert 'getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)' in source
    setup = source.split("@Before fun setup()", 1)[1].split("@Test fun", 1)[0]
    capture = setup.index("originalAutomationFlags = serviceInfo.flags")
    configure = setup.index("serviceInfo.flags = serviceInfo.flags or AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS")
    apply = setup.index("automation.serviceInfo = serviceInfo")
    readback = setup.index("assertTrue(automation.serviceInfo.flags and AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS != 0)")
    assert capture < configure < apply < readback < setup.index("BridgeService.stop(context, true)")
    cleanup = source.split("val actions: List<() -> Unit> = listOf(", 1)[1].split("private fun shell", 1)[0]
    assert 'originalAutomationFlags?.let { flags ->' in cleanup
    assert 'serviceInfo.flags = flags' in cleanup
    assert 'automation.serviceInfo = serviceInfo' in cleanup
    assert 'assertEquals(flags, automation.serviceInfo.flags)' in cleanup
    assert cleanup.index('shell("am force-stop $fixture")') < cleanup.index('serviceInfo.flags = flags')
    assert 'if (failure == null) failure = error else if (failure !== error) failure.addSuppressed(error)' in cleanup
    screenshot = source.split("@Test fun realPlatformScreenshotApi35OrExplicitApi31Unsupported()", 1)[1].split("@Test fun localWarning", 1)[0]
    for required in (
        'automation.rootInActiveWindow?.window ?: error("Actual fixture window must be independently observable")',
        'localWindow.getBoundsInScreen(it)',
        'assertEquals(fixture, localWindow.root?.packageName?.toString())',
        'nativeBounds[index], bounds.getInt(index)',
        'assertEquals(bounds.getInt(index), windowBounds.getInt(index))',
        'BitmapFactory.decodeByteArray', 'bitmap.getPixel(x, y)', 'pixels.size > 12',
    ):
        assert required in screenshot


def test_real_production_transport_and_real_ui_readbacks_have_no_bypass_or_skip():
    source = TEST.read_text()
    assert set(re.findall(r"@Test fun (\w+)\(", source)) == EXPECTED_TESTS
    assert INSTRUMENTATION == {"org.nous.phonebridge.DebugControlInstrumentationTest": EXPECTED_TESTS}
    for required in (
        'BridgeHttp.claim(qr, "Debug automatic CI", false)', 'Credentials(context).save(claimed)',
        'BridgeService.start(it)', '"wss://${qr.server.host}/v1/device/ws"',
        '"/v1/devices/${deviceId ?: error("Missing paired device")}/commands"',
        '"/v1/commands/${queued.getString("id")}"', 'command("debug.status", JSONObject())',
        '"debug_session"', '"screen_bounds"', 'BitmapFactory.decodeByteArray',
        '"screen.tap"', '"screen.input"', '"screen.swipe"', '"UNSUPPORTED"',
        'localText("counter")', 'localText("editor")', '"android:id/button1"',
        '"android:id/button2"', '"debug_enable_10"', '"debug_enable_15"',
        '"debug_enable_60"', '"debug_enable_manual"',
        '"org.nous.phonebridge:id/debug_stop"', '"org.nous.phonebridge:id/debug_status"',
        'assertFalse(result.getBoolean("verified"))',
    ):
        assert required in source
    for forbidden in (
        'NativeHandlers(', '.handle(', 'enableLocal(', 'DebugControl(', 'DebugConsent.',
        'Assume.', '@Ignore', 'MockWebServer', 'WebSocketListener', 'adoptShellPermissionIdentity',
        'System.setProperty', 'SystemClock.setCurrentTimeMillis', 'takeScreenshot(',
    ):
        assert forbidden not in source


def test_fixture_is_separate_permissionless_debug_only_real_android_ui():
    build = (FIXTURE / "build.gradle.kts").read_text()
    manifest = ET.parse(FIXTURE / "src/main/AndroidManifest.xml").getroot()
    android = "{http://schemas.android.com/apk/res/android}"
    assert 'applicationId = "org.nous.phonebridge.debugfixture"' in build
    assert 'namespace = "org.nous.phonebridge.debugfixture"' in build
    assert 'beforeVariants(selector().withBuildType("release")) { it.enable = false }' in build
    assert manifest.findall("uses-permission") == []
    app = manifest.find("application")
    assert app is not None
    assert app.attrib[android + "allowBackup"] == "false"
    assert app.attrib[android + "usesCleartextTraffic"] == "false"
    activity = (FIXTURE / "src/main/java/org/nous/phonebridge/debugfixture/FixtureActivity.kt").read_text()
    for required in (
        'Button(this)', 'EditText(this)', 'ScrollView(this)', 'counter++',
        'FLAG_SECURE', 'TYPE_TEXT_VARIATION_PASSWORD', '"Synthetic password only"',
        'R.id.increment', 'R.id.counter', 'R.id.editor', 'R.id.scroll',
    ):
        assert required in activity
    assert 'include(":debugfixture")' in (ROOT / "android/settings.gradle.kts").read_text()
    assert "debugfixture" not in (ROOT / "android/app/build.gradle.kts").read_text()
    assert "debugfixture" not in (ROOT / "android/app/src/main/AndroidManifest.xml").read_text()


def test_only_isolated_synthetic_server_helper_supplies_credentials_and_transport():
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    helper = (ROOT / "ci/prepare_emulator_server.py").read_text()
    assert workflow.count("run: python3 ci/prepare_emulator_server.py start") == 1
    assert '"${PHONEBRIDGE_CI_ADMIN_TOKEN_FILE:?' in script
    assert 'IFS= read -r admin_token < "$PHONEBRIDGE_CI_ADMIN_TOKEN_FILE"' in script
    assert 'secrets.token_urlsafe(48)' in helper
    assert 'if os.environ.get("GITHUB_ACTIONS") != "true":' in helper
    assert '"PHONEBRIDGE_DB_PATH": str(root / "phonebridge.sqlite3")' in helper
    assert 'environment = isolated_environment(root)' in helper
    assert '"--host", "127.0.0.1", "--port", "18940", "--no-access-log"' in helper
    assert 'PHONEBRIDGE_CI_PUBLIC_URL' in workflow + script
    assert 'serverUrl=http://10.0.2.2:18940' in script
    assert 'expectedPublicServerUrl=$PHONEBRIDGE_CI_PUBLIC_URL' in script
    assert '"$PHONEBRIDGE_CI_PUBLIC_URL" =~ ^https://' in script
    assert 'timeout --signal=TERM --kill-after=30s 18m ./gradlew' in script
    assert 'timeout 60s adb -s "$serial" install -r' in script
    for forbidden in ("curl ", "wget ", "aws ", "gcloud ", "firebase ", "oauth", "PHONEBRIDGE_KEYSTORE"):
        assert forbidden not in workflow + script


def test_capability_sync_and_refusal_layers_are_explicit_without_weakening_phone_session_gate():
    source = TEST.read_text()
    enable = source.split("private fun localEnable(", 1)[1].split("private fun localStop()", 1)[0]
    assert enable.index('localClick("android:id/button1")') < enable.index("waitForScreenCapabilities(true)") < enable.index('return status.getString("debug_session")')
    assert 'val device = http("GET", "/v1/devices/${deviceId ?: error("Missing paired device")}")' in source
    assert 'device.getJSONArray("capabilities")' in source
    assert 'waitUntil { advertisedScreenOperations() == expected }' in source
    assert 'expected = 422' in source
    assert 'assertEquals("UNSUPPORTED", response.getJSONObject("error").getString("code"))' in source
    assert 'if (response.status == 422)' in source
    assert '200, response.status)' in source
    assert 'refused(completedCommand(response.body), "PERMISSION_REQUIRED")' in source
    capture = source.split("@Test fun realPlatformScreenshotApi35OrExplicitApi31Unsupported()", 1)[1].split("@Test fun localWarning", 1)[0]
    assert 'serverEnqueueUnsupported("screen.screenshot", args(session))' in capture
    assert capture.index('serverEnqueueUnsupported("screen.screenshot", args(session))') < capture.index('success(command("screen.screenshot", args(session)))')
    assert 'image.getJSONArray("window_bounds")' in capture
    assert 'actualWindowBounds' in capture
    stale = source.split("@Test fun localStopAndFreshEnableRejectOldGeneration()", 1)[1].split("@Test fun ownConsent", 1)[0]
    fresh = stale.split("val fresh = localEnable()", 1)[1]
    assert 'waitForScreenCapabilities(true)' in fresh
    assert 'assertEquals(fixture, tree(fresh).getString("package"))' in fresh
    assert 'refused(command("screen.tree", args(old)), "PERMISSION_REQUIRED")' in fresh
    assert 'refused(command("screen.tap", args(old)' in fresh
    assert "unavailableConsentRefusal" not in fresh
    assert "serverEnqueueUnsupported" not in fresh
    positive = source.split("@Test fun realHttpWsTreeCoordinateTapInputAndSingleSwipeWithIndependentReadback()", 1)[1].split("@Test fun realPlatformScreenshot", 1)[0]
    assert 'args(session, "org.nous.phonebridge.synthetic.mismatch")), "FOREGROUND_REQUIRED")' in positive
    assert 'JSONObject().put("debug_session", session).put("action", "unlock")), "VALIDATION_ERROR")' in positive
    assert positive.index('"FOREGROUND_REQUIRED"') < positive.index('val tap = success(')
    assert positive.index('"VALIDATION_ERROR"') < positive.index('val tap = success(')


def raw_report(classname, outcomes):
    suite = ET.Element("testsuite")
    for name, outcome in outcomes:
        node = ET.SubElement(suite, "testcase", {"classname": classname, "name": name, "time": "999"})
        if outcome:
            ET.SubElement(node, outcome, {"message": "PRIVATE_EXCEPTION_TOKEN", "type": "PRIVATE_TYPE"}).text = "PRIVATE_STACK_TRACE"
    ET.SubElement(suite, "system-out").text = "PRIVATE_DEVICE_AND_TOKEN"
    return ET.tostring(suite)


def assert_observed_totals(xml, counts, outcomes):
    root = ET.fromstring(xml)
    cases = root.findall("testcase")
    expected_tags = [outcome if isinstance(outcome, set) else set() if outcome == "pass" else {outcome} for outcome in outcomes]
    assert [{child.tag for child in case} for case in cases] == expected_tags
    assert all(len(case) == len(tags) for case, tags in zip(cases, expected_tags))
    expected = {"tests": len(cases), "failures": sum("failure" in tags for tags in expected_tags), "errors": sum("error" in tags for tags in expected_tags), "skipped": sum("skipped" in tags for tags in expected_tags)}
    assert {key: int(root.attrib[key]) for key in expected} == expected
    assert counts["observed_tests"] == expected["tests"]
    assert counts["passed_tests"] == sum(not tags for tags in expected_tags)
    assert counts["failed_tests"] == expected["failures"]
    assert counts["error_tests"] == expected["errors"]
    assert counts["skipped_tests"] == expected["skipped"]
    assert b"PRIVATE" not in xml
    assert root.find("system-out") is None
    assert all(set(case.attrib) == {"classname", "name"} for case in cases)


def test_evidence_empty_never_fabricates_required_instrumentation_or_unit_cases():
    for required in (INSTRUMENTATION, UNIT):
        xml, counts = sanitize_reports([], required)
        assert_observed_totals(xml, counts, [])
        assert counts["missing_suites"] == len(required)
        assert counts["missing_tests"] == sum(len(names or ()) for names in required.values())
        assert counts["report_validation_failures"] > 0
        assert b"requiredUnitReport" not in xml
        assert not counts["complete"]


def test_evidence_partial_does_not_turn_real_pass_into_failure():
    classname, names = next(iter(INSTRUMENTATION.items()))
    name = sorted(names)[0]
    xml, counts = sanitize_reports([raw_report(classname, [(name, None)])], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"])
    assert counts["missing_tests"] == len(names) - 1
    assert not counts["complete"]


def test_evidence_complete_pass_counts_match_published_xml_and_fixed_summary():
    classname, names = next(iter(INSTRUMENTATION.items()))
    xml, counts = sanitize_reports([raw_report(classname, [(name, None) for name in sorted(names)])], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"] * len(names))
    unit_reports = [raw_report(name, [(next(iter(required or {"realUnitMethod"})), None)]) for name, required in UNIT.items()]
    unit_xml, units = sanitize_reports(unit_reports, UNIT, exact_names=False)
    assert_observed_totals(unit_xml, units, ["pass"] * len(UNIT))
    wholeflow = restricted_wholeflow(json.dumps({"phases": WHOLEFLOW_PHASES, "host_adb_taps": 1, "coordinator_error": False}).encode())
    summary = evidence_summary(counts, units, 0, 0, wholeflow)
    assert summary["instrumentation"] == counts
    assert summary["unit"] == units
    assert summary["complete"]
    for primary, cleanup in ((1, 0), (0, 1), (143, 1)):
        failed = evidence_summary(counts, units, primary, cleanup, wholeflow)
        assert not failed["complete"]
        assert failed["primary_process_exit"] == primary
        assert failed["cleanup_exit"] == cleanup
        assert failed["instrumentation"] == counts


def test_evidence_real_failure_error_skip_keep_their_outcomes_and_strip_private_messages():
    classname, names = next(iter(INSTRUMENTATION.items()))
    outcomes = [None, "failure", "error", "skipped"]
    xml, counts = sanitize_reports([raw_report(classname, list(zip(sorted(names), outcomes)))], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass", "failure", "error", "skipped"])
    assert not counts["complete"]
    assert counts["missing_tests"] == len(names) - len(outcomes)


def test_evidence_duplicates_remain_two_observations_without_poisoning_passes():
    classname, names = next(iter(INSTRUMENTATION.items()))
    name = sorted(names)[0]
    xml, counts = sanitize_reports([raw_report(classname, [(name, None), (name, "skipped")])], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass", "skipped"])
    assert counts["duplicate_tests"] == 1
    assert not counts["complete"]


def test_unit_missing_report_keeps_other_real_unit_pass_and_boundary_missing_separate():
    classname = "org.nous.phonebridge.DebugControlTest"
    xml, counts = sanitize_reports([raw_report(classname, [("realUnitMethod", None)]), None], UNIT, exact_names=False)
    assert_observed_totals(xml, counts, ["pass"])
    assert counts["missing_tests"] == 1
    assert counts["missing_suites"] == len(UNIT) - 1
    assert counts["report_validation_failures"] == 1
    assert b"requiredUnitReport" not in xml
    assert not counts["complete"]


def test_unit_outcomes_and_duplicates_are_sanitized_by_the_actual_publisher_parser():
    classname = "org.nous.phonebridge.DebugPayloadBoundsTest"
    xml, counts = sanitize_reports([raw_report(classname, [("realUnitMethod", None), ("realUnitMethod", "failure"), ("otherUnitMethod", "skipped"), ("lastUnitMethod", "error")])], UNIT, exact_names=False)
    assert_observed_totals(xml, counts, ["pass", "failure", "skipped", "error"])
    assert counts["duplicate_tests"] == 1
    assert not counts["complete"]


def test_evidence_unexpected_identity_is_observed_but_not_leaked_or_counted_as_expected():
    xml, counts = sanitize_reports([raw_report("PRIVATE_CLASS", [("PRIVATE_TEST", "failure")])], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["failure"])
    assert counts["unexpected_tests"] == 1
    assert counts["missing_tests"] == len(EXPECTED_TESTS)
    assert not counts["complete"]


def test_evidence_invalid_empty_oversize_reports_do_not_synthesize_cases():
    for report in (b"<broken PRIVATE", b"<testsuite/>", b"<other/>", b"x" * (4 * 1024 * 1024 + 1), b'<!DOCTYPE x [<!ENTITY secret "PRIVATE">]><testsuite/>'):
        xml, counts = sanitize_reports([report], INSTRUMENTATION)
        assert_observed_totals(xml, counts, [])
        assert counts["report_validation_failures"] > 0
        assert not counts["complete"]


def test_evidence_full_pass_report_remains_all_pass_when_another_report_is_invalid():
    classname, names = next(iter(INSTRUMENTATION.items()))
    report = raw_report(classname, [(name, None) for name in sorted(names)])
    xml, counts = sanitize_reports([report, b"<broken PRIVATE_EXCEPTION"], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"] * len(names))
    assert counts["missing_tests"] == 0
    assert counts["report_validation_failures"] == 1
    assert not counts["complete"]


def test_evidence_full_inventory_with_skip_never_counts_skipped_as_passed():
    classname, names = next(iter(INSTRUMENTATION.items()))
    ordered = sorted(names)
    xml, counts = sanitize_reports([raw_report(classname, [(name, "skipped" if index == 0 else None) for index, name in enumerate(ordered)])], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["skipped"] + ["pass"] * (len(names) - 1))
    assert counts["missing_tests"] == 0
    assert counts["skipped_tests"] == 1
    assert not counts["complete"]


def test_evidence_report_and_case_bounds_fail_without_fabricated_executions():
    classname, names = next(iter(INSTRUMENTATION.items()))
    report = raw_report(classname, [(sorted(names)[0], None)])
    xml, counts = sanitize_reports([report] * 65, INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"] * 64)
    assert counts["report_validation_failures"] == 1
    assert counts["duplicate_tests"] == 63
    assert not counts["complete"]
    oversized = raw_report(classname, [(sorted(names)[0], None)] * 4097)
    xml, counts = sanitize_reports([oversized], INSTRUMENTATION)
    assert_observed_totals(xml, counts, [])
    assert counts["report_validation_failures"] == 1
    assert not counts["complete"]


def test_evidence_incorrect_declared_totals_are_harness_failure_not_changed_outcome():
    classname, names = next(iter(INSTRUMENTATION.items()))
    report = ET.fromstring(raw_report(classname, [(sorted(names)[0], None)]))
    report.set("tests", "8")
    report.set("failures", "1")
    xml, counts = sanitize_reports([ET.tostring(report)], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"])
    assert counts["report_validation_failures"] > 0
    assert not counts["complete"]


def test_evidence_multiple_marker_kinds_preserve_overlapping_categories_without_inventing_cases():
    classname, names = next(iter(INSTRUMENTATION.items()))
    selected = sorted(names)[:2]
    report = ET.fromstring(raw_report(classname, [(name, None) for name in selected]))
    for tag in ("failure", "error", "skipped"):
        ET.SubElement(report.findall("testcase")[0], tag, {"message": "PRIVATE_MESSAGE", "type": "PRIVATE_TYPE"}).text = "PRIVATE_STACK"
    report.attrib.update({"tests": "2", "failures": "1", "errors": "1", "skipped": "1"})
    xml, counts = sanitize_reports([ET.tostring(report)], INSTRUMENTATION)
    assert_observed_totals(xml, counts, [{"failure", "error", "skipped"}, "pass"])
    assert counts["report_validation_failures"] == 1
    assert counts["missing_tests"] == len(names) - 2
    assert not counts["complete"]


def test_evidence_repeated_same_kind_markers_count_one_observed_test_and_one_category():
    classname, names = next(iter(INSTRUMENTATION.items()))
    selected = sorted(names)[:2]
    for tag, field in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
        report = ET.fromstring(raw_report(classname, [(name, None) for name in selected]))
        for _ in range(3):
            ET.SubElement(report.findall("testcase")[0], tag, {"message": "PRIVATE_MESSAGE"}).text = "PRIVATE_STACK"
        report.attrib.update({"tests": "2", field: "1"})
        xml, counts = sanitize_reports([ET.tostring(report)], INSTRUMENTATION)
        assert_observed_totals(xml, counts, [tag, "pass"])
        assert counts["report_validation_failures"] == 1
        assert counts["missing_tests"] == len(names) - 2
        assert not counts["complete"]


def test_report_loader_reads_a_real_regular_file_through_the_actual_sanitizer(tmp_path):
    classname, names = next(iter(INSTRUMENTATION.items()))
    payload = raw_report(classname, [(name, None) for name in sorted(names)])
    path = tmp_path / "TEST-observed.xml"
    path.write_bytes(payload)
    loaded = read_report(path)
    assert loaded == payload
    xml, counts = sanitize_reports([loaded], INSTRUMENTATION)
    assert_observed_totals(xml, counts, ["pass"] * len(names))
    assert counts["complete"]


def test_report_loader_missing_symlink_and_oversize_files_never_synthesize_cases(tmp_path):
    missing = tmp_path / "TEST-missing.xml"
    regular = tmp_path / "TEST-regular.xml"
    regular.write_bytes(b"<testsuite/>")
    symlink = tmp_path / "TEST-symlink.xml"
    symlink.symlink_to(regular)
    dangling = tmp_path / "TEST-dangling.xml"
    dangling.symlink_to(missing)
    oversized = tmp_path / "TEST-oversize.xml"
    with oversized.open("wb") as destination:
        destination.truncate(MAX_REPORT_BYTES + 1)
    for path in (missing, symlink, dangling, oversized):
        loaded = read_report(path)
        assert loaded is None
        xml, counts = sanitize_reports([loaded], INSTRUMENTATION)
        assert_observed_totals(xml, counts, [])
        assert counts["report_validation_failures"] == 1
        assert counts["missing_tests"] == len(EXPECTED_TESTS)
        assert not counts["complete"]


def test_report_loader_rejects_directory_and_fifo_before_open(tmp_path, monkeypatch):
    directory = tmp_path / "TEST-directory.xml"
    directory.mkdir()
    fifo = tmp_path / "TEST-fifo.xml"
    os.mkfifo(fifo)

    def refuse_open(*args, **kwargs):
        raise AssertionError("Nonregular reports must be rejected before open")

    monkeypatch.setattr(os, "open", refuse_open)
    for path in (directory, fifo):
        assert read_report(path) is None


def test_report_loader_fifo_replacement_is_nonblocking_and_rejected_after_open(tmp_path, monkeypatch):
    path = tmp_path / "TEST-replaced.xml"
    path.write_bytes(b"<testsuite/>")
    original_open = os.open
    opened = []

    def replace_with_fifo(target, flags):
        assert flags & os.O_NONBLOCK
        assert flags & os.O_NOFOLLOW
        path.unlink()
        os.mkfifo(path)
        opened.append(target)
        return original_open(target, flags)

    monkeypatch.setattr(os, "open", replace_with_fifo)
    assert read_report(path) is None
    assert opened == [path]



def test_wholeflow_requires_exact_ninth_case_genuine_asset_and_observable_host_adb_transport():
    workflow = WORKFLOW.read_text()
    script = SCRIPT.read_text()
    source = TEST.read_text()
    assert len(EXPECTED_TESTS) == 9
    assert "realWholeFlowHttpWssGenuineClockHostAdbToggleAndRestartPreserveSurvivor" in EXPECTED_TESTS
    assert workflow.count("GH_TOKEN: ${{ github.token }}") == 1
    assert "timeout 120s gh release download phonebridge-ci-vendor-fixtures --repo freQuensy23-coder/hermes-phonebridge --pattern google-8.2.apk" in workflow
    assert "cf71cbc0728bb44e37d5ad19772b6ae9b39aae07b46945e7cde3a930cbccd383" in workflow
    assert "sha256sum --check --status" in workflow
    assert '-Pandroid.testInstrumentationRunnerArguments.genuineClockWholeFlow=true' in script
    for required in (
        'ro.product.cpu.abi', 'ro.build.version.sdk', 'shell pm clear com.google.android.deskclock',
        '"$RUNNER_TEMP/clock-fixture/google-8.2.apk"',
        '["adb", "-s", serial, *arguments]',
        'adb("shell", "input", "tap", str(request["x"]), str(request["y"]))',
        'adb("shell", "-T", "run-as", package', '"expires_elapsed_ms"',
        '"expected_package"] != clock', '"debug_session"', '"nonce"',
        'read_private("request.json") != request', 'if consumed is not None:',
        'time.monotonic() + 18 * 60', 'timeout=10', 'len(result.stdout) > 4096',
        'kill -TERM "$adb_coordinator_pid"', 'wait "$adb_coordinator_pid" || cleanup_status=1',
        '"host_adb_shell_input"', '"host_adb_taps"] = 1',
    ):
        assert required in script
    for required in (
        'GoogleClockProfile.installed(context)', 'nativeClockNodes', 'automation.rootInActiveWindow',
        '"alarm_recycler_view"', '"digital_clock"', '"onoff"',
        'requestHostAdbToggle(session, target)', 'target.copy(enabled = false)',
        '"clock.alarms.create"', '"clock.alarms.list"', '"clock.alarms.disable"', '"clock.alarms.enable"',
        'genericClockTap(session, nativeAlarmCard(target.label))', 'genericClockTap(session, deletion)',
        'expectNativeAlarms(baseline + survivor)', 'showNativeAlarms(restart = true)',
        'shell("pidof $clockPackage").trim().isEmpty()',
        'wholeFlowPhase("completed")', 'wholeFlowPhase("failed")',
    ):
        assert required in source
    assert 'ClockAdapters(' not in source
    assert 'NativeClocks(' not in source


def test_failure_evidence_keeps_only_allowlisted_source_coordinates():
    classname, names = next(iter(INSTRUMENTATION.items()))
    report = ET.fromstring(raw_report(classname, [(sorted(names)[0], "failure")]))
    failure = report.find("testcase/failure")
    assert failure is not None
    failure.text = (
        "java.lang.AssertionError: PRIVATE_TOKEN bridge_code=UNSUPPORTED bridge_reason=SYSTEM_WINDOW\n"
        "at org.nous.phonebridge.DebugControlInstrumentationTest.waitUntil(DebugControlInstrumentationTest.kt:120)\n"
        "at org.nous.phonebridge.DebugControlInstrumentationTest.setup(DebugControlInstrumentationTest.kt:324)\n"
        "at org.nous.phonebridge.PRIVATE_TOKEN(PRIVATE_TOKEN.kt:987)\n"
    )
    xml, counts = sanitize_reports([ET.tostring(report)], INSTRUMENTATION)
    observed = ET.fromstring(xml).find("testcase/failure")
    assert observed is not None and observed.text is not None
    assert counts["failed_tests"] == 1
    assert b"PRIVATE" not in xml
    assert json.loads(observed.text) == {
        "locations": ["DebugControlInstrumentationTest.kt:120", "DebugControlInstrumentationTest.kt:324"],
        "bridge_code": "UNSUPPORTED",
        "bridge_reason": "SYSTEM_WINDOW",
    }


def test_host_adb_focus_uses_display_dump_and_requires_one_exact_clock_window():
    script = SCRIPT.read_text()
    assert 'adb("shell", "dumpsys", "window", "displays")' in script
    assert 'adb("shell", "dumpsys", "window", "windows")' not in script
    assert 'if not clock_is_focused(focus):' in script
    source = "def clock_is_focused(focus):" + script.split("def clock_is_focused(focus):", 1)[1].split("def write_ack", 1)[0]
    namespace: dict[str, object] = {"re": re}
    exec(compile(source, "host-adb-focus", "exec"), namespace)
    focused = namespace["clock_is_focused"]
    assert callable(focused)
    valid = "  mCurrentFocus=Window{123abc u0 com.google.android.deskclock/com.android.deskclock.DeskClock}\n"
    assert focused(valid)
    assert focused("Display: mDisplayId=0\n" + valid + "  mLastFocus=null\n")
    for value in (
        "", "mCurrentFocus=null\n", valid + valid,
        valid + "  mCurrentFocus=null\n",
        valid.replace("com.google.android.deskclock/", "com.example.clock/"),
        valid.replace("com.google.android.deskclock/", "com.google.android.deskclock.evil/"),
        valid.replace("mCurrentFocus=", "mLastFocus="),
        "Window #0 " + valid.strip(),
        valid.rstrip() + " forged\n",
    ):
        assert not focused(value)


def test_host_adb_json_reads_use_shell_exit_status_for_missing_private_files():
    source = SCRIPT.read_text()
    read = source.split("def read_private(name):", 1)[1].split("def write_ack", 1)[0]
    assert 'adb("shell", "-T", "run-as", package, "head"' in read
    assert 'if result.returncode != 0:' in read
    assert 'return None' in read


def test_wholeflow_receipt_is_bounded_private_and_never_substitutes_for_observed_junit():
    observed = {"phases": WHOLEFLOW_PHASES, "host_adb_taps": 1, "coordinator_error": False}
    receipt = restricted_wholeflow(json.dumps(observed).encode())
    assert receipt["receipt_valid"] and receipt["complete"]
    classname, names = next(iter(INSTRUMENTATION.items()))
    _, missing = sanitize_reports([], INSTRUMENTATION)
    _, present = sanitize_reports([raw_report(classname, [(name, None) for name in names])], INSTRUMENTATION)
    _, unit = sanitize_reports([raw_report(name, [(next(iter(required or {"realUnitMethod"})), None)]) for name, required in UNIT.items()], UNIT, exact_names=False)
    assert not evidence_summary(missing, unit, 0, 0, receipt)["complete"]
    for value in (
        None, b"PRIVATE", b"x" * 4097,
        json.dumps(observed | {"private": "PRIVATE"}).encode(),
        json.dumps(observed | {"phases": WHOLEFLOW_PHASES + ["PRIVATE"]}).encode(),
        json.dumps(observed | {"phases": WHOLEFLOW_PHASES * 3}).encode(),
        json.dumps(observed | {"host_adb_taps": True}).encode(),
        json.dumps(observed | {"host_adb_taps": 2}).encode(),
    ):
        restricted = restricted_wholeflow(value)
        assert not restricted["receipt_valid"]
        assert "PRIVATE" not in json.dumps(restricted)
        assert not evidence_summary(present, unit, 0, 0, restricted)["complete"]
    for mutation in (
        {"phases": WHOLEFLOW_PHASES[:-1]}, {"phases": WHOLEFLOW_PHASES + ["failed"]},
        {"phases": list(reversed(WHOLEFLOW_PHASES))}, {"host_adb_taps": 0}, {"coordinator_error": True},
    ):
        restricted = restricted_wholeflow(json.dumps(observed | mutation).encode())
        assert restricted["receipt_valid"] and not restricted["complete"]
        assert not evidence_summary(present, unit, 0, 0, restricted)["complete"]


def test_bridge_window_diagnostic_reads_only_live_service_numeric_geometry_on_ui_thread():
    source = TEST.read_text()
    capture = source.split("private fun bridgeWindowsDiagnostic(): String?", 1)[1].split("private fun success(", 1)[0]
    for required in (
        "UiThread.call {", "BridgeAccessibility.current", "service.rootInActiveWindow",
        "service.windows.take(16)", "selected?.windowId ?: -1", "vector.add(windows.size)",
        "listOf(-1, -1, -1, -1)", "window.id, window.type",
        "if (window.isActive) 1 else 0", "if (window.isFocused) 1 else 0",
        "window.getBoundsInScreen(it)", "node.getBoundsInScreen(it)", "bounds(window.root)",
        'vector.joinToString(",", "[", "]")', "catch (_: Exception) { null }",
    ):
        assert required in capture
    for forbidden in (
        "automation", "packageName", "text", "contentDescription", "viewIdResourceName",
        "childCount", "getChild", "toString()", "shell(", "performAction", "layer",
    ):
        assert forbidden not in capture
    success = source.split("private fun success(value: JSONObject)", 1)[1].split("private fun refused(", 1)[0]
    assert 'if (value.optString("status") != "succeeded") bridgeWindowsDiagnostic()' in success
    assert '" bridge_windows=$it"' in success
    assert 'bridge_reason=$reason$windows' in success
    assert '"succeeded", value.getString("status")' in success
    assert '"Unsupported Clock collection item", "Unknown Clock collection rectangle" -> "CLOCK_COLLECTION_SCHEMA"' in success
    assert '"Clock promotion popup is not a collection host" -> "CLOCK_POPUP"' in success
    assert '"Clock window root unavailable"' in success


def bridge_diagnostic_report(stack, outcome="failure", classname="org.nous.phonebridge.DebugControlInstrumentationTest"):
    name = sorted(INSTRUMENTATION["org.nous.phonebridge.DebugControlInstrumentationTest"])[0]
    report = ET.fromstring(raw_report(classname, [(name, outcome)]))
    report.find("testcase/" + outcome).text = stack
    return ET.tostring(report)


def test_window_numeric_vector_preserves_geometry_null_roots_and_outcomes_without_private_data():
    vectors = [
        [42, 0, 63, 1080, 2214, 2,
         42, 1, 1, 1, 0, 63, 1080, 2214, 0, 63, 1080, 2214,
         43, 3, 0, 0, 0, 2214, 1080, 2280, -1, -1, -1, -1],
        [-1, -1, -1, -1, -1, 0],
        [42, -2147483648, -1, 2147483647, 0, 1,
         42, 6, 0, 1, -2147483648, -1, 2147483647, 0, -1, -1, -1, -1],
        [42, 0, 0, 1080, 2280, 16] + [value for window in range(16) for value in [window, 1, 0, 0, 0, 0, 1080, 2280, -1, -1, -1, -1]],
    ]
    for vector in vectors:
        for outcome, reason in (("failure", "SYSTEM_WINDOW"), ("error", "PROTECTED_UI")):
            stack = "java.lang.AssertionError: PRIVATE_TOKEN bridge_code=UNSUPPORTED bridge_reason=" + reason + " bridge_windows=" + json.dumps(vector, separators=(",", ":")) + " expected:<succeeded> but was:<failed>\nPRIVATE_STACK"
            xml, counts = sanitize_reports([bridge_diagnostic_report(stack, outcome)], INSTRUMENTATION)
            assert_observed_totals(xml, counts, [outcome])
            marker = ET.fromstring(xml).find("testcase/" + outcome)
            assert json.loads(marker.text) == {
                "bridge_code": "UNSUPPORTED", "bridge_reason": reason,
                "bridge_windows": vector, "locations": [],
            }
            assert not counts["complete"]


def test_window_numeric_vector_rejects_malformed_private_or_unbounded_markers():
    vector = [42, 0, 0, 1080, 2280, 1, 42, 1, 1, 1, 0, 0, 1080, 2280, -1, -1, -1, -1]
    token = json.dumps(vector, separators=(",", ":"))
    malformed = [
        "PRIVATE_TOKEN", "", "null", "{}", "[]", "[1,2]", token + "PRIVATE_TOKEN",
        token + ",PRIVATE_TOKEN", token + ";PRIVATE_TOKEN", token + "\\PRIVATE_TOKEN",
        token.replace("42", "PRIVATE_TOKEN", 1), token.replace("42", '"42"', 1),
        token.replace("42", "true", 1), token.replace("42", "42.0", 1),
        token.replace("42", "4.2e1", 1), token.replace("42", "+42", 1),
        token.replace("42", "042", 1), token.replace("42", "４２", 1),
        token.replace("42", "2147483648", 1), token.replace("42", "-2147483649", 1),
        token.replace(",0,", ",-0,", 1), token.replace(",", ", ", 1),
        token.replace(",", ",\n", 1), token.replace(",", ",\t", 1),
        token[:-1] + ",0]", token[:-1], token + "]", "[" + "1," * 4096 + "1]",
        token + " bridge_windows=" + token,
        "PRIVATE_TOKEN bridge_windows=" + token,
    ]
    for index, value in ((0, -2), (5, -1), (5, 0), (5, 2), (5, 17), (6, -1), (7, 0), (7, 7), (8, -1), (8, 2), (9, 2)):
        changed = vector.copy()
        changed[index] = value
        malformed.append(json.dumps(changed, separators=(",", ":")))
    malformed.append(json.dumps([-1, 0, 0, 0, 0, 0], separators=(",", ":")))
    malformed.append(json.dumps([42, 0, 0, 1080, 2280, 17] + vector[6:] * 17, separators=(",", ":")))
    for token in malformed:
        stack = "PRIVATE_TOKEN bridge_code=UNSUPPORTED bridge_reason=SYSTEM_WINDOW bridge_windows=" + token
        xml, counts = sanitize_reports([bridge_diagnostic_report(stack)], INSTRUMENTATION)
        assert_observed_totals(xml, counts, ["failure"])
        marker = ET.fromstring(xml).find("testcase/failure")
        assert json.loads(marker.text) == {"bridge_code": "UNSUPPORTED", "bridge_reason": "SYSTEM_WINDOW", "locations": []}
        assert b"bridge_windows" not in xml
        assert not counts["complete"]


def test_clock_reason_categories_and_direct_source_frames_are_fixed_and_privacy_filtered():
    source = TEST.read_text().split("private fun success(value: JSONObject)", 1)[1].split("private fun refused(", 1)[0]
    reasons = {
        "CLOCK_MISSING_ROOT", "CLOCK_POPUP", "CLOCK_WINDOW_GRAPH", "CLOCK_WINDOW_CHANGED",
        "CLOCK_INCOMPLETE_TREE", "CLOCK_STALE_NODE", "CLOCK_FOREIGN_NODE", "CLOCK_DETAIL_OR_LOADING",
        "CLOCK_COLLECTION_ANCHOR", "CLOCK_COLLECTION_COUNT", "CLOCK_COLLECTION_SCHEMA",
        "CLOCK_COLLECTION_INCOMPLETE", "CLOCK_COLLECTION_GEOMETRY", "CLOCK_ALARM_IDENTITY",
        "CLOCK_ALARM_SCHEMA", "CLOCK_ALARM_SCHEDULE",
    }
    assert set(re.findall(r'-> "(CLOCK_[A-Z_]+)"', source)) == reasons
    for reason in reasons:
        stack = (
            "PRIVATE_TOKEN bridge_code=UNSUPPORTED bridge_reason=" + reason + "\n"
            "at org.nous.phonebridge.GoogleClockAdapter.requireSchema(GoogleClockAdapter.kt:26)\n"
            "at org.nous.phonebridge.GoogleClockAdapter.index(GoogleClockAdapter.kt:116)\n"
            "at org.nous.phonebridge.GoogleClockModalGraph.requireSingle(GoogleClockProfile.kt:88)\n"
            "at org.nous.phonebridge.PRIVATE_TOKEN(PRIVATE_TOKEN.kt:123)\n"
        )
        xml, counts = sanitize_reports([bridge_diagnostic_report(stack)], INSTRUMENTATION)
        assert_observed_totals(xml, counts, ["failure"])
        marker = ET.fromstring(xml).find("testcase/failure")
        assert json.loads(marker.text) == {
            "bridge_code": "UNSUPPORTED", "bridge_reason": reason,
            "locations": ["GoogleClockAdapter.kt:26", "GoogleClockAdapter.kt:116", "GoogleClockProfile.kt:88"],
        }
    for reason in ("PRIVATE_TOKEN", "CLOCK_COLLECTION_SCHEMA_PRIVATE_TOKEN", "CLOCK_POPUP_PRIVATE", "CLOCK_UNRECOGNIZED"):
        xml, counts = sanitize_reports([bridge_diagnostic_report("PRIVATE_TOKEN bridge_reason=" + reason)], INSTRUMENTATION)
        assert_observed_totals(xml, counts, ["failure"])
        assert "bridge_reason" not in json.loads(ET.fromstring(xml).find("testcase/failure").text)


def test_window_markers_from_unexpected_or_unit_cases_are_not_exported():
    stack = "PRIVATE_TOKEN bridge_windows=[-1,-1,-1,-1,-1,0]"
    for classname, required in (("PRIVATE_CLASS", INSTRUMENTATION), ("org.nous.phonebridge.DebugPayloadBoundsTest", UNIT)):
        xml, counts = sanitize_reports([bridge_diagnostic_report(stack, classname=classname)], required, exact_names=False)
        assert_observed_totals(xml, counts, ["failure"])
        assert b"bridge_windows" not in xml


def test_private_coordinator_failure_accepts_only_one_exact_enum_and_rejects_secrets(tmp_path):
    codes = {"ADB_FAILURE", "DISPLAY_BOUNDS", "REQUEST_EXPIRED", "WRONG_FOCUS", "INVALID_HANDSHAKE", "DEADLINE", "INTERNAL"}
    for code in codes:
        assert restricted_coordinator_failure(json.dumps({"code": code}).encode()) == code
    for payload in (
        None, b"PRIVATE_TOKEN", b"{}", b"null", b"[]", b"[1]", b'[["code","ADB_FAILURE"]]',
        b'[{"code":"ADB_FAILURE"}]', b'{"code":true}', b'{"code":1}', b'{"code":null}',
        b'{"code":{"private":"PRIVATE_TOKEN"}}', b'{"code":["ADB_FAILURE"]}',
        b'{"code":"PRIVATE_TOKEN"}', b'{"code":"ADB_FAILURE_PRIVATE_TOKEN"}',
        b'{"code":"ADB_FAILURE","private":"PRIVATE_TOKEN"}',
        b'{"private":"PRIVATE_TOKEN","code":"ADB_FAILURE"}',
        b'{"code":"PRIVATE_TOKEN","code":"ADB_FAILURE"}',
        b'{"code":"ADB_FAILURE","code":"ADB_FAILURE"}',
        b'{"code":"ADB_FAILURE"} PRIVATE_TOKEN', b'\xff',
        b'{"code":"ADB_FAILURE"}' + b" " * 256,
    ):
        assert restricted_coordinator_failure(payload) is None
    path = tmp_path / "debug-wholeflow-diagnostic.json"
    path.write_bytes(b'{"code":"ADB_FAILURE"}')
    assert restricted_coordinator_failure(read_report(path, max_bytes=256)) == "ADB_FAILURE"
    path.write_bytes(b'{"code":"ADB_FAILURE"}' + b" " * 256)
    assert read_report(path, max_bytes=256) is None
    assert restricted_coordinator_failure(read_report(path, max_bytes=256)) is None


def test_coordinator_failure_is_optional_summary_only_without_changing_receipt_or_counts():
    source = (ROOT / "ci/debug_evidence.py").read_text()
    main = source.split("def main():", 1)[1]
    assert '"debug-wholeflow-diagnostic.json", max_bytes=256' in main
    assert 'coordinator_failure = restricted_coordinator_failure(read_report(' in main
    assert 'if coordinator_failure is not None:\n        summary["coordinator_failure"] = coordinator_failure' in main
    summary = evidence_summary({"complete": False}, {"complete": False}, 1, 0, {"complete": False})
    assert "coordinator_failure" not in summary
    receipt = restricted_wholeflow(json.dumps({"phases": [], "host_adb_taps": 0, "coordinator_error": True}).encode())
    assert receipt["receipt_valid"]
    assert receipt["coordinator_error"] and not receipt["complete"]
    assert "coordinator_failure" not in receipt
