from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from shapely.geometry import box

from .cache import FileCache
from .engine import GeoRenderer
from .geometry import (
    WEB_MERCATOR_BOUNDS,
    ensure_mercator,
    expand_bounds_pixels,
    mercator_bounds_for_features,
    mercator_bounds_from_center_zoom,
    mercator_tile_bounds,
    viewport_from_bounds,
    load_geom,
)
from .sources import SourceError, SourceStore

BASE_DIR = Path(__file__).resolve().parent.parent
RULESETS_DIR = BASE_DIR / "rulesets"
ASSETS_DIR = BASE_DIR / "assets"
MAPS_DIR = BASE_DIR / "maps"
CACHE_DIR = BASE_DIR / "cache"
CONNECTIONS_PATH = BASE_DIR / "connections.json"
RENDERER_REVISION = "0.2.0-ofm"

app = FastAPI(title="OFM Symbolic Renderer", version=RENDERER_REVISION)
renderer = GeoRenderer(RULESETS_DIR, ASSETS_DIR)
sources = SourceStore(MAPS_DIR, CONNECTIONS_PATH)
cache = FileCache(CACHE_DIR)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/rulesets")
def list_rulesets() -> dict[str, list[str]]:
    return {"rulesets": renderer.rules.list_names()}


@app.get("/maps")
def list_maps() -> dict[str, list[dict[str, Any]]]:
    return {"maps": sources.list_sources()}


@app.get("/maps/{map_name}")
def get_map(map_name: str) -> dict[str, Any]:
    try:
        source = sources.get(map_name)
        return {"map": source.data, "slug": source.slug, "revision": source.revision}
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/{map_name}/{ruleset}/tilejson.json")
def tilejson(request: Request, map_name: str, ruleset: str) -> dict[str, Any]:
    try:
        source = sources.get(map_name)
        renderer.rules.load(ruleset)
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    base = source.data.get("base") or {}
    tiles_url = str(request.url_for("render_named_tile", map_name=map_name, ruleset=ruleset, z=0, x=0, y=0))
    tiles_url = tiles_url.replace("/0/0/0.png", "/{z}/{x}/{y}.png")
    center = [float(base.get("lng", 0)), float(base.get("lat", 0)), float(base.get("zoom", 0))]
    return {
        "tilejson": "3.0.0",
        "name": source.data.get("name", map_name),
        "description": source.data.get("description", ""),
        "tiles": [tiles_url],
        "minzoom": 0,
        "maxzoom": 22,
        "center": center,
        "bounds": [-180.0, -85.05112878, 180.0, 85.05112878],
        "attribution": source.data.get("copyright", ""),
    }


@app.get("/{map_name}/{ruleset}/{z}/{x}/{y}.png", name="render_named_tile")
def render_named_tile(
    request: Request,
    map_name: str,
    ruleset: str,
    z: int,
    x: int,
    y: int,
    tile_size: int = Query(default=256, ge=64, le=1024),
    buffer_px: int = Query(default=64, ge=0, le=512),
):
    try:
        source = sources.get(map_name)
        ruleset_revision = renderer.rules.revision(ruleset)
        tile_bounds = mercator_tile_bounds(x, y, z)
        fetch_bounds = expand_bounds_pixels(tile_bounds, tile_size, tile_size, buffer_px)
        fetched = sources.fetch_for_bounds(map_name, fetch_bounds, tile=(z, x, y))
        etag = _hash_payload(
            {
                "map": map_name,
                "ruleset": ruleset,
                "source_revision": fetched.revision,
                "ruleset_revision": ruleset_revision,
                "renderer": RENDERER_REVISION,
                "z": z,
                "x": x,
                "y": y,
                "tile_size": tile_size,
                "buffer_px": buffer_px,
            }
        )
        if _request_matches_etag(request, etag):
            return Response(status_code=304)
        cache_path = cache.tile_path(
            map_name,
            ruleset,
            fetched.revision,
            ruleset_revision,
            RENDERER_REVISION,
            z,
            x,
            y,
            tile_params={"tile_size": tile_size, "buffer_px": buffer_px},
        )
        cached = cache.read_bytes(cache_path)
        if cached is not None:
            return _png_response(cached, etag=etag, cache_control="public, max-age=3600")

        features = fetched.features
        mercator_geoms = [ensure_mercator(load_geom(f), fetched.source_crs) for f in features]
        viewport = viewport_from_bounds(
            tile_bounds,
            width=tile_size + buffer_px * 2,
            height=tile_size + buffer_px * 2,
            padding_px=buffer_px,
        )
        img = renderer.render_tile_image(features, mercator_geoms, ruleset, viewport)
        cropped = img.crop((buffer_px, buffer_px, buffer_px + tile_size, buffer_px + tile_size))
        png = _image_to_png_bytes(cropped)
        cache.write_bytes(cache_path, png)
        return _png_response(png, etag=etag, cache_control="public, max-age=3600")
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/{map_name}/{ruleset}/image.png")
def render_named_image(
    request: Request,
    map_name: str,
    ruleset: str,
    width: int = Query(default=1024, ge=64, le=8192),
    height: int = Query(default=1024, ge=64, le=8192),
    padding_px: int = Query(default=32, ge=0, le=1024),
    bbox: str | None = Query(default=None),
    bbox_crs: str = Query(default="EPSG:4326"),
):
    try:
        ruleset_revision = renderer.rules.revision(ruleset)
        bounds = _resolve_named_image_bounds(map_name, width, height, bbox, bbox_crs)
        fetch_bounds = expand_bounds_pixels(bounds, width, height, padding_px)
        fetched = sources.fetch_for_bounds(map_name, fetch_bounds)
        image_params = {
            "bounds": [round(v, 6) for v in bounds],
            "width": width,
            "height": height,
            "padding_px": padding_px,
        }
        etag = _hash_payload(
            {
                "map": map_name,
                "ruleset": ruleset,
                "source_revision": fetched.revision,
                "ruleset_revision": ruleset_revision,
                "renderer": RENDERER_REVISION,
                "image": image_params,
            }
        )
        if _request_matches_etag(request, etag):
            return Response(status_code=304)
        cache_path = cache.image_path(map_name, ruleset, fetched.revision, ruleset_revision, RENDERER_REVISION, image_params)
        cached = cache.read_bytes(cache_path)
        if cached is not None:
            return _png_response(cached, etag=etag, cache_control="public, max-age=3600")
        geojson = {"type": "FeatureCollection", "features": fetched.features}
        png = renderer.render_png(
            geojson=geojson,
            ruleset_name=ruleset,
            width=width,
            height=height,
            source_crs=fetched.source_crs,
            bbox=list(bounds),
            padding_px=padding_px,
        )
        cache.write_bytes(cache_path, png)
        return _png_response(png, etag=etag, cache_control="public, max-age=3600")
    except SourceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/render/{ruleset}.png")
