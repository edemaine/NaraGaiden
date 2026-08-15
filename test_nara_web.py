import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import nara_live_export
import nara_web
from nara_web import build_html, build_json


class BuildJsonTest(unittest.TestCase):
    def test_normalizes_fractional_timestamps_to_integer_milliseconds(self):
        child_key = "child"
        payload = build_json(
            latest_feed={child_key: {"beginDt": 1783940152000.5}},
            latest_diaper={child_key: {"beginDt": 1783940136125.748}},
            latest_poopy_diapers_map={child_key: {"beginDt": 1783892553000.25}},
            child_map={child_key: "Child"},
            generated_at=1783948813585.75,
        )

        child = payload["children"][0]
        self.assertEqual(payload["generatedAt"], 1783948813585)
        self.assertEqual(child["feed"]["beginDt"], 1783940152000)
        self.assertEqual(child["diaper"]["beginDt"], 1783940136125)
        self.assertEqual(child["lastPoopDiaperBeginDt"], 1783892553000)
        self.assertIsInstance(payload["generatedAt"], int)
        self.assertIsInstance(child["feed"]["beginDt"], int)
        self.assertIsInstance(child["diaper"]["beginDt"], int)
        self.assertIsInstance(child["lastPoopDiaperBeginDt"], int)


class CommandRunTest(unittest.TestCase):
    @mock.patch("nara_live_export.subprocess.run")
    def test_commands_do_not_inherit_terminal_stdin(self, subprocess_run):
        subprocess_run.return_value = SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        )

        nara_live_export.run(["adb", "devices"])

        self.assertIs(
            subprocess_run.call_args.kwargs["stdin"],
            nara_live_export.subprocess.DEVNULL,
        )


