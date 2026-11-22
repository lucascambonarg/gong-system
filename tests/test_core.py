"""Tests for the gong app's logic. Run with: python -m unittest discover tests"""

import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

import core  # noqa: E402

FIX = REPO / "tests" / "fixtures"
SCHEDULES_TEXT = (FIX / "schedules.json").read_text(encoding="utf-8")
TIMETABLE_TEXT = (FIX / "timetable.csv").read_text(encoding="utf-8")
NOW = datetime(2026, 11, 22, 10, 0, 0)


class Clock(unittest.TestCase):
    def test_every_gong_of_every_day_type_rings_exactly_once(self):
        schedules = core.parse_schedules(SCHEDULES_TEXT)
        for day_type, times in schedules.items():
            clock = core.GongClock()
            rung = []
            now = datetime(2027, 3, 1, 0, 0, 0)
            # The app looks four times a second; step the same way through a whole day.
            while now.date() == date(2027, 3, 1):
                rung += clock.due(now, times)
                now += timedelta(milliseconds=250)
            self.assertEqual(rung, times, day_type)

    def test_gong_still_rings_if_noticed_a_few_seconds_late(self):
        clock = core.GongClock()
        self.assertEqual(clock.due(datetime(2027, 3, 1, 4, 0, 7), ["04:00:00"]), ["04:00:00"])
        self.assertEqual(clock.due(datetime(2027, 3, 1, 4, 0, 8), ["04:00:00"]), [])

    def test_old_gongs_do_not_ring_when_the_app_starts_mid_day(self):
        clock = core.GongClock()
        self.assertEqual(clock.due(datetime(2027, 3, 1, 12, 0, 0), ["04:00:00", "11:00:00", "12:50:00"]), [])

    def test_gongs_ring_again_the_next_day(self):
        clock = core.GongClock()
        self.assertEqual(clock.due(datetime(2027, 3, 1, 4, 0, 0), ["04:00:00"]), ["04:00:00"])
        self.assertEqual(clock.due(datetime(2027, 3, 2, 4, 0, 0), ["04:00:00"]), ["04:00:00"])

    def test_empty_day_has_no_gongs(self):
        schedules = core.parse_schedules(SCHEDULES_TEXT)
        self.assertEqual(schedules["Empty"], [])

    def test_next_gong(self):
        self.assertEqual(core.next_gong(datetime(2027, 3, 1, 5, 0, 0), ["04:00:00", "06:30:10"]), "06:30:10")
        self.assertIsNone(core.next_gong(datetime(2027, 3, 1, 22, 0, 0), ["04:00:00", "06:30:10"]))


class Words(unittest.TestCase):
    def test_every_day_type_in_use_has_a_plain_name(self):
        for day_type in core.parse_schedules(SCHEDULES_TEXT):
            self.assertIn(day_type, core.DAY_NAMES)
        self.assertEqual(core.day_name("SomethingNew"), "SomethingNew")
        self.assertEqual(core.day_name(None), "No timetable entry")

    def test_short_time(self):
        self.assertEqual(core.short_time("04:00:00"), "04:00")
        self.assertEqual(core.short_time("06:30:10"), "06:30:10")

    def test_countdown(self):
        now = datetime(2027, 3, 1, 16, 28, 45)
        self.assertEqual(core.countdown(now, "17:00:00"), "in 32 min")
        self.assertEqual(core.countdown(now, "19:07:00"), "in 2 h 39 min")
        self.assertEqual(core.countdown(now, "16:29:10"), "in 25 s")
        self.assertEqual(core.countdown(now, "16:00:00"), "")

    def test_has_rung(self):
        clock = core.GongClock()
        clock.due(datetime(2027, 3, 1, 4, 0, 0), ["04:00:00", "06:30:10"])
        self.assertTrue(clock.has_rung(date(2027, 3, 1), "04:00:00"))
        self.assertFalse(clock.has_rung(date(2027, 3, 1), "06:30:10"))
        self.assertFalse(clock.has_rung(date(2027, 3, 2), "04:00:00"))


class Files(unittest.TestCase):
    def test_bad_files_are_rejected(self):
        for bad in ("", "Day,Kind\n2027-01-01,Day_0\n", "Date,Type\n01/02/2027,Day_0\n", "Date,Type\n2027-01-01,\n"):
            with self.assertRaises(core.DataError, msg=bad):
                core.parse_timetable(bad)
        for bad in ("", "[]", '{"Day_0": "07:50:00"}', '{"Day_0": ["7:50"]}', '{"Day_0": ["25:00:00"]}'):
            with self.assertRaises(core.DataError, msg=bad):
                core.parse_schedules(bad)


