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

**Symbolizers**: `icon`, `polygon_fill`, `polygon_pattern`, `polygon_texture`, `line_pattern`, `wms`, `ai_image`.

### `wms` (viewport raster)

Live WMS GetMap layer, composited across the whole viewport. Unlike the other symbolizers it doesn't iterate over features — `geometry` and `filter` are optional. Place it low in `z_index` to use as a basemap, or higher to overlay on top of data layers.

```jsonc
{
  "name": "aerofoto-1976",
  "z_index": 0,
  "symbolizer": {
    "type": "wms",
    "url": "https://servizigis.regione.emilia-romagna.it/wms/Aerofoto_RER",
    "layers": "RER_1976_78",
    "version": "1.3.0",
    "format": "image/jpeg",
    "transparent": false,
    "opacity": 1.0
  }
}
```

| Field | Default | Notes |
|---|---|---|
| `url` | required | Base WMS endpoint. |
| `layers` | required | Comma-separated layer name(s). |
| `version` | `"1.3.0"` | Also accepts `"1.1.1"`. |
| `format` | `"image/png"` | Use `"image/jpeg"` for opaque photo layers. |
| `crs` | `"EPSG:3857"` | Renderer's native — no reprojection, no axis-order surprises. Pass `"EPSG:4326"` if the server requires it; the 1.3.0 lat/lon flip is handled automatically. |
| `styles` | `""` | Server default. |
| `transparent` | `true` | Set false for opaque rasters (smaller payloads). |
| `opacity` | `1.0` | Final layer opacity. |
| `extra_params` | `{}` | Free-form GET params merged into the request. |
| `cache` | `true` | Disk-cache responses under `cache/sources/wms/`. Set false for upstreams whose content changes (weather, real-time). |

Network failures and non-image responses (HTML error pages from misconfigured WMS) degrade gracefully — the renderer drops the layer and the rest of the scene still produces a PNG. `edge_fade.distance_px` softens the raster's outer border into transparency.

### `georender.json` bundle

A geocontext repo can ship a `georender.json` at its root that declares the ruleset, asset collections, default extent (`bbox` or `base`), and canvas (`render.width/height/padding_px`) — see the full schema at [`schemas/georender.schema.json`](schemas/georender.schema.json). Add the `$schema` pointer to get autocomplete + validation in VSCode and most JSON editors:

```jsonc
{
  "$schema": "https://raw.githubusercontent.com/openfantasymap/georender/main/schemas/georender.schema.json",
  "version": 1,
  "ruleset": "georender_ruleset.json",
  "bbox": [12.089, 44.695, 12.127, 44.722],
  "render": { "width": 4096, "height": 4096, "padding_px": 64 },
  "assets": {
    "vt": {
      "acqua_increspata": {
        "file": "assets/blue-rippled-water-background.jpg",
        "kind": "texture",
        "tileable": true
      }
    }
  },
  "geocontext": { "manifest": "gcx.json" }
}
```

The runtime parser tolerates `//` and `/* */` comments and trailing commas, so the file can carry inline documentation.

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

### `ai_image` (generated viewport raster)

Paints the viewport with an OpenRouter image model. Like `wms` it runs once per viewport rather than per feature, so `geometry` and `filter` are optional — but unlike `wms` it *does* read them, to choose which features ground the image.

**The output is derived from the data, not prompted into existence.** Every request carries a control image built from the features this rule matches: each one is drawn as a flat colour region at its true projected position, and the prompt tells the model to preserve every boundary. The model repaints the real map; it does not invent a layout.

```jsonc
{
  "name": "ai-terrain",
  "z_index": 0,
  "geometry": ["Polygon", "MultiPolygon"],
  "symbolizer": {
    "type": "ai_image",
    "model": "google/gemini-2.5-flash-image",
    "prompt": "Low-altitude black-and-white aerial survey photograph, 1970s film grain.",
    "describe_scene": true,
    "control": {
      "group_by": "__layer",
      "groups": {
        "Dossi":      { "color": "#ff3b30", "describe": "sandy dune ridge, pale dry soil, sparse scrub" },
        "Paleoalvei": { "color": "#34c759", "describe": "silted former river channel, dark damp clay" }
      }
    },
    "anchor_zoom_delta": 4
  }
}
```

