"""Unit tests for the route-matching / consolidation service (no real HTTP).

The Super Dispatch client is replaced with a tiny in-memory fake so we can assert
the matcher's behavior: normalization, the (origin AND destination) match rule,
posted-only filtering, multi-match, the per-run GUID cache, and error isolation.
"""
import consolidation


# --------------------------- fake SD client ---------------------------
class FakeSD:
    """Stand-in for sd_api: find_by_vin returns short {guid} stubs; get_order returns
    a full order from `orders`. Counts get_order calls per GUID to prove caching."""
    def __init__(self, vin_to_guids, orders, raise_on=None):
        self.vin_to_guids = vin_to_guids        # vin -> [guid, ...]
        self.orders = orders                    # guid -> full order dict
        self.raise_on = raise_on or {}          # vin/guid -> Exception to raise
        self.find_calls = []
        self.get_calls = []

    def find_by_vin(self, vin):
        self.find_calls.append(vin)
        if vin in self.raise_on:
            raise self.raise_on[vin]
        return [{"guid": g} for g in self.vin_to_guids.get(vin, [])]

    def get_order(self, guid):
        self.get_calls.append(guid)
        if guid in self.raise_on:
            raise self.raise_on[guid]
        return self.orders[guid]


def venue(city, state, zip_, street=""):
    return {"venue": {"city": city, "state": state, "zip": zip_, "address": street}}


def order(guid, number, pu, do, vins, posted=True, price=1000):
    return {
        "guid": guid, "number": number, "pickup": pu, "delivery": do,
        "vehicles": [{"vin": v, "guid": f"veh-{v}"} for v in vins],
        "price": price, "status": "new", "is_posted_to_loadboard": posted,
        "posted_to_loadboard_at": "2026-06-01" if posted else None,
    }


# --------------------------- normalize / endpoints ---------------------------
def test_normalize_lowercases_trims_collapses():
    assert consolidation.normalize("  Los   ANGELES ") == "los angeles"
    assert consolidation.normalize(None) == ""


def test_route_endpoint_default_is_city_state_zip():
    v = venue("Austin", "TX", "78701", "100 Main St")
    assert consolidation.route_endpoint(v) == ("austin", "tx", "78701")
    # street only included when asked
    assert consolidation.route_endpoint(v, use_street=True)[0] == "100 main st"


# --------------------------- routes_match rule ---------------------------
def test_routes_match_requires_both_ends():
    a_pu, a_do = venue("Bend", "OR", "97703"), venue("Duluth", "GA", "30096")
    b_pu, b_do = venue("BEND", "or", " 97703 "), venue("Duluth", "GA", "30096")
    assert consolidation.routes_match(a_pu, a_do, b_pu, b_do) is True
    # same origin, different destination -> NOT a match
    other_do = venue("Atlanta", "GA", "30301")
    assert consolidation.routes_match(a_pu, a_do, b_pu, other_do) is False


def test_routes_match_street_toggle_tightens():
    a_pu = venue("Austin", "TX", "78701", "100 Main St")
    b_pu = venue("Austin", "TX", "78701", "200 Other Ave")
    do = venue("Reno", "NV", "89501")
    # city/state/zip agree -> match by default
    assert consolidation.routes_match(a_pu, do, b_pu, do) is True
    # but different streets -> no match when street is required
    assert consolidation.routes_match(a_pu, do, b_pu, do, use_street=True) is False


# --------------------------- find_orders_for_vins ---------------------------
def test_per_run_cache_fetches_shared_guid_once():
    pu, do = venue("Bend", "OR", "97703"), venue("Duluth", "GA", "30096")
    o = order("g1", "A1", pu, do, ["VIN1", "VIN2"])
    # two different VINs both resolve to the same order g1
    sd = FakeSD({"VIN1": ["g1"], "VIN2": ["g1"]}, {"g1": o})
    res = consolidation.find_orders_for_vins(["VIN1", "VIN2"], throttle_s=0, sd=sd)
    assert len(res.orders) == 1
    assert sd.get_calls == ["g1"]            # fetched once despite two VINs
    assert res.checked_vins == 2


def test_multi_match_one_vin_many_orders():
    pu, do = venue("A", "TX", "1"), venue("B", "TX", "2")
    sd = FakeSD({"VINX": ["g1", "g2"]},
                {"g1": order("g1", "A1", pu, do, ["VINX"]),
                 "g2": order("g2", "A2", pu, do, ["VINX", "VINY"])})
    res = consolidation.find_orders_for_vins(["VINX"], throttle_s=0, sd=sd)
    assert {o["guid"] for o in res.orders} == {"g1", "g2"}


def test_vin_not_found_is_normal():
    sd = FakeSD({}, {})
    res = consolidation.find_orders_for_vins(["NOPE"], throttle_s=0, sd=sd)
    assert res.orders == [] and res.not_found_vins == ["NOPE"] and res.errors == []
    assert res.auth_error is None


