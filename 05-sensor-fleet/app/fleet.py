"""The fleet controller: streams chiller telemetry to the logic engine and
lets each pump's actuator run only while its breaker allows it.

    python3 app/fleet.py                 # the scripted day: one pump overheats
    python3 app/fleet.py --pumps 3 --ticks 40 --interval 0.5
    python3 app/fleet.py --pump pump-7 --temp 92 --ticks 8     # one event stream by hand
    python3 app/fleet.py --run tue                             # replay the day on fresh subjects

Every reading is one event to POST /v1/logic/events; the platform answers
at once with what fired and, for each accumulator, the band the subject's
pattern now sits in. That band outlives the reading that reached it: the
accumulator decays on its half-life, so a pump that overheated keeps
reporting its band on cool readings until the stress has decayed. Before an
actuator tick, the controller calls check() on that pump's subject: a hold
pauses the pump, an open breaker stops it, a review appears for a person.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dmzagent import DMZAgent, DMZAgentError, RateLimitError, subject_id_for_division

HERE = Path(__file__).resolve().parent
SUBJECT_TYPE = "sensor"


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


class LogicDoor:
    """POST /v1/logic/events — the SDK has no method for the logic door yet,
    so this is the one REST call the controller makes itself."""

    def __init__(self, base_url: str, api_key: str, workspace_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workspace_id = workspace_id
        self._last = 0.0

    def pace(self) -> None:
        """Space every platform call (the event POST and the breaker check
        alike) so a fleet of pumps stays under the per-vendor burst limit."""
        gap = 0.15 - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def emit(self, event: dict) -> dict:
        body = json.dumps({"workspace_id": self.workspace_id, "event": event}).encode()
        req = urllib.request.Request(self.base_url + "/v1/logic/events", data=body, method="POST", headers={
            "authorization": f"Bearer {self.api_key}", "content-type": "application/json",
            "accept": "application/json", "user-agent": "dmzagent-examples/sensor-fleet"})
        for attempt in range(3):
            self.pace()
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429 and attempt < 2:
                    try:
                        wait = float(exc.headers.get("Retry-After") or json.loads(raw).get("retry_after") or 1)
                    except ValueError:
                        wait = 1.0
                    time.sleep(wait + 0.1)
                    continue
                return {"accepted": False, "error": exc.code, "detail": raw[:200]}
            except urllib.error.URLError as exc:
                return {"accepted": False, "error": 0, "detail": str(exc.reason)}
        return {"accepted": False, "error": 429, "detail": "rate limited"}


class Pump:
    """A pump with a temperature profile; `hot_from` is the tick after
    which its coolant climbs past the rulebook's limit."""

    def __init__(self, name: str, *, hot_from: int | None = None, offline_at: int | None = None,
                 seed: int = 0):
        self.name = name
        self.hot_from = hot_from
        self.offline_at = offline_at
        self.rng = random.Random(seed)
        self.running = True

    def reading(self, tick: int) -> dict:
        if self.offline_at is not None and tick >= self.offline_at:
            return {"status": "offline"}
        base = 72.0 + self.rng.uniform(-2, 2)
        if self.hot_from is not None and tick >= self.hot_from:
            base = 86.0 + min(12.0, (tick - self.hot_from) * 1.5) + self.rng.uniform(-1, 1)
        return {"status": "ok", "temp_c": round(base, 1), "pressure_bar": round(6.5 + self.rng.uniform(-0.4, 0.4), 2),
                "rpm": 1450 if self.running else 0}


class Controller:
    def __init__(self, env: dict[str, str]):
        self.division = env["DMZAGENT_DIVISION_ID"]
        self.door = LogicDoor(env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com"), env["DMZAGENT_APP_KEY"],
                              env["DMZAGENT_WORKSPACE_ID"])
        self.dmz = DMZAgent(api_key=env["DMZAGENT_APP_KEY"], base_url=env.get("DMZAGENT_BASE_URL", "https://api.dmzagent.com"))
        self.log: list[dict] = []

    def subject(self, pump: Pump) -> str:
        return subject_id_for_division(self.division, pump.name, subject_type=SUBJECT_TYPE)

    def tick(self, tick: int, pump: Pump) -> dict:
        subject = self.subject(pump)
        reading = pump.reading(tick)
        ack = self.door.emit({"subject_id": subject, "pump": pump.name, "tick": tick, **reading})
        fired = [f["rule_id"] for f in ack.get("fired", [])]
        bands = list(dict.fromkeys(f"{e['rule_id']}→{e['band']}" for e in ack.get("escalations", []) if e.get("band")))
        try:
            check = self._check(subject)
            decision = {"state": check.state, "allow": check.allow, "held": bool(check.raw.get("held")),
                        "warning": check.warning, "reason": check.reason}
        except DMZAgentError as exc:
            # Fail closed: a pump whose breaker cannot be read does not run.
            decision = {"state": "unknown", "allow": False, "held": False, "warning": True,
                        "reason": f"breaker could not be checked ({str(exc)[:80]}); actuator refused until it can be"}
        pump.running = bool(decision["allow"])
        row = {"tick": tick, "pump": pump.name, "reading": reading, "accepted": ack.get("accepted", False),
               "fired": fired, "bands": bands, "decision": decision, "error": ack.get("detail")}
        self.log.append(row)
        return row

    def _check(self, subject: str):
        """The SDK never retries a 429 on its own; wait out one before giving up."""
        for attempt in range(2):
            self.door.pace()
            try:
                return self.dmz.check(subject_id=subject)
            except RateLimitError as exc:
                if attempt:
                    raise
                time.sleep((exc.retry_after or 1) + 0.1)
        raise AssertionError("unreachable")

    def soul(self, pump: Pump) -> dict:
        url = (self.door.base_url + "/v1/logic/soul?" +
               urllib.parse.urlencode({"workspace_id": self.door.workspace_id, "subject_id": self.subject(pump)}))
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {self.door.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, ValueError) as exc:
            return {"labels": [], "error": str(exc)[:120]}


