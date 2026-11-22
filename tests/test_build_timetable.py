"""Tests for the timetable builder. Run with: python -m unittest discover tests"""

import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "updater"))

import build_timetable as bt  # noqa: E402

FIXTURE = (REPO / "tests" / "fixtures" / "schedule_2026-11-22.html").read_text(encoding="utf-8")
TODAY = date(2026, 11, 22)
E = bt.Entry


class FromSavedPage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("timetable.csv", "schedules.json"):
            shutil.copy(REPO / "tests" / "fixtures" / name, self.tmp / name)
        (self.tmp / "overrides.csv").write_text("Date,Type\n")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_reproduces_the_hand_checked_timetable(self):
        # The fixture timetable was filled and checked by hand against the same page.
        self.assertEqual(bt.run(FIXTURE, self.tmp, TODAY, write=False), [])

    def test_rebuilds_missing_future_and_keeps_history(self):
        full = bt.read_timetable(self.tmp / "timetable.csv")
        cut = {d: t for d, t in full.items() if d <= date(2027, 2, 6)}
        cut[date(2026, 3, 1)] = "Empty"  # a hand edit in the past must survive
        bt.write_timetable(self.tmp / "timetable.csv", cut)
        changes = bt.run(FIXTURE, self.tmp, TODAY, write=True)
        self.assertTrue(changes)
        rebuilt = bt.read_timetable(self.tmp / "timetable.csv")
        self.assertEqual(rebuilt[date(2026, 3, 1)], "Empty")
        full[date(2026, 3, 1)] = "Empty"
        self.assertEqual(rebuilt, full)

    def test_override_wins_over_the_website(self):
        (self.tmp / "overrides.csv").write_text("Date,Type\n2027-03-10,Empty\n")
        changes = bt.run(FIXTURE, self.tmp, TODAY, write=True)
        self.assertEqual(changes, ["10 Mar 2027: Service_GSP -> Empty"])
        self.assertEqual(bt.read_timetable(self.tmp / "timetable.csv")[date(2027, 3, 10)], "Empty")

    def test_override_with_unknown_type_stops(self):
        (self.tmp / "overrides.csv").write_text("Date,Type\n2027-03-10,Holiday\n")
        with self.assertRaisesRegex(bt.TimetableError, "unknown day type"):
            bt.run(FIXTURE, self.tmp, TODAY, write=True)

    def test_unknown_course_type_stops_and_writes_nothing(self):
        before = (self.tmp / "timetable.csv").read_text()
        page = FIXTURE.replace('sch-row-1 45-DayOSC"', 'sch-row-1 TeenagerCourse"')
        with self.assertRaisesRegex(bt.TimetableError, "Unknown course type"):
            bt.run(page, self.tmp, TODAY, write=True)
        self.assertEqual((self.tmp / "timetable.csv").read_text(), before)

    def test_empty_page_stops(self):
        with self.assertRaisesRegex(bt.TimetableError, "No courses were found"):
            bt.run("<html><body>Maintenance</body></html>", self.tmp, TODAY, write=True)

    def test_timetable_running_out_stops(self):
        with self.assertRaisesRegex(bt.TimetableError, "only runs"):
            bt.run(FIXTURE, self.tmp, date(2028, 1, 10), write=False)