def test_auth_error_stops_run():
    boom = consolidation.sd_api.SDError("GET /x -> 401: nope")
    sd = FakeSD({"V": ["g"]}, {}, raise_on={"V": boom})
    res = consolidation.find_orders_for_vins(["V", "V2"], throttle_s=0, sd=sd)
    assert res.auth_error is not None
    assert sd.find_calls == ["V"]            # stopped before checking V2


def test_transient_error_isolated_not_fatal():
    pu, do = venue("A", "TX", "1"), venue("B", "TX", "2")
    sd = FakeSD({"BAD": ["gbad"], "GOOD": ["ggood"]},
                {"ggood": order("ggood", "A2", pu, do, ["GOOD"])},
                raise_on={"gbad": RuntimeError("GET /x -> 500: boom")})
    res = consolidation.find_orders_for_vins(["BAD", "GOOD"], throttle_s=0, sd=sd)
    assert {o["guid"] for o in res.orders} == {"ggood"}   # GOOD still collected
    assert any(e.get("guid") == "gbad" for e in res.errors)
    assert res.auth_error is None


# --------------------------- match_against_routes ---------------------------
def test_match_posted_only_and_already_on():
    pu, do = venue("Bend", "OR", "97703"), venue("Duluth", "GA", "30096")
    posted = consolidation.normalize_order(order("g1", "A1", pu, do, ["VIN1"], posted=True))
    unposted = consolidation.normalize_order(order("g2", "A2", pu, do, ["VIN2"], posted=False))
    board = [{"number": "B1", "pickup": pu, "delivery": do}]
    out = consolidation.match_against_routes([posted, unposted], board, my_vins=["VIN1", "ZZZ"])
    by_guid = {o["guid"]: o for o in out}
    # posted + same route -> candidate; unposted same route -> NOT a candidate
    assert by_guid["g1"]["is_candidate"] is True
    assert by_guid["g2"]["is_candidate"] is False
    # board route key tagged, and VIN1 flagged as already on g1
    assert "97703|30096" in by_guid["g1"]["matches_board_routes"]
    assert by_guid["g1"]["already_on"] == ["VIN1"]


def test_no_match_when_routes_differ():
    pu, do = venue("Bend", "OR", "97703"), venue("Duluth", "GA", "30096")
    o = consolidation.normalize_order(order("g1", "A1", pu, do, ["VIN1"]))
    board = [{"number": "B1", "pickup": venue("Reno", "NV", "89501"),
              "delivery": venue("Mesa", "AZ", "85201")}]
    out = consolidation.match_against_routes([o], board)
    assert out[0]["matches_board_routes"] == [] and out[0]["is_candidate"] is False


def test_accepted_order_is_never_a_candidate():
    """Accepted/pending/picked-up loads belong to a carrier — offering them as
    consolidation targets is how VINs got appended onto already-accepted loads."""
    pu, do = venue("Bend", "OR", "97703"), venue("Duluth", "GA", "30096")
    o = consolidation.normalize_order(order("g1", "A1", pu, do, ["VIN1"], posted=False))
    o["loadboard_status"] = "accepted"                   # found on the Accepted tab
    board = [{"number": "B1", "pickup": pu, "delivery": do}]
    out = consolidation.match_against_routes([o], board)
    assert out[0]["loadboard_status"] == "accepted"
    assert out[0]["editable"] is False
    assert out[0]["is_candidate"] is False               # route matches, but not editable


def test_order_editable_checks_both_status_fields():
    # lifecycle status blocks even when loadboard_status is empty (SD clears it on accept)
    for st in ("accepted", "pending", "picked_up", "delivered", "invoiced", "paid", "canceled"):
        assert consolidation.order_editable({"status": st, "loadboard_status": None}) is False
        assert consolidation.order_editable({"status": "", "loadboard_status": st}) is False
    assert consolidation.order_editable({"status": "new", "loadboard_status": None}) is True
    assert consolidation.order_editable({"status": "posted", "loadboard_status": "posted"}) is True


def test_exact_street_match_required_by_default():
    pu_a = venue("Austin", "TX", "78701", "100 Main St")
    pu_b = venue("Austin", "TX", "78701", "999 Other Ave")   # same zip, different street
    do = venue("Reno", "NV", "89501", "5 Pine St")
    same = consolidation.normalize_order(order("g1", "A1", pu_a, do, ["V1"], posted=True))
    diff = consolidation.normalize_order(order("g2", "A2", pu_b, do, ["V2"], posted=True))
    board = [{"number": "B1", "pickup": pu_a, "delivery": do}]
    out = {x["guid"]: x for x in consolidation.match_against_routes([same, diff], board)}
    assert out["g1"]["is_candidate"] is True             # exact street matches
    assert out["g2"]["matches_board_routes"] == []       # street differs -> no match
    assert out["g2"]["is_candidate"] is False
