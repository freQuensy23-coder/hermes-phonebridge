import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import uuid
import zipfile

REPOSITORY = "freQuensy23-coder/hermes-phonebridge"
OWNER = "freQuensy23-coder"
WORKFLOW = ".github/workflows/ci.yml"
VERSION = "0.1.0"
RUNTIME_PYTHON = str(Path.home() / ".local/bin/python3.11")
REQUIRED_JOBS = {"security", "python", "android-build", "android-e2e (31)", "android-e2e (35)", "android-physical"}
TRUST_FILES = {"bootstrap.py": "bin/bootstrap.py", "rollout.py": "bin/deploy_entry.py", "phonebridge.service": "unit", "provision_https.py": "bin/provision_https.py"}
BASE = Path.home() / ".local/share/phonebridge"
CONFIG = Path.home() / ".config/phonebridge/server.env"
UNIT = Path.home() / ".config/systemd/user/phonebridge.service"
SCRATCH = Path.home() / ".hermes/cache/scratch"
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_ENTRIES = 256
MAX_JSON = 65536
DEADLINE = None


def remaining(limit=30):
    value = limit if DEADLINE is None else min(limit, DEADLINE - time.monotonic())
    if value <= 0:
        raise TimeoutError("Deployment deadline exceeded")
    return value


def run(*args, timeout=30, capture_output=False, text=False, **kwargs):
    SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryFile(dir=SCRATCH) as output:
        process = subprocess.Popen(args, stdout=output, stderr=subprocess.DEVNULL, start_new_session=True, **kwargs)
        try:
            code = process.wait(timeout=remaining(timeout))
            if code:
                raise subprocess.CalledProcessError(code, args)
        except BaseException as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            if isinstance(error, subprocess.TimeoutExpired):
                raise TimeoutError("Bounded subprocess deadline exceeded") from None
            raise
        result = subprocess.CompletedProcess(args, code)
        if capture_output:
            if output.tell() > 8 * 1024 * 1024:
                raise RuntimeError("Subprocess output limit exceeded")
            output.seek(0)
            result.stdout = output.read()
            if text:
                result.stdout = result.stdout.decode("utf-8")
        return result


