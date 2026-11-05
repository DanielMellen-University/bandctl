"""Render the README screenshots headlessly. No root, no tc, no network.

Every screen uses fake processes and the fake shapers from the unit tests, so
the numbers are made up and repeatable. Output is one SVG per screen in
docs/screenshots. With --png, a headless Chrome or Chromium turns each SVG
into the PNG the docs link to.

    .venv/bin/python tools/screenshots.py --png          # every screen
    .venv/bin/python tools/screenshots.py main rules     # some screens, SVG only
"""
import os

# Colour has to be on before Textual is imported.
for name in ("NO_COLOR", "FORCE_COLOR"):
    os.environ.pop(name, None)
os.environ["COLORTERM"] = "truecolor"
os.environ.setdefault("TERM", "xterm-256color")

import asyncio, datetime, math, random, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from unittest.mock import PropertyMock

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots"
sys.path[:0] = [str(REPO), str(REPO / "tests")]
from test_app import FakeShaper
from test_shaper import _shaper
from bandctl.app import BandctlApp
from bandctl.procscan import ProcessInfo
from bandctl.settings import Settings, write_settings
from bandctl.rules import AutoRule
from textual.widgets import Input, DataTable

PROCS = [
    ProcessInfo(4012, "curl", "curl -O https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso", "daniel", 2, 4012),
    ProcessInfo(1184, "firefox", "/usr/lib/firefox/firefox --new-window", "daniel", 14, 1184),
    ProcessInfo(2210, "steam", "/home/daniel/.steam/ubuntu12_32/steam -silent", "daniel", 9, 2210),
    ProcessInfo(3307, "rsync", "rsync -a --info=progress2 photos/ nas:/backup/photos/", "daniel", 3, 3307),
    ProcessInfo(812, "discord", "/usr/share/discord/Discord", "daniel", 6, 812),
    ProcessInfo(655, "systemd-resolve", "/usr/lib/systemd/systemd-resolved", "systemd-resolve", 4, 655),
    ProcessInfo(901, "sshd", "sshd: daniel@pts/0", "root", 1, 901),
]
BASE = {4012: (29.6e6, 0.6e6), 1184: (8.4e6, 0.9e6), 2210: (19e6, 1.2e6), 3307: (0.7e6, 9.7e6),
        812: (2.1e6, 1.4e6), 655: (20e3, 10e3), 901: (12e3, 30e3)}
TOTALS = {4012: (1_430_000_000, 30_000_000), 1184: (412_000_000, 51_000_000), 2210: (2_900_000_000, 80_000_000),
          3307: (12_000_000, 610_000_000), 812: (95_000_000, 60_000_000), 655: (1_200_000, 800_000),
          901: (300_000, 900_000)}
rnd = random.Random(7)
def rates():
    return {pid: (d * rnd.uniform(0.97, 1.02), u * rnd.uniform(0.95, 1.03)) for pid, (d, u) in BASE.items()}

class Fake(FakeShaper):
    def sample(self):
        r = rates()
        return {pid: r.get(pid, (0.0, 0.0)) for pid in self._shares}

def seed_history(app, pids):
    now = time.monotonic()
    for i in range(60, 0, -1):
        sample = {}
        for pid, (d, u) in BASE.items():
            wd = 1 + 0.25 * math.sin(i / 5 + pid) + 0.1 * math.sin(i * 1.7)
            wu = 1 + 0.3 * math.sin(i / 3 + pid / 7) + 0.1 * math.cos(i * 1.3)
            sample[pid] = (d * max(wd, 0.2), u * max(wu, 0.2))
        app._history.record(now - i, set(pids), sample)

def make_app(*, shaper=None, procs=PROCS, root=True, clock=None, startup=None, link=100_000_000):
    app = BandctlApp(iface="eth0", shaper=shaper or Fake(), processes=lambda: list(procs), link_bps=link,
                     is_root=root, rates=rates, clock=clock, startup_settings=startup)
    return app

