import json
import os
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET


INSTRUMENTATION = {
    "org.nous.phonebridge.DebugControlInstrumentationTest": {
        "realHttpWsTreeCoordinateTapInputAndSingleSwipeWithIndependentReadback",
        "realPlatformScreenshotApi35OrExplicitApi31Unsupported",
        "localWarningCancellationAndAllFourDurationsRequireConfirmation",
        "localStopAndFreshEnableRejectOldGeneration",
        "ownConsentControlsCannotBeRemotelyReadOrOperated",
        "accessibilitySettingLossRevokesConsentAndPreventsEffects",
        "secureAndPasswordWindowsRefuseCaptureAndEffects",
        "serviceTeardownDoesNotRestoreIndefiniteConsent",
        "realWholeFlowHttpWssGenuineClockHostAdbToggleAndRestartPreserveSurvivor",
    },
}
UNIT = {
    "org.nous.phonebridge.DebugControlTest": {"deadlineRejectsAtExactBoundaryWithoutTimer"},
    "org.nous.phonebridge.DebugPayloadBoundsTest": None,
    "org.nous.phonebridge.DebugExecutionGuardTest": None,
    "org.nous.phonebridge.RemoteWindowPolicyTest": None,
}
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_REPORTS = 64
MAX_CASES = 4096
OUTCOMES = ("failure", "error", "skipped")
WHOLEFLOW_PHASES = [
    "clock_ready", "baseline_listed", "target_created", "survivor_created", "created_restarted",
    "adb_requested", "adb_observed", "adb_restarted", "enabled_restarted", "disabled_restarted",
    "reenabled_restarted", "debug_detail", "debug_deleted", "deleted_restarted", "completed",
]
WHOLEFLOW_OPTIONAL_PHASES = {"promotion_refused", "promotion_dismissed", "failed"}


def restricted_bridge_windows(stack):
    markers = re.findall(r"(?<!\S)bridge_windows=(\S*)", stack)
    if len(markers) != 1:
        return None
    marker = markers[0]
    if len(marker) > 4096 or re.fullmatch(r"\[(?:0|-?[1-9][0-9]{0,9})(?:,(?:0|-?[1-9][0-9]{0,9}))*\]", marker) is None:
        return None
    vector = [int(value) for value in marker[1:-1].split(",")]
    if any(value < -2147483648 or value > 2147483647 for value in vector):
        return None
    if len(vector) < 6 or not 0 <= vector[5] <= 16 or len(vector) != 6 + 12 * vector[5]:
        return None
    if vector[0] < -1 or (vector[0] == -1 and vector[1:5] != [-1] * 4):
        return None
    for offset in range(6, len(vector), 12):
        if vector[offset] < 0 or vector[offset + 1] not in range(1, 7) or vector[offset + 2] not in (0, 1) or vector[offset + 3] not in (0, 1):
            return None
    return vector


def restricted_bridge_barproof(stack):
    markers = re.findall(r"(?<!\S)bridge_barproof=(\S*)", stack)
    if len(markers) != 1:
        return None
    marker = markers[0]
    if len(marker) > 1024 or re.fullmatch(r"\[(?:0|-?[1-9][0-9]{0,9})(?:,(?:0|-?[1-9][0-9]{0,9}))*\]", marker) is None:
        return None
    vector = [int(value) for value in marker[1:-1].split(",")]
    if any(value < -2147483648 or value > 2147483647 for value in vector):
        return None
    if len(vector) < 18 or vector[0] not in (0, 1) or not 0 <= vector[17] <= 16 or len(vector) != 18 + 2 * vector[17]:
        return None
    if any(value < 0 for value in vector[5:17]) or (vector[0] == 0 and vector[1:17] != [0] * 16):
        return None
    identifiers = vector[18::2]
    if len(set(identifiers)) != len(identifiers) or any(value < 0 for value in identifiers) or any(value not in range(5) for value in vector[19::2]):
        return None
    return vector


