# Nara Gaiden Android

This Android project contains:

- `app`: phone app and home-screen widget
- `wear`: Wear OS app, tile, and complication backed by the phone app

The phone app fetches data from the server (`/json` endpoint).
The Wear app syncs through the phone app.

## Setup

1) Install Android Studio (if needed)
   - Download from https://developer.android.com/studio
   - Open it once and let it finish first-time setup.
2) Open the project
   - In Android Studio: Open.
   - Select the folder `android`.
   - If prompted to Trust or Sync Gradle, accept.
3) Let Gradle sync
   - Wait for "Gradle Sync Finished".
   - If Android Studio asks to update Gradle or the Android plugin, accept recommended updates.
4) Set the server URL
    - Edit `android/local.properties` to add server URL configuration via
      `naraGaidenServerUrl=http://your-host.example:port`.
    - `android/local.properties` is ignored by Git, so this stays local.
    - See `android/local.properties.example` for an example configuration.
5) Connect your Android phone
    - Physical cable:
      - Use a USB cable to connect the phone to your computer.
      - Enable Developer Options if needed: Settings -> About phone -> tap Build number 7 times.
      - Enable USB Debugging: Settings -> Developer options -> USB debugging.
      - When prompted, allow USB debugging on the phone.
    - Wireless:
      - Settings -> Developer options -> Wireless debugging.
      - Pair the device with Android Studio.
6) If you want to run the Wear app, connect a Wear OS watch or emulator too.
   - In Android Studio Device Manager, create a Wear OS virtual device if needed.
   - Pair the watch with the phone/emulator if you want phone-to-watch sync to work.

## Run

1) Start the server: `python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554`
2) Build and run the phone app on your phone.
   - In Android Studio, select your device from the run dropdown.
   - Choose the `app` run configuration and click Run; the app should install and open.
3) If you also want the watch app, run the `wear` configuration to the watch/emulator.
   - Android Studio does not automatically install both modules together by default.
   - Run `app` to the phone and `wear` to the watch as separate run targets.
   - Some Android Studio versions let you create a compound configuration if you want one-click deploy.
4) Add the widget from the home-screen widgets list.
   - Long-press on the home screen -> Widgets.
   - Find "Nara Gaiden" and drag it to the home screen.
   - Tap "Refresh Widget" on the widget to test.
5) Open the Wear app on the watch and tap refresh.
   - The watch asks the phone app for the latest snapshot.
   - The tile and complication also use the same synced snapshot.
6) If you use LockStar (Samsung phones), add the widget to the lock screen from there.
    - Install Good Lock and LockStar from Galaxy Store.
    - Enable lock screen widgets in LockStar.
    - Add the Nara Gaiden widget to your LockStar layout.
    - Note: Requires One UI 8.0 or newer for lock screen rendering.

## Notes

- The phone widget has a Refresh button you can tap.
- The phone widget has a button to open the NaraGaiden app,
  if you press it twice. This also triggers a refresh.
- The phone widget has a button (`n`) to open the Nara app,
  if you press it twice. Use this to enter data.
- The Wear app is designed for round screens and should still work on square ones.
- The Wear tile and complication reflect the last synced snapshot from the phone.
- If Gradle fails under Java 25, use Android Studio's bundled JBR / Java 21 runtime.
