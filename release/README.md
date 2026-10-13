# Release trust and parent integration

Only `.github/workflows/release.yml` and `release/` belong to this change. No imports from `deploy/` are used.

## Required CI integration

Parent adds an uncredentialed `release-input` job to `.github/workflows/ci.yml`, with `contents: read`, after `security`, `python`, `android-build`, and both `android-e2e` matrix jobs. Run for successful push/main and PR CI without signing secrets; only push/main artifacts are releasable. Checkout the exact CI SHA with persisted credentials disabled. Download the current run's `python-<sha>` into `input/python` and `android-<sha>` into `input/android`, using `actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093` (upstream v4.3.0 verified through GitHub GET). Use two exact `name` downloads, not a pattern or merged layout. Python artifact layout is `python-results.xml` plus `dist/`; Android layout is `outputs/apk/release/app-release-unsigned.apk` alongside debug/test output. These layouts were inspected directly in existing Actions artifact ZIP directories, without extracting or executing their contents. Do not rebuild.

Invoke automatically in CI:

    python release/prepare.py --python-dist input/python/dist --apk input/android/outputs/apk/release/app-release-unsigned.apk --output "$RUNNER_TEMP/release-input" --lock server/pyproject.toml --lock cli/pyproject.toml --lock android/build.gradle.kts --lock android/app/build.gradle.kts --lock android/gradle/wrapper/gradle-wrapper.properties

Include actual dependency lockfiles when available. The script records input SHA256 hashes and hosted runner/toolchain context; this records inputs rather than asserting full transitive dependency locking. The fresh `release-input` job must have hosted `JAVA_HOME_17_X64` and `ANDROID_HOME` context available; it does not build or install dependencies.

Upload `$RUNNER_TEMP/release-input/` with `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02`, artifact name `phonebridge-release-input-${{ github.sha }}`, `if-no-files-found: error`, `overwrite: true`, no hidden files. For coherent full CI reruns, parent must also set `overwrite: true` on every CI upload retaining its name across attempts, including the existing Python and Android producers; otherwise v4 rejects duplicate names and the rerun cannot succeed. Overwrite deletes the previous artifact and creates a new immutable artifact ID; the release binds the latest attempt, new ID/digest, producer creation window, and manifest attempt. A rerun beginning during release invalidates authorization. The signed release transfer uses a run-attempt-specific name and retains `overwrite: false`.

The release-input ZIP must have exactly these four root-level regular files, with no directory records:

    manifest.json
    phonebridge_cli-0.1.0-py3-none-any.whl
    phonebridge_server-0.1.0-py3-none-any.whl
    phonebridge-android-unsigned.apk

`manifest.json` has schema 1, version `0.1.0`, repository, source_sha, CI run_id/run_attempt, file sizes/SHA256, dependency input hashes, and toolchain context. `prepare.py` defines the exact contract. The release gate binds the artifact creation time to the successful `release-input` job in the latest attempt; all required jobs must belong to that attempt. Partial reruns with inherited previous-attempt jobs/artifacts fail closed: rerun all jobs.

Parent adds `release/tests` to automatic pytest and `release` to automatic lint/source scanning. No local tests/build/lint/scanners are authorized. `test_workflow.py` checks that the trusted inline workflow code matches these source modules exactly.

## Release operation

Release is `workflow_dispatch` on `main`, not a tag-owned workflow. Inputs are exact tag `v0.1.0`, exact 40-lowercase-hex source SHA, and explicit `allow_historical` (false by default). Create the tag separately through the owner's authorized process. No release step creates or updates tags. Historical main releases require the explicit opt-in and an exact owner-created, owner-merged, same-repository PR whose merge commit is the source SHA; an ancestor alone is insufficient.

The release workflow revision must itself be current accepted main, with the same PR acceptance rule and its own exact successful CI run/attempt. Historical releases require successful CI both for the source and for current trusted main. Main movement aborts a running release. Dispatch from other refs fails. Repository ownership and owner actors, exact CI workflow path/ID, push/main identity, completed success, latest coherent run attempt, all paginated jobs/artifacts, exact tag resolution, and input manifest hashes are rechecked before signing and before publishing. The tag and current main are checked again at the end of each authorization, immediately before the credentialed operation.

The `promote` job is read-only and does not rebuild. Signing and publishing use separate fresh hosted runners, no project checkout, execution/import of candidate source, package installation, project build dependencies, Gradle, or caches. Ordinary trusted workflow-inline Python contains literal copies of `release/gate.py`, `release/payload.py`, and `release/runtime.py`. Each fresh runner materializes these main-workflow-authored literals into its own new mode-0700 temporary package with mode-0400 source files, then invokes it with isolated Python (`-I`). Nothing executable is obtained from a checkout, downloaded source, CI artifact, cache, or other runner. Separate materialization steps keep every `run` below GitHub's 21,000-character limit. Automatic tests require exact source/literal equality; changes to these source modules must also update the corresponding workflow literals. Artifacts are only parsed/copied. Signing uses preinstalled Android build-tools 35.0.0 and JDK17; absence fails closed rather than installing dependencies on the signing runner. Repository secrets `ANDROID_KEYSTORE_BASE64` and `ANDROID_KEYSTORE_PASSWORD` occur only in the isolated signing step, with alias `phonebridge` and mode-0600 temporary key cleanup. No environment protection or paid-plan feature is assumed.

Set the nonsecret repository variable `PHONEBRIDGE_SIGNER_SHA256` to exactly 64 lowercase hex digits of the expected APK signer certificate SHA256, without colons. It is checked against the sole observed signer before publishing. This change does not read, set, or rotate the variable or secrets.

The publisher has job-level `contents: write` because GitHub permissions are job-scoped, but its token is explicitly injected only into one ordinary inline publication step. No checkout, external action, install, artifact execution, or shell command from input runs there. It revalidates tag/source/CI/main before creating an absent draft release, uploads exact assets, reads back and hashes their bytes, revalidates again, then publishes and reads back the exact release. It does not overwrite existing releases. Failures can leave a draft for deliberate owner cleanup, and do not falsely claim immutability.

Published assets: signed APK, both accepted CI wheels, SHA256SUMS, and provenance.json. Provenance binds tag/source, CI run/attempt/artifact/digest, release workflow revision/run/attempt, toolchain/dependency input metadata, stable certificate fingerprint, unsigned APK hash and published binary hashes. The APK is cryptographically signed; wheels and manifest have hashes and authenticated GitHub producer provenance, not cryptographic wheel signatures. No paid attestation service is required.

## Remaining authority limits

Repository branch protection is unavailable under the observed private plan. Sole-owner PR provenance is enforced by source gates, not independent reviewer approval or protection against an owner rewriting this workflow. Workflow files and repository-scoped secrets remain ultimately controlled by the owner. Tag movement cannot be made atomic with publication using ordinary REST; immediate repeated validation reduces races, but does not replace supported immutable tags/releases. The parent must merge/activate reviewed main code, run automatic exact-head CI, inspect results, set the certificate variable through an authorized operation, and perform an independently authorized release dispatch before delivery is verified.
