import json
from pathlib import Path
import sys


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
