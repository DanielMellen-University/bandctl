# Architecture

bandctl is a Textual app in front of one `Shaper` object. The app never shells out itself. `shaper.py` is the only place that runs `tc`, `nft`, `ip`, and `modprobe`, or that writes a cgroup file.

## Command line

`cli.py` formats a process scan as text or JSON without process arguments. `bandctl list` does not start the TUI or the shaper. The CLI also validates share inputs and resolves a PID or exact process name, refusing ambiguous names and unknown start times. `cli_session.py` uses the shaper for verified limit and remove requests and commits imported settings after validation. Its refresh updates detected link capacity, retries failed capacity updates, and reconciles the same automatic-rule and schedule trackers used by the TUI. `cli_protocol.py` validates and dispatches structured requests to that session. `cli_transport.py` serves bounded requests over a private local socket and removes only the socket it created. `cli_worker.py` holds the exclusive session lock, recovers stale state, runs the socket server, and tears down shaping on exit. `cli_client.py` finds the running session, or starts a worker for a limit or import, and waits for cleanup after `stop`. The worker is a separate process, so limits stay active after the command that created them exits. `session_lock.py` provides a nonblocking exclusive lock for ownership of host shaping state; the root TUI holds it until cleanup completes. The CLI worker holds the same lock while it owns shaping state.

## Settings interchange

`settings.py` parses and validates a versioned JSON settings format before returning it. It includes the interface, budgets in bits per second, automatic rules with optional daily windows, and group members with PID start times and the boot ID, so an importer can reject identities from another boot. It rejects duplicate or unknown fields and overlapping groups. File reads are bounded and validated before returning. Exports replace the destination atomically with a private file. A session snapshot omits process arguments. The shaper returns locked copies of shares so a snapshot does not change during an update. A separate preflight checks the interface, boot ID, and every group member identity without changing the shaper. A group change plan identifies members to keep, add, or remove and rejects conflicts with individual limits. The shaper can replace group membership under its lock after verifying the boot, interface, process identities, and capacity. It restores the prior shares after an error where possible and reports an incomplete rollback. `settings_session.py` exports a snapshot and validates an import before asking the shaper to replace groups. It returns imported rules and budgets only after that succeeds. The settings screen uses this service and commits rules and budgets only after the shaper succeeds. The file interface must match the active shaper interface. With an explicit `--config` at startup, the saved interface is validated and selected before the app opens. Process arguments are not included.

## Shares

`groups.py` validates named group definitions and builds a sorted group view from installed shares, rejecting conflicting member state. Group names are unique without regard to case, and a PID can occur only once across all groups. The shaper plans one HTB class per class ID, even when several process leaves share it. Members with conflicting limits for one class ID are rejected. The shaper can install a group of unlimited process instances after checking their start times. A failed installation, including a failed process move, restores members where possible and keeps any process that could not be restored tracked. The shaper can add a verified, unlimited process to an existing group, and retains tracking if a failed addition cannot be restored. The shaper can update every member's ceiling together, restoring the old shares if synchronization fails. The group editor creates groups, updates ceilings, and adds members. Removing a member uses the existing per-process Remove action.

`rules.py` validates automatic-rule values and matches a case-insensitive substring of the process name. When several rules match, the first rule takes precedence. Its tracker identifies instances by PID and start time, skips processes without a known start time, preserves manual removals for a seen instance, and delays retries after a failure.

`schedule.py` defines daily local-clock windows with inclusive starts and exclusive ends. A window can cross midnight; equal endpoints are rejected. Rule matching skips scheduled rules outside their window and picks the first active match.

`ScheduledOwnership` tracks limits installed by automatic rules. It identifies limits to remove when their rule stops winning, and drops ownership if the process instance or share revision changes. Deleting an always-active rule releases ownership without removing its limit. Failed removals are retried after a delay.

Each successful apply gives its share a new in-memory revision. A caller can require that revision when removing a share, so a concurrent change does not get removed using an old decision.

Automatic applies also require that the PID still has no share while the shaper lock is held. A concurrent manual limit is left alone.

