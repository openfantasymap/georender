from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from shapely.geometry import box

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
    def __init__(self, base_dir: str | Path, connections_path: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.connections_path = Path(connections_path) if connections_path else None
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
