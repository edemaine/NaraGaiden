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
