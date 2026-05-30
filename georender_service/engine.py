from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter
from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import (
    Viewport,
    ensure_mercator,
    geom_to_pixel,
    load_geom,
    mercator_bounds_for_features,
    safe_tangent_angle,
    viewport_from_bounds,
)
from .rules import RulesetStore, feature_matches


class AssetStore:
    REMOTE_PREFIXES = ("http://", "https://", "github://")

    def __init__(self, base_dir: str | Path, cache_dir: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.registry_path = self.base_dir / "assets.json"
        self.registry = self._load_registry()
        # Overlay registry of collections added at runtime (e.g. from a georender.json
        # bundle). Looked up before the on-disk registry, so a bundle can shadow or
        # extend the project's default collections for one render run.
        self._overlay_collections: dict[str, dict[str, Any]] = {}
        self._file_cache: dict[tuple[str, int], Image.Image] = {}
        self._materialized_cache: dict[tuple[str, int, int, float, bool, bool, float, float], Image.Image] = {}

    def register_overlay(self, collections: dict[str, Any]) -> None:
        """Merge ad-hoc collection definitions into the runtime overlay registry.

        Used by `georender.json` bundle loading: a remote bundle's `assets` block
        lands here so its collections take precedence over `assets/assets.json`
        for the lifetime of the AssetStore.
        """
        for name, defn in (collections or {}).items():
            self._overlay_collections[str(name)] = defn

    def clear_overlay(self) -> None:
        self._overlay_collections.clear()

    def _all_collections(self) -> dict[str, Any]:
        merged = dict(self.registry.get("collections", {}))
        merged.update(self._overlay_collections)
        return merged

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {"collections": {}}
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        data.setdefault("collections", {})
        return data

    def load(self, name: str, size_px: int | None = None) -> Image.Image:
        return self.load_for_ruleset(name, asset_collections=None, size_px=size_px)

    def load_for_ruleset(
        self,
        name: str,
        asset_collections: list[str] | dict[str, str] | None,
        size_px: int | None = None,
        seed: str | None = None,
    ) -> Image.Image:
        resolved_id, asset_def = self.resolve(name, asset_collections)
        return self._materialize_asset(resolved_id, asset_def, size_px=size_px, seed=seed)

    def resolve(
        self,
        name: str,
        asset_collections: list[str] | dict[str, str] | None,
    ) -> tuple[str, dict[str, Any]]:
        if not name:
            raise ValueError("Asset name is required")

        # Direct URI references skip the collection lookup entirely.
        if name.startswith(self.REMOTE_PREFIXES):
            return f"url:{name}", {"file": name}

        collections = self._all_collections()
        aliases = self._normalize_asset_collections(asset_collections)

        path = self.base_dir / name
        if path.exists():
            return f"file:{name}", {"file": name}

        if "." in name:
            prefix, asset_name = name.split(".", 1)
            collection_name = aliases.get(prefix, prefix)
            collection = collections.get(collection_name)
            if collection and asset_name in collection:
                return f"{collection_name}.{asset_name}", collection[asset_name]
            # Fallback: the ruleset aliased the prefix to a collection that
            # doesn't carry the asset, but a collection literally named `prefix`
            # does. This happens when a georender.json bundle declares a
            # collection under the same short name the ruleset uses as an alias
            # — e.g. ruleset says "vt": "valle_trebba" while the bundle ships
            # a collection called "vt" directly. Honour the literal prefix.
            if collection_name != prefix:
                direct = collections.get(prefix)
                if direct and asset_name in direct:
                    return f"{prefix}.{asset_name}", direct[asset_name]

        found: list[tuple[str, dict[str, Any]]] = []
        search_order = list(dict.fromkeys(list(aliases.values()) + list(collections.keys())))
        for collection_name in search_order:
            collection = collections.get(collection_name, {})
            if name in collection:
                found.append((f"{collection_name}.{name}", collection[name]))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ValueError(
                f"Asset '{name}' is ambiguous across collections; use a qualified reference like alias.{name}"
            )
        raise FileNotFoundError(f"Asset not found: {name}")

    def _normalize_asset_collections(self, asset_collections: list[str] | dict[str, str] | None) -> dict[str, str]:
        if asset_collections is None:
            return {}
        if isinstance(asset_collections, list):
            return {value: value for value in asset_collections}
        return {str(k): str(v) for k, v in asset_collections.items()}

    def _materialize_asset(
        self,
        resolved_id: str,
        asset_def: dict[str, Any],
        *,
        size_px: int | None,
        seed: str | None,
    ) -> Image.Image:
        seed_value = _stable_hash(seed or resolved_id)
        variant_index, variant = self._pick_variant(asset_def, seed_value)
        file_name = variant.get("file") or asset_def.get("file")
        if not file_name:
            raise ValueError(f"Asset '{resolved_id}' has no file or variants")

        randomization = {}
        randomization.update(asset_def.get("randomization") or {})
        randomization.update(variant.get("randomization") or {})

        rotation = _choose_rotation(randomization.get("rotation"), seed_value)
        flip_x = bool(randomization.get("flip_x")) and bool((seed_value >> 3) & 1)
        flip_y = bool(randomization.get("flip_y")) and bool((seed_value >> 4) & 1)
        brightness = _choose_jitter_factor(randomization.get("brightness_jitter"), seed_value, shift=5)
        contrast = _choose_jitter_factor(randomization.get("contrast_jitter"), seed_value, shift=13)

        materialized_key = (
            resolved_id,
            int(size_px or 0),
            variant_index,
            float(rotation),
            flip_x,
            flip_y,
            round(brightness, 4),
            round(contrast, 4),
        )
        if materialized_key in self._materialized_cache:
            return self._materialized_cache[materialized_key].copy()

        img = self._load_file(file_name, size_px=size_px)
        if rotation:
            img = img.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        if flip_x:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if flip_y:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if abs(brightness - 1.0) > 1e-6:
            img = ImageEnhance.Brightness(img).enhance(brightness)
        if abs(contrast - 1.0) > 1e-6:
            img = ImageEnhance.Contrast(img).enhance(contrast)

        self._materialized_cache[materialized_key] = img.copy()
        return img

    def _pick_variant(self, asset_def: dict[str, Any], seed_value: int) -> tuple[int, dict[str, Any]]:
        variants = asset_def.get("variants") or []
        if not variants:
            return 0, asset_def
        total = 0.0
        weights: list[float] = []
        for variant in variants:
            weight = float(variant.get("weight", 1))
            total += max(weight, 0.0)
            weights.append(max(weight, 0.0))
        if total <= 0:
            return 0, variants[0]
        needle = (seed_value % 10_000_000) / 10_000_000 * total
        acc = 0.0
        for idx, (variant, weight) in enumerate(zip(variants, weights, strict=False)):
            acc += weight
            if needle <= acc:
                return idx, variant
        return len(variants) - 1, variants[-1]

    def _load_file(self, name: str, size_px: int | None = None) -> Image.Image:
        key = (name, int(size_px or 0))
        if key in self._file_cache:
            return self._file_cache[key].copy()
        path = self._resolve_file_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Asset file not found: {name}")
        img = Image.open(path).convert("RGBA")
        if size_px:
            scale = size_px / max(img.width, img.height)
            w = max(1, int(round(img.width * scale)))
            h = max(1, int(round(img.height * scale)))
            img = img.resize((w, h), Image.LANCZOS)
        self._file_cache[key] = img.copy()
        return img

    def _resolve_file_path(self, name: str) -> Path:
        """Resolve an asset `file` reference to a local readable Path.

        Accepts:
          - bare names / relative paths → `base_dir / name` (existing behaviour).
          - `http://` and `https://` URLs → download to `cache_dir/assets/...`.
          - `github://<owner>/<repo>[@<ref>]/<path>` → expand to jsDelivr, then download.
        """
        if name.startswith("github://"):
            from .uris import expand_github_uri
            url = expand_github_uri(name)
            return self._fetch_remote_asset(url, source_ref=name)
        if name.startswith("http://") or name.startswith("https://"):
            return self._fetch_remote_asset(name, source_ref=name)
        return self.base_dir / name

    def _fetch_remote_asset(self, url: str, *, source_ref: str) -> Path:
        if not self.cache_dir:
            raise FileNotFoundError(
                f"Remote asset requested ({source_ref}) but AssetStore has no cache_dir; "
                "configure one in GeoRenderer to enable remote asset downloads."
            )
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        cache_path = self.cache_dir / "assets" / f"{digest}{suffix}"
        if cache_path.exists():
            return cache_path
        import httpx

        try:
            response = httpx.get(url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FileNotFoundError(f"Failed to fetch remote asset {source_ref}: {exc}") from exc
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        return cache_path


class GeoRenderer:
    def __init__(
        self,
        rules_dir: str | Path,
        assets_dir: str | Path,
        cache_dir: str | Path | None = None,
    ):
        self.rules = RulesetStore(rules_dir, cache_dir=cache_dir)
        self.assets = AssetStore(assets_dir, cache_dir=cache_dir)

    def render_png(
        self,
        geojson: dict[str, Any],
        ruleset_name: str,
        width: int,
        height: int,
        source_crs: str = "EPSG:4326",
        bbox: list[float] | None = None,
        padding_px: int = 32,
        bbox_crs: str = "EPSG:3857",
    ) -> bytes:
        ruleset = self.rules.load(ruleset_name)
        features = list(geojson.get("features", []))
        mercator_geoms = [ensure_mercator(load_geom(f), source_crs) for f in features]

        if bbox and len(bbox) == 4:
            from .geometry import ensure_mercator as _ensure
            from shapely.geometry import box
            bounds = _ensure(box(*bbox), bbox_crs).bounds
        else:
            bounds = mercator_bounds_for_features(mercator_geoms)
        viewport = viewport_from_bounds(bounds, width=width, height=height, padding_px=padding_px)
        image = self._render_scene(features, mercator_geoms, ruleset, viewport)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()

    def render_tile_image(
        self,
        features: list[dict[str, Any]],
        mercator_geoms: list[BaseGeometry],
        ruleset_name: str,
        viewport: Viewport,
    ) -> Image.Image:
        ruleset = self.rules.load(ruleset_name)
        return self._render_scene(features, mercator_geoms, ruleset, viewport)

    def _render_scene(
        self,
        features: list[dict[str, Any]],
        mercator_geoms: list[BaseGeometry],
        ruleset: dict[str, Any],
        viewport: Viewport,
    ) -> Image.Image:
        bg = ruleset.get("background", "#00000000")
        image = Image.new("RGBA", (viewport.width, viewport.height), _parse_color(bg))

        indexed = list(zip(features, mercator_geoms, strict=False))
        rules = sorted(ruleset.get("rules", []), key=lambda r: r.get("z_index", r.get("z", 0)))
        asset_collections = ruleset.get("asset_collections")
        for rule in rules:
            self._apply_rule(image, indexed, viewport, rule, asset_collections)
        return image

    def _apply_rule(
        self,
        image: Image.Image,
        indexed: list[tuple[dict[str, Any], BaseGeometry]],
        viewport: Viewport,
        rule: dict[str, Any],
        asset_collections: list[str] | dict[str, str] | None,
    ) -> None:
        symbolizer = rule["symbolizer"]
        stype = symbolizer["type"]
        rule_name = rule.get("name") or rule.get("id") or stype
        # Viewport-wide symbolizers don't iterate over features: they paint the
        # whole canvas once based on the viewport's bounds.
        if stype == "wms":
            self._render_wms(image, viewport, symbolizer, rule.get("edge_fade"), rule_name)
            return
        for feature_idx, (feature, world_geom) in enumerate(indexed):
            if world_geom.is_empty:
                continue
            geom_type = feature.get("geometry", {}).get("type")
            if not feature_matches(rule, feature, geom_type):
                continue
            pixel_geom = geom_to_pixel(world_geom, viewport)
            if stype == "icon":
                self._render_icon(image, pixel_geom, symbolizer, asset_collections, rule_name, feature_idx)
            elif stype == "polygon_fill":
                self._render_polygon_fill(image, pixel_geom, symbolizer, rule.get("edge_fade"))
            elif stype == "polygon_pattern":
                self._render_polygon_pattern(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx)
            elif stype == "polygon_texture":
                self._render_polygon_texture(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx)
            elif stype == "line_pattern":
                self._render_line_pattern(image, pixel_geom, symbolizer, rule.get("edge_fade"), asset_collections, rule_name, feature_idx)

    def _render_icon(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
    ) -> None:
        opacity = float(symbolizer.get("opacity", 1.0))

        points: list[Point] = []
        if isinstance(geom, Point):
            points = [geom]
        elif isinstance(geom, MultiPoint):
            points = list(geom.geoms)
        elif isinstance(geom, (Polygon, MultiPolygon)):
            points = [geom.representative_point()]

        for pt in points:
            seed = f"icon|{rule_name}|{feature_idx}|{pt.x:.2f}|{pt.y:.2f}"
            asset = self.assets.load_for_ruleset(
                symbolizer["asset"],
                asset_collections=asset_collections,
                size_px=symbolizer.get("size_px", 24),
                seed=seed,
            )
            asset = _apply_opacity(asset, opacity)
            x = int(round(pt.x - asset.width / 2))
            y = int(round(pt.y - asset.height / 2))
            image.alpha_composite(asset, dest=(x, y))

    def _render_polygon_fill(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer, "RGBA")
        color = _parse_color(symbolizer.get("fill", "#ffffff80"))
        for poly in _iter_polygons(geom):
            _draw_polygon(draw, poly, fill=color)
        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_polygon_pattern(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        spacing = int(symbolizer.get("spacing_px", 32))
        tile_offset_x = int(symbolizer.get("offset_x_px", 0))
        tile_offset_y = int(symbolizer.get("offset_y_px", 0))
        opacity = float(symbolizer.get("opacity", 1.0))

        if symbolizer.get("asset"):
            size_px = symbolizer.get("size_px", 24)
            probe = self.assets.load_for_ruleset(
                symbolizer["asset"],
                asset_collections=asset_collections,
                size_px=size_px,
                seed=f"probe|{rule_name}|{feature_idx}",
            )
            start_x = -probe.width
            end_x = image.size[0] + probe.width
            start_y = -probe.height
            end_y = image.size[1] + probe.height
            for x in range(start_x, end_x, spacing):
                for y in range(start_y, end_y, spacing):
                    seed = f"pattern|{rule_name}|{feature_idx}|{x}|{y}"
                    asset = self.assets.load_for_ruleset(
                        symbolizer["asset"],
                        asset_collections=asset_collections,
                        size_px=size_px,
                        seed=seed,
                    )
                    asset = _apply_opacity(asset, opacity)
                    layer.alpha_composite(asset, dest=(x + tile_offset_x, y + tile_offset_y))
        else:
            draw = ImageDraw.Draw(layer, "RGBA")
            fill = _parse_color(symbolizer.get("fill", "#ffffff55"))
            for x in range(0, image.size[0], spacing):
                for y in range(0, image.size[1], spacing):
                    r = max(1, int(symbolizer.get("dot_radius_px", 2)))
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_polygon_texture(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
    ) -> None:
        """Tile a photorealistic texture across a polygon.

        Unlike `polygon_pattern`, the variant + rotation + brightness/contrast jitter
        from the asset definition are resolved ONCE per feature (using a feature-stable
        seed) and the resulting tile is repeated across the polygon bbox aligned to a
        global pixel grid. That keeps adjacent stamps visually continuous instead of
        producing a per-tile mosaic.

        Symbolizer fields:
          - asset (required): tileable texture asset reference.
          - tile_size_px (default 128): nominal tile edge.
          - rotation: feature-level rotation. Same shape as asset `randomization.rotation`
            (0|90|180|270|true|[angles...]). Default 0.
          - tint: optional CSS/hex color (with alpha) multiplied over the texture.
            `#7a8c4a80` muddies toward a 50% green; `#ffffff00` is a no-op.
          - opacity (default 1.0).
        """
        if geom.is_empty:
            return
        if not symbolizer.get("asset"):
            return

        feature_seed = f"texture|{rule_name}|{feature_idx}"
        tile_size_px = int(symbolizer.get("tile_size_px", symbolizer.get("size_px", 128)))
        opacity = float(symbolizer.get("opacity", 1.0))
        rotation_spec = symbolizer.get("rotation", 0)
        tint = symbolizer.get("tint")

        seed_value = _stable_hash(feature_seed)
        rotation_deg = _resolve_texture_rotation(rotation_spec, seed_value)

        tile = self.assets.load_for_ruleset(
            symbolizer["asset"],
            asset_collections=asset_collections,
            size_px=tile_size_px,
            seed=feature_seed,
        )
        if rotation_deg:
            tile = tile.rotate(-rotation_deg, expand=True, resample=Image.BICUBIC)

        tw, th = tile.size
        if tw <= 0 or th <= 0:
            return

        poly_bounds = geom.bounds
        if not poly_bounds or len(poly_bounds) != 4:
            return
        minx, miny, maxx, maxy = poly_bounds

        layer_minx = max(0, int(minx) - tw)
        layer_miny = max(0, int(miny) - th)
        layer_maxx = min(image.size[0], int(maxx) + tw)
        layer_maxy = min(image.size[1], int(maxy) + th)
        if layer_maxx <= layer_minx or layer_maxy <= layer_miny:
            return

        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        # Align to a global grid so neighbouring polygons that share the same texture
        # + rotation tile through each other without re-phasing at the polygon edge.
        start_x = (layer_minx // tw) * tw
        start_y = (layer_miny // th) * th
        for y in range(start_y, layer_maxy + 1, th):
            for x in range(start_x, layer_maxx + 1, tw):
                layer.alpha_composite(tile, dest=(x, y))

        if tint:
            layer = _apply_tint(layer, _parse_color(tint))
        if opacity < 1.0:
            layer = _apply_opacity(layer, opacity)

        mask = self._mask_for_geometry(image.size, geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _render_wms(
        self,
        image: Image.Image,
        viewport: Viewport,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        rule_name: str,
    ) -> None:
        """Composite a WMS GetMap response onto the canvas.

        Fields on the symbolizer:
          - url (required): base WMS endpoint.
          - layers (required): comma-separated layer names.
          - version: WMS protocol version, default "1.3.0" ("1.1.1" also OK).
          - format: image MIME, default "image/png" ("image/jpeg" for opaque
            aerial photos is usually smaller + faster).
          - crs: requested SRS/CRS, default "EPSG:3857" (the renderer's native
            projection — avoids reprojection cost AND the 1.3.0 geographic-CRS
            axis-order footgun).
          - styles: default "" (server default).
          - transparent: default true (false for opaque photos).
          - opacity: default 1.0, applied to the final RGBA layer.
          - extra_params: free-form dict of additional query-string params
            (e.g. ArcGIS-specific overrides).
          - cache: default true. Set false for layers whose upstream content
            changes (the 1976 aerofoto is fine to cache forever; weather isn't).
        """
        url = symbolizer.get("url")
        layers = symbolizer.get("layers")
        if not url or not layers:
            return  # silently skip — keeps a partially-configured ruleset renderable

        version = str(symbolizer.get("version", "1.3.0"))
        crs = str(symbolizer.get("crs", "EPSG:3857")).upper()
        bbox_str = _wms_bbox_for_viewport(viewport, crs, version)
        request_url = _wms_get_map_url(
            url,
            version=version,
            layers=str(layers),
            styles=str(symbolizer.get("styles", "")),
            crs=crs,
            bbox=bbox_str,
            width=viewport.width,
            height=viewport.height,
            fmt=str(symbolizer.get("format", "image/png")),
            transparent=bool(symbolizer.get("transparent", True)),
            extra=symbolizer.get("extra_params") or {},
        )

        png_bytes = self._fetch_wms(request_url, use_cache=bool(symbolizer.get("cache", True)))
        if png_bytes is None:
            return

        try:
            layer_img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        except Exception:
            # Servers sometimes return an HTML error masquerading as image/png.
            # Don't crash the whole render — drop this layer.
            return

        if layer_img.size != (viewport.width, viewport.height):
            layer_img = layer_img.resize((viewport.width, viewport.height), Image.LANCZOS)

        opacity = float(symbolizer.get("opacity", 1.0))
        if opacity < 1.0:
            layer_img = _apply_opacity(layer_img, opacity)

        if edge_fade and edge_fade.get("distance_px", 0) > 0:
            radius = float(edge_fade["distance_px"])
            mask = Image.new("L", layer_img.size, 255)
            mask = mask.filter(ImageFilter.GaussianBlur(radius=radius / 2))
            existing_alpha = layer_img.getchannel("A")
            layer_img.putalpha(ImageChopsMultiply(existing_alpha, mask))

        image.alpha_composite(layer_img)

    def _fetch_wms(self, url: str, *, use_cache: bool) -> bytes | None:
        """Fetch a WMS response, optionally backed by the on-disk asset cache."""
        cache_path: Path | None = None
        if use_cache and self.assets.cache_dir is not None:
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            cache_path = self.assets.cache_dir / "wms" / f"{digest}.bin"
            if cache_path.exists():
                return cache_path.read_bytes()

        import httpx

        try:
            response = httpx.get(url, timeout=60.0, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        data = response.content
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        return data

    def _render_line_pattern(
        self,
        image: Image.Image,
        geom: BaseGeometry,
        symbolizer: dict[str, Any],
        edge_fade: dict[str, Any] | None,
        asset_collections: list[str] | dict[str, str] | None,
        rule_name: str,
        feature_idx: int,
    ) -> None:
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        spacing = float(symbolizer.get("spacing_px", 20))
        base_size_px = symbolizer.get("size_px", 16)
        buffer_px = float(symbolizer.get("buffer_px", base_size_px / 2))
        cross_step_px = float(symbolizer.get("cross_step_px", max(8, buffer_px)))
        rotate = bool(symbolizer.get("rotate", True))
        opacity = float(symbolizer.get("opacity", 1.0))
        probe = self.assets.load_for_ruleset(
            symbolizer["asset"],
            asset_collections=asset_collections,
            size_px=base_size_px,
            seed=f"probe|line|{rule_name}|{feature_idx}",
        )

        offsets = [0.0]
        if buffer_px > probe.height * 0.75:
            max_offset = max(0.0, buffer_px - probe.height / 2)
            rails = int(max_offset // max(cross_step_px, 1))
            offsets = [0.0]
            for i in range(1, rails + 1):
                offsets.extend([i * cross_step_px, -i * cross_step_px])
            offsets = [o for o in offsets if abs(o) <= max_offset + 1e-6]

        line_mask_geom = geom.buffer(buffer_px, cap_style=2, join_style=2)
        for line_idx, line in enumerate(_iter_lines(geom)):
            for offset in offsets:
                target_line = _offset_line(line, offset)
                if target_line.is_empty:
                    continue
                length = target_line.length
                if length <= 0:
                    continue
                d = 0.0
                while d <= length:
                    pt = target_line.interpolate(d)
                    seed = f"line|{rule_name}|{feature_idx}|{line_idx}|{offset:.2f}|{d:.2f}"
                    stamp = self.assets.load_for_ruleset(
                        symbolizer["asset"],
                        asset_collections=asset_collections,
                        size_px=base_size_px,
                        seed=seed,
                    )
                    if rotate:
                        angle = safe_tangent_angle(target_line, d)
                        stamp = stamp.rotate(-angle, expand=True, resample=Image.BICUBIC)
                    stamp = _apply_opacity(stamp, opacity)
                    x = int(round(pt.x - stamp.width / 2))
                    y = int(round(pt.y - stamp.height / 2))
                    layer.alpha_composite(stamp, dest=(x, y))
                    d += spacing

        mask = self._mask_for_geometry(image.size, line_mask_geom, edge_fade)
        layer.putalpha(ImageChopsMultiply(layer.getchannel("A"), mask))
        image.alpha_composite(layer)

    def _mask_for_geometry(
        self,
        size: tuple[int, int],
        geom: BaseGeometry,
        edge_fade: dict[str, Any] | None,
    ) -> Image.Image:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask, "L")
        for poly in _iter_polygons(geom):
            _draw_polygon(draw, poly, fill=255)
        if edge_fade and edge_fade.get("distance_px", 0) > 0:
            radius = float(edge_fade.get("distance_px", 0))
            mask = mask.filter(ImageFilter.GaussianBlur(radius=radius / 2))
        return mask


def ImageChopsMultiply(a: Image.Image, b: Image.Image) -> Image.Image:
    return ImageChops.multiply(a, b)


def _parse_color(value: str | tuple[int, int, int] | tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, tuple):
        if len(value) == 3:
            return (*value, 255)
        return value
    rgba = ImageColor.getcolor(value, "RGBA")
    return rgba


from .uris import expand_github_uri as _expand_github_uri  # re-exported for tests


def _wms_bbox_for_viewport(viewport: Viewport, crs: str, version: str) -> str:
    """Build the BBOX query string for a WMS GetMap matching the viewport.

    The viewport is always EPSG:3857. If the WMS server speaks a different CRS
    we reproject the corners. WMS 1.3.0 also flipped the axis order for
    geographic CRSes (EPSG:4326 / CRS:84 → lat,lon instead of lon,lat) — handle
    that here so the symbolizer config doesn't have to.
    """
    minx, miny, maxx, maxy = viewport.minx, viewport.miny, viewport.maxx, viewport.maxy
    crs_upper = crs.upper()
    if crs_upper not in ("EPSG:3857", "EPSG:900913"):
        try:
            from pyproj import Transformer

            transformer = Transformer.from_crs("EPSG:3857", crs_upper, always_xy=True)
            minx, miny = transformer.transform(viewport.minx, viewport.miny)
            maxx, maxy = transformer.transform(viewport.maxx, viewport.maxy)
        except Exception:
            # Fall back to passing the raw mercator numbers — the server will
            # reject the request loudly, which is the right failure mode.
            pass
    if version == "1.3.0" and crs_upper in ("EPSG:4326", "CRS:84"):
        return f"{miny},{minx},{maxy},{maxx}"
    return f"{minx},{miny},{maxx},{maxy}"


def _wms_get_map_url(
    base: str,
    *,
    version: str,
    layers: str,
    styles: str,
    crs: str,
    bbox: str,
    width: int,
    height: int,
    fmt: str,
    transparent: bool,
    extra: dict[str, Any],
) -> str:
    from urllib.parse import urlencode

    params: dict[str, str] = {
        "SERVICE": "WMS",
        "VERSION": version,
        "REQUEST": "GetMap",
        "LAYERS": layers,
        "STYLES": styles,
        # WMS 1.1.1 calls it SRS; 1.3.0 calls it CRS. Send both — extra ones
        # are harmless and some servers are picky.
        ("CRS" if version == "1.3.0" else "SRS"): crs,
        "BBOX": bbox,
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": fmt,
        "TRANSPARENT": "TRUE" if transparent else "FALSE",
    }
    for k, v in extra.items():
        params[str(k).upper()] = str(v)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(params)}"


def _apply_tint(layer: Image.Image, tint_rgba: tuple[int, int, int, int]) -> Image.Image:
    """Multiply the layer's RGB by `tint_rgba`, weighted by the tint's alpha.

    Alpha 255 = full multiply (image fully tinted). Alpha 0 = passthrough.
    The layer's own alpha channel is preserved unchanged.
    """
    r, g, b, a = tint_rgba
    strength = a / 255.0
    if strength <= 0.0:
        return layer
    base_rgb = layer.convert("RGB")
    tint_solid = Image.new("RGB", layer.size, (r, g, b))
    multiplied = ImageChops.multiply(base_rgb, tint_solid)
    blended = Image.blend(base_rgb, multiplied, strength)
    return Image.merge("RGBA", (*blended.split(), layer.getchannel("A")))


def _resolve_texture_rotation(rotation_spec: Any, seed_value: int) -> float:
    """Same shape as asset randomization.rotation, but returns one stable angle."""
    if rotation_spec is None or rotation_spec is False:
        return 0.0
    if rotation_spec is True:
        opts = [0.0, 90.0, 180.0, 270.0]
        return opts[seed_value % len(opts)]
    if isinstance(rotation_spec, list):
        opts = [float(v) for v in rotation_spec]
        return opts[seed_value % len(opts)] if opts else 0.0
    if isinstance(rotation_spec, (int, float)):
        return float(rotation_spec)
    return 0.0


def _apply_opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 1.0:
        return img
    out = img.copy()
    alpha = out.getchannel("A").point(lambda p: int(p * opacity))
    out.putalpha(alpha)
    return out


def _stable_hash(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)


def _choose_rotation(rotation_spec: Any, seed_value: int) -> float:
    if not rotation_spec:
        return 0.0
    if isinstance(rotation_spec, list):
        values = [float(v) for v in rotation_spec]
        return values[seed_value % len(values)] if values else 0.0
    if rotation_spec is True:
        values = [0.0, 90.0, 180.0, 270.0]
        return values[seed_value % len(values)]
    return 0.0


def _choose_jitter_factor(amount: Any, seed_value: int, *, shift: int) -> float:
    if not amount:
        return 1.0
    amt = abs(float(amount))
    raw = ((seed_value >> shift) & 1023) / 1023.0
    centered = (raw * 2.0) - 1.0
    return 1.0 + (centered * amt)


def _iter_polygons(geom: BaseGeometry) -> Iterable[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, (GeometryCollection,)):
        polys: list[Polygon] = []
        for g in geom.geoms:
            polys.extend(_iter_polygons(g))
        return polys
    return []


def _iter_lines(geom: BaseGeometry) -> Iterable[BaseGeometry]:
    if geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if isinstance(geom, GeometryCollection):
        lines: list[BaseGeometry] = []
        for g in geom.geoms:
            lines.extend(_iter_lines(g))
        return lines
    return []


def _draw_polygon(draw: ImageDraw.ImageDraw, poly: Polygon, fill: Any) -> None:
    exterior = [(x, y) for x, y in poly.exterior.coords]
    draw.polygon(exterior, fill=fill)
    for hole in poly.interiors:
        coords = [(x, y) for x, y in hole.coords]
        draw.polygon(coords, fill=0)


def _offset_line(line: BaseGeometry, offset: float) -> BaseGeometry:
    if abs(offset) < 1e-6:
        return line
    side = "left" if offset > 0 else "right"
    shifted = line.parallel_offset(abs(offset), side=side, join_style=2)
    if shifted.geom_type == "MultiLineString":
        longest = max(shifted.geoms, key=lambda g: g.length, default=None)
        return longest if longest is not None else GeometryCollection()
    return shifted