The app reconciles scheduled ownership during refresh using the current local clock. It removes an expired share only if its revision still matches, then allows the next active matching rule to apply. A successful manual apply or removal releases scheduled ownership for that process instance.

The TUI, CLI, and automatic rules pass the process start time from the scan to `Shaper.apply`. The shaper checks it again before changing anything and rejects a missing or changed process. It also rejects an update if an existing verified share belongs to an older process instance. This check reduces the chance of acting on a reused PID; the process may still exit after the check.

Shares created with a verified start time are retired during sampling when that process exits or its PID is reused. The replacement PID is not moved to the old process's cgroup. If the start time cannot be read, the share is kept for a later check. A missing stat file counts as an exit only when the cgroup file is also missing. A new share is refused if its old leaf cgroup still has processes in it.

A share is two optional integers, download and upload, each from 1 to 100. Blank means that direction is not enforced.

Capacity for a direction is the budget the user typed, or else the sysfs link speed, in bit/s. A requested direction without a capacity is rejected before the process moves, including for the first share. `rates.percent_to_bps` is integer arithmetic: 30% of 100_000_000 is 30_000_000. Display divides by 1_000_000 for Mbit/s and by 8_000_000 for MB/s.

`rates.htb_plan` turns those ceilings into kbit numbers for HTB:

- each ceil is the requested share, capped at the parent
- 1 kbit is reserved for the default class (unmarked traffic)
- if the ceils fit in what is left, each class `rate` equals its `ceil`
- if they do not, the rates shrink in proportion and the ceils stay

The default class `rate` is the leftover. Its `ceil` is the whole parent, so unmarked traffic can use the link when the limited classes are idle. A limited class cannot borrow past its ceil.

## Who owns the packets

bandctl creates `inet bandctl` exclusively before filling its rules. It refuses an existing table. Recovery state records table ownership, so an idle session does not delete someone else's table. Old state files lack that field and retain the earlier cleanup behavior.

The process is moved to `/sys/fs/cgroup/bandctl/bandctl-<pid>`. nft's `socket cgroupv2 level N` matches one path component, not the whole path. Level 2 is the leaf name `bandctl-<pid>`, which is unique under `bandctl/`.

```
add rule inet bandctl output socket cgroupv2 level 2 "bandctl-<pid>" \
    ct mark set <minor> meta mark set <minor>
add rule inet bandctl prerouting socket cgroupv2 level 2 "bandctl-<pid>" \
    ct mark set <minor> meta mark set <minor>
```

The minor (10, 11, ...) is both the fwmark and the HTB class id `1:<minor>`.

The kernel stores a socket's cgroup when the socket is created and does not update it when the process is moved. An already-open connection would miss the nft rule. So bandctl also reads `/proc/<pid>/fd` and `/proc/net/{tcp,tcp6,udp,udp6}` and installs a flower filter per socket:

```
tc filter add dev IFACE parent 1: protocol ip prio 2 handle <n> flower \
    ip_proto tcp src_ip <local> src_port <lport> \
    dst_ip <remote> dst_port <rport> classid 1:<minor>
tc filter add dev IFACE parent ffff: protocol ip prio 1 handle <n> flower \
    ip_proto tcp dst_ip <local> dst_port <lport> \
    src_ip <remote> src_port <rport> \
    action skbedit mark <minor> pipe \
    action mirred egress redirect dev ifb-bandctl
```

Upload filters send the packet straight to the class. Download filters run before the matchall rule, stamp the mark, and redirect. The matchall rule is still what shapes a socket created after the move, once nft has stored the connmark. Listeners are skipped. Unconnected UDP is matched on the local address and port. A connected UDP filter has higher priority than an unconnected filter, so its remote address and port take precedence. A wildcard local address does not match the actual packet address, so an existing wildcard-bound UDP socket is not reliably shaped by default. With `--match-wildcard`, a wildcard-bound UDP flow instead matches only its local port and address family. Address-specific flower filters have higher priority than these port-only filters. The broad match can still shape other processes sharing that port. The session keeps this setting until it exits. The set is rebuilt on apply and on each rate sample, about once a second. A socket that closes loses its filter. If deletion fails, the filter stays tracked and the next sample retries it.

