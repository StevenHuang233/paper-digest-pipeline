from __future__ import annotations

import json
from pathlib import Path

from ..models import Paper


def fetch_json(config: dict, *, config_dir: Path) -> list[Paper]:
    path = Path(config["discovery"]["json_path"])
    if not path.is_absolute():
        path = config_dir / path
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    records = value.get("papers", []) if isinstance(value, dict) else value
    if not isinstance(records, list):
        raise ValueError("JSON source must be a list or an object containing a papers list")
    return [Paper.from_dict(record) for record in records[: int(config["discovery"]["max_candidates"])]]

