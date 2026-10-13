import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
import zipfile

VERSION = "0.1.0"
REPOSITORY = "freQuensy23-coder/hermes-phonebridge"
WORKFLOW = ".github/workflows/ci.yml"
TRUST_FILES = ("bootstrap.py", "rollout.py", "phonebridge.service", "provision_https.py")
MAX_WHEEL = 128 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wheel_identity(path):
    if (not stat.S_ISREG(path.lstat().st_mode) or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", path.name)
            or not 0 < path.stat().st_size <= MAX_WHEEL):
        raise RuntimeError("Expected bounded regular wheel, no source distributions")
    with zipfile.ZipFile(path) as archive:
        metadata = [entry for entry in archive.infolist() if entry.filename.endswith(".dist-info/METADATA")]
        wheels = [entry for entry in archive.infolist() if entry.filename.endswith(".dist-info/WHEEL")]
        if len(metadata) != 1 or len(wheels) != 1 or metadata[0].file_size > 65536 or wheels[0].file_size > 65536:
            raise RuntimeError("Invalid wheel metadata")
        data = BytesParser().parsebytes(archive.read(metadata[0]))
        tags = BytesParser().parsebytes(archive.read(wheels[0])).get_all("Tag", [])
        if not tags or any(not re.fullmatch(r"[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+", tag) for tag in tags):
            raise RuntimeError("Wheel requires explicit Python/ABI/platform tags")
        name, version = data.get("Name", ""), data.get("Version", "")
        normalized = re.sub(r"[-_.]+", "_", name).lower()
        if not re.fullmatch(r"[a-z0-9_]+", normalized) or not version or not path.name.startswith(normalized + "-" + version.replace("-", "_") + "-"):
            raise RuntimeError("Wheel filename/metadata mismatch")
        if normalized in {"phonebridge_server", "phonebridge_cli"} and version != VERSION:
            raise RuntimeError("Project wheel version must be 0.1.0")
        return normalized, version


def package(wheelhouse, destination, source, environment):
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("CI deployment wheelhouse requires CPython 3.11")
    sha = environment.get("GITHUB_SHA", "")
    if (not re.fullmatch(r"[0-9a-f]{40}", sha) or environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_EVENT_NAME") != "push" or environment.get("GITHUB_REF") != "refs/heads/main"
            or environment.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/{WORKFLOW}@refs/heads/main"):
        raise RuntimeError("Deployment artifacts must be produced by exact push/main CI")
    producer = {"repository": REPOSITORY, "workflow_path": WORKFLOW, "event": "push", "ref": "refs/heads/main"}
    for name, variable in (("repository_id", "GITHUB_REPOSITORY_ID"), ("workflow_id", "PHONEBRIDGE_CI_WORKFLOW_ID"), ("run_id", "GITHUB_RUN_ID"), ("run_attempt", "GITHUB_RUN_ATTEMPT")):
        value = environment.get(variable, "")
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise RuntimeError("Missing exact CI producer identifiers")
        producer[name] = int(value)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink() or destination.exists():
        raise RuntimeError("Use existing wheelhouse and unique absent artifact destination")
    paths = sorted(wheelhouse.iterdir())
    if not 2 <= len(paths) < 256 or sum(path.stat().st_size for path in paths) > 512 * 1024 * 1024:
        raise RuntimeError("Wheelhouse bounds exceeded")
    names, wheels = set(), {}
    for path in paths:
        name, _ = wheel_identity(path)
        if name in names:
            raise RuntimeError("Duplicate distribution in offline wheelhouse")
        names.add(name)
        wheels["wheelhouse/" + path.name] = sha256(path)
    if not {"phonebridge_server", "phonebridge_cli"} <= names:
        raise RuntimeError("Both project wheels required")
    trust = {}
    for name in TRUST_FILES:
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError("Expected regular deployment trust source")
        trust[name] = sha256(path)
    manifest = {"format": 1, "source_sha": sha, "version": VERSION, "schema_generation": 1,
                "artifact_name": "phonebridge-deployment-" + sha, "producer": producer, "runtime_python": "cpython-3.11",
                "trust": trust, "wheels": wheels, "toolchain": {"python": sys.version.split()[0], "platform": platform.platform()}}
    destination.mkdir(mode=0o700, parents=True)
    try:
        shutil.copytree(wheelhouse, destination / "wheelhouse")
        (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = package(args.wheelhouse, args.output, Path(__file__).resolve().parent, os.environ)
    print(json.dumps({"artifact_name": manifest["artifact_name"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
