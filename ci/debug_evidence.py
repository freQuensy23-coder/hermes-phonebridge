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
                    files.update({"BridgeAccessibility.kt", "BridgeService.kt", "DebugControl.kt", "RemoteWindowPolicy.kt"})
                    locations = list(dict.fromkeys(
                        file + ":" + line for file, line in re.findall(
                            r"(?m)^at org\.nous\.phonebridge\.[A-Za-z0-9_.$]+\(([A-Za-z0-9_]+\.kt):([0-9]{1,6})\)\s*$", stack
                        ) if file in files
                    ))[:8]
                    codes = re.findall(r"\bbridge_code=([A-Z_]+)\b", stack)
                    code = codes[0] if codes and codes[0] in {"UNSUPPORTED", "PERMISSION_REQUIRED", "FOREGROUND_REQUIRED", "PHONE_LOCKED", "AMBIGUOUS_STATE", "VALIDATION_ERROR", "NOT_FOUND", "EXPIRED", "INTERNAL_ERROR"} else None
                    diagnostic = {"locations": locations, "bridge_code": code}
                    reasons = re.findall(r"\bbridge_reason=([A-Z_]+)\b", stack)
                    if reasons and reasons[0] in {"SYSTEM_WINDOW", "MISSING_WINDOW_ROOT", "PASSWORD_WINDOW", "INCOMPLETE_TREE", "FOREIGN_OWNER", "OVERLAP", "OWN_UI", "PROTECTED_UI", "INVALID_ACTIVE_WINDOW", "INVALID_WINDOW_GRAPH", "UNCLASSIFIED"}:
                        diagnostic["bridge_reason"] = reasons[0]
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


def read_report(path):
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_REPORT_BYTES:
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_REPORT_BYTES:
                return None
            payload = source.read(MAX_REPORT_BYTES + 1)
            return payload if len(payload) <= MAX_REPORT_BYTES else None
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
