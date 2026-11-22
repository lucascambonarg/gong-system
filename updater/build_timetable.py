"""Build timetable.csv for the gong app from the Dhamma Padhana course schedule.

Reads the public schedule page, turns every listed course into day types,
and merges the result into timetable.csv. It never guesses: anything it does
not recognise (a new course type, dates that do not add up, a day nothing
covers) stops the run with a clear message and leaves timetable.csv untouched.

Standard library only.

Usage:
    python updater/build_timetable.py                 # fetch the live page
    python updater/build_timetable.py --html page.html
    python updater/build_timetable.py --check         # report, write nothing
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEDULE_URL = "https://www.dhamma.org/en/schedules/schpadhana"

# The timetable must reach at least this far past today, or the run fails.
MIN_DAYS_AHEAD = 21

# Row class on the website -> how the course is coded.
#   ("long", N)   N-day long course
#   ("short", ..) short course: (span in days, has a Vipassana day)
#   "3day", "service", "other"
COURSE_KINDS = {
    "20-DayOSC": ("long", 20),
    "30-DayOSC": ("long", 30),
    "45-DayOSC": ("long", 45),
    "60-DayOSC": ("long", 60),
    "10-DaySpecial": ("short", 11, False),
    "Satipatthana": ("short", 9, False),
    "10-DayExecutive": ("short", 11, True),
    "10-DayOSC": ("short", 11, True),
    "3-DayOSC": ("3day",),
    "ServicePeriod": ("service",),
    "Other": ("other",),
}

# Day of a long course on which Vipassana is given (from the glossary).
VIPASSANA_DAY = {20: 8, 30: 11, 45: 16, 60: 21}
# Day of a 10-day course with a Vipassana day on which it is given.
SHORT_VIPASSANA_DAY = 4

# "Other" rows are only understood when they are one of these. Each maps to
# the type every day of the event gets.
OTHER_EVENTS = {"Trust Meeting": "Empty"}

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


class TimetableError(Exception):
    """The schedule could not be turned into a timetable safely."""


@dataclass(frozen=True)
class Entry:
    kind: str  # row class from the website, e.g. "30-DayOSC"
    name: str  # visible course name, e.g. "30-Day"
    start: date
    end: date

    def label(self) -> str:
        return f"{self.name} ({self.start:%d %b %Y} - {self.end:%d %b %Y})"


# --------------------------------------------------------------------------
# Reading the website
# --------------------------------------------------------------------------


def fetch(url: str = SCHEDULE_URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (timetable-updater)"})
    last_error: OSError | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(20 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except OSError as exc:  # network trouble: try again before giving up
            last_error = exc
    raise last_error


def _text(fragment: str) -> str:
    fragment = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(fragment)).strip()


def _parse_day(text: str, default_year: int) -> date:
    """'21 Dec' (year from the section heading) or '28 December, 2026'."""
    m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+)(?:,? (\d{4}))?", text)
    if not m:
        raise TimetableError(f"Could not read the date {text!r} on the schedule page.")
    month = MONTHS.get(m.group(2)[:3].lower())
    if month is None:
        raise TimetableError(f"Could not read the month in {text!r} on the schedule page.")
    year = int(m.group(3)) if m.group(3) else default_year
    try:
        return date(year, month, int(m.group(1)))
    except ValueError as exc:
        raise TimetableError(f"{text!r} is not a real date.") from exc


def parse_schedule(page: str) -> list[Entry]:
    """Every row of the course table, in page order."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    token = re.compile(
        r'<div class="[^"]*sch-header-year[^"]*">(?P<year>.*?)</div>'
        r'|<tr class="sch-row-\d+ (?P<kind>[^"]+)">(?P<row>.*?)</tr>',
        flags=re.S,
    )
    entries: list[Entry] = []
    year: int | None = None
    for m in token.finditer(page):
        if m.group("year") is not None:
            ym = re.search(r"\b(20\d\d)\b", _text(m.group("year")))
            if ym:
                year = int(ym.group(1))
            continue
        if year is None:
            raise TimetableError("Found a course row before any year heading on the schedule page.")
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group("row"), flags=re.S)
        if len(cells) < 3:
            raise TimetableError("A course row on the schedule page has an unexpected layout.")
        days = [_text(s) for s in re.findall(r"<span[^>]*>(.*?)</span>", cells[1], flags=re.S)]
        if len(days) != 2:
            raise TimetableError(
                f"Could not read the dates of a course row: {_text(cells[1])!r}."
            )
        start = _parse_day(days[0], year)
        end = _parse_day(days[1], year)
        entries.append(Entry(m.group("kind").strip(), _text(cells[2]), start, end))
    if not entries:
        raise TimetableError(
            "No courses were found on the schedule page. The page layout may have changed."
        )
    return entries


