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


# ---------------------------------------------------------------------------
# polygon_texture symbolizer
# ---------------------------------------------------------------------------


def _polygon_texture_ruleset(tile_ref, **extra):
    symbolizer = {"type": "polygon_texture", "asset": tile_ref, "tile_size_px": 16}
    symbolizer.update(extra)
    return {
        "name": "texture",
        "background": "#00000000",
        "asset_collections": {"test": "test"},
        "rules": [
            {
                "name": "ground",
                "z_index": 1,
                "geometry": ["Polygon", "MultiPolygon"],
                "filter": {"kind": "water"},
                "symbolizer": symbolizer,
            }
        ],
    }


def test_polygon_texture_renders_pixels_inside_polygon(tmp_ruleset_dir, tmp_assets):
    (tmp_ruleset_dir / "texture.json").write_text(
        json.dumps(_polygon_texture_ruleset("test.marker")), encoding="utf-8"
    )
    renderer = GeoRenderer(tmp_ruleset_dir, tmp_assets)
    data = renderer.render_png(SIMPLE_GEOJSON, "texture", width=128, height=128)
    img = Image.open(__import__("io").BytesIO(data))
    # Some pixels should now be opaque (the polygon interior), not background.
    alphas = [px[3] for px in img.getdata()]
    assert any(a > 0 for a in alphas)


def test_polygon_texture_is_deterministic_across_renders(tmp_ruleset_dir, tmp_assets):
    (tmp_ruleset_dir / "texture.json").write_text(
        json.dumps(_polygon_texture_ruleset("test.marker", rotation=True)),
        encoding="utf-8",
    )
    renderer = GeoRenderer(tmp_ruleset_dir, tmp_assets)
    first = renderer.render_png(SIMPLE_GEOJSON, "texture", width=64, height=64)
    second = renderer.render_png(SIMPLE_GEOJSON, "texture", width=64, height=64)
    assert first == second  # same seed → same bytes


def test_polygon_texture_tint_changes_output(tmp_ruleset_dir, tmp_assets):
    base_def = _polygon_texture_ruleset("test.marker")
    tinted_def = _polygon_texture_ruleset("test.marker", tint="#ff000080")
    (tmp_ruleset_dir / "base.json").write_text(json.dumps(base_def), encoding="utf-8")
    (tmp_ruleset_dir / "tinted.json").write_text(json.dumps(tinted_def), encoding="utf-8")
    renderer = GeoRenderer(tmp_ruleset_dir, tmp_assets)
    base = renderer.render_png(SIMPLE_GEOJSON, "base", width=64, height=64)
    tinted = renderer.render_png(SIMPLE_GEOJSON, "tinted", width=64, height=64)
    assert base != tinted  # tint must have observable effect


def test_polygon_texture_missing_asset_is_silent(tmp_ruleset_dir, tmp_assets):
    """A rule with an empty/missing `asset` should leave the canvas as background,
    not crash the render."""
    ruleset_def = _polygon_texture_ruleset("test.marker")
    ruleset_def["rules"][0]["symbolizer"].pop("asset")
    (tmp_ruleset_dir / "noasset.json").write_text(json.dumps(ruleset_def), encoding="utf-8")
    renderer = GeoRenderer(tmp_ruleset_dir, tmp_assets)
    data = renderer.render_png(SIMPLE_GEOJSON, "noasset", width=32, height=32)
    assert data[:8] == PNG_SIGNATURE


def test_resolve_texture_rotation_handles_all_specs():
    from georender_service.engine import _resolve_texture_rotation, _stable_hash

    seed = _stable_hash("anything")
    assert _resolve_texture_rotation(0, seed) == 0.0
    assert _resolve_texture_rotation(None, seed) == 0.0
    assert _resolve_texture_rotation(False, seed) == 0.0
    assert _resolve_texture_rotation(45, seed) == 45.0
    assert _resolve_texture_rotation(True, seed) in {0.0, 90.0, 180.0, 270.0}
    assert _resolve_texture_rotation([15, 30, 45], seed) in {15.0, 30.0, 45.0}
    assert _resolve_texture_rotation([], seed) == 0.0


