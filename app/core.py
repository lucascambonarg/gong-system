"""Gong app logic with no window and no sound, so it can be tested anywhere.

- where the app's files live (always next to the app itself)
- reading and checking the timetable (timetable.csv) and gong times (schedules.json)
- downloading newer copies and keeping the last good ones
- deciding which gongs are due
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

RAW = "https://raw.githubusercontent.com/lucascambonarg/gong-system/main/"

DEFAULT_SETTINGS = {
    "timetable_url": RAW + "timetable.csv",
    "schedules_url": RAW + "schedules.json",
    # How often to look for a newer timetable when everything is fine.
    "refresh_hours": 6,
    # How often to retry when today's date is missing or the files are bad.
    "retry_minutes": 15,
    "sound_file": "Gong Recorded (edited).wav",
    "icon_file": "PlayIcon.png",
}

TIMETABLE_FILE = "timetable.csv"
SCHEDULES_FILE = "schedules.json"
SETTINGS_FILE = "settings.json"

# The gong at this time is played twice, as in earlier versions.
DOUBLE_GONG_TIME = "04:00:00"
DOUBLE_GONG_GAP_MS = 39_000

# A gong still rings if the app notices it up to this many seconds late
# (for example if the tablet was busy for a moment).
GRACE_SECONDS = 20

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


class DataError(Exception):
    """A timetable or schedules file is not usable."""


def app_dir() -> Path:
    """Folder the app runs from: beside the .exe when packaged, else this folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_settings(folder: Path) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    path = folder / SETTINGS_FILE
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings.update({k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass  # a broken settings file must never stop the gongs
    return settings


# --------------------------------------------------------------------------
# Reading and checking the two data files
# --------------------------------------------------------------------------


def parse_schedules(text: str) -> dict[str, list[str]]:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise DataError(f"schedules file is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict) or not raw:
        raise DataError("schedules file has no day types")
    out: dict[str, list[str]] = {}
    for day_type, times in raw.items():
        if not isinstance(times, list):
            raise DataError(f"schedules file: {day_type!r} is not a list of times")
        cleaned = []
        for t in times:
            if t == "" or t is None:
                continue  # "Empty" is written as [""]
            if not isinstance(t, str) or not _TIME_RE.match(t):
                raise DataError(f"schedules file: {t!r} in {day_type!r} is not an HH:MM:SS time")
            cleaned.append(t)
        out[str(day_type)] = sorted(set(cleaned))
    return out


def parse_timetable(text: str) -> dict[date, str]:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or [c.strip() for c in rows[0][:2]] != ["Date", "Type"]:
        raise DataError("timetable file must start with the header 'Date,Type'")
    out: dict[date, str] = {}
    for n, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        try:
            d = date.fromisoformat(row[0].strip())
        except ValueError as exc:
            raise DataError(f"timetable file line {n}: {row[0]!r} is not a YYYY-MM-DD date") from exc
        day_type = row[1].strip() if len(row) > 1 else ""
        if not day_type:
            raise DataError(f"timetable file line {n}: {d} has no day type")
        out[d] = day_type
    if not out:
        raise DataError("timetable file has no dates")
    return out


def check_pair(timetable: dict[date, str], schedules: dict[str, list[str]], today: date) -> None:
    """A downloaded timetable is only accepted if the app could run on it today."""
    if today not in timetable:
        raise DataError(f"timetable has no entry for today ({today})")
    unknown = sorted({t for d, t in timetable.items() if d >= today and t not in schedules})
    if unknown:
        raise DataError(f"timetable uses day types with no gong times: {', '.join(unknown)}")


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "GongApp", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


class DataStore:
    """The timetable and gong times in use, plus where they came from."""

    def __init__(self, folder: Path, settings: dict | None = None, download=_download):
        self.folder = folder
        self.settings = settings or load_settings(folder)
        self._download = download
        self.timetable: dict[date, str] = {}
        self.schedules: dict[str, list[str]] = {}
        self.local_problem: str | None = None  # saved files unreadable
        self.online_problem: str | None = None  # last download attempt failed
        self.last_checked: datetime | None = None  # last successful download
        self.last_attempt: datetime | None = None

    def load_local(self) -> None:
        """Read the copies saved beside the app. Never raises."""
        try:
            schedules = parse_schedules((self.folder / SCHEDULES_FILE).read_text(encoding="utf-8"))
            timetable = parse_timetable((self.folder / TIMETABLE_FILE).read_text(encoding="utf-8"))
        except (OSError, DataError) as exc:
            self.local_problem = str(exc)
            return
        self.schedules, self.timetable, self.local_problem = schedules, timetable, None

    def refresh(self, now: datetime | None = None) -> bool:
        """Download newer copies. Returns True if what the app uses changed.

        A download that fails, or a file that does not pass the checks, is
        ignored and the app carries on with what it already has. Never raises.
        """
        now = now or datetime.now()
        self.last_attempt = now
        try:
            schedules_text = self._download(self.settings["schedules_url"])
            timetable_text = self._download(self.settings["timetable_url"])
        except Exception as exc:  # no internet, timeout, server error
            self.online_problem = f"could not download ({exc.__class__.__name__})"
            return False
        try:
            schedules = parse_schedules(schedules_text)
            timetable = parse_timetable(timetable_text)
            check_pair(timetable, schedules, now.date())
        except DataError as exc:
            self.online_problem = f"downloaded files rejected: {exc}"
            return False

        changed = schedules != self.schedules or timetable != self.timetable
        if changed:
            try:
                _write_atomic(self.folder / SCHEDULES_FILE, schedules_text)
                _write_atomic(self.folder / TIMETABLE_FILE, timetable_text)
            except OSError as exc:
                # Still use the new data in memory; it just is not saved for next start.
                self.online_problem = f"could not save the new files ({exc})"
            else:
                self.online_problem = None
            self.schedules, self.timetable, self.local_problem = schedules, timetable, None
        else:
            self.online_problem = None
        self.last_checked = now
        return changed

    # -- questions the window asks -----------------------------------------

    def day_type(self, day: date) -> str | None:
        return self.timetable.get(day)

    def times_for(self, day_type: str | None) -> list[str]:
        return self.schedules.get(day_type, []) if day_type else []

    def runs_until(self) -> date | None:
        return max(self.timetable) if self.timetable else None

    def healthy_today(self, today: date) -> bool:
        day_type = self.day_type(today)
        return day_type is not None and day_type in self.schedules


# --------------------------------------------------------------------------
# Which gongs are due
# --------------------------------------------------------------------------


class GongClock:
    """Remembers which of today's gongs have rung, so each rings exactly once."""

    def __init__(self, grace_seconds: int = GRACE_SECONDS):
        self.grace = timedelta(seconds=grace_seconds)
        self._day: date | None = None
        self._rung: set[str] = set()

    def due(self, now: datetime, times: list[str]) -> list[str]:
        """Times that should ring right now and have not rung yet today."""
        if now.date() != self._day:
            self._day, self._rung = now.date(), set()
        out = []
        for t in times:
            if t in self._rung:
                continue
            h, m, s = (int(x) for x in t.split(":"))
            at = now.replace(hour=h, minute=m, second=s, microsecond=0)
            if at <= now < at + self.grace:
                self._rung.add(t)
                out.append(t)
        return out


def next_gong(now: datetime, times: list[str]) -> str | None:
    current = now.strftime("%H:%M:%S")
    later = [t for t in times if t > current]
    return min(later) if later else None
