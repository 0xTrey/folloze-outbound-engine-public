import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.folloze_asset_catalog import normalize_asset_title, resolve_asset_by_title  # noqa: E402


def test_normalize_asset_title_handles_dash_variants():
    assert normalize_asset_title("Build a Campaign from a Brief - Campaign Agent") == normalize_asset_title(
        "Build a Campaign from a Brief — Campaign Agent"
    )


def test_resolve_asset_by_title_prefers_exact_visible_title():
    assets = {
        "2211717": {
            "asset_id": "2211717",
            "board_id": "238181",
            "title": "Build a Campaign from a Brief - Campaign Agent",
            "title_aliases": [],
        },
        "2211727": {
            "asset_id": "2211727",
            "board_id": "238181",
            "title": "Build a Campaign from a Brief — Campaign Agent",
            "title_aliases": [],
        },
    }

    result = resolve_asset_by_title(
        "Build a Campaign from a Brief — Campaign Agent",
        assets,
        board_id="238181",
    )

    assert result is not None
    assert result["asset"]["asset_id"] == "2211727"
    assert result["match_type"] == "exact"
