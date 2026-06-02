from __future__ import annotations

import re
from typing import Any


# Body labels are derived from the Apache-2.0 metadata used by
# SaltieRL/DistributedReplays, with a small local extension for newer bodies
# observed through Ballchasing + Carball overlap.
BODY_NAME_BY_ID: dict[int, str] = {
    21: "Backfire",
    22: "Breakout",
    23: "Octane",
    24: "Paladin",
    25: "Road Hog",
    26: "Gizmo",
    28: "X-Devil",
    29: "Hotshot",
    30: "Merc",
    31: "Venom",
    402: "Takumi",
    403: "Dominus",
    404: "Scarab",
    523: "Zippy",
    597: "DeLorean Time Machine",
    600: "Ripper",
    607: "Grog",
    803: "Batmobile",
    1018: "Dominus GT",
    1159: "X-Devil Mk2",
    1171: "Masamune",
    1172: "Marauder",
    1286: "Aftershock",
    1295: "Takumi RX-T",
    1300: "Road Hog XL",
    1317: "Esper",
    1416: "Breakout Type-S",
    1475: "Proteus",
    1478: "Triton",
    1533: "Vulcan",
    1568: "Octane ZSR",
    1603: "Twin Mill III",
    1623: "Bone Shaker",
    1624: "Endo",
    1675: "Ice Charger",
    1691: "Mantis",
    1856: "Jaeger 619 RS",
    1919: "Centio V17",
    1932: "Animus GP",
    2268: "'70 Dodge Charger R/T",
    2269: "'99 Nissan Skyline GT-R R34",
    4284: "Fennec",
}

_CAR_STYLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fennec", ("fennec",)),
    ("plank", ("batmobile", "delorean", "skyline", "ice charger", "mantis", "twin mill")),
    ("dominus", ("dominus", "aftershock", "animus", "centio", "endo")),
    ("breakout", ("breakout", "samurai", "komodo")),
    ("merc", ("merc", "road hog", "marauder", "grog")),
    ("octane", ("octane", "takumi", "paladin", "hotshot")),
    ("hybrid", ("x-devil", "venom", "masamune", "ripper", "jager", "esper", "nimbus", "insidio", "tygris")),
)


def normalize_car_name(name: str | None) -> str | None:
    if not name:
        return None
    text = re.sub(r"\s+", " ", str(name)).strip()
    return text or None


def car_name_from_body_id(car_body_id: Any) -> str | None:
    try:
        if car_body_id is None:
            return None
        return BODY_NAME_BY_ID.get(int(car_body_id))
    except (TypeError, ValueError):
        return None


def car_style_from_name(name: str | None) -> str | None:
    normalized = normalize_car_name(name)
    if not normalized:
        return None
    key = normalized.casefold()
    for family, hints in _CAR_STYLE_HINTS:
        if any(hint in key for hint in hints):
            return family
    return None


def car_profile(*, car_name: str | None = None, car_body_id: Any = None) -> dict[str, Any]:
    resolved_name = normalize_car_name(car_name) or car_name_from_body_id(car_body_id)
    try:
        resolved_body_id = int(car_body_id) if car_body_id is not None else None
    except (TypeError, ValueError):
        resolved_body_id = None
    return {
        "car_body_id": resolved_body_id,
        "car_name": resolved_name,
        "car_family": car_style_from_name(resolved_name),
    }