def test_apply_tint_with_zero_alpha_is_noop():
    from georender_service.engine import _apply_tint

    layer = Image.new("RGBA", (4, 4), (100, 150, 200, 255))
    out = _apply_tint(layer, (255, 0, 0, 0))
    assert list(out.getdata()) == list(layer.getdata())


# ---------------------------------------------------------------------------
# Remote assets via github:// + http(s)://
# ---------------------------------------------------------------------------


def test_expand_github_uri_basic():
    from georender_service.engine import _expand_github_uri

    assert _expand_github_uri("github://alice/world/path/to/tex.png") == (
        "https://cdn.jsdelivr.net/gh/alice/world@HEAD/path/to/tex.png"
    )
    assert _expand_github_uri("github://alice/world@v1.2/textures/dirt.png") == (
        "https://cdn.jsdelivr.net/gh/alice/world@v1.2/textures/dirt.png"
    )


def test_expand_github_uri_rejects_malformed():
    from georender_service.engine import _expand_github_uri

    with pytest.raises(ValueError):
        _expand_github_uri("github://no-repo-here")
    with pytest.raises(ValueError):
        _expand_github_uri("github://owner/repo")  # missing path


def test_asset_store_resolve_short_circuits_for_urls(tmp_assets):
    store = AssetStore(tmp_assets)
    resolved_id, asset_def = store.resolve("github://alice/world/x.png", None)
    assert resolved_id.startswith("url:")
    assert asset_def == {"file": "github://alice/world/x.png"}

    resolved_id, asset_def = store.resolve("https://example.com/x.png", None)
    assert resolved_id.startswith("url:")


def test_asset_store_remote_without_cache_raises(tmp_assets):
    store = AssetStore(tmp_assets)  # no cache_dir
    with pytest.raises(FileNotFoundError, match="no cache_dir"):
        store.load_for_ruleset("https://example.com/x.png", None)


def _png_bytes(color=(20, 220, 80, 255)):
    """Build a tiny solid-colour PNG for the network stub to return."""
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_asset_store_downloads_and_caches_http(tmp_assets, tmp_path, monkeypatch):
    """Remote asset fetched once, then served from the on-disk cache."""
    import georender_service.engine as engine_mod

    payload = _png_bytes()
    calls: list[str] = []

    class _Resp:
        status_code = 200
        content = payload
        def raise_for_status(self):
            return None

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        return _Resp()

    # Patch httpx.get inside the engine module — _fetch_remote_asset does a local import.
    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fake_get)

    cache_dir = tmp_path / "cache"
    store = AssetStore(tmp_assets, cache_dir=cache_dir)

    img1 = store.load_for_ruleset(
        "https://example.com/textures/dirt.png", None, size_px=8,
    )
    img2 = store.load_for_ruleset(
        "https://example.com/textures/dirt.png", None, size_px=8,
    )
    assert isinstance(img1, Image.Image) and isinstance(img2, Image.Image)
    assert len(calls) == 1  # second call must come from disk cache
    cached = list((cache_dir / "assets").iterdir())
    assert len(cached) == 1
    assert cached[0].suffix == ".png"


def test_asset_store_github_uri_routes_through_jsdelivr(tmp_assets, tmp_path, monkeypatch):
    """A `github://` reference must end up hitting the matching jsDelivr URL."""
    payload = _png_bytes((200, 50, 50, 255))
    received_urls: list[str] = []

    class _Resp:
        status_code = 200
        content = payload
        def raise_for_status(self):
            return None

    def fake_get(url, *args, **kwargs):
        received_urls.append(url)
        return _Resp()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", fake_get)

    store = AssetStore(tmp_assets, cache_dir=tmp_path / "cache")
    img = store.load_for_ruleset(
        "github://openhistorymap/valle_trebba@HEAD/backgrounds/rer_1976_78.png",
        None,
        size_px=8,
    )
    assert isinstance(img, Image.Image)
    assert received_urls == [
        "https://cdn.jsdelivr.net/gh/openhistorymap/valle_trebba@HEAD/backgrounds/rer_1976_78.png"
    ]


