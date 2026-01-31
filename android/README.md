# Nara Gaiden Android Widget

This is a minimal Android app that exposes a home-screen widget to show the latest feed/diaper data from
the server (via `/json` API endpoint).

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
4) Check the server URL
   - Open `android/app/src/main/java/com/nara/gaiden/NaraGaidenConfig.kt`.
   - Change server IP or port if yours are different.
5) Connect your Android phone
   - Physical cable:
     - Use a USB cable to connect the phone to your computer.
     - Enable Developer Options if needed: Settings -> About phone -> tap Build number 7 times.
     - Enable USB Debugging: Settings -> Developer options -> USB debugging.
     - When prompted, allow USB debugging on the phone.
   - Wireless:
     - Settings -> Developer options -> Wireless debugging.
     - Pair the device with Android Studio.

## Run

1) Start the server: `python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554`
2) Build and run the app on your phone.
   - In Android Studio, select your device from the run dropdown.
   - Click the green Run button; the app should install and open.
3) Add the widget from the home-screen widgets list.
   - Long-press on the home screen -> Widgets.
   - Find "Nara Gaiden" and drag it to the home screen.
   - Tap "Refresh Widget" on the widget to test.
4) If you use LockStar (Samsung phones), add the widget to the lock screen from there.
   - Install Good Lock and LockStar from Galaxy Store.
   - Enable lock screen widgets in LockStar.
   - Add the Nara Gaiden widget to your LockStar layout.
   - Note: Requires One UI 8.0 or newer for lock screen rendering.

## Notes

- The widget has a Refresh button you can tap.
- It also updates on the system schedule (about every 30 minutes).