That produces a request shaped like this:

```
control image (real geometry, flat colours)     prompt
+-----------------------------+                 preserve every boundary exactly …
|   ####      ~~~~~~~~~~      |                 Style: 1970s aerial survey photo …
|  ######   ~~~~~~~~          |                 #ff3b30 -> sandy dune ridge, …
|      ~~~~~~~                |                 #34c759 -> silted former river channel, …
+-----------------------------+                 0.62 m/pixel, 1.10 km wide, north up,
   #### Dossi   ~~~~ Paleoalvei                 17 x Dossi, 4 x Paleoalvei
```

| Field | Default | Notes |
|---|---|---|
| `model` | client default (`google/gemini-2.5-flash-image`) | Must accept image input and return image output. |
| `prompt` | none | Style/content direction. Optional — without it the model is just asked to repaint faithfully. |
| `control.group_by` | none | Property whose value defines a control group; each value gets its own colour. |
| `control.groups` | `{}` | Per-value `color` + `describe`. Undeclared groups still get a stable colour derived from the key, but no description. |
| `control.source` | `"features"` | `"canvas"` sends the scene painted so far; `"both"` sends canvas + flat render as two images. Unavailable under anchoring (see below). |
| `control.background` | `#000000ff` | Fill where no feature lands. |
| `control.line_width_px` / `point_radius_px` | `6` / `8` | How lines and points are thickened for the control render. |
| `control.include_legend` | auto | On when any group has a `describe`. |
| `describe_scene` | `false` | Appends measured extent, ground resolution (m/px, cos-latitude corrected), north-up, feature counts and names. |
| `anchor` | `true` on tiles | See anchor grid below. |
| `anchor_zoom_delta` / `anchor_zoom` / `anchor_size_px` | `4` / — / `1024` | Anchor cell selection and generation size. |
| `max_side_px` | `1024` | Control image is downscaled to this before sending. |
| `opacity` | `1.0` | Final layer opacity. |
| `replace_canvas` | `false` | Paste over everything beneath instead of compositing. |
| `cache` | `true` | Disk-cache the anchor cell and the generation. **Leave this on** — off means paying for every tile of every render. |

The descriptive text is opt-in; the control image never is. A bare `{"type": "ai_image"}` still sends the geometry and still gets a data-faithful repaint — it just won't know that the red blobs are dune ridges.

#### Choosing control colours

This is the single biggest quality lever, and it is not what you'd guess.

**Use muted, terrain-plausible colours — not saturated ones.** Tested against real Valle Trebba data with `google/gemini-2.5-flash-image`: a palette of saturated primaries (`#ff3b30`, `#34c759`, `#ffcc00`) produced excellent *registration* — every boundary landed correctly — but the model repainted only the large background regions and preserved the small ones as flat neon shapes, reading them as an annotation overlay to keep rather than material to replace. Swapping to colours already close to the target material (`#b9a479` sand, `#55614e` silted clay, `#d8cdb0` pale beach sand) on the same geometry produced a coherent aerial photograph.

The muted palette also fails better: a region the model declines to repaint still looks like terrain instead of a highlighter stroke.

Two other things that follow from the same experiment:

- Order matters, and is handled for you. Features are drawn **largest first** (`control.draw_order`, default `"area"`), so a viewport-sized backdrop polygon can't bury the units inside it — a single rule paints every feature in one pass and has no per-rule `z_index` to fall back on. Lines and points have zero area and therefore always land on top.
- Exclude pure backdrop layers with a filter. A `Sfondo`-style rectangle spanning the whole extent contributes nothing and costs contrast: `"filter": { "__layer": { "not_in": ["Sfondo"] } }`.

Undeclared groups get a saturated auto-colour derived from their key. That default favours unambiguous segmentation, which is the right call when nothing is known about the layer — but declare `control.groups` with a muted palette for anything you care about.

