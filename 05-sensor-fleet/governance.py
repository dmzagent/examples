"""Governance for Harbor Supply's chiller fleet — sensors, not chatbots.

The same platform that governs an agent governs any system that acts. This
workspace runs the deterministic **logic engine**: no model, no prompt. A
rulebook of predicates and half-life accumulators reads every telemetry
event, keeps a "logic soul" per pump (labels whose strength grows with each
match and decays over time), and policies on that soul move each pump's
circuit breaker. The fleet controller (app/fleet.py) checks the breaker
before it lets a pump's actuator run.

This workspace must be a **logic** workspace (engine kind chosen when the
workspace is created in the console). The key in DMZAGENT_API_KEY belongs
to it.
"""
from giaas import Governance, presence, settings, strength

SLUG = "chiller-thermal"

# The rulebook: predicates over each event, plus accumulators whose bands
# route to the record / enforce / human lanes as they climb.
RULEBOOK = {
    "rules": [
        {
            "id": "over-temp",
            "description": "coolant above 85 °C; sustained excursions escalate, isolated ones decay away",
            "match": {"predicate": "comparison", "field": "temp_c", "gt": 85},
            "accumulate": {"weight": 1.0, "half_life": "10m",
                           "bands": {"record": 1, "enforce": 3, "human": 5}},
            "on_true": [{"disposition": "enforce", "level": "hold", "tag": "thermal_stress", "severity": 3}],
        },
        {
            "id": "pressure-spike",
            "description": "discharge pressure over 9 bar",
            "match": {"predicate": "comparison", "field": "pressure_bar", "gt": 9},
            "accumulate": {"weight": 2.0, "half_life": "5m",
                           "bands": {"record": 2, "enforce": 4}},
            "on_true": [{"disposition": "enforce", "level": "block", "tag": "over_pressure", "severity": 4}],
        },
        {
            "id": "sensor-offline",
            "description": "the sensor stopped reporting a valid status",
            "match": {"predicate": "value", "field": "status", "in": ["offline", "fault"]},
            "on_true": [{"disposition": "coordinate", "level": "review", "tag": "sensor_offline"}],
        },
        {
            "id": "bad-reading",
            "description": "a reading outside the physically possible range is data trouble, not thermal trouble",
            "match": {"op": "or", "of": [
                {"predicate": "comparison", "field": "temp_c", "lt": -40},
                {"predicate": "comparison", "field": "temp_c", "gt": 200},
            ]},
            "on_true": [{"disposition": "record", "tag": "bad_reading"}],
        },
    ]
}

governance = Governance("sensor-fleet", "deterministic governance for a chiller fleet")

governance.division_config(enforcement_posture=settings.get("posture", "enforce"))

rulebook = governance.logic_rulebook(
    "Chiller thermal rulebook",
    slug=SLUG,
    rulebook=RULEBOOK,
    description="thermal and pressure controls for the chiller pumps",
)

# The rulebook's inline dispositions become policies on the logic soul when
# it is installed (one per band). These add the responses the rulebook does
# not name: a hard stop when stress keeps climbing past every band, and a
# review when a sensor goes quiet.
governance.policy(
    "Block a pump under sustained thermal stress",
    when=[strength("thermal_stress", ">=", 6)],
    lane="enforce",
    level="block",
    soul="logic",
    description="Six weighted excursions inside the half-life window is a failing pump, not a hot afternoon.",
)
governance.policy(
    "Review an offline sensor",
    when=[presence("sensor_offline")],
    lane="coordinate",
    level="review",
    soul="logic",
)

governance.sdk_key("fleet controller", env_var="DMZAGENT_APP_KEY")
governance.env("FLEET_RULEBOOK_SLUG", SLUG)

governance.expect(
    "four excursions hold the pump",
    soul="logic", labels={f"{SLUG}@1:over-temp": 4.0, "thermal_stress": 4.0},
    enforce="hold",
)
governance.expect(
    "six excursions stop it",
    soul="logic", labels={f"{SLUG}@1:over-temp": 6.0, "thermal_stress": 6.0},
    enforce="block", coordinate="review",
    policies=["Block a pump under sustained thermal stress"],
)
governance.expect(
    "one excursion is only recorded",
    soul="logic", labels={f"{SLUG}@1:over-temp": 1.0, "thermal_stress": 1.0},
    enforce=None, coordinate=None,
)
