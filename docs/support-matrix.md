# Support and verification boundaries

## Target platforms

Android 12 and newer (API 31+), including Samsung and Pixel. The CI emulator matrix covers API 31 and 35. Emulator success does not establish Samsung One UI or physical Pixel compatibility. There is currently no physical OEM execution evidence.

## Existing clocks

The native clock handler selects an installed, known clock package only after Android resolves the exact typed intent. Alarm creation, dismiss/snooze/show and timer creation/dismiss/show return `outcome: dispatched`, `verified: false`. This means the request was submitted, not that the clock changed. Dismissing a repeating alarm skips its next occurrence; dismissing a timer without its content URI concerns expired timers only.

Google Clock (`com.google.android.deskclock`) and Samsung Clock (`com.sec.android.app.clockpackage`) currently have no verified version-specific accessibility selector profiles. Their UI alarm listing, enable/disable/delete and timer listing/cancel/pause/resume are unsupported and must not be advertised as available. The source-derived AOSP adapter is separate and must not be used as evidence of Google/Samsung support.

The optional general accessibility interface remains distinct from clock adapters. It does not establish reliable clock CRUD. It requires explicit opt-in, a supported current screen and an unlocked phone; it cannot bypass the lock screen or protected windows.

## Background connection

The phone initiates its authenticated connection to the server. A visible foreground-service notification and one-time Android permission grants are required. Android force-stop, OEM battery restrictions, revoked permissions, offline connectivity and foreground-activity restrictions can still interrupt connection or action availability. Capabilities are discovered at runtime rather than inferred from the phone model.

## Delivery evidence

Source, authored tests, successful automatic CI, deployed revision health and physical OEM execution are separate evidence levels. No runtime or hardware support claim may be inferred from an authored test, an intent resolver, a mock or this document. The requested delivery remains incomplete until the actual current-head CI, signed release, server deployment and installed CLI/skill have been verified.