Expect some noncompliance regardless: models occasionally add text despite being told not to, and partial repaints happen.

Two worked examples ship with the repo, both against the real Valle Trebba data:

| Ruleset | Look |
|---|---|
| `rulesets/valle-trebba-ai.json` | 1976 black-and-white survey flight, muted greys |
| `rulesets/valle-trebba-vivid.json` | modern full-colour orthophoto, natural material palette |

#### Fidelity vs. realism

These pull against each other, and the tension is worth understanding before you start tuning prompts.

Push hard on *"replace every region, leave nothing flat"* and the model starts naturalising: in testing it collapsed 178 separate beach ridges into a single field patch, emptied the rest of the frame, and rotated the shoreline — a beautiful photograph of somewhere else. Push hard on *"preserve exactly"* and it plays safe, keeping the control colours as flat overlay strips.

What resolves it is pairing the two in the same breath rather than choosing between them: say what may change **and** what may not. The built-in preamble does this — *"change what a region is made of, never where it is: its texture changes, its outline does not"* — and adding a realism instruction to your own `prompt` without a matching constraint is what reintroduces the drift. Wording that invites reshaping (*"soft irregular natural edges"*) is the specific thing to avoid.

Zoom matters too. The same data at 6.3 m/pixel gives the model nothing to build texture from; at 1.7 m/pixel it can render crop rows, tracks and windbreaks, and the result reads as photography rather than illustration.

Always eyeball the control image against the output for a new ruleset — a render can look superb and still have quietly moved half your geometry. The dry-run recipe is at the end of this section.

#### Anchor grid (tiles)

Generating per tile would mean one API call per tile and a different hallucination in each one — hard seams everywhere. Instead, tile requests snap to a coarse cell (`anchor_zoom_delta` zooms up, so 4 → one generation per 16×16 block of tiles), generate once for the whole cell, and crop each tile out of it:

```
request z14/x8745/y5934
        |
        v  snap to anchor cell z10/x546/y370
  +-----------------+   <- ONE generation, cached
  | . . . . . . . . |
  | . . +--+ . . . .|      each small square is one tile,
  | . . |##| . . . .|      cropped from the shared image
  | . . +--+ . . . .|
  +-----------------+
```

The renderer fetches the features covering the whole cell (not just the requested tile) before building the control image, so the generation sees the real data for the area it covers. Cells are cached under `cache/sources/openrouter/anchors/` keyed by rule, symbolizer, cell and source revision; the generation itself is cached under `cache/sources/openrouter/gen/` keyed by model + prompt + control-image content. Two cells whose control images come out identical share one generation.

Seams can still appear at cell boundaries. `image.png` never anchors — it has one viewport and renders it directly.

#### Configuration

Set `OPENROUTER_API_KEY`, or copy `openrouter.example.json` → `openrouter.json` for project defaults (model, base URL, timeout). The environment wins for the key; the file wins for the defaults.

Failures degrade like `wms`: a missing key, a network error, a refusal or an undecodable payload drops the layer, warns on stderr, and lets the rest of the scene render. Because the generation cache is consulted *before* the key is required, a render that has run once stays reproducible offline.

#### Inspecting what gets sent

Dump the control image and prompt without spending anything — wrap `httpx.post`, write the request out, and hand back the control image as the response:

```python
import base64, httpx
from georender_service.app import renderer

def dry_post(url, *a, **kw):
    for part in kw["json"]["messages"][0]["content"]:
        if part["type"] == "text":
            open("prompt.txt", "w").write(part["text"])
        elif part["type"] == "image_url":
            _, _, b64 = part["image_url"]["url"].partition(",")
            open("control.png", "wb").write(base64.b64decode(b64))
    raise httpx.ConnectError("dry run")   # layer drops, render still completes

httpx.post = dry_post
renderer.render_png(geojson, "my-ruleset", width=1024, height=1024, ...)
```

