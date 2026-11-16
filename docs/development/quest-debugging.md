# Quest 3 debugging from the Mac (USB + WiFi adb, screenshots, CDP)

Field-proven 2026-07-06 (029 pass) and 2026-07-17 (078 pass). Everything here is read-only
against the headset; nothing touches the network fleet.

## adb basics

- adb lives at `/opt/homebrew/share/android-commandlinetools/platform-tools/adb` — it is NOT
  on PATH, and `brew install android-platform-tools` fails on this Mac (/opt/homebrew
  ownership); the commandlinetools cask already bundles it.
- The Quest 3 enumerates as `eureka`, serial `2G0YC1ZF7X03Y0`.
- `adb devices -l` states: `device` (ready), `unauthorized` (see below), absent (check the
  cable AND `system_profiler SPUSBDataType | grep -A3 Quest`).

## Authorization — the part that eats an afternoon

- The "Allow USB debugging?" dialog renders **only inside VR while the headset is awake** —
  never as a phone/desktop notification. Have the operator put the headset ON to look for it.
- Authorization can silently lapse between sessions (observed after ~11 days / OS activity).
  `unauthorized` cannot be fixed from the Mac alone.
- **Recovery recipe that worked (2026-07-17)** when re-accepting the dialog did NOT stick:
  1. Regenerate the Mac's keypair: `adb kill-server`, move `~/.android/adbkey{,.pub}` aside,
     `adb start-server` (mints a fresh key → fresh fingerprint prompt).
  2. Operator toggles in-headset **Settings → System → Developer → USB debugging** off/on
     (an OS update can drop developer mode entirely; then it's the Meta phone app).
  3. Accept the new in-VR dialog with "Always allow from this computer".
- macOS Ventura+ can also gate the accessory on the *Mac* side ("Allow accessory to
  connect?") — approve that popup if it appears.
- Waiting for the flip without polling by hand:
  `until adb devices | sed -n 2p | grep -q "device$"; do sleep 2; done`

## WiFi adb — cable-free after the first authorization

- Wireless **cannot bootstrap** trust: no mdns `_adb-tls-connect` service is advertised until
  debugging is authorized once over USB (or via Meta Quest Developer Hub). Verify with
  `adb mdns services`.
- Once USB shows `device`:
  `QIP=$(adb shell ip addr show wlan0 | grep -o 'inet [0-9.]*' | cut -d' ' -f2)`
  `adb tcpip 5555 && adb connect "$QIP:5555"`
  Both transports then appear in `adb devices`; the cable can come off. Survives until the
  headset reboots (then redo from USB). Last seen at `<headset-ip>:5555`.

## Screenshots

- **Operator-taken** (clean 2D captures of the browser/app):
  `adb shell ls -t /sdcard/Oculus/Screenshots/ | head` → `adb pull /sdcard/Oculus/Screenshots/<name>`.
  Named `com.oculus.browser-YYYYMMDD-HHMMSS.jpg` for browser captures.
- **Live grab** (no operator action): `adb exec-out screencap -p > shot.png`. The raw frame is
  a per-eye fisheye stereo composite — fine for verifying scene state remotely, not pretty.
  Works over the WiFi transport (`adb -s <ip>:5555 exec-out …`).

## Chrome DevTools Protocol (live page/JS debugging)

- `adb forward tcp:9222 localabstract:chrome_devtools_remote`, then `curl localhost:9222/json`
  lists tabs. The WebSocket handshake needs `suppress_origin` (websocket-client) or Chromium
  403s it. Helper pattern: `uv run --with websocket-client python cdp.py eval|logs …`
  (Runtime.evaluate with awaitPromise/returnByValue; Log.enable replays buffered console).
- The immersive viewer exposes `window.__obiDebug` (scene, camera, world, settings panel,
  band state) precisely because module closures are unreachable from CDP.
- To reach a live `XRSession` beyond that: hook `XRSession.prototype.requestAnimationFrame`
  to capture `this` into `window.__xrSession`; parallel rAF consumers are allowed.
  `location.reload()` via CDP kicks the headset out of the immersive session.

## Gotchas

- XR frames stop while the headset is off-head (`session.visibilityState === 'hidden'`) — a
  recorder counting 0 frames is not a bug.
- Quest hands expose a vestigial all-zero 10-button `gamepad` on hand input sources.
- `adb reconnect` drops and re-negotiates the transport — useful to re-trigger the auth
  prompt, but expect the device to vanish from `adb devices` for a few seconds.
