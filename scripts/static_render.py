#!/usr/bin/env python3
"""Render a map statically — either a registered map (via the FastAPI app) or
an ad-hoc geocontext repo (resolved on the fly, no maps/*/timeline.json needed).

Registered (back-compat):
    python scripts/static_render.py <map_slug> <ruleset> <output.png> [options]
    python scripts/static_render.py --map <slug> <ruleset> <output.png> [options]

Ad-hoc geocontext (no timeline.json required):
    python scripts/static_render.py --mode geocontext \\
        --repository <owner>/<repo> [--ref <ref>] [--manifest <name>] \\
        [--layers Tombe,Dossi] <ruleset> <output.png> --bbox ...
"""
import argparse
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Two positional forms. arg3 is optional so the legacy 3-positional UX
    # (<map_slug> <ruleset> <output>) still works; the new UX uses 2
    # positionals (<ruleset> <output>) plus --map or --mode flags.
    parser.add_argument("arg1")
    parser.add_argument("arg2", nargs="?", default=None)
    parser.add_argument("arg3", nargs="?", default=None)

    parser.add_argument("--map", default=None, help="registered map slug (alt to first positional)")
    parser.add_argument(
        "--mode",
        choices=["geocontext"],
        default=None,
        help="ad-hoc source mode (skips the maps/*/timeline.json registry)",
    )
    parser.add_argument("--repository", default=None, help="<owner>/<repo> (with --mode geocontext)")
    parser.add_argument("--ref", default="HEAD", help="git ref for --mode geocontext (default HEAD)")
    parser.add_argument("--manifest", default=None, help="manifest filename (default geocontext.json → gcx.json)")
    parser.add_argument(
        "--layers",
        default=None,
        help="comma-separated layer whitelist (geocontext only); omit to render all data-driven layers",
    )

    parser.add_argument("--bbox", default=None, help="bounding box 'minx,miny,maxx,maxy'")
    parser.add_argument("--bbox-crs", default="EPSG:4326", help="CRS of --bbox (default EPSG:4326)")
    # Default to None so we can tell "user didn't pass it" from "user passed 1024
    # explicitly" — the difference matters when a georender.json bundle wants to
    # provide its own canvas defaults.
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--padding", type=int, default=None)
    return parser, parser.parse_args(argv)


# Final defaults applied when neither the CLI nor a bundle supplies a value.
_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 1024
_DEFAULT_PADDING = 32


def _resolve_positional(parser, args):
    """Resolve the positional forms into (map_slug_or_None, ruleset_or_None, output).

    Forms supported:
      A. legacy 3-positional registered:  <map_slug> <ruleset> <output>
      B. --map <slug> <ruleset> <output>
      C. --mode geocontext --repository <owner>/<repo> <ruleset> <output>
      D. --mode geocontext --repository <owner>/<repo> <output>
         (ruleset comes from the repo's georender.json bundle)
    """
    if args.arg3 is not None:
        # 3 positionals — legacy form requires no --map/--mode.
        if args.map or args.mode:
            parser.error(
                "cannot mix positional <map_slug> with --map / --mode; "
                "use the 2-positional form (<ruleset> <output>) when passing flags"
            )
        return args.arg1, args.arg2, args.arg3
    if args.mode == "geocontext":
        if not args.repository:
            parser.error("--mode geocontext requires --repository <owner>/<repo>")
        if args.arg2 is not None:
            # Form C: <ruleset> <output>
            return None, args.arg1, args.arg2
        # Form D: just <output>; ruleset will come from georender.json.
        return None, None, args.arg1
    if args.arg2 is None:
        parser.error("registered mode requires <ruleset> <output>")
    if args.map:
        return args.map, args.arg1, args.arg2
    parser.error(
        "missing source: pass either a positional <map_slug>, --map <slug>, "
        "or --mode geocontext --repository <owner>/<repo>"
    )