async def render_geojson_png(
    ruleset: str,
    request: Request,
    width: int = Query(default=1024, ge=64, le=8192),
    height: int = Query(default=1024, ge=64, le=8192),
    padding_px: int = Query(default=32, ge=0, le=1024),
    source_crs: str = Query(default="EPSG:4326"),
    bbox: str | None = Query(default=None),
    bbox_crs: str = Query(default="EPSG:4326"),
):
    try:
        payload = await request.json()
        renderer.rules.load(ruleset)
        parsed_bbox = _parse_bbox_query(bbox, bbox_crs) if bbox else None
        ruleset_revision = renderer.rules.revision(ruleset)
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        cache_payload = {
            "body_hash": body_hash,
            "ruleset": ruleset,
            "ruleset_revision": ruleset_revision,
            "renderer": RENDERER_REVISION,
            "width": width,
            "height": height,
            "padding_px": padding_px,
            "source_crs": source_crs,
            "bbox": parsed_bbox,
        }
        cache_path = cache.ad_hoc_image_path(ruleset, cache_payload)
        cached = cache.read_bytes(cache_path)
        if cached is not None:
            return _png_response(cached, cache_control="private, max-age=60")
        png = renderer.render_png(
            geojson=payload,
            ruleset_name=ruleset,
            width=width,
            height=height,
            source_crs=source_crs,
            bbox=parsed_bbox,
            padding_px=padding_px,
        )
        cache.write_bytes(cache_path, png)
        return _png_response(png, cache_control="private, max-age=60")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.exception_handler(Exception)
async def generic_exception_handler(_, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def _resolve_named_image_bounds(
    map_name: str,
    width: int,
    height: int,
    bbox: str | None,
    bbox_crs: str,
) -> tuple[float, float, float, float]:
    if bbox:
        parsed = _parse_bbox_query(bbox, bbox_crs)
        if parsed is None:
            raise SourceError("bbox must contain four comma-separated numbers")
        return parsed

    source = sources.get(map_name)
    base = source.data.get("base") or {}
    if {"lat", "lng", "zoom"}.issubset(base.keys()):
        return mercator_bounds_from_center_zoom(
            lng=float(base["lng"]),
            lat=float(base["lat"]),
            zoom=float(base["zoom"]),
            width=width,
            height=height,
        )

    if source.mode == "geojson":
        fetched = sources.fetch_for_bounds(map_name, WEB_MERCATOR_BOUNDS)
        mercator_geoms = [ensure_mercator(load_geom(f), fetched.source_crs) for f in fetched.features]
        return mercator_bounds_for_features(mercator_geoms)

    raise SourceError(
        f"Map '{map_name}' needs either base.lat/lng/zoom or an explicit bbox to render image.png"
    )


def _parse_bbox_query(bbox: str, bbox_crs: str) -> list[float] | None:
    try:
        values = [float(part) for part in bbox.split(",")]
        if len(values) != 4:
            return None
        mercator_bbox = ensure_mercator(box(*values), bbox_crs).bounds
        return [float(v) for v in mercator_bbox]
    except Exception:
        return None


def _png_response(data: bytes, *, etag: str | None = None, cache_control: str = "no-store") -> Response:
    headers = {"Cache-Control": cache_control}
    if etag:
        headers["ETag"] = etag
    return Response(content=data, media_type="image/png", headers=headers)


def _request_matches_etag(request: Request, etag: str) -> bool:
    return request.headers.get("if-none-match") == etag


def _hash_payload(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]


def _image_to_png_bytes(image) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
