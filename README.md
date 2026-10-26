# bandctl

Terminal UI for per-process bandwidth shares on Linux.

Pick a running process and give it a download percent and an upload percent. Each percent is a ceiling on that direction's link capacity: 30% of a 100 Mbit/s link is 30 Mbit/s, and the process cannot go past it while bandctl is running. Under the UI this is a cgroup, nftables marks, and an HTB qdisc. Quit, and those rules are removed.

> Status: **0.1.0**. Separate download and upload shares, a bits/bytes display toggle, sort by measured usage, and a scrolling log of tc, nft, and cgroup errors. Shares are not saved across runs.

## Layout

```
bandctl/
  __main__.py    python -m bandctl
  app.py         Textual screens, keys, and the 1 second refresh
  widgets.py     the not-root dialog
  theme.tcss     dark grey, pastel green, orange
  procscan.py    /proc process list and socket counts
  traffic.py     per-process TCP rates from ss
  rates.py       percents, units, HTB split
  iface.py       default route and link speed
  shaper.py      cgroup, nft, tc
install.sh       system install
uninstall.sh     remove that install
tests/           unittest, no root required
docs/            usage, architecture, testing
```

## Install

One command. It installs `python3`, `iproute2`, and `nftables` if they are missing, puts bandctl in `/opt/bandctl`, and links `bandctl` onto `PATH`.

```
curl -fsSL https://raw.githubusercontent.com/DanielMellen-University/bandctl/main/install.sh | sudo bash
```

From a git checkout:

```
sudo ./install.sh
```

Then:

```
sudo bandctl
sudo bandctl --iface wlan0
```

bandctl has to be root to move processes between cgroups and to change qdiscs. The installer is what makes `sudo bandctl` find Textual. `sudo python -m bandctl` often does not.

Without root the UI still opens. It lists processes and tells you the exact command to re-run. Apply and remove stay disabled, and nothing is installed on the interface.

## Uninstall

Quit bandctl first if it is open. `q` removes the limits. Then:

```
sudo bandctl-uninstall
```

That deletes `/opt/bandctl` and the `bandctl` command. If the last run crashed, it also clears the qdisc, nft table, and cgroup that run left behind. It does not remove `iproute2` or `nftables`.

## Run from a checkout

Skip the installer when you are working on the code. Debian and Ubuntu need the `python3-venv` package before `venv` will work.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m bandctl
```

Name the venv interpreter. `sudo python` is often a different install and will not see Textual.

`--iface` overrides the device on the IPv4 default route. There is no interface switch inside the UI. Quit and start again with the flag.

## Usage

The window needs 64 columns and 24 rows. Below that, bandctl asks you to make the terminal bigger. Wider windows put the meters next to the percent fields and add the user and socket columns. The process table fills the width instead of scrolling sideways.

The table lists processes using more than 1% of the link. When the link speed is unknown, that 1% is of the TCP traffic measured right now, ignoring loopback. `n` lists every process. A process that already has a share stays on screen so you can remove it. Type `/` to filter by name, pid, or user. Select a row with the arrow keys or a click.

Type a download percent, an upload percent, or both (1 to 100). Leave a field blank to leave that direction uncapped. Enter in a percent field, the Apply button, or `a` installs the share. `u` removes it. Limited rows are orange, except the selected row, which stays green so the text stays readable. The bar is green while usage is under the ceiling and orange when it is at the cap.

The header shows the interface, whether this process is root, and the active unit (`Mbit/s` or `MB/s`). `b` toggles the unit. Percents stay percents. Budgets and the usage numbers follow the toggle.

If sysfs has no positive link speed (common on Wi-Fi), the capacity line says unset. Type a budget in the down and up fields. Apply uses that number, and so does Enter in the budget field. The number is in the unit currently shown: `100` in Mbit/s is 100 Mbit/s, and the same digits in MB/s are 100 megabytes per second. An empty budget means "use the detected speed".

Keys:

| Key | Action |
| --- | --- |
| Up/Down, click | Select a process |
| `/` | Filter the table |
| Enter, `a`, Apply | Apply both percent fields |
| `u`, Remove | Remove the share |
| `r` | Rescan processes |
| `n` | Show every process, or go back to processes above 1% |
| `s` | Cycle sort: name, pid, download, upload |
| `b` | Toggle Mbit/s and MB/s |
| `q` | Quit and remove every rule this process installed |

The log under the bars keeps the last successes and failures. A failed `tc`, `nft`, or cgroup write is one line there and in the status bar. The previous share is left in place when an update fails, and a brand new share is rolled back.

Full steps and the manual check with a real download are in [docs/USAGE.md](docs/USAGE.md) and [docs/TESTING.md](docs/TESTING.md).

## What a share means

The ceiling is a percent of capacity, not of whatever the link happens to be doing this second. Capacity is the detected link speed, unless you typed a budget. Several shares may add up to more than 100%. Each process still cannot pass its own percent. The guaranteed HTB rates are scaled down so they fit, and the status line says so.

Rules exist only while bandctl is running. Exit restores each process to the cgroup it had before, deletes the nftables table `inet bandctl`, deletes `ifb-bandctl`, and puts the previous root qdisc back. A crash writes `/run/bandctl.json`. The next start uses that file to finish the cleanup.

While a share is active, bandctl replaces the root qdisc on the chosen interface with HTB. Unmarked traffic can still use the full link. Do not point bandctl at an interface that already has an `htb` qdisc. It will refuse.

Connections that are already open are shaped too. Moving a process does not move sockets it created earlier, so those are matched by address and port. The list is refreshed about once a second.

## Docs

- [Usage](docs/USAGE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Testing](docs/TESTING.md)
- [Changelog](CHANGELOG.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: run the unit tests before pushing, and change the docs in the same commit as the behaviour they describe.

## License

MIT. See [LICENSE](LICENSE).
