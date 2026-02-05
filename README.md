# Nara Gaiden

Nara Gaiden is a companion viewer for the
[Nara Baby tracking app](https://nara.com/pages/nara-baby-tracker-app).
Nara Gaiden is designed especially for raising multiple babies:
it lets you quickly see who was fed and changed when,
and who had their vitamins today.

Given that Nara Baby doesn't offer an API,
Nara Gaiden takes the (rather hacky) approach
of grabbing the database from an Android emulator
running the Nara Baby app.
It offers the data via a web server,
which a web, Android, or iOS app can connect to.

Note that there is no authentication. This is safe within a LAN,
but you might not want to broadcast your babies' data to the Internet.

## Quick Start

1. Install [Android Studio](https://developer.android.com/studio).
2. Emulate a medium tablet with Play Store, install Nara Baby,
   and export the APK splits via:

   ```sh
   adb shell pm path com.naraorganics.nara
   adb pull /data/app/...path from above.../base.apk
   adb pull /data/app/...path from above.../split_config...
   ```

   In my case, I obtained four files (`base.apk`,
   `split_config.en.apk`, `split_config.x86_64.apk`,
   and `split_config.xhdpi.apk`) but your mileage may vary.

3. Emulate a medium tablet with Google APIs (no Play Store)
   and install the exported APK splits via:

   ```sh
   adb install-multiple -r base.apk split_config.*.apk
   ```

4. Start that Android emulator and sign into Nara Baby.
5. Optional: Try running the exporter:
   - `python nara_live_export.py`
   - Optionally set `ADB_DEVICE` to target the specific emulator/device.
6. Run the server:
   - `python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554`
   - (`--adb-device` should match whatever `adb devices` lists)
7. Connect web browser to `localhost:8888` (or modify to your IP address)
   for the web view.
8. For mobile apps, configure clients to point at your server:
   - iOS: edit `ios/Shared/NaraAPI.swift` (`NaraConfig.serverURLString`).
   - Android: edit `android/app/src/main/java/com/nara/gaiden/NaraGaidenConfig.kt`.
9. Build/install the Android and/or iOS apps as desired:
   - iOS setup details: `ios/README.md`
   - Android setup details: `android/README.md`

## Technical Overview

1. Nara Baby runs inside an Android emulator on the server.
2. `nara_live_export.py` uses ADB to pull data from the emulator and produce JSON.
3. `nara_web.py` serves the JSON and a simple web UI.
4. Android and iOS apps/widgets poll the `/json` endpoint and render the overview.

## Components

- `nara_live_export.py`: pulls data from the Android emulator via ADB.
- `nara_web.py`: serves `/json` plus a web view optimized for multi-baby overview.
- `android/`: Android app + widget.
- `ios/`: iOS app + widget.