def sanitize_reports(reports, required, exact_names=True):
    clean = ET.Element("testsuite", {"name": "Restricted observed debug tests"})
    counts = {
        "expected_tests": sum(len(names or ()) for names in required.values()),
        "expected_suites": len(required),
        "observed_tests": 0,
        "passed_tests": 0,
        "failed_tests": 0,
        "error_tests": 0,
        "skipped_tests": 0,
        "missing_tests": 0,
        "missing_suites": 0,
        "duplicate_tests": 0,
        "unexpected_tests": 0,
        "report_validation_failures": 0,
    }
    seen = set()
    seen_classes = set()
    report_count = 0
    for payload in reports:
        report_count += 1
        if report_count > MAX_REPORTS:
            counts["report_validation_failures"] += 1
            break
        try:
            if payload is None or len(payload) > MAX_REPORT_BYTES:
                raise ValueError
            text = payload.decode("utf-8")
            if "<!doctype" in text.lower() or "<!entity" in text.lower() or "\x00" in text:
                raise ValueError
            report = ET.fromstring(text)
            if report.tag not in ("testsuite", "testsuites"):
                raise ValueError
            cases = list(report.iter("testcase"))
            if not cases or len(cases) + counts["observed_tests"] > MAX_CASES:
                raise ValueError
        except (ValueError, UnicodeError, ET.ParseError):
            counts["report_validation_failures"] += 1
            continue
        for suite in report.iter():
            if suite.tag not in ("testsuite", "testsuites"):
                continue
            suite_cases = list(suite.iter("testcase"))
            actual = {"tests": len(suite_cases)}
            actual.update({field: sum(case.find(tag) is not None for case in suite_cases) for field, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))})
            for key, value in actual.items():
                if key in suite.attrib and suite.attrib[key] != str(value):
                    counts["report_validation_failures"] += 1
        for case in cases:
            classname = case.get("classname", "")
            name = case.get("name", "")
            identity = (classname, name)
            valid_name = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is not None
            known = classname in required and valid_name and (not exact_names or required[classname] is None or name in required[classname])
            if identity in seen:
                counts["duplicate_tests"] += 1
            seen.add(identity)
            if known:
                seen_classes.add(classname)
            else:
                counts["unexpected_tests"] += 1
            child = ET.SubElement(clean, "testcase", {"classname": classname if known else "restricted.unexpected", "name": name if known else "unexpected_testcase"})
            marker_counts = {tag: len(case.findall(tag)) for tag in OUTCOMES}
            tags = [tag for tag in OUTCOMES if marker_counts[tag]]
            if sum(marker_counts.values()) > 1:
                counts["report_validation_failures"] += 1
            for tag in tags:
                marker = ET.SubElement(child, tag, {"message": "Restricted observed test outcome"})
                if known and tag in ("failure", "error"):
                    original = case.find(tag)
                    stack = original.text or "" if original is not None else ""
                    files = set(name.rsplit(".", 1)[-1] + ".kt" for name in INSTRUMENTATION | UNIT)
                    files.update({"BridgeAccessibility.kt", "BridgeService.kt", "DebugControl.kt", "RemoteWindowPolicy.kt", "GoogleClockAdapter.kt", "GoogleClockProfile.kt"})
                    locations = list(dict.fromkeys(
                        file + ":" + line for file, line in re.findall(
                            r"(?m)^at org\.nous\.phonebridge\.[A-Za-z0-9_.$]+\(([A-Za-z0-9_]+\.kt):([0-9]{1,6})\)\s*$", stack
                        ) if file in files
                    ))[:8]
                    codes = re.findall(r"\bbridge_code=([A-Z_]+)\b", stack)
                    code = codes[0] if codes and codes[0] in {"UNSUPPORTED", "PERMISSION_REQUIRED", "FOREGROUND_REQUIRED", "PHONE_LOCKED", "AMBIGUOUS_STATE", "VALIDATION_ERROR", "NOT_FOUND", "EXPIRED", "INTERNAL_ERROR"} else None
                    diagnostic = {"locations": locations, "bridge_code": code}
                    reasons = re.findall(r"\bbridge_reason=([A-Z_]+)\b", stack)
                    if reasons and reasons[0] in {"SYSTEM_WINDOW", "MISSING_WINDOW_ROOT", "PASSWORD_WINDOW", "INCOMPLETE_TREE", "FOREIGN_OWNER", "OVERLAP", "OWN_UI", "PROTECTED_UI", "INVALID_ACTIVE_WINDOW", "INVALID_WINDOW_GRAPH", "LOCAL_MAIN", "UNCLASSIFIED", "CLOCK_MISSING_ROOT", "CLOCK_POPUP", "CLOCK_WINDOW_GRAPH", "CLOCK_WINDOW_CHANGED", "CLOCK_INCOMPLETE_TREE", "CLOCK_STALE_NODE", "CLOCK_FOREIGN_NODE", "CLOCK_DETAIL_OR_LOADING", "CLOCK_COLLECTION_ANCHOR", "CLOCK_COLLECTION_COUNT", "CLOCK_COLLECTION_SCHEMA", "CLOCK_COLLECTION_INCOMPLETE", "CLOCK_COLLECTION_GEOMETRY", "CLOCK_ALARM_IDENTITY", "CLOCK_ALARM_SCHEMA", "CLOCK_ALARM_SCHEDULE"}:
                        diagnostic["bridge_reason"] = reasons[0]
                    if classname in INSTRUMENTATION:
                        windows = restricted_bridge_windows(stack)
                        if windows is not None:
                            diagnostic["bridge_windows"] = windows
                        barproof = restricted_bridge_barproof(stack)
                        if barproof is not None:
                            diagnostic["bridge_barproof"] = barproof
                        local = re.findall(r"(?<!\S)bridge_local=(\S*)", stack)
                        if len(local) == 1 and re.fullmatch(r"\[[01],[01],[01],[0-3],[0-3],[01],[0-2]\]", local[0]):
                            diagnostic["bridge_local"] = [int(value) for value in local[0][1:-1].split(",")]
                    marker.text = json.dumps(diagnostic, sort_keys=True)
            counts["observed_tests"] += 1
            counts["passed_tests"] += not tags
            counts["failed_tests"] += "failure" in tags
            counts["error_tests"] += "error" in tags
            counts["skipped_tests"] += "skipped" in tags
    if report_count == 0:
        counts["report_validation_failures"] += 1
    counts["missing_tests"] = sum((classname, name) not in seen for classname, names in required.items() for name in names or ())
    counts["missing_suites"] = len(set(required) - seen_classes)
    counts["complete"] = not any(counts[field] for field in (
        "missing_tests", "missing_suites", "duplicate_tests", "unexpected_tests",
        "report_validation_failures", "failed_tests", "error_tests", "skipped_tests",
    ))
    for attribute, field in (("tests", "observed_tests"), ("failures", "failed_tests"), ("errors", "error_tests"), ("skipped", "skipped_tests")):
        clean.set(attribute, str(counts[field]))
    return ET.tostring(clean, encoding="utf-8", xml_declaration=True), counts