@contextmanager
def network_deadline(timeout):
    started = time.monotonic()
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(number, frame):
        raise TimeoutError("Network wall-clock deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


def github(path):
    return json.loads(run("gh", "api", "--method", "GET", "-H", "X-GitHub-Api-Version: 2022-11-28", path, capture_output=True, text=True).stdout)


def pages(path, key=None):
    result = []
    total = None
    identifiers = set()
    for page in range(1, 21):
        data = github(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        items = data if key is None else data[key]
        if not isinstance(items, list) or len(items) > 100:
            raise RuntimeError("Invalid paginated collection")
        if key is not None:
            count = data.get("total_count")
            if type(count) is not int or count < 0 or count > 2000 or total not in (None, count):
                raise RuntimeError("Changing or excessive pagination total")
            total = count
        for item in items:
            identity = item.get("id", item.get("number"))
            if type(identity) is not int or identity in identifiers:
                raise RuntimeError("Ambiguous paginated identity")
            identifiers.add(identity)
            result.append(item)
        if len(items) < 100:
            if total is not None and len(result) != total:
                raise RuntimeError("Incomplete paginated collection")
            return result
    raise RuntimeError("Pagination limit exceeded")


def valid_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuntimeError("Invalid lowercase source SHA")
    return value


def positive(value):
    if type(value) is not int or value < 1:
        raise RuntimeError("Invalid producer identity")
    return value


def current_main(revision):
    valid_sha(revision)
    if github(f"repos/{REPOSITORY}/commits/main")["sha"] != revision:
        raise RuntimeError("Revision is not current main")


def accepted_pr(revision):
    accepted = []
    for item in pages(f"repos/{REPOSITORY}/commits/{revision}/pulls"):
        pull = github(f"repos/{REPOSITORY}/pulls/{positive(item['number'])}")
        base, head = pull.get("base") or {}, pull.get("head") or {}
        if (pull.get("merged") is True and pull.get("state") == "closed"
                and pull.get("merge_commit_sha") == revision and base.get("ref") == "main"
                and (base.get("repo") or {}).get("full_name") == REPOSITORY
                and (head.get("repo") or {}).get("full_name") == REPOSITORY
                and (pull.get("user") or {}).get("login") == OWNER
                and (pull.get("merged_by") or {}).get("login") == OWNER):
            accepted.append(pull["number"])
    if len(accepted) != 1:
        raise RuntimeError("Revision requires exactly one owner-merged same-repository pull request")
    return accepted[0]


def validate_run(data, revision, workflow_id, repository_id):
    if (data.get("path") != WORKFLOW or data.get("workflow_id") != workflow_id
            or data.get("head_sha") != revision or data.get("head_branch") != "main"
            or data.get("event") != "push" or data.get("status") != "completed"
            or data.get("conclusion") != "success"
            or (data.get("repository") or {}).get("id") != repository_id
            or (data.get("repository") or {}).get("full_name") != REPOSITORY
            or (data.get("head_repository") or {}).get("id") != repository_id
            or (data.get("head_repository") or {}).get("full_name") != REPOSITORY):
        raise RuntimeError("CI producer must be exact successful completed push/main workflow")
    positive(data["id"])
    positive(data["run_attempt"])


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def accepted_revision(revision):
    current_main(revision)
    repository = github(f"repos/{REPOSITORY}")
    repository_id = positive(repository["id"])
    if repository.get("full_name") != REPOSITORY or repository.get("default_branch") != "main":
        raise RuntimeError("Unexpected repository identity")
    workflow = github(f"repos/{REPOSITORY}/actions/workflows/ci.yml")
    if workflow.get("path") != WORKFLOW or workflow.get("state") != "active":
        raise RuntimeError("Expected active CI workflow path")
    workflow_id = positive(workflow["id"])
    candidates = pages(f"repos/{REPOSITORY}/actions/workflows/{workflow_id}/runs?head_sha={revision}&event=push&branch=main", "workflow_runs")
    if not candidates:
        raise RuntimeError("No exact CI producer run")
    selected = max(candidates, key=lambda item: positive(item["id"]))
    run_id = selected["id"]
    latest = github(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    validate_run(latest, revision, workflow_id, repository_id)
    attempt = latest["run_attempt"]
    exact = github(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}")
    validate_run(exact, revision, workflow_id, repository_id)
    if exact["id"] != run_id or exact["run_attempt"] != attempt:
        raise RuntimeError("CI attempt identity mismatch")
    jobs = pages(f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    names = {}
    for job in jobs:
        if (job.get("run_id") != run_id or job.get("run_attempt", attempt) != attempt
                or job.get("head_sha") != revision or job.get("status") != "completed"
                or job.get("conclusion") != "success"):
            raise RuntimeError("Unsuccessful or incoherent CI attempt jobs")
        names.setdefault(job["name"], []).append(job)
    if any(len(names.get(name, [])) != 1 for name in REQUIRED_JOBS):
        raise RuntimeError("Missing or ambiguous required CI jobs")
    name = f"phonebridge-deployment-{revision}"
    artifacts = pages(f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts", "artifacts")
    start, end = timestamp(exact["run_started_at"]), timestamp(exact["updated_at"])
    matches = [item for item in artifacts if item.get("name") == name
               and start <= timestamp(item["created_at"]) <= end]
    if len(matches) != 1:
        raise RuntimeError("Missing or ambiguous latest-attempt deployment artifact; rerun all jobs")
    artifact = matches[0]
    producer = artifact.get("workflow_run") or {}
    if (artifact.get("expired") is not False or not 0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE
            or producer.get("id") != run_id or producer.get("repository_id") != repository_id
            or producer.get("head_repository_id") != repository_id or producer.get("head_sha") != revision
            or producer.get("head_branch") != "main"):
        raise RuntimeError("Invalid deployment artifact provenance")
    positive(artifact["id"])
    digest = artifact.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("CI artifact requires GitHub SHA256 digest")
    confirmed = github(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    validate_run(confirmed, revision, workflow_id, repository_id)
    if confirmed["run_attempt"] != attempt or confirmed["run_started_at"] != exact["run_started_at"]:
        raise RuntimeError("CI latest attempt changed during authorization")
    return {"source_sha": revision, "repository_id": repository_id, "workflow_id": workflow_id,
            "run_id": run_id, "run_attempt": attempt, "artifact_id": artifact["id"],
            "artifact_name": name, "artifact_digest": digest, "pull_request": accepted_pr(revision)}


def revalidate(provenance):
    latest = accepted_revision(provenance["source_sha"])
    if latest != provenance:
        raise RuntimeError("CI producer changed during deployment")
    current_main(provenance["source_sha"])


def origin(value):
    try:
        address = urlsplit(value)
        host = address.hostname
        labels = host.split(".") if host else []
        if (address.scheme != "https" or address.username is not None or address.password is not None
                or address.query or address.fragment or address.path not in ("", "/")
                or address.port not in (None, 443) or not host or len(host) > 253 or len(labels) < 2
                or value != value.strip() or host != host.lower()
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or re.fullmatch(r"[0-9.]+", host)):
            raise ValueError()
    except ValueError:
        raise RuntimeError("Invalid public HTTPS DNS origin") from None
    return f"https://{host}" + (":443" if address.port == 443 else "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def healthy(revision, url="http://127.0.0.1:18940/healthz", timeout=25):
    end = time.monotonic() + remaining(timeout)
    client = opener()
    while time.monotonic() < end:
        try:
            request_timeout = min(2, remaining(), max(0.001, end - time.monotonic()))
            with network_deadline(request_timeout):
                with client.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=request_timeout) as response:
                    raw = response.read(4097)
                    if (response.status != 200 or response.geturl() != url or len(raw) > 4096
                            or response.headers.get_content_type() != "application/json"):
                        raise ValueError()
                    body = json.loads(raw, object_pairs_hook=unique_object)
            if body == {"status": "ok", "version": VERSION, "revision": revision}:
                return body
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(min(0.25, max(0, end - time.monotonic())))
    raise RuntimeError("Exact bounded nonredirecting health failed")


def digest_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError("Expected regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            remaining()
            digest.update(chunk)
    return digest.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def load_json(path):
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_JSON:
        raise RuntimeError("Invalid bounded JSON file")
    return json.loads(path.read_bytes(), object_pairs_hook=unique_object)


def download_artifact(provenance, target):
    with network_deadline(remaining(90)):
        download_archive(provenance, target)


def download_archive(provenance, target):
    token = run("gh", "auth", "token", capture_output=True, text=True).stdout.strip()
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{provenance['artifact_id']}/zip"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    client = opener()
    try:
        response = client.open(request, timeout=remaining(15))
    except urllib.error.HTTPError as error:
        if error.code != 302:
            raise RuntimeError("Artifact download authorization failed") from None
        location = error.headers.get("Location", "")
        error.close()
        parsed = urlsplit(location)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
            raise RuntimeError("Invalid signed artifact download URL")
        response = client.open(location, timeout=remaining(15))
    with response, target.open("xb") as handle:
        if response.status != 200:
            raise RuntimeError("Artifact download failed")
        size = 0
        end = time.monotonic() + remaining(60)
        while True:
            if time.monotonic() >= end:
                raise TimeoutError("Artifact download deadline exceeded")
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise RuntimeError("Archive download limit exceeded")
            handle.write(chunk)
    if "sha256:" + digest_file(target) != provenance["artifact_digest"]:
        raise RuntimeError("Artifact transport digest mismatch")


def extract_archive(archive, destination):
    if archive.stat().st_size > MAX_ARCHIVE or any(destination.iterdir()):
        raise RuntimeError("Archive requires bounded input and empty extraction root")
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if not 1 <= len(entries) <= MAX_ENTRIES:
            raise RuntimeError("Archive entry limit exceeded")
        names, total = set(), 0
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            if (name in names or entry.orig_filename != name or "\\" in name or name != str(path) or path.is_absolute()
                    or ".." in path.parts or entry.is_dir() or entry.flag_bits & 1
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                    or not (name == "manifest.json" or re.fullmatch(r"wheelhouse/[A-Za-z0-9_.+-]+\.whl", name))):
                raise RuntimeError("Unsafe or unexpected ZIP entry")
            if entry.file_size <= 0 or entry.file_size > MAX_FILE or entry.file_size > max(1, entry.compress_size) * 200:
                raise RuntimeError("Archive file size or expansion limit exceeded")
            total += entry.file_size
            if total > MAX_EXPANDED:
                raise RuntimeError("Archive expansion limit exceeded")
            names.add(name)
        for entry in entries:
            target = destination / entry.filename
            target.parent.mkdir(exist_ok=True)
            with source.open(entry) as reader, target.open("xb") as writer:
                copied = 0
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    remaining()
                    copied += len(chunk)
                    if copied > entry.file_size:
                        raise RuntimeError("Archive expanded size mismatch")
                    writer.write(chunk)
                if copied != entry.file_size:
                    raise RuntimeError("Archive truncated file")


def validate_manifest(root, provenance):
    manifest = load_json(root / "manifest.json")
    producer = manifest.get("producer")
    expected = {key: provenance[key] for key in ("repository_id", "workflow_id", "run_id", "run_attempt")}
    expected.update(repository=REPOSITORY, workflow_path=WORKFLOW, event="push", ref="refs/heads/main")
    if (manifest.get("format") != 1 or manifest.get("source_sha") != provenance["source_sha"]
            or manifest.get("version") != VERSION or manifest.get("schema_generation") != 1
            or manifest.get("runtime_python") != "cpython-3.11"
            or producer != expected or manifest.get("artifact_name") != provenance["artifact_name"]):
        raise RuntimeError("Manifest source/version/producer mismatch")
    wheels = manifest.get("wheels")
    if not isinstance(wheels, dict) or not 2 <= len(wheels) < MAX_ENTRIES:
        raise RuntimeError("Missing complete offline wheelhouse")
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    if actual != {"manifest.json", *wheels}:
        raise RuntimeError("Wheelhouse file contract mismatch")
    projects = set()
    for name, digest in wheels.items():
        if not re.fullmatch(r"wheelhouse/[A-Za-z0-9_.+-]+\.whl", name) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("Invalid wheel manifest entry")
        if digest_file(root / name) != digest:
            raise RuntimeError("Wheel SHA256 mismatch")
        filename = Path(name).name
        for project in ("phonebridge_server", "phonebridge_cli"):
            if filename.startswith(project + "-"):
                if not filename.startswith(project + "-" + VERSION + "-") or project in projects:
                    raise RuntimeError("Invalid project wheel version or duplicate")
                projects.add(project)
    if projects != {"phonebridge_server", "phonebridge_cli"}:
        raise RuntimeError("Missing exact project wheels")
    trust = manifest.get("trust")
    if not isinstance(trust, dict) or set(trust) != set(TRUST_FILES):
        raise RuntimeError("Missing source trust manifest")
    for filename, digest in trust.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError("Invalid source trust digest")
        source = github(f"repos/{REPOSITORY}/contents/deploy/{filename}?ref={provenance['source_sha']}")
        if source.get("type") != "file" or source.get("encoding") != "base64" or source.get("size", MAX_JSON + 1) > MAX_JSON:
            raise RuntimeError("Invalid source trust blob")
        content = base64.b64decode(source["content"].replace("\n", ""), validate=True)
        if hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeError("Source trust hash differs from accepted Git object")
    return manifest


def validate_installed_trust(manifest):
    try:
        installed = load_json(BASE / "trust.json")
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("Missing or outdated installed trust controller; approved bootstrap update required") from None
    if installed != {"format": 1, "trust": manifest["trust"]}:
        raise RuntimeError("Outdated installed trust controller; approved bootstrap update required")
    for filename, location in TRUST_FILES.items():
        path = UNIT if location == "unit" else BASE / location
        try:
            matches = digest_file(path) == manifest["trust"][filename]
        except (OSError, RuntimeError):
            matches = False
        if not matches:
            raise RuntimeError("Outdated installed trust controller/unit; approved bootstrap update required")
    if Path(__file__).resolve() != (BASE / "bin/deploy_entry.py").resolve():
        raise RuntimeError("Controller must execute the installed canonical entry path")


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def switch(target):
    temporary = BASE / ("current." + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        temporary.replace(BASE / "current")
    finally:
        temporary.unlink(missing_ok=True)


def contained_release(path):
    releases = (BASE / "releases").resolve()
    if ((BASE / "releases").is_symlink() or path.is_symlink() or path.resolve().parent != releases
            or not re.fullmatch(r"[0-9a-f]{40}-[0-9a-f]{32}", path.name)):
        raise RuntimeError("Rollback release escapes validated containment")


def release_metadata(path):
    contained_release(path)
    if not path.is_dir():
        raise RuntimeError("Missing rollback release")
    data = load_json(path / "release.json")
    valid_sha(data.get("source_sha"))
    if (data.get("version") != VERSION or data.get("schema_generation") != 1
            or not re.fullmatch(data["source_sha"] + r"-[0-9a-f]{32}", path.name)
            or (path / ".venv").is_symlink() or (path / ".venv/bin").is_symlink()
            or not (path / ".venv/bin/python").is_file()
            or not stat.S_ISREG((path / "revision.env").lstat().st_mode)
            or (path / "revision.env").read_text() != f"PHONEBRIDGE_REVISION={data['source_sha']}\n"):
        raise RuntimeError("Damaged or health-incompatible rollback release")
    return data


def prepare_release(root, manifest, provenance):
    releases = BASE / "releases"
    if releases.is_symlink():
        raise RuntimeError("Release root must not be a symlink")
    releases.mkdir(exist_ok=True)
    release = releases / (provenance["source_sha"] + "-" + uuid.uuid4().hex)
    release.mkdir(mode=0o700)
    try:
        shutil.copytree(root / "wheelhouse", release / "wheelhouse")
        shutil.copyfile(root / "manifest.json", release / "manifest.json")
        requirements = "".join(f"./{name} --hash=sha256:{digest}\n" for name, digest in sorted(manifest["wheels"].items()))
        (release / "requirements.txt").write_text(requirements)
        run(RUNTIME_PYTHON, "-m", "venv", str(release / ".venv"), timeout=45)
        run(str(release / ".venv/bin/python"), "-m", "pip", "--isolated", "install", "--no-index", "--no-cache-dir", "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes", "--find-links", str(release / "wheelhouse"), "-r", str(release / "requirements.txt"), cwd=release, timeout=90)
        (release / "revision.env").write_text(f"PHONEBRIDGE_REVISION={provenance['source_sha']}\n")
        atomic_json(release / "release.json", {"source_sha": provenance["source_sha"], "version": VERSION, "schema_generation": 1, "provenance": provenance})
        return release
    except BaseException:
        shutil.rmtree(release)
        raise


def public_origin():
    settings = dict(line.strip().split("=", 1) for line in CONFIG.read_text().splitlines() if "=" in line and not line.lstrip().startswith("#"))
    return origin(settings["PHONEBRIDGE_PUBLIC_URL"])


def rollback(previous, public):
    if previous is None:
        (BASE / "current").unlink(missing_ok=True)
        run("systemctl", "--user", "stop", "phonebridge.service", timeout=25)
        return {"stopped": True}
    metadata = release_metadata(previous)
    switch(previous)
    run("systemctl", "--user", "restart", "phonebridge.service", timeout=25)
    revision = metadata["source_sha"]
    return {"health": healthy(revision), "public_health": healthy(revision, public + "/healthz")}


def safe_failure(error):
    return type(error).__name__ + (": " + str(error) if isinstance(error, (RuntimeError, TimeoutError)) else "")


def recover(public, original=None):
    global DEADLINE
    journal_path = BASE / "transaction.json"
    if not journal_path.exists():
        return
    journal = load_json(journal_path)
    if journal.get("phase") not in ("switching", "committed"):
        raise RuntimeError("Invalid interrupted transaction phase")
    previous = Path(journal["previous"]) if journal.get("previous") else None
    candidate = Path(journal["candidate"])
    contained_release(candidate)
    original_failure = safe_failure(original) if original else journal.get("original_failure", "Interrupted prior transaction")
    if original is not None:
        journal["original_failure"] = original_failure
        atomic_json(journal_path, journal)
    if journal["phase"] == "committed":
        journal_path.unlink()
        return
    DEADLINE = time.monotonic() + 90
    try:
        evidence = rollback(previous, public)
    except BaseException as failure:
        result = {"deployed": False, "original_failure": original_failure, "rollback_failure": safe_failure(failure)}
        atomic_json(BASE / "last-result.json", result)
        raise RuntimeError(json.dumps(result)) from None
    result = {"deployed": False, "original_failure": original_failure, "rollback": evidence}
    atomic_json(BASE / "last-result.json", result)
    journal_path.unlink()


def deploy(revision):
    global DEADLINE
    public = public_origin()
    with ignored_interrupts():
        recover(public)
    DEADLINE = time.monotonic() + 360
    provenance = accepted_revision(revision)
    previous = (BASE / "current").resolve(strict=True) if (BASE / "current").is_symlink() else None
    if (BASE / "current").exists() and previous is None:
        raise RuntimeError("Current must be a validated release symlink")
    if previous is not None:
        metadata = release_metadata(previous)
        healthy(metadata["source_sha"])
        healthy(metadata["source_sha"], public + "/healthz")
    scratch = BASE / "staging"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        root = Path(directory)
        archive = root / "artifact.zip"
        download_artifact(provenance, archive)
        extracted = root / "extracted"
        extracted.mkdir()
        extract_archive(archive, extracted)
        manifest = validate_manifest(extracted, provenance)
        validate_installed_trust(manifest)
        release = prepare_release(extracted, manifest, provenance)
    revalidate(provenance)
    try:
        atomic_json(BASE / "transaction.json", {"phase": "switching", "candidate": str(release), "previous": str(previous) if previous else None})
        switch(release)
        run("systemctl", "--user", "restart", "phonebridge.service", timeout=25)
        body = healthy(revision)
        public_health = healthy(revision, public + "/healthz")
        revalidate(provenance)
        result = {"deployed": True, "health": body, "public_health": public_health, "provenance": provenance, "release": release.name}
        atomic_json(BASE / "last-result.json", result)
        atomic_json(BASE / "transaction.json", {"phase": "committed", "candidate": str(release), "previous": str(previous) if previous else None})
        (BASE / "transaction.json").unlink()
        print(json.dumps(result))
    except BaseException as error:
        with ignored_interrupts():
            recover(public, error)
        raise


@contextmanager
def ignored_interrupts():
    saved = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for number, handler in saved.items():
            signal.signal(number, handler)


def interrupted(number, frame):
    raise RuntimeError(f"Interrupted deployment signal {number}")


@contextmanager
def deployment_lock(path, timeout=15):
    with path.open("a") as handle:
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("Deployment lock deadline exceeded")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main():
    try:
        command = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    except ValueError:
        raise SystemExit("Only deploy <40-lowercaseSHA> is permitted") from None
    if len(command) != 2 or command[0] != "deploy" or not re.fullmatch(r"[0-9a-f]{40}", command[1]):
        raise SystemExit("Only deploy <40-lowercaseSHA> is permitted")
    os.umask(0o077)
    for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(number, interrupted)
    BASE.mkdir(parents=True, exist_ok=True)
    try:
        with deployment_lock(BASE / "deployment.lock"):
            deploy(command[1])
    except BaseException as error:
        print(json.dumps({"deployed": False, "failure": safe_failure(error)}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