def render(row: dict) -> str:
    r = row["reading"]
    reading = (f"{r['temp_c']:5.1f} °C {r['pressure_bar']:4.1f} bar" if r.get("status") == "ok"
               else f"{r.get('status'):>18}")
    d = row["decision"]
    state = d["state"] + ("*" if d.get("warning") else "")
    actuator = "RUN " if d["allow"] else ("HOLD" if d.get("held") else "STOP")
    fired = ", ".join(row["bands"] or row["fired"]) or "—"
    note = "" if row["accepted"] else f"  [not accepted: {row.get('error')}]"
    return f"t={row['tick']:>3} {row['pump']:<12} {reading}  logic: {fired:<28} breaker: {state:<10} actuator: {actuator}{note}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="stream chiller telemetry through the logic engine")
    p.add_argument("--pumps", type=int, default=3)
    p.add_argument("--ticks", type=int, default=24)
    p.add_argument("--interval", type=float, default=0.4, help="seconds between ticks")
    p.add_argument("--pump", default=None, help="drive one pump by name instead of the scripted fleet")
    p.add_argument("--temp", type=float, default=None, help="with --pump: a fixed coolant temperature")
    p.add_argument("--run", default=None, help="a tag appended to every pump name: the platform remembers a pump's "
                                                "stress and breaker between runs, so a replay wants fresh subjects")
    args = p.parse_args(argv)
    env = load_env(HERE / ".env")
    missing = [k for k in ("DMZAGENT_APP_KEY", "DMZAGENT_WORKSPACE_ID", "DMZAGENT_DIVISION_ID") if not env.get(k)]
    if missing:
        print(f"app/.env is missing {', '.join(missing)} — run ./setup.sh first", file=sys.stderr)
        return 2
    ctl = Controller(env)

    if args.pump:
        pump = Pump(args.pump)
        if args.temp is not None:
            pump.reading = lambda tick: {"status": "ok", "temp_c": args.temp, "pressure_bar": 6.5, "rpm": 1450}  # type: ignore[assignment]
        pumps = [pump]
    else:
        # The scripted day: pump-7 overheats from tick 6; pump-9's sensor goes offline at tick 14.
        suffix = f"-{args.run}" if args.run else ""
        pumps = [Pump(f"pump-{i + 5}{suffix}", seed=i) for i in range(args.pumps)]
        if len(pumps) > 2:
            pumps[2].hot_from = 6
        if len(pumps) > 4:
            pumps[4].offline_at = 14
        elif len(pumps) > 1:
            pumps[1].offline_at = 14

    print(f"fleet of {len(pumps)}: " + ", ".join(ctl.subject(x) for x in pumps))
    print("every reading is one logic event; the breaker is checked before each actuator tick")
    print("logic: what fired on this reading, and the band each accumulator's pattern sits in\n")
    for tick in range(1, args.ticks + 1):
        for pump in pumps:
            print(render(ctl.tick(tick, pump)))
        time.sleep(args.interval)

    print("\nlogic souls (accumulated labels, decaying on their half-lives):")
    for pump in pumps:
        soul = ctl.soul(pump)
        labels = ", ".join(f"{l['label']}={l['strength']:.2f}" + (f" ({l['tag']})" if l.get("tag") else "")
                           for l in soul.get("labels", []))
        print(f"  {pump.name:<12} {labels or '—'}")
    stopped = [x.name for x in pumps if not x.running]
    print(f"\nactuators stopped or held by the breaker: {', '.join(stopped) or 'none'}")
    print("release a held pump from the console's review queue, or with POST /v1/cb/release; the breaker moves on the ledger.")
    print("the stress and the breaker live on the platform, not here: run again within the half-life and the pump starts")
    print("where it left off. --run <tag> replays the day on fresh subjects.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
