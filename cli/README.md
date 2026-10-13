# PhoneBridge CLI 0.1.0

Python 3.11+. Implements docs/contract-v1.md. Runtime dependencies and direct CI dependencies are pinned in pyproject.toml. Only cli/ and skills/ belong to this component.

## Installation and configuration

Install when authorized: `python -m pip install ./cli` or the built phonebridge-cli wheel. Entry point: `phonebridge`; alternative: `python -m phonebridge`. Set PHONEBRIDGE_TOKEN through the existing private environment. PHONEBRIDGE_URL defaults to http://127.0.0.1:18940. Remote HTTP, URL credentials, query parameters and redirects are rejected; HTTPS uses system TLS verification. Tokens are header-only and removed from JSON output. No token flag exists.

Optional `phonebridge --config /private/config.json ...` reads a current-user-owned, regular, nonsymlink mode-0600 JSON file containing only url and token. Environment overrides config; --url overrides both. No configuration or credential is created automatically. Global flags precede the subcommand. --request-timeout defaults to 10 seconds, bounded 0.1..120.

Success JSON goes to stdout. Safe JSON errors go to stderr with nonzero exit; failed/expired command resources remain JSON on stdout with nonzero exit. Upstream HTTP bodies and command failure messages are not echoed. Known token values and secret fields are sanitized recursively. Native data is preserved; outcome:dispatched is conservatively emitted with verified:false even if an inconsistent server claims verification.

## Commands

    phonebridge pair --name 'Pixel' --ttl 300 --qr /private/phone.png
    phonebridge pair status PAIRING_ID
    phonebridge pair revoke PAIRING_ID
    phonebridge devices list
    phonebridge devices show DEVICE
    phonebridge devices revoke DEVICE
    phonebridge capabilities DEVICE
    phonebridge command DEVICE device.info --args '{}' --wait --timeout 60
    phonebridge command-status COMMAND_ID --wait --timeout 30
    phonebridge events DEVICE --after 0 --limit 50
    phonebridge clock providers DEVICE --wait
    phonebridge install-skill

Pair TTL is 1..900 seconds, matching the current server validator. Pair creates a real local PNG, mode 0600, without overwriting any file or symlink. Parent directory must exist. Its one-use token is in the PNG only, not stdout. Public QR server must be HTTPS. Output contains id, expires_at, status and qr_path. If file creation fails after POST, the safe error includes pairing_id so it can be inspected/revoked. install-skill uses HERMES_HOME, is independent of server credentials, reads an installed package resource, refuses symlink directories and conflicting content, and never changes Hermes core/config. No installation was performed during implementation.

All command/convenience operations accept --args JSON object, --ttl (default 60), --wait, --timeout (default 60), --poll-interval (default 0.5) and --idempotency-key. Raw args reject duplicate JSON keys, nonfinite values, nonobjects and oversized input. Convenience flags cannot collide with --args members. --wait submits exactly once and only polls GET /v1/commands/ID. Without --wait, output is the queued acknowledgement, not proof of execution. The generated idempotency key is sent to the server and returned in uncertain submission errors. No retry occurs for POST, DELETE, GET, rate limits, transport failures or ambiguous effects. Timeout/expiry errors contain command_id; inspect it without resubmission. A local expiry is not synthesized into a server expired result. Keyboard interruption also leaves effects uncertain.

Events sends after and limit (default 50) and returns next_cursor unchanged. Device/pairing revoke performs DELETE then GET; verified true requires not-found or explicit revoked state. Online false does not establish revocation. capabilities reads the advertised capabilities and permissions from GET /v1/devices/DEVICE, not a speculative phone command.

## Operation arguments

| CLI helper | Contract operation | JSON arguments / convenience flags |
| --- | --- | --- |
| clock providers | clock.providers | --args JSON |
| alarms create | clock.alarms.create | hour/--hour 0..23, minute/--minute 0..59, label/--label, days/--days comma-separated 1..7 Monday..Sunday, package/--package |
| alarms dismiss | clock.alarms.dismiss | optional label, hour, minute, package |
| alarms snooze | clock.alarms.snooze | optional minutes/--minutes, package |
| alarms show/list | clock.alarms.show/list | optional package; list is supported UI only |
| alarms enable/disable/delete | clock.alarms.enable/disable/delete | exact adapter args via --args; --id forwards id only for adapters that accept id; optional package |
| timers create | clock.timers.create | seconds/--seconds 1..86400, optional label, package |
| timers dismiss | clock.timers.dismiss | optional uri/--uri, package; missing URI dismisses expired timers only |
| timers show/list | clock.timers.show/list | optional package; list is supported UI only |
| timers cancel/pause/resume | clock.timers.cancel/pause/resume | exact adapter args via --args, optional --id and package |
| notifications list | notifications.list | optional handler args via --args, e.g. limit |
| notifications dismiss | notifications.dismiss | key/--key |
| notifications reply | notifications.reply | key/--key, action/--action index, text/--text, optional result_key/--result-key |
| notifications action | notifications.action | key/--key, action/--action index |

Generic `command DEVICE OP --args JSON` forwards the contract's other permissioned operations without invented schemas. Actual Android handlers own validation, permissions and capability advertising. UI adapter target schemas are not specified universally by contract v1; use current list/adapter evidence, never guessed IDs. Existing clocks only. Native dispatched/verified:false is never rewritten as verified true. See bundled skill for user-intent, notification injection boundaries, read-back and locked-phone limits.

## Automatic GitHub CI only

Do not run tests, linters, scanners, builds or packaging verification manually. The parent owns .github/ and must install `./cli[test]`, because the QR decode tests require pinned zxing-cpp. Commands for automatic CI jobs only:

    python -m pip install './cli[test]' setuptools==80.9.0 wheel==0.45.1
    python -m pytest cli/tests -q
    python -m build cli --outdir dist

The package test additionally builds sdist/wheel, builds a wheel from an isolated extracted sdist, installs the wheel into a temporary target, invokes the installed entry point/module and installs the bundled skill into a temporary HERMES_HOME. Other tests exercise real loopback HTTP fixtures, safe errors, exact wire requests, independently decoded QR, expiry, bounded polling, no resubmission, native unverified outcomes and safe filesystem handling. These are CI tests, not evidence that Android hardware or the live service was exercised.

Implementation gaps pending parent verification: current-head CI, release wheel and server integration, Android capability/UI adapter availability, physical OEM evidence and explicitly authorized live CLI/skill installation. This implementation does not alter auth or provide its own clocks. No local checks or builds were executed.
