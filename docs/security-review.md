# Security review — 22 October 2026

Scope: preview `0f059c7`, development head `fec4619`, and changes in this PR. This is a source review of Android, relay authentication/storage/file delivery, CLI credentials, and build/distribution. It does not certify the deployed server, the agent, or an APK obtained elsewhere.

## Findings and changes

| Finding | Practical impact | Change |
| --- | --- | --- |
| **High: published APK signing key** (`android/app/phonebridge-debug.keystore`, introduced in `93b214b`) | Anyone can sign an APK accepted as an update to that preview. Installing such an update can give attacker code the app's identity and existing privileges. This requires getting the owner to install the update; possession of the key alone does not remotely install it. | Removed the tracked key and shared signing override. Debug builds use local keys; release signing remains environment-configured. Old installations must be uninstalled and replaced with a privately signed trusted build. The key is still compromised in Git history and old releases. |
| **High: agent could operate its own consent controls** (`BridgeAccessibility.kt`) | During a short screen session, an agent could navigate back to PhoneBridge and tap “until stopped”, or change the app's trust controls. | Mutating remote operations check on the UI thread that PhoneBridge is neither resumed nor present in an interactive accessibility window. This covers coordinate taps/swipes, text input and global actions, including app dialogs. Reading other apps and system navigation remain available. |
| **High when relay is directly reachable: unauthenticated upload buffering** (`RequestBoundary`) | An unauthenticated client could make the relay buffer up to 32 MiB per request before the route checked admin auth. Concurrent requests could exhaust a small server. The supplied nginx configuration already blocks public admin upload paths, but the relay should also enforce this itself. | Shared admin bearer verification now runs at the request boundary before reading any upload body; a regression test makes body reads fail if attempted. |
| **Medium: stop preserved screen consent** (`BridgeService.kt`) | After “Stop connection”, resuming could silently restore a previously granted indefinite screen session. | Both the notification stop path and the app's pause path now clear screen consent, even when the service is absent. Reconnection after an ordinary network interruption retains the owner's chosen session; an explicit stop clears it. |
| **Medium: stale pairing completion** (`MainActivity.kt`) | A pending HTTP claim could write credentials after pairing was cancelled or the Activity was destroyed. The existing generation counter was not checked before saving. | Check the generation while holding the same monitor used by cancellation, before writing credentials; ignore stale completion callbacks and interrupt the worker on destruction. A server claim already consumed before cancellation may still leave an orphan registration; remove it on the server and use a fresh code. |
| **Privacy: one-time secrets in the app UI** | Visible pairing codes could appear in Recents or be read through the Accessibility tree. | Secure app window, password-style code field without state saving/autofill or keyboard learning, and redacted password-node text/descriptions in screen trees. This does not promise screenshot redaction of sensitive content in other apps. |

The release linked in the request had no screen-consent gate. The current branch already restored timed consent in `6264541`; that existing fix is retained and is not attributed to this PR.

## What was checked

Relay admin routes require a bearer token and use constant-time hash comparison. Pairing tokens are random, expiring and single-use; device tokens are hashed in SQLite. Claims pin the WebSocket endpoint to the confirmed server origin. Redirects are disabled when claiming credentials. File downloads authorize the device against the transfer, and the phone verifies size and SHA-256 before publishing a file. Names cannot traverse directories. Android credentials use AES-GCM with Android Keystore and backups are disabled. The CLI uses private files and refuses redirects/symlink credential files. The supplied nginx configuration exposes device endpoints and keeps admin endpoints local.

No direct unauthenticated device-control route, cross-device download bypass, arbitrary local command execution, or plaintext device-token persistence was identified in these paths. That is a scoped finding, not a guarantee of absence.

## Boundaries that remain

The server admin token controls every paired phone on that relay. The agent and server operators can see screen results, typed text, uploaded files and retained command history. Screen control is broad across other apps; it is not restricted to an app allowlist. Grant it only while using an agent/server you trust. Other Accessibility services and compromised/rooted Android installations are outside this app's trust boundary.

Private release signing, rotating a compromised server admin token, removing old debug APKs, inspecting the deployed nginx configuration and enabling Android's installation protections remain operator actions. This PR does not deploy or publish an APK.

## Verification

Regression coverage checks upload rejection before any body read, refusal to operate PhoneBridge's own controls and that an explicit connection stop clears indefinite screen consent. The native owner UI tour checks connected/paused affordances and captures four actual Android screenshots with synthetic state. Full test results and environment limitations are recorded in the PR.
