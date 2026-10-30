# Testing

## Unit tests

No root, no `tc`, no network namespace.

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

| Module | What it checks |
| --- | --- |
| `tests/test_rates.py` | Percent parsing (rejects 0, 101, and junk). 30% of 100 Mbit/s is 30 Mbit/s and 3.75 MB/s. Over-commit keeps the ceils and shrinks the rates. |
| `tests/test_history.py` | One-minute rate history, missing samples, process removal, and peak grouping for narrow graphs. |
| `tests/test_traffic.py` socket accounting | Individual socket deltas, new sockets, resets, sample gaps, PID totals, retention of idle processes, and non-UTF-8 process names in `ss` output. |
| `tests/test_session_lock.py` | Exclusive lock contention, release, and refusal to follow a symlink, and TUI cleanup before release, using temporary files and mocks. |
| `tests/test_cli_process.py` | A real worker subprocess using fake shaping state and temporary socket and lock paths. |
| `tests/test_cli_integration.py` | CLI limit, remove, export, import, and stop over a temporary socket with a fake host. Wildcard UDP matching is checked when starting and reusing sessions. |
| `tests/test_cli_client.py` | Session discovery, concurrent startup, startup failure, timeout, and waiting for cleanup. |
| `tests/test_cli_worker.py` | Worker lock ownership, failed recovery, fake-host cleanup, and safe handling of stale or live socket paths. |
| `tests/test_cli_transport.py` | Temporary local sockets, request framing, deeply nested requests, private permissions, refresh warnings, and cleanup without host shaping. |
| `tests/test_cli_protocol.py` | Request fields, dispatch, error responses, and explicit session stop. |
| `tests/test_cli_session.py` | Verified CLI session limits and removals, capacity failures, PID reuse, settings imports, changing capacity, scheduled rules, and manual overrides using a fake host. |
| `tests/test_cli.py` | Text and JSON process lists, omission of command arguments, CLI dispatch without opening the TUI, process resolution, and share validation. |
| `tests/test_settings_screen.py` | Export and import through the TUI, explicit startup import, and rejection of an invalid file without changing the session. |
| `tests/test_settings_session.py` | Session export and import against a fake host, including invalid and deeply nested files, missing processes, and shaper failures. |
| `tests/test_settings.py` | Settings round trips, atomic private file writes, bounded reads, and rejection of invalid fields, schedules, budgets, process identities, boot IDs, overlapping groups, session snapshots, import target checks, and group membership change plans. |
| `tests/test_groups.py` | Group name, share, and membership validation, including duplicate names and processes, and the active group view. |
| `tests/test_rules.py` | Automatic rule validation, matching, process identity, manual overrides, and retry timing. |
| `tests/test_schedule.py` | Daily local-time windows, including boundaries and overnight schedules. |
| `tests/test_app.py` filtering | Case-insensitive name, command line, user, and PID searches, no matches, and restoring the list. A search also finds idle processes under the 1% cutoff without pressing `n`, and says when nothing matches. |
| `tests/test_group_screen.py` | Group creation, adding a PID, updating shares, rejecting an unverified PID, and counting a group once for over-commit. |
| `tests/test_app.py` rule editor | Adding and deleting rules, time validation, Enter in a field adds the rule, and only errors use the orange status color. |
| `tests/test_iface.py` | Default-route parsing from `ip route` and `/proc/net/route`. Speed `-1` is unknown. `--iface` names are checked against a fake `/sys/class/net`, bad names are refused, and an unknown name exits with code 2. |
| `tests/test_procscan.py` | Socket inodes and flower endpoints against a fake `/proc`, plus one read of the real `/proc` that must not include this process. Listeners are dropped. IPv4-mapped IPv6 rows come back as IPv4. |
| `tests/test_shaper.py` | First-share capacity validation, stable share snapshots, shared class planning, sampling, group installation, adding a member, group replacement and rollback, group updates, identity checks and rollback after move or sync failures, and the command list a fake host records: cgroup move, nft leaf match, flower filters for sockets that are already open, optional wildcard UDP port matching, HTB rates, upload-only skips IFB, nft failure rolls the qdisc and the cgroup back, remove restores `fq_codel`, class byte counters become bit/s, crash recovery reads the state file. A failed new share removes its leaf cgroup. Teardown leaves a root qdisc it never replaced alone and rejects an apply waiting for its lock. Remove keeps the share when the cgroup write fails, and drops it when the process has exited. |
| `tests/test_traffic.py` | `ss` text becomes per-pid byte totals. Loopback and sockets with no pid are dropped. The second sample is bit/s. Exactly 1% does not count as above the cutoff. |
| `tests/test_app.py` | Headless Textual pilot. The table starts at processes above 1%, `n` shows the rest, a share stays visible after traffic drops, filter, sort, unit toggle, apply, remove, a bad percent, a narrow resize that drops the user column and keeps the table inside the window, a tiny terminal that asks for 60 by 18, the footer key colors, and the not-root dialog. A layout pass at 60x18, 60x20, 64x24, 80x24, 100x30, 107x31, 108x32, 120x40, 160x50, 77x38, 87x44, and 200x60 checks that the title, capacity row, filter, table (header plus five rows or more), percent fields, buttons, meters, log (two lines or more), status line, and footer are all on screen without overlapping, that no button label wraps, and that the table has no horizontal scrollbar. The size classes are checked at each step, and so is the not-root dialog at 60x20. |
| `tests/test_install.py` | `install.sh` and `uninstall.sh` syntax, the sudo re-exec, and an uninstall against a fake prefix. No apt and no qdisc. |

