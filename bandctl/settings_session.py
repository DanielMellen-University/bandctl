"""Export and import one running session's settings."""

from __future__ import annotations

from bandctl.groups import active_groups
from bandctl.settings import plan_group_changes, read_settings, snapshot_settings, validate_targets, write_settings
from bandctl.shaper import ShaperError


_BOOT_PATH = "/proc/sys/kernel/random/boot_id"


def _boot_id(shaper) -> str:
    try:
        return shaper.host.read(_BOOT_PATH).strip()
    except OSError as exc:
        raise ValueError("cannot read the current boot ID") from exc


def export_session(path, shaper, iface, down_budget, up_budget, rules) -> None:
    """Write an explicit snapshot of the active session."""
    shares = shaper.shares
    boot_id = _boot_id(shaper) if active_groups(shares) else None
    settings = snapshot_settings(iface, down_budget, up_budget, rules, shares, boot_id)
    write_settings(path, settings)


def import_session(path, shaper, iface, detected_bps, processes):
    """Validate the whole file and install its groups before returning settings."""
    settings = read_settings(path)
    boot_id = _boot_id(shaper) if settings.groups else ""
    validate_targets(settings, iface, boot_id, processes)
    plan_group_changes(settings, shaper.shares)
    down_cap = settings.down_budget if settings.down_budget is not None else detected_bps
    up_cap = settings.up_budget if settings.up_budget is not None else detected_bps
    try:
        shaper.replace_groups(settings, down_cap, up_cap)
    except ShaperError as exc:
        raise ValueError(str(exc)) from exc
    return settings
