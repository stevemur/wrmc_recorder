#!/usr/bin/env python3
"""
WRMC show recorder for Avery's shows.

Commands:
  wrmc.py update [--quiet]           Read the weekly schedule. For each of
                                     Avery's shows in the next 8 days, set a
                                     recording, a Mac alert 12 hours before,
                                     and an Apple Reminder. Then show a
                                     notice about the next show.
  wrmc.py next                       Show (and notify) Avery's next show.
  wrmc.py list                       Show the shows that are set to record.
  wrmc.py test                       Record 30 seconds now.
  wrmc.py record TITLE START END     Record one show (launchd runs this).
  wrmc.py remind TITLE START END     Alert to keep the Mac on (launchd runs this).
  wrmc.py uninstall                  Remove all WRMC launchd events.

Uses only the Python standard library, curl, and ffmpeg.
"""

import glob
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

# ---- Settings ---------------------------------------------------------------

SCHEDULE_URL = "https://wrmc.middlebury.edu/schedule-weekly"
STREAM_URL = "https://us2.maindigitalstream.com/ssl/WRMC"

# Record every show that has one of these host tags (from /tags/<name>).
HOST_TAGS = ["avery-murch"]
# Also record shows with these exact titles, even if the hosts change.
# Example: SHOW_TITLES = ["TAM", "Trail Mix"]
SHOW_TITLES = []

START_EARLY_MIN = 2      # start recording this many minutes early
END_LATE_MIN = 2         # stop recording this many minutes late
LOOKAHEAD_DAYS = 8
REMIND_HOURS_BEFORE = 12  # Mac alert and Apple Reminder this many hours before
USE_APPLE_REMINDERS = True  # also add a reminder in the Reminders app (syncs to iPhone)

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, "Library", "Application Support", "WRMC")
OUT_DIR = os.path.join(HOME, "Music", "WRMC")
AGENTS_DIR = os.path.join(HOME, "Library", "LaunchAgents")
LOG_FILE = os.path.join(OUT_DIR, "recorder.log")
SCHEDULE_FILE = os.path.join(APP_DIR, "schedule.json")
SHOW_PREFIX = "com.murch.wrmc.show."
REMIND_PREFIX = "com.murch.wrmc.remind."
UID = os.getuid()

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")


# ---- Helpers ----------------------------------------------------------------

