import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

from .gate import GitHub, REPOSITORY, RELEASE_PATH, OWNER, require, integer, attempt_jobs, bind_artifact, current_binding, artifact_bytes
from .payload import WHEELS, UNSIGNED, SIGNED, INPUT_NAMES, PUBLIC_NAMES, canonical, read_json, extract_contract, file_record, verify_input, verify_public, certificate_fingerprint, expected_certificate


def temporary_root():
    root = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(root.is_absolute() and root.is_dir(), "Missing runner temporary root")
    return root


def identity():
    return {"workflow_path": RELEASE_PATH, "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"], "run_id": int(os.environ["GITHUB_RUN_ID"]), "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])}


def inputs(api, binding, prefix):
    parent = Path(tempfile.mkdtemp(prefix=prefix, dir=temporary_root()))
    directory = extract_contract(artifact_bytes(api, binding["input_artifact_id"], binding["input_artifact_digest"]), parent / "input", INPUT_NAMES)
    manifest = verify_input(directory, binding)
    return directory, manifest


def api_client():
    return GitHub(os.environ["GH_TOKEN"])


def promote():
    api = api_client()
    binding = current_binding(api)
    directory, manifest = inputs(api, binding, "promote-")
    require(manifest["source_sha"] == binding["source_sha"], "Input source mismatch")
    require(current_binding(api) == binding, "Provenance moved during promotion")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("binding=" + canonical(binding).decode())
    shutil.rmtree(directory.parent)


def android_tools():
    home = Path(os.environ["ANDROID_HOME"])
    java = Path(os.environ["JAVA_HOME_17_X64"])
    require(home.is_absolute() and java.is_absolute(), "Unexpected hosted toolchain path")
    tools = home / "build-tools" / "35.0.0"
    for name in ("zipalign", "apksigner", "aapt"):
        require((tools / name).is_file() and os.access(tools / name, os.X_OK), "Preinstalled Android build-tools 35.0.0 required")
    require((java / "bin" / "java").is_file(), "Preinstalled JDK17 required")
    environment = {"PATH": str(java / "bin") + ":/usr/bin:/bin", "JAVA_HOME": str(java), "HOME": str(temporary_root()), "LANG": "C.UTF-8"}
    return tools, environment


def tool(arguments, environment, allowed_failure=False):
    result = subprocess.run([str(value) for value in arguments], env=environment, cwd=temporary_root(), capture_output=True, text=True, timeout=120)
    require(len(result.stdout) <= 128 * 1024 and len(result.stderr) <= 128 * 1024, "Unexpected Android tool output")
    require(allowed_failure or result.returncode == 0, "Android tool failed")
    return result


def signing_preflight():
    api = api_client()
    binding = current_binding(api)
    expected_certificate(os.environ["EXPECTED_CERTIFICATE"])
    directory, manifest = inputs(api, binding, "sign-input-")
    tools, environment = android_tools()
    badging = tool([tools / "aapt", "dump", "badging", directory / UNSIGNED], environment).stdout
    require(any(line == "package: name='org.nous.phonebridge' versionCode='1' versionName='0.1.0'" or line.startswith("package: name='org.nous.phonebridge' versionCode='1' versionName='0.1.0' ") for line in badging.splitlines()), "APK package/version does not match tag")
    unsigned = tool([tools / "apksigner", "verify", directory / UNSIGNED], environment, allowed_failure=True)
    require(unsigned.returncode != 0, "CI input APK is already signed")
    aligned = directory.parent / "aligned.apk"
    tool([tools / "zipalign", "-p", "-f", "4", directory / UNSIGNED, aligned], environment)
    state = {"binding": binding, "directory": str(directory), "aligned": file_record(aligned), "manifest": manifest}
    (temporary_root() / "sign-state.json").write_bytes(canonical(state))


