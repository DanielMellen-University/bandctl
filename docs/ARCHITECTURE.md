# Architecture

bandctl is a Textual app in front of one `Shaper` object. The app never shells out itself. `shaper.py` is the only place that runs `tc`, `nft`, `ip`, and `modprobe`, or that writes a cgroup file.

## Shares

A share is two optional integers, download and upload, each from 1 to 100. Blank means that direction is not enforced.

Capacity for a direction is the budget the user typed, or else the sysfs link speed, in bit/s. `rates.percent_to_bps` is integer arithmetic: 30% of 100_000_000 is 30_000_000. Display divides by 1_000_000 for Mbit/s and by 8_000_000 for MB/s.

`rates.htb_plan` turns those ceilings into kbit numbers for HTB:

- each ceil is the requested share, capped at the parent
- 1 kbit is reserved for the default class (unmarked traffic)
- if the ceils fit in what is left, each class `rate` equals its `ceil`
- if they do not, the rates shrink in proportion and the ceils stay

The default class `rate` is the leftover. Its `ceil` is the whole parent, so unmarked traffic can use the link when the limited classes are idle. A limited class cannot borrow past its ceil.

## Who owns the packets

The process is moved to `/sys/fs/cgroup/bandctl/bandctl-<pid>`. nft's `socket cgroupv2 level N` matches one path component, not the whole path. Level 2 is the leaf name `bandctl-<pid>`, which is unique under `bandctl/`.

```
add rule inet bandctl output socket cgroupv2 level 2 "bandctl-<pid>" \
    ct mark set <minor> meta mark set <minor>
add rule inet bandctl prerouting socket cgroupv2 level 2 "bandctl-<pid>" \
    ct mark set <minor> meta mark set <minor>
```

The minor (10, 11, ...) is both the fwmark and the HTB class id `1:<minor>`.

Upload: the output hook runs before the device qdisc, so `meta mark` is visible to `tc filter ... fw` on the real interface.

Download: the ingress qdisc runs before netfilter, so a mark set in prerouting is too late for that packet. The output rule also sets the conntrack mark. On ingress, a `ctinfo cpmark` action copies that connmark onto the skb, then `mirred` redirects the packet to `ifb-bandctl`, where a second HTB class caps it. Return traffic of a flow the process has already sent on is therefore shaped. The first inbound packet of a brand new inbound connection is not, until something the process sends (an ACK is enough) has stored the connmark.

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

Class rates are replaced in place when a percent or the capacity changes. Filters are added once per class. Removing the last share in a direction deletes that direction's qdisc. Removing the last share overall also deletes the nft table.

## Failure and exit

`apply` copies the previous share map. On any error it restores that map, writes the pid back to its old cgroup if this call had moved it, and runs sync again so the qdisc matches the restored map. A partial IFB setup is deleted before the error is raised.

`teardown` is idempotent (`_torn`). It restores every pid, syncs to an empty share map (which restores the root qdisc and deletes IFB and nft), removes the cgroup directories, and deletes the state file. `recover` does the same from the state file on the next start. SIGTERM and `atexit` call teardown as well as the Textual unmount hook. The second call returns immediately.

## UI

`app.py` refreshes once a second on a worker thread (`asyncio.to_thread`) so the `/proc` walk and `tc -s class show` do not block the keyboard. The shaper lock is an `RLock` shared with Apply. Selection is a pid, not a row index, so a rescan does not lose the highlight. Below 88 columns the user and socket columns are dropped. Below 28 rows the log and the detail strip get shorter. Both use the screen size from the resize event, because `App.size` can still be the old size while that handler runs.

Measured usage is the difference of `Sent N bytes` on the class, times 8, divided by the sample interval. The first sample is a baseline and shows zero.
