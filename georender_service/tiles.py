from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from shapely.geometry import box

from .engine import GeoRenderer
from .geometry import (
    ensure_mercator,
    load_geom,
    mercator_bounds_for_features,
    mercator_tile_bounds,
    viewport_from_bounds,
)


def render_tile_pyramid_zip(
    renderer: GeoRenderer,
    geojson: dict[str, Any],
    ruleset_name: str,
    z_min: int,
    z_max: int,
    tile_size: int,
    buffer_px: int,
    source_crs: str,
    clip_to_geojson_bounds: bool = True,
) -> bytes:
    features = list(geojson.get("features", []))
    mercator_geoms = [ensure_mercator(load_geom(f), source_crs) for f in features]
    geo_bounds = mercator_bounds_for_features(mercator_geoms)
    world_box = box(*geo_bounds)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        metadata = {
            "ruleset_name": ruleset_name,
            "z_min": z_min,
            "z_max": z_max,
            "tile_size": tile_size,
            "buffer_px": buffer_px,
            "source_crs": source_crs,
            "clip_to_geojson_bounds": clip_to_geojson_bounds,
        }
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))

        for z in range(z_min, z_max + 1):
            n = 2**z
            minx, miny, maxx, maxy = geo_bounds
            full_span = 40075016.68557849 / n
            min_tx = max(0, int((minx + 20037508.342789244) // full_span) - 1)
            max_tx = min(n - 1, int((maxx + 20037508.342789244) // full_span) + 1)
            min_ty = max(0, int((20037508.342789244 - maxy) // full_span) - 1)
            max_ty = min(n - 1, int((20037508.342789244 - miny) // full_span) + 1)
            for x in range(min_tx, max_tx + 1):
                for y in range(min_ty, max_ty + 1):
                    tile_bounds = mercator_tile_bounds(x, y, z)
                    tile_box = box(*tile_bounds)
                    if clip_to_geojson_bounds and not tile_box.intersects(world_box):
                        continue
                    viewport = viewport_from_bounds(
                        tile_bounds,
                        width=tile_size + buffer_px * 2,
                        height=tile_size + buffer_px * 2,
                        padding_px=buffer_px,
                    )
                    img = renderer.render_tile_image(features, mercator_geoms, ruleset_name, viewport)
                    cropped = img.crop((buffer_px, buffer_px, buffer_px + tile_size, buffer_px + tile_size))
                    tile_buf = io.BytesIO()
                    cropped.save(tile_buf, format="PNG")
                    zf.writestr(f"{z}/{x}/{y}.png", tile_buf.getvalue())
    return buf.getvalue()
