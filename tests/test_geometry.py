from __future__ import annotations

import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from georender_service.geometry import (
    WEB_MERCATOR_HALF,
    Viewport,
    ensure_mercator,
    expand_bounds_pixels,
    geom_to_pixel,
    load_geom,
    mercator_bounds_for_features,
    mercator_bounds_from_center_zoom,
    mercator_tile_bounds,
    safe_tangent_angle,
    tile_range_for_bounds,
    viewport_from_bounds,
)

APPROX = pytest.approx


# ---------------------------------------------------------------------------
# ensure_mercator
# ---------------------------------------------------------------------------


def test_ensure_mercator_noop_for_3857():
    pt = Point(1_000_000, 2_000_000)
    result = ensure_mercator(pt, "EPSG:3857")
    assert result.x == APPROX(pt.x)
    assert result.y == APPROX(pt.y)


def test_ensure_mercator_converts_4326_origin():
    pt = Point(0.0, 0.0)
    result = ensure_mercator(pt, "EPSG:4326")
    assert result.x == APPROX(0.0, abs=1.0)
    assert result.y == APPROX(0.0, abs=1.0)


def test_ensure_mercator_converts_4326_known_point():
    # lng=180 should map to +WEB_MERCATOR_HALF
    pt = Point(180.0, 0.0)
    result = ensure_mercator(pt, "EPSG:4326")
    assert result.x == APPROX(WEB_MERCATOR_HALF, rel=1e-4)


def test_ensure_mercator_case_insensitive():
    pt = Point(0.0, 0.0)
    result = ensure_mercator(pt, "epsg:4326")
    assert result.x == APPROX(0.0, abs=1.0)


def test_ensure_mercator_unsupported_crs_raises():
    with pytest.raises(ValueError, match="Unsupported"):
        ensure_mercator(Point(0, 0), "EPSG:27700")


# ---------------------------------------------------------------------------
# mercator_tile_bounds
# ---------------------------------------------------------------------------


def test_mercator_tile_bounds_z0_is_full_world():
    minx, miny, maxx, maxy = mercator_tile_bounds(0, 0, 0)
    assert minx == APPROX(-WEB_MERCATOR_HALF, rel=1e-6)
    assert miny == APPROX(-WEB_MERCATOR_HALF, rel=1e-6)
    assert maxx == APPROX(WEB_MERCATOR_HALF, rel=1e-6)
    assert maxy == APPROX(WEB_MERCATOR_HALF, rel=1e-6)


def test_mercator_tile_bounds_z1_top_left():
    minx, miny, maxx, maxy = mercator_tile_bounds(0, 0, 1)
    assert minx == APPROX(-WEB_MERCATOR_HALF, rel=1e-6)
    assert miny == APPROX(0.0, abs=1.0)
    assert maxx == APPROX(0.0, abs=1.0)
    assert maxy == APPROX(WEB_MERCATOR_HALF, rel=1e-6)


def test_mercator_tile_bounds_z1_bottom_right():
    minx, miny, maxx, maxy = mercator_tile_bounds(1, 1, 1)
    assert minx == APPROX(0.0, abs=1.0)
    assert miny == APPROX(-WEB_MERCATOR_HALF, rel=1e-6)
    assert maxx == APPROX(WEB_MERCATOR_HALF, rel=1e-6)
    assert maxy == APPROX(0.0, abs=1.0)


def test_mercator_tile_bounds_adjacent_tiles_share_edge():
    _, _, maxx_left, _ = mercator_tile_bounds(0, 0, 1)
    minx_right, _, _, _ = mercator_tile_bounds(1, 0, 1)
    assert maxx_left == APPROX(minx_right, abs=1.0)


# ---------------------------------------------------------------------------
# expand_bounds_pixels
# ---------------------------------------------------------------------------


