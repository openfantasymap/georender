# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**georender** is a FastAPI service that applies named rulesets (JSON) to geospatial data (GeoJSON, PostGIS, or remote MVT tiles) and renders symbolic PNG images — either as map tiles (`/{map}/{ruleset}/{z}/{x}/{y}.png`), full images (`/{map}/{ruleset}/image.png`), or ad hoc POSTed GeoJSON (`POST /render/{ruleset}.png`).

## Commands

```bash
# Install (dev, includes pytest)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Run (dev)
uvicorn georender_service.app:app --reload

# Run tests
pytest

# Run a single test file
pytest tests/test_engine.py

# Run with coverage
pytest --cov --cov-report=term-missing
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
| `openrouter.py` | `OpenRouterClient` — image generation over OpenRouter, content-addressed disk cache, defensive response parsing |
| `tiles.py` | (tile math helpers) |
| `models.py` | (shared dataclasses) |

### Data flow

1. Request arrives → `app.py` resolves bounds (tile coords, center/zoom from timeline, or explicit bbox).
2. `SourceStore.fetch_for_bounds()` picks the right adapter and fetches GeoJSON features clipped to those bounds.
3. `GeoRenderer.render_tile_image()` / `render_png()` loads the ruleset, iterates rules sorted by `z_index`, and calls `_apply_rule()` for each matching feature.
4. Symbolizers: `icon`, `polygon_fill`, `polygon_pattern`, `polygon_texture`, `line_pattern`, `wms`, `ai_image` — all rendered via Pillow onto an RGBA canvas. `polygon_texture` is the photorealistic-tile variant: variant/rotation/jitter resolved once per feature, multiply `tint`, and global-grid alignment so neighbouring polygons stay seamless. `wms` is the viewport-wide raster: live WMS GetMap fetched against the viewport bounds, disk-cached under `cache/sources/wms/`, composited at the rule's z_index; geometry/filter are not required. `ai_image` is the generated viewport raster — see below.
5. PNG bytes are written to `FileCache` and returned with ETag/Cache-Control.

### Map sources (`maps/`)

Maps are discovered from `maps/*.json` or `maps/*/timeline.json`. The `mode` field selects the adapter:
- `geojson` — reads a local file; path resolved relative to the timeline file.
- `postgis` — queries a PostGIS DB via `connections.json`; tables come from `events` and `relatedLayers` fields.
- `mvt` — fetches tiles from a `tile_url_template`; decodes with `mapbox-vector-tile`.
- `geocontext` — fetches a `geocontext.json` manifest from a public GitHub repo (`<owner>/<repo>` via `cdn.jsdelivr.net/gh/`); resolves `datasources[]` (inline / remote GeoJSON, CSV-of-points, derived `transform` pipelines like `buffer`); features are tagged with `__layer` and `__source_layer` for ruleset filtering. Downloaded assets are cached under `cache/sources/geocontext/<owner>/<repo>/<sha>/`.

The map `slug` is derived from the `url` field, or the file/directory name as fallback.

### Rulesets (`rulesets/`)

Each ruleset is a JSON file with:
- `background`: canvas fill color (CSS/hex with alpha).
- `asset_collections`: `{"alias": "collection_name"}` — maps short names to collections in `assets/assets.json`.
- `rules[]`: ordered by `z_index`; each rule has `geometry` (allowed types), `filter` (property matchers), `symbolizer`, and optional `edge_fade`.

Filter operators: equality, `in`, `not_in`, `exists`, `gte`, `lte`.

Legacy keys are normalized on load: `paint→symbolizer`, `z→z_index`, `where→filter`.

A ruleset file can also be a `{"$remote": "github://owner/repo@ref/path.json"}` stub. `RulesetStore` follows the pointer (via jsDelivr for `github://`, direct fetch otherwise), caches the payload on disk under `cache/sources/rulesets/<sha>.json`, and folds its content hash into `revision()` so tile caches invalidate when the upstream changes. Accepted schemes: `https://`, `http://`, `github://`.

### Assets (`assets/`)