class LiveUpdateTest(unittest.TestCase):
    def make_cache_server(self):
        return SimpleNamespace(
            adb_path="adb",
            adb_device="emulator-5554",
            nara_db_path="nara.db",
            firebase_db_path="firebase.db",
            cache_ttl=10,
            cache_data={"cached": True},
            cache_time=0,
            cache_lock=threading.Lock(),
            db_signature="same",
            inotify_debounce_lock=threading.Lock(),
            inotify_generation=0,
            cache_dirty=False,
        )

    def make_inotify_server(self):
        return SimpleNamespace(
            adb_path="adb",
            adb_device="emulator-5554",
            inotify_stop=threading.Event(),
            inotify_process=None,
            inotify_process_lock=threading.Lock(),
            inotify_debounce_lock=threading.Lock(),
            inotify_debounce_timer=None,
            inotify_generation=0,
            cache_dirty=False,
            inotify_status="starting",
            emulator_check_wakeup=threading.Event(),
            change_condition=threading.Condition(),
            data_revision=0,
            sse_client_count=2,
        )

    @mock.patch("nara_web.adb_pull")
    @mock.patch("nara_web.remote_db_signature", return_value="same")
    def test_matching_signature_avoids_database_pulls(self, signature, adb_pull):
        server = self.make_cache_server()

        data, stale = nara_web.fetch_live_data(server)

        self.assertEqual(data, {"cached": True})
        self.assertFalse(stale)
        signature.assert_called_once_with(server)
        adb_pull.assert_not_called()
        self.assertGreater(server.cache_time, 0)

    @mock.patch("nara_web.collect_live_data", return_value={"fresh": True})
    @mock.patch("nara_web.adb_pull")
    @mock.patch("nara_web.remote_db_signature", side_effect=("changed", "after-pull"))
    def test_changed_signature_pulls_and_rebuilds_cache(self, signature, adb_pull, collect):
        server = self.make_cache_server()

        data, stale = nara_web.fetch_live_data(server)

        self.assertEqual(data, {"fresh": True})
        self.assertFalse(stale)
        self.assertEqual(adb_pull.call_count, 2)
        collect.assert_called_once_with("nara.db", "firebase.db")
        self.assertEqual(server.db_signature, "after-pull")

    @mock.patch("nara_web.collect_live_data", return_value={"fresh": True})
    @mock.patch("nara_web.adb_pull")
    @mock.patch("nara_web.remote_db_signature", return_value="after-pull")
    def test_detected_change_bypasses_fresh_cache(self, signature, adb_pull, collect):
        server = self.make_cache_server()
        server.cache_time = nara_web.time.time()
        server.cache_dirty = True
        server.inotify_generation = 3

        data, stale = nara_web.fetch_live_data(server)

        self.assertEqual(data, {"fresh": True})
        self.assertFalse(stale)
        self.assertEqual(adb_pull.call_count, 2)
        collect.assert_called_once_with("nara.db", "firebase.db")
        signature.assert_called_once_with(server)
        self.assertFalse(server.cache_dirty)

    def test_initial_inotify_start_failure_disables_notifications(self):
        server = self.make_inotify_server()
        process = mock.Mock()
        process.poll.return_value = 1
        process.communicate.return_value = ("inotifyd: inaccessible", None)

        with mock.patch("nara_web.start_inotify_process", return_value=process) as start, mock.patch(
            "nara_web.INOTIFY_STARTUP_GRACE_SECONDS", 0
        ):
            nara_web.inotify_monitor_loop(server)

        start.assert_called_once_with(server)
        self.assertEqual(server.inotify_status, "disabled")

    def test_inotify_restarts_after_a_later_exit(self):
        server = self.make_inotify_server()
        first = mock.Mock()
        first.poll.return_value = None
        first.stdout = []
        second = mock.Mock()
        second.poll.return_value = None

        def stop_after_restart():
            server.inotify_stop.set()
            yield from ()

        second.stdout = stop_after_restart()

        with mock.patch(
            "nara_web.start_inotify_process", side_effect=(first, second)
        ) as start, mock.patch("nara_web.INOTIFY_STARTUP_GRACE_SECONDS", 0), mock.patch(
            "nara_web.INOTIFY_RESTART_DELAY_SECONDS", 0
        ):
            nara_web.inotify_monitor_loop(server)

        self.assertEqual(start.call_count, 2)
        self.assertNotEqual(server.inotify_status, "disabled")
        self.assertTrue(server.emulator_check_wakeup.is_set())

    @mock.patch("nara_web.fetch_live_data")
    def test_inotify_refreshes_and_publishes_revision(self, fetch):
        server = self.make_inotify_server()
        server.inotify_generation = 4
        server.cache_dirty = True
        server.inotify_debounce_timer = mock.Mock()

        with self.assertLogs(level="INFO") as logs:
            nara_web.refresh_after_inotify(server, 4)

        fetch.assert_called_once_with(server)
        self.assertIsNone(server.inotify_debounce_timer)
        self.assertEqual(server.data_revision, 1)
        self.assertIn("Android database changed; refreshing", logs.output[0])
        self.assertIn("Notifying 2 browsers of revision 1", logs.output[1])

    @mock.patch("nara_web.adb_pull")
    def test_debounce_only_publishes_when_client_already_refreshed(self, adb_pull):
        server = self.make_cache_server()
        server.cache_time = nara_web.time.time()
        server.inotify_generation = 4
        server.inotify_stop = threading.Event()
        server.inotify_debounce_timer = mock.Mock()
        server.change_condition = threading.Condition()
        server.data_revision = 0
        server.sse_client_count = 2

        with self.assertLogs(level="INFO") as logs:
            nara_web.refresh_after_inotify(server, 4)

        adb_pull.assert_not_called()
        self.assertEqual(server.data_revision, 1)
        self.assertIn("already refreshed by a client request", logs.output[0])
        self.assertIn("Notifying 2 browsers of revision 1", logs.output[1])

    @mock.patch("nara_web.threading.Timer")
    def test_inotify_event_immediately_marks_cache_dirty(self, timer):
        server = self.make_inotify_server()

        nara_web.schedule_inotify_refresh(server)

        self.assertTrue(server.cache_dirty)
        self.assertEqual(server.inotify_generation, 1)
        timer.return_value.start.assert_called_once_with()

    @mock.patch("nara_web.subprocess.Popen")
    def test_inotify_watches_for_modifications(self, popen):
        server = self.make_inotify_server()

        nara_web.start_inotify_process(server)

        command = popen.call_args.args[0]
        self.assertIn(f"{nara_web.REMOTE_NARA_DB}:c", command)
        self.assertIn(f"{nara_web.REMOTE_FIREBASE_DB}:c", command)
        options = popen.call_args.kwargs
        if nara_web.sys.platform == "cygwin":
            self.assertNotIn("creationflags", options)
            self.assertNotIn("start_new_session", options)
        elif nara_web.os.name == "nt":
            self.assertEqual(
                options["creationflags"],
                nara_web.subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(options["start_new_session"])

    def test_stopping_inotify_waits_for_process_exit(self):
        process = mock.Mock()
        process.poll.return_value = None

        nara_web.stop_inotify_process(process)

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=2.0)

    def test_stopping_stuck_inotify_kills_process(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = (nara_web.subprocess.TimeoutExpired("adb", 2), None)

        nara_web.stop_inotify_process(process)

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_ctrl_c_exit_code_stops_server_instead_of_restarting(self):
        server = self.make_inotify_server()
        server.shutdown = mock.Mock()
        process = mock.Mock()
        process.poll.return_value = 130
        process.returncode = 130
        process.communicate.return_value = ("", None)

        with mock.patch("nara_web.start_inotify_process", return_value=process), mock.patch(
            "nara_web.INOTIFY_STARTUP_GRACE_SECONDS", 0
        ), self.assertLogs(level="INFO") as logs:
            nara_web.inotify_monitor_loop(server)

        server.shutdown.assert_called_once_with()
        self.assertTrue(server.inotify_stop.is_set())
        self.assertEqual(server.inotify_status, "stopping")
        self.assertIn("Ctrl-C interrupted inotifyd", logs.output[0])

    def test_web_page_subscribes_to_change_events(self):
        page = build_html({}, {}, {}, {}, 0)

        self.assertIn('new EventSource("/events")', page)
        self.assertIn('addEventListener("ready", refreshContent)', page)
        self.assertIn('addEventListener("changed", refreshContent)', page)

    @mock.patch("nara_web.fetch_live_data")
    def test_request_during_supervised_startup_returns_starting_page(self, fetch):
        handler = SimpleNamespace(
            path="/",
            headers={},
            server=SimpleNamespace(
                password_hash=None,
                emulator_avd="Nara_Tablet",
                emulator_ready=threading.Event(),
            ),
            send_emulator_starting=mock.Mock(),
        )

        nara_web.Handler.do_GET(handler)

        handler.send_emulator_starting.assert_called_once_with(html_page=True)
        fetch.assert_not_called()

    def test_sse_ready_event_waits_for_first_live_data(self):
        live_data_ready = threading.Event()
        inotify_stop = threading.Event()
        change_condition = mock.MagicMock()

        def finish_startup(_timeout):
            live_data_ready.set()
            return True

        def finish_stream(*_args, **_kwargs):
            inotify_stop.set()
            return True

        live_data_ready.wait = mock.Mock(side_effect=finish_startup)
        change_condition.wait_for.side_effect = finish_stream
        server = SimpleNamespace(
            change_condition=change_condition,
            sse_client_count=0,
            data_revision=0,
            inotify_stop=inotify_stop,
            emulator_avd="Nara_Tablet",
            live_data_ready=live_data_ready,
        )
        handler = SimpleNamespace(
            wfile=io.BytesIO(),
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        nara_web.Handler.send_event_stream(handler, server)

        stream = handler.wfile.getvalue().decode("utf-8")
        self.assertLess(
            stream.index(": waiting for Android emulator"),
            stream.index("event: ready"),
        )

    @mock.patch("nara_web.fetch_live_data")
    def test_json_generated_at_is_response_time_not_cached_snapshot_time(self, fetch):
        fetch.return_value = (
            {
                "events": [],
                "children": {},
                "generatedAt": 1000,
            },
            False,
        )
        handler = SimpleNamespace(
            path="/json",
            headers={},
            server=SimpleNamespace(password_hash=None),
            wfile=io.BytesIO(),
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
        )

        with mock.patch("nara_web.time.time", return_value=1785510123.456):
            nara_web.Handler.do_GET(handler)

        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["generatedAt"], 1785510123456)


class EmulatorSupervisorTest(unittest.TestCase):
    def make_server(self, process=None):
        return SimpleNamespace(
            adb_path="adb",
            adb_device="emulator-5554",
            emulator_avd="Nara_Tablet",
            emulator_path="emulator",
            emulator_args=["-no-window"],
            emulator_app_package="com.naraorganics.nara",
            emulator_check_interval=0,
            emulator_failure_threshold=2,
            emulator_boot_timeout=180,
            emulator_process=process,
            emulator_process_lock=threading.Lock(),
            emulator_boot_started_at=None,
            emulator_ready=threading.Event(),
            emulator_check_wakeup=threading.Event(),
            inotify_stop=threading.Event(),
            emulator_owned=process is not None,
            emulator_adopted=False,
            change_condition=threading.Condition(),
            live_data_ready=threading.Event(),
            shutdown=mock.Mock(),
        )

    def test_emulator_arguments_default_to_headless(self):
        with mock.patch.dict(nara_web.os.environ, {}, clear=True):
            self.assertEqual(
                nara_web.configured_emulator_args(),
                ["-no-window", "-no-audio", "-no-boot-anim"],
            )

    def test_empty_emulator_arguments_restore_normal_ui(self):
        with mock.patch.dict(
            nara_web.os.environ, {"NARA_EMULATOR_ARGS": ""}, clear=True
        ):
            self.assertEqual(nara_web.configured_emulator_args(), [])

    def test_prints_stop_command_for_adopted_emulator(self):
        server = self.make_server()
        server.emulator_adopted = True

        with mock.patch("builtins.print") as print_message:
            nara_web.print_adopted_emulator_stop_message(server)

        self.assertEqual(print_message.call_count, 2)
        self.assertIn("already running", print_message.call_args_list[0].args[0])
        self.assertIn(
            "adb -s emulator-5554 emu kill",
            print_message.call_args_list[1].args[0],
        )

    def test_does_not_print_stop_command_for_managed_emulator(self):
        server = self.make_server()

        with mock.patch("builtins.print") as print_message:
            nara_web.print_adopted_emulator_stop_message(server)

        print_message.assert_not_called()

    @mock.patch("nara_web.adb_supervisor_run")
    def test_watchdog_gets_boot_and_foreground_state_in_one_adb_call(self, adb_run):
        server = self.make_server()
        adb_run.return_value = (
            "NARA_BOOT=1\n"
            "mResumedActivity: ActivityRecord{abc u0 "
            "com.naraorganics.nara/.MainActivity t42}\n"
        )

        problem, package = nara_web.emulator_watchdog_status(server)

        self.assertIsNone(problem)
        self.assertEqual(package, "com.naraorganics.nara")
        adb_run.assert_called_once_with(
            server, "shell", nara_web.ANDROID_WATCHDOG_COMMAND
        )

    @mock.patch("nara_web.adb_supervisor_run")
    def test_finds_resumed_android_package(self, adb_run):
        adb_run.return_value = (
            "NARA_BOOT=1\n"
            "mResumedActivity: ActivityRecord{abc u0 "
            "com.naraorganics.nara/.MainActivity t42}\n"
        )

        package = nara_web.foreground_android_package(self.make_server())

        self.assertEqual(package, "com.naraorganics.nara")

    @mock.patch("nara_web.threading.Thread")
    @mock.patch("nara_web.run")
    @mock.patch("nara_web.subprocess.Popen")
    def test_starts_configured_avd_with_extra_arguments(self, popen, run, thread):
        server = self.make_server()
        popen.return_value.poll.return_value = None

        nara_web.start_managed_emulator(server)

        if nara_web.sys.platform == "cygwin":
            popen.assert_not_called()
            command = run.call_args.args[0]
            self.assertEqual(command[1:], [
                "--hide", "emulator", "-avd", "Nara_Tablet", "-no-window"
            ])
            self.assertTrue(run.call_args.kwargs["start_new_session"])
            self.assertIsNone(server.emulator_process)
            self.assertTrue(server.emulator_owned)
            thread.assert_not_called()
            return

        self.assertEqual(
            popen.call_args.args[0],
            ["emulator", "-avd", "Nara_Tablet", "-no-window"],
        )
        self.assertIs(server.emulator_process, popen.return_value)
        options = popen.call_args.kwargs
        if nara_web.os.name == "nt":
            self.assertEqual(
                options["creationflags"],
                nara_web.subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(options["start_new_session"])
        self.assertIs(
            thread.call_args.kwargs["target"],
            nara_web.managed_emulator_monitor_loop,
        )
        thread.return_value.start.assert_called_once_with()

    def test_ctrl_c_exit_from_emulator_stops_server(self):
        process = mock.Mock()
        process.poll.return_value = 130
        server = self.make_server(process)

        with self.assertLogs(level="INFO") as logs:
            nara_web.managed_emulator_monitor_loop(server, process)

        server.shutdown.assert_called_once_with()
        self.assertTrue(server.inotify_stop.is_set())
        self.assertIn("Ctrl-C interrupted Android emulator", logs.output[0])

    @mock.patch("nara_web.run")
    def test_stops_native_emulator_after_cygwin_wrapper_exits(self, run):
        process = mock.Mock()
        process.poll.return_value = 130
        server = self.make_server(process)

        nara_web.stop_managed_emulator(server)

        run.assert_called_once_with(
            ["adb", "-s", "emulator-5554", "emu", "kill"],
            timeout=5.0,
        )

    def test_relaunches_nara_when_another_app_is_foreground(self):
        server = self.make_server()

        def stop_after_launch(_server):
            server.inotify_stop.set()

        with mock.patch(
            "nara_web.emulator_watchdog_status",
            return_value=(None, "com.android.launcher"),
        ), mock.patch(
            "nara_web.launch_nara_android_app", side_effect=stop_after_launch
        ) as launch:
            nara_web.emulator_supervisor_loop(server)

        launch.assert_called_once_with(server)
        self.assertTrue(server.emulator_ready.is_set())

    def test_restarts_live_managed_emulator_after_failure_threshold(self):
        process = mock.Mock()
        process.poll.return_value = None
        server = self.make_server(process)

        def stop_after_restart(_server):
            server.inotify_stop.set()

        with mock.patch(
            "nara_web.emulator_watchdog_status",
            return_value=("ADB timed out", None),
        ), mock.patch(
            "nara_web.restart_managed_emulator", side_effect=stop_after_restart
        ) as restart:
            nara_web.emulator_supervisor_loop(server)

        restart.assert_called_once_with(server)

    def test_waits_for_threshold_if_preexisting_emulator_later_fails(self):
        server = self.make_server()
        health_results = iter((None, "ADB timed out", "ADB timed out"))

        def health(_server):
            return next(health_results), "com.naraorganics.nara"

        def stop_after_restart(_server):
            server.inotify_stop.set()

        with mock.patch("nara_web.emulator_watchdog_status", side_effect=health), mock.patch(
            "nara_web.start_managed_emulator"
        ) as start, mock.patch(
            "nara_web.restart_managed_emulator", side_effect=stop_after_restart
        ) as restart:
            nara_web.emulator_supervisor_loop(server)

        start.assert_not_called()
        restart.assert_called_once_with(server)

    def test_does_not_restart_managed_emulator_during_boot_grace(self):
        process = mock.Mock()
        process.poll.return_value = None
        server = self.make_server(process)
        server.emulator_boot_started_at = 100
        server.emulator_check_interval = 60

        def stop_after_wait(_timeout):
            server.inotify_stop.set()
            return True

        server.emulator_check_wakeup.wait = mock.Mock(side_effect=stop_after_wait)
        with mock.patch(
            "nara_web.emulator_watchdog_status",
            return_value=(nara_web.EMULATOR_BOOTING_PROBLEM, None),
        ), mock.patch("nara_web.time.monotonic", return_value=110), mock.patch(
            "nara_web.restart_managed_emulator"
        ) as restart:
            nara_web.emulator_supervisor_loop(server)

        restart.assert_not_called()
        server.emulator_check_wakeup.wait.assert_called_once_with(
            nara_web.EMULATOR_STARTUP_CHECK_INTERVAL_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