def test_expand_bounds_pixels_zero_pad_is_noop():
    bounds = (0.0, 0.0, 100.0, 100.0)
    assert expand_bounds_pixels(bounds, 100, 100, 0) == bounds


def test_expand_bounds_pixels_expands_symmetrically():
    bounds = (100.0, 200.0, 200.0, 400.0)
    result = expand_bounds_pixels(bounds, 100, 200, 10)
    minx, miny, maxx, maxy = result
    # 10% expansion in each direction
    assert minx < 100.0
    assert miny < 200.0
    assert maxx > 200.0
    assert maxy > 400.0
    # Symmetric expansion
    assert (100.0 - minx) == APPROX(maxx - 200.0, rel=1e-6)
    assert (200.0 - miny) == APPROX(maxy - 400.0, rel=1e-6)


# ---------------------------------------------------------------------------
# tile_range_for_bounds
# ---------------------------------------------------------------------------


def test_tile_range_z0_returns_single_tile():
    full_world = (-WEB_MERCATOR_HALF, -WEB_MERCATOR_HALF, WEB_MERCATOR_HALF, WEB_MERCATOR_HALF)
    tiles = tile_range_for_bounds(full_world, 0)
    assert tiles == [(0, 0, 0)]


def test_tile_range_z1_full_world_returns_four_tiles():
    full_world = (-WEB_MERCATOR_HALF, -WEB_MERCATOR_HALF, WEB_MERCATOR_HALF, WEB_MERCATOR_HALF)
    tiles = tile_range_for_bounds(full_world, 1)
    assert len(tiles) == 4
    assert (1, 0, 0) in tiles
    assert (1, 1, 1) in tiles


def test_tile_range_small_bounds_returns_one_tile():
    # A tiny region that fits within a single z=1 tile (top-left quadrant)
    small = (-WEB_MERCATOR_HALF + 1, 1.0, -WEB_MERCATOR_HALF + 1000, 1000.0)
    tiles = tile_range_for_bounds(small, 1)
    assert len(tiles) == 1
    assert tiles[0] == (1, 0, 0)


# ---------------------------------------------------------------------------
# viewport_from_bounds / world_to_pixel
# ---------------------------------------------------------------------------


def test_viewport_from_bounds_no_padding():
    vp = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), width=100, height=100, padding_px=0)
    assert vp.minx == APPROX(0.0)
    assert vp.maxx == APPROX(100.0)
    assert vp.miny == APPROX(0.0)
    assert vp.maxy == APPROX(100.0)
    assert vp.width == 100
    assert vp.height == 100


def test_viewport_world_to_pixel_corners():
    vp = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), width=100, height=100, padding_px=0)
    # Bottom-left world → bottom-left pixel (y is flipped: high world-y = low pixel-y)
    px, py = vp.world_to_pixel(0.0, 0.0)
    assert px == APPROX(0.0)
    assert py == APPROX(100.0)

    # Top-right world → top-right pixel
    px, py = vp.world_to_pixel(100.0, 100.0)
    assert px == APPROX(100.0)
    assert py == APPROX(0.0)


def test_viewport_world_to_pixel_center():
    vp = viewport_from_bounds((0.0, 0.0, 200.0, 200.0), width=200, height=200, padding_px=0)
    px, py = vp.world_to_pixel(100.0, 100.0)
    assert px == APPROX(100.0)
    assert py == APPROX(100.0)


def test_viewport_from_bounds_with_padding_expands_world():
    # padding_px=10 on a 100x100 canvas should expand the viewport world coords beyond bounds
    vp_no_pad = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), 100, 100, padding_px=0)
    vp_padded = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), 100, 100, padding_px=10)
    assert vp_padded.minx < vp_no_pad.minx
    assert vp_padded.miny < vp_no_pad.miny


# ---------------------------------------------------------------------------
# geom_to_pixel
# ---------------------------------------------------------------------------


