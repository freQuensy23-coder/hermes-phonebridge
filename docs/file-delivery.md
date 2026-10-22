# Send files to a phone

One-way delivery accepts a local path on the agent's machine. Ordinary files appear in `Download/PhoneBridge`; images appear in `Pictures/PhoneBridge` and the gallery. No phone filesystem listing, reading, arbitrary-path writes, remote execution or automatic opening is exposed. Accessibility and experimental debug consent are not required.

`phonebridge files send DEVICE /path/report.pdf --wait`

`phonebridge images send DEVICE /path/photo.png --wait`

Use `--name` for a different phone filename and `--mime` for an explicit media type. Images support PNG, JPEG, WebP and GIF. Maximum file size is 32 MiB. Existing files are never overwritten. The phone may assign a collision-safe name. A notification is posted when Android notifications are allowed; delivery still works when notifications are disabled.

Without `--wait`, output acknowledges queueing only. With `--wait`, success requires a completed Android result including the published MediaStore URI, actual display name, byte size and SHA-256. A timeout does not establish failure or permit blind resubmission: inspect the original command ID. Same idempotency key and identical payload return the same command; conflicting content is rejected.

## Transport

Admin-authenticated `POST /v1/devices/DEVICE/files` accepts an `application/octet-stream` body, exact `Content-Length`, lowercase hexadecimal `X-Content-SHA256` and optional `Idempotency-Key`. Query fields are `name`, `mime`, `kind` (`file` or `image`) and `ttl_seconds` (default 300, range 1–900). The server atomically stores bounded temporary bytes and queues `files.receive` with `transfer_id`, `name`, `mime`, `kind`, `size` and `sha256`. Local source paths and credentials never enter command metadata. Generic command submission cannot create an unbound `files.receive` command.

The paired phone downloads only its own dispatched, unexpired transfer from `GET /v1/device/files/TRANSFER` using its device bearer credential. Downloads stay on the paired server origin; redirects and arbitrary supplied URLs are refused. Pending data is bounded to 128 MiB globally and 16 transfers per device, and removed after completion, expiry or revocation. The command result retains delivery metadata.

Android streams into an unpublished MediaStore item, verifies byte size and SHA-256, validates image contents for gallery delivery, and publishes only after verification. Failed, expired or interrupted writes remove the pending item. Native command journaling prevents automatic replay of uncertain effects.

## Automatic acceptance

GitHub CI exercises the installed CLI against the real backend and authenticated Android WSS connection on API 31 and 35. Independent instrumentation reads saved MediaStore bytes and verifies binary files, images, empty files and same-name collisions. Unit and transport checks do not replace this whole-flow test.
