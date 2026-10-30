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

`--iface` is the device whose root qdisc will be replaced while an upload share exists, and whose ingress is redirected while a download share exists. The default is the device on the IPv4 default route. If no default route is found, the TUI shows an error and disables Apply and Remove. Restart with `--iface` to select a device. The name has to match a device in `/sys/class/net`. If it does not, bandctl exits before the UI opens and lists the devices it found, for example `no network interface named 'eth9'. Interfaces here: eth0, lo, wlan0.`

## Command line

`bandctl list` prints running PIDs, users, and process names. `bandctl list --json` returns PID, user, name, and socket count as JSON. Command-line arguments are omitted. The other commands need root:

```
sudo bandctl --iface eth0 limit firefox --down 30 --up 10
sudo bandctl remove firefox
sudo bandctl export settings.json
sudo bandctl import settings.json
sudo bandctl stop
```

A target is a PID or an exact process name, ignoring case. If several processes have the same name, use a PID. Shares are whole percentages from 1 to 100. Leave a direction out to leave it uncapped. The first limit or import starts a background session, which keeps limits and scheduled rules active after the command exits. `stop` removes limits and waits for cleanup. Remove, export, and stop need an existing CLI session. A root TUI and CLI session cannot run at the same time. An explicit `--iface` must match the active session. Add `--json` after a command for machine-readable results and errors. A later limit, remove, import, export, or stop command reports recent background rule, socket filter, or capacity errors.

When link speed is unknown, import a settings file containing capacity budgets before applying a limit. Import validates the complete file and process identities. It selects the saved interface when starting a session. Import and export use paths relative to the command's working directory.

Only one root bandctl session can control shaping at a time. A second root session exits with an error until the first finishes cleanup. The read-only process list does not take the lock.

## Not root

The window still opens. A dialog shows the `sudo` command. After you dismiss it, the process list works. Apply and Remove are disabled. No cgroup, nft table, or qdisc is created.

## Setting a share

The window needs at least 60 columns and 18 rows. At 108 columns or more the meters sit beside the percent fields. Under 32 rows the inputs and buttons shrink to one row each so the table keeps its space. The README has the full list of size steps.

1. Select a process. The table starts with processes above 1% of the link (or of the traffic measured right now, when the link speed is unknown). Loopback sockets are not counted. `n` shows every process, and a process that already has a share stays listed either way. Arrow keys or a click. `/` filters by command, pid, or user, and a search covers every process, not only the ones above 1%. Esc returns to the table. `s` cycles the sort: name (sockets first), pid, download rate, upload rate.
2. Type a whole number from 1 to 100 in `down %`, `up %`, or both. A blank field means that direction is not capped.
3. Press Enter in either percent field, press `a`, or click Apply.

Apply requires a readable process start time. bandctl checks it again before changing the limit. If the process has exited or its PID was reused, rescan and select the intended process.

The status line and the log confirm the share, for example `pid 4242 download 30%, upload 10%`. Limited rows are orange. The selected row stays green. The bars show measured class throughput against the ceiling and turn orange at the cap. `u` or Remove clears the share and puts the process back in its old cgroup. If that write fails while the process is still running, the share stays in place and the log shows the error. If the process has exited, the share is dropped.

A transfer that is already running is included. The kernel does not move an existing socket into the new cgroup, so bandctl also matches the process's open connections by address and port. A connection opened later is marked by the cgroup rule as soon as it exists, and the address list catches up within about a second.

`r` rescans `/proc` immediately. The table also refreshes every second. The selection stays on the same pid.

## Capacity

The header shows the detected link speed from `/sys/class/net/<iface>/speed`. That file is in megabits per second. `-1` or `0` (typical for Wi-Fi and some virtual devices) is shown as `unknown`, and the capacity line says `unset` until you type a budget.

When `ip route` is unavailable, bandctl picks the active IPv4 default route with the lowest metric from `/proc/net/route`.

The two budget fields are in the unit shown in the header. `b` flips between Mbit/s and MB/s and rewrites a budget you already typed so the underlying bit rate stays the same. Apply reads those fields, and so does Enter while the cursor is in one of them. Empty means "use the detected speed again". If updating active limits fails, the previous submitted budgets are retained and the error is shown. If rollback also fails, class rates may not match those budgets. The fields keep your edits so you can retry.

The graphs beside the bars show the selected process's download and upload rates over the last 60 seconds, with the newest sample on the right. Each graph scales to its own peak. When the graph is narrower than 60 characters, each character shows the highest rate in its group of seconds. Blank spaces indicate spans with no samples. History is kept while the process remains in the list and is discarded when it exits or its PID is reused, when the process start time is available.

`TCP total` shows estimated downloaded plus uploaded bytes observed since bandctl started. The selected process always shows it in the detail line; the table includes it when there is room. `--` means no owned TCP socket has been observed. Byte counts do not change when rate units change. The first sample sets a baseline for existing sockets. A socket first seen later contributes its counters from zero, which can include earlier traffic. Sockets that open and close between samples, UDP, loopback, and sockets whose owner is not reported are not counted. A socket shared by several processes is counted for each owner. These totals are estimates, not billing records.