def test_geom_to_pixel_point():
    vp = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), width=100, height=100, padding_px=0)
    result = geom_to_pixel(Point(50.0, 50.0), vp)
    assert result.x == APPROX(50.0)
    assert result.y == APPROX(50.0)


def test_geom_to_pixel_preserves_type():
    vp = viewport_from_bounds((0.0, 0.0, 100.0, 100.0), width=100, height=100, padding_px=0)
    line = LineString([(0, 0), (100, 100)])
    result = geom_to_pixel(line, vp)
    assert result.geom_type == "LineString"


# ---------------------------------------------------------------------------
# mercator_bounds_from_center_zoom
# ---------------------------------------------------------------------------


def test_mercator_bounds_from_center_zoom_origin():
    bounds = mercator_bounds_from_center_zoom(lng=0.0, lat=0.0, zoom=0, width=256, height=256)
    minx, miny, maxx, maxy = bounds
    assert minx < 0 < maxx
    assert miny < 0 < maxy
    # Should be symmetric around origin
    assert abs(minx + maxx) < 1.0
    assert abs(miny + maxy) < 1.0


def test_mercator_bounds_from_center_zoom_higher_zoom_is_smaller():
    b_low = mercator_bounds_from_center_zoom(0.0, 0.0, zoom=1, width=256, height=256)
    b_high = mercator_bounds_from_center_zoom(0.0, 0.0, zoom=5, width=256, height=256)
    span_low = b_low[2] - b_low[0]
    span_high = b_high[2] - b_high[0]
    assert span_high < span_low


# ---------------------------------------------------------------------------
# mercator_bounds_for_features
# ---------------------------------------------------------------------------


def test_mercator_bounds_for_features_single_point():
    pt = ensure_mercator(Point(10.0, 44.0), "EPSG:4326")
    bounds = mercator_bounds_for_features([pt])
    minx, miny, maxx, maxy = bounds
    # Single point gets expanded to avoid zero span
    assert maxx > minx
    assert maxy > miny


def test_mercator_bounds_for_features_multiple():
    pts = [
        ensure_mercator(Point(0.0, 0.0), "EPSG:4326"),
        ensure_mercator(Point(10.0, 45.0), "EPSG:4326"),
    ]
    bounds = mercator_bounds_for_features(pts)
    minx, miny, maxx, maxy = bounds
    assert minx < 0 < maxx
    assert miny < 0 < maxy


def test_mercator_bounds_for_features_empty_returns_world():
    bounds = mercator_bounds_for_features([])
    assert bounds == (-WEB_MERCATOR_HALF, -WEB_MERCATOR_HALF, WEB_MERCATOR_HALF, WEB_MERCATOR_HALF)


# ---------------------------------------------------------------------------
# safe_tangent_angle
# ---------------------------------------------------------------------------


def test_safe_tangent_angle_horizontal_line():
    line = LineString([(0, 0), (100, 0)])
    angle = safe_tangent_angle(line, 50.0)
    assert angle == APPROX(0.0, abs=1.0)


def test_safe_tangent_angle_vertical_line():
    line = LineString([(0, 0), (0, 100)])
    angle = safe_tangent_angle(line, 50.0)
    assert abs(angle) == APPROX(90.0, abs=1.0)


def test_safe_tangent_angle_diagonal():
    line = LineString([(0, 0), (100, 100)])
    angle = safe_tangent_angle(line, 50.0)
    assert angle == APPROX(45.0, abs=1.0)


# ---------------------------------------------------------------------------
# load_geom
# ---------------------------------------------------------------------------


def test_load_geom_point():
    feature = {"geometry": {"type": "Point", "coordinates": [10.0, 44.0]}}
    geom = load_geom(feature)
    assert geom.geom_type == "Point"


def test_load_geom_missing_geometry_returns_empty():
    geom = load_geom({"geometry": None})
    assert geom.is_empty


def test_load_geom_no_geometry_key_returns_empty():
    geom = load_geom({})
    assert geom.is_empty