def restricted_wholeflow(payload):
    result = {"receipt_valid": False, "phases": [], "host_adb_taps": 0, "coordinator_error": None, "complete": False}
    try:
        if payload is None or len(payload) > 4096:
            return result
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"phases", "host_adb_taps", "coordinator_error"}:
            return result
        phases = value["phases"]
        if not isinstance(phases, list) or len(phases) > 32 or any(not isinstance(phase, str) or phase not in WHOLEFLOW_PHASES + list(WHOLEFLOW_OPTIONAL_PHASES) for phase in phases):
            return result
        if type(value["host_adb_taps"]) is not int or value["host_adb_taps"] not in (0, 1) or type(value["coordinator_error"]) is not bool:
            return result
        result.update(value, receipt_valid=True)
        result["complete"] = [phase for phase in phases if phase not in {"promotion_refused", "promotion_dismissed"}] == WHOLEFLOW_PHASES and value["host_adb_taps"] == 1 and not value["coordinator_error"]
        return result
    except (ValueError, UnicodeError):
        return result


def restricted_coordinator_failure(payload):
    try:
        if payload is None or len(payload) > 256:
            return None
        value = json.loads(payload, object_pairs_hook=list)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], tuple) or value[0][0] != "code":
            return None
        code = value[0][1]
        if isinstance(code, str) and code in {"ADB_FAILURE", "DISPLAY_BOUNDS", "REQUEST_EXPIRED", "WRONG_FOCUS", "INVALID_HANDSHAKE", "DEADLINE", "INTERNAL"}:
            return code
    except (ValueError, UnicodeError):
        pass
    return None


