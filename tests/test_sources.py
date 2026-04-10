from __future__ import annotations

import json
from pathlib import Path

import pytest

from georender_service.geometry import WEB_MERCATOR_HALF, ensure_mercator
from georender_service.sources import (
    SourceError,
    SourceStore,
    _collect_tables,
    _resolve_connection_dsn,
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
