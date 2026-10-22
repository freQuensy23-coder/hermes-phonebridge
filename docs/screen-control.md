# Screen control

PhoneBridge controls the phone through its own Android Accessibility service. This is not ADB, root or Developer Options. Once the owner grants the PhoneBridge Accessibility service in Android settings, the paired server can read the screen and operate any app as the owner. Revoking Accessibility, stopping the connection in the app or revoking the device on the server ends it.

The phone advertises `screen.*` operations whenever the Accessibility service is connected. Commands act on whatever is on the display: the active window for the tree and text input, the whole display for screenshots and gestures.

## Operations

- `screen.tree {}` → `{package, nodes, truncated, untrusted}`. `package` is the app of the active window. Nodes are the informative visible ones in tree order: text, content description, resource id, class, `clickable`, `editable`, `scrollable`, `focused`, `checked` and `bounds [left, top, right, bottom]` in screen pixels. At most 100 nodes; `truncated:true` means more exist.
- `screen.screenshot {}` → JPEG of the whole display, longest side at most 1280 px, at most 240 KiB, with `width`, `height`, `screen_bounds [0, 0, W, H]` (same as `window_bounds`) and `package`. Map image pixel `(u, v)` to the screen with `x = u*W/width`, `y = v*H/height`. Android allows about one screenshot per second.
- `screen.tap {x, y}`, `screen.swipe {x1, y1, x2, y2, duration_ms 50..5000, default 400}` in screen pixels, 0..100000.
- `screen.input {text, resource_id?}`: sets the text of the focused input field, or of the editable node with that resource id, and reads it back (`verified`).
- `screen.global {action}`: `back`, `home`, `recents`, `notifications`, `quick_settings`.

Gesture results are `outcome:"dispatched", verified:false`: Android only confirms that the gesture was injected. Commands are never retried automatically; `AMBIGUOUS_STATE` means the agent must observe before deciding anything.

Errors: without Accessibility the phone stops advertising `screen.*`, so the server rejects new screen commands with `UNSUPPORTED` ("not advertised by this device") and fails queued ones with `UNSUPPORTED` ("Capability no longer advertised"); a command already on the phone fails with `PERMISSION_REQUIRED`. `NOT_FOUND` means there is no window (screen off) or no matching field, other `UNSUPPORTED` results mean Android refused the action, `VALIDATION_ERROR` means bad arguments.
