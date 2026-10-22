# phonebridge CLI

`pip install ./cli` installs `phonebridge`. Configuration: `PHONEBRIDGE_URL` (default `http://127.0.0.1:18940`; remote servers must use HTTPS) and `PHONEBRIDGE_TOKEN` (admin token), or `--config FILE` with a mode-0600 JSON `{"url", "token"}`. Output is JSON: results on stdout, errors on stderr as `{"error": {"code", "message", ...}}` with a nonzero exit code. Known credentials are redacted from all output.

## Commands

    phonebridge pair --name 'My phone' --ttl 300 --qr /private/new/pair.png
    phonebridge pair status PAIRING_ID
    phonebridge pair revoke PAIRING_ID
    phonebridge devices list | show DEVICE | revoke DEVICE
    phonebridge capabilities DEVICE
    phonebridge debug status DEVICE
    phonebridge screen tree|screenshot|tap|swipe|input|global DEVICE --debug-session SESSION ...
    phonebridge files send DEVICE PATH [--image] [--name NAME] [--mime TYPE] [--wait]
    phonebridge images send DEVICE PATH [--name NAME] [--mime TYPE] [--wait]
    phonebridge command DEVICE OP --args JSON [--wait]
    phonebridge command-status COMMAND_ID [--wait] [--output shot.jpg]
    phonebridge events DEVICE --after CURSOR --limit 50
    phonebridge install-skill [--replace]

Screen helpers: `tree` (`--package` optional), `screenshot` (`--package`, `--output new.jpg` writes a private JPEG), `tap` (`--package --x --y`), `swipe` (`--package --x1 --y1 --x2 --y2 [--duration-ms]`), `input` (`--package --resource-id --text [--expected-text]`), `global` (`--action back|home|recents|notifications|quick_settings`). See [../docs/screen-control.md](../docs/screen-control.md).

`files send` uploads a regular local file (no symlinks, at most 32 MiB) and queues delivery; `images send` and `--image` save PNG, JPEG, WebP or GIF to the gallery. See [../docs/file-delivery.md](../docs/file-delivery.md).

## Submission and waiting

Every submission is sent once with an idempotency key. `--wait` polls the command until it is `succeeded`, `failed` or `expired`, bounded by `--timeout`. On a timeout, transport error or interrupt the error includes `command_id` or `idempotency_key` and `may_have_been_submitted`; inspect with `command-status` instead of resubmitting. `--ttl` is how long the server keeps an undelivered command.
