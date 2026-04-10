from __future__ import annotations

import pytest

from georender_service.cache import FileCache


@pytest.fixture()
def cache(tmp_path):
    return FileCache(tmp_path / "cache")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_tile_path_is_deterministic(cache):
    p1 = cache.tile_path("mymap", "myruleset", "src_rev", "rs_rev", "rend_rev", 3, 4, 2)
    p2 = cache.tile_path("mymap", "myruleset", "src_rev", "rs_rev", "rend_rev", 3, 4, 2)
    assert p1 == p2


def test_tile_path_different_tile_coords_differ(cache):
    p1 = cache.tile_path("mymap", "myruleset", "src_rev", "rs_rev", "rend_rev", 3, 4, 2)
    p2 = cache.tile_path("mymap", "myruleset", "src_rev", "rs_rev", "rend_rev", 3, 4, 3)
    assert p1 != p2


def test_tile_path_different_map_differs(cache):
    p1 = cache.tile_path("map_a", "rs", "s", "r", "v", 0, 0, 0)
    p2 = cache.tile_path("map_b", "rs", "s", "r", "v", 0, 0, 0)
    assert p1 != p2


def test_tile_path_different_source_revision_differs(cache):
    p1 = cache.tile_path("map", "rs", "rev_1", "r", "v", 0, 0, 0)
    p2 = cache.tile_path("map", "rs", "rev_2", "r", "v", 0, 0, 0)
    assert p1 != p2


def test_tile_path_tile_params_affect_path(cache):
    p1 = cache.tile_path("m", "rs", "s", "r", "v", 0, 0, 0, {"tile_size": 256})
    p2 = cache.tile_path("m", "rs", "s", "r", "v", 0, 0, 0, {"tile_size": 512})
    assert p1 != p2


def test_tile_path_under_tiles_subdir(cache):
    p = cache.tile_path("mymap", "rs", "s", "r", "v", 3, 4, 2)
    assert "tiles" in p.parts


def test_image_path_under_images_subdir(cache):
    p = cache.image_path("mymap", "rs", "s", "r", "v", {"width": 512})
    assert "images" in p.parts


def test_ad_hoc_path_under_adhoc_subdir(cache):
    p = cache.ad_hoc_image_path("rs", {"body_hash": "abc", "width": 256})
    assert "adhoc" in p.parts


def test_image_path_deterministic(cache):
    params = {"bounds": [0.0, 0.0, 1.0, 1.0], "width": 1024, "height": 768}
    p1 = cache.image_path("m", "rs", "s", "r", "v", params)
    p2 = cache.image_path("m", "rs", "s", "r", "v", params)
    assert p1 == p2


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def test_read_bytes_returns_none_for_missing(cache):
    result = cache.read_bytes(cache.base_dir / "does_not_exist.png")
    assert result is None


def test_round_trip_write_read(cache):
    path = cache.tile_path("m", "rs", "s", "r", "v", 0, 0, 0)
    data = b"\x89PNG test data"
    cache.write_bytes(path, data)
    assert cache.read_bytes(path) == data


def test_write_creates_parent_directories(cache):
    path = cache.tile_path("deep", "rs", "s", "r", "v", 5, 12, 7)
    cache.write_bytes(path, b"pixels")
    assert path.exists()


def test_second_write_overwrites(cache):
    path = cache.image_path("m", "rs", "s", "r", "v", {"w": 1})
    cache.write_bytes(path, b"v1")
    cache.write_bytes(path, b"v2")
    assert cache.read_bytes(path) == b"v2"