def evidence_summary(instrumentation, unit, primary_status, cleanup_status, wholeflow):
    return {
        "suite": "DebugControlInstrumentationTest",
        "instrumentation": instrumentation,
        "unit": unit,
        "complete": instrumentation["complete"] and unit["complete"] and wholeflow["complete"] and primary_status == 0 and cleanup_status == 0,
        "wholeflow": wholeflow,
        "primary_process_exit": primary_status,
        "cleanup_exit": cleanup_status,
        "physical_device_claim": False,
        "expiry_e2e": "not_exercised_no_production_clock_backdoor",
        "expiry_unit": "deadlineRejectsAtExactBoundaryWithoutTimer_10_15_60_minutes",
        "api35_capture": "real_JPEG_pixels_and_native_bounds_required",
        "api31_capture": "server_enqueue_HTTP422_UNSUPPORTED_unadvertised_no_phone_refusal_claim",
    }


def read_report(path, max_bytes=MAX_REPORT_BYTES):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                return None
            payload = source.read(max_bytes + 1)
            return payload if len(payload) <= max_bytes else None
    except OSError:
        return None


def main():
    root = Path.cwd()
    output = Path(os.environ["RUNNER_TEMP"]) / "debug-control-evidence"
    output.mkdir(exist_ok=True)
    os.chmod(output, 0o700)
    instrumentation_paths = (root / "android/app/build/outputs/androidTest-results/connected").rglob("TEST-*.xml")
    instrumentation_xml, instrumentation = sanitize_reports((read_report(path) for path in instrumentation_paths), INSTRUMENTATION)
    unit_paths = (root / "android/app/build/test-results/testDebugUnitTest" / ("TEST-" + classname + ".xml") for classname in UNIT)
    unit_xml, unit = sanitize_reports((read_report(path) for path in unit_paths), UNIT, exact_names=False)
    wholeflow = restricted_wholeflow(read_report(Path(os.environ["RUNNER_TEMP"]) / "debug-wholeflow-host.json"))
    summary = evidence_summary(instrumentation, unit, int(os.environ["DEBUG_CONTROL_PRIMARY_STATUS"]), int(os.environ["DEBUG_CONTROL_CLEANUP_STATUS"]), wholeflow)
    coordinator_failure = restricted_coordinator_failure(read_report(Path(os.environ["RUNNER_TEMP"]) / "debug-wholeflow-diagnostic.json", max_bytes=256))
    if coordinator_failure is not None:
        summary["coordinator_failure"] = coordinator_failure
    for name, payload in (
        ("debug-control-junit.xml", instrumentation_xml),
        ("debug-control-unit-junit.xml", unit_xml),
        ("debug-control-summary.log", (json.dumps(summary, sort_keys=True) + "\n").encode("utf-8")),
    ):
        destination = output / name
        destination.write_bytes(payload)
        os.chmod(destination, 0o600)
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
