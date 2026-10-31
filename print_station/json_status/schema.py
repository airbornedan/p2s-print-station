"""Allowlisted public status. Reused at the upload boundary."""
import math
import re


def number(value, low, high):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and low <= n <= high else None
    except (TypeError, ValueError):
        return None


def clean(doc):
    def text(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 .+_-]{1,48}", value) else None

    slots = []
    for slot in doc.get("ams", [])[:32]:
        unit = number(slot.get("unit"), 0, 15)
        index = number(slot.get("slot_index"), 0, 3)
        if unit is None or index is None or int(unit) != unit or int(index) != index:
            continue
        color = slot.get("color")
        slots.append({"unit": int(unit), "slot_index": int(index), "slot_number": int(index) + 1,
                      "material": text(slot.get("material")), "name": text(slot.get("name")),
                      "color": color if isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else None,
                      "remaining_percent": number(slot.get("remaining_percent"), 0, 100)})
    state = doc.get("state")
    return {"schema_version": 1, "generated_at_unix": number(doc.get("generated_at_unix"), 0, 1e11),
            "observed_at_unix": number(doc.get("observed_at_unix"), 0, 1e11),
            "ams_observed_at_unix": number(doc.get("ams_observed_at_unix"), 0, 1e11),
            "state": state if state in {"IDLE", "PREPARE", "SLICING", "RUNNING", "PAUSE", "FINISH", "FAILED"} else None,
            "progress_percent": number(doc.get("progress_percent"), 0, 100),
            "remaining_minutes": number(doc.get("remaining_minutes"), 0, 100000),
            "layer": number(doc.get("layer"), 0, 1000000),
            "total_layers": number(doc.get("total_layers"), 0, 1000000), "ams": slots}
