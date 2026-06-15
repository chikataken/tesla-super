"""Unit tests for the loadboard-scan PURE parsing (_cards_to_hits). The Playwright
DOM walk itself can't be unit-tested (it's behind auth) — but the card-record ->
hit filtering is pure and is where the zip-pair heuristic lives."""
import sd_scrape


def card(href, vins, text, _id="A1"):
    return {"href": href, "id": _id, "vins": list(vins), "text": text}


def test_keeps_card_whose_zip_pair_matches():
    pairs = {("34744", "33714")}
    cards = [card("/orders/g1", ["1HGCM82633A111111"],
                  "Kissimmee, FL 34744 to Saint Petersburg, FL 33714 Open")]
    hits = sd_scrape._cards_to_hits(cards, "posted", pairs)
    assert len(hits) == 1
    h = hits[0]
    assert h == {"loadboard_status": "posted", "vin": "1HGCM82633A111111",
                 "pickup_zip": "34744", "dropoff_zip": "33714",
                 "order_id": "A1", "detail_url": "/orders/g1"}


def test_drops_card_with_non_matching_zip_pair():
    pairs = {("34744", "33714")}
    cards = [card("/orders/g2", ["1HGCM82633A222222"], "Reno, NV 89501 to Mesa, AZ 85201")]
    assert sd_scrape._cards_to_hits(cards, "posted", pairs) == []


def test_drops_card_with_no_vin():
    pairs = {("34744", "33714")}
    cards = [card("/orders/g3", [], "Kissimmee 34744 to St Pete 33714")]
    assert sd_scrape._cards_to_hits(cards, "accepted", pairs) == []


def test_drops_card_with_single_zip():
    pairs = {("34744", "33714")}
    cards = [card("/orders/g4", ["1HGCM82633A444444"], "Somewhere 34744 only")]
    assert sd_scrape._cards_to_hits(cards, "posted", pairs) == []


def test_first_and_last_zip_are_origin_and_dest():
    # three zips on the card (e.g. a stray middle zip) -> first=origin, last=dest
    pairs = {("34744", "33714")}
    cards = [card("/orders/g5", ["1HGCM82633A555555"],
                  "34744 ... 99999 ... 33714")]
    hits = sd_scrape._cards_to_hits(cards, "accepted", pairs)
    assert len(hits) == 1
    assert (hits[0]["pickup_zip"], hits[0]["dropoff_zip"]) == ("34744", "33714")
    assert hits[0]["loadboard_status"] == "accepted"
