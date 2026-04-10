# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**georender** is a FastAPI service that applies named rulesets (JSON) to geospatial data (GeoJSON, PostGIS, or remote MVT tiles) and renders symbolic PNG images — either as map tiles (`/{map}/{ruleset}/{z}/{x}/{y}.png`), full images (`/{map}/{ruleset}/image.png`), or ad hoc POSTed GeoJSON (`POST /render/{ruleset}.png`).

## Commands

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run (dev)
uvicorn georender_service.app:app --reload

# Run tests (none exist yet; run individual checks with)
python -m py_compile georender_service/*.py
```

## Architecture

All core logic lives under `georender_service/`:

| Module | Role |
|--------|------|
| `app.py` | FastAPI routes and request orchestration |
| `engine.py` | `GeoRenderer` (scene rendering pipeline) + `AssetStore` (asset loading, variants, randomization) |
| `rules.py` | `RulesetStore` — loads/validates JSON rulesets; `feature_matches()` filter evaluation |
| `sources.py` | `SourceStore` — discovers maps, dispatches to `GeoJSONAdapter`, `PostGISAdapter`, `MVTAdapter` |
| `geometry.py` | CRS transforms (EPSG:4326↔3857), `Viewport`, tile math, pixel projection |
| `cache.py` | `FileCache` — content-addressed disk cache keyed by map+ruleset+source/ruleset revisions |
| `tiles.py` | (tile math helpers) |
| `models.py` | (shared dataclasses) |

### Data flow

1. Request arrives → `app.py` resolves bounds (tile coords, center/zoom from timeline, or explicit bbox).
2. `SourceStore.fetch_for_bounds()` picks the right adapter and fetches GeoJSON features clipped to those bounds.
3. `GeoRenderer.render_tile_image()` / `render_png()` loads the ruleset, iterates rules sorted by `z_index`, and calls `_apply_rule()` for each matching feature.
4. Symbolizers: `icon`, `polygon_fill`, `polygon_pattern`, `line_pattern` — all rendered via Pillow onto an RGBA canvas.
5. PNG bytes are written to `FileCache` and returned with ETag/Cache-Control.

### Map sources (`maps/`)

Maps are discovered from `maps/*.json` or `maps/*/timeline.json`. The `mode` field selects the adapter:
- `geojson` — reads a local file; path resolved relative to the timeline file.
- `postgis` — queries a PostGIS DB via `connections.json`; tables come from `events` and `relatedLayers` fields.
- `mvt` — fetches tiles from a `tile_url_template`; decodes with `mapbox-vector-tile`.

The map `slug` is derived from the `url` field, or the file/directory name as fallback.

### Rulesets (`rulesets/`)

Each ruleset is a JSON file with:
- `background`: canvas fill color (CSS/hex with alpha).
- `asset_collections`: `{"alias": "collection_name"}` — maps short names to collections in `assets/assets.json`.
- `rules[]`: ordered by `z_index`; each rule has `geometry` (allowed types), `filter` (property matchers), `symbolizer`, and optional `edge_fade`.

Filter operators: equality, `in`, `not_in`, `exists`, `gte`, `lte`.

Legacy keys are normalized on load: `paint→symbolizer`, `z→z_index`, `where→filter`.

### Assets (`assets/`)

Defined in `assets/assets.json` under `collections`. An asset can be a plain file or a `variant_set` with weighted variants and `randomization` options (`rotation`, `flip_x`, `flip_y`, `brightness_jitter`, `contrast_jitter`). Variant and randomization selection is deterministic — seeded from the rule name + feature index + position — so adjacent tiles stay visually stable.

### Cache invalidation

Tiles are cached by a hash of `(map_slug, ruleset, source_revision, ruleset_revision, renderer_revision, tile_params)`. Source revision for GeoJSON is derived from file mtime+size; for PostGIS/MVT it comes from an explicit `revision` field in the timeline or falls back to a hash of the config. Bump `RENDERER_REVISION` in `app.py` to bust all caches globally.

### PostGIS setup

Copy `connections.example.json` → `connections.json` and add DSN entries keyed by the `connection.db` value in the timeline:

```json
{ "mydb": { "dsn": "postgresql://user:pass@host:5432/mydb" } }
```

Geometry column defaults to `geom`; override with `geometry_column` in the timeline.