Set any value for `OPENROUTER_API_KEY` first — the key check runs before the request, so without one you never reach the hook. Drop `raise` and forward to the real `httpx.post` to capture the request *and* generate in the same run.

Two traps worth knowing:

- **A dry run that returns a fake image poisons the cache.** The generation cache is content-addressed on (model, prompt, control image) and can't tell a stub from a real generation — a later real run will silently serve the stub. Clear `cache/sources/openrouter/gen/` before going live, and sanity-check the byte size: a real 1024px generation is ~2 MB, a flat control image ~30 KB.
- **Compare the control against the output, not just the output.** Registration failures are invisible if you only look at the result.

**Filter operators**: equality, `in`, `not_in`, `exists`, `gte`, `lte`.

## Assets

Assets are defined in `assets/assets.json` grouped into collections. A ruleset references them via `asset_collections`. Variant selection and randomization (rotation, flip, brightness/contrast jitter) are deterministic per position, so tiles stay visually stable across requests.

A `file` entry can be:

| Form | Loaded from |
|---|---|
| `tree.png` (bare / relative) | `assets/tree.png` on disk. |
| `https://…` / `http://…` | Downloaded once, cached under `cache/sources/assets/<sha>.<ext>`. |
| `github://<owner>/<repo>[@<ref>]/<path>` | Expanded to `cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>` and cached. `<ref>` defaults to `HEAD`. |
| `openrouter://<model>` | Generated from the entry's `prompt` and cached under `cache/sources/openrouter/gen/<sha>.png`. See below. |

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

### Generated assets (`openrouter://`)

An asset can be synthesized instead of loaded. The geometry stays exactly as precise as ever — Pillow still draws the shapes; only the artwork is generated, and the existing symbolizers stamp it unchanged.

The prompt interpolates `{property}` placeholders from the properties of the **feature actually being drawn**, which is what keeps generated artwork tied to the data:

```json
"collections": {
  "vt": {
    "shrine": {
      "file": "openrouter://google/gemini-2.5-flash-image",
      "kind": "icon",
      "prompt": "Map symbol for {name}, a {kind} of the {period} period. Ink-on-vellum, single centred glyph.",
      "transparent": true,
      "vary_by_seed": true
    },
    "dune_ground": {
      "file": "openrouter://google/gemini-2.5-flash-image",
      "kind": "texture",
      "prompt": "Top-down sandy dune ridge with dry coastal scrub, even lighting.",
      "tileable": true
    }
  }
}
```

| Field | Default | Notes |
|---|---|---|
| `prompt` | required | `{property}` placeholders resolve against the feature; unknown ones collapse to empty. |
| `vary_by_seed` | `false` | Off: one image per prompt, shared by every feature using it — one API call, and the right default for textures. On: a distinct image per stamp, so cost scales with the number of stamps. |
| `tileable` | `false` | Appends a seamless-tiling instruction. Pair with `polygon_texture`. |
| `transparent` | `false` | Appends a transparent-background instruction. Models honour it imperfectly. |
| `kind` | none | `icon` and `texture` add a framing instruction (centred glyph / flat swatch). |
| `cache` | `true` | Disk-cache the generation. |

Variants work normally: a `variant_set` can mix generated and on-disk files, and each variant may carry its own `prompt` refining the parent's.

Unlike `ai_image`, a failed generation here **raises** — a missing prompt or an unreachable API is a configuration error in an asset the scene depends on, so it surfaces as a 400 rather than silently rendering a hole.

## Static rendering

Besides the HTTP API, `scripts/static_render.py` produces a single PNG to a file. Three invocation forms:

```bash
# 1. Registered map from maps/*/timeline.json
python scripts/static_render.py valle-trebba valle-trebba out.png --bbox 12.103,44.695,12.127,44.718

# 2. Same, but with the explicit --map flag (the positional <ruleset> <output> form)
python scripts/static_render.py --map valle-trebba valle-trebba out.png --bbox 12.103,44.695,12.127,44.718

# 3. Ad-hoc geocontext repo — no maps/ entry required
python scripts/static_render.py \
    --mode geocontext --repository <owner>/<repo> \
    [--ref HEAD] [--manifest geocontext.json] [--layers Layer1,Layer2] \
    [<ruleset>] out.png
```