async def select(app, pilot, pid):
    table = app.query_one("#procs", DataTable)
    for row in range(table.row_count):
        if str(table.coordinate_to_cell_key((row, 0)).row_key.value) == str(pid):
            table.move_cursor(row=row); await pilot.pause(0.3); return
    raise SystemExit(f"row for {pid} not found")

async def apply(app, pilot, pid, down, up):
    await select(app, pilot, pid)
    app.query_one("#down-pct", Input).value = down
    app.query_one("#up-pct", Input).value = up
    await pilot.press("a"); await pilot.pause(0.4)

async def main_setup(app, pilot):
    await pilot.pause(1.2)
    seed_history(app, [p.pid for p in PROCS])
    type(app._sampler).totals = PropertyMock(return_value=TOTALS)
    await apply(app, pilot, 3307, "", "10")
    await apply(app, pilot, 4012, "30", "")
    await pilot.pause(2.2)
    app.query_one("#procs", DataTable).focus(); await pilot.pause(0.3)

def save(app, name):
    (OUT / f"{name}.svg").write_text(app.export_screenshot(title="bandctl"))
    print("saved", name)

async def scene_main(name, w, h):
    app = make_app()
    async with app.run_test(size=(w, h)) as pilot:
        await main_setup(app, pilot); save(app, name)

async def scene_not_root():
    sys.argv[0] = "/usr/local/bin/bandctl"
    app = make_app(root=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1.5); save(app, "not-root-100x30")

async def scene_rules():
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await main_setup(app, pilot)
        await pilot.press("t"); await pilot.pause(0.3)
        s = app.screen
        for name, down, up, start, end in (("steam", "20", "5", "18:00", "23:00"), ("discord", "", "10", "", "")):
            s.query_one("#rule-name", Input).value = name
            s.query_one("#rule-down", Input).value = down
            s.query_one("#rule-up", Input).value = up
            s.query_one("#rule-start", Input).value = start
            s.query_one("#rule-end", Input).value = end
            await pilot.click("#rule-add"); await pilot.pause(0.3)
        s.query_one("#rule-name", Input).focus(); await pilot.pause(0.3)
        save(app, "rules-80x24")

async def scene_auto_rules():
    app = make_app(clock=lambda: datetime.time(19, 30))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(1.2)
        seed_history(app, [p.pid for p in PROCS])
        type(app._sampler).totals = PropertyMock(return_value=TOTALS)
        await pilot.press("t"); await pilot.pause(0.3)
        s = app.screen
        for name, down, up, start, end in (("steam", "20", "5", "18:00", "23:00"), ("discord", "", "10", "", "")):
            s.query_one("#rule-name", Input).value = name
            s.query_one("#rule-down", Input).value = down
            s.query_one("#rule-up", Input).value = up
            s.query_one("#rule-start", Input).value = start
            s.query_one("#rule-end", Input).value = end
            await pilot.click("#rule-add"); await pilot.pause(0.3)
        await pilot.press("escape"); await pilot.pause(2.5)
        await select(app, pilot, 2210)
        app.query_one("#procs", DataTable).focus(); await pilot.pause(1.2)
        save(app, "auto-rules-120x36")

async def scene_search():
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await main_setup(app, pilot)
        await pilot.press("/"); await pilot.pause(0.2)
        for ch in "resolve":
            await pilot.press(ch)
        await pilot.pause(1.2); save(app, "search-100x30")

def real_shaper():
    shaper, host = _shaper()
    for p in PROCS:
        host.files[f"/proc/{p.pid}/stat"] = f"{p.pid} ({p.comm}) S " + "0 " * 18 + f"{p.start_ticks} 0\n"
        host.files[f"/proc/{p.pid}/cgroup"] = "0::/user.slice/user-1000.slice/session-1.scope\n"
    return shaper

