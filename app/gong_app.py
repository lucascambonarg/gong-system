"""Gong System, version 3.

Plays the gong at the times set for today's type of day.

Same screen and behaviour as version 2, with these differences:
- all files are found next to the app, so it runs on any Windows device
- no file to select at start-up: the timetable is downloaded automatically
  and a saved copy is used whenever the internet is not available
- a bad or missing timetable never stops the app silently: it says so on
  screen and keeps trying

Run "GongApp.exe --selftest" to check the files without opening the window.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

import core

VERSION = "3.0"
TICK_MS = 250

log = logging.getLogger("gong")


def setup_logging(folder) -> None:
    try:
        handler = RotatingFileHandler(folder / "gong_app.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# --------------------------------------------------------------------------
# Sound
# --------------------------------------------------------------------------


class Gong:
    """Plays the gong recording. Problems are reported, never raised."""

    def __init__(self, sound_path):
        self.sound_path = sound_path
        self.problem: str | None = None
        self._lock = threading.Lock()
        self._ready = False

    def _prepare(self) -> bool:
        if self._ready:
            return True
        if not self.sound_path.exists():
            self.problem = f"Sound file missing: {self.sound_path.name}"
            return False
        try:
            import pygame

            pygame.mixer.init()
            self._ready = True
            self.problem = None
        except Exception as exc:
            self.problem = f"Sound not available ({exc})"
        return self._ready

    def _play_blocking(self) -> None:
        with self._lock:
            if not self._prepare():
                log.error("gong not played: %s", self.problem)
                return
            try:
                import pygame

                pygame.mixer.music.load(str(self.sound_path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
            except Exception as exc:
                self._ready = False  # re-open the sound device next time
                self.problem = f"Sound failed ({exc})"
                log.exception("gong failed")

    def play(self) -> None:
        threading.Thread(target=self._play_blocking, daemon=True).start()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


class GongWindow:
    def __init__(self, folder):
        import tkinter as tk

        self.tk = tk
        self.folder = folder
        self.store = core.DataStore(folder)
        self.store.load_local()
        self.clock = core.GongClock()
        self.gong = Gong(folder / self.store.settings["sound_file"])

        self.today = date.today()
        self.deactivated_today = False
        self._refreshing = False
        self._next_refresh = datetime.now()  # try straight away

        root = self.root = tk.Tk()
        root.title("Gong System")

        tk.Label(root, text="Gong System", font=("Arial", 50)).pack(pady=20)
        started = time.strftime("%d-%m-%Y %H:%M", time.localtime())
        tk.Label(root, text=f"Running since: {started}", font=("Arial", 25)).pack(pady=10)
        self.day_label = tk.Label(root, text="", font=("Arial", 30))
        self.day_label.pack(pady=10)
        self.clock_label = tk.Label(root, text="", font=("Arial", 30))
        self.clock_label.pack(pady=10)

        self._icon = None
        try:
            self._icon = tk.PhotoImage(file=str(folder / self.store.settings["icon_file"]))
            play = tk.Button(root, image=self._icon, command=self.gong.play)
        except Exception:
            play = tk.Button(root, text="Play gong", font=("Arial", 25), command=self.gong.play)
        play.pack(pady=20)

        self.toggle_button = tk.Button(
            root, text="Deactivate automations for Today", font=("Arial", 16), command=self.toggle_automations
        )
        self.toggle_button.pack(pady=20)

        self.alert_label = tk.Label(root, text="", font=("Arial", 20, "bold"), fg="#b00020", wraplength=900)
        self.alert_label.pack(pady=5)
        self.status_label = tk.Label(root, text="", font=("Arial", 12), fg="#555555", wraplength=900)
        self.status_label.pack(pady=5)
        tk.Label(root, text=f"Version {VERSION}", font=("Arial", 9), fg="#888888").pack(side="bottom", pady=4)

        root.bind("<space>", lambda event: self.gong.play())
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        log.info("started, version %s, folder %s", VERSION, folder)
        self.tick()

    # -- actions -----------------------------------------------------------

    def toggle_automations(self) -> None:
        self.deactivated_today = not self.deactivated_today
        log.info("automations %s for today", "deactivated" if self.deactivated_today else "activated")

    def current_type(self) -> str | None:
        if self.deactivated_today:
            return "Empty"
        return self.store.day_type(self.today)

    def start_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True

        def work():
            try:
                changed = self.store.refresh()
                if changed:
                    log.info("timetable updated, now runs to %s", self.store.runs_until())
                elif self.store.online_problem:
                    log.warning("update check: %s", self.store.online_problem)
            except Exception:
                log.exception("update check crashed")
            finally:
                self._refreshing = False

        threading.Thread(target=work, daemon=True).start()

    # -- the loop ----------------------------------------------------------

    def tick(self) -> None:
        try:
            self._tick()
        except Exception:
            log.exception("tick failed")  # whatever happens, keep running
        self.root.after(TICK_MS, self.tick)

    def _tick(self) -> None:
        now = datetime.now()
        if now.date() != self.today:
            self.today = now.date()
            self.deactivated_today = False
            self._next_refresh = now  # look for a newer timetable at the start of each day
            log.info("day changed to %s, type %s", self.today, self.store.day_type(self.today))

        if now >= self._next_refresh and not self._refreshing:
            healthy = self.store.healthy_today(self.today)
            wait = (
                timedelta(hours=self.store.settings["refresh_hours"])
                if healthy
                else timedelta(minutes=self.store.settings["retry_minutes"])
            )
            self._next_refresh = now + wait
            self.start_refresh()

        day_type = self.current_type()
        times = self.store.times_for(day_type)
        for t in self.clock.due(now, times):
            log.info("gong %s (%s)", t, day_type)
            self.gong.play()
            if t == core.DOUBLE_GONG_TIME:
                self.root.after(core.DOUBLE_GONG_GAP_MS, self.gong.play)

        self.clock_label.config(text=now.strftime("%H:%M:%S"))
        self.day_label.config(text=f"Current Type: {day_type if day_type else 'none'}")
        self.toggle_button.config(
            text="Activate automations for Today" if self.deactivated_today else "Deactivate automations for Today"
        )
        self.alert_label.config(text=self.alert_text(day_type))
        self.status_label.config(text=self.status_text(now, times))

    def alert_text(self, day_type) -> str:
        if self.gong.problem:
            return f"{self.gong.problem}. Gongs cannot be played."
        if self.deactivated_today:
            return ""
        if day_type is None:
            return (
                f"No timetable entry for today ({self.today:%d-%m-%Y}). "
                "No automatic gongs. Ring the gong by hand."
            )
        if day_type not in self.store.schedules:
            return f"No gong times known for day type '{day_type}'. No automatic gongs. Ring the gong by hand."
        return ""

    def status_text(self, now, times) -> str:
        parts = []
        nxt = core.next_gong(now, times)
        parts.append(f"Next gong today: {nxt}" if nxt else "No more gongs today")
        until = self.store.runs_until()
        if until:
            parts.append(f"Timetable runs to {until:%d-%m-%Y}")
        if self.store.last_checked:
            parts.append(f"last checked online {self.store.last_checked:%d-%m-%Y %H:%M}")
        if self.store.online_problem:
            parts.append(f"using saved timetable ({self.store.online_problem})")
        return "   |   ".join(parts)

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------


def selftest(folder, online: bool) -> int:
    """Check the files beside the app without opening a window. 0 means OK."""
    lines = [f"Gong System {VERSION} self-test", f"Folder: {folder}"]
    store = core.DataStore(folder)
    store.load_local()
    ok = True
    if store.local_problem:
        ok = False
        lines.append(f"FAIL saved files: {store.local_problem}")
    else:
        lines.append(f"OK   saved files: {len(store.timetable)} days, to {store.runs_until()}")
    if online:
        store.refresh()
        if store.online_problem:
            ok = False
            lines.append(f"FAIL download: {store.online_problem}")
        else:
            lines.append(f"OK   download: {len(store.timetable)} days, to {store.runs_until()}")
    today = date.today()
    day_type = store.day_type(today)
    if store.healthy_today(today):
        lines.append(f"OK   today {today}: {day_type}, gongs at {', '.join(store.times_for(day_type)) or 'none'}")
    else:
        ok = False
        lines.append(f"FAIL today {today}: day type {day_type!r} not usable")
    for name in (store.settings["sound_file"], store.settings["icon_file"]):
        present = (folder / name).exists()
        ok = ok and present
        lines.append(f"{'OK  ' if present else 'FAIL'} file present: {name}")
    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    text = "\n".join(lines)
    try:
        (folder / "selftest.txt").write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    if sys.stdout:
        print(text)
    return 0 if ok else 1


def main() -> int:
    folder = core.app_dir()
    if "--selftest" in sys.argv or "--selftest-online" in sys.argv:
        return selftest(folder, online="--selftest-online" in sys.argv)
    setup_logging(folder)
    GongWindow(folder).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