# --------------------------------------------------------------------------
# Turning courses into day types
# --------------------------------------------------------------------------


def _course_days(entry: Entry) -> dict[date, str]:
    kind = COURSE_KINDS[entry.kind]
    span = (entry.end - entry.start).days
    day = lambda n: entry.start + timedelta(days=n)  # noqa: E731
    out = {entry.start: "Day_0"}

    if kind[0] == "long":
        n = kind[1]
        if span != n + 1:
            raise TimetableError(
                f"{entry.label()} is listed as a {n}-day course but spans {span} days "
                f"(expected {n + 1})."
            )
        for i in range(1, span):
            out[day(i)] = "LongCourse_Normal"
        out[day(VIPASSANA_DAY[n])] = "LongCourse_Vipassana"
        if n == 20:
            out[day(span - 1)] = "LongCourse_Metta_1"
        else:
            out[day(span - 2)] = "LongCourse_Metta_1"
            out[day(span - 1)] = "LongCourse_Metta_2"
        out[entry.end] = "LongCourse_Final"

    elif kind[0] == "short":
        _, expected, has_vipassana = kind
        if span != expected:
            raise TimetableError(
                f"{entry.label()} spans {span} days (expected {expected} for this course type)."
            )
        for i in range(1, span):
            out[day(i)] = "ShortCourse_Normal"
        if has_vipassana:
            out[day(SHORT_VIPASSANA_DAY)] = "10day_Vipassana"
        out[day(span - 1)] = "ShortCourse_Metta"
        out[entry.end] = "ShortCourse_Final"

    elif kind[0] == "3day":
        if span != 4:
            raise TimetableError(f"{entry.label()} spans {span} days (expected 4 for a 3-day course).")
        for i in (1, 2, 3):
            out[day(i)] = f"3Day_{i}"
        out[entry.end] = "3Day_Final"

    return out


