"""``python -m bandctl``."""

from __future__ import annotations

import argparse
import atexit
import signal
import sys

from bandctl import __version__
from bandctl.app import BandctlApp
from bandctl.iface import IfaceError, check_iface
from bandctl.settings import read_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bandctl",
        description=(
            "Terminal UI for per-process download and upload shares. "
            "A share is a percent of link capacity. Rules exist only while "
            "bandctl is running, and exit removes them."
        ),
    )
    parser.add_argument("--version", action="version", version=f"bandctl {__version__}")
    parser.add_argument(
        "--iface",
        help="Interface to shape. Default: the device on the IPv4 default route.",
    )
    parser.add_argument("--config", help="Import this settings file at startup.")
    args = parser.parse_args(argv)
    if args.config is not None:
        try:
            settings = read_settings(args.config)
        except ValueError as exc:
            parser.error(str(exc))
        if args.iface is not None and args.iface != settings.iface:
            parser.error("--iface differs from the settings file interface")
        args.iface = settings.iface
    if args.iface is not None:
        try:
            check_iface(args.iface)
        except IfaceError as exc:
            parser.error(str(exc))
    app = BandctlApp(iface=args.iface, startup_settings=args.config)

    def _stop(signum, _frame) -> None:
        if app.shaper is not None:
            app.shaper.teardown()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _stop)
    atexit.register(_cleanup, app)
    app.run()
    return 0


def _cleanup(app: BandctlApp) -> None:
    shaper = app.shaper
    if shaper is not None and app.is_root:
        shaper.teardown()


if __name__ == "__main__":
    sys.exit(main())