class Rules(unittest.TestCase):
    def days(self, kind, name, start, end):
        return bt.generate([E(kind, name, start, end)])

    def test_30_day(self):
        d = self.days("30-DayOSC", "30-Day", date(2027, 3, 14), date(2027, 4, 14))
        self.assertEqual(d[date(2027, 3, 14)], "Day_0")
        self.assertEqual(d[date(2027, 3, 25)], "LongCourse_Vipassana")  # day 11
        self.assertEqual(d[date(2027, 4, 12)], "LongCourse_Metta_1")
        self.assertEqual(d[date(2027, 4, 13)], "LongCourse_Metta_2")
        self.assertEqual(d[date(2027, 4, 14)], "LongCourse_Final")
        self.assertEqual(sum(t == "LongCourse_Normal" for t in d.values()), 27)

    def test_20_day_has_only_metta_1(self):
        d = self.days("20-DayOSC", "20-Day", date(2027, 1, 31), date(2027, 2, 21))
        self.assertEqual(d[date(2027, 2, 8)], "LongCourse_Vipassana")  # day 8
        self.assertEqual(d[date(2027, 2, 20)], "LongCourse_Metta_1")
        self.assertNotIn("LongCourse_Metta_2", d.values())

    def test_45_and_60_day_vipassana_days(self):
        d = self.days("45-DayOSC", "45-Day", date(2027, 7, 23), date(2027, 9, 7))
        self.assertEqual(d[date(2027, 8, 8)], "LongCourse_Vipassana")  # day 16
        d = self.days("60-DayOSC", "60-Day", date(2026, 2, 14), date(2026, 4, 16))
        self.assertEqual(d[date(2026, 3, 7)], "LongCourse_Vipassana")  # day 21

    def test_satipatthana_and_special_have_metta_but_no_vipassana_day(self):
        for kind, name, s, e in [
            ("Satipatthana", "Satipatthana Sutta", date(2027, 4, 19), date(2027, 4, 28)),
            ("10-DaySpecial", "Special 10-Day", date(2027, 2, 24), date(2027, 3, 7)),
        ]:
            d = self.days(kind, name, s, e)
            self.assertNotIn("10day_Vipassana", d.values())
            self.assertEqual(list(d.values())[-2:], ["ShortCourse_Metta", "ShortCourse_Final"])

    def test_executive_and_old_student_10_day_have_vipassana_on_day_4(self):
        for kind in ("10-DayExecutive", "10-DayOSC"):
            d = self.days(kind, "x", date(2027, 5, 5), date(2027, 5, 16))
            self.assertEqual(d[date(2027, 5, 9)], "10day_Vipassana")
            self.assertEqual(d[date(2027, 5, 15)], "ShortCourse_Metta")

    def test_3_day(self):
        d = self.days("3-DayOSC", "3-Day", date(2027, 12, 9), date(2027, 12, 13))
        self.assertEqual(list(d.values()), ["Day_0", "3Day_1", "3Day_2", "3Day_3", "3Day_Final"])

    def test_service_days_are_only_the_days_between_courses(self):
        d = bt.generate([
            E("3-DayOSC", "3-Day", date(2027, 12, 9), date(2027, 12, 13)),
            E("ServicePeriod", "General Service Period", date(2027, 12, 13), date(2027, 12, 17)),
            E("3-DayOSC", "3-Day", date(2027, 12, 17), date(2027, 12, 21)),
        ])
        self.assertEqual(d[date(2027, 12, 13)], "3Day_Final")
        self.assertEqual([d[date(2027, 12, n)] for n in (14, 15, 16)], ["Service_GSP"] * 3)
        self.assertEqual(d[date(2027, 12, 17)], "Day_0")

    def test_trust_meeting_days_are_empty(self):
        d = bt.generate([
            E("3-DayOSC", "3-Day", date(2027, 4, 10), date(2027, 4, 14)),
            E("ServicePeriod", "General Service Period", date(2027, 4, 14), date(2027, 4, 19)),
            E("Other", "Trust Meeting", date(2027, 4, 17), date(2027, 4, 18)),
            E("3-DayOSC", "3-Day", date(2027, 4, 19), date(2027, 4, 23)),
        ])
        self.assertEqual([d[date(2027, 4, n)] for n in (15, 16, 17, 18)],
                         ["Service_GSP", "Service_GSP", "Empty", "Empty"])

    def test_wrong_length_stops(self):
        with self.assertRaisesRegex(bt.TimetableError, "spans 30 days"):
            self.days("30-DayOSC", "30-Day", date(2027, 3, 14), date(2027, 4, 13))

    def test_unknown_event_stops(self):
        with self.assertRaisesRegex(bt.TimetableError, "Unknown event"):
            bt.generate([
                E("3-DayOSC", "3-Day", date(2027, 4, 10), date(2027, 4, 14)),
                E("Other", "Open Day", date(2027, 4, 15), date(2027, 4, 15)),
            ])

    def test_uncovered_day_stops(self):
        with self.assertRaisesRegex(bt.TimetableError, "not covered"):
            bt.generate([
                E("3-DayOSC", "3-Day", date(2027, 12, 9), date(2027, 12, 13)),
                E("3-DayOSC", "3-Day", date(2027, 12, 17), date(2027, 12, 21)),
            ])

    def test_overlapping_courses_stop(self):
        with self.assertRaisesRegex(bt.TimetableError, "overlaps"):
            bt.generate([
                E("3-DayOSC", "3-Day", date(2027, 12, 9), date(2027, 12, 13)),
                E("3-DayOSC", "3-Day", date(2027, 12, 12), date(2027, 12, 16)),
            ])


if __name__ == "__main__":
    unittest.main()