def generate(entries: list[Entry]) -> dict[date, str]:
    """Day types for every day from the first listed start to the last listed end."""
    for e in entries:
        if e.kind not in COURSE_KINDS:
            raise TimetableError(
                f"Unknown course type on the schedule page: {e.name!r} (code {e.kind!r}, "
                f"{e.start:%d %b %Y} - {e.end:%d %b %Y}). The rules do not cover it yet."
            )
        if e.end < e.start:
            raise TimetableError(f"{e.label()} ends before it starts.")
        if COURSE_KINDS[e.kind][0] == "other" and e.name not in OTHER_EVENTS:
            raise TimetableError(
                f"Unknown event on the schedule page: {e.label()}. The rules do not cover it yet."
            )

    days: dict[date, str] = {}
    owner: dict[date, Entry] = {}

    courses = [e for e in entries if COURSE_KINDS[e.kind][0] in ("long", "short", "3day")]
    if not courses:
        raise TimetableError("The schedule page lists no courses, only service periods or events.")
    for e in courses:
        for d, t in _course_days(e).items():
            if d in days:
                raise TimetableError(f"{e.label()} overlaps {owner[d].label()} on {d:%d %b %Y}.")
            days[d], owner[d] = t, e

    for e in entries:
        if COURSE_KINDS[e.kind][0] != "other":
            continue
        d = e.start
        while d <= e.end:
            if d in days:
                raise TimetableError(f"{e.label()} falls on a course day ({owner[d].label()}).")
            days[d], owner[d] = OTHER_EVENTS[e.name], e
            d += timedelta(days=1)

    # Every remaining day must sit inside a listed service period.
    service_days: set[date] = set()
    for e in entries:
        if COURSE_KINDS[e.kind][0] == "service":
            d = e.start
            while d <= e.end:
                service_days.add(d)
                d += timedelta(days=1)

    first = min(e.start for e in courses)
    last = max(e.end for e in courses)
    result: dict[date, str] = {}
    d = first
    while d <= last:
        if d in days:
            result[d] = days[d]
        elif d in service_days:
            result[d] = "Service_GSP"
        else:
            raise TimetableError(
                f"{d:%a %d %b %Y} is not covered by any course, service period or event on "
                f"the schedule page."
            )
        d += timedelta(days=1)
    return result


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def read_timetable(path: Path) -> dict[date, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or [c.strip() for c in rows[0][:2]] != ["Date", "Type"]:
        raise TimetableError(f"{path.name} must start with the header 'Date,Type'.")
    out: dict[date, str] = {}
    for n, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        try:
            d = date.fromisoformat(row[0].strip())
        except ValueError as exc:
            raise TimetableError(f"{path.name} line {n}: {row[0]!r} is not a YYYY-MM-DD date.") from exc
        if d in out:
            raise TimetableError(f"{path.name} line {n}: {d} appears twice.")
        out[d] = row[1].strip() if len(row) > 1 else ""
    return out


def write_timetable(path: Path, days: dict[date, str]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["Date", "Type"])
        for d in sorted(days):
            w.writerow([d.isoformat(), days[d]])
    tmp.replace(path)


def merge(existing: dict[date, str], generated: dict[date, str]) -> dict[date, str]:
    """Keep history before the website's first listed course; replace the rest."""
    first, last = min(generated), max(generated)
    if existing:
        prev_last = max(existing)
        if first > prev_last + timedelta(days=1):
            raise TimetableError(
                f"The schedule page now starts on {first:%d %b %Y}, but the timetable only "
                f"runs to {prev_last:%d %b %Y}. The days in between are missing."
            )
        if prev_last > last:
            raise TimetableError(
                f"The timetable runs to {prev_last:%d %b %Y}, but the schedule page now ends "
                f"on {last:%d %b %Y}. A course at the end may have been removed; check by hand."
            )
    merged = {d: t for d, t in existing.items() if d < first}
    merged.update(generated)
    return merged


def apply_overrides(days: dict[date, str], overrides: dict[date, str]) -> list[date]:
    applied = []
    for d, t in overrides.items():
        if d in days and days[d] != t:
            days[d] = t
            applied.append(d)
    return applied


def validate(days: dict[date, str], valid_types: set[str], today: date) -> None:
    ordered = sorted(days)
    for a, b in zip(ordered, ordered[1:]):
        if b - a != timedelta(days=1):
            raise TimetableError(f"The timetable has a gap between {a} and {b}.")
    for d in ordered:
        if days[d] not in valid_types:
            raise TimetableError(f"{d}: {days[d]!r} is not a day type in schedules.json.")
    if today not in days:
        raise TimetableError(f"Today ({today}) is not in the timetable.")
    ahead = (ordered[-1] - today).days
    if ahead < MIN_DAYS_AHEAD:
        raise TimetableError(
            f"The timetable only runs {ahead} more days (to {ordered[-1]:%d %b %Y}). "
            f"The schedule page has no later courses yet."
        )


def describe_changes(old: dict[date, str], new: dict[date, str]) -> list[str]:
    """Human-readable list of what differs, with consecutive days grouped."""
    changed = [d for d in sorted(set(old) | set(new)) if old.get(d) != new.get(d)]
    lines: list[str] = []
    i = 0
    while i < len(changed):
        j = i
        while (
            j + 1 < len(changed)
            and changed[j + 1] - changed[j] == timedelta(days=1)
            and old.get(changed[j + 1]) == old.get(changed[i])
            and new.get(changed[j + 1]) == new.get(changed[i])
        ):
            j += 1
        a, b = changed[i], changed[j]
        span = f"{a:%d %b %Y}" if a == b else f"{a:%d %b %Y} to {b:%d %b %Y}"
        before, after = old.get(a, "(not in timetable)"), new.get(a, "(removed)")
        lines.append(f"{span}: {before} -> {after}")
        i = j + 1
    return lines


# --------------------------------------------------------------------------


def run(page: str, repo: Path, today: date, write: bool) -> list[str]:
    valid_types = set(json.loads((repo / "schedules.json").read_text(encoding="utf-8")))
    existing = read_timetable(repo / "timetable.csv")
    overrides = read_timetable(repo / "overrides.csv")
    for d, t in overrides.items():
        if t not in valid_types:
            raise TimetableError(f"overrides.csv: {d} has unknown day type {t!r}.")

    merged = merge(existing, generate(parse_schedule(page)))
    apply_overrides(merged, overrides)
    validate(merged, valid_types, today)

    changes = describe_changes(existing, merged)
    if write and changes:
        write_timetable(repo / "timetable.csv", merged)
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--html", type=Path, help="read the schedule from a saved page instead of the website")
    ap.add_argument("--repo", type=Path, default=REPO, help="folder holding timetable.csv and schedules.json")
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="pretend today is this date (testing)")
    ap.add_argument("--check", action="store_true", help="report what would change; write nothing")
    ap.add_argument("--summary", type=Path, help="also write the result to this text file")
    args = ap.parse_args(argv)

    def report(text: str) -> None:
        print(text)
        if args.summary:
            args.summary.write_text(text + "\n", encoding="utf-8")

    try:
        page = args.html.read_text(encoding="utf-8") if args.html else fetch()
        changes = run(page, args.repo, args.today or date.today(), write=not args.check)
    except TimetableError as exc:
        report(f"STOPPED, timetable left unchanged: {exc}")
        return 1
    except OSError as exc:
        report(f"STOPPED, timetable left unchanged: could not read the schedule ({exc}).")
        return 1

    if changes:
        verb = "would change" if args.check else "changed"
        report(f"Timetable {verb} ({len(changes)} range(s)):\n" + "\n".join(f"- {c}" for c in changes))
    else:
        report("Timetable is already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
