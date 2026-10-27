"""Validate and dispatch requests to a non-interactive shaping session."""

from __future__ import annotations

from bandctl.shaper import ShaperError


_FIELDS = {
    "limit": {"command", "target", "down", "up"},
    "remove": {"command", "target"},
    "export": {"command", "path"},
    "import": {"command", "path"},
    "stop": {"command"},
    "ping": {"command"},
}


def dispatch(session, request) -> tuple[dict, bool]:
    """Return a JSON-safe response and whether the session should stop."""
    try:
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            raise ValueError("invalid request")
        command = request["command"]
        if command not in _FIELDS or set(request) != _FIELDS[command]:
            raise ValueError("invalid command fields")
        if command == "ping":
            return {"ok": True, "result": {"iface": session.iface}}, False
        if command in ("limit", "remove"):
            if not isinstance(request["target"], str):
                raise ValueError("target must be text")
        if command == "limit":
            if any(value is not None and not isinstance(value, str) for value in (request["down"], request["up"])):
                raise ValueError("shares must be text")
            result = session.limit(request["target"], request["down"], request["up"])
        elif command == "remove":
            result = session.remove(request["target"])
        elif command in ("export", "import"):
            path = request["path"]
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                raise ValueError("enter a file path")
            if command == "export":
                session.export(path)
            else:
                session.import_file(path)
            result = {"path": path}
        else:
            return {"ok": True, "result": {}}, True
        return {"ok": True, "result": result}, False
    except (ValueError, ShaperError, OSError) as exc:
        return {"ok": False, "error": str(exc)}, False