def sign():
    os.umask(0o077)
    api = api_client()
    binding = current_binding(api)
    fingerprint = expected_certificate(os.environ["EXPECTED_CERTIFICATE"])
    state = read_json((temporary_root() / "sign-state.json").read_bytes())
    require(state["binding"] == binding, "Signing binding changed")
    directory = Path(state["directory"])
    require(directory.resolve().is_relative_to(temporary_root()) and directory.name == "input", "Signing input escaped runner temp")
    manifest = verify_input(directory, binding)
    require(manifest == state["manifest"], "Signing input changed")
    aligned = directory.parent / "aligned.apk"
    require(file_record(aligned) == state["aligned"], "Aligned APK changed")
    output = temporary_root() / "signed"
    require(not output.exists(), "Signing output must be fresh")
    output.mkdir(mode=0o700)
    tools, environment = android_tools()
    key = os.environ["SIGNING_KEY"]
    password = os.environ["SIGNING_PASSWORD"]
    require(0 < len(key) <= 128 * 1024 and 0 < len(password) <= 1024, "Missing/oversized signing credentials")
    decoded = base64.b64decode(key, validate=True)
    require(0 < len(decoded) <= 96 * 1024, "Keystore size exceeded")
    descriptor, keyname = tempfile.mkstemp(prefix="keystore-", suffix=".jks", dir=temporary_root())
    try:
        with os.fdopen(descriptor, "wb") as keyfile:
            keyfile.write(decoded)
        signing_environment = dict(environment, SIGNING_PASSWORD=password)
        tool([tools / "apksigner", "sign", "--ks", keyname, "--ks-key-alias", "phonebridge", "--ks-pass", "env:SIGNING_PASSWORD", "--key-pass", "env:SIGNING_PASSWORD", "--out", output / SIGNED, aligned], signing_environment)
        verified = tool([tools / "apksigner", "verify", "--verbose", "--print-certs", output / SIGNED], environment)
        require(certificate_fingerprint(verified.stdout) == fingerprint, "APK certificate differs from pinned signer")
        tool([tools / "zipalign", "-c", "-p", "4", output / SIGNED], environment)
    finally:
        Path(keyname).unlink(missing_ok=True)
    for name in WHEELS:
        shutil.copyfile(directory / name, output / name)
    provenance = {"schema": 1, "version": "0.1.0", "binding": binding, "ci_input": manifest, "signer_sha256": fingerprint, "release": identity(), "files": {name: file_record(output / name) for name in (SIGNED, *WHEELS)}, "wheel_signatures": "not-implemented"}
    (output / "provenance.json").write_bytes(canonical(provenance))
    names = sorted({SIGNED, *WHEELS, "provenance.json"})
    (output / "SHA256SUMS").write_text("".join(file_record(output / name)["sha256"] + "  " + name + "\n" for name in names))
    verify_public(output, binding, manifest, fingerprint, identity())
    require(current_binding(api) == binding, "Source moved while signing")
    shutil.rmtree(directory.parent)
    (temporary_root() / "sign-state.json").unlink()


def signed_input(api):
    run = api.get("actions/runs/" + str(integer(identity()["run_id"])))
    require(run["repository"]["full_name"] == REPOSITORY and run["head_repository"]["full_name"] == REPOSITORY, "Signed artifact repository mismatch")
    require(run["head_sha"] == identity()["workflow_sha"] and run["head_branch"] == "main" and run["event"] == "workflow_dispatch", "Signed artifact workflow source mismatch")
    require(run["run_attempt"] == identity()["run_attempt"] and run["actor"]["login"] == OWNER and run["triggering_actor"]["login"] == OWNER, "Signed artifact attempt/actor mismatch")
    workflow = api.get("actions/workflows/release.yml")
    require(workflow["path"] == RELEASE_PATH and run["workflow_id"] == workflow["id"] and run["path"] in (RELEASE_PATH, RELEASE_PATH + "@refs/heads/main"), "Signed artifact workflow path/ID mismatch")
    jobs = attempt_jobs(api, run)
    producers = [job for job in jobs if job["name"] == "sign"]
    require(len(producers) == 1 and producers[0]["status"] == "completed" and producers[0]["conclusion"] == "success", "Successful signing job required")
    name = "phonebridge-signed-" + str(run["id"]) + "-" + str(run["run_attempt"])
    artifact = bind_artifact(api, run, name, producers[0])
    require(str(artifact["id"]) == os.environ["SIGNED_ARTIFACT_ID"] and artifact["digest"] == "sha256:" + os.environ["SIGNED_ARTIFACT_DIGEST"], "Signed transfer artifact ID/digest changed")
    parent = Path(tempfile.mkdtemp(prefix="publish-", dir=temporary_root()))
    return extract_contract(artifact_bytes(api, artifact["id"], artifact["digest"]), parent / "assets", PUBLIC_NAMES)