def log(msg):
    os.makedirs(OUT_DIR, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(msg):
    subprocess.run(
        ["osascript", "-e", f'display notification "{msg}" with title "WRMC Recorder"'],
        capture_output=True,
    )


def parse_dt(text):
    # Example: 2026-10-31T09:00:00-0400
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")


def safe_name(title):
    return re.sub(r"[^A-Za-z0-9]+", "", title) or "Show"


# ---- Schedule parsing -------------------------------------------------------

class ScheduleParser(HTMLParser):
    """Collects each <article class="show-teaser"> with its times, title, hosts."""

    def __init__(self):
        super().__init__()
        self.shows = []
        self.cur = None
        self.depth = 0
        self.in_h3 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "article":
            if self.cur is not None:
                self.depth += 1
            elif "show-teaser" in (a.get("class") or "").split():
                self.cur = {"title": "", "times": [], "hosts": []}
                self.depth = 0
            return
        if self.cur is None:
            return
        if tag == "time" and a.get("datetime"):
            self.cur["times"].append(a["datetime"])
        elif tag == "h3":
            self.in_h3 = True
        elif tag == "a":
            m = re.match(r"^/tags/([^/?#]+)", a.get("href") or "")
            if m:
                self.cur["hosts"].append(m.group(1))

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_h3 = False
        if tag == "article" and self.cur is not None:
            if self.depth == 0:
                self.shows.append(self.cur)
                self.cur = None
            else:
                self.depth -= 1

    def handle_data(self, data):
        if self.cur is not None and self.in_h3:
            self.cur["title"] += data


def fetch_schedule_html():
    r = subprocess.run(
        ["curl", "-fsSL", "--max-time", "60", "-A", "Mozilla/5.0", SCHEDULE_URL],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Could not download schedule (curl exit {r.returncode}): {r.stderr.strip()}")
    return r.stdout


def find_shows(html):
    p = ScheduleParser()
    p.feed(html)
    found = {}
    for s in p.shows:
        title = " ".join(s["title"].split())
        if len(s["times"]) < 2 or not title:
            continue
        wanted = any(h in HOST_TAGS for h in s["hosts"]) or title in SHOW_TITLES
        if not wanted:
            continue
        start, end = parse_dt(s["times"][0]), parse_dt(s["times"][1])
        if end <= start:
            end += timedelta(days=1)
        found[(title, start)] = {"title": title, "start": start, "end": end}
    return sorted(found.values(), key=lambda x: x["start"])


# ---- launchd ----------------------------------------------------------------

def label_for(show):
    return SHOW_PREFIX + show["start"].strftime("%Y%m%d%H%M") + "." + safe_name(show["title"]).lower()


def plist_path(label):
    return os.path.join(AGENTS_DIR, label + ".plist")


def is_loaded(label):
    return subprocess.run(["launchctl", "print", f"gui/{UID}/{label}"],
                          capture_output=True).returncode == 0


def load(path):
    subprocess.run(["launchctl", "bootstrap", f"gui/{UID}", path], capture_output=True)


def unload(label):
    subprocess.run(["launchctl", "bootout", f"gui/{UID}/{label}"], capture_output=True)


def show_plist(show, label):
    fire = (show["start"] - timedelta(minutes=START_EARLY_MIN)).astimezone()  # Mac local time
    return {
        "Label": label,
        "ProgramArguments": [
            sys.executable, os.path.abspath(__file__), "record",
            show["title"], show["start"].isoformat(), show["end"].isoformat(),
        ],
        "StartCalendarInterval": {
            "Month": fire.month, "Day": fire.day, "Hour": fire.hour, "Minute": fire.minute,
        },
        # Also runs at login or wake-up. The record step checks the time, so it
        # only records if the show is on (or about to start).
        "RunAtLoad": True,
        "StandardOutPath": LOG_FILE,
        "StandardErrorPath": LOG_FILE,
    }


# ---- Commands ---------------------------------------------------------------

def remind_label_for(show):
    return REMIND_PREFIX + show["start"].strftime("%Y%m%d%H%M") + "." + safe_name(show["title"]).lower()


def remind_plist(show, label):
    fire = (show["start"] - timedelta(hours=REMIND_HOURS_BEFORE)).astimezone()
    return {
        "Label": label,
        "ProgramArguments": [
            sys.executable, os.path.abspath(__file__), "remind",
            show["title"], show["start"].isoformat(), show["end"].isoformat(),
        ],
        # If the Mac is asleep at this time, launchd runs it when the Mac wakes.
        "StartCalendarInterval": {
            "Month": fire.month, "Day": fire.day, "Hour": fire.hour, "Minute": fire.minute,
        },
        "StandardOutPath": LOG_FILE,
        "StandardErrorPath": LOG_FILE,
    }


def ensure_agent(label, plist_dict):
    """Write and load a launchd agent. Do nothing if it is unchanged."""
    path = plist_path(label)
    data = plistlib.dumps(plist_dict)
    old = open(path, "rb").read() if os.path.exists(path) else None
    if old == data and is_loaded(label):
        return False  # unchanged; do not disturb (it may be running now)
    if old is not None:
        unload(label)
    with open(path, "wb") as f:
        f.write(data)
    load(path)
    return True


def remove_stale_agents(prefix, wanted, now, keep_until_end=False):
    """Remove agents for shows that ended or left the schedule.
    Never stop one whose show is on air now. With keep_until_end, keep it
    until the show ends (a reminder may be keeping the Mac awake)."""
    for path in glob.glob(os.path.join(AGENTS_DIR, prefix + "*.plist")):
        label = os.path.basename(path)[:-len(".plist")]
        if label in wanted:
            continue
        try:
            args = plistlib.load(open(path, "rb"))["ProgramArguments"]
            start = datetime.fromisoformat(args[-2])
            end = datetime.fromisoformat(args[-1])
        except Exception:
            start = end = now
        protect_from = now if keep_until_end else start - timedelta(minutes=10)
        if protect_from <= now <= end + timedelta(minutes=END_LATE_MIN):
            continue
        unload(label)
        os.remove(path)
        log(f"Removed old event: {label}")


def as_applescript_date(var, dt):
    """AppleScript lines that set variable `var` to local datetime `dt`."""
    dt = dt.astimezone()
    return (f"set {var} to current date\n"
            f"set day of {var} to 1\n"
            f"set year of {var} to {dt.year}\n"
            f"set month of {var} to {dt.month}\n"
            f"set day of {var} to {dt.day}\n"
            f"set time of {var} to {dt.hour * 3600 + dt.minute * 60}\n")


def reminder_name(show):
    return f"WRMC: keep Mac on for {show['title']} ({show['start'].astimezone():%a %d %b %H:%M})"


def sync_apple_reminders(upcoming, now, quiet=False):
    """Make one Apple Reminders item per show, and remove WRMC items that
    are no longer on the schedule. Reminders sync to iPhone through iCloud.
    Skips the (slow) Reminders app when nothing has changed."""
    if not USE_APPLE_REMINDERS:
        return
    def q(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    items = []
    for s in upcoming:
        when = max(s["start"] - timedelta(hours=REMIND_HOURS_BEFORE), now + timedelta(minutes=1))
        body = (f"Avery's show {s['title']} starts at {s['start'].astimezone():%H:%M} "
                f"on {s['start'].astimezone():%A %d %B}. Keep the Mac plugged in, "
                f"open, and awake so it can record the show.")
        items.append((reminder_name(s), body, when))

    state_file = os.path.join(APP_DIR, "reminders_done.json")
    signature = sorted(n for n, _, _ in items)
    try:
        if json.load(open(state_file)) == signature:
            return  # already up to date
    except Exception:
        pass

    if not quiet:
        print("Updating Apple Reminders (this can take up to a minute)...", flush=True)
    # Read the names of open reminders ONE time. Searching each reminder
    # with "whose" is very slow when a list has many old items.
    lines = ['tell application "Reminders"',
             "set L to default list",
             "set openNames to name of (reminders of L whose completed is false)"]
    for name, body, when in items:
        lines.append(as_applescript_date("d", when))
        lines.append(f'if openNames does not contain "{q(name)}" then '
                     f'make new reminder at end of L with properties '
                     f'{{name:"{q(name)}", body:"{q(body)}", remind me date:d}}')
    keep = "{" + ", ".join(f'"{q(n)}"' for n, _, _ in items) + "}"
    lines += [f"set keepNames to {keep}",
              "repeat with n in openNames",
              "set n to n as text",
              'if n starts with "WRMC: " and keepNames does not contain n then',
              "delete (reminders of L whose name is n and completed is false)",
              "end if",
              "end repeat",
              "end tell"]
    try:
        r = subprocess.run(["osascript", "-"], input="\n".join(lines),
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        log("Apple Reminders did not answer in 90 seconds. It will try again at "
            "the next update. (The Mac alert 12 hours before the show still works.)")
        return
    if r.returncode != 0:
        log(f"Could not update Apple Reminders: {r.stderr.strip()} "
            f"(The Mac alert 12 hours before the show still works.)")
        return
    with open(state_file, "w") as f:
        json.dump(signature, f)


def describe(show):
    local = show["start"].astimezone()
    days = (local.date() - datetime.now().astimezone().date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"{local:%A %d %B}"
    return f"{show['title']}, {when} at {local:%H:%M}"


def cmd_update(quiet=False):
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(AGENTS_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)

    # Only one update at a time.
    import fcntl
    lock = open(os.path.join(APP_DIR, "update.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if not quiet:
            print("Another update is running now. Please wait a minute and try again.")
        return 0

    if not quiet:
        print("Reading the WRMC schedule...", flush=True)

    try:
        shows = find_shows(fetch_schedule_html())
    except Exception as e:
        log(f"UPDATE FAILED: {e}. Existing events are kept.")
        notify("Could not read the WRMC schedule today. Existing recordings are kept.")
        return 1

    if not shows:
        log("WARNING: No matching shows found on the schedule. Existing events are kept.")
        notify("No shows by Avery found on this week's WRMC schedule.")
        return 0

    horizon = now + timedelta(days=LOOKAHEAD_DAYS)
    upcoming = [s for s in shows if s["end"] > now and s["start"] < horizon]

    # 1. Recording events.
    wanted = {}
    for s in upcoming:
        label = label_for(s)
        wanted[label] = s
        if ensure_agent(label, show_plist(s, label)):
            log(f"Recording set: {s['title']}  {s['start']:%a %d %b %H:%M} New York "
                f"= {s['start'].astimezone():%a %d %b %H:%M} on this Mac")
    remove_stale_agents(SHOW_PREFIX, wanted, now)

    # 2. Mac alerts 12 hours before (only if that time is still ahead).
    wanted_r = {}
    for s in upcoming:
        if s["start"] - timedelta(hours=REMIND_HOURS_BEFORE) <= now:
            continue
        label = remind_label_for(s)
        wanted_r[label] = s
        if ensure_agent(label, remind_plist(s, label)):
            log(f"Mac alert set: {REMIND_HOURS_BEFORE} hours before {s['title']}")
    remove_stale_agents(REMIND_PREFIX, wanted_r, now, keep_until_end=True)

    # 3. Apple Reminders (also reach the iPhone).
    sync_apple_reminders(upcoming, now, quiet)

    with open(SCHEDULE_FILE, "w") as f:
        json.dump([{"title": s["title"], "start": s["start"].isoformat(),
                    "end": s["end"].isoformat()} for s in upcoming], f, indent=2)
    log(f"Update OK. {len(upcoming)} show(s) set to record.")

    # 4. Tell the person about the next show.
    if upcoming and not quiet:
        msg = f"Next show: {describe(upcoming[0])}. The recording is set."
        print("\n" + msg)
        notify(msg)
    return 0


def cmd_remind(title, start_s, end_s):
    """Alert 12 hours before a show. Can keep the Mac awake until the show ends."""
    start, end = datetime.fromisoformat(start_s), datetime.fromisoformat(end_s)
    now = datetime.now(timezone.utc)
    if now >= end:
        return 0
    # Skip if the show was taken off the schedule since this alert was set.
    try:
        current = json.load(open(SCHEDULE_FILE))
        if not any(c["title"] == title and datetime.fromisoformat(c["start"]) == start
                   for c in current):
            return 0
    except Exception:
        pass
    show = {"title": title, "start": start, "end": end}
    msg = (f"Avery's show {describe(show)}. "
           f"Keep this Mac plugged in, with the lid open, so it can record the show.")
    script = (f'display alert "WRMC: {title}" message "{msg}" '
              f'buttons {{"OK", "Keep Mac awake until the show ends"}} '
              f'default button 2 giving up after 3600')
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    # Keep the Mac awake unless the person pressed OK (also if nobody answered).
    if "button returned:OK" not in r.stdout:
        seconds = int((end + timedelta(minutes=END_LATE_MIN) - now).total_seconds())
        log(f"Keeping the Mac awake for {seconds // 3600} h {seconds % 3600 // 60} min, "
            f"until {title} ends")
        subprocess.run(["caffeinate", "-i", "-t", str(seconds)])
    return 0


def cmd_next():
    try:
        current = json.load(open(SCHEDULE_FILE))
    except Exception:
        current = []
    now = datetime.now(timezone.utc)
    shows = [{"title": c["title"], "start": datetime.fromisoformat(c["start"]),
              "end": datetime.fromisoformat(c["end"])} for c in current]
    shows = [s for s in shows if s["end"] > now]
    if not shows:
        print("No upcoming shows are known. Run: wrmc update")
        return 0
    msg = f"Next show: {describe(shows[0])}."
    print(msg)
    notify(msg)
    return 0


def cmd_list():
    files = sorted(glob.glob(os.path.join(AGENTS_DIR, SHOW_PREFIX + "*.plist")))
    if not files:
        print("No shows are set to record. Run: wrmc.py update")
        return 0
    for path in files:
        args = plistlib.load(open(path, "rb"))["ProgramArguments"]
        title, start, end = args[-3], datetime.fromisoformat(args[-2]), datetime.fromisoformat(args[-1])
        print(f"{title:<20} {start:%a %d %b %H:%M} New York  |  "
              f"{start.astimezone():%a %d %b %H:%M} here  ({int((end - start).seconds / 60)} min)")
    return 0


def record(title, seconds, date_tag):
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{safe_name(title)}_{date_tag}.m4a")
    if os.path.exists(out):
        out = out[:-4] + f"_{datetime.now():%H%M%S}.m4a"
    log(f"Recording {title} for {seconds // 60} min {seconds % 60} s -> {out}")
    r = subprocess.run([
        "caffeinate", "-i", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "30",
        "-i", STREAM_URL, "-t", str(seconds),
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-metadata", f"title={title} {date_tag}",
        "-metadata", "artist=WRMC 91.1 FM",
        "-metadata", f"album={title}",
        out,
    ])
    log(f"Finished {title} (ffmpeg exit code {r.returncode})")
    if r.returncode == 0:
        notify(f"Saved {os.path.basename(out)}")
    return r.returncode


def cmd_record(title, start_s, end_s):
    start, end = datetime.fromisoformat(start_s), datetime.fromisoformat(end_s)
    now = datetime.now(timezone.utc)
    if now < start - timedelta(minutes=10):
        return 0  # launched at login long before the show; nothing to do
    stop = end + timedelta(minutes=END_LATE_MIN)
    seconds = int((stop - now).total_seconds())
    if seconds < 60:
        return 0  # show is over
    # If a recording of this show is already running, do not start a second one.
    import fcntl
    os.makedirs(APP_DIR, exist_ok=True)
    lock = open(os.path.join(APP_DIR, f"lock_{safe_name(title)}_{start:%Y%m%d%H%M}"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    return record(title, seconds, start.strftime("%Y-%m-%d"))


def cmd_uninstall():
    for path in glob.glob(os.path.join(AGENTS_DIR, "com.murch.wrmc*.plist")):
        unload(os.path.basename(path)[:-len(".plist")])
        os.remove(path)
    print("All WRMC events removed. Recordings in ~/Music/WRMC are not changed.")
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "update":
        return cmd_update(quiet="--quiet" in argv)
    if cmd == "next":
        return cmd_next()
    if cmd == "remind" and len(argv) == 4:
        return cmd_remind(*argv[1:])
    if cmd == "list":
        return cmd_list()
    if cmd == "test":
        return record("Test", 30, datetime.now().strftime("%Y-%m-%d"))
    if cmd == "record" and len(argv) == 4:
        return cmd_record(*argv[1:])
    if cmd == "uninstall":
        return cmd_uninstall()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