async def scene_groups():
    app = make_app(shaper=real_shaper())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(1.2)
        seed_history(app, [p.pid for p in PROCS])
        type(app._sampler).totals = PropertyMock(return_value=TOTALS)
        await pilot.press("g"); await pilot.pause(0.3)
        s = app.screen
        s.query_one("#group-name", Input).value = "downloads"
        s.query_one("#group-pids", Input).value = "4012, 2210"
        s.query_one("#group-down", Input).value = "40"
        await pilot.click("#group-create"); await pilot.pause(0.4)
        s.query_one("#group-name", Input).value = "backup"
        s.query_one("#group-pids", Input).value = "3307"
        s.query_one("#group-down", Input).value = ""
        s.query_one("#group-up", Input).value = "15"
        await pilot.click("#group-create"); await pilot.pause(0.4)
        s.query_one("#group-table", DataTable).focus(); await pilot.pause(0.4)
        save(app, "groups-80x24")

async def scene_settings():
    app = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await main_setup(app, pilot)
        await pilot.press("x"); await pilot.pause(0.3)
        app.screen.query_one("#settings-path", Input).value = "/home/daniel/bandctl.json"
        app.screen.query_one("#settings-path", Input).focus(); await pilot.pause(0.3)
        save(app, "settings-80x24")

async def scene_startup():
    d = tempfile.mkdtemp(); path = Path(d) / "bandctl.json"
    write_settings(path, Settings("eth0", 80_000_000, 20_000_000, (AutoRule("steam", 20, 5),), ()))
    app = make_app(shaper=real_shaper(), startup=str(path), link=None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(1.2)
        seed_history(app, [p.pid for p in PROCS])
        type(app._sampler).totals = PropertyMock(return_value=TOTALS)
        await pilot.pause(2.2)
        app.query_one("#procs", DataTable).focus(); await pilot.pause(0.3)
        save(app, "startup-config-80x24")

SCENES = {
    "main": lambda: scene_main("main-120x36", 120, 36),
    "compact": lambda: scene_main("compact-80x24", 80, 24),
    "narrow": lambda: scene_main("narrow-77x38", 77, 38),
    "small": lambda: scene_main("small-60x20", 60, 20),
    "notroot": scene_not_root, "rules": scene_rules, "auto": scene_auto_rules,
    "search": scene_search, "groups": scene_groups, "settings": scene_settings, "startup": scene_startup,
}
NAMES = {
    "main": "main-120x36", "compact": "compact-80x24", "narrow": "narrow-77x38", "small": "small-60x20",
    "notroot": "not-root-100x30", "rules": "rules-80x24", "auto": "auto-rules-120x36",
    "search": "search-100x30", "groups": "groups-80x24", "settings": "settings-80x24",
    "startup": "startup-config-80x24",
}


def to_png(svg: Path) -> None:
    browser = next((b for b in ("google-chrome", "chromium", "chromium-browser") if shutil.which(b)), None)
    if browser is None:
        raise SystemExit("--png needs google-chrome or chromium on PATH")
    head = svg.read_text()[:400]
    view = head.split('viewBox="', 1)[1].split('"', 1)[0].split()
    width, height = int(float(view[2])), math.ceil(float(view[3]))
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "shot.html"
        page.write_text(f'<html><body style="margin:0"><img src="{svg.as_uri()}" '
                        f'width="{width}" height="{height}" style="display:block"></body></html>')
        subprocess.run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000", "--virtual-time-budget=4000",
                        f"--window-size={width},{height}", f"--screenshot={svg.with_suffix('.png')}",
                        page.as_uri()], check=True, capture_output=True)
    svg.unlink()


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--png"]
    unknown = [a for a in args if a not in SCENES]
    if unknown:
        raise SystemExit(f"unknown screen: {', '.join(unknown)}. Choose from: {', '.join(SCENES)}")
    for key in args or SCENES:
        asyncio.run(SCENES[key]())
        if "--png" in sys.argv:
            to_png(OUT / f"{NAMES[key]}.svg")


if __name__ == "__main__":
    main()
