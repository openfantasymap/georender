# georender

[![Build and push Docker image](https://github.com/openfantasymap/georender/actions/workflows/docker.yml/badge.svg)](https://github.com/openfantasymap/georender/actions/workflows/docker.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A symbolic map rendering service for [Open Fantasy Maps](https://github.com/openfantasymap). It applies a named JSON ruleset to geospatial data and renders PNG images — either as slippy-map tiles, full bounding-box images, or ad hoc POSTed GeoJSON.

## Quick start

```bash
docker pull ghcr.io/openfantasymap/georender:main
docker run -p 8000:8000 ghcr.io/openfantasymap/georender:main
```

Then try the included demo:

```bash
curl "http://localhost:8000/demo/demo/3/4/2.png" --output tile.png
curl "http://localhost:8000/demo/demo/image.png?width=1024&height=768" --output image.png
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/rulesets` | List available rulesets |
| GET | `/maps` | List configured map sources |
| GET | `/maps/{map}` | Describe a map source |
| GET | `/{map}/{ruleset}/{z}/{x}/{y}.png` | Slippy-map tile |
| GET | `/{map}/{ruleset}/tilejson.json` | TileJSON 3.0 descriptor |
| GET | `/{map}/{ruleset}/image.png` | Full image (bbox or center/zoom from timeline) |
| POST | `/render/{ruleset}.png` | Render ad hoc GeoJSON body |

Query params for tile/image routes: `tile_size`, `buffer_px`, `padding_px`, `width`, `height`, `bbox`, `bbox_crs`.

## Map sources

Map sources are discovered from `maps/*.json` and `maps/*/timeline.json`. The `mode` field selects the data backend:

**GeoJSON** — reads a local file:
```json
{
  "name": "My World", "url": "/myworld", "mode": "geojson",
  "geojson": "../../myworld.geojson",
  "base": { "zoom": 4, "lat": 0, "lng": 0 }
}
```

**PostGIS** — queries a PostGIS database (requires `connections.json`, see below):
```json
{
  "name": "Alien", "url": "/alien", "mode": "postgis",
  "connection": { "db": "alien" },
  "events": "locations",
  "relatedLayers": ["systems-circle", "spacestation-circle"],
  "base": { "zoom": 9.85, "lat": 0, "lng": 0 }
}
```

**MVT** — fetches remote Mapbox Vector Tiles:
```json
{
  "name": "Remote", "url": "/remote", "mode": "mvt",
  "tile_url_template": "https://example.com/tiles/{z}/{x}/{y}.pbf",
  "relatedLayers": ["roads", "water"]
}
```

### `connections.json`

Required for PostGIS sources. Copy the example and fill in your DSN:

```bash
cp connections.example.json connections.json
```

```json
{
  "mydb": { "dsn": "postgresql://user:password@host:5432/mydb" }
}
```

When running via Docker, mount it at runtime — do not bake credentials into the image:

```bash
docker run -v $(pwd)/connections.json:/app/connections.json \
           -v $(pwd)/maps:/app/maps \
           -p 8000:8000 \
           ghcr.io/openfantasymap/georender:main
```

## Rulesets

Rulesets live in `rulesets/<name>.json`. Rules are applied in ascending `z_index` order. Each rule matches features by geometry type and property filters, then renders them with a symbolizer.

```json
{
  "background": "#f7fbff",
  "asset_collections": { "terrain": "terrain" },
  "rules": [
    {
      "name": "water", "z_index": 1,
      "geometry": ["Polygon", "MultiPolygon"],
      "filter": { "kind": "water" },
      "symbolizer": { "type": "polygon_fill", "fill": "#9fd7ffcc" },
      "edge_fade": { "distance_px": 10 }
    }
  ]
}
```

**Symbolizers**: `icon`, `polygon_fill`, `polygon_pattern`, `line_pattern`.

**Filter operators**: equality, `in`, `not_in`, `exists`, `gte`, `lte`.

## Assets

Assets are defined in `assets/assets.json` grouped into collections. A ruleset references them via `asset_collections`. Variant selection and randomization (rotation, flip, brightness/contrast jitter) are deterministic per position, so tiles stay visually stable across requests.

```json
{
  "collections": {
    "terrain": {
      "stone-floor": {
        "kind": "variant_set",
        "variants": [
          { "file": "stone_01.png", "weight": 4 },
          { "file": "stone_02.png", "weight": 2 }
        ],
        "randomization": { "rotation": [0, 90, 180, 270], "flip_x": true }
      }
    }
  }
}
```

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn georender_service.app:app --reload
```

## Cache

Rendered tiles and images are cached on disk under `cache/`. The cache key includes the map slug, source revision, ruleset revision, and renderer version — so edits to a ruleset or timeline file automatically invalidate affected entries. To bust everything, bump `RENDERER_REVISION` in `georender_service/app.py`.

## License

Apache 2.0 — see [LICENSE](LICENSE).
