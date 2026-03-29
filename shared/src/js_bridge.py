from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "python-bridge.mjs"
REPO_ROOT = Path(__file__).resolve().parents[2]


class JsBridgeError(RuntimeError):
    pass


def _run_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        ["node", str(BRIDGE_SCRIPT), json.dumps(payload)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        try:
            err = json.loads(proc.stderr or "{}")
            message = err.get("error") or proc.stderr.strip() or "Unknown JS bridge error"
        except Exception:
            message = proc.stderr.strip() or "Unknown JS bridge error"
        raise JsBridgeError(message)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise JsBridgeError(f"Invalid bridge response: {proc.stdout!r}") from exc


def inspect_export(module_relative_path: str, export_name: str) -> tuple[str, Any]:
    response = _run_bridge(
        {
            "mode": "inspect",
            "modulePath": str((REPO_ROOT / module_relative_path).resolve()),
            "exportName": export_name,
        }
    )
    return response.get("type", "undefined"), response.get("value")


def call_export(module_relative_path: str, export_name: str, *args: Any) -> Any:
    response = _run_bridge(
        {
            "mode": "call",
            "modulePath": str((REPO_ROOT / module_relative_path).resolve()),
            "exportName": export_name,
            "args": list(args),
        }
    )
    return response.get("value")


def export_accessor(module_relative_path: str, export_name: str) -> Any:
    export_type, value = inspect_export(module_relative_path, export_name)
    if export_type == "function":
        def _wrapped(*args: Any) -> Any:
            return call_export(module_relative_path, export_name, *args)

        _wrapped.__name__ = export_name
        return _wrapped
    return value
