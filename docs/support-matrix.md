# Platform support

- Android 12 (API 31) and newer. CI runs on API 31 and 35 emulators. Physical Samsung and Pixel phones are the target, but there is no automated physical-device run.
- The connection runs in a foreground service with a visible notification. Force-stop, aggressive OEM battery management (allow "unrestricted" battery use on Samsung and Pixel) or no network interrupt it; after a reboot it restores itself unless it was force-stopped. While the phone is idle in Doze, reconnecting after a dropped connection can be delayed until the phone wakes.
- APKs installed outside an app store on Android 13+ may show Accessibility as a "restricted setting". Allow it from the app's info screen (⋮ → Allow restricted settings) before granting Accessibility.
- Screen control acts on whatever is on the display. With the screen off it finds no window; Android blanks windows that set FLAG_SECURE in screenshots.
