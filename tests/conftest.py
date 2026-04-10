from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Minimal PNG factory (no Pillow dependency)
# ---------------------------------------------------------------------------

def make_png(width: int = 8, height: int = 8, color: tuple = (180, 120, 60, 255)) -> bytes:
    """Return a valid RGBA PNG of the given size and solid colour."""
    r, g, b, a = color

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    raw = b"".join(b"\x00" + bytes([r, g, b, a] * width) for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


MINIMAL_PNG = make_png()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# ---------------------------------------------------------------------------
# Sample GeoJSON (lon/lat, EPSG:4326)
# ---------------------------------------------------------------------------

SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"kind": "water", "name": "Test Lake"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[10.0, 44.0], [11.0, 44.0], [11.0, 45.0], [10.0, 45.0], [10.0, 44.0]]
                ],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "road"},
            "geometry": {
                "type": "LineString",
                "coordinates": [[10.2, 44.3], [10.5, 44.5], [10.8, 44.7]],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "city", "name": "Test City"},
            "geometry": {"type": "Point", "coordinates": [10.5, 44.5]},
        },
    ],
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_assets(tmp_path: Path) -> Path:
    """Asset directory with minimal PNGs and an assets.json registry."""
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    (assets_dir / "icon.png").write_bytes(MINIMAL_PNG)
    (assets_dir / "tile.png").write_bytes(make_png(color=(60, 180, 60, 255)))

    registry = {
        "collections": {
            "test": {
                "marker": {"file": "icon.png"},
                "ground": {
                    "kind": "variant_set",
                    "variants": [
                        {"file": "tile.png", "weight": 3},
                        {"file": "icon.png", "weight": 1},
                    ],
                    "randomization": {
                        "rotation": [0, 90, 180, 270],
                        "flip_x": True,
                        "brightness_jitter": 0.1,
                    },
                },
            }
        }
    }
    (assets_dir / "assets.json").write_text(json.dumps(registry), encoding="utf-8")
    return assets_dir


@pytest.fixture()
def tmp_ruleset_dir(tmp_path: Path) -> Path:
    ruleset_dir = tmp_path / "rulesets"
    ruleset_dir.mkdir()
    ruleset = {
        "background": "#e8e0d0",
        "asset_collections": {"test": "test"},
        "rules": [
            {
                "name": "water",
                "z_index": 1,
                "geometry": ["Polygon", "MultiPolygon"],
                "filter": {"kind": "water"},
                "symbolizer": {"type": "polygon_fill", "fill": "#5599ffaa"},
                "edge_fade": {"distance_px": 8},
            },
            {
                "name": "roads",
                "z_index": 2,
                "geometry": ["LineString", "MultiLineString"],
                "filter": {},
                "symbolizer": {
                    "type": "line_pattern",
                    "asset": "test.marker",
                    "size_px": 4,
                    "spacing_px": 4,
                    "buffer_px": 4,
                },
            },
            {
                "name": "markers",
                "z_index": 3,
                "geometry": ["Point", "MultiPoint"],
                "filter": {},
                "symbolizer": {"type": "icon", "asset": "test.marker", "size_px": 8},
            },
            {
                "name": "terrain",
                "z_index": 0,
                "geometry": ["Polygon", "MultiPolygon"],
                "filter": {"kind": "park"},
                "symbolizer": {
                    "type": "polygon_pattern",
                    "asset": "test.ground",
                    "size_px": 8,
                    "spacing_px": 8,
                },
            },
        ],
    }
    (ruleset_dir / "test.json").write_text(json.dumps(ruleset), encoding="utf-8")
    return ruleset_dir


@pytest.fixture()
def tmp_maps_dir(tmp_path: Path) -> Path:
    maps_dir = tmp_path / "maps"
    maps_dir.mkdir()
    map_dir = maps_dir / "testmap"
    map_dir.mkdir()

    (map_dir / "data.geojson").write_text(json.dumps(SAMPLE_GEOJSON), encoding="utf-8")
    timeline = {
        "name": "Test Map",
        "url": "/testmap",
        "mode": "geojson",
        "geojson": "data.geojson",
        "base": {"zoom": 5.0, "lat": 44.5, "lng": 10.5},
    }
    (map_dir / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    return maps_dir


@pytest.fixture(scope="session")
def demo_client() -> TestClient:
    """TestClient backed by the real app with the bundled demo data."""
    from georender_service.app import app
    return TestClient(app)
