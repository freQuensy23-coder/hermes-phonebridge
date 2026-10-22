# Hermes PhoneBridge

Android companion for cloud-hosted agents. The server generates a short-lived QR; the app scans it and opens an authenticated outbound connection. CLI and Hermes skill expose permission-aware native phone actions and explicit screen-dependent automation.

Targets modern Samsung and Pixel. Existing clock applications remain in use. No own clock subsystem or mandatory root/ADB. Android permissions are granted once by the phone owner. A background connection does not bypass screen lock or Android capability restrictions.

Implementation and device verification are in progress. See [current contract](docs/contract-v1.md) and [support boundaries](docs/support-matrix.md).

## CI/CD

`.github/workflows/ci.yml` runs on every push and pull request:

- `lint`: ruff, shellcheck, actionlint and Android lint.
- `typecheck`: mypy (`mypy.ini`) and Kotlin compilation of main, unit-test and instrumentation sources.
- `no-comments`: `ci/check_no_comments.py` fails on any comment or docstring in tracked code, YAML, shell, TOML, XML and Docker files.
- `python` and `android-build`: unit tests, wheels and debug/instrumentation/release APKs.
- `image`: builds `server/Dockerfile` and smoke-tests `/healthz`.
- `security`, `android-e2e` and `android-physical`: dependency and workflow audits, emulator and physical-device instrumentation.

Every push to `main` that passes all gates is released as `vN`, where N is the highest existing `vN` tag plus one. The `release` job signs the APK built in the same run with the stable key, checks its certificate against `PHONEBRIDGE_SIGNER_SHA256`, pushes `ghcr.io/frequensy23-coder/phonebridge-server:vN` and `:latest`, and publishes `phonebridge-vN.apk`, both wheels and `SHA256SUMS` as a GitHub release. The APK has `versionCode` N and `versionName` `0.1.0+N`.
