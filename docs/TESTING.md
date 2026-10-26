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
| `tests/test_iface.py` | Default-route parsing from `ip route` and `/proc/net/route`. Speed `-1` is unknown. |
| `tests/test_procscan.py` | Socket inodes and flower endpoints against a fake `/proc`, plus one read of the real `/proc` that must not include this process. Listeners are dropped. IPv4-mapped IPv6 rows come back as IPv4. |
| `tests/test_shaper.py` | The command list a fake host records: cgroup move, nft leaf match, flower filters for sockets that are already open, HTB rates, upload-only skips IFB, nft failure rolls the qdisc and the cgroup back, remove restores `fq_codel`, class byte counters become bit/s, crash recovery reads the state file. |
| `tests/test_app.py` | Headless Textual pilot. Filter, sort, unit toggle, apply, remove, a bad percent, a narrow resize that drops the user column, and the not-root dialog. |
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
