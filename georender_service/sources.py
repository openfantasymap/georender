from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform as shapely_transform

from .geometry import (
    ensure_mercator,
    load_geom,
    mercator_tile_bounds,
    tile_range_for_bounds,
)


class SourceError(ValueError):
    pass


@dataclass(slots=True)
class SourceDefinition:
    slug: str
    data: dict[str, Any]
    path: Path

    @property
    def mode(self) -> str:
        return str(self.data.get("mode", "geojson")).lower()

    @property
    def revision(self) -> str:
        explicit = self.data.get("revision")
        if explicit:
            return str(explicit)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()[:16]
        return digest


@dataclass(slots=True)
class FetchedFeatures:
    features: list[dict[str, Any]]
    source_crs: str
    revision: str


class SourceStore:
    def __init__(
        self,
        base_dir: str | Path,
        connections_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ):
        self.base_dir = Path(base_dir)
        self.connections_path = Path(connections_path) if connections_path else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._sources: dict[str, SourceDefinition] | None = None
        self._connections_cache: dict[str, Any] | None = None

    def list_names(self) -> list[str]:
        return sorted(self._load_sources().keys())

    def list_sources(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for slug, source in sorted(self._load_sources().items()):
            data = source.data
            items.append(
                {
                    "slug": slug,
                    "name": data.get("name", slug),
                    "url": data.get("url", f"/{slug}"),
                    "mode": data.get("mode", "geojson"),
                    "date": data.get("date"),
                    "tags": data.get("tags", []),
                    "base": data.get("base"),
                    "revision": source.revision,
                }
            )
        return items

    def get(self, slug: str) -> SourceDefinition:
        source = self._load_sources().get(slug)
        if source is None:
            raise SourceError(f"Map '{slug}' not found")
        return source

    def fetch_for_bounds(
        self,
        slug: str,
        bounds_3857: tuple[float, float, float, float],
        *,
        tile: tuple[int, int, int] | None = None,
    ) -> FetchedFeatures:
        source = self.get(slug)
        adapter = self._get_adapter(source)
        return adapter.fetch_for_bounds(source, bounds_3857, tile=tile)

    def _get_adapter(self, source: SourceDefinition):
        mode = source.mode
        if mode == "geojson":
            return GeoJSONAdapter()
        if mode == "postgis":
            return PostGISAdapter(self._load_connections())
        if mode == "mvt":
            return MVTAdapter()
        if mode == "geocontext":
            return GeocontextAdapter(cache_dir=self.cache_dir)
        raise SourceError(f"Unsupported source mode: {mode}")

    def _load_sources(self) -> dict[str, SourceDefinition]:
        if self._sources is not None:
            return self._sources
        sources: dict[str, SourceDefinition] = {}
        if not self.base_dir.exists():
            self._sources = {}
            return self._sources

        for path in sorted(self.base_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            slug = _slug_for_source(data, path)
            sources[slug] = SourceDefinition(slug=slug, data=data, path=path)

        for path in sorted(self.base_dir.glob("*/timeline.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            slug = _slug_for_source(data, path)
            sources[slug] = SourceDefinition(slug=slug, data=data, path=path)

        self._sources = sources
        return self._sources

    def _load_connections(self) -> dict[str, Any]:
        if self._connections_cache is not None:
            return self._connections_cache
        if not self.connections_path or not self.connections_path.exists():
            self._connections_cache = {}
            return self._connections_cache
        self._connections_cache = json.loads(self.connections_path.read_text(encoding="utf-8"))
        return self._connections_cache


class GeoJSONAdapter:
    def fetch_for_bounds(
        self,
        source: SourceDefinition,
        bounds_3857: tuple[float, float, float, float],
        *,
        tile: tuple[int, int, int] | None = None,
    ) -> FetchedFeatures:
        data = source.data
        path = _resolve_geojson_path(source)
        if not path.exists():
            raise SourceError(f"GeoJSON file not found for map '{source.slug}': {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = list(payload.get("features", []))
        source_crs = str(data.get("source_crs") or payload.get("crs", {}).get("properties", {}).get("name") or "EPSG:4326")
        if bounds_3857:
            query_box = box(*bounds_3857)
            filtered: list[dict[str, Any]] = []
            for feature in features:
                geom = ensure_mercator(load_geom(feature), source_crs)
                if geom.is_empty or not geom.intersects(query_box):
                    continue
                filtered.append(feature)
            features = filtered
        revision = hashlib.sha256((source.revision + str(path.stat().st_mtime_ns) + str(path.stat().st_size)).encode("utf-8")).hexdigest()[:16]
        return FetchedFeatures(features=features, source_crs=source_crs, revision=revision)


class PostGISAdapter:
    def __init__(self, connections: dict[str, Any]):
        self.connections = connections

    def fetch_for_bounds(
        self,
        source: SourceDefinition,
        bounds_3857: tuple[float, float, float, float],
        *,
        tile: tuple[int, int, int] | None = None,
    ) -> FetchedFeatures:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg import sql
        except Exception as exc:
            raise SourceError(
                "PostGIS mode requires psycopg>=3. Install dependencies from requirements.txt."
            ) from exc

        connection_alias = ((source.data.get("connection") or {}).get("db"))
        if not connection_alias:
            raise SourceError(f"Map '{source.slug}' is in postgis mode but connection.db is missing")
        dsn = _resolve_connection_dsn(self.connections, connection_alias)
        if not dsn:
            raise SourceError(
                f"No DSN configured for connection alias '{connection_alias}'. Add it to connections.json"
            )

        tables = _collect_tables(source.data)
        minx, miny, maxx, maxy = bounds_3857
        features: list[dict[str, Any]] = []
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                for table_def in tables:
                    table_name = table_def["name"]
                    geom_col = table_def["geometry_column"]
                    query = sql.SQL(
                        """
                        SELECT *, ST_AsGeoJSON(ST_Transform({geom_col}, 3857)) AS __geometry__
                        FROM {table_name}
                        WHERE {geom_col} IS NOT NULL
                          AND ST_Intersects(
                                ST_Transform({geom_col}, 3857),
                                ST_MakeEnvelope(%s, %s, %s, %s, 3857)
                              )
                        """
                    ).format(
                        geom_col=sql.Identifier(geom_col),
                        table_name=sql.Identifier(table_name),
                    )
                    cur.execute(query, (minx, miny, maxx, maxy))
                    for row in cur.fetchall():
                        geometry_json = row.pop("__geometry__", None)
                        if not geometry_json:
                            continue
                        row.pop(geom_col, None)
                        row["__source_layer"] = table_name
                        features.append(
                            {
                                "type": "Feature",
                                "geometry": json.loads(geometry_json),
                                "properties": row,
                            }
                        )
        revision_seed = json.dumps(source.data, sort_keys=True)
        revision = hashlib.sha256(revision_seed.encode("utf-8")).hexdigest()[:16]
        return FetchedFeatures(features=features, source_crs="EPSG:3857", revision=revision)


class MVTAdapter:
    def fetch_for_bounds(
        self,
        source: SourceDefinition,
        bounds_3857: tuple[float, float, float, float],
        *,
        tile: tuple[int, int, int] | None = None,
    ) -> FetchedFeatures:
        try:
            import mapbox_vector_tile  # noqa: F401
        except Exception as exc:
            raise SourceError(
                "MVT mode requires mapbox-vector-tile. Install dependencies from requirements.txt."
            ) from exc

        from mapbox_vector_tile import decode

        tile_url_template = (
            source.data.get("tile_url_template")
            or (source.data.get("connection") or {}).get("tile_url_template")
            or (source.data.get("connection") or {}).get("url")
        )
        if not tile_url_template:
            raise SourceError(
                f"Map '{source.slug}' is in mvt mode but no tile_url_template was configured"
            )

        layer_whitelist = set(source.data.get("relatedLayers") or [])
        if source.data.get("events"):
            layer_whitelist.add(str(source.data["events"]))

        if tile is not None:
            tiles = [tile]
        else:
            zoom = int(round(float((source.data.get("base") or {}).get("zoom", 8))))
            tiles = tile_range_for_bounds(bounds_3857, zoom)

        features: list[dict[str, Any]] = []
        seen_tiles: set[tuple[int, int, int]] = set()
        for z, x, y in tiles:
            if (z, x, y) in seen_tiles:
                continue
            seen_tiles.add((z, x, y))
            url = tile_url_template.format(z=z, x=x, y=y)
            response = httpx.get(url, timeout=20.0)
            response.raise_for_status()
            decoded = decode(response.content)
            tile_bounds = mercator_tile_bounds(x, y, z)
            for layer_name, layer_payload in decoded.items():
                if layer_whitelist and layer_name not in layer_whitelist:
                    continue
                extent = float(layer_payload.get("extent", 4096))
                layer_features = layer_payload.get("features", [])
                for feature in layer_features:
                    geometry = _mvt_geometry_to_geojson(feature.get("geometry"), tile_bounds, extent)
                    if geometry is None:
                        continue
                    props = dict(feature.get("properties") or {})
                    props["__source_layer"] = layer_name
                    features.append({"type": "Feature", "geometry": geometry, "properties": props})
        revision_seed = json.dumps(source.data, sort_keys=True)
        revision = hashlib.sha256(revision_seed.encode("utf-8")).hexdigest()[:16]
        return FetchedFeatures(features=features, source_crs="EPSG:3857", revision=revision)


class GeocontextAdapter:
    """Adapter for the geocontext.json format (https://github.com/openhistorymap/geocontext-front).

    Fetches a manifest from `cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/`, resolves the
    declared `datasources[]` (inline / remote GeoJSON / CSV / derived transforms), and
    returns the union of features that belong to data-driven layers, tagged with
    `__layer` and `__source_layer` properties so rulesets can filter on them.
    """

    JSDELIVR_BASE = "https://cdn.jsdelivr.net/gh"
    DATA_LAYER_TYPES = {"features", "feature", "markers"}
    DEFAULT_MANIFEST_CANDIDATES = ("geocontext.json", "gcx.json")
    DEFAULT_BUNDLE_NAME = "georender.json"

    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def fetch_for_bounds(
        self,
        source: SourceDefinition,
        bounds_3857: tuple[float, float, float, float],
        *,
        tile: tuple[int, int, int] | None = None,
    ) -> FetchedFeatures:
        cfg = _extract_geocontext_config(source)
        owner = cfg["owner"]
        repo = cfg["repo"]
        ref = cfg.get("ref") or "HEAD"
        manifest_name = cfg.get("manifest")
        whitelist = set(cfg.get("layers") or [])

        resolved_ref = self._resolve_ref(owner, repo, ref) or ref

        manifest, manifest_name_used = self._load_manifest(owner, repo, resolved_ref, manifest_name)
        datasources = self._resolve_datasources(owner, repo, resolved_ref, manifest)

        features: list[dict[str, Any]] = []
        for layer in manifest.get("layers") or []:
            layer_type = str(layer.get("type") or "").lower()
            if layer_type not in self.DATA_LAYER_TYPES:
                continue
            layer_name = str(layer.get("name") or "")
            ds_name = str(layer.get("datasource") or "")
            if not ds_name or ds_name not in datasources:
                continue
            if whitelist and layer_name not in whitelist:
                continue
            for feat in datasources[ds_name]:
                clone = dict(feat)
                props = dict(clone.get("properties") or {})
                props["__layer"] = layer_name
                props["__source_layer"] = ds_name
                clone["properties"] = props
                features.append(clone)

        if bounds_3857:
            query_box = box(*bounds_3857)
            filtered: list[dict[str, Any]] = []
            for feat in features:
                geom = ensure_mercator(load_geom(feat), "EPSG:4326")
                if geom.is_empty or not geom.intersects(query_box):
                    continue
                filtered.append(feat)
            features = filtered

        revision_seed = f"{owner}/{repo}@{resolved_ref}:{manifest_name_used}"
        revision = hashlib.sha256(revision_seed.encode("utf-8")).hexdigest()[:16]
        return FetchedFeatures(features=features, source_crs="EPSG:4326", revision=revision)

    def fetch_render_config(self, source: SourceDefinition) -> dict[str, Any] | None:
        """Fetch a repo's `georender.json` bundle, if present.

        Schema (matching the in-repo convention):

            {
              "type": "GeoRender",
              "version": "1.0",
              "assets":   "assets/default.json"       // path, OR inline {coll: {...}}
              "rulesets": {"default": "ruleset.json"} // name -> path, OR inline object
              "bbox":     [W, S, E, N],               // EPSG:4326, optional
              "output":   {"width": ..., "height": ..., "padding_px": ...},
              "geocontext": {"manifest": "...", "layers": [...]}
            }

        The returned dict has all bare-relative `file` paths inside any inline
        `assets` block rewritten to absolute `github://<owner>/<repo>@<ref>/...`
        URIs, and a synthetic `_repo` field carries the resolved repo + ref so
        downstream loaders can rewrite string-shaped pointers themselves.

        Returns None if the bundle is absent (any HTTP error during fetch).
        """
        cfg = _extract_geocontext_config(source)
        owner = cfg["owner"]
        repo = cfg["repo"]
        ref = cfg.get("ref") or "HEAD"
        resolved_ref = self._resolve_ref(owner, repo, ref) or ref
        try:
            payload = self._fetch_repo_asset(owner, repo, resolved_ref, self.DEFAULT_BUNDLE_NAME)
        except httpx.HTTPError:
            return None
        try:
            data = _loose_json_loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(
                f"{self.DEFAULT_BUNDLE_NAME} for {owner}/{repo}@{resolved_ref} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise SourceError(
                f"{self.DEFAULT_BUNDLE_NAME} for {owner}/{repo}@{resolved_ref} must be a JSON object"
            )
        data["_repo"] = {"owner": owner, "repo": repo, "ref": resolved_ref}
        if isinstance(data.get("assets"), dict):
            data["assets"] = _rewrite_files_to_github(data["assets"], owner, repo, resolved_ref)
        return data

    # ------------------------------------------------------------------
    # HTTP / cache helpers
    # ------------------------------------------------------------------

    def _resolve_ref(self, owner: str, repo: str, ref: str) -> str | None:
        """Try to resolve a branch/HEAD to a short commit SHA via the GitHub API.

        Returns None on any failure — callers fall back to the literal ref.
        """
        try:
            url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
            response = httpx.get(
                url,
                timeout=10.0,
                follow_redirects=True,
                headers={"Accept": "application/vnd.github+json"},
            )
            if response.status_code != 200:
                return None
            sha = response.json().get("sha")
            return sha[:12] if sha else None
        except Exception:
            return None

    def _fetch_repo_asset(self, owner: str, repo: str, ref: str, path: str) -> bytes:
        cache = self._cache_path(owner, repo, ref, path)
        if cache is not None and cache.exists():
            return cache.read_bytes()
        url = f"{self.JSDELIVR_BASE}/{owner}/{repo}@{ref}/{path.lstrip('/')}"
        data = self._http_get_bytes(url)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(data)
        return data

    def _fetch_asset_path(self, owner: str, repo: str, ref: str, path: str) -> bytes:
        """Resolve a datasource/media path per FORMAT.md §9 and fetch its bytes."""
        if not path:
            raise SourceError("geocontext datasource path is empty")
        if path.startswith("http://") or path.startswith("https://"):
            return self._http_get_bytes(path)
        if path.startswith("//"):
            return self._http_get_bytes("https:" + path)
        if path.startswith("/"):
            parts = path.lstrip("/").split("/")
            # Cross-repo form: /<otherUser>/<otherProject>[@<ref>]/assets/...
            if len(parts) >= 3 and parts[2] == "assets":
                other_owner = parts[0]
                other_repo_ref = parts[1]
                if "@" in other_repo_ref:
                    other_repo, other_ref = other_repo_ref.split("@", 1)
                else:
                    other_repo, other_ref = other_repo_ref, "HEAD"
                other_path = "/".join(parts[2:])
                return self._fetch_repo_asset(other_owner, other_repo, other_ref, other_path)
            return self._fetch_repo_asset(owner, repo, ref, "/".join(parts))
        return self._fetch_repo_asset(owner, repo, ref, path)

    def _http_get_bytes(self, url: str) -> bytes:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        return response.content

    def _cache_path(self, owner: str, repo: str, ref: str, rel: str) -> Path | None:
        if not self.cache_dir:
            return None
        safe_rel = rel.replace("..", "").lstrip("/")
        if not safe_rel:
            return None
        return self.cache_dir / "geocontext" / owner / repo / ref / safe_rel

    # ------------------------------------------------------------------
    # Manifest + datasource resolution
    # ------------------------------------------------------------------

    def _load_manifest(
        self,
        owner: str,
        repo: str,
        ref: str,
        manifest_name: str | None,
    ) -> tuple[dict[str, Any], str]:
        # When the user pinned an explicit name we surface any error; for the default
        # probe sequence we keep trying the next candidate on any HTTP failure (jsDelivr
        # returns 404 for missing assets but 502 once that 404 has been edge-cached).
        if manifest_name:
            candidates: tuple[str, ...] = (manifest_name,)
            tolerant = False
        else:
            candidates = self.DEFAULT_MANIFEST_CANDIDATES
            tolerant = True

        last_error: Exception | None = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                data = self._fetch_repo_asset(owner, repo, ref, candidate)
                return json.loads(data.decode("utf-8")), candidate
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if tolerant:
                    continue
                if exc.response is not None and exc.response.status_code == 404:
                    continue
                raise SourceError(f"Failed to fetch geocontext manifest '{candidate}': {exc}") from exc
            except httpx.HTTPError as exc:
                last_error = exc
                if tolerant:
                    continue
                raise SourceError(f"Failed to fetch geocontext manifest '{candidate}': {exc}") from exc
            except json.JSONDecodeError as exc:
                raise SourceError(
                    f"geocontext manifest '{candidate}' is not valid JSON: {exc}"
                ) from exc
        raise SourceError(
            f"No geocontext manifest found for {owner}/{repo}@{ref} "
            f"(tried {', '.join(c for c in candidates if c)})"
        ) from last_error

    def _resolve_datasources(
        self,
        owner: str,
        repo: str,
        ref: str,
        manifest: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        declared = [ds for ds in (manifest.get("datasources") or []) if ds.get("name")]
        resolved: dict[str, list[dict[str, Any]]] = {}
        pending = list(declared)
        # Iterate in dependency-order waves; bound by the number of declared datasources
        # to guarantee termination even on circular transform.from references.
        for _ in range(len(declared) + 1):
            if not pending:
                break
            still: list[dict[str, Any]] = []
            progressed = False
            for ds in pending:
                name = str(ds["name"])
                ds_type = str(ds.get("type") or "").lower()
                conf = ds.get("conf") or {}
                try:
                    if ds_type == "geojson":
                        fc = conf.get("data") or {}
                        resolved[name] = list(fc.get("features") or [])
                        progressed = True
                    elif ds_type == "geojson+http+remote":
                        payload = self._fetch_asset_path(owner, repo, ref, conf.get("source") or "")
                        fc = json.loads(payload.decode("utf-8"))
                        resolved[name] = list(fc.get("features") or [])
                        progressed = True
                    elif ds_type == "csv":
                        resolved[name] = _csv_to_features(
                            conf.get("data") or "", conf.get("structure") or []
                        )
                        progressed = True
                    elif ds_type == "csv+http+remote":
                        payload = self._fetch_asset_path(owner, repo, ref, conf.get("source") or "")
                        resolved[name] = _csv_to_features(
                            payload.decode("utf-8"), conf.get("structure") or []
                        )
                        progressed = True
                    elif ds_type == "transform":
                        parent = str(conf.get("from") or "")
                        if parent not in resolved:
                            still.append(ds)
                            continue
                        resolved[name] = _apply_transforms(
                            resolved[parent], list(conf.get("transforms") or [])
                        )
                        progressed = True
                    else:
                        # Per FORMAT.md, unknown datasource types are skipped silently.
                        resolved[name] = []
                        progressed = True
                except SourceError:
                    raise
                except Exception:
                    # A broken individual datasource should not invalidate the whole map.
                    resolved[name] = []
                    progressed = True
            pending = still
            if not progressed:
                # Circular or unresolved transforms — drop them.
                break
        return resolved


def _slug_for_source(data: dict[str, Any], path: Path) -> str:
    default_slug = path.parent.name if path.name == "timeline.json" else path.stem
    raw = str(data.get("url") or default_slug)
    raw = raw.strip("/")
    return raw or path.stem


def _resolve_geojson_path(source: SourceDefinition) -> Path:
    data = source.data
    candidates = [
        data.get("geojson"),
        data.get("file"),
        data.get("path"),
        (data.get("connection") or {}).get("file"),
        (data.get("connection") or {}).get("path"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate_path = (source.path.parent / candidate_path).resolve()
        return candidate_path
    raise SourceError(
        f"Map '{source.slug}' is in geojson mode but no geojson/file/path was configured"
    )


def _resolve_connection_dsn(connections: dict[str, Any], alias: str) -> str | None:
    value = connections.get(alias)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("dsn"):
            return str(value["dsn"])
        if value.get("url"):
            return str(value["url"])
        keys = {"dbname": value.get("dbname"), "user": value.get("user"), "password": value.get("password"), "host": value.get("host"), "port": value.get("port")}
        return " ".join(f"{k}={v}" for k, v in keys.items() if v is not None)
    return None


def _collect_tables(data: dict[str, Any]) -> list[dict[str, str]]:
    default_geom = str(data.get("geometry_column", "geom"))
    names: list[str] = []
    if data.get("events"):
        names.append(str(data["events"]))
    for layer in data.get("relatedLayers") or []:
        if layer not in names:
            names.append(str(layer))
    if (data.get("tracks") or {}).get("table") and data["tracks"]["table"] not in names:
        names.append(str(data["tracks"]["table"]))
    return [{"name": name, "geometry_column": default_geom} for name in names]


_METERS_PER_UNIT = {
    "meters": 1.0,
    "metres": 1.0,
    "meter": 1.0,
    "metre": 1.0,
    "kilometers": 1000.0,
    "kilometres": 1000.0,
    "km": 1000.0,
    "miles": 1609.344,
    "mile": 1609.344,
    "feet": 0.3048,
    "ft": 0.3048,
}


def _loose_json_loads(text: str) -> Any:
    """Parse JSON tolerating // and /* */ comments and trailing commas.

    Used for `georender.json` only — every other config in the repo (rulesets,
    timeline.json, geocontext manifests) stays strict JSON. This loose mode
    exists because `georender.json` is hand-edited config that benefits from
    inline comments, and the in-repo convention already uses them.
    """
    return json.loads(_strip_trailing_commas(_strip_json_comments(text)))


def _strip_json_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            # Consume a string literal verbatim (including escaped quotes) so
            # that `//` and `/*` inside URLs don't get clobbered.
            out.append(ch)
            i += 1
            while i < n:
                out.append(text[i])
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2  # consume the closing */
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    import re

    return re.sub(r",(\s*[\]}])", r"\1", text)


def _rewrite_files_to_github(
    obj: Any,
    owner: str,
    repo: str,
    ref: str,
) -> Any:
    """Walk `obj` and rewrite every `"file": "..."` value that's a bare-relative
    path (no `://` scheme, no leading slash, no `github://` prefix) into the
    explicit `github://<owner>/<repo>@<ref>/<path>` form.

    Lets a `georender.json` author write `"file": "backgrounds/photo.jpg"` and
    have it resolve to a real CDN URL the AssetStore can fetch.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k == "file" and isinstance(v, str):
                out[k] = _maybe_rewrite_path(v, owner, repo, ref)
            else:
                out[k] = _rewrite_files_to_github(v, owner, repo, ref)
        return out
    if isinstance(obj, list):
        return [_rewrite_files_to_github(item, owner, repo, ref) for item in obj]
    return obj


def _maybe_rewrite_path(value: str, owner: str, repo: str, ref: str) -> str:
    if not value:
        return value
    if value.startswith(("http://", "https://", "github://")):
        return value
    if value.startswith("//"):
        return "https:" + value
    return f"github://{owner}/{repo}@{ref}/{value.lstrip('/')}"


def _extract_geocontext_config(source: SourceDefinition) -> dict[str, Any]:
    data = source.data
    connection = data.get("connection") or {}
    owner = data.get("owner") or connection.get("owner")
    repo = data.get("repo") or connection.get("repo")
    combined = data.get("repository") or connection.get("repository")
    if combined and not (owner and repo) and "/" in str(combined):
        owner, _, repo = str(combined).partition("/")
    if not owner or not repo:
        raise SourceError(
            f"Map '{source.slug}' is in geocontext mode but owner/repo are missing "
            f"(set top-level 'owner' and 'repo', or 'repository': '<owner>/<repo>')"
        )
    return {
        "owner": str(owner),
        "repo": str(repo),
        "ref": data.get("ref") or connection.get("ref"),
        "manifest": data.get("manifest") or connection.get("manifest"),
        "layers": data.get("layers") or data.get("relatedLayers"),
    }


def _csv_to_features(csv_text: str, structure: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not csv_text.strip():
        return []
    lat_col: str | None = None
    lon_col: str | None = None
    type_hints: dict[str, str] = {}
    for col in structure or []:
        name = col.get("column")
        if not name:
            continue
        tags = col.get("tags") or []
        if col.get("type"):
            type_hints[name] = str(col["type"])
        if "gcx:lat" in tags:
            lat_col = name
        if "gcx:lon" in tags:
            lon_col = name
    if not lat_col or not lon_col:
        # FORMAT.md mandates at least one gcx:lat + one gcx:lon column.
        return []
    reader = csv.DictReader(io.StringIO(csv_text))
    features: list[dict[str, Any]] = []
    for row in reader:
        try:
            lat = float(row.get(lat_col, "") or "")
            lon = float(row.get(lon_col, "") or "")
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        props: dict[str, Any] = dict(row)
        for key, hint in type_hints.items():
            if hint == "number" and key in props:
                try:
                    props[key] = float(props[key])
                except (TypeError, ValueError):
                    pass
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return features


def _apply_transforms(
    features: list[dict[str, Any]],
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [f for f in features if f.get("geometry")]
    for step in steps or []:
        step_type = str(step.get("type") or "").lower()
        try:
            if step_type == "buffer":
                out = _buffer_features(out, step)
            # Unknown step types are skipped silently (FORMAT.md §3).
        except Exception:
            # A broken step yields the previous step's output unchanged.
            continue
    return out


def _buffer_features(
    features: list[dict[str, Any]],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        radius = float(params.get("radius") or 0)
    except (TypeError, ValueError):
        return features
    if radius == 0:
        return features
    units = str(params.get("units") or "meters").lower()
    try:
        steps = int(params.get("steps") or 8)
    except (TypeError, ValueError):
        steps = 8

    meters = _METERS_PER_UNIT.get(units)
    to_mercator = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
    to_lonlat = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform

    buffered: list[dict[str, Any]] = []
    for feat in features:
        try:
            geom = load_geom(feat)
            if geom.is_empty:
                continue
            if meters is not None:
                merc = shapely_transform(to_mercator, geom)
                merc_buf = merc.buffer(radius * meters, quad_segs=steps)
                if merc_buf.is_empty:
                    continue
                out_geom = shapely_transform(to_lonlat, merc_buf)
            else:
                size = math.degrees(radius) if units == "radians" else radius
                out_geom = geom.buffer(size, quad_segs=steps)
                if out_geom.is_empty:
                    continue
            clone = dict(feat)
            clone["geometry"] = mapping(out_geom)
            buffered.append(clone)
        except Exception:
            continue
    return buffered


def _mvt_geometry_to_geojson(
    geometry: Any,
    tile_bounds: tuple[float, float, float, float],
    extent: float,
) -> dict[str, Any] | None:
    if not geometry:
        return None
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if not geom_type or coords is None:
        return None

    minx, miny, maxx, maxy = tile_bounds
    span_x = maxx - minx
    span_y = maxy - miny

    def project(pt: list[float] | tuple[float, float]) -> list[float]:
        x = minx + (float(pt[0]) / extent) * span_x
        y = maxy - (float(pt[1]) / extent) * span_y
        return [x, y]

    def transform_coords(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        if value and isinstance(value[0], (int, float)):
            return project(value)
        return [transform_coords(v) for v in value]

    return {"type": geom_type, "coordinates": transform_coords(coords)}
