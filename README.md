# OFM Symbolic Rendering Service

A small FastAPI renderer that applies a named ruleset to either:

- a named OFM-style map source loaded from `maps/*/timeline.json` or `maps/*.json`
- an ad hoc GeoJSON posted directly in the request body

It now supports the URL contract discussed for OFM-like timelines:

- `GET /{map}/{ruleset}/{z}/{x}/{y}.png`
- `GET /{map}/{ruleset}/tilejson.json`
- `GET /{map}/{ruleset}/image.png?...`
- `POST /render/{ruleset}.png`

The service caches rendered PNG tiles and images on disk.

## What `map` means

`map` is a source descriptor, not raw data. In OFM terms this is the timeline entry.

A timeline JSON can point to:

- `mode: "geojson"` for local GeoJSON files
- `mode: "postgis"` for PostGIS-backed layers
- `mode: "mvt"` for remote MVT sources

The renderer resolves the source from the timeline entry, fetches the relevant features, and then applies the selected ruleset.

## Project structure

```text
georender_service/
├── assets/
│   └── assets.json
├── cache/
├── connections.example.json
├── georender_service/
│   ├── app.py
│   ├── cache.py
│   ├── engine.py
│   ├── geometry.py
│   ├── models.py
│   ├── rules.py
│   ├── sources.py
│   └── tiles.py
├── maps/
│   └── demo/
│       └── timeline.json
├── rulesets/
│   └── demo.json
├── example.geojson
└── requirements.txt
```

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
uvicorn georender_service.app:app --reload
```

## Routes

### Health and catalog

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/rulesets
curl http://127.0.0.1:8000/maps
curl http://127.0.0.1:8000/maps/demo
```

### Named map tile

```bash
curl "http://127.0.0.1:8000/demo/demo/3/4/2.png?tile_size=256&buffer_px=32" --output tile.png
```

### Named map TileJSON

```bash
curl http://127.0.0.1:8000/demo/demo/tilejson.json
```

### Named map image

If `bbox` is omitted, the service uses `base.lng`, `base.lat`, and `base.zoom` from the map timeline when available.

```bash
curl "http://127.0.0.1:8000/demo/demo/image.png?width=1024&height=768" --output image.png
```

Explicit bbox example:

```bash
curl "http://127.0.0.1:8000/demo/demo/image.png?width=1024&height=768&bbox=10.5,44.3,11.8,45.3&bbox_crs=EPSG:4326" --output image.png
```

### Ad hoc GeoJSON render

```bash
curl -X POST "http://127.0.0.1:8000/render/demo.png?width=1024&height=768" \
  -H "Content-Type: application/json" \
  --data-binary @example.geojson \
  --output out.png
```

## Timeline format

A minimal geojson-backed timeline:

```json
{
  "name": "Demo",
  "url": "/demo",
  "mode": "geojson",
  "geojson": "../../example.geojson",
  "base": {
    "zoom": 3.8,
    "lat": 45.0,
    "lng": 11.0
  }
}
```

A PostGIS-backed timeline in OFM style:

```json
{
  "name": "Alien",
  "url": "/alien",
  "mode": "postgis",
  "connection": {
    "db": "alien"
  },
  "events": "locations",
  "relatedLayers": ["systems-circle", "systems-circle-major", "spacestation-circle"],
  "base": {
    "zoom": 9.85,
    "lat": 0,
    "lng": 0
  }
}
```

## connections.json

For PostGIS sources, create a `connections.json` file at the project root.

Example:

```json
{
  "alien": {
    "dsn": "postgresql://user:password@localhost:5432/alien"
  }
}
```

The service ships with `connections.example.json` as a template.


## Assets and variants

Assets can now be defined externally in `assets/assets.json`, grouped into collections.
A ruleset can then import one or more collections through `asset_collections`, either as a list or as an alias map.

Example asset registry:

```json
{
  "collections": {
    "terrain": {
      "stone-floor": {
        "kind": "variant_set",
        "variants": [
          {"file": "stone_01.jpg", "weight": 4},
          {"file": "stone_02.jpg", "weight": 2}
        ],
        "randomization": {
          "rotation": [0, 90, 180, 270],
          "flip_x": true
        }
      }
    }
  }
}
```

Example ruleset fragment:

```json
{
  "asset_collections": {"terrain": "terrain"},
  "rules": [
    {
      "geometry": ["Polygon", "MultiPolygon"],
      "filter": {"kind": "park"},
      "symbolizer": {
        "type": "polygon_pattern",
        "asset": "terrain.stone-floor",
        "size_px": 32,
        "spacing_px": 32
      }
    }
  ]
}
```

Variant selection is deterministic, based on the placement position and rule context, so neighboring tiles stay visually stable instead of re-rolling every request.

## Rulesets

Rulesets still live in `rulesets/<name>.json`.

Supported symbolizers:

- `icon`
- `polygon_fill`
- `polygon_pattern`
- `line_pattern`

Lines are rendered as repeated rotated stamps along the geometry, then clipped through a buffered mask. That is the bit that keeps rivers, roads, and walls from looking like badly stretched stickers.

## Cache behavior

Named map tiles and images are cached on disk under `cache/`.

The cache key includes:

- map slug
- source revision
- ruleset revision
- renderer version
- tile coordinates or image parameters

Ad hoc POST renders are cached separately by body hash and render parameters.

## Current limits

- PostGIS expects a common geometry column name, defaulting to `geom`.
- PostGIS cache invalidation is config-based unless you also version the source explicitly with `revision` in the timeline file.
- MVT mode depends on `mapbox-vector-tile` and a usable `tile_url_template`.
- There is still no label engine.
- Edge fading is blurred-mask based, not a true distance field.

## Demo assets included

The repo includes a local `demo` map wired to `example.geojson`, so the named routes work immediately after install.
