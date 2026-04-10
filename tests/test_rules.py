from __future__ import annotations

import json

import pytest

from georender_service.rules import RulesetError, RulesetStore, feature_matches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _feature(geom_type: str, **props) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": []},
        "properties": props,
    }


def _rule(geom_types: list, filter_: dict = None, **extra) -> dict:
    return {
        "geometry": geom_types,
        "filter": filter_ or {},
        "symbolizer": {"type": "polygon_fill"},
        **extra,
    }


# ---------------------------------------------------------------------------
# feature_matches — geometry gating
# ---------------------------------------------------------------------------


def test_feature_matches_wrong_geometry_type():
    rule = _rule(["Point"])
    assert not feature_matches(rule, _feature("Polygon", kind="water"), "Polygon")


def test_feature_matches_correct_geometry_type():
    rule = _rule(["Polygon"])
    assert feature_matches(rule, _feature("Polygon", kind="water"), "Polygon")


def test_feature_matches_multi_geometry_types():
    rule = _rule(["Polygon", "MultiPolygon"])
    assert feature_matches(rule, _feature("MultiPolygon"), "MultiPolygon")


# ---------------------------------------------------------------------------
# feature_matches — filter operators
# ---------------------------------------------------------------------------


def test_feature_matches_no_filter():
    rule = _rule(["Point"], filter_={})
    assert feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_equality_hit():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_equality_miss():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert not feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_equality_missing_prop():
    rule = _rule(["Point"], filter_={"kind": "city"})
    assert not feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_in_operator_hit():
    rule = _rule(["Point"], filter_={"kind": {"in": ["city", "town"]}})
    assert feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_in_operator_miss():
    rule = _rule(["Point"], filter_={"kind": {"in": ["city", "town"]}})
    assert not feature_matches(rule, _feature("Point", kind="village"), "Point")


def test_feature_matches_not_in_operator_hit():
    rule = _rule(["Point"], filter_={"kind": {"not_in": ["city"]}})
    assert feature_matches(rule, _feature("Point", kind="town"), "Point")


def test_feature_matches_not_in_operator_miss():
    rule = _rule(["Point"], filter_={"kind": {"not_in": ["city"]}})
    assert not feature_matches(rule, _feature("Point", kind="city"), "Point")


def test_feature_matches_exists_true():
    rule = _rule(["Point"], filter_={"name": {"exists": True}})
    assert feature_matches(rule, _feature("Point", name="Alpha"), "Point")


def test_feature_matches_exists_true_miss():
    rule = _rule(["Point"], filter_={"name": {"exists": True}})
    assert not feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_exists_false():
    rule = _rule(["Point"], filter_={"name": {"exists": False}})
    assert feature_matches(rule, _feature("Point"), "Point")


def test_feature_matches_gte_hit():
    rule = _rule(["Point"], filter_={"pop": {"gte": 1000}})
    assert feature_matches(rule, _feature("Point", pop=5000), "Point")


def test_feature_matches_gte_miss():
    rule = _rule(["Point"], filter_={"pop": {"gte": 1000}})
    assert not feature_matches(rule, _feature("Point", pop=500), "Point")


def test_feature_matches_lte_hit():
    rule = _rule(["Point"], filter_={"level": {"lte": 5}})
    assert feature_matches(rule, _feature("Point", level=3), "Point")


def test_feature_matches_lte_miss():
    rule = _rule(["Point"], filter_={"level": {"lte": 5}})
    assert not feature_matches(rule, _feature("Point", level=10), "Point")


def test_feature_matches_multiple_conditions_all_must_pass():
    rule = _rule(["Point"], filter_={"kind": "city", "pop": {"gte": 1000}})
    assert feature_matches(rule, _feature("Point", kind="city", pop=2000), "Point")
    assert not feature_matches(rule, _feature("Point", kind="city", pop=500), "Point")
    assert not feature_matches(rule, _feature("Point", kind="town", pop=2000), "Point")


# ---------------------------------------------------------------------------
# RulesetStore — load / list / revision
# ---------------------------------------------------------------------------


def test_ruleset_store_list_names(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    assert "test" in store.list_names()


def test_ruleset_store_load_returns_dict(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    data = store.load("test")
    assert isinstance(data, dict)
    assert "rules" in data


def test_ruleset_store_load_not_found_raises(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    with pytest.raises(RulesetError, match="not found"):
        store.load("nonexistent")


def test_ruleset_store_revision_is_string(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    rev = store.revision("test")
    assert isinstance(rev, str) and len(rev) == 16


def test_ruleset_store_revision_changes_on_edit(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    rev1 = store.revision("test")
    ruleset_path = tmp_ruleset_dir / "test.json"
    data = json.loads(ruleset_path.read_text())
    data["background"] = "#000000"
    ruleset_path.write_text(json.dumps(data))
    rev2 = store.revision("test")
    assert rev1 != rev2


def test_ruleset_store_revision_not_found_raises(tmp_ruleset_dir):
    store = RulesetStore(tmp_ruleset_dir)
    with pytest.raises(RulesetError):
        store.revision("ghost")


# ---------------------------------------------------------------------------
# RulesetStore — legacy key normalization
# ---------------------------------------------------------------------------


def _write_ruleset(path, ruleset: dict) -> None:
    path.write_text(json.dumps(ruleset), encoding="utf-8")


def test_normalize_paint_to_symbolizer(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "paint": {"type": "polygon_fill", "fill": "#ff0000"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert "symbolizer" in data["rules"][0]
    assert "paint" not in data["rules"][0]


def test_normalize_z_to_z_index(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "z": 5,
                    "symbolizer": {"type": "polygon_fill"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert data["rules"][0]["z_index"] == 5


def test_normalize_where_to_filter(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "compat.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "where": {"all": [{"field": "kind", "in": ["water"]}]},
                    "symbolizer": {"type": "polygon_fill"},
                }
            ]
        },
    )
    data = store.load("compat")
    assert "filter" in data["rules"][0]
    assert "kind" in data["rules"][0]["filter"]


# ---------------------------------------------------------------------------
# RulesetStore — validation errors
# ---------------------------------------------------------------------------


def test_validate_missing_rules_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(tmp_path / "bad.json", {"background": "#fff"})
    with pytest.raises(RulesetError, match="rules"):
        store.load("bad")


def test_validate_bad_symbolizer_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {"rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "explode"}}]},
    )
    with pytest.raises(RulesetError, match="symbolizer"):
        store.load("bad")


def test_validate_bad_geometry_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {"rules": [{"geometry": ["Triangle"], "symbolizer": {"type": "polygon_fill"}}]},
    )
    with pytest.raises(RulesetError, match="geometry"):
        store.load("bad")


def test_validate_negative_edge_fade_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {
            "rules": [
                {
                    "geometry": ["Polygon"],
                    "symbolizer": {"type": "polygon_fill"},
                    "edge_fade": {"distance_px": -1},
                }
            ]
        },
    )
    with pytest.raises(RulesetError, match="edge_fade"):
        store.load("bad")


def test_validate_asset_collections_bad_type_raises(tmp_path):
    store = RulesetStore(tmp_path)
    _write_ruleset(
        tmp_path / "bad.json",
        {
            "asset_collections": "not-a-list-or-dict",
            "rules": [{"geometry": ["Polygon"], "symbolizer": {"type": "polygon_fill"}}],
        },
    )
    with pytest.raises(RulesetError, match="asset_collections"):
        store.load("bad")
