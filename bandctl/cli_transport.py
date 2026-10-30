"""Local socket transport for a non-interactive shaping session."""

from __future__ import annotations

import json
import os
import socket
import stat
import time

from bandctl.cli_protocol import dispatch

_MAX_MESSAGE = 16_384


def _read_line(connection: socket.socket) -> bytes:
    data = bytearray()
    while len(data) <= _MAX_MESSAGE:
        part = connection.recv(min(4096, _MAX_MESSAGE + 1 - len(data)))
        if not part:
            break
        data.extend(part)
        if b"\n" in part:
            break
    if len(data) > _MAX_MESSAGE:
        raise ValueError("request is too large")
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise ValueError("invalid request framing")
    return bytes(data[:-1])


def request(path: str, payload: dict, *, timeout: float = 3.0) -> dict:
    """Send one request and read one bounded response."""
    encoded = json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_MESSAGE:
        raise ValueError("request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.sendall(encoded)
        data = _read_line(connection)
    try:
        response = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("invalid session response") from exc
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise ValueError("invalid session response")
    return response


def _encode_response(response: dict) -> bytes:
    """Keep errors and recent warnings within the client's response limit."""
    def encode(value):
        return json.dumps(value, allow_nan=False).encode("utf-8") + b"\n"

    encoded = encode(response)
    if len(encoded) <= _MAX_MESSAGE:
        return encoded
    error = response.get("error")
    if isinstance(error, str) and len(json.dumps(error).encode("utf-8")) > _MAX_MESSAGE // 2:
        suffix = " ... [error truncated]"
        low, high = 0, len(error)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(error[:middle] + suffix).encode("utf-8")) <= _MAX_MESSAGE // 2:
                low = middle
            else:
                high = middle - 1
        response = dict(response, error=error[:low] + suffix)
        encoded = encode(response)
    warnings = response.get("warnings")
    if len(encoded) <= _MAX_MESSAGE or not isinstance(warnings, list) or not warnings:
        return encoded
    marker = "Some background warnings omitted"
    limited = dict(response, warnings=[marker])
    size = len(encode(limited))
    retained = []
    for warning in reversed(warnings):
        item_size = len(json.dumps(warning, allow_nan=False).encode("utf-8")) + 2
        if size + item_size > _MAX_MESSAGE:
            break
        retained.append(warning)
        size += item_size
    limited["warnings"] = [marker, *reversed(retained)]
    return encode(limited)


def serve(session, path: str, *, interval: float = 1.0, on_stop=None) -> None:
    """Serve requests until stop; the caller owns the session lock and teardown."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        # Bind under a restrictive umask so the socket is private from creation.
        old_mask = os.umask(0o177)
        try:
            listener.bind(path)
        finally:
            os.umask(old_mask)
        info = os.lstat(path)
        identity = (info.st_dev, info.st_ino)
        try:
            listener.listen(8)
            listener.settimeout(min(interval, 0.5))
            next_refresh = time.monotonic() + interval
            stopping = False
            while not stopping:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    connection = None
                if connection is not None:
                    with connection:
                        connection.settimeout(0.5)
                        try:
                            try:
                                payload = json.loads(_read_line(connection).decode("utf-8"))
                            except RecursionError as exc:
                                raise ValueError("request is nested too deeply") from exc
                            response, stopping = dispatch(session, payload)
                            if stopping and on_stop is not None:
                                try:
                                    notes = on_stop()
                                    if notes:
                                        response = {"ok": False, "error": "; ".join(notes)}
                                except (OSError, RuntimeError) as exc:
                                    response = {"ok": False, "error": str(exc)}
                            warnings = getattr(session, "warnings", None)
                            if isinstance(warnings, list) and warnings:
                                response["warnings"] = list(warnings)
                        except (ValueError, UnicodeDecodeError, OSError) as exc:
                            response = {"ok": False, "error": str(exc)}
                        try:
                            connection.sendall(_encode_response(response))
                        except OSError:
                            pass
                if not stopping and time.monotonic() >= next_refresh:
                    try:
                        session.refresh()
                    except (OSError, RuntimeError, ValueError) as exc:
                        session.warnings = [f"refresh failed: {exc}"]
                    next_refresh = time.monotonic() + interval
        finally:
            try:
                current = os.lstat(path)
                if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.unlink(path)
            except FileNotFoundError:
                pass
