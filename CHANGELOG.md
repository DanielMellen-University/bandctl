# Changelog

## Unreleased

- TCP rate sampling no longer uses an expired baseline when an absent socket reappears.

- Process scans show a numeric UID if user lookup fails.

- Recovery state stays until exit cleanup, even after the last share is removed.

- The TUI reports cleanup errors after exit and returns a failure status.

- Failed cgroup leaf cleanup is reported and retried on the next start.

- A failed process cgroup lookup no longer changes capacity for existing limits.

- Recovery rejects invalid ownership flags before changing network state.

- Recovery rejects invalid process start times instead of skipping identity checks.

- A refresh arriving after the TUI screen closes no longer raises an error.

- Remove keeps a share for retry when process liveness cannot be checked.

- Unreadable or malformed recovery files block startup and are kept for retry.

- Recovery keeps a process in its leaf for retry if its cgroup cannot be read.

- Failed process restoration keeps its original cgroup in recovery state for a later retry.

- Recovery rejects an invalid PID mapping without crashing or discarding the state file.

- bandctl refuses an existing `inet bandctl` nftables table and leaves tables it did not create alone.

- Failed nftables cleanup keeps recovery state and blocks new limits until cleanup succeeds.

- Failed download cleanup is reported and keeps recovery state for a retry. The IFB stays in place if ingress removal fails.

- Download setup refuses an existing IFB device or ingress qdisc and leaves it untouched. Recovery records which download objects belonged to the session.

- Failed root qdisc recovery is shown in the TUI and stops CLI session startup.

- Failed root qdisc restoration is reported and keeps recovery state for a retry.

- Recovery state updates replace the old file atomically, so a failed write does not truncate it.

- A process is not moved until its original cgroup is recorded for crash recovery.

- Crash recovery checks the saved process identity and cgroup before restoring a PID, so a reused PID is not moved to the old process's cgroup.

- Manual TUI limits verify the selected process start time before changing it, so a reused PID is not mistaken for the old process.

- Crash recovery cleans the interface saved by the previous session, even when the new session uses another interface. Download-only recovery leaves the root qdisc alone.

- A failed new limit keeps its process tracked if restoring its cgroup also fails, so Remove can retry.

- An apply waiting for teardown no longer installs a limit after shutdown.

- Deeply nested settings files are rejected without stopping the session.

- A deeply nested CLI socket request no longer stops the background session.

- Process names with non-UTF-8 bytes no longer break TCP rate sampling.

- CLI mode is complete: `list`, `limit`, `remove`, `export`, `import`, and `stop`, with `--json`. Commands use the same validation and shaper as the TUI. Later commands report recent background errors, and a failed `stop` shows its own error.

- CLI sessions update detected link capacity while they run.

- CLI limit, remove, import, export, and stop commands use a locked background shaping session.

- A first individual limit now requires a known capacity before moving the process.

- Root TUI sessions hold an exclusive lock until cleanup finishes, so two sessions cannot control the same shaping state.

- `bandctl list` and `bandctl list --json` show running processes without opening the TUI.

- Import and export rules, schedules, budgets, and groups as a JSON settings file with `x`. Group process identities are checked before import. `--config` selects the saved interface and imports explicitly at startup.

- Named groups can share one download and upload ceiling. Press `g` to create and edit groups or add a process. Remove a member from the main table.

