# Deployment

The server runs as a `private` user service, bound to loopback port 18940. An HTTPS reverse proxy exposes health, pairing claim and authenticated phone WebSocket. Remote admin routes are denied by the proxy; CLI uses loopback.

Only owner-triggered accepted main revisions enter CD after the security, Python and both Android emulator CI jobs pass. The server independently requires the exact revision to belong to an owner-created, owner-merged same-repository PR; direct main pushes cannot deploy. Production SSH uses a dedicated restricted forced-command key with a pinned host key. Signing and runtime secrets stay outside Git. A failed local or public revision health check restores the previous symlink and restarts the previous service.

GitHub rejected private-repository branch protection/ruleset access with an upgrade-required 403. This repository currently has only its owner as a collaborator. The implementation therefore does not claim enforced GitHub branch protection or private environment isolation. Repository-scoped signing/deployment secrets are referenced only by the owner-gated tag/main jobs, never PR jobs. The restricted server-side deployment command provides a separate revision/merged-PR/checks boundary; it does not make repository write access safe for untrusted collaborators. Private GitHub environment protection is not assumed.

The deployment bootstrap and HTTPS route must be provisioned before activating CD. Completion requires a successful CD run and deployed revision read-back, not just a workflow file.