Upload: the output hook runs before the device qdisc, so `meta mark` is visible to `tc filter ... fw` on the real interface.

Download: the ingress qdisc runs before netfilter, so a mark set in prerouting is too late for that packet. The output rule also sets the conntrack mark. On ingress, a `ctinfo cpmark` action copies that connmark onto the skb, then `mirred` redirects the packet to `ifb-bandctl`, where a second HTB class caps it. So return traffic of a flow the process has already sent on is shaped. The first inbound packet of a brand new inbound connection is not, until something the process sends (an ACK is enough) has stored the connmark.

If the IFB setup fails, the apply is rolled back and the status line shows the `tc` or `ip` error. Download is not silently treated as capped.

## What gets installed

Upload, first time any process has an upload percent:

```
tc qdisc replace dev IFACE root handle 1: htb default 999
tc class replace ... classid 1:1 htb rate <parent>
tc class replace ... classid 1:999 htb rate <default> ceil <parent>
tc class replace ... classid 1:<minor> htb rate <rate> ceil <ceil>
tc filter add ... protocol ip   handle <minor> fw flowid 1:<minor>
tc filter add ... protocol ipv6 handle <minor> fw flowid 1:<minor>
```

The previous root qdisc kind is read first. `fq_codel`, `pfifo_fast`, `mq`, `fq`, `pfifo`, `cake`, and `noqueue` may be replaced. An existing `htb` qdisc is refused. The kind is stored in `/run/bandctl.json` together with each pid's original cgroup path (`0::` from `/proc/<pid>/cgroup`).

Download, first time any process has a download percent:

```
modprobe ifb
ip link add ifb-bandctl type ifb
ip link set ifb-bandctl up
tc qdisc add dev IFACE handle ffff: ingress
tc filter add dev IFACE parent ffff: prio 10 matchall \
    action ctinfo cpmark pipe \
    action mirred egress redirect dev ifb-bandctl
```

The same HTB shape is then added on `ifb-bandctl`. `ctinfo` is `pipe` so the redirect still runs. The default for a tc action is to stop the action list. The filter is `matchall` at priority 10. The `u32` classifier rejects `protocol all`, so that form never installed the redirect. Priority 10 leaves a lower number free for a filter that should match first.

Download setup refuses an existing `ifb-bandctl` device or ingress qdisc. It removes only the objects it created if setup fails. Recovery state records IFB and ingress ownership separately; older state files lack those fields and use the earlier IFB-based cleanup.

Members of one group use the same minor, so their leaf rules and flower filters lead to the same HTB class. The class is counted once when rates are planned. Class rates are replaced in place when a percent or the capacity changes. A failed capacity update retries the old class rates and reports an incomplete rollback if that also fails. Filters are added once per class. Failed class or mark filter deletion is reported so the operation can roll back. Removing the last share in a direction deletes that direction's qdisc. Removing the last share overall also deletes the nft table.

## Failure and exit

`apply` copies the previous share map. On error it restores the prior shares, writes the pid back to its old cgroup if a move was attempted, removes the leaf cgroup it made, and runs sync again. If the restore fails, it retains the new share and its original cgroup for a later cleanup and reports an incomplete rollback. A failed rollback sync is reported too. Its ceiling may not be active. A partial IFB setup is deleted before the error is raised.

`remove` checks the process is still in its bandctl leaf before writing it back to its old cgroup. If it moved elsewhere, bandctl leaves it there and drops the share. If the cgroup cannot be read or the write fails and the pid still exists, the share is kept and the error is raised, so a later Remove or the exit cleanup can try again. If the pid is gone, the share is dropped. An unreadable process entry is treated as still live, so the share stays tracked. A process can still move or exit after the membership check.

