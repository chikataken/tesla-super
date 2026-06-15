"""Unit tests for the transit ruleset + shipment date windows (pure, no I/O)."""
import datetime

import transit


def test_same_state_transit_is_zero():
    # same state -> pickup can be the same day as delivery
    assert transit.transit_days("TX", "TX") == 0
    assert transit.transit_days("California", "California") == 0


def test_cross_country_takes_longer_than_regional_takes_longer_than_same_state():
    same = transit.transit_days("CA", "CA")
    regional = transit.transit_days("CA", "NV")          # neighbors
    cross = transit.transit_days("CA", "FL")             # coast to coast
    assert same < regional < cross
    assert cross >= 6                                     # a week-ish for coast to coast


def test_windows_still_report_same_state_transit_zero():
    # transit time is still 0 for same-state (shown as ~0d in the UI), even though
    # the pickup window no longer depends on transit.
    w = transit.shipment_windows(datetime.date(2026, 7, 1), "TX", "TX",
                                 today=datetime.date(2026, 6, 1))
    assert w["transit_days"] == 0


def test_full_names_and_abbrevs_both_work():
    assert transit.transit_days("Oregon", "Florida") == transit.transit_days("OR", "FL")


def test_unknown_state_falls_back():
    assert transit.transit_days("Atlantis", "TX") == transit.UNKNOWN_TRANSIT_DAYS


def test_delivery_window_is_2_days_ending_on_need_by():
    today = datetime.date(2026, 6, 1)
    nb = datetime.date(2026, 7, 1)                        # far future -> no clamping
    w = transit.shipment_windows(nb, "CA", "FL", today=today)
    assert w["delivery"]["latest"] == "2026-07-01"       # latest == need-by
    assert w["delivery"]["earliest"] == "2026-06-30"     # 2-day window


def test_pickup_window_opens_today_and_runs_to_tomorrow():
    today = datetime.date(2026, 6, 1)
    nb = datetime.date(2026, 7, 1)                        # plenty of room before need-by
    w = transit.shipment_windows(nb, "CA", "FL", today=today)
    assert w["pickup"]["earliest"] == "2026-06-01"        # today
    assert w["pickup"]["latest"] == "2026-06-02"          # tomorrow (2-day window)


def test_pickup_collapses_to_today_when_tomorrow_is_past_need_by():
    today = datetime.date(2026, 6, 12)
    nb = datetime.date(2026, 6, 12)                       # need-by is today -> tomorrow is past it
    w = transit.shipment_windows(nb, "CA", "FL", today=today)
    assert w["pickup"]["earliest"] == "2026-06-12"
    assert w["pickup"]["latest"] == "2026-06-12"          # collapsed to a single day (today)


def test_epoch_seconds_accepted_and_none_returns_none():
    today = datetime.date(2026, 6, 1)
    ts = datetime.datetime(2026, 7, 1, 16, 0).timestamp()
    w = transit.shipment_windows(ts, "TX", "TX", today=today)
    assert w["delivery"]["latest"] == "2026-07-01"
    assert transit.shipment_windows(None, "TX", "TX") is None
