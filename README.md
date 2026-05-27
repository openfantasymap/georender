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

**Geocontext** — pulls a [`geocontext.json`](https://github.com/openhistorymap/geocontext-front/blob/main/FORMAT.md) manifest from a public GitHub repo (via `cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/`), resolves its `datasources[]` (inline GeoJSON, remote GeoJSON, CSV-of-points, or client-side `transform` derivations like `buffer`), and tags features with `__layer` / `__source_layer` so rulesets can filter on them:
```json
{
  "name": "Valle Trebba", "url": "/valle-trebba", "mode": "geocontext",
  "repository": "openhistorymap/valle-trebba",
  "ref": "HEAD",
  "layers": ["graves"],
  "base": { "zoom": 15, "lat": 44.702654, "lng": 12.121156 }
}
```
- `repository` is shorthand for `"owner": "...", "repo": "..."` — either form works.
- `ref` defaults to `HEAD` (the repo's default branch). Pin to a tag or commit SHA for reproducibility.
- `manifest` lets you override the filename. Defaults to `geocontext.json`, with `gcx.json` as a fallback.
- `layers` (optional) whitelists which data-driven layers to include; omit to pull them all.
- Downloaded manifest + GeoJSON / CSV assets are cached on disk under `cache/sources/geocontext/<owner>/<repo>/<sha>/` so repeated tiles for the same revision don't re-hit jsDelivr.

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

**Symbolizers**: `icon`, `polygon_fill`, `polygon_pattern`, `polygon_texture`, `line_pattern`.

### Remote rulesets (`$remote`)

A ruleset can live alongside its data in a public GitHub repo. To use it, drop a one-line stub in `rulesets/`:

```jsonc
// rulesets/valle-trebba.json
{ "$remote": "github://openhistorymap/valle_trebba@HEAD/ruleset.json" }
```

On `load()`, the renderer fetches the upstream JSON via jsDelivr, caches it under `cache/sources/rulesets/<sha>.json`, and treats it as the real ruleset (normalization + validation included). The tile cache's `ruleset_revision` folds in the remote payload's hash, so publishing a new commit to the upstream `ruleset.json` invalidates downstream tiles once the local CDN cache is busted. Network failures on `revision()` are tolerated — tiles still serve from the existing cache.

Accepted schemes: `https://`, `http://`, and `github://<owner>/<repo>[@<ref>]/<path>` (default `<ref>` = `HEAD`).

### `polygon_texture`

Photorealistic tile fill for polygons. Variant + rotation + brightness/contrast jitter from the asset definition are resolved **once per feature** (stable seed) and the resulting tile is repeated across the polygon's bbox aligned to a global pixel grid — so the inside of a polygon reads as one continuous texture, and neighbouring polygons that share a rule tile through each other without re-phasing at the seam.

```json
{
  "name": "dossi",
  "z_index": 4,
  "geometry": ["Polygon", "MultiPolygon"],
  "filter": {"__layer": "Dossi"},
  "symbolizer": {
    "type": "polygon_texture",
    "asset": "ground.dirt_seamless",
    "tile_size_px": 256,
    "rotation": [0, 90, 180, 270],
    "tint": "#806b4d80",
    "opacity": 1.0
  },
  "edge_fade": {"distance_px": 6}
}
```

| Field | Default | Notes |
|---|---|---|
| `asset` | required | Tileable texture asset (variant_set OK). |
| `tile_size_px` | `128` | Nominal tile edge in output pixels. |
| `rotation` | `0` | Single angle, `true` (random 0/90/180/270), or `[angles]`. Applied once per feature. |
| `tint` | none | CSS/hex color, alpha-weighted multiply blend (`#7a8c4a80` = 50% green). |
| `opacity` | `1.0` | Final layer opacity. |

**Filter operators**: equality, `in`, `not_in`, `exists`, `gte`, `lte`.

## Assets

Assets are defined in `assets/assets.json` grouped into collections. A ruleset references them via `asset_collections`. Variant selection and randomization (rotation, flip, brightness/contrast jitter) are deterministic per position, so tiles stay visually stable across requests.

A `file` entry can be:

| Form | Loaded from |
|---|---|
| `tree.png` (bare / relative) | `assets/tree.png` on disk. |
| `https://…` / `http://…` | Downloaded once, cached under `cache/sources/assets/<sha>.<ext>`. |
| `github://<owner>/<repo>[@<ref>]/<path>` | Expanded to `cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>` and cached. `<ref>` defaults to `HEAD`. |

This lets a geocontext-style map ship its own textures, sketches, or aerial-photo overlays from the same GitHub repo that holds the manifest — and the ruleset references them through the normal collection mechanism:

```json
"collections": {
  "valle_trebba": {
    "aerofoto_1976": {
      "file": "github://openhistorymap/valle_trebba@HEAD/backgrounds/rer_1976_78.jpg",
      "kind": "sprite"
    }
  }
}
```

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

### Purging jsDelivr in geocontext repos

The `geocontext` source mode fetches the manifest, datasets, ruleset, and assets from `cdn.jsdelivr.net`, which edge-caches every file for 12 hours. When you push a change to a geocontext repo, downstream renderers keep seeing the stale copy until that TTL elapses.

`scripts/workflows/purge-jsdelivr.yml` is a drop-in GitHub Action that automates the purge. Copy it into any geocontext repo as `.github/workflows/purge-jsdelivr.yml` — no edits needed; it reads `${{ github.repository }}` and `${{ github.ref_name }}` from the workflow context. On every push to `main`/`master` it walks `git ls-files`, batches the paths through jsDelivr's bulk endpoint (100 paths per request), and purges both `@HEAD` and `@<branch>` aliases.

## License

Apache 2.0 — see [LICENSE](LICENSE).
