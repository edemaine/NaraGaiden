import threading
import unittest
from types import SimpleNamespace
from unittest import mock

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
            inotify_status="starting",
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

    @mock.patch("nara_web.fetch_live_data")
    def test_inotify_refresh_forces_pull_and_publishes_revision(self, fetch):
        server = self.make_inotify_server()
        server.inotify_generation = 4
        server.inotify_debounce_timer = mock.Mock()

        with self.assertLogs(level="INFO") as logs:
            nara_web.refresh_after_inotify(server, 4)

        fetch.assert_called_once_with(server, force=True)
        self.assertIsNone(server.inotify_debounce_timer)
        self.assertEqual(server.data_revision, 1)
        self.assertIn("Android database changed; refreshing", logs.output[0])
        self.assertIn("Notifying 2 browsers of revision 1", logs.output[1])

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


if __name__ == "__main__":
    unittest.main()
