"""Gong System, version 3.

Plays the gong at the times set for today's type of day.

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

VERSION = "3.1"
TICK_MS = 250

log = logging.getLogger("gong")

# Colours. Set explicitly so the app looks the same on every device.
BG = "#F3EFE7"  # page
CARD = "#FFFFFF"
LINE = "#E2DCCF"  # card borders, dividers
INK = "#22252A"  # main text
MUTED = "#70747C"  # secondary text
FAINT = "#A9ACB2"  # gongs already past
ACCENT = "#1F5F5B"  # main button, next gong
ACCENT_DOWN = "#174845"
SOFT = "#E9E4D8"  # secondary button
SOFT_DOWN = "#DAD3C3"
GOOD = "#2E7D4F"
WARN = "#A86400"
WARN_BG = "#FBF1DD"
BAD = "#B3261E"
BAD_BG = "#FBE9E7"

# Sizes at the design size of the window; they scale with the window.
DESIGN_W, DESIGN_H = 1180, 760
FONT_SIZES = {
    "title": (22, "bold"),
    "clock": (44, "bold"),
    "date": (15, "normal"),
    "label": (12, "bold"),
    "day": (30, "bold"),
    "code": (13, "normal"),
    "next": (84, "bold"),
    "countdown": (22, "normal"),
    "gong": (21, "normal"),
    "gong_next": (21, "bold"),
    "button": (20, "bold"),
    "button2": (15, "normal"),
    "alert": (18, "bold"),
    "status": (13, "normal"),
}


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
        self.playing = False
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

                self.playing = True
                pygame.mixer.music.load(str(self.sound_path))
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)
            except Exception as exc:
                self._ready = False  # re-open the sound device next time
                self.problem = f"Sound failed ({exc})"
                log.exception("gong failed")
            finally:
                self.playing = False

    def play(self) -> None:
        threading.Thread(target=self._play_blocking, daemon=True).start()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


class GongWindow:
    def __init__(self, folder):
        import tkinter as tk
        from tkinter import font as tkfont

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
        self._shown: dict = {}  # last values put on screen, to avoid needless redraws
        self._gong_rows: list = []
        self._gong_rows_for: tuple = ()
        self._scale = 0.0

        root = self.root = tk.Tk()
        root.title("Gong System")
        root.configure(bg=BG)
        root.minsize(860, 560)
        root.geometry(f"{DESIGN_W}x{DESIGN_H}")
        if sys.platform == "win32":
            try:
                root.state("zoomed")  # fill the tablet's screen
            except tk.TclError:
                pass

        available = set(tkfont.families(root))
        family = next((f for f in ("Segoe UI", "Helvetica Neue", "Arial") if f in available), "TkDefaultFont")
        self.fonts = {
            name: tkfont.Font(root=root, family=family, size=size, weight=weight)
            for name, (size, weight) in FONT_SIZES.items()
        }
        F = self.fonts

        page = tk.Frame(root, bg=BG)
        page.pack(fill="both", expand=True, padx=28, pady=22)
        page.columnconfigure(0, weight=3, uniform="cols")
        page.columnconfigure(1, weight=2, uniform="cols")
        page.rowconfigure(2, weight=1)

        # Header: name on the left, clock and date on the right.
        header = tk.Frame(page, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(header, text="Gong System", font=F["title"], bg=BG, fg=INK).pack(side="left", anchor="s", pady=(0, 6))
        right = tk.Frame(header, bg=BG)
        right.pack(side="right")
        self.clock_label = tk.Label(right, text="", font=F["clock"], bg=BG, fg=INK)
        self.clock_label.pack(anchor="e")
        self.date_label = tk.Label(right, text="", font=F["date"], bg=BG, fg=MUTED)
        self.date_label.pack(anchor="e")

        # Alert banner, only shown when something needs attention.
        self.alert = tk.Label(page, text="", font=F["alert"], bg=BAD_BG, fg=BAD, padx=18, pady=12, anchor="w", justify="left")
        self.alert.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.alert.bind("<Configure>", lambda e: self.alert.config(wraplength=max(200, e.width - 40)))
        self.alert.grid_remove()

        # Left card: today's type of day and the next gong.
        left_card, left = self._card(page)
        left_card.grid(row=2, column=0, sticky="nsew", pady=18, padx=(0, 9))
        tk.Label(left, text="TODAY", font=F["label"], bg=CARD, fg=MUTED).pack(anchor="w")
        self.day_label = tk.Label(left, text="", font=F["day"], bg=CARD, fg=INK, anchor="w", justify="left")
        self.day_label.pack(anchor="w", fill="x")
        self.day_label.bind("<Configure>", lambda e: self.day_label.config(wraplength=max(200, e.width - 4)))
        self.code_label = tk.Label(left, text="", font=F["code"], bg=CARD, fg=MUTED)
        self.code_label.pack(anchor="w")
        tk.Frame(left, bg=LINE, height=1).pack(fill="x", pady=18)
        tk.Label(left, text="NEXT GONG", font=F["label"], bg=CARD, fg=MUTED).pack(anchor="w")
        self.next_label = tk.Label(left, text="", font=F["next"], bg=CARD, fg=ACCENT)
        self.next_label.pack(anchor="w")
        self.countdown_label = tk.Label(left, text="", font=F["countdown"], bg=CARD, fg=MUTED, anchor="w", justify="left")
        self.countdown_label.pack(anchor="w", fill="x")
        self.countdown_label.bind("<Configure>", lambda e: self.countdown_label.config(wraplength=max(200, e.width - 4)))

        # Right card: all of today's gongs.
        gongs_card, right_card = self._card(page)
        gongs_card.grid(row=2, column=1, sticky="nsew", pady=18, padx=(9, 0))
        tk.Label(right_card, text="TODAY'S GONGS", font=F["label"], bg=CARD, fg=MUTED).pack(anchor="w")
        self.gong_list = tk.Frame(right_card, bg=CARD)
        self.gong_list.pack(fill="both", expand=True, pady=(10, 0))
        self.gong_list.columnconfigure(0, weight=1, uniform="g")
        self.gong_list.columnconfigure(1, weight=1, uniform="g")
        self.gong_note = tk.Label(right_card, text="", font=F["code"], bg=CARD, fg=MUTED, anchor="w", justify="left")
        self.gong_note.pack(anchor="w", fill="x")
        self.gong_note.bind("<Configure>", lambda e: self.gong_note.config(wraplength=max(200, e.width - 4)))

        # Buttons.
        buttons = tk.Frame(page, bg=BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.ring_button = self._button(buttons, "Ring gong now", F["button"], ACCENT, ACCENT_DOWN, "#FFFFFF", self.gong.play, padx=44, pady=18)
        self.ring_button.pack(side="left")
        self.toggle_button = self._button(buttons, "", F["button2"], SOFT, SOFT_DOWN, INK, self.toggle_automations, padx=26, pady=18)
        self.toggle_button.pack(side="right")

        # Status line.
        status = tk.Frame(page, bg=BG)
        status.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.status_dot = tk.Label(status, text="●", font=F["status"], bg=BG, fg=MUTED)
        self.status_dot.pack(side="left")
        self.status_label = tk.Label(status, text="", font=F["status"], bg=BG, fg=MUTED, anchor="w")
        self.status_label.pack(side="left", padx=(6, 0))
        started = time.strftime("%d %b %Y %H:%M", time.localtime())
        tk.Label(status, text=f"Running since {started}   ·   Version {VERSION}", font=F["status"], bg=BG, fg=MUTED).pack(side="right")

        root.bind("<space>", lambda event: self.gong.play())
        root.bind("<Configure>", self._on_resize)
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        log.info("started, version %s, folder %s", VERSION, folder)
        self.tick()

    # -- building blocks ---------------------------------------------------

    def _card(self, parent):
        """A white panel. Returns (the panel to place, the padded area to fill)."""
        tk = self.tk
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=26, pady=22)
        return outer, inner

    def _button(self, parent, text, font, bg, bg_down, fg, command, padx, pady):
        """A flat button that looks the same on every device and works with touch."""
        button = self.tk.Label(parent, text=text, font=font, bg=bg, fg=fg, padx=padx, pady=pady, cursor="hand2")
        button.bind("<Button-1>", lambda e: button.config(bg=bg_down))

        def release(event):
            button.config(bg=bg)
            if 0 <= event.x <= button.winfo_width() and 0 <= event.y <= button.winfo_height():
                command()

        button.bind("<ButtonRelease-1>", release)
        return button

    def _on_resize(self, event) -> None:
        if event.widget is not self.root:
            return
        scale = max(0.7, min(event.width / DESIGN_W, event.height / DESIGN_H, 2.2))
        if abs(scale - self._scale) < 0.03:
            return
        self._scale = scale
        for name, (size, _) in FONT_SIZES.items():
            self.fonts[name].configure(size=max(8, round(size * scale)))

    def _set(self, widget, **options) -> None:
        """Change a widget only when the value is different from what it shows."""
        key = id(widget)
        if self._shown.get(key) != options:
            self._shown[key] = options
            widget.config(**options)

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

        self.draw(now, day_type, times)

    # -- drawing -----------------------------------------------------------

    def draw(self, now, day_type, times) -> None:
        real_type = self.store.day_type(self.today)
        self._set(self.clock_label, text=now.strftime("%H:%M:%S"))
        self._set(self.date_label, text=f"{now:%A} {now.day} {now:%B %Y}")

        # Today
        self._set(self.day_label, text=core.day_name(real_type))
        self._set(self.code_label, text=real_type or "")

        # Next gong
        nxt = core.next_gong(now, times)
        if nxt:
            self._set(self.next_label, text=core.short_time(nxt), fg=ACCENT)
            self._set(self.countdown_label, text=core.countdown(now, nxt))
        else:
            self._set(self.next_label, text="None", fg=FAINT)
            self._set(self.countdown_label, text=self.after_last_gong_text(times))

        # With automatic gongs off, still show the day's times, greyed out.
        listed = self.store.times_for(real_type) if self.deactivated_today else times
        self.draw_gong_list(now, listed, nxt)

        # Buttons
        self._set(self.ring_button, text="Ringing…" if self.gong.playing else "Ring gong now")
        self._set(
            self.toggle_button,
            text="Turn automatic gongs back on" if self.deactivated_today else "Turn off automatic gongs for today",
        )

        # Alert banner
        text, colours = self.alert_text(real_type)
        if text:
            self._set(self.alert, text=text, bg=colours[0], fg=colours[1])
            if not self.alert.winfo_ismapped():
                self.alert.grid()
        elif self.alert.winfo_ismapped():
            self.alert.grid_remove()

        colour, text = self.status_text(now)
        self._set(self.status_dot, fg=colour)
        self._set(self.status_label, text=text)

    def after_last_gong_text(self, times) -> str:
        if self.deactivated_today:
            return "Automatic gongs are off for today"
        tomorrow = self.today + timedelta(days=1)
        t_type = self.store.day_type(tomorrow)
        t_times = self.store.times_for(t_type)
        head = "No more gongs today" if times else "No gongs today"
        if t_type is None:
            return f"{head}. Tomorrow is not in the timetable."
        if not t_times:
            return f"{head}. Tomorrow: {core.day_name(t_type)}, no gongs."
        return f"{head}. Tomorrow: {core.day_name(t_type)}, first gong {core.short_time(t_times[0])}."

    def draw_gong_list(self, now, times, nxt) -> None:
        tk = self.tk
        key = tuple(times)
        if key != self._gong_rows_for:
            for child in self.gong_list.winfo_children():
                child.destroy()
            self._gong_rows = []
            self._gong_rows_for = key
            per_column = (len(times) + 1) // 2 if len(times) > 6 else len(times)
            for i, t in enumerate(times):
                row = tk.Frame(self.gong_list, bg=CARD)
                column, line = (i // per_column, i % per_column) if per_column else (0, 0)
                row.grid(row=line, column=column, sticky="w", pady=3)
                mark = tk.Label(row, text="", font=self.fonts["gong"], bg=CARD, width=2, anchor="w")
                mark.pack(side="left")
                label = tk.Label(row, text=core.short_time(t), font=self.fonts["gong"], bg=CARD, anchor="w")
                label.pack(side="left")
                self._gong_rows.append((t, mark, label))

        current = now.strftime("%H:%M:%S")
        for t, mark, label in self._gong_rows:
            if t == nxt:
                self._set(mark, text="▶", fg=ACCENT)
                self._set(label, fg=ACCENT, font=self.fonts["gong_next"])
            elif self.clock.has_rung(self.today, t):
                self._set(mark, text="✓", fg=GOOD)
                self._set(label, fg=FAINT, font=self.fonts["gong"])
            elif t <= current or self.deactivated_today:
                self._set(mark, text="", fg=FAINT)
                self._set(label, fg=FAINT, font=self.fonts["gong"])
            else:
                self._set(mark, text="", fg=INK)
                self._set(label, fg=INK, font=self.fonts["gong"])

        if self.deactivated_today:
            note = "Automatic gongs are off for today."
        elif not times:
            note = "No gongs are set for today."
        elif any(self.clock.has_rung(self.today, t) for t in times):
            note = "✓ rung by this app today"
        else:
            note = ""
        self._set(self.gong_note, text=note)

    def alert_text(self, real_type):
        """(message, (background, text colour)); message is '' when all is well."""
        if self.gong.problem:
            return f"{self.gong.problem}. Gongs cannot be played.", (BAD_BG, BAD)
        if self.deactivated_today:
            return (
                "Automatic gongs are off for today. They come back on by themselves tomorrow.",
                (WARN_BG, WARN),
            )
        if real_type is None:
            return (
                f"No timetable entry for today ({self.today.day} {self.today:%B %Y}). "
                "No automatic gongs: ring the gong by hand.",
                (BAD_BG, BAD),
            )
        if real_type not in self.store.schedules:
            return (
                f"No gong times are known for the day type '{real_type}'. "
                "No automatic gongs: ring the gong by hand.",
                (BAD_BG, BAD),
            )
        return "", (BAD_BG, BAD)

    def status_text(self, now):
        """(dot colour, text) for the line at the bottom."""
        store = self.store
        until = store.runs_until()
        runs = f"runs to {until.day} {until:%b %Y}" if until else "no timetable saved"
        if not store.healthy_today(self.today):
            detail = store.online_problem or store.local_problem or "today is missing"
            return BAD, f"Timetable problem: {detail}. Trying again every {store.settings['retry_minutes']} minutes."
        if until and (until - self.today).days < 14:
            return WARN, f"Timetable ends soon: {runs}."
        if store.online_problem:
            return WARN, f"Using the saved timetable ({runs}). Last online check failed: {store.online_problem}."
        if store.last_checked:
            when = store.last_checked
            day = "today" if when.date() == now.date() else f"{when.day} {when:%b}"
            return GOOD, f"Timetable up to date, {runs}. Checked online {day} at {when:%H:%M}."
        return MUTED, f"Timetable {runs}. Checking online…"

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
    name = store.settings["sound_file"]
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