def test_asset_store_remote_through_registry(tmp_assets, tmp_path, monkeypatch):
    """A collection entry whose `file` is a URL should pull that URL too."""
    import json as _json

    payload = _png_bytes()

    class _Resp:
        status_code = 200
        content = payload
        def raise_for_status(self):
            return None

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "get", lambda url, *a, **kw: _Resp())

    registry = _json.loads((tmp_assets / "assets.json").read_text())
    registry["collections"]["remote"] = {
        "aerofoto": {"file": "github://alice/world/backgrounds/aerial.png"}
    }
    (tmp_assets / "assets.json").write_text(_json.dumps(registry))

    store = AssetStore(tmp_assets, cache_dir=tmp_path / "cache")
    img = store.load_for_ruleset("remote.aerofoto", {"remote": "remote"}, size_px=8)
    assert isinstance(img, Image.Image)


def test_asset_store_remote_fetch_failure_raises_filenotfound(tmp_assets, tmp_path, monkeypatch):
    import httpx as _httpx

    def raising_get(*args, **kwargs):
        raise _httpx.ConnectError("boom")

    monkeypatch.setattr(_httpx, "get", raising_get)
    store = AssetStore(tmp_assets, cache_dir=tmp_path / "cache")
    with pytest.raises(FileNotFoundError, match="Failed to fetch remote asset"):
        store.load_for_ruleset("https://example.com/x.png", None)


def test_asset_store_overlay_collections_shadow_disk(tmp_assets):
    store = AssetStore(tmp_assets)
    # marker exists on disk under "test"; the overlay re-defines it.
    store.register_overlay({"override": {"marker": {"file": "tile.png"}}})
    resolved_id, asset_def = store.resolve("override.marker", {"override": "override"})
    assert resolved_id == "override.marker"
    assert asset_def == {"file": "tile.png"}


def test_asset_store_clear_overlay_restores_disk_only(tmp_assets):
    store = AssetStore(tmp_assets)
    store.register_overlay({"shadow": {"x": {"file": "tile.png"}}})
    store.clear_overlay()
    with pytest.raises(FileNotFoundError):
        store.resolve("shadow.x", {"shadow": "shadow"})


def test_resolve_falls_back_to_literal_prefix_when_alias_misses(tmp_assets):
    """Ruleset alias `vt → valle_trebba` but a literal `vt` overlay collection
    holds the asset — resolver should use the overlay rather than 404."""
    store = AssetStore(tmp_assets)
    store.register_overlay({"vt": {"acqua": {"file": "tile.png"}}})
    resolved_id, asset_def = store.resolve(
        "vt.acqua",
        asset_collections={"vt": "valle_trebba"},  # alias points elsewhere
    )
    assert resolved_id == "vt.acqua"
    assert asset_def == {"file": "tile.png"}


def test_alias_target_still_wins_when_it_has_the_asset(tmp_assets):
    """If the aliased collection DOES have the asset, the alias wins — the
    literal-prefix fallback only kicks in on a miss."""
    store = AssetStore(tmp_assets)
    store.register_overlay(
        {
            "vt": {"x": {"file": "tile.png"}},
            "valle_trebba": {"x": {"file": "icon.png"}},
        }
    )
    resolved_id, asset_def = store.resolve(
        "vt.x", asset_collections={"vt": "valle_trebba"}
    )
    assert resolved_id == "valle_trebba.x"
    assert asset_def == {"file": "icon.png"}
