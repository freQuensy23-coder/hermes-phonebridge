# Deployment

The server runs as a `private` user service, bound to loopback port 18940. An HTTPS reverse proxy exposes health, pairing claim and authenticated phone WebSocket. Remote admin routes are denied by the proxy; CLI uses loopback.

Only accepted main revisions enter CD after Python and both Android emulator CI jobs pass. Production SSH uses a dedicated restricted forced-command key with pinned host key. Signing and runtime secrets stay outside Git. A failed version health check restores the previous symlink and restarts the previous service.

The deployment bootstrap and HTTPS route must be provisioned before activating CD. Completion requires a successful CD run and deployed revision read-back, not just a workflow file.
