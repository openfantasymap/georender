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
    "polygon_texture",
    "line_pattern",
    "wms",
}

# Symbolizers that paint the whole viewport once, independent of any feature.
# Rules using these don't need a `geometry` whitelist or `filter`.
VIEWPORT_SYMBOLIZERS = {"wms"}


class RulesetStore:
    def __init__(self, base_dir: str | Path, cache_dir: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        # Cache `(resolved_payload_dict, content_hash_hex)` keyed by URL so we don't
        # hit the network on every tile request after the first.
        self._remote_cache: dict[str, tuple[dict[str, Any], str]] = {}
        # Inline ad-hoc rulesets, registered by name at runtime (e.g. from a
        # georender.json bundle's inline `ruleset` block, or a one-shot CLI render).
        # Looked up before the on-disk files so they shadow any same-named file.
        self._inline_registry: dict[str, tuple[dict[str, Any], str]] = {}

    def register_inline(self, name: str, data: dict[str, Any]) -> None:
        """Register an in-memory ruleset under `name`.

        Subsequent `load(name)` / `revision(name)` calls return this dict (after
        normalization + validation) instead of touching the on-disk registry.
        The data is deep-copied so callers can't mutate the registered form.
        """
        if not isinstance(data, dict):
            raise RulesetError(f"Inline ruleset '{name}' must be a JSON object")
        clone = json.loads(json.dumps(data))
        payload = json.dumps(clone, sort_keys=True, separators=(",", ":")).encode("utf-8")
        content_hash = hashlib.sha256(payload).hexdigest()[:16]
        self._inline_registry[name] = (clone, content_hash)

    def clear_inline(self) -> None:
        self._inline_registry.clear()

    def list_names(self) -> list[str]:
        names = {p.stem for p in self.base_dir.glob("*.json")}
        names.update(self._inline_registry.keys())
        return sorted(names)

    def revision(self, name: str) -> str:
        if name in self._inline_registry:
            return self._inline_registry[name][1]
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            raise RulesetError(f"Ruleset '{name}' not found")
        stub_bytes = path.read_bytes()
        # If the stub points to a remote ruleset, fold the remote content's hash
        # into the revision so the renderer's tile cache invalidates when the
        # upstream changes — without hammering the network if the lookup fails.
        remote = _extract_remote_pointer(stub_bytes)
        if remote:
            try:
                _, content_hash = self._load_remote(remote)
                seed = stub_bytes + b"|" + content_hash.encode("ascii")
            except RulesetError:
                seed = stub_bytes
            return hashlib.sha256(seed).hexdigest()[:16]
        return hashlib.sha256(stub_bytes).hexdigest()[:16]

    def load(self, name: str) -> dict[str, Any]:
        if name in self._inline_registry:
            data = json.loads(json.dumps(self._inline_registry[name][0]))
            data = self._normalize(data)
            self.validate(data)
            return data
        path = self.base_dir / f"{name}.json"
        if not path.exists():
            raise RulesetError(f"Ruleset '{name}' not found")
        raw_bytes = path.read_bytes()
        remote = _extract_remote_pointer(raw_bytes)
        if remote:
            data, _ = self._load_remote(remote)
            # Defensive deep copy so callers can't mutate the in-memory cache.
            data = json.loads(json.dumps(data))
        else:
            data = json.loads(raw_bytes.decode("utf-8"))
        data = self._normalize(data)
        self.validate(data)
        return data

    def _load_remote(self, ref: str) -> tuple[dict[str, Any], str]:
        """Resolve a `$remote` ruleset pointer, cache it on disk + in memory.

        Accepts http://, https://, and github://<owner>/<repo>[@<ref>]/<path>.
        Returns `(payload_dict, content_hash_hex)`.
        """
        if ref in self._remote_cache:
            return self._remote_cache[ref]
        if ref.startswith("github://"):
            from .uris import expand_github_uri
            url = expand_github_uri(ref)
        elif ref.startswith("http://") or ref.startswith("https://"):
            url = ref
        else:
            raise RulesetError(f"Unsupported $remote scheme: {ref}")
        if not self.cache_dir:
            raise RulesetError(
                f"Remote ruleset requested ({ref}) but RulesetStore has no cache_dir; "
                "configure one in GeoRenderer to enable remote rulesets."
            )
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        cache_path = self.cache_dir / "rulesets" / f"{digest}.json"
        if cache_path.exists():
            payload = cache_path.read_bytes()
        else:
            import httpx

            try:
                response = httpx.get(url, timeout=30.0, follow_redirects=True)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise RulesetError(f"Failed to fetch remote ruleset {ref}: {exc}") from exc
            payload = response.content
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(payload)
        try:
            data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise RulesetError(f"Remote ruleset {ref} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RulesetError(f"Remote ruleset {ref} must be a JSON object")
        result = (data, hashlib.sha256(payload).hexdigest()[:16])
        self._remote_cache[ref] = result
        return result

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
            # Viewport-wide symbolizers (e.g. `wms`) don't filter on features,
            # so the geometry whitelist is optional. Per-feature symbolizers
            # still require it.
            if symbolizer_type not in VIEWPORT_SYMBOLIZERS:
                geoms = rule.get("geometry", [])
                if not geoms or not all(g in SUPPORTED_GEOMS for g in geoms):
                    raise RulesetError(f"Rule #{idx} has invalid geometry types: {geoms}")
            else:
                geoms = rule.get("geometry") or []
                if geoms and not all(g in SUPPORTED_GEOMS for g in geoms):
                    raise RulesetError(f"Rule #{idx} has invalid geometry types: {geoms}")
            edge_fade = rule.get("edge_fade")
            if edge_fade is not None:
                distance = edge_fade.get("distance_px", 0)
                if not isinstance(distance, (int, float)) or distance < 0:
                    raise RulesetError(
                        f"Rule #{idx} edge_fade.distance_px must be >= 0"
                    )


def _extract_remote_pointer(raw_bytes: bytes) -> str | None:
    """If `raw_bytes` is a JSON object with a `$remote` field, return that URL.

    Used by RulesetStore to detect remote stubs without committing to parsing
    the whole file twice. Returns None on any parse failure so callers can
    keep treating the file as a plain (local) ruleset.
    """
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    remote = data.get("$remote")
    if isinstance(remote, str) and remote.strip():
        return remote.strip()
    return None


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
