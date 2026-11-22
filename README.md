# Gong System

Rings the gong at Dhamma Padhāna at the right times for each type of day,
with no manual upkeep.

```
course schedule on dhamma.org
        │  read every day by a GitHub job
        ▼
timetable.csv  +  schedules.json      (in this repository)
        │  downloaded by the app, several times a day
        ▼
GongApp.exe on the tablet  →  gong
```

## The files that matter

| File | What it is | Who changes it |
|---|---|---|
| `timetable.csv` | One line per date with its type of day | The daily job. Do not edit by hand; use `overrides.csv` |
| `overrides.csv` | Dates you want to force to a given type | You |
| `schedules.json` | Gong times for each type of day | You |

Anything changed here reaches the tablet by itself within a few hours.

### Changing a day by hand

Add a line to `overrides.csv`. It wins over the website for that date and
survives every later update:

```
Date,Type
2027-06-12,Empty
```

The type must be one of the names in `schedules.json`.

### Changing gong times

Edit `schedules.json`. Times are `HH:MM:SS`. The 04:00:00 gong is played twice.

## How dates become day types

| Course on the website | Day types |
|---|---|
| 20, 30, 45, 60-Day | `Day_0`, then `LongCourse_Normal`; Vipassana on day 8 / 11 / 16 / 21; `Metta_1` and `Metta_2` on the last two full days (20-day: `Metta_1` only); `LongCourse_Final` on the end date |
| Special 10-Day, Satipatthana Sutta | `Day_0`, `ShortCourse_Normal`, `ShortCourse_Metta` the day before the end, `ShortCourse_Final`. No Vipassana day |
| Executive Course, 10-Day for Old Students | As above, plus `10day_Vipassana` on day 4 |
| 3-Day | `Day_0`, `3Day_1`, `3Day_2`, `3Day_3`, `3Day_Final` |
| General Service Period | `Service_GSP` on the days strictly between two courses |
| Trust Meeting | `Empty` on each day of the meeting |

A course's start date is always `Day_0` and its end date is always the
`Final` type.

## When something is wrong

The daily job never guesses. If the website lists a course type it does not
know, dates that do not add up, or a day nothing covers, it leaves
`timetable.csv` untouched and opens an issue in this repository called
"Timetable update needs attention" (GitHub emails the owner). The app keeps
ringing from the timetable it already has. The issue closes itself once an
update succeeds again.

A new course type is added in `COURSE_KINDS` in `updater/build_timetable.py`.

## The app

Source in `app/`. It needs only the files in its own folder, so it runs on
any Windows device. See `app/INSTALL.txt` for putting it on the tablet.

Build `GongApp.exe`: Actions tab → "Build gong app" → "Run workflow", then
download the `GongApp` artifact. Pushing a tag such as `v3.0` also publishes
it as a release.

## Working on it

```
python -m unittest discover tests          # all tests
python updater/build_timetable.py --check  # what would change, writes nothing
python app/gong_app.py --selftest          # needs the data files beside it
```