## Shared limits

Press `g` to open shared limits. Enter a name, a comma-separated list of running PIDs, and a download percent, upload percent, or both. Choose Create. The combined traffic of those processes shares one ceiling per direction. Each PID must be verified and have no existing limit. Select a group row to fill its name and percentages. Change the percentages and choose Update to change the shared ceiling. To add a running process with no limit, enter its PID and choose Add. The list of processes is scanned when the editor opens. Close it and rescan if a PID is missing.

To remove one member, close the editor, select that process in the main table, and press `u` or Remove. The other members keep their shared ceiling. The group disappears when its last member is removed. A process must leave its group before it can receive an individual limit. The selected process's detail line names its group. Its bars compare that process's measured rate with the group's ceiling; they do not show the group's combined rate. Groups last until bandctl exits.

## Import and export settings

Press `x`, enter a path, and choose Export to write a JSON settings file. Submit any budget edits first. The file contains the interface, budgets in bits per second, auto-apply rules and schedules, and group definitions. It does not contain process command lines or individual limits. Export replaces the destination with a file readable only by its owner.

Choose Import to load a regular file. bandctl validates all fields before changing settings. Imported rules, budgets, and groups replace the current ones. Individual limits remain; an imported group cannot claim a process with an individual limit. Group members are identified by PID, start time, and boot ID. If a member has exited, its PID was reused, or the machine has rebooted, the import is refused. The interface in the file must match the current interface. To start with the saved interface and explicitly import the file, run `sudo bandctl --config settings.json`. An explicit `--iface` must match the saved interface. A shaper error can prevent import; if rollback also fails, the error reports that it is incomplete. Settings are never loaded automatically.

## Auto-apply rules

Press `t` to open the rule editor. Enter part of a process name and one or both percentages, then press Enter or choose Add. Rules match process names without regard to case. They do not match command arguments. The editor needs root, like Apply. The first active matching rule wins. Select a rule and choose Delete to remove it, or Close to return to the process list. Rules last until bandctl exits.

For a daily schedule, enter both start and end in 24-hour `HH:MM` local time. The start is included and the end is excluded. An end earlier than the start crosses midnight. Leave both fields blank for an always-active rule. A scheduled limit is removed when its window ends or its rule is deleted, and can apply again in the next window. A limit from an always-active rule stays in place if that rule is deleted. A manual apply or remove takes precedence for that process instance.

Rules apply to matching processes already running and to new matches found in later scans. A process that already has a limit is left alone. Removing its limit manually prevents automatic reapplication to that instance. A rule needs a known process start time so bandctl can check that the PID still refers to the scanned process. A failed application appears in the log and is retried after 15 seconds. Rules use the detected link speed or the last submitted budgets. Process scans pause while the editor is open.

A budget must be a finite number that represents at least 1 bit/s.

Example. The link is unknown. You are in Mbit/s and type `100` as the download budget. A 30% download share then ceilings at 30 Mbit/s. Press `b` and the same budget reads `12.5`, which is 100 Mbit/s expressed in MB/s.

## While it is running

Unmarked traffic is not capped. It uses a default HTB class whose ceil is the whole capacity.

If download shares (or upload shares) add up to more than 100%, each process still cannot cross its own percent. The rates HTB guarantees are scaled so they fit in the parent class. The status line reports the total.

A failed share or group update or removal is rolled back where possible. If rollback fails, the error reports that it is incomplete and the active ceiling may differ from the listed share. A failed new share is rolled back, and its empty cgroup is removed. If the process cannot be restored, the error reports an incomplete rollback and the share stays listed so you can try Remove. Its ceiling may not be active. The error text from `tc`, `nft`, or the cgroup write is in the log. bandctl refuses to use an existing `inet bandctl` table or replace an HTB root qdisc. A download share also requires that the interface has no existing ingress qdisc and that `ifb-bandctl` does not already exist.

## Exit

`q`, Ctrl+C, or SIGTERM:

- every process bandctl moved is written back to its original cgroup
- table `inet bandctl` is deleted
- the ingress redirect and `ifb-bandctl` are deleted
- if bandctl replaced the root qdisc, the HTB qdisc is deleted and the previous one (usually `fq_codel`) is added again. A root qdisc bandctl never replaced is not touched
- `/sys/fs/cgroup/bandctl` is removed
- `/run/bandctl.json` is removed

A crash skips that path. The next start reads `/run/bandctl.json` and finishes it. Reboot also drops the qdisc and the file, because neither is persistent.

If restoring a process or the root qdisc, cleaning cgroups, removing the download redirect, or deleting the nftables table fails, bandctl reports the error and keeps the recovery file. The TUI prints cleanup errors after exit and returns a failure status. Remove can retry while the session is open. After exit, the next start retries recovery. If it still fails, the TUI shows the error and disables Apply and Remove, and the CLI session does not start.
