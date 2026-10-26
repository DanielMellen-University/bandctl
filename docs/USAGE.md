# Usage

## Install and start

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m bandctl
```

Host packages: `iproute2`, `nftables`, and the `ifb` module (`modprobe ifb` needs to work, which it does on a normal desktop kernel).

`sudo python -m bandctl` often fails because `sudo` resets `PATH` and picks a Python that does not have Textual. Always name the venv interpreter.

```
sudo .venv/bin/python -m bandctl --iface wlan0
```

`--iface` is the device whose root qdisc will be replaced while an upload share exists, and whose ingress is redirected while a download share exists. The default is the device on the IPv4 default route.

## Not root

The window still opens. A dialog shows the `sudo` command. After you dismiss it, the process list works. Apply and Remove are disabled. No cgroup, nft table, or qdisc is created.

## Setting a share

1. Select a process. Arrow keys or a click. `/` filters by command, pid, or user. `n` hides processes with no sockets. `s` cycles the sort: name (sockets first), pid, download rate, upload rate.
2. Type a whole number from 1 to 100 in `down %`, `up %`, or both. A blank field means that direction is not capped.
3. Press Enter in either percent field, press `a`, or click Apply.

The status line and the log confirm the share, for example `pid 4242 download 30%, upload 10%`. Limited rows are orange. The selected row stays green. The bars show measured class throughput against the ceiling and turn orange at the cap. `u` or Remove clears the share and puts the process back in its old cgroup.

`r` rescans `/proc` immediately. The table also refreshes every second. The selection stays on the same pid.

## Capacity

The header shows the detected link speed from `/sys/class/net/<iface>/speed`. That file is in megabits per second. `-1` or `0` (typical for Wi-Fi and some virtual devices) is shown as `unknown`, and the capacity line says `unset` until you type a budget.

The two budget fields are in the unit shown in the header. `b` flips between Mbit/s and MB/s and rewrites a budget you already typed so the underlying bit rate stays the same. Press Enter in a budget field to commit it. Empty means "use the detected speed again".

Example. The link is unknown. You are in Mbit/s and type `100` as the download budget. A 30% download share then ceilings at 30 Mbit/s. Press `b` and the same budget reads `12.5`, which is 100 Mbit/s expressed in MB/s.

## While it is running

Unmarked traffic is not capped. It uses a default HTB class whose ceil is the whole capacity.

If download shares (or upload shares) add up to more than 100%, each process still cannot cross its own percent. The rates HTB guarantees are scaled so they fit in the parent class. The status line reports the total.

A failed apply does not leave a half-moved process. The error text from `tc`, `nft`, or the cgroup write is in the log. If the interface already has an HTB root qdisc, bandctl refuses to replace it.

## Exit

`q`, Ctrl+C, or SIGTERM:

- every process bandctl moved is written back to its original cgroup
- table `inet bandctl` is deleted
- the ingress redirect and `ifb-bandctl` are deleted
- the HTB root qdisc is deleted and the previous root qdisc (usually `fq_codel`) is added again
- `/sys/fs/cgroup/bandctl` is removed
- `/run/bandctl.json` is removed

A crash skips that path. The next start reads `/run/bandctl.json` and finishes it. Reboot also drops the qdisc and the file, because neither is persistent.
