from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from pyproj import Transformer
from shapely import GeometryCollection, line_interpolate_point
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

WEB_MERCATOR_HALF = 20037508.342789244
WEB_MERCATOR_BOUNDS = (
    -WEB_MERCATOR_HALF,
    -WEB_MERCATOR_HALF,
    WEB_MERCATOR_HALF,
    WEB_MERCATOR_HALF,
)


@dataclass(slots=True)
class Viewport:
    minx: float
    miny: float
    maxx: float
    maxy: float
    width: int
    height: int

    @property
    def x_span(self) -> float:
        return max(self.maxx - self.minx, 1e-9)

    @property
    def y_span(self) -> float:
        return max(self.maxy - self.miny, 1e-9)

    def world_to_pixel(self, x: float, y: float) -> tuple[float, float]:
        px = (x - self.minx) / self.x_span * self.width
        py = self.height - (y - self.miny) / self.y_span * self.height
        return px, py


def load_geom(feature: dict) -> BaseGeometry:
    geometry = feature.get("geometry")
    if not geometry:
        return GeometryCollection()
    return shape(geometry)


def ensure_mercator(geom: BaseGeometry, source_crs: str) -> BaseGeometry:
    source_crs = (source_crs or "EPSG:4326").upper()
    if source_crs == "EPSG:3857":
        return geom
    if source_crs == "EPSG:4326":
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        return shapely_transform(transformer.transform, geom)
    raise ValueError(f"Unsupported source CRS: {source_crs}")


def lonlat_to_mercator(lng: float, lat: float) -> tuple[float, float]:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    return transformer.transform(lng, lat)


def mercator_bounds_from_center_zoom(
    lng: float,
    lat: float,
    zoom: float,
    width: int,
    height: int,
    tile_size: int = 256,
) -> tuple[float, float, float, float]:
    center_x, center_y = lonlat_to_mercator(lng, lat)
    initial_resolution = (2 * WEB_MERCATOR_HALF) / tile_size
    resolution = initial_resolution / (2**zoom)
    half_w = width * resolution / 2
    half_h = height * resolution / 2
    return (
        center_x - half_w,
        center_y - half_h,
        center_x + half_w,
        center_y + half_h,
    )


def expand_bounds_pixels(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    pad_px: int,
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bounds
    if pad_px <= 0:
        return bounds
    dx = (maxx - minx) * (pad_px / max(width, 1))
    dy = (maxy - miny) * (pad_px / max(height, 1))
    return minx - dx, miny - dy, maxx + dx, maxy + dy


def tile_range_for_bounds(bounds: tuple[float, float, float, float], z: int) -> list[tuple[int, int, int]]:
    minx, miny, maxx, maxy = bounds
    n = 2**z
    tile_span = (2 * WEB_MERCATOR_HALF) / n
    min_tx = max(0, int((minx + WEB_MERCATOR_HALF) // tile_span))
    max_tx = min(n - 1, int((maxx + WEB_MERCATOR_HALF) // tile_span))
    min_ty = max(0, int((WEB_MERCATOR_HALF - maxy) // tile_span))
    max_ty = min(n - 1, int((WEB_MERCATOR_HALF - miny) // tile_span))
    tiles: list[tuple[int, int, int]] = []
    for x in range(min_tx, max_tx + 1):
        for y in range(min_ty, max_ty + 1):
            tiles.append((z, x, y))
    return tiles


def mercator_tile_bounds(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    n = 2**z
    tile_span = (2 * WEB_MERCATOR_HALF) / n
    minx = -WEB_MERCATOR_HALF + x * tile_span
    maxx = minx + tile_span
    maxy = WEB_MERCATOR_HALF - y * tile_span
    miny = maxy - tile_span
    return (minx, miny, maxx, maxy)


def mercator_bounds_for_features(geoms: Iterable[BaseGeometry]) -> tuple[float, float, float, float]:
    bounds = [g.bounds for g in geoms if not g.is_empty]
    if not bounds:
        return WEB_MERCATOR_BOUNDS
    minx = min(b[0] for b in bounds)
    miny = min(b[1] for b in bounds)
    maxx = max(b[2] for b in bounds)
    maxy = max(b[3] for b in bounds)
    if minx == maxx:
        minx -= 1
        maxx += 1
    if miny == maxy:
        miny -= 1
        maxy += 1
    return minx, miny, maxx, maxy


def expand_bounds(bounds: tuple[float, float, float, float], pad_ratio: float = 0.0) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bounds
    dx = (maxx - minx) * pad_ratio
    dy = (maxy - miny) * pad_ratio
    if dx == 0:
        dx = 1
    if dy == 0:
        dy = 1
    return minx - dx, miny - dy, maxx + dx, maxy + dy


def viewport_from_bounds(
    bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    padding_px: int = 0,
) -> Viewport:
    minx, miny, maxx, maxy = bounds
    dx = max(maxx - minx, 1e-9)
    dy = max(maxy - miny, 1e-9)
    sx = width / max(width - 2 * padding_px, 1)
    sy = height / max(height - 2 * padding_px, 1)
    return Viewport(
        minx=minx - dx * (sx - 1) / 2,
        miny=miny - dy * (sy - 1) / 2,
        maxx=maxx + dx * (sx - 1) / 2,
        maxy=maxy + dy * (sy - 1) / 2,
        width=width,
        height=height,
    )


def geom_to_pixel(geom: BaseGeometry, viewport: Viewport) -> BaseGeometry:
    def _map(x: float, y: float, z: float | None = None):
        px, py = viewport.world_to_pixel(x, y)
        if z is None:
            return (px, py)
        return (px, py, z)

    return shapely_transform(_map, geom)


def safe_tangent_angle(line: BaseGeometry, distance: float) -> float:
    length = max(line.length, 1e-9)
    d1 = max(0.0, min(length, distance - 1.0))
    d2 = max(0.0, min(length, distance + 1.0))
    p1 = line_interpolate_point(line, d1)
    p2 = line_interpolate_point(line, d2)
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    if dx == 0 and dy == 0:
        return 0.0
    return math.degrees(math.atan2(dy, dx))
