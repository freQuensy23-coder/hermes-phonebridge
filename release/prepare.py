import argparse
import hashlib
import json
import os
import platform
import re
import shutil
from pathlib import Path


def ensure(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(python_dist, apk, output, locks, context):
    wheels = ("phonebridge_cli-0.1.0-py3-none-any.whl", "phonebridge_server-0.1.0-py3-none-any.whl")
    dist = Path(python_dist)
    expected_dist = {*wheels, "phonebridge_cli-0.1.0.tar.gz", "phonebridge_server-0.1.0.tar.gz"}
    ensure(not dist.is_symlink() and dist.is_dir() and {path.name for path in dist.iterdir()} == expected_dist, "Unexpected Python dist layout")
    for path in dist.iterdir():
        ensure(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= 16 * 1024 * 1024, "Invalid Python distribution")
    apk = Path(apk)
    ensure(apk.name == "app-release-unsigned.apk" and not apk.is_symlink() and apk.is_file() and 0 < apk.stat().st_size <= 96 * 1024 * 1024, "Invalid unsigned APK")
    ensure(context["GITHUB_REPOSITORY"] == "freQuensy23-coder/hermes-phonebridge", "Unexpected repository")
    sha = context["GITHUB_SHA"]
    ensure(re.fullmatch("[0-9a-f]{40}", sha) is not None, "Invalid source SHA")
    ensure(re.fullmatch("[1-9][0-9]*", context["GITHUB_RUN_ID"]) is not None and re.fullmatch("[1-9][0-9]*", context["GITHUB_RUN_ATTEMPT"]) is not None, "Invalid CI identity")
    dependency_inputs = {}
    ensure(1 <= len(locks) <= 20, "Dependency inputs required")
    for name in locks:
        ensure(re.fullmatch("[a-zA-Z0-9_./-]{1,200}", name) is not None and not name.startswith("/") and ".." not in name.split("/") and name not in dependency_inputs, "Invalid dependency input")
        path = Path(name)
        ensure(not path.is_symlink() and path.is_file() and 0 < path.stat().st_size <= 1024 * 1024, "Invalid dependency input file")
        dependency_inputs[name] = digest(path)
    output = Path(output)
    ensure(output.is_absolute() and not output.exists(), "Output must be a fresh absolute directory")
    output.mkdir(mode=0o700)
    for name in wheels:
        shutil.copyfile(dist / name, output / name)
    shutil.copyfile(apk, output / "phonebridge-android-unsigned.apk")
    files = {path.name: {"size": path.stat().st_size, "sha256": digest(path)} for path in output.iterdir()}
    manifest = {"schema": 1, "version": "0.1.0", "repository": context["GITHUB_REPOSITORY"], "source_sha": sha, "run_id": int(context["GITHUB_RUN_ID"]), "run_attempt": int(context["GITHUB_RUN_ATTEMPT"]), "files": files, "dependency_inputs": dependency_inputs, "toolchain": {"python": platform.python_version(), "runner_os": context["RUNNER_OS"], "runner_arch": context["RUNNER_ARCH"], "java_home_17": context["JAVA_HOME_17_X64"], "android_home": context["ANDROID_HOME"]}}
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    for path in output.iterdir():
        path.chmod(0o600)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-dist", required=True)
    parser.add_argument("--apk", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--lock", action="append", required=True)
    args = parser.parse_args()
    prepare(args.python_dist, args.apk, args.output, args.lock, os.environ)


if __name__ == "__main__":
    main()
