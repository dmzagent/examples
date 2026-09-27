"""Dotenv files the way the examples use them: ids and keys an application
reads at start, written mode 0600 because one of the values is a key."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def set_env_var(path: Path, name: str, value: str) -> None:
    """Set NAME=value, replacing an existing line for NAME."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    rendered = f"{name}={_quote(value)}"
    for i, line in enumerate(lines):
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            lines[i] = rendered
            break
    else:
        lines.append(rendered)
    if not path.exists():
        path.touch(mode=0o600)
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_env_var(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) == name:
            raw = line.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            return raw
    return None


def _quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]*", value):
        return value
    return json.dumps(value)