class Store(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        shutil.copy(FIX / "schedules.json", self.folder / "schedules.json")
        shutil.copy(FIX / "timetable.csv", self.folder / "timetable.csv")
        self.online = {"schedules.json": SCHEDULES_TEXT, "timetable.csv": TIMETABLE_TEXT}

    def store(self):
        def download(url):
            value = self.online[url.rsplit("/", 1)[-1]]
            if isinstance(value, Exception):
                raise value
            return value

        s = core.DataStore(self.folder, download=download)
        s.load_local()
        return s

    def test_loads_saved_files(self):
        s = self.store()
        self.assertIsNone(s.local_problem)
        self.assertEqual(s.day_type(date(2026, 11, 22)), "ShortCourse_Normal")
        self.assertEqual(s.day_type(date(2027, 4, 17)), "Empty")
        self.assertEqual(s.runs_until(), date(2028, 1, 19))

    def test_newer_timetable_is_picked_up_and_saved(self):
        self.online["timetable.csv"] = TIMETABLE_TEXT.replace("2027-03-10,Service_GSP", "2027-03-10,Empty")
        s = self.store()
        self.assertTrue(s.refresh(NOW))
        self.assertEqual(s.day_type(date(2027, 3, 10)), "Empty")
        self.assertIn("2027-03-10,Empty", (self.folder / "timetable.csv").read_text())
        self.assertFalse(s.refresh(NOW))  # nothing new the second time

    def test_new_gong_times_are_picked_up(self):
        self.online["schedules.json"] = SCHEDULES_TEXT.replace('"12:50:00", "14:00:00"]', '"12:50:00", "14:05:00"]')
        s = self.store()
        self.assertTrue(s.refresh(NOW))
        self.assertEqual(s.times_for("Day_0"), ["07:50:00", "12:50:00", "14:05:00"])

    def test_no_internet_keeps_the_saved_timetable(self):
        self.online["timetable.csv"] = OSError("offline")
        s = self.store()
        self.assertFalse(s.refresh(NOW))
        self.assertIn("could not download", s.online_problem)
        self.assertEqual(s.day_type(date(2026, 11, 22)), "ShortCourse_Normal")

    def test_bad_downloads_are_ignored(self):
        saved = (self.folder / "timetable.csv").read_text()
        cases = {
            "error page": "<html>Not found</html>",
            "cut off before today": "Date,Type\n2025-12-19,Day_0\n",
            "unknown type": TIMETABLE_TEXT.replace("2027-03-10,Service_GSP", "2027-03-10,Holiday"),
        }
        for label, text in cases.items():
            self.online["timetable.csv"] = text
            s = self.store()
            self.assertFalse(s.refresh(NOW), label)
            self.assertIn("rejected", s.online_problem, label)
            self.assertEqual(s.day_type(date(2027, 3, 10)), "Service_GSP", label)
            self.assertEqual((self.folder / "timetable.csv").read_text(), saved, label)

    def test_missing_saved_files_are_reported_then_fixed_by_download(self):
        (self.folder / "timetable.csv").unlink()
        s = self.store()
        self.assertIsNotNone(s.local_problem)
        self.assertFalse(s.healthy_today(NOW.date()))
        self.assertTrue(s.refresh(NOW))
        self.assertTrue(s.healthy_today(NOW.date()))
        self.assertTrue((self.folder / "timetable.csv").exists())

    def test_day_missing_from_timetable_is_not_healthy(self):
        s = self.store()
        self.assertFalse(s.healthy_today(date(2028, 1, 20)))
        self.assertEqual(s.times_for(s.day_type(date(2028, 1, 20))), [])

    def test_broken_settings_file_falls_back_to_defaults(self):
        (self.folder / "settings.json").write_text("{not json")
        self.assertEqual(core.load_settings(self.folder), core.DEFAULT_SETTINGS)
        (self.folder / "settings.json").write_text('{"refresh_hours": 1, "unknown": 5}')
        settings = core.load_settings(self.folder)
        self.assertEqual(settings["refresh_hours"], 1)
        self.assertNotIn("unknown", settings)


if __name__ == "__main__":
    unittest.main()
