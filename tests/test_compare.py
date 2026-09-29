import datetime as dt
from decimal import Decimal

from askdata.compare import results_match


def m(gc, gr, pc, pr, key=None):
    return results_match(gc, gr, pc, pr, key)[0]


def test_order_insensitive_by_default():
    assert m(["f", "n"], [("a", 1), ("b", 2)], ["x", "y"], [("b", 2), ("a", 1)])


def test_column_order_and_names_ignored():
    assert m(["f", "n"], [("a", 1)], ["n", "f"], [(1, "a")])


def test_extra_prediction_columns_allowed_missing_not():
    assert m(["n"], [(5,)], ["label", "n"], [("x", 5)])
    assert not m(["f", "n"], [("a", 5)], ["n"], [(5,)])


def test_row_count_must_match():
    assert not m(["n"], [(1,), (2,)], ["n"], [(1,)])
    assert not m(["n"], [(1,)], ["n"], [(1,), (1,)])


def test_numeric_tolerance_and_rounding():
    assert m(["v"], [(0.268317,)], ["v"], [(0.2683,)])          # rounded to 4 dp
    assert m(["v"], [(194670.4,)], ["v"], [(194670,)])           # within 1e-3 relative
    assert m(["v"], [(5.845588,)], ["v"], [(5.85,)])             # prediction rounded to 2 dp
    assert not m(["v"], [(0.2683,)], ["v"], [(0.28,)])
    assert m(["v"], [(Decimal("969"),)], ["v"], [(969.0,)])


def test_percent_vs_fraction():
    assert m(["share"], [(0.2755,)], ["pct"], [(27.55,)])
    assert m(["pct"], [(27.554,)], ["share"], [(0.27554,)])


def test_order_key_enforced_but_ties_free():
    gold = [("reddit", 25), ("airbnb", 16), ("pinterest", 16)]
    assert m(["c", "n"], gold, ["c", "n"], [("reddit", 25), ("pinterest", 16), ("airbnb", 16)], "n")
    assert not m(["c", "n"], gold, ["c", "n"], [("airbnb", 16), ("reddit", 25), ("pinterest", 16)], "n")


def test_dates_and_timestamps_normalise():
    assert m(["d"], [(dt.date(2026, 9, 1),)], ["d"], [(dt.datetime(2026, 9, 1, 0, 0),)])
    assert m(["d"], [(dt.date(2026, 9, 1),)], ["d"], [("2026-09-01 00:00:00",)])


def test_strings_case_and_space_insensitive():
    assert m(["c"], [("Reddit",)], ["c"], [(" reddit ",)])


def test_nulls():
    assert m(["v"], [(None,), (1.0,)], ["v"], [(1,), (None,)])
    assert not m(["v"], [(None,)], ["v"], [(0,)])


def test_empty_results():
    assert m(["v"], [], ["v"], [])
    assert not m(["v"], [(1,)], ["v"], [])


def test_greedy_tolerance_cannot_steal_exact_partner():
    # regression: with a 1e-3 relative tolerance, 2021 used to "match" 2022 and the greedy pass then failed
    gold = [(2025, 51), (2023, 5), (2021, 4), (2024, 12), (2022, 1)]
    pred = [(2021, 4), (2022, 1), (2023, 5), (2024, 12), (2025, 51)]
    assert m(["yr", "n"], gold, ["y", "n"], pred)


def test_integers_compare_exactly():
    assert not m(["n"], [(2021,)], ["n"], [(2022,)])
    assert not m(["n"], [(5755,)], ["n"], [(5754,)])


def test_timestamp_answers_a_date_question():
    assert m(["d"], [(dt.date(2021, 1, 23),)], ["ts"], [(dt.datetime(2021, 1, 23, 4, 35, 25),)])
    assert not m(["d"], [(dt.date(2021, 1, 23),)], ["ts"], [(dt.datetime(2021, 1, 24, 4, 35, 25),)])
