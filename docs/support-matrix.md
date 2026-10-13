# Support and verification boundaries

## Target platforms

Android 12 and newer (API 31+), including Samsung and Pixel. The CI emulator matrix covers API 31 and 35. Emulator success does not establish Samsung One UI or physical Pixel compatibility. There is currently no physical OEM execution evidence.

## Existing clocks

The native clock handler selects an installed, known clock package only after Android resolves the exact typed intent. Alarm creation, dismiss/snooze/show and timer creation/dismiss/show return `outcome: dispatched`, `verified: false`. This means the request was submitted, not that the clock changed. Dismissing a repeating alarm skips its next occurrence; dismissing a timer without its content URI concerns expired timers only.

A source-bound candidate Google Clock 8.2 (`com.google.android.deskclock`) accessibility profile has been implemented for exact APK/version, English UI, 24-hour format and API 31/35. Alarm selection uses an exact current label and displayed time; timer selection uses an exact current label and state. It exposes no durable vendor IDs and refuses duplicate, clipped, incomplete or unknown collections before effects. Collection bounds are eight alarm rows or one complete row of at most two timers. Initial genuine vendor CI exposed positive-path failures; the profile is not yet verified. A separate Samsung Clock 12.5.07.13 (`com.sec.android.app.clockpackage`) candidate binds the exact inspected APK/signature, Samsung API 36, English LTR and explicit 24-hour format. Its authored selectors and readback are not physical OEM proof; other installed releases remain unsupported. AOSP fixtures do not establish Google/Samsung support. Native clock dispatch remains unverified.

Google alarm deletion uses a stable complete pre-selection baseline, a freshly bound native modal identity and recurrence, one Delete dispatch, stable complete survivor readback, and native activity recreation. The hidden collection is not treated as fresh Accessibility evidence. Detected pre-effect changes refuse; unproven post-effect readback returns `AMBIGUOUS_STATE` without retry. This is not a vendor database lock: same-value model replacement, edits that revert between reads, and a final read/click race cannot be excluded. Production refuses the first-use music promotion; explicit dismissal belongs only to the opted-in disposable CI fixture. These modal protocol changes await automatic current-head CI.

The optional general accessibility interface remains distinct from clock adapters. It does not establish reliable clock CRUD. It requires explicit opt-in, a supported current screen and an unlocked phone; it cannot bypass the lock screen or protected windows.

## Background connection

The phone initiates its authenticated connection to the server. A visible foreground-service notification and one-time Android permission grants are required. Android force-stop, OEM battery restrictions, revoked permissions, offline connectivity and foreground-activity restrictions can still interrupt connection or action availability. Capabilities are discovered at runtime rather than inferred from the phone model.

## Delivery evidence

Source, authored tests, successful automatic CI, deployed revision health and physical OEM execution are separate evidence levels. No runtime or hardware support claim may be inferred from an authored test, an intent resolver, a mock or this document. The requested delivery remains incomplete until the actual current-head CI, signed release, server deployment and installed CLI/skill have been verified.
