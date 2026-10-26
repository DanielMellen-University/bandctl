# Changelog

## 0.1.0 (2026-10-26)

First usable UI.

- A process gets a download percent and an upload percent, each a ceiling on that direction's capacity (link speed, or a budget typed in the header). The ceiling is not a share of whatever traffic happens to be on the link this second, because that number moves when the cap moves.
- `b` toggles every absolute readout between Mbit/s and MB/s. Percent fields stay percents. 30% of 100 Mbit/s is shown as 30.0 Mbit/s or 3.75 MB/s.
- When several ceilings in one direction sum past the link, each ceil stays at the requested percent and the guaranteed HTB rates shrink so they fit. The status line says the shares add up to more than 100%.
- Upload is an HTB class on the real interface. nft marks the socket in the output hook, which runs before that qdisc. Download is an HTB class on `ifb-bandctl`. Ingress runs before netfilter, so the mark is copied from conntrack with the `ctinfo` action and the packet is redirected onto the IFB device.
- The process is moved into `/sys/fs/cgroup/bandctl/bandctl-<pid>` and put back on remove and on exit. Quit deletes the nft table, the IFB device, and the HTB root qdisc, then restores the previous root qdisc. `/run/bandctl.json` lets the next start finish that cleanup after a crash.
- The UI is Textual: a process table with a green cursor and orange limited rows, filter, sort by measured class rate, live bars, and a log of shaper errors. Without root the list still works and apply stays off.
- Unit tests cover the rate split, the command plan (against a fake host), and a headless pass over the screen. They do not touch a live interface.
