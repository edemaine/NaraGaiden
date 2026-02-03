# Nara Gaiden iOS Widget

This folder contains a minimal iOS app with a lock screen widget that reads the `/json` endpoint from `nara_web.py`.

Load `NaraGaiden.xcodeproj` with Xcode.

## Configuration

- Update `NaraConfig.serverURLString` in `ios/Shared/NaraAPI.swift` to your server URL, for example `http://192.168.1.20:8787/json`.
- If you use HTTP instead of HTTPS, add an ATS exception in both the app and widget extension Info.plist. The simplest dev option is:
  - NSAppTransportSecurity -> NSAllowsArbitraryLoads = YES

## Run

- Start the server: `python nara_web.py --host 0.0.0.0 --port 8787`
- Confirm the endpoint is reachable from your device: `http://<host>:8787/json`

## Deploy to a Phone

- Connect your iPhone via USB and unlock it; trust the computer if prompted.
- On the phone: Settings -> Privacy & Security -> Developer Mode -> enable it (required on recent iOS).
- Open `ios/NaraGaiden.xcodeproj` in Xcode.
- Select the `NaraGaidenApp` target -> "Signing & Capabilities" -> set Team to your Apple ID and choose a unique Bundle Identifier.
- In the scheme selector, choose your iPhone (not a simulator).
- Press Run (Cmd+R) to install the app on your phone.
- Add the widget: long-press the Home Screen -> "+" -> search "Nara Gaiden" -> add.

If Xcode complains about signing:

- Xcode -> Settings -> Accounts -> add your Apple ID.
- In the target's "Signing & Capabilities", make sure "Automatically manage signing" is on.

If the phone says "Untrusted Developer":

- Settings -> General -> VPN & Device Management (or Profiles & Device Management).
- Under "Developer App", tap your Apple ID.
- Tap "Trust" and confirm, then relaunch the app.

## Lock Screen Widget (Simulator)

- Lock the device: Hardware -> Lock (or Cmd+L).
- Wake/unlock: press the Home button or click the screen, then swipe up.
- If a passcode is set, type it with your keyboard and press Enter.
- Long-press the lock screen -> Customize -> Lock Screen -> tap the widget area under the clock -> add "Nara Gaiden".

## Lock Screen Widget (Phone)

- Wake and unlock the phone, then long-press the lock screen.
- Tap Customize -> Lock Screen.
- Tap the widget area under the clock.
- Add "Nara Gaiden", then tap Done.