- Each process has a one-minute graph of its download and upload rates beside the bars.
- `TCP total` shows an estimate of the TCP bytes each process has moved since bandctl started, in the table when there is room and always on the detail line.
- Auto-apply rules (`t`): part of a process name plus a download percent, an upload percent, or both. Matching processes get the limit when they appear. A rule can have a daily `HH:MM` window, and the limit comes off when the window ends. Rules last for the session only.
- A search in the filter box covers every process, including idle ones under the 1% cutoff. Before, it only searched the rows already listed, and the hint to press `n` could not be used while typing.
- Enter in a rule editor field adds the rule. Success messages in the editor are green, and only errors are orange.
- Budget fields refuse values that are not finite or are below 1 bit/s. The default route with the lowest metric is used when there are several.
- The layout fits the terminal. Column widths fill the window instead of scrolling sideways, and below 108 columns the meters sit under the percent fields. Below 60 columns or 18 rows, bandctl asks for a bigger window.
- Under 32 rows the inputs and buttons take one row each and each meter fits on one line, so an 80 by 24 terminal shows ten process rows instead of two. Under 80 columns the percent headers become `Dn%` and `Up%` so the command column stays readable. The log keeps at least two visible lines, and its scrollbar only shows when there is something to scroll.
- The Remove button label no longer wraps onto two lines. The table no longer shows a horizontal scrollbar when the vertical one appears.
- Footer keys are listed most used first: Quit, Apply, Remove, Search, Sort, All, Unit, Rules. Refresh (`r`) still works but is no longer shown.
- `--iface` is checked at start. An unknown or malformed name exits with an error that lists the real interfaces.
- Quitting no longer replaces a root qdisc bandctl never touched. Before, a failed nft cleanup at exit could swap the interface's qdisc for `fq_codel`.
- Remove keeps the share when the process cannot be moved back to its old cgroup. Before, bandctl forgot the share while the process stayed in the bandctl cgroup.
- A new share that fails to apply also removes the empty cgroup made for it.
- The process table lists processes using more than 1% of the link. With no link speed, the cutoff is 1% of the non-loopback TCP traffic measured from `ss`. `n` shows every process. A process that already has a share stays visible after its traffic drops.
- Footer keys are black on pastel green, and the key labels and the status line are white, so the bottom bar stays readable.
- Apply commits the budget fields. A number typed in the header is used even when Enter was not pressed there.
- Moving the highlight repaints limited rows immediately. The selected row stays green. The other limited rows stay orange.
- A failed refresh is written to the log once. The next successful refresh clears that status.
- Putting a process back in its original cgroup reports the write error. The host raises that as a shaper error, and the old handler only caught `OSError`.
- The ingress redirect is a `matchall` filter. `u32` rejects `protocol all`, so download setup could fail before any class existed.
- Open sockets are shaped even though the kernel leaves them in the cgroup where they were created. Each of the process's connections gets a flower filter, refreshed when rates are sampled. Listeners are skipped. A socket that closes loses its filter.
- `sudo ./install.sh` installs bandctl under `/opt/bandctl` and links `bandctl` onto `PATH`, so the run command is `sudo bandctl`. The same script can be piped to `sudo bash` from GitHub. `sudo bandctl-uninstall` removes that install and clears qdisc, nft, and cgroup state left by a crash.

## 0.1.0 (2026-10-26)

First usable UI.

- A process gets a download percent and an upload percent, each a ceiling on that direction's capacity (link speed, or a budget typed in the header). The ceiling is not a share of whatever traffic happens to be on the link this second, because that number moves when the cap moves.
- `b` toggles every absolute readout between Mbit/s and MB/s. Percent fields stay percents. 30% of 100 Mbit/s is shown as 30.0 Mbit/s or 3.75 MB/s.
- When several ceilings in one direction sum past the link, each ceil stays at the requested percent and the guaranteed HTB rates shrink so they fit. The status line says the shares add up to more than 100%.
- Upload is an HTB class on the real interface. nft marks the socket in the output hook, which runs before that qdisc. Download is an HTB class on `ifb-bandctl`. Ingress runs before netfilter, so the mark is copied from conntrack with the `ctinfo` action and the packet is redirected onto the IFB device.
- The process is moved into `/sys/fs/cgroup/bandctl/bandctl-<pid>` and put back on remove and on exit. Quit deletes the nft table, the IFB device, and the HTB root qdisc, then restores the previous root qdisc. `/run/bandctl.json` lets the next start finish that cleanup after a crash.
- The UI is Textual: a process table with a green cursor and orange limited rows, filter, sort by measured class rate, live bars, and a log of shaper errors. Without root the list still works and apply stays off.
- Unit tests cover the rate split, the command plan (against a fake host), and a headless pass over the screen. They do not touch a live interface.
