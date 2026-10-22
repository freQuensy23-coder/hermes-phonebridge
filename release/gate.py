import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "freQuensy23-coder/hermes-phonebridge"
OWNER = "freQuensy23-coder"
CI_PATH = ".github/workflows/ci.yml"
RELEASE_PATH = ".github/workflows/release.yml"
REQUIRED_JOBS = {"security", "python", "android-build", "android-e2e (31)", "android-e2e (35)", "release-input"}
MAX_ARCHIVE = 128 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value):
    require(type(value) is int and value > 0, "Invalid positive integer")
    return value


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        require(bool(token), "Missing GitHub token")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method="GET", data=None, accept="application/vnd.github+json", upload=False):
        host = "https://uploads.github.com" if upload else "https://api.github.com"
        require(path == "/repos/" + REPOSITORY or path.startswith("/repos/" + REPOSITORY + "/"), "Unexpected API scope")
        headers = {"Authorization": "Bearer " + self.token, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        return self.opener.open(urllib.request.Request(host + path, data=data, headers=headers, method=method), timeout=45)

    def get(self, path):
        with self.request("/repos/" + REPOSITORY + (("/" + path) if path else "")) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        require(len(data) <= 8 * 1024 * 1024, "Oversized API response")
        return json.loads(data)

    def pages(self, path, key=None):
        items = []
        total = None
        for page in range(1, 201):
            separator = "&" if "?" in path else "?"
            body = self.get(path + separator + "per_page=100&page=" + str(page))
            batch = body[key] if key else body
            require(isinstance(batch, list) and len(batch) <= 100, "Invalid API page")
            if key:
                count = body["total_count"]
                require(type(count) is int and count >= 0, "Invalid total")
                require(total is None or total == count, "Collection changed during pagination")
                total = count
            items.extend(batch)
            if len(batch) < 100:
                require(total is None or len(items) == total, "Incomplete pagination")
                identities = [item["id"] for item in items]
                require(len(identities) == len(set(identities)), "Duplicate API identities")
                return items
        raise ValueError("Pagination limit exceeded")

    def download(self, path, limit=MAX_ARCHIVE):
        try:
            response = self.request("/repos/" + REPOSITORY + "/" + path, accept="application/octet-stream")
        except urllib.error.HTTPError as error:
            require(error.code == 302, "Unexpected download response")
            target = urllib.parse.urlsplit(error.headers["Location"])
            hostname = target.hostname or ""
            require(target.scheme == "https" and not target.username and not target.password and target.port in (None, 443), "Unsafe download redirect")
            require(any(hostname.endswith(suffix) for suffix in (".blob.core.windows.net", ".actions.githubusercontent.com", ".githubusercontent.com", ".github.com")), "Unexpected download host")
            response = self.opener.open(urllib.request.Request(target.geturl()), timeout=45)
        with response:
            data = response.read(limit + 1)
        require(0 < len(data) <= limit, "Oversized or empty download")
        return data


def accepted_pr(api, sha):
    require(re.fullmatch("[0-9a-f]{40}", sha) is not None, "Invalid source SHA")
    candidates = api.pages("commits/" + sha + "/pulls")
    accepted = []
    for candidate in candidates:
        pull = api.get("pulls/" + str(integer(candidate["number"])))
        if pull.get("merge_commit_sha") != sha:
            continue
        require(pull["state"] == "closed" and pull["merged"] is True, "Source PR is not merged")
        require(pull["user"]["login"] == OWNER and pull["merged_by"]["login"] == OWNER, "Source PR was not owner-created and owner-merged")
        require(pull["base"]["ref"] == "main" and pull["base"]["repo"]["full_name"] == REPOSITORY, "Unexpected PR base")
        require(pull["head"]["repo"]["full_name"] == REPOSITORY, "Fork source forbidden")
        accepted.append(integer(pull["number"]))
    require(len(accepted) == 1, "Require one exact merged source PR")
    return accepted[0]


def peeled_tag(api, tag):
    require(tag == "v0.1.0", "Only exact stable v0.1.0 tag is supported")
    ref = api.get("git/ref/tags/" + tag)
    require(ref["ref"] == "refs/tags/" + tag, "Tag identity mismatch")
    obj = ref["object"]
    seen = set()
    for _ in range(5):
        require(re.fullmatch("[0-9a-f]{40}", obj["sha"]) is not None, "Invalid tag object")
        require(obj["sha"] not in seen, "Tag object cycle")
        seen.add(obj["sha"])
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Tag does not reference a commit")
        obj = api.get("git/tags/" + obj["sha"])["object"]
    raise ValueError("Tag nesting limit exceeded")


def bind_artifact(api, run, name, producer):
    artifacts = api.pages("actions/runs/" + str(integer(run["id"])) + "/artifacts", "artifacts")
    matches = [item for item in artifacts if item["name"] == name]
    require(len(matches) == 1, "Missing or ambiguous artifact")
    artifact = api.get("actions/artifacts/" + str(integer(matches[0]["id"])))
    require(artifact["id"] == matches[0]["id"] and artifact["name"] == name and artifact["expired"] is False, "Artifact identity/expiration mismatch")
    require(0 < artifact["size_in_bytes"] <= MAX_ARCHIVE, "Artifact size exceeded")
    origin = artifact["workflow_run"]
    require(origin["id"] == run["id"] and origin["head_sha"] == run["head_sha"] and origin["head_branch"] == "main", "Artifact run/source mismatch")
    require(origin["repository_id"] == run["repository"]["id"] and origin["head_repository_id"] == run["repository"]["id"], "Artifact repository mismatch")
    require(re.fullmatch("sha256:[0-9a-f]{64}", artifact.get("digest", "")) is not None, "Artifact digest unavailable")
    require(timestamp(producer["started_at"]) <= timestamp(artifact["created_at"]) <= timestamp(producer["completed_at"]), "Artifact not produced in selected attempt")
    require(timestamp(artifact["updated_at"]) <= timestamp(producer["completed_at"]), "Artifact updated outside producer window")
    return artifact


def attempt_jobs(api, run):
    attempt = integer(run["run_attempt"])
    jobs = api.pages("actions/runs/" + str(integer(run["id"])) + "/attempts/" + str(attempt) + "/jobs", "jobs")
    for job in jobs:
        require(job["run_id"] == run["id"] and job["head_sha"] == run["head_sha"], "Job source/run mismatch")
        require(type(job.get("run_attempt")) is int and job["run_attempt"] == attempt, "Mixed/missing job attempts")
        if job.get("started_at"):
            require(timestamp(job["started_at"]) >= timestamp(run["run_started_at"]), "Inherited previous-attempt job")
    return jobs


def accepted_ci(api, sha, repository):
    workflow = api.get("actions/workflows/ci.yml")
    require(workflow["path"] == CI_PATH and workflow["state"] == "active", "Unexpected CI workflow")
    runs = api.pages("actions/workflows/" + str(integer(workflow["id"])) + "/runs?event=push&branch=main&head_sha=" + sha, "workflow_runs")
    require(bool(runs), "No exact main CI run")
    run = api.get("actions/runs/" + str(max(integer(item["id"]) for item in runs)))
    require(run["workflow_id"] == workflow["id"] and run["path"] in (CI_PATH, CI_PATH + "@refs/heads/main"), "CI path/ID mismatch")
    require(run["event"] == "push" and run["head_branch"] == "main" and run["head_sha"] == sha, "CI source/event mismatch")
    require(run["repository"]["full_name"] == REPOSITORY and run["head_repository"]["full_name"] == REPOSITORY, "CI repository mismatch")
    require(run["repository"]["id"] == repository["id"] and run["head_repository"]["id"] == repository["id"], "CI repository ID mismatch")
    require(run["actor"]["login"] == OWNER and run["triggering_actor"]["login"] == OWNER, "CI actor mismatch")
    require(run["status"] == "completed" and run["conclusion"] == "success", "Latest exact CI run is not successful")
    jobs = attempt_jobs(api, run)
    for name in REQUIRED_JOBS:
        matching = [job for job in jobs if job["name"] == name]
        require(len(matching) == 1 and matching[0]["status"] == "completed" and matching[0]["conclusion"] == "success", "Missing/ambiguous/unsuccessful required CI job: " + name)
    producer = next(job for job in jobs if job["name"] == "release-input")
    artifact = bind_artifact(api, run, "phonebridge-release-input-" + sha, producer)
    latest = api.get("actions/runs/" + str(run["id"]))
    require(latest["run_attempt"] == run["run_attempt"] and latest["status"] == "completed" and latest["conclusion"] == "success", "CI attempt changed during authorization")
    return workflow, run, artifact


def authorize(api, tag, sha, historical, workflow_sha, workflow_ref):
    require(historical in ("true", "false"), "Invalid historical opt-in")
    require(workflow_ref == REPOSITORY + "/" + RELEASE_PATH + "@refs/heads/main", "Workflow must be dispatched from main")
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY and os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch", "Unexpected release context")
    require(os.environ["GITHUB_ACTOR"] == OWNER and os.environ["GITHUB_TRIGGERING_ACTOR"] == OWNER, "Unexpected release actor")
    repository = api.get("")
    require(repository["default_branch"] == "main" and repository["owner"]["login"] == OWNER, "Repository identity mismatch")
    main = api.get("git/ref/heads/main")["object"]["sha"]
    require(workflow_sha == main and os.environ["GITHUB_SHA"] == workflow_sha, "Trusted workflow main moved")
    workflow_pr = accepted_pr(api, main)
    source_pr = accepted_pr(api, sha)
    require(peeled_tag(api, tag) == sha, "Tag moved or wrong source")
    if sha != main:
        require(historical == "true", "Historical release requires explicit opt-in")
        comparison = api.get("compare/" + sha + "..." + main)
        require(comparison["status"] == "ahead" and comparison["merge_base_commit"]["sha"] == sha, "Source is not an accepted main ancestor")
    workflow, run, artifact = accepted_ci(api, sha, repository)
    trusted_run = run if sha == main else accepted_ci(api, main, repository)[1]
    require(peeled_tag(api, tag) == sha, "Tag moved during authorization")
    require(api.get("git/ref/heads/main")["object"]["sha"] == workflow_sha, "Trusted main moved during authorization")
    return {"repository": REPOSITORY, "source_sha": sha, "tag": tag, "source_pr": source_pr, "workflow_pr": workflow_pr, "workflow_sha": workflow_sha, "ci_workflow_id": workflow["id"], "ci_run_id": run["id"], "ci_run_attempt": run["run_attempt"], "trusted_ci_run_id": trusted_run["id"], "trusted_ci_run_attempt": trusted_run["run_attempt"], "input_artifact_id": artifact["id"], "input_artifact_digest": artifact["digest"]}


def current_binding(api):
    binding = authorize(api, os.environ["TAG"], os.environ["SOURCE_SHA"], os.environ["ALLOW_HISTORICAL"], os.environ["GITHUB_WORKFLOW_SHA"], os.environ["GITHUB_WORKFLOW_REF"])
    expected = os.environ.get("EXPECTED_BINDING")
    require(not expected or json.loads(expected) == binding, "Selected release provenance changed")
    return binding


def artifact_bytes(api, artifact_id, digest):
    data = api.download("actions/artifacts/" + str(integer(artifact_id)) + "/zip")
    require("sha256:" + hashlib.sha256(data).hexdigest() == digest, "Artifact transport digest mismatch")
    return data
