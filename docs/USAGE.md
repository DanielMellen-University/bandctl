# Usage

## Install and start

```
curl -fsSL https://raw.githubusercontent.com/DanielMellen-University/bandctl/main/install.sh | sudo bash
sudo bandctl
```

From a git checkout, `sudo ./install.sh` does the same thing. It installs `python3`, `iproute2`, and `nftables` when they are missing, and links `bandctl` onto `PATH`.

```
sudo bandctl --iface wlan0
```

Host packages the limits need: `iproute2`, `nftables`, and the `ifb` module (`modprobe ifb` needs to work, which it does on a normal desktop kernel).

To remove it, quit any open bandctl (`q` drops the limits), then:

```
sudo bandctl-uninstall
```

Uninstall deletes `/opt/bandctl` and the commands. A crashed run's qdisc, nft table, and cgroup are cleared too. `iproute2` and `nftables` stay.

A checkout can skip the installer:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m bandctl
```

`sudo python -m bandctl` often fails because `sudo` resets `PATH` and picks a Python that does not have Textual. The installed `bandctl` command does not have that problem. A checkout has to name the venv interpreter.

`--iface` is the device whose root qdisc will be replaced while an upload share exists, and whose ingress is redirected while a download share exists. The default is the device on the IPv4 default route. The name has to match a device in `/sys/class/net`. If it does not, bandctl exits before the UI opens and lists the devices it found, for example `no network interface named 'eth9'. Interfaces here: eth0, lo, wlan0.`

## Not root

The window still opens. A dialog shows the `sudo` command. After you dismiss it, the process list works. Apply and Remove are disabled. No cgroup, nft table, or qdisc is created.

## Setting a share

The window needs at least 60 columns and 18 rows. At 108 columns or more the meters sit beside the percent fields. Under 32 rows the inputs and buttons shrink to one row each so the table keeps its space. The README has the full list of size steps.

1. Select a process. The table starts with processes above 1% of the link (or of the traffic measured right now, when the link speed is unknown). Loopback sockets are not counted. `n` shows every process, and a process that already has a share stays listed either way. Arrow keys or a click. `/` filters by command, pid, or user. `s` cycles the sort: name (sockets first), pid, download rate, upload rate.
2. Type a whole number from 1 to 100 in `down %`, `up %`, or both. A blank field means that direction is not capped.
3. Press Enter in either percent field, press `a`, or click Apply.

The status line and the log confirm the share, for example `pid 4242 download 30%, upload 10%`. Limited rows are orange. The selected row stays green. The bars show measured class throughput against the ceiling and turn orange at the cap. `u` or Remove clears the share and puts the process back in its old cgroup. If that write fails while the process is still running, the share stays in place and the log shows the error. If the process has exited, the share is dropped.

A transfer that is already running is included. The kernel does not move an existing socket into the new cgroup, so bandctl also matches the process's open connections by address and port. A connection opened later is marked by the cgroup rule as soon as it exists, and the address list catches up within about a second.

`r` rescans `/proc` immediately. The table also refreshes every second. The selection stays on the same pid.

## Capacity

The header shows the detected link speed from `/sys/class/net/<iface>/speed`. That file is in megabits per second. `-1` or `0` (typical for Wi-Fi and some virtual devices) is shown as `unknown`, and the capacity line says `unset` until you type a budget.

The two budget fields are in the unit shown in the header. `b` flips between Mbit/s and MB/s and rewrites a budget you already typed so the underlying bit rate stays the same. Apply reads those fields, and so does Enter while the cursor is in one of them. Empty means "use the detected speed again".

Example. The link is unknown. You are in Mbit/s and type `100` as the download budget. A 30% download share then ceilings at 30 Mbit/s. Press `b` and the same budget reads `12.5`, which is 100 Mbit/s expressed in MB/s.

## While it is running

Unmarked traffic is not capped. It uses a default HTB class whose ceil is the whole capacity.

If download shares (or upload shares) add up to more than 100%, each process still cannot cross its own percent. The rates HTB guarantees are scaled so they fit in the parent class. The status line reports the total.

A failed apply does not leave a half-moved process, and a new share that fails also removes the cgroup it created. The error text from `tc`, `nft`, or the cgroup write is in the log. If the interface already has an HTB root qdisc, bandctl refuses to replace it.

## Exit

`q`, Ctrl+C, or SIGTERM:

- every process bandctl moved is written back to its original cgroup
- table `inet bandctl` is deleted
- the ingress redirect and `ifb-bandctl` are deleted
- if bandctl replaced the root qdisc, the HTB qdisc is deleted and the previous one (usually `fq_codel`) is added again. A root qdisc bandctl never replaced is not touched
- `/sys/fs/cgroup/bandctl` is removed
- `/run/bandctl.json` is removed

A crash skips that path. The next start reads `/run/bandctl.json` and finishes it. Reboot also drops the qdisc and the file, because neither is persistent.
