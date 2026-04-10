from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class ImageRenderRequest(BaseModel):
    geojson: dict[str, Any]
    ruleset_name: str
    width: int = Field(default=1024, ge=64, le=8192)
    height: int = Field(default=1024, ge=64, le=8192)
    bbox: list[float] | None = None
    padding_px: int = Field(default=32, ge=0, le=1024)
    source_crs: str = "EPSG:4326"
    fit_mode: Literal["bbox", "features"] = "features"


class TileRenderRequest(BaseModel):
    geojson: dict[str, Any]
    ruleset_name: str
    z_min: int = Field(default=0, ge=0, le=22)
    z_max: int = Field(default=2, ge=0, le=22)
    tile_size: int = Field(default=256, ge=64, le=1024)
    buffer_px: int = Field(default=64, ge=0, le=512)
    source_crs: str = "EPSG:4326"
    clip_to_geojson_bounds: bool = True
