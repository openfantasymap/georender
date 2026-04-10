from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from georender_service.engine import AssetStore, GeoRenderer
from georender_service.geometry import WEB_MERCATOR_HALF, viewport_from_bounds

FULL_WORLD = (-WEB_MERCATOR_HALF, -WEB_MERCATOR_HALF, WEB_MERCATOR_HALF, WEB_MERCATOR_HALF)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# ---------------------------------------------------------------------------
# Fixtures pointing at real project data
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_ASSETS = PROJECT_ROOT / "assets"
REAL_RULESETS = PROJECT_ROOT / "rulesets"
REAL_GEOJSON = PROJECT_ROOT / "example.geojson"


# ---------------------------------------------------------------------------
# AssetStore — file loading
# ---------------------------------------------------------------------------


def test_asset_store_load_plain_file(tmp_assets):
    store = AssetStore(tmp_assets)
    img = store.load("icon.png")
    assert isinstance(img, Image.Image)


def test_asset_store_load_resizes(tmp_assets):
    store = AssetStore(tmp_assets)
    img = store.load("icon.png", size_px=4)
    assert max(img.width, img.height) == 4


def test_asset_store_load_converts_to_rgba(tmp_assets):
    store = AssetStore(tmp_assets)
    img = store.load("icon.png")
    assert img.mode == "RGBA"


# ---------------------------------------------------------------------------
# AssetStore — collection resolution
# ---------------------------------------------------------------------------


def test_asset_store_resolve_qualified_name(tmp_assets):
    store = AssetStore(tmp_assets)
    resolved_id, asset_def = store.resolve("test.marker", {"test": "test"})
    assert resolved_id == "test.marker"


def test_asset_store_resolve_alias(tmp_assets):
    """alias 'test' maps to collection 'test'; 'test.marker' resolves via alias."""
    store = AssetStore(tmp_assets)
    img = store.load_for_ruleset("test.marker", asset_collections={"test": "test"}, size_px=8)
    assert isinstance(img, Image.Image)


def test_asset_store_resolve_unqualified_unique_name(tmp_assets):
    """Unambiguous bare name resolves without a prefix."""
    store = AssetStore(tmp_assets)
    img = store.load_for_ruleset("marker", asset_collections={"test": "test"})
    assert isinstance(img, Image.Image)


def test_asset_store_missing_raises(tmp_assets):
    store = AssetStore(tmp_assets)
    with pytest.raises(FileNotFoundError):
        store.load("no_such_file.png")


def test_asset_store_missing_collection_asset_raises(tmp_assets):
    store = AssetStore(tmp_assets)
    with pytest.raises(FileNotFoundError):
        store.load_for_ruleset("test.ghost", asset_collections={"test": "test"})


def test_asset_store_ambiguous_raises(tmp_assets):
    """Same name in two collections → ambiguous error."""
    store = AssetStore(tmp_assets)
    # Add a second collection that also has 'marker'
    registry = json.loads((tmp_assets / "assets.json").read_text())
    registry["collections"]["other"] = {"marker": {"file": "tile.png"}}
    (tmp_assets / "assets.json").write_text(json.dumps(registry))
    store2 = AssetStore(tmp_assets)
    with pytest.raises(ValueError, match="ambiguous"):
        store2.resolve("marker", {"test": "test", "other": "other"})


# ---------------------------------------------------------------------------
# AssetStore — deterministic variant selection
# ---------------------------------------------------------------------------


def test_asset_store_variant_selection_is_deterministic(tmp_assets):
    store = AssetStore(tmp_assets)
    img1 = store.load_for_ruleset("test.ground", {"test": "test"}, size_px=8, seed="fixed-seed")
    img2 = store.load_for_ruleset("test.ground", {"test": "test"}, size_px=8, seed="fixed-seed")
    # Same seed must produce identical images
    assert list(img1.getdata()) == list(img2.getdata())


def test_asset_store_different_seeds_may_differ(tmp_assets):
    store = AssetStore(tmp_assets)
    imgs = {
        store.load_for_ruleset("test.ground", {"test": "test"}, size_px=8, seed=f"seed-{i}").tobytes()
        for i in range(20)
    }
    # With 2 variants and randomization, at least 2 distinct outputs expected in 20 draws
    assert len(imgs) >= 2


# ---------------------------------------------------------------------------
# GeoRenderer — render_png
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_renderer(tmp_ruleset_dir, tmp_assets):
    return GeoRenderer(tmp_ruleset_dir, tmp_assets)


SIMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"kind": "water"},
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
                "coordinates": [[10.2, 44.3], [10.8, 44.7]],
            },
        },
        {
            "type": "Feature",
            "properties": {"kind": "city"},
            "geometry": {"type": "Point", "coordinates": [10.5, 44.5]},
        },
    ],
}


def test_render_png_returns_bytes(test_renderer):
    data = test_renderer.render_png(SIMPLE_GEOJSON, "test", width=128, height=128)
    assert isinstance(data, bytes)
    assert data[:8] == PNG_SIGNATURE


def test_render_png_correct_dimensions(test_renderer):
    data = test_renderer.render_png(SIMPLE_GEOJSON, "test", width=200, height=150)
    img = Image.open(__import__("io").BytesIO(data))
    assert img.size == (200, 150)


def test_render_png_empty_features_renders_background(test_renderer):
    empty = {"type": "FeatureCollection", "features": []}
    data = test_renderer.render_png(empty, "test", width=64, height=64)
    assert data[:8] == PNG_SIGNATURE
    img = Image.open(__import__("io").BytesIO(data))
    assert img.size == (64, 64)


def test_render_png_with_explicit_bbox(test_renderer):
    from georender_service.geometry import ensure_mercator
    from shapely.geometry import box

    merc_bounds = ensure_mercator(box(10.0, 44.0, 11.0, 45.0), "EPSG:4326").bounds
    data = test_renderer.render_png(
        SIMPLE_GEOJSON, "test", width=128, height=128, bbox=list(merc_bounds)
    )
    assert data[:8] == PNG_SIGNATURE


def test_render_tile_image_returns_pil_image(test_renderer):
    from georender_service.geometry import ensure_mercator, load_geom, mercator_tile_bounds
    from shapely.geometry import box as shapely_box

    features = SIMPLE_GEOJSON["features"]
    mercator_geoms = [ensure_mercator(load_geom(f), "EPSG:4326") for f in features]
    tile_bounds = mercator_tile_bounds(4, 2, 3)
    vp = viewport_from_bounds(tile_bounds, width=256, height=256, padding_px=0)
    img = test_renderer.render_tile_image(features, mercator_geoms, "test", vp)
    assert isinstance(img, Image.Image)
    assert img.mode == "RGBA"


# ---------------------------------------------------------------------------
# GeoRenderer — integration with real demo data
# ---------------------------------------------------------------------------


def test_demo_renderer_renders_png():
    renderer = GeoRenderer(REAL_RULESETS, REAL_ASSETS)
    geojson = json.loads(REAL_GEOJSON.read_text(encoding="utf-8"))
    data = renderer.render_png(geojson, "demo", width=256, height=256)
    assert data[:8] == PNG_SIGNATURE
    img = Image.open(__import__("io").BytesIO(data))
    assert img.size == (256, 256)
