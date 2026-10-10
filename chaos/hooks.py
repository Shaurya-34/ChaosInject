"""Placeholder hooks. Replace with the real implementations from #8 and #9.

Agree these signatures with the owners before changing them.
"""

import json
import time
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def should_stop(sample, baseline, policy):
    """Safety check (#8). Called after every probe during the fault.

    sample:   {"t": float, "ok": bool, "status": int|None, "latency_ms": float}
    baseline: list of samples taken before the fault
    policy:   contents of policy.json

    Return a reason string to stop the experiment early, or None to continue.
    Placeholder: never stops. The controller still enforces duration and hard timeout.
    """
    return None


def save_result(record):
    """Results store (#9). Persist one experiment record, return where it went."""
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{record['experiment']['name']}.json"
    path.write_text(json.dumps(record, indent=2))
    return str(path)
