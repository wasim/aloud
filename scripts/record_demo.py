"""Record a real `aloud` session into an asciinema v2 .cast file.

Drives the session through a PTY (synthesis + key presses), captures timed
output, and clamps long idle gaps (e.g. model load) so the cast stays snappy.
"""
import os, pty, select, subprocess, time, json, fcntl, termios, struct, sys

COLS, ROWS = 88, 20
SAMPLE = (
    "aloud turns any article into a private, narrated read along. "
    "Watch each word light up as it is spoken.\n\n"
    "It runs completely offline on your Mac, with no cloud and no "
    "subscriptions. Press space to pause, or q to quit."
)

master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
proc = subprocess.Popen(
    [os.path.expanduser("~/aloud/.venv/bin/python"),
     os.path.expanduser("~/aloud/aloud.py"), "--text", SAMPLE],
    stdin=slave, stdout=slave, stderr=slave,
    close_fds=True, start_new_session=True,
    env={**os.environ, "TERM": "xterm-256color"},
)
os.close(slave)

events = []          # (abs_time, bytes)
t0 = time.time()
alive = True

def collect(duration):
    end = time.time() + duration
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if data:
                events.append((time.time() - t0, data))

def buf():
    return b"".join(d for _, d in events)

def send(b):
    os.write(master, b)

# wait for playback to begin
while time.time() - t0 < 60 and b"Playing" not in buf():
    collect(0.1)
collect(7.0)          # listen through paragraph one
send(b" "); collect(1.5)      # pause
send(b" "); collect(6.0)      # resume into paragraph two
send(b"q"); collect(1.0)      # quit

alive = False
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill()

# --- write asciinema v2 cast, clamping idle gaps to keep it tight ----------- #
MAX_GAP = 1.2
out_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/demo.cast"
with open(out_path, "w") as f:
    f.write(json.dumps({"version": 2, "width": COLS, "height": ROWS,
                        "env": {"TERM": "xterm-256color"}}) + "\n")
    clock, prev = 0.0, 0.0
    for t, data in events:
        gap = min(t - prev, MAX_GAP)
        clock += gap
        prev = t
        f.write(json.dumps([round(clock, 3), "o",
                            data.decode("utf-8", "replace")]) + "\n")
print(f"wrote {out_path}  ({len(events)} events, ~{clock:.1f}s)")