CI (`.github/workflows/ci.yml`) runs the same command on Ubuntu for Python 3.10 and 3.12.

## Manual check, needs root

Do this on a machine where losing the current qdisc for a minute is acceptable. It replaces the root qdisc and redirects ingress through IFB until you quit.

1. Note the current qdisc: `tc qdisc show dev eth0` (use your interface).
2. `sudo bandctl --iface eth0` after `sudo ./install.sh`, or `sudo .venv/bin/python -m bandctl --iface eth0` from a checkout.
3. If the header says the link is unknown, set a download budget of `10` while the unit is Mbit/s. That is a 10 Mbit/s pipe.
4. In another terminal start `curl -o /dev/null https://speed.cloudflare.com/__down?bytes=100000000` (or any multi-megabyte download).
5. Select that `curl`, set download to `10`, leave upload blank, Apply.
6. The download bar should move, and `tc -s class show dev ifb-bandctl` should show a class `1:10` (or the next minor) whose ceil is about 1 Mbit (10% of 10 Mbit) and whose byte counter increases. `tc filter show dev eth0 ingress` should include a flower filter for that curl socket. The socket was open before Apply, and the cgroup match does not see it.
7. Press `q`.
8. `tc qdisc show dev eth0` should be back to the qdisc from step 1. `nft list table inet bandctl` should fail with "No such file or directory". `ip link show ifb-bandctl` should fail the same way. `ls /sys/fs/cgroup/bandctl` should fail.

Upload-only is the same with a sender (`iperf3 -c` or similar) and `tc -s class show dev eth0`. No IFB device should appear.

If you kill the process with `kill -9`, start bandctl once as root and quit. `/run/bandctl.json` from the crashed run is what the second start uses to restore the qdisc and the cgroups.

## Manual check of the command line, needs root

Same warning as above: the root qdisc is replaced while a limit exists. These steps use the same curl download.

1. Start the download from step 4 above. Note the PID with `bandctl list`.
2. `sudo bandctl --iface eth0 limit <pid> --down 10`. It prints `pid <pid>: down 10%, up unlimited` and returns. If the header would say the link is unknown, the command fails and asks for a budget; import a settings file with a capacity budget first.
3. Confirm the same `tc` and `nft` output as step 6 above. The limit stays after the command exits because a background session holds it.
4. `sudo bandctl export settings.json`, then `sudo bandctl remove <pid>`, then `sudo bandctl import settings.json`. Export and import should each print the path.
5. `sudo bandctl stop`. It prints `session stopped` after cleanup. The qdisc, nft table, IFB device, and cgroup should be gone, as in step 8.
6. While a session is running, `sudo bandctl` (the TUI) should exit with "another bandctl session is already running".
