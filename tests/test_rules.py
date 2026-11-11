"""Unit tests for kill_switch.py decision logic.

Tests use fake insights data, no network calls, no real credentials.

Run with:
  python -m pytest tests/
  python tests/test_rules.py      # also works without pytest
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kill_switch import evaluate_item, sum_results, is_protected, item_age_hours

# ---------------------------------------------------------------------------
# Helpers to build fake item dicts
# ---------------------------------------------------------------------------

def _make_item(
    item_id="120250000000000001",
    name="Test Campaign",
    level="campaign",
    created_hours_ago=48,
    lifetime_spend=0.0,
    lifetime_actions=None,
    daily_spend=0.0,
    daily_actions=None,
):
    """Build a fake item dict as the Meta API would return it."""
    from datetime import datetime, timezone, timedelta
    created = datetime.now(timezone.utc) - timedelta(hours=created_hours_ago)
    item = {
        "id": item_id,
        "name": name,
        "created_time": created.isoformat(),
    }
    if lifetime_spend > 0 or lifetime_actions:
        item["insights"] = {"data": [{"spend": str(lifetime_spend),
                                       "actions": lifetime_actions or []}]}
    if daily_spend > 0 or daily_actions:
        item["today"] = {"data": [{"spend": str(daily_spend),
                                    "actions": daily_actions or []}]}
    return item


def _action(atype, value=1):
    return {"action_type": atype, "value": str(value)}


_DEFAULT_TYPES = [
    "purchase",
    "offsite_conversion.fb_pixel_purchase",
    "onsite_conversion.lead_grouped",
]


def _eval(item, level="campaign", result_types=None, lifetime=50.0, daily=40.0, min_age=0, protect=None):
    return evaluate_item(
        item, level,
        result_types or _DEFAULT_TYPES,
        lifetime, daily, min_age,
        protect or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLifetimeRule:
    def test_over_limit_zero_results_paused(self):
        item = _make_item(lifetime_spend=55.0)
        v = _eval(item)
        assert v["action"] == "pause", f"Expected pause, got {v}"

    def test_over_limit_has_results_kept(self):
        item = _make_item(
            lifetime_spend=55.0,
            lifetime_actions=[_action("purchase", 2)],
        )
        v = _eval(item)
        assert v["action"] == "keep", f"Expected keep, got {v}"

    def test_under_limit_no_results_kept(self):
        item = _make_item(lifetime_spend=20.0)
        v = _eval(item)
        assert v["action"] == "keep", f"Expected keep, got {v}"

    def test_exactly_at_limit_zero_results_paused(self):
        item = _make_item(lifetime_spend=50.0)
        v = _eval(item)
        assert v["action"] == "pause"

    def test_one_cent_under_limit_kept(self):
        item = _make_item(lifetime_spend=49.99)
        v = _eval(item)
        assert v["action"] == "keep"

    def test_lead_grouped_counts_as_result(self):
        item = _make_item(
            lifetime_spend=60.0,
            lifetime_actions=[_action("onsite_conversion.lead_grouped", 1)],
        )
        v = _eval(item)
        assert v["action"] == "keep"

    def test_pixel_purchase_counts_as_result(self):
        item = _make_item(
            lifetime_spend=60.0,
            lifetime_actions=[_action("offsite_conversion.fb_pixel_purchase", 1)],
        )
        v = _eval(item)
        assert v["action"] == "keep"

    def test_unrelated_action_not_counted(self):
        # link_click should not count as a result
        item = _make_item(
            lifetime_spend=60.0,
            lifetime_actions=[_action("link_click", 100)],
        )
        v = _eval(item)
        assert v["action"] == "pause"

    def test_zero_spend_no_action(self):
        item = _make_item(lifetime_spend=0.0)
        v = _eval(item)
        assert v["action"] == "keep"


class TestDailyRule:
    def test_daily_over_limit_zero_results_paused(self):
        item = _make_item(
            level="campaign",
            lifetime_spend=20.0,
            daily_spend=45.0,
        )
        v = _eval(item, level="campaign")
        assert v["action"] == "pause"
        assert "today" in v["reason"].lower() or "daily" in v["reason"].lower()

    def test_daily_rule_has_results_kept(self):
        item = _make_item(
            level="campaign",
            lifetime_spend=20.0,
            daily_spend=45.0,
            daily_actions=[_action("purchase", 1)],
        )
        v = _eval(item, level="campaign")
        assert v["action"] == "keep"

    def test_daily_rule_does_not_fire_on_adset(self):
        # Daily rule is campaigns-only
        item = _make_item(
            level="adset",
            lifetime_spend=20.0,
            daily_spend=45.0,
        )
        v = _eval(item, level="adset")
        assert v["action"] == "keep"

    def test_daily_rule_does_not_fire_on_ad(self):
        item = _make_item(
            level="ad",
            lifetime_spend=20.0,
            daily_spend=45.0,
        )
        v = _eval(item, level="ad")
        assert v["action"] == "keep"

    def test_lifetime_rule_wins_over_daily_rule(self):
        # Both rules could fire, lifetime rule runs first
        item = _make_item(lifetime_spend=55.0, daily_spend=45.0)
        v = _eval(item, level="campaign")
        assert v["action"] == "pause"
        assert "lifetime" in v["reason"].lower()


class TestProtectList:
    def test_protected_by_id_skipped(self):
        item = _make_item(item_id="120250000000000099", lifetime_spend=99.0)
        v = _eval(item, protect=["120250000000000099"])
        assert v["action"] == "skip"

    def test_protected_by_name_substring_skipped(self):
        item = _make_item(name="Always-On Brand Awareness", lifetime_spend=99.0)
        v = _eval(item, protect=["Always-On"])
        assert v["action"] == "skip"

    def test_non_matching_protect_not_skipped(self):
        item = _make_item(name="Retargeting - Video 3", lifetime_spend=99.0)
        v = _eval(item, protect=["Always-On"])
        assert v["action"] == "pause"

    def test_empty_protect_list_works(self):
        item = _make_item(lifetime_spend=99.0)
        v = _eval(item, protect=[])
        assert v["action"] == "pause"


class TestMinAge:
    def test_too_new_skipped(self):
        item = _make_item(created_hours_ago=1, lifetime_spend=99.0)
        v = _eval(item, min_age=4)
        assert v["action"] == "skip"
        assert "new" in v["reason"].lower()

    def test_old_enough_not_skipped(self):
        item = _make_item(created_hours_ago=10, lifetime_spend=99.0)
        v = _eval(item, min_age=4)
        assert v["action"] == "pause"

    def test_min_age_zero_no_skip(self):
        item = _make_item(created_hours_ago=0, lifetime_spend=99.0)
        v = _eval(item, min_age=0)
        assert v["action"] == "pause"


class TestSumResults:
    def test_purchase_counted(self):
        actions = [{"action_type": "purchase", "value": "3"}]
        assert sum_results(actions, ["purchase"]) == 3.0

    def test_multiple_types(self):
        actions = [
            {"action_type": "purchase", "value": "1"},
            {"action_type": "onsite_conversion.lead_grouped", "value": "2"},
        ]
        assert sum_results(actions, ["purchase", "onsite_conversion.lead_grouped"]) == 3.0

    def test_unrecognised_type_ignored(self):
        actions = [{"action_type": "post_engagement", "value": "50"}]
        assert sum_results(actions, ["purchase"]) == 0.0

    def test_empty_actions(self):
        assert sum_results([], ["purchase"]) == 0.0

    def test_custom_event_scoped_match(self):
        actions = [
            {
                "action_type": "offsite_conversion.fb_pixel_custom",
                "action_target_id": "OrderFormPurchase",
                "value": "2",
            }
        ]
        assert sum_results(actions, ["offsite_conversion.fb_pixel_custom:OrderFormPurchase"]) == 2.0

    def test_custom_event_wrong_name_not_counted(self):
        actions = [
            {
                "action_type": "offsite_conversion.fb_pixel_custom",
                "action_target_id": "SomethingElse",
                "value": "2",
            }
        ]
        assert sum_results(actions, ["offsite_conversion.fb_pixel_custom:OrderFormPurchase"]) == 0.0


class TestIsProtected:
    def test_exact_id_match(self):
        assert is_protected({"id": "12345", "name": "Test"}, ["12345"])

    def test_name_substring_match(self):
        assert is_protected({"id": "1", "name": "Always-On Brand"}, ["always-on"])

    def test_no_match(self):
        assert not is_protected({"id": "1", "name": "Video Ad"}, ["Brand", "12345"])

    def test_empty_protect_list(self):
        assert not is_protected({"id": "1", "name": "Video Ad"}, [])


# ---------------------------------------------------------------------------
# Runner for plain python tests/test_rules.py
# ---------------------------------------------------------------------------

def _run_all():
    import traceback
    suites = [
        TestLifetimeRule,
        TestDailyRule,
        TestProtectList,
        TestMinAge,
        TestSumResults,
        TestIsProtected,
    ]
    passed = 0
    failed = 0
    for suite_cls in suites:
        suite = suite_cls()
        for name in dir(suite_cls):
            if not name.startswith("test_"):
                continue
            method = getattr(suite, name)
            try:
                method()
                print(f"  PASS  {suite_cls.__name__}.{name}")
                passed += 1
            except Exception:
                print(f"  FAIL  {suite_cls.__name__}.{name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    failures = _run_all()
    sys.exit(failures)
