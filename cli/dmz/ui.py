"""Terminal output: colour when it helps, plain when it is piped, JSON when asked."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Iterable, Sequence

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
          "magenta": "35", "cyan": "36"}
_enabled: bool | None = None


def configure(mode: str = "auto") -> None:
    """`auto` colours a terminal and not a pipe; NO_COLOR (https://no-color.org) wins over auto."""
    global _enabled
    if mode == "always":
        _enabled = True
    elif mode == "never":
        _enabled = False
    else:
        _enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def enabled() -> bool:
    if _enabled is None:
        configure()
    return bool(_enabled)


def style(text: Any, *names: str) -> str:
    text = str(text)
    if not enabled() or not names:
        return text
    return "".join(f"\033[{_CODES[n]}m" for n in names if n in _CODES) + text + "\033[0m"


def ok(text: Any) -> str:
    return style(text, "green")


def warn(text: Any) -> str:
    return style(text, "yellow")


def fail(text: Any) -> str:
    return style(text, "red")


def dim(text: Any) -> str:
    return style(text, "dim")


def bold(text: Any) -> str:
    return style(text, "bold")


def accent(text: Any) -> str:
    return style(text, "cyan")


def say(*parts: Any) -> None:
    print(" ".join(str(p) for p in parts))


def note(msg: str) -> None:
    """Progress and context: stderr, so stdout stays pipeable."""
    print(dim(msg), file=sys.stderr)


def hint(msg: str) -> None:
    print(dim("hint: ") + msg, file=sys.stderr)


def error(msg: str) -> None:
    print(fail("dmz: ") + msg, file=sys.stderr)


def kv(rows: Iterable[tuple[str, Any]], indent: int = 2) -> str:
    rows = [(k, v) for k, v in rows]
    width = max((len(k) for k, _ in rows), default=0)
    pad = " " * indent
    return "\n".join(f"{pad}{dim(k.ljust(width))}  {v}" for k, v in rows)


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], indent: int = 2) -> str:
    rows = [[_plain(c) for c in r] for r in rows]
    if not rows:
        return " " * indent + dim("(none)")
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(_strip(c)))
    pad = " " * indent
    lines = [pad + "  ".join(dim(h.ljust(w)) for h, w in zip(headers, widths))]
    for r in rows:
        lines.append(pad + "  ".join(c + " " * (w - len(_strip(c))) for c, w in zip(r, widths)))
    return "\n".join(lines)


def _plain(value: Any) -> str:
    if value is None:
        return dim("—")
    return str(value)


def _strip(text: str) -> str:
    """Length without colour codes, for alignment."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            j = text.find("m", i)
            i = (j + 1) if j != -1 else len(text)
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def breaker(state: str | None, warning: bool = False) -> str:
    """A breaker state coloured by what it means for the actuator."""
    s = state or "unknown"
    if s == "closed":
        return ok(s) if not warning else warn(f"{s} (warning)")
    if s in ("half_open", "hold"):
        return warn(s)
    if s == "open":
        return fail(s)
    return warn(s)


def verdict(v: str | None) -> str:
    return {"allow": ok("allow"), "review": warn("review"), "block": fail("block")}.get(v or "", warn(v or "?"))


def short(value: Any, n: int = 12) -> str:
    """A shortened id for tables: first n characters and an ellipsis."""
    s = str(value or "")
    return s if len(s) <= n else s[:n] + "…"