def upload_response(api, path, method, data, upload=False):
    with api.request("/repos/" + REPOSITORY + "/" + path, method=method, data=data, upload=upload) as response:
        body = response.read(1024 * 1024 + 1)
    require(len(body) <= 1024 * 1024, "Oversized write response")
    return read_json(body)


def release_absent(api, tag):
    require(not any(release["tag_name"] == tag for release in api.pages("releases")), "Existing published or draft release will not be overwritten")
    try:
        api.get("releases/tags/" + tag)
    except urllib.error.HTTPError as error:
        require(error.code == 404, "Release absence not established")
        return
    raise ValueError("Existing release will not be overwritten")


def verify_remote_release(api, release_id, directory, binding, draft):
    release = api.get("releases/" + str(integer(release_id)))
    require(release["id"] == release_id and release["tag_name"] == binding["tag"] and release["target_commitish"] == binding["source_sha"], "Release identity mismatch")
    require(release["draft"] is draft and release["prerelease"] is False and release["author"]["login"] in (OWNER, "github-actions[bot]"), "Release state/author mismatch")
    assets = api.pages("releases/" + str(release_id) + "/assets")
    require(len(assets) == len(PUBLIC_NAMES) and {asset["name"] for asset in assets} == PUBLIC_NAMES, "Published assets mismatch")
    for asset in assets:
        expected = file_record(directory / asset["name"])
        require(asset["state"] == "uploaded" and asset["size"] == expected["size"], "Remote asset state/size mismatch")
        data = api.download("releases/assets/" + str(integer(asset["id"])), limit=expected["size"])
        require(hashlib.sha256(data).hexdigest() == expected["sha256"], "Remote asset hash mismatch")
    return release


def publish():
    api = api_client()
    binding = current_binding(api)
    fingerprint = expected_certificate(os.environ["EXPECTED_CERTIFICATE"])
    input_directory, manifest = inputs(api, binding, "publish-input-")
    directory = signed_input(api)
    verify_public(directory, binding, manifest, fingerprint, identity())
    release_absent(api, binding["tag"])
    require(current_binding(api) == binding, "Tag/source/CI moved before publication")
    release = upload_response(api, "releases", "POST", {"tag_name": binding["tag"], "target_commitish": binding["source_sha"], "name": "PhoneBridge " + binding["tag"], "body": "Stable-key signed Android APK and exact accepted CI wheels. SHA256SUMS and provenance.json bind source, CI producer and signer. Wheels are not cryptographically signed.", "draft": True, "prerelease": False, "generate_release_notes": False})
    release_id = integer(release["id"])
    readback = api.get("releases/" + str(release_id))
    require(readback["draft"] is True and readback["tag_name"] == binding["tag"] and readback["target_commitish"] == binding["source_sha"], "Draft creation readback mismatch")
    for name in sorted(PUBLIC_NAMES):
        require(current_binding(api) == binding, "Tag/source/CI changed during upload")
        uploaded = upload_response(api, "releases/" + str(release_id) + "/assets?name=" + urllib.parse.quote(name, safe=""), "POST", (directory / name).read_bytes(), upload=True)
        asset = api.get("releases/assets/" + str(integer(uploaded["id"])))
        require(asset["name"] == name and asset["state"] == "uploaded" and asset["size"] == file_record(directory / name)["size"], "Asset upload readback mismatch")
    verify_remote_release(api, release_id, directory, binding, True)
    require(current_binding(api) == binding, "Tag/source/CI moved before draft promotion")
    upload_response(api, "releases/" + str(release_id), "PATCH", {"draft": False, "prerelease": False, "tag_name": binding["tag"], "target_commitish": binding["source_sha"]})
    published = verify_remote_release(api, release_id, directory, binding, False)
    require(current_binding(api) == binding, "Tag/source/CI moved after publication")
    print("Verified release " + published["html_url"])
    shutil.rmtree(directory.parent)
    shutil.rmtree(input_directory.parent)
