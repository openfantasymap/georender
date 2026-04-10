from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class RulesetError(ValueError):
    pass


SUPPORTED_GEOMS = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
}

SUPPORTED_SYMBOLIZERS = {
    "icon",
    "polygon_fill",
    "polygon_pattern",
    "line_pattern",
}


class RulesetStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def list_names(self) -> list[str]:
        return sorted(p.stem for p in self.base_dir.glob("*.json"))

    def revision(self, name: str) -> str:
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            raise RulesetError(f"Ruleset '{name}' not found")
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    def load(self, name: str) -> dict[str, Any]:
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            raise RulesetError(f"Ruleset '{name}' not found")
        data = json.loads(path.read_text(encoding="utf-8"))
        data = self._normalize(data)
        self.validate(data)
        return data

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = json.loads(json.dumps(data))
        for rule in normalized.get("rules", []):
            if "paint" in rule and "symbolizer" not in rule:
                rule["symbolizer"] = rule.pop("paint")
            if "z" in rule and "z_index" not in rule:
                rule["z_index"] = rule["z"]
            if "where" in rule and "filter" not in rule:
                rule["filter"] = _where_to_filter(rule["where"])
            if "type" in rule and "symbolizer" not in rule:
                # compatibility for very old flat rules, if ever needed
                rule["symbolizer"] = {"type": rule["type"]}
        return normalized

    def validate(self, data: dict[str, Any]) -> None:
        if "rules" not in data or not isinstance(data["rules"], list):
            raise RulesetError("Ruleset must contain a 'rules' array")
        asset_collections = data.get("asset_collections")
        if asset_collections is not None and not isinstance(asset_collections, (list, dict)):
            raise RulesetError("asset_collections must be either a list or an object map")
        for idx, rule in enumerate(data["rules"]):
            symbolizer = rule.get("symbolizer", {})
            symbolizer_type = symbolizer.get("type")
            if symbolizer_type not in SUPPORTED_SYMBOLIZERS:
                raise RulesetError(
                    f"Rule #{idx} has unsupported symbolizer type: {symbolizer_type}"
                )
            geoms = rule.get("geometry", [])
            if not geoms or not all(g in SUPPORTED_GEOMS for g in geoms):
                raise RulesetError(f"Rule #{idx} has invalid geometry types: {geoms}")
            edge_fade = rule.get("edge_fade")
            if edge_fade is not None:
                distance = edge_fade.get("distance_px", 0)
                if not isinstance(distance, (int, float)) or distance < 0:
                    raise RulesetError(
                        f"Rule #{idx} edge_fade.distance_px must be >= 0"
                    )


def feature_matches(rule: dict[str, Any], feature: dict[str, Any], geom_type: str) -> bool:
    if geom_type not in rule.get("geometry", []):
        return False

    filt = rule.get("filter") or {}
    props = feature.get("properties") or {}

    for key, expected in filt.items():
        if isinstance(expected, dict):
            if "in" in expected and props.get(key) not in expected["in"]:
                return False
            if "not_in" in expected and props.get(key) in expected["not_in"]:
                return False
            if "exists" in expected and bool(key in props) != bool(expected["exists"]):
                return False
            if "gte" in expected and not (props.get(key) is not None and props.get(key) >= expected["gte"]):
                return False
            if "lte" in expected and not (props.get(key) is not None and props.get(key) <= expected["lte"]):
                return False
        else:
            if props.get(key) != expected:
                return False
    return True


def _where_to_filter(where: dict[str, Any]) -> dict[str, Any]:
    all_rules = where.get("all") or []
    not_rules = where.get("not") or []
    out: dict[str, Any] = {}
    for item in all_rules:
        field = item.get("field")
        values = item.get("in")
        if field and values is not None:
            out[field] = {"in": values}
    for item in not_rules:
        field = item.get("field")
        values = item.get("in")
        if field and values is not None:
            existing = out.setdefault(field, {}) if isinstance(out.get(field), dict) else {}
            existing["not_in"] = values
            out[field] = existing
    return out
