from __future__ import annotations

import json

import pytest

from tests.conftest import PNG_SIGNATURE, SAMPLE_GEOJSON

# All tests in this module use the session-scoped demo_client fixture
# which points at the real bundled demo map and demo ruleset.

# ---------------------------------------------------------------------------
# Health / catalog
# ---------------------------------------------------------------------------


def test_health(demo_client):
    resp = demo_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_list_rulesets_includes_demo(demo_client):
    resp = demo_client.get("/rulesets")
    assert resp.status_code == 200
    assert "demo" in resp.json()["rulesets"]


def test_list_maps_includes_demo(demo_client):
    resp = demo_client.get("/maps")
    assert resp.status_code == 200
    slugs = [m["slug"] for m in resp.json()["maps"]]
    assert "demo" in slugs


def test_get_map_demo(demo_client):
    resp = demo_client.get("/maps/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "demo"
    assert "revision" in body


def test_get_map_not_found(demo_client):
    resp = demo_client.get("/maps/does_not_exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TileJSON
# ---------------------------------------------------------------------------


def test_tilejson_structure(demo_client):
    resp = demo_client.get("/demo/demo/tilejson.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tilejson"] == "3.0.0"
    assert isinstance(body["tiles"], list)
    assert len(body["tiles"]) == 1
    assert "{z}" in body["tiles"][0]
    assert "center" in body
    assert len(body["center"]) == 3


def test_tilejson_unknown_map(demo_client):
    resp = demo_client.get("/ghost/demo/tilejson.json")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tile endpoint
# ---------------------------------------------------------------------------


def test_tile_returns_png(demo_client):
    resp = demo_client.get("/demo/demo/3/4/2.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == PNG_SIGNATURE


def test_tile_has_cache_control(demo_client):
    resp = demo_client.get("/demo/demo/3/4/2.png")
    assert "cache-control" in resp.headers
    assert "max-age" in resp.headers["cache-control"]


def test_tile_has_etag(demo_client):
    resp = demo_client.get("/demo/demo/3/4/2.png")
    assert "etag" in resp.headers


def test_tile_etag_returns_304(demo_client):
    resp1 = demo_client.get("/demo/demo/3/4/2.png")
    etag = resp1.headers["etag"]
    resp2 = demo_client.get("/demo/demo/3/4/2.png", headers={"if-none-match": etag})
    assert resp2.status_code == 304


def test_tile_custom_tile_size(demo_client):
    from PIL import Image
    import io

    resp = demo_client.get("/demo/demo/3/4/2.png?tile_size=128")
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (128, 128)


def test_tile_unknown_map_returns_404(demo_client):
    resp = demo_client.get("/ghost/demo/3/4/2.png")
    assert resp.status_code == 404


def test_tile_unknown_ruleset_returns_400(demo_client):
    resp = demo_client.get("/demo/ghost_ruleset/3/4/2.png")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Image endpoint
# ---------------------------------------------------------------------------


def test_image_returns_png(demo_client):
    resp = demo_client.get("/demo/demo/image.png?width=256&height=256")
    assert resp.status_code == 200
    assert resp.content[:8] == PNG_SIGNATURE


def test_image_correct_dimensions(demo_client):
    from PIL import Image
    import io

    resp = demo_client.get("/demo/demo/image.png?width=320&height=240")
    assert resp.status_code == 200
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (320, 240)


def test_image_with_explicit_bbox(demo_client):
    resp = demo_client.get(
        "/demo/demo/image.png?width=256&height=256&bbox=10.5,44.3,11.8,45.3&bbox_crs=EPSG:4326"
    )
    assert resp.status_code == 200
    assert resp.content[:8] == PNG_SIGNATURE


def test_image_unknown_map_returns_404(demo_client):
    resp = demo_client.get("/ghost/demo/image.png")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /render/{ruleset}.png
# ---------------------------------------------------------------------------


def test_post_render_returns_png(demo_client):
    resp = demo_client.post(
        "/render/demo.png?width=256&height=256",
        content=json.dumps(SAMPLE_GEOJSON),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.content[:8] == PNG_SIGNATURE


def test_post_render_correct_dimensions(demo_client):
    from PIL import Image
    import io

    resp = demo_client.post(
        "/render/demo.png?width=200&height=150",
        content=json.dumps(SAMPLE_GEOJSON),
        headers={"content-type": "application/json"},
    )
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (200, 150)


def test_post_render_unknown_ruleset_returns_400(demo_client):
    resp = demo_client.post(
        "/render/no_such_ruleset.png",
        content=json.dumps(SAMPLE_GEOJSON),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_post_render_with_explicit_bbox(demo_client):
    resp = demo_client.post(
        "/render/demo.png?width=256&height=256&bbox=10.0,44.0,11.0,45.0&bbox_crs=EPSG:4326",
        content=json.dumps(SAMPLE_GEOJSON),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.content[:8] == PNG_SIGNATURE


def test_post_render_cached_on_second_request(demo_client):
    """Second identical POST should return same bytes (from cache)."""
    body = json.dumps(SAMPLE_GEOJSON)
    headers = {"content-type": "application/json"}
    resp1 = demo_client.post("/render/demo.png?width=128&height=128", content=body, headers=headers)
    resp2 = demo_client.post("/render/demo.png?width=128&height=128", content=body, headers=headers)
    assert resp1.content == resp2.content