Defined in `assets/assets.json` under `collections`. An asset can be a plain file or a `variant_set` with weighted variants and `randomization` options (`rotation`, `flip_x`, `flip_y`, `brightness_jitter`, `contrast_jitter`). Variant and randomization selection is deterministic — seeded from the rule name + feature index + position — so adjacent tiles stay visually stable.

### OpenRouter image generation

Two entry points, both in `engine.py`, both backed by `OpenRouterClient`:

- **`ai_image` symbolizer** — viewport-wide, like `wms`. It renders the features its rule matches into a flat colour-coded *control image* at the viewport, and sends that plus a preamble instructing the model to preserve every boundary. The output is a repaint of the real geometry, not an invented layout. `control.groups` maps a property value → colour + description; undeclared groups get a colour derived from a hash of the key (stable across anchor cells, which see different subsets). The prompt's legend and the drawn pixels come from the same resolved colours, so they cannot disagree. `describe_scene` adds measured extent / ground resolution / feature counts. Control features are drawn largest-area-first (`control.draw_order`), because one rule paints everything in a single pass and has no per-rule `z_index` — without it a viewport-sized backdrop polygon buries every unit inside it.
- **`openrouter://<model>` asset files** — `AssetStore` synthesizes the image from the entry's `prompt`, whose `{property}` placeholders interpolate from the feature being drawn. Geometry is untouched; the normal symbolizers stamp the result. Note that `_materialized_cache` keys include the resolved prompt hash — two features sharing an asset id can resolve to different images.

**Anchoring**: tile requests snap to a coarse cell (`anchor_zoom_delta`, default 4) and generate once per cell, cropping each tile out. This needs features for the *cell*, not the tile, so `RenderContext.fetch_features` is a callback `app.py` wires to `SourceStore`. `image.png` never anchors.

**Caching** (both under `cache/sources/openrouter/`): `gen/` is content-addressed on (model, prompt, control image) — this is the one that costs money to rebuild; `anchors/` maps (rule, symbolizer, cell, source revision) → generated cell, letting a warm tile skip the fetch and control render. `generate_image()` checks the cache *before* requiring an API key, so a render that ran once stays reproducible offline.

**Failure policy**: `ai_image` degrades like `wms` — warn on stderr, drop the layer, keep rendering. Generated *assets* raise instead, because a missing prompt or unreachable API is a config error in something the scene depends on.

**Prompt tuning** — two findings from testing against real Valle Trebba data, both documented at length in the README and both easy to regress:

1. *Control colours drive whether regions get repainted at all.* Saturated primaries give excellent registration but the model preserves small regions as flat neon overlay, reading them as annotation. Colours near the target material get repainted and degrade gracefully. Both example rulesets (`valle-trebba-ai.json`, `valle-trebba-vivid.json`) carry natural palettes deliberately.
2. *Fidelity and realism pull against each other.* Pushing "leave nothing flat" without a matching constraint makes the model naturalise — in testing it collapsed 178 beach ridges into one patch and rotated the shoreline. `_AI_PREAMBLE` therefore pairs every licence with a limit ("its texture changes, its outline does not"). Avoid wording that invites reshaping. When changing the preamble, compare the control image against the output — a render can look superb and have silently moved half the geometry.

Config: `OPENROUTER_API_KEY` (env wins) or `openrouter.json` (project defaults for model/base_url/timeout). See `openrouter.example.json`. `openrouter.json` holds a secret and must never be committed.

### Cache invalidation

Tiles are cached by a hash of `(map_slug, ruleset, source_revision, ruleset_revision, renderer_revision, tile_params)`. Source revision for GeoJSON is derived from file mtime+size; for PostGIS/MVT it comes from an explicit `revision` field in the timeline or falls back to a hash of the config. Bump `RENDERER_REVISION` in `app.py` to bust all caches globally.

### PostGIS setup

Copy `connections.example.json` → `connections.json` and add DSN entries keyed by the `connection.db` value in the timeline:

```json
{ "mydb": { "dsn": "postgresql://user:pass@host:5432/mydb" } }
```

Geometry column defaults to `geom`; override with `geometry_column` in the timeline.
