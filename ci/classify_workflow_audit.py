import hashlib
import json
from pathlib import Path
import sys


REVIEWED_DEPLOY_WORKFLOW_SHA256 = "df3e0b60a11f537754eb7e543a33f114043b813bed80d44967aa65d9eec1b2e4"
TRIGGER_REASON = "The reviewed workflow_run job never checks out or executes upstream code/artifacts. It authenticates the exact repository, CI workflow path, successful push/main run, owner actors and current main before passing a SHA to a separately restricted host gate. The complete workflow bytes are pinned here; any change requires re-review. The generic high-severity trigger finding remains visible."
EXCEPTIONS = {
    (".github/workflows/deploy.yml", "deploy"): "Private environment protection is unavailable. Only owner-originated completed main CI is accepted; the restricted host command independently verifies the merged PR and coherent CI producer.",
    (".github/workflows/release.yml", "sign"): "Private environment protection is unavailable. Signing is isolated from build execution on a fresh runner, gated by owner-merged source and accepted CI provenance.",
    (".github/workflows/release.yml", "publish"): "Private environment protection is unavailable. A fresh publisher receives only validated inert release assets and an ephemeral least-scope publication token.",
}
SEVERITIES = {"Informational", "Low", "Medium", "High"}


def scopes(finding):
    found = set()
    for location in finding.get("locations", []):
        symbolic = location.get("symbolic", {})
        local = symbolic.get("key", {}).get("Local", {})
        path = local.get("verbatim_path", "")
        if path.startswith("./"):
            path = path[2:]
        route = symbolic.get("route", {}).get("route", [])
        for index, part in enumerate(route[:-1]):
            if part == {"Key": "jobs"} and "Key" in route[index + 1]:
                found.add((path, route[index + 1]["Key"]))
    return found


def reviewed_trigger(finding):
    if finding["ident"] != "dangerous-triggers" or finding["determinations"]["severity"] != "High":
        return False
    paths = set()
    for location in finding.get("locations", []):
        path = location.get("symbolic", {}).get("key", {}).get("Local", {}).get("verbatim_path", "")
        paths.add(path[2:] if path.startswith("./") else path)
    if paths != {".github/workflows/deploy.yml"}:
        return False
    source = Path(__file__).resolve().parents[1] / ".github/workflows/deploy.yml"
    return not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest() == REVIEWED_DEPLOY_WORKFLOW_SHA256


def classify(findings):
    if not isinstance(findings, list):
        raise ValueError("Workflow audit must be a complete JSON-v1 findings array")
    result = {"blocking": [], "documented_exceptions": [], "non_blocking": []}
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Malformed workflow audit finding")
        severity = finding.get("determinations", {}).get("severity")
        if severity not in SEVERITIES or not isinstance(finding.get("ident"), str):
            raise ValueError("Unknown workflow audit schema or severity")
        if severity in {"Informational", "Low"}:
            result["non_blocking"].append(finding)
            continue
        if reviewed_trigger(finding):
            result["documented_exceptions"].append({"finding": finding, "reasons": [TRIGGER_REASON]})
            continue
        locations = scopes(finding)
        if (
            severity == "Medium"
            and finding["ident"] == "secrets-outside-env"
            and locations
            and all(location in EXCEPTIONS for location in locations)
        ):
            result["documented_exceptions"].append({
                "finding": finding,
                "reasons": sorted({EXCEPTIONS[location] for location in locations}),
            })
        else:
            result["blocking"].append(finding)
    return result


def main():
    source = Path(sys.argv[1])
    if source.stat().st_size > 8_000_000:
        raise ValueError("Workflow audit report exceeds limit")
    result = classify(json.loads(source.read_text()))
    Path("workflow-audit-classified.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({key: len(value) for key, value in result.items()}))
    for exception in result["documented_exceptions"]:
        print(json.dumps({"rule": exception["finding"]["ident"], "reasons": exception["reasons"]}))
    if result["blocking"]:
        raise SystemExit("Unresolved workflow security findings")


if __name__ == "__main__":
    main()