When a geocontext repo ships a `georender.json` bundle, the trailing `<ruleset>` positional becomes optional — the bundle's declared ruleset, asset collections, default bbox/base, and canvas size are used as defaults. CLI flags (`--bbox`, `--bbox-crs`, `--width`, `--height`, `--padding`) override the bundle when present.

### From the published Docker image

`scripts/static_render.py` ships inside the image, so the same commands run without a local Python install. Mount a host directory for the output PNG:

```bash
# Render a public geocontext repo using whatever its georender.json declares.
# Nothing local required beyond an output directory.
docker run --rm \
  -v "$(pwd)":/out \
  ghcr.io/openfantasymap/georender:main \
  python scripts/static_render.py \
    --mode geocontext --repository openhistorymap/valle_trebba \
    /out/valle-trebba.png

# Override canvas + bbox from the CLI (overrides win against the bundle).
docker run --rm \
  -v "$(pwd)":/out \
  ghcr.io/openfantasymap/georender:main \
  python scripts/static_render.py \
    --mode geocontext --repository openhistorymap/valle_trebba \
    --bbox 12.103,44.695,12.127,44.718 --width 2048 --height 2048 \
    /out/valle-trebba.png

# Render a registered map using your own maps/ and rulesets/ from the host.
docker run --rm \
  -v "$(pwd)/maps":/app/maps \
  -v "$(pwd)/rulesets":/app/rulesets \
  -v "$(pwd)":/out \
  ghcr.io/openfantasymap/georender:main \
  python scripts/static_render.py mymap mystyle /out/render.png \
    --bbox 9,43,12,46 --width 2048 --height 2048
```

Tips:
- Mount `-v "$(pwd)/cache":/app/cache` if you want the downloaded geocontext datasets, rulesets, and remote assets to persist across container runs — otherwise every invocation re-fetches them from jsDelivr.
- For PostGIS-backed maps, mount `connections.json` at `/app/connections.json` and make sure the container can reach your database host.
- `--mode geocontext` only needs network access to GitHub + jsDelivr; no `connections.json` or local map registry is required.
- For rulesets using `ai_image` or generated assets, pass `-e OPENROUTER_API_KEY` (or mount `openrouter.json` at `/app/openrouter.json`). Mounting `cache/` matters even more here: without it every run regenerates, and pays again.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn georender_service.app:app --reload
```

## Cache

Rendered tiles and images are cached on disk under `cache/`. The cache key includes the map slug, source revision, ruleset revision, and renderer version — so edits to a ruleset or timeline file automatically invalidate affected entries. To bust everything, bump `RENDERER_REVISION` in `georender_service/app.py`.

OpenRouter generations get two extra layers, both under `cache/sources/openrouter/`: `gen/` is content-addressed on (model, prompt, control image) and is what stops you paying twice for the same picture; `anchors/` maps (rule, symbolizer, anchor cell, source revision) to a generated cell, so a warm tile skips the source fetch and the control render entirely. Deleting `gen/` costs money to rebuild — deleting `anchors/` only costs time.

### Purging jsDelivr in geocontext repos

The `geocontext` source mode fetches the manifest, datasets, ruleset, and assets from `cdn.jsdelivr.net`, which edge-caches every file for 12 hours. When you push a change to a geocontext repo, downstream renderers keep seeing the stale copy until that TTL elapses.

`scripts/workflows/purge-jsdelivr.yml` is a drop-in GitHub Action that automates the purge. Copy it into any geocontext repo as `.github/workflows/purge-jsdelivr.yml` — no edits needed; it reads `${{ github.repository }}` and `${{ github.ref_name }}` from the workflow context. On every push to `main`/`master` it walks `git ls-files`, batches the paths through jsDelivr's bulk endpoint (100 paths per request), and purges both `@HEAD` and `@<branch>` aliases.

## License

Apache 2.0 — see [LICENSE](LICENSE).