`teardown` is idempotent (`_torn`). It restores each pid still in its leaf, syncs to an empty share map (which restores the root qdisc and deletes IFB and nft), removes the cgroup directories, and deletes the state file. If that sync fails, it falls back to a scrub that deletes what bandctl may have created. The scrub restores the root qdisc only when this run had replaced it. Recovery state remains until exit cleanup even after the last share is removed, because a leaf can still contain inherited processes. A failed process restore, cgroup leaf cleanup, root qdisc restore, download cleanup, or nftables deletion is reported and leaves the state file for a later retry. A process that could not be restored or whose cgroup cannot be read stays in its leaf so recovery can retry its original cgroup. Recovery also keeps a leaf and its state when a reused PID is still inside it, so cleanup does not move the replacement. Download cleanup removes ingress before the IFB and records each completed step. A completed root qdisc restore is recorded too. Recovery disables new limits if a retry fails. An unreadable or malformed state file, a missing or invalid interface, or invalid PID, cgroup path, qdisc, start-time, or ownership fields also block recovery and leave the state file in place. `recover` uses the interface and root qdisc status recorded in the state file, even when the new session uses another interface. Older state files lack that status, so recovery restores their root qdisc. SIGTERM and `atexit` call teardown as well as the Textual unmount hook. The second call returns immediately.

Before moving a process or replacing a root qdisc, the shaper records the recovery information and refuses the operation if that write fails. State updates use a private temporary file and an atomic replace so a failed update preserves the old file. Recovery restores a recorded PID to its original cgroup only if it is still in its bandctl leaf and its start time matches the saved value. Old state files lack start times, so recovery checks only the leaf for those processes. During the final scrub, bandctl checks each remaining process is still in its leaf before moving it to the root cgroup. If its cgroup cannot be read, cleanup reports the error and keeps the leaf for retry. A process can still move or exit after the check.

## UI

If user lookup fails, the process scan shows the numeric UID and continues.

`app.py` refreshes once a second on a worker thread (`asyncio.to_thread`) so the `/proc` walk, `ss -tinp`, and `tc -s class show` do not block the keyboard. The Down and Up columns are the change in `bytes_received` and `bytes_acked` on non-loopback TCP sockets. The table hides a process at or below 1% of link capacity, or of that measured total when the link speed is unknown. Each table render uses one share snapshot and computes the traffic totals at most once. `tc -s class show` still drives the flower refresh. The shaper lock is an `RLock` shared with Apply. Selection is a pid, not a row index, so a rescan does not lose the highlight. Column widths are chosen so the table fits the terminal. User and sockets are dropped first. The command column takes the leftover width. If that would leave it under 16 cells, the percent headers become `Dn%` and `Up%` and the rate columns lose a cell. The table has no horizontal scrollbar.

The screen gets size classes that `theme.tcss` keys off:

| Class | When | Effect |
| --- | --- | --- |
| `-stacked` | under 108 columns | percent fields above the meters |
| `-narrow` | under 80 columns | short header, tighter footer |
| `-compact` | under 32 rows | one-row inputs and buttons, one-line meters, borderless log |
| `-short` | under 22 rows | two-line log |
| `-tall` | 42 rows or more | taller log |
| `-too-small` | under 60 columns or 18 rows | a message asking for a bigger terminal |

Sizes come from the resize event, because `App.size` can still be the old size while that handler runs.

The on-screen rate is each socket's counter delta times 8, divided by its sample interval, summed by PID. The first `ss` sample is a baseline and shows zero. `ss -tinpeH` supplies socket identities; a socket first seen later starts at zero. Class byte counters are still read so the flower filters stay in sync.

`traffic.parse_ss_sockets` can identify individual TCP sockets by the `ss` socket cookie, then inode, then endpoint pair. This keeps counters for separate sockets distinct. Shared sockets are attributed to each listed process.

The sampler accumulates observed socket deltas per PID. Live processes keep their totals while idle; totals for processes absent from scans expire after five minutes. When a scanned PID has a new start time, the app clears its previous total.

Byte counts use decimal units such as kB and MB. They remain byte counts when the rate display switches between bits and bytes per second.

`history.py` keeps one sample per second for each listed PID, for the last 60 seconds. A second where `ss` fails, even with partial output, is left as a gap, and a PID that leaves the list or gets a new start time loses its history. The graph splits the minute into as many columns as fit beside the bar, draws the highest rate in each column, and scales to the highest rate in the window.

The filter box matches a case-folded substring of the process name, command line, user, and pid. An empty filter shows the rows above the 1% cutoff (or every row after `n`). Any text in the filter searches every scanned process.
