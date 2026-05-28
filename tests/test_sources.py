from __future__ import annotations

import json
from pathlib import Path

import pytest

from georender_service.geometry import WEB_MERCATOR_HALF, ensure_mercator
from georender_service.sources import (
    GeocontextAdapter,
    SourceError,
    SourceStore,
    _apply_transforms,
    _collect_tables,
    _csv_to_features,
    _extract_geocontext_config,
    _maybe_rewrite_path,
    _resolve_connection_dsn,
    _rewrite_files_to_github,
    _slug_for_source,
)

FULL_WORLD = (-WEB_MERCATOR_HALF, -WEB_MERCATOR_HALF, WEB_MERCATOR_HALF, WEB_MERCATOR_HALF)

# A small mercator bbox around northern Italy (covers the sample GeoJSON)
ITALY_BOUNDS = (
    ensure_mercator(__import__("shapely.geometry", fromlist=["box"]).box(9.0, 43.0, 12.0, 46.0), "EPSG:4326").bounds
)


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------


def test_store_discovers_flat_json(tmp_path):
    timeline = {"name": "Alpha", "url": "/alpha", "mode": "geojson", "file": "data.geojson"}
    (tmp_path / "alpha.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path)
    assert "alpha" in store.list_names()


def test_store_discovers_subdir_timeline(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    assert "testmap" in store.list_names()


def test_store_slug_from_url_field(tmp_path):
    timeline = {"name": "X", "url": "/myslug", "mode": "geojson"}
    (tmp_path / "something.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path)
    assert "myslug" in store.list_names()
    assert "something" not in store.list_names()


def test_store_slug_from_filename_fallback(tmp_path):
    timeline = {"name": "X", "mode": "geojson"}  # no url
    (tmp_path / "mymap.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path)
    assert "mymap" in store.list_names()


def test_store_empty_directory(tmp_path):
    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    store = SourceStore(maps_dir)
    assert store.list_names() == []


def test_store_missing_directory(tmp_path):
    store = SourceStore(tmp_path / "nonexistent")
    assert store.list_names() == []


def test_get_unknown_slug_raises(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    with pytest.raises(SourceError, match="not found"):
        store.get("does_not_exist")


# ---------------------------------------------------------------------------
# list_sources
# ---------------------------------------------------------------------------


def test_list_sources_has_expected_fields(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    sources = store.list_sources()
    assert len(sources) >= 1
    source = next(s for s in sources if s["slug"] == "testmap")
    assert "name" in source
    assert "mode" in source
    assert "slug" in source
    assert "revision" in source


# ---------------------------------------------------------------------------
# GeoJSONAdapter — fetch_for_bounds
# ---------------------------------------------------------------------------


def test_geojson_fetch_returns_features(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    fetched = store.fetch_for_bounds("testmap", FULL_WORLD)
    assert len(fetched.features) == 3  # polygon + line + point


def test_geojson_fetch_result_has_crs(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    fetched = store.fetch_for_bounds("testmap", FULL_WORLD)
    assert fetched.source_crs == "EPSG:4326"


def test_geojson_fetch_filters_by_bounds(tmp_maps_dir, tmp_path):
    # Add a second GeoJSON file with a feature far away (e.g. in Australia)
    aus_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"kind": "water"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[140, -30], [145, -30], [145, -35], [140, -35], [140, -30]]],
                },
            }
        ],
    }
    map_dir = tmp_maps_dir / "aus"
    map_dir.mkdir()
    (map_dir / "data.geojson").write_text(json.dumps(aus_geojson), encoding="utf-8")
    (map_dir / "timeline.json").write_text(
        json.dumps({"name": "Aus", "url": "/aus", "mode": "geojson", "geojson": "data.geojson"}),
        encoding="utf-8",
    )

    store = SourceStore(tmp_maps_dir)
    # Fetch with Italy bounds — Australia polygon should be excluded
    fetched = store.fetch_for_bounds("aus", ITALY_BOUNDS)
    assert fetched.features == []


def test_geojson_fetch_revision_is_string(tmp_maps_dir):
    store = SourceStore(tmp_maps_dir)
    fetched = store.fetch_for_bounds("testmap", FULL_WORLD)
    assert isinstance(fetched.revision, str) and len(fetched.revision) > 0


def test_geojson_missing_file_raises(tmp_path):
    map_dir = tmp_path / "maps" / "broken"
    map_dir.mkdir(parents=True)
    timeline = {"name": "Broken", "url": "/broken", "mode": "geojson", "geojson": "missing.geojson"}
    (map_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path / "maps")
    with pytest.raises(SourceError, match="not found"):
        store.fetch_for_bounds("broken", FULL_WORLD)


# ---------------------------------------------------------------------------
# PostGIS — error path (no connections.json)
# ---------------------------------------------------------------------------


def test_postgis_no_connections_raises(tmp_path):
    map_dir = tmp_path / "maps" / "pg"
    map_dir.mkdir(parents=True)
    timeline = {
        "name": "PG Map",
        "url": "/pg",
        "mode": "postgis",
        "connection": {"db": "mydb"},
        "events": "locations",
    }
    (map_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path / "maps")  # no connections.json
    with pytest.raises(SourceError, match="DSN"):
        store.fetch_for_bounds("pg", FULL_WORLD)


# ---------------------------------------------------------------------------
# MVT — error path (no tile_url_template)
# ---------------------------------------------------------------------------


def test_mvt_no_template_raises(tmp_path):
    map_dir = tmp_path / "maps" / "mvt"
    map_dir.mkdir(parents=True)
    timeline = {"name": "MVT Map", "url": "/mvt", "mode": "mvt"}
    (map_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path / "maps")
    with pytest.raises(SourceError, match="tile_url_template"):
        store.fetch_for_bounds("mvt", FULL_WORLD)


# ---------------------------------------------------------------------------
# SourceDefinition — revision
# ---------------------------------------------------------------------------


def test_source_explicit_revision_used(tmp_path):
    timeline = {"name": "X", "url": "/x", "mode": "geojson", "revision": "my-custom-rev-42"}
    (tmp_path / "x.json").write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path)
    source = store.get("x")
    assert source.revision == "my-custom-rev-42"


def test_source_revision_derived_from_file_when_absent(tmp_path):
    timeline = {"name": "X", "url": "/x", "mode": "geojson"}
    path = tmp_path / "x.json"
    path.write_text(json.dumps(timeline), encoding="utf-8")
    store = SourceStore(tmp_path)
    source = store.get("x")
    rev = source.revision
    assert isinstance(rev, str) and len(rev) == 16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_collect_tables_from_events_and_related_layers():
    data = {"events": "locations", "relatedLayers": ["roads", "rivers"]}
    tables = _collect_tables(data)
    names = [t["name"] for t in tables]
    assert names == ["locations", "roads", "rivers"]


def test_collect_tables_deduplicates():
    data = {"events": "locations", "relatedLayers": ["locations", "roads"]}
    tables = _collect_tables(data)
    names = [t["name"] for t in tables]
    assert names.count("locations") == 1


def test_collect_tables_default_geometry_column():
    data = {"events": "locs"}
    tables = _collect_tables(data)
    assert tables[0]["geometry_column"] == "geom"


def test_resolve_connection_dsn_string(tmp_path):
    dsn = _resolve_connection_dsn({"mydb": "postgresql://user:pass@host/db"}, "mydb")
    assert dsn == "postgresql://user:pass@host/db"


def test_resolve_connection_dsn_dict():
    dsn = _resolve_connection_dsn({"mydb": {"dsn": "postgresql://user:pass@host/db"}}, "mydb")
    assert dsn == "postgresql://user:pass@host/db"


def test_resolve_connection_dsn_missing_returns_none():
    assert _resolve_connection_dsn({}, "missing") is None


def test_slug_for_source_strips_leading_slash():
    from pathlib import Path

    data = {"url": "/my-world"}
    slug = _slug_for_source(data, Path("/maps/x.json"))
    assert slug == "my-world"


# ---------------------------------------------------------------------------
# Geocontext — config extraction
# ---------------------------------------------------------------------------


def _make_source(tmp_path, data):
    """Build a SourceDefinition-like object backed by a real file on disk."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    store = SourceStore(tmp_path)
    return store.get(_slug_for_source(data, path))


def test_geocontext_config_requires_owner_repo(tmp_path):
    src = _make_source(tmp_path, {"name": "X", "url": "/x", "mode": "geocontext"})
    with pytest.raises(SourceError, match="owner/repo"):
        _extract_geocontext_config(src)


def test_geocontext_config_supports_repository_shorthand(tmp_path):
    src = _make_source(
        tmp_path,
        {"name": "X", "url": "/x", "mode": "geocontext", "repository": "alice/world"},
    )
    cfg = _extract_geocontext_config(src)
    assert cfg["owner"] == "alice"
    assert cfg["repo"] == "world"


def test_geocontext_config_picks_up_layers_whitelist(tmp_path):
    src = _make_source(
        tmp_path,
        {
            "name": "X",
            "url": "/x",
            "mode": "geocontext",
            "owner": "alice",
            "repo": "world",
            "layers": ["graves"],
        },
    )
    cfg = _extract_geocontext_config(src)
    assert cfg["layers"] == ["graves"]


def test_geocontext_unsupported_mode_in_dispatch_raises(tmp_path):
    # Sanity check that the dispatch knows about the mode.
    src = _make_source(
        tmp_path,
        {"name": "X", "url": "/x", "mode": "geocontext", "owner": "a", "repo": "b"},
    )
    store = SourceStore(tmp_path)
    adapter = store._get_adapter(src)
    assert isinstance(adapter, GeocontextAdapter)


# ---------------------------------------------------------------------------
# Geocontext — CSV → features
# ---------------------------------------------------------------------------


def test_csv_to_features_basic_points():
    text = "lat,lon,name\n44.5,11.3,Alpha\n44.6,11.4,Beta\n"
    structure = [
        {"column": "lon", "type": "number", "tags": ["gcx:lon"]},
        {"column": "lat", "type": "number", "tags": ["gcx:lat"]},
        {"column": "name", "type": "string", "tags": ["gcx:title"]},
    ]
    features = _csv_to_features(text, structure)
    assert len(features) == 2
    assert features[0]["geometry"] == {"type": "Point", "coordinates": [11.3, 44.5]}
    assert features[0]["properties"]["name"] == "Alpha"
    assert features[0]["properties"]["lat"] == 44.5


def test_csv_to_features_skips_rows_with_unparseable_coords():
    text = "lat,lon\n44.5,11.3\nabc,xyz\n,11.4\n"
    structure = [
        {"column": "lon", "type": "number", "tags": ["gcx:lon"]},
        {"column": "lat", "type": "number", "tags": ["gcx:lat"]},
    ]
    features = _csv_to_features(text, structure)
    assert len(features) == 1


def test_csv_to_features_empty_without_lat_lon_tags():
    text = "x,y\n1,2\n"
    features = _csv_to_features(text, [{"column": "x"}, {"column": "y"}])
    assert features == []


def test_csv_to_features_empty_text():
    assert _csv_to_features("", []) == []


# ---------------------------------------------------------------------------
# Geocontext — transforms (buffer)
# ---------------------------------------------------------------------------


def test_buffer_transform_grows_point_to_polygon():
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Point", "coordinates": [11.0, 44.0]},
        }
    ]
    out = _apply_transforms(
        features, [{"type": "buffer", "radius": 100, "units": "meters"}]
    )
    assert len(out) == 1
    assert out[0]["geometry"]["type"] == "Polygon"
    # Buffered ring should bound the original point on all sides.
    ring = out[0]["geometry"]["coordinates"][0]
    xs = [pt[0] for pt in ring]
    ys = [pt[1] for pt in ring]
    assert min(xs) < 11.0 < max(xs)
    assert min(ys) < 44.0 < max(ys)


def test_buffer_transform_zero_radius_is_passthrough():
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        }
    ]
    out = _apply_transforms(features, [{"type": "buffer", "radius": 0}])
    assert out[0]["geometry"]["type"] == "Point"


def test_apply_transforms_unknown_step_is_noop():
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        }
    ]
    out = _apply_transforms(features, [{"type": "totally-unknown"}])
    assert out == features


def test_apply_transforms_drops_features_with_no_geometry():
    features = [
        {"type": "Feature", "properties": {}, "geometry": None},
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
    ]
    out = _apply_transforms(features, [])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Geocontext — full fetch flow (httpx monkeypatched)
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    def __init__(self, *, status_code: int, content: bytes = b"", json_body=None):
        self.status_code = status_code
        self.content = content
        self._json_body = json_body
        from httpx import Request

        self.request = Request("GET", "https://example.invalid/")

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )

    def json(self):
        if self._json_body is not None:
            return self._json_body
        return json.loads(self.content.decode("utf-8"))


def _install_fake_http(monkeypatch, routes):
    """Monkeypatch httpx.get to return canned responses based on URL prefix."""
    import georender_service.sources as src_mod

    def fake_get(url, *args, **kwargs):
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        return _FakeHttpResponse(status_code=404)

    monkeypatch.setattr(src_mod.httpx, "get", fake_get)


GC_MANIFEST = {
    "title": "Sample",
    "type": "2d",
    "center": [44.0, 11.0],
    "minzoom": 1,
    "startzoom": 5,
    "maxzoom": 18,
    "datasources": [
        {
            "name": "inline_points",
            "type": "geojson",
            "conf": {
                "data": {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {"name": "A"},
                            "geometry": {"type": "Point", "coordinates": [11.0, 44.0]},
                        }
                    ],
                }
            },
        },
        {
            "name": "remote_points",
            "type": "geojson+http+remote",
            "conf": {"source": "datasets/extra.geojson"},
        },
    ],
    "layers": [
        {"name": "primary", "type": "features", "datasource": "inline_points"},
        {"name": "extra", "type": "features", "datasource": "remote_points"},
        {"name": "basemap", "type": "osm-tiled"},  # non-data — must be ignored
    ],
}

GC_EXTRA_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"name": "B"},
            "geometry": {"type": "Point", "coordinates": [11.1, 44.1]},
        }
    ],
}


def test_geocontext_fetch_resolves_inline_and_remote(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "deadbeef" * 5}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@deadbeefdead/geocontext.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_MANIFEST).encode("utf-8")
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@deadbeefdead/datasets/extra.geojson": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_EXTRA_GEOJSON).encode("utf-8")
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {
            "name": "GC",
            "url": "/gc",
            "mode": "geocontext",
            "owner": "alice",
            "repo": "world",
        },
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    fetched = adapter.fetch_for_bounds(src, FULL_WORLD)
    layers = sorted({f["properties"]["__layer"] for f in fetched.features})
    assert layers == ["extra", "primary"]
    assert len(fetched.features) == 2
    assert fetched.source_crs == "EPSG:4326"
    assert isinstance(fetched.revision, str) and len(fetched.revision) == 16


def test_geocontext_falls_back_to_gcx_json(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "abc123abc123abc"}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@abc123abc123/geocontext.json": _FakeHttpResponse(
            status_code=404
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@abc123abc123/gcx.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_MANIFEST).encode("utf-8")
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@abc123abc123/datasets/extra.geojson": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_EXTRA_GEOJSON).encode("utf-8")
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    fetched = adapter.fetch_for_bounds(src, FULL_WORLD)
    assert len(fetched.features) == 2


def test_geocontext_layer_whitelist_restricts_output(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "feedface" * 5}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@feedfacefeed/geocontext.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_MANIFEST).encode("utf-8")
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@feedfacefeed/datasets/extra.geojson": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_EXTRA_GEOJSON).encode("utf-8")
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {
            "name": "GC",
            "url": "/gc",
            "mode": "geocontext",
            "owner": "alice",
            "repo": "world",
            "layers": ["primary"],
        },
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    fetched = adapter.fetch_for_bounds(src, FULL_WORLD)
    layer_names = {f["properties"]["__layer"] for f in fetched.features}
    assert layer_names == {"primary"}


def test_geocontext_caches_downloaded_assets(tmp_path, monkeypatch):
    calls: list[str] = []
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "0123456789abcdef"}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@0123456789ab/geocontext.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_MANIFEST).encode("utf-8")
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@0123456789ab/datasets/extra.geojson": _FakeHttpResponse(
            status_code=200, content=json.dumps(GC_EXTRA_GEOJSON).encode("utf-8")
        ),
    }
    import georender_service.sources as src_mod

    def counting_get(url, *args, **kwargs):
        calls.append(url)
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response
        return _FakeHttpResponse(status_code=404)

    monkeypatch.setattr(src_mod.httpx, "get", counting_get)

    cache_dir = tmp_path / "cache"
    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=cache_dir)
    adapter.fetch_for_bounds(src, FULL_WORLD)
    first_round = len(calls)
    adapter2 = GeocontextAdapter(cache_dir=cache_dir)
    adapter2.fetch_for_bounds(src, FULL_WORLD)
    # The second run should re-hit the GitHub commits API (ref resolution),
    # but the cached manifest + GeoJSON should not be re-downloaded from jsDelivr.
    second_round_jsdelivr = [
        u for u in calls[first_round:] if u.startswith("https://cdn.jsdelivr.net/")
    ]
    assert second_round_jsdelivr == []


def test_geocontext_missing_manifest_raises(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(status_code=404),
        "https://cdn.jsdelivr.net/gh/alice/world@HEAD/geocontext.json": _FakeHttpResponse(
            status_code=404
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@HEAD/gcx.json": _FakeHttpResponse(status_code=404),
    }
    _install_fake_http(monkeypatch, routes)
    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    with pytest.raises(SourceError, match="No geocontext manifest"):
        adapter.fetch_for_bounds(src, FULL_WORLD)


def test_geocontext_transform_datasource_runs_after_parent(tmp_path, monkeypatch):
    manifest = {
        "title": "T",
        "type": "2d",
        "center": [44.0, 11.0],
        "datasources": [
            {
                "name": "raw",
                "type": "geojson",
                "conf": {
                    "data": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"name": "A"},
                                "geometry": {"type": "Point", "coordinates": [11.0, 44.0]},
                            }
                        ],
                    }
                },
            },
            {
                "name": "buffered",
                "type": "transform",
                "conf": {
                    "from": "raw",
                    "transforms": [{"type": "buffer", "radius": 100, "units": "meters"}],
                },
            },
        ],
        "layers": [
            {"name": "rings", "type": "features", "datasource": "buffered"},
        ],
    }
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "cafebabecafebabe"}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@cafebabecafe/geocontext.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(manifest).encode("utf-8")
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    fetched = adapter.fetch_for_bounds(src, FULL_WORLD)
    assert len(fetched.features) == 1
    assert fetched.features[0]["geometry"]["type"] == "Polygon"
    assert fetched.features[0]["properties"]["__layer"] == "rings"


# ---------------------------------------------------------------------------
# Geocontext — georender.json bundle
# ---------------------------------------------------------------------------


def test_maybe_rewrite_path_passes_through_absolute():
    assert _maybe_rewrite_path("https://x/y.png", "o", "r", "main") == "https://x/y.png"
    assert _maybe_rewrite_path("github://o/r/x.png", "o", "r", "main") == "github://o/r/x.png"
    assert _maybe_rewrite_path("//cdn/y.png", "o", "r", "main") == "https://cdn/y.png"


def test_maybe_rewrite_path_anchors_relative_to_repo():
    out = _maybe_rewrite_path("backgrounds/aerial.jpg", "alice", "world", "abc123")
    assert out == "github://alice/world@abc123/backgrounds/aerial.jpg"


def test_maybe_rewrite_path_empty_is_empty():
    assert _maybe_rewrite_path("", "o", "r", "main") == ""


def test_rewrite_files_to_github_walks_nested():
    collections = {
        "vt": {
            "aerofoto": {"file": "backgrounds/rer.jpg", "kind": "sprite"},
            "ground": {
                "kind": "variant_set",
                "variants": [
                    {"file": "textures/a.png", "weight": 1},
                    {"file": "https://elsewhere/x.png", "weight": 1},
                ],
            },
        }
    }
    out = _rewrite_files_to_github(collections, "alice", "world", "abc")
    assert out["vt"]["aerofoto"]["file"] == "github://alice/world@abc/backgrounds/rer.jpg"
    assert out["vt"]["ground"]["variants"][0]["file"] == "github://alice/world@abc/textures/a.png"
    # Absolute URL passes through.
    assert out["vt"]["ground"]["variants"][1]["file"] == "https://elsewhere/x.png"


def test_geocontext_fetch_render_config_rewrites_assets(tmp_path, monkeypatch):
    bundle = {
        "version": 1,
        "ruleset": "ruleset.json",
        "assets": {
            "vt": {
                "aerofoto": {"file": "backgrounds/rer_1976_78.jpg"},
            }
        },
        "base": {"lat": 44.7, "lng": 12.1, "zoom": 15},
    }
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "deadbeef" * 5}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@deadbeefdead/georender.json": _FakeHttpResponse(
            status_code=200, content=json.dumps(bundle).encode("utf-8")
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    cfg = adapter.fetch_render_config(src)
    assert cfg is not None
    assert cfg["ruleset"] == "ruleset.json"  # ruleset string paths are NOT rewritten
    assert (
        cfg["assets"]["vt"]["aerofoto"]["file"]
        == "github://alice/world@deadbeefdead/backgrounds/rer_1976_78.jpg"
    )
    assert cfg["_repo"] == {"owner": "alice", "repo": "world", "ref": "deadbeefdead"}


def test_geocontext_fetch_render_config_returns_none_when_absent(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "feedface" * 5}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@feedfacefeed/georender.json": _FakeHttpResponse(
            status_code=404
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    assert adapter.fetch_render_config(src) is None


def test_geocontext_fetch_render_config_raises_on_bad_json(tmp_path, monkeypatch):
    routes = {
        "https://api.github.com/repos/alice/world/commits/HEAD": _FakeHttpResponse(
            status_code=200, json_body={"sha": "cafebabe" * 5}
        ),
        "https://cdn.jsdelivr.net/gh/alice/world@cafebabecafe/georender.json": _FakeHttpResponse(
            status_code=200, content=b"not json"
        ),
    }
    _install_fake_http(monkeypatch, routes)

    src = _make_source(
        tmp_path,
        {"name": "GC", "url": "/gc", "mode": "geocontext", "owner": "alice", "repo": "world"},
    )
    adapter = GeocontextAdapter(cache_dir=tmp_path / "cache")
    with pytest.raises(SourceError, match="not valid JSON"):
        adapter.fetch_render_config(src)


# ---------------------------------------------------------------------------
# Loose JSON parsing (for georender.json bundles)
# ---------------------------------------------------------------------------


def test_loose_json_strips_line_comments():
    from georender_service.sources import _loose_json_loads

    data = _loose_json_loads("""{
        "a": 1, // first key
        "b": 2  // second
    }""")
    assert data == {"a": 1, "b": 2}


def test_loose_json_strips_block_comments():
    from georender_service.sources import _loose_json_loads

    assert _loose_json_loads('{"a": /* inline */ 1, /* nope */ "b": 2}') == {"a": 1, "b": 2}


def test_loose_json_preserves_urls_inside_strings():
    """`//` inside a string literal must NOT be treated as a comment."""
    from georender_service.sources import _loose_json_loads

    data = _loose_json_loads('{"url": "https://x/y", "other": "a // b"}')
    assert data == {"url": "https://x/y", "other": "a // b"}


def test_loose_json_tolerates_trailing_commas():
    from georender_service.sources import _loose_json_loads

    assert _loose_json_loads('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}
    assert _loose_json_loads('[1, 2, 3,]') == [1, 2, 3]


def test_loose_json_real_world_georender_sample():
    """Mirror the upstream openhistorymap/valle_trebba style."""
    from georender_service.sources import _loose_json_loads

    text = """{
        "version": 1,
        "ruleset": "georender_ruleset.json",          // path in repo, or inline object
        "assets": {                                   // optional collections
          "vt": {
            "aerofoto_1976": {
              "file": "backgrounds/rer_1976_78.jpg"   // bare paths get auto-rewritten
            }
          }
        },
        "base": {"lat": 44.702654, "lng": 12.121156, "zoom": 15},
        "render": {"width": 4096, "height": 4096, "padding_px": 64},
        "geocontext": {
          "manifest": "gcx.json",
        }
    }"""
    data = _loose_json_loads(text)
    assert data["ruleset"] == "georender_ruleset.json"
    assert data["base"]["lng"] == 12.121156
    assert data["geocontext"]["manifest"] == "gcx.json"


def test_loose_json_still_raises_on_truly_bad_json():
    from georender_service.sources import _loose_json_loads

    with pytest.raises(json.JSONDecodeError):
        _loose_json_loads("{not even close")


# ---------------------------------------------------------------------------
# georender.schema.json — sanity checks on the shipped schema
# ---------------------------------------------------------------------------


def _load_schema():
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "georender.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def test_schema_file_is_valid_json_and_self_describes():
    schema = _load_schema()
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["title"] == "georender.json"
    assert schema["type"] == "object"
    # Every top-level field the runtime knows about must appear in the schema.
    expected = {"version", "ruleset", "assets", "base", "bbox", "render", "geocontext"}
    assert expected.issubset(set(schema["properties"].keys()))


def test_schema_example_roundtrips_through_runtime_parser():
    from georender_service.sources import _loose_json_loads

    schema = _load_schema()
    examples = schema.get("examples", [])
    assert examples, "schema must ship at least one example bundle"
    for example in examples:
        text = json.dumps(example)
        # The loose parser is what fetch_render_config feeds the bundle into.
        # Sanity-check that a schema example survives that path unchanged.
        assert _loose_json_loads(text) == example


def test_schema_example_drives_through_rewrite():
    from georender_service.sources import _rewrite_files_to_github

    schema = _load_schema()
    example = schema["examples"][0]
    rewritten = _rewrite_files_to_github(example["assets"], "owner", "repo", "main")
    aerofoto = rewritten["vt"]["aerofoto_1976"]["file"]
    assert aerofoto == "github://owner/repo@main/backgrounds/rer_1976_78.jpg"