def _render_registered(map_slug, ruleset, output, args):
    from georender_service.app import app  # noqa: E402

    width = args.width if args.width is not None else _DEFAULT_WIDTH
    height = args.height if args.height is not None else _DEFAULT_HEIGHT
    padding = args.padding if args.padding is not None else _DEFAULT_PADDING
    args.width, args.height, args.padding = width, height, padding
    params = {
        "width": str(width),
        "height": str(height),
        "padding_px": str(padding),
    }
    if args.bbox:
        params["bbox"] = args.bbox
        params["bbox_crs"] = args.bbox_crs
    client = TestClient(app)
    url = "/{}/{}/image.png".format(map_slug, ruleset)
    response = client.get(url, params=params)
    if response.status_code != 200:
        sys.stderr.write(
            "FAIL {} {} :: {}\n".format(response.status_code, url, response.text[:400])
        )
        return 1
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(response.content)
    print("OK wrote {} bytes to {} ({}x{})".format(
        len(response.content), out, args.width, args.height
    ))
    return 0


def _render_geocontext(ruleset, output, args):
    from shapely.geometry import box as _box

    from georender_service.app import CACHE_DIR, renderer  # noqa: E402
    from georender_service.geometry import (  # noqa: E402
        WEB_MERCATOR_BOUNDS,
        ensure_mercator,
        expand_bounds_pixels,
        mercator_bounds_for_features,
        mercator_bounds_from_center_zoom,
        load_geom,
    )
    from georender_service.sources import GeocontextAdapter, SourceDefinition  # noqa: E402

    owner, _, repo = args.repository.partition("/")
    if not (owner and repo):
        sys.stderr.write("--repository must be of the form <owner>/<repo>\n")
        return 2

    src_data = {"name": "ad-hoc", "url": "/ad-hoc", "mode": "geocontext", "owner": owner, "repo": repo}
    if args.ref:
        src_data["ref"] = args.ref
    if args.manifest:
        src_data["manifest"] = args.manifest
    if args.layers:
        src_data["layers"] = [s.strip() for s in args.layers.split(",") if s.strip()]

    # SourceDefinition.path is used only for its `revision` property — which
    # GeocontextAdapter doesn't read — so a synthetic path is safe.
    source = SourceDefinition(slug="ad-hoc", data=src_data, path=Path("/dev/null"))
    adapter = GeocontextAdapter(cache_dir=CACHE_DIR / "sources")

    # Try to load georender.json. It carries defaults for ruleset / assets /
    # canvas / extent; CLI flags below still take precedence.
    bundle = adapter.fetch_render_config(source)

    # Apply bundle-derived overrides to the source config (manifest / layers).
    if bundle:
        gc_overrides = bundle.get("geocontext") or {}
        if not args.manifest and gc_overrides.get("manifest"):
            src_data["manifest"] = gc_overrides["manifest"]
        if not args.layers and gc_overrides.get("layers"):
            src_data["layers"] = list(gc_overrides["layers"])
        # Re-build source after override so the adapter sees the merged config.
        source = SourceDefinition(slug="ad-hoc", data=src_data, path=Path("/dev/null"))

        # Default render canvas from the bundle when the user didn't pass an
        # explicit CLI flag. (None = not passed.)
        render_defaults = bundle.get("render") or {}
        if args.width is None and render_defaults.get("width"):
            args.width = int(render_defaults["width"])
        if args.height is None and render_defaults.get("height"):
            args.height = int(render_defaults["height"])
        if args.padding is None and render_defaults.get("padding_px") is not None:
            args.padding = int(render_defaults["padding_px"])

        # Register the bundle's asset collections so the ruleset can reference them.
        bundle_assets = bundle.get("assets")
        if isinstance(bundle_assets, dict):
            renderer.assets.register_overlay(bundle_assets)

        # Resolve the ruleset from the bundle when the user didn't supply one.
        if ruleset is None:
            ruleset = _install_bundle_ruleset(renderer, adapter, source, bundle)
            if ruleset is None:
                sys.stderr.write(
                    "georender.json found but no ruleset declared; pass <ruleset> on the CLI\n"
                )
                return 1
    elif ruleset is None:
        sys.stderr.write(
            "no georender.json in {repo}; pass <ruleset> on the CLI explicitly\n".format(
                repo=args.repository
            )
        )
        return 1

    # Apply hard defaults to any canvas dimension still unset after CLI + bundle.
    if args.width is None:
        args.width = _DEFAULT_WIDTH
    if args.height is None:
        args.height = _DEFAULT_HEIGHT
    if args.padding is None:
        args.padding = _DEFAULT_PADDING

    # Resolve render bounds. Precedence: CLI --bbox → bundle.bbox (EPSG:4326 W,S,E,N)
    # → bundle.base center+zoom → fit to all features.
    merc_bounds = None
    if args.bbox:
        try:
            values = [float(v) for v in args.bbox.split(",")]
        except ValueError:
            sys.stderr.write("--bbox must be four comma-separated numbers\n")
            return 2
        if len(values) != 4:
            sys.stderr.write("--bbox must have 4 values\n")
            return 2
        merc_bounds = ensure_mercator(_box(*values), args.bbox_crs).bounds
    elif bundle and isinstance(bundle.get("bbox"), list) and len(bundle["bbox"]) == 4:
        try:
            values = [float(v) for v in bundle["bbox"]]
            merc_bounds = ensure_mercator(_box(*values), "EPSG:4326").bounds
        except (TypeError, ValueError) as exc:
            sys.stderr.write("georender.json `bbox` must be 4 numbers (W,S,E,N): {}\n".format(exc))
            return 2
    elif bundle and bundle.get("base"):
        base = bundle["base"]
        if all(k in base for k in ("lat", "lng", "zoom")):
            merc_bounds = mercator_bounds_from_center_zoom(
                lng=float(base["lng"]),
                lat=float(base["lat"]),
                zoom=float(base["zoom"]),
                width=args.width,
                height=args.height,
            )

    fetch_bounds = merc_bounds if merc_bounds else WEB_MERCATOR_BOUNDS
    fetched = adapter.fetch_for_bounds(source, fetch_bounds)

    if not merc_bounds:
        geoms = [ensure_mercator(load_geom(f), fetched.source_crs) for f in fetched.features]
        merc_bounds = mercator_bounds_for_features(geoms)

    # Pad the canvas slightly so edge_fade has room to fall off.
    padded = expand_bounds_pixels(merc_bounds, args.width, args.height, args.padding)
    # Re-fetch only if a wider bbox could pick up more features (no-op for `--bbox`
    # callers when the manifest is fully contained in the requested window).
    if padded != merc_bounds and merc_bounds is not fetch_bounds:
        fetched = adapter.fetch_for_bounds(source, padded)

    print(
        "  bundle={} features={}  layers={}".format(
            "yes" if bundle else "no",
            len(fetched.features),
            sorted({f["properties"].get("__layer") for f in fetched.features}),
        )
    )

    png = renderer.render_png(
        geojson={"type": "FeatureCollection", "features": fetched.features},
        ruleset_name=ruleset,
        width=args.width,
        height=args.height,
        source_crs=fetched.source_crs,
        bbox=list(merc_bounds),
        padding_px=args.padding,
    )
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print("OK wrote {} bytes to {} ({}x{})".format(
        len(png), out, args.width, args.height
    ))
    return 0


_BUNDLE_INLINE_RULESET_NAME = "__georender_bundle__"


def _install_bundle_ruleset(renderer, adapter, source, bundle):
    """Install the bundle-declared ruleset into the live RulesetStore.

    `bundle.ruleset` is either:
      - an inline ruleset dict (registered as-is via register_inline)
      - a string path inside the repo (fetched via jsDelivr, then registered)

    Returns the name that subsequent `renderer.render_png(..., ruleset_name=...)`
    calls should use, or None if the bundle doesn't declare a ruleset.
    """
    import json as _json

    decl = bundle.get("ruleset")
    if decl is None:
        return None
    repo = bundle.get("_repo") or {}
    owner = repo.get("owner") or source.data.get("owner")
    repo_name = repo.get("repo") or source.data.get("repo")
    ref = repo.get("ref") or source.data.get("ref") or "HEAD"
    if isinstance(decl, dict):
        renderer.rules.register_inline(_BUNDLE_INLINE_RULESET_NAME, decl)
        return _BUNDLE_INLINE_RULESET_NAME
    if isinstance(decl, str):
        payload = adapter._fetch_asset_path(owner, repo_name, ref, decl)
        data = _json.loads(payload.decode("utf-8"))
        renderer.rules.register_inline(_BUNDLE_INLINE_RULESET_NAME, data)
        return _BUNDLE_INLINE_RULESET_NAME
    sys.stderr.write(
        "georender.json `ruleset` must be a path string or an inline object; got {}\n".format(
            type(decl).__name__
        )
    )
    return None


def main(argv):
    parser, args = parse_args(argv)
    map_slug, ruleset, output = _resolve_positional(parser, args)
    if args.mode == "geocontext":
        return _render_geocontext(ruleset, output, args)
    return _render_registered(map_slug, ruleset, output, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
