"""Unit tests for the Super Dispatch client (sd_api) with the HTTP layer mocked.

We monkeypatch sd_api.requests so no network is hit, and assert: token caching +
one-shot 401 re-auth, find_by_vin list / 404->[], get_order envelope unwrap,
patch_order's merge-patch content type, the build_vehicles_merge rules, and that a
429 honors Retry-After.
"""
import json

import pytest


class FakeResp:
    def __init__(self, status=200, json_data=None, text=None, headers=None):
        self.status_code = status
        self._json = {} if json_data is None else json_data
        if text is not None:
            self.text = text
        elif json_data is not None:
            self.text = json.dumps(json_data)
        else:
            self.text = ""
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._json


@pytest.fixture
def sd(monkeypatch):
    """Fresh sd_api with creds set, token cache cleared, and auth (POST) stubbed."""
    import sd_api
    sd_api._token = None
    sd_api._token_expiry = 0.0
    monkeypatch.setattr(sd_api.config, "SD_CLIENT_ID", "id")
    monkeypatch.setattr(sd_api.config, "SD_CLIENT_SECRET", "secret")
    monkeypatch.setattr(sd_api.config, "SD_API_BASE", "https://api.example.com")
    calls = {"post": [], "request": []}

    def fake_post(url, **kw):
        calls["post"].append((url, kw))
        return FakeResp(200, {"access_token": "tok-123", "expires_in": 3600})

    monkeypatch.setattr(sd_api.requests, "post", fake_post)
    return sd_api, calls


def set_responses(monkeypatch, sd_api, calls, responses):
    queue = list(responses)

    def fake_request(method, url, **kw):
        calls["request"].append((method, url, kw))
        return queue.pop(0) if queue else FakeResp(200, {})

    monkeypatch.setattr(sd_api.requests, "request", fake_request)


def test_token_fetched_and_cached(sd):
    sd_api, calls = sd
    assert sd_api.get_token() == "tok-123"
    assert sd_api.get_token() == "tok-123"
    assert len(calls["post"]) == 1            # cached -> only one auth call


def test_request_reauths_once_on_401(sd, monkeypatch):
    sd_api, calls = sd
    set_responses(monkeypatch, sd_api, calls,
                  [FakeResp(401, text="expired"),
                   FakeResp(200, {"data": {"object": {"guid": "g"}}})])
    assert sd_api.get_order("g") == {"guid": "g"}
    assert len(calls["request"]) == 2         # retried after refresh
    assert len(calls["post"]) >= 2            # initial token + forced refresh


def test_find_by_vin_returns_list(sd, monkeypatch):
    sd_api, calls = sd
    set_responses(monkeypatch, sd_api, calls,
                  [FakeResp(200, {"data": {"objects": [{"guid": "g1"}, {"guid": "g2"}]}})])
    assert sd_api.find_by_vin("VIN") == [{"guid": "g1"}, {"guid": "g2"}]


def test_find_by_vin_404_is_empty(sd, monkeypatch):
    sd_api, calls = sd
    set_responses(monkeypatch, sd_api, calls, [FakeResp(404, text="not found")])
    assert sd_api.find_by_vin("VIN") == []    # normal, not an error


def test_get_order_unwraps_envelope(sd, monkeypatch):
    sd_api, calls = sd
    set_responses(monkeypatch, sd_api, calls,
                  [FakeResp(200, {"data": {"object": {"guid": "g", "number": "A1"}}})])
    assert sd_api.get_order("g") == {"guid": "g", "number": "A1"}


def test_patch_order_uses_merge_patch_content_type(sd, monkeypatch):
    sd_api, calls = sd
    set_responses(monkeypatch, sd_api, calls,
                  [FakeResp(200, {"data": {"object": {"guid": "g"}}})])
    body = {"vehicles": [{"vin": "V"}]}
    sd_api.patch_order("g", body)
    method, url, kw = calls["request"][0]
    assert method == "PATCH"
    assert url.endswith("/v1/public/orders/g")
    assert kw["headers"]["Content-Type"] == "application/merge-patch+json"
    assert kw["json"] == body


def test_build_vehicles_merge_keeps_guids_adds_new_no_dupes(sd):
    sd_api, _ = sd
    existing = {"vehicles": [{"vin": "OLD1", "guid": "vg1", "make": "Tesla"},
                             {"vin": "OLD2", "guid": "vg2"}]}
    merge = sd_api.build_vehicles_merge(existing, [{"vin": "NEW1"}, {"vin": "OLD1"}])
    by_vin = {v["vin"]: v for v in merge["vehicles"]}
    assert [v["vin"] for v in merge["vehicles"]] == ["OLD1", "OLD2", "NEW1"]  # OLD1 not re-added
    assert by_vin["OLD1"]["guid"] == "vg1" and by_vin["OLD2"]["guid"] == "vg2"  # kept
    assert "guid" not in by_vin["NEW1"]       # new vehicle carries no guid


def test_build_vehicles_merge_dedupes_case_and_whitespace(sd):
    # A VIN differing only by case or stray whitespace is the SAME car — appending it
    # again is how an accepted load once ended up with a duplicate vehicle.
    sd_api, _ = sd
    existing = {"vehicles": [{"vin": "5YJ3E1EA4RF864263", "guid": "vg1"}]}
    merge = sd_api.build_vehicles_merge(
        existing, [{"vin": "5yj3e1ea4rf864263"}, {"vin": " 5YJ3E1EA4RF864263 "}])
    assert [v["vin"] for v in merge["vehicles"]] == ["5YJ3E1EA4RF864263"]


def test_429_honors_retry_after(sd, monkeypatch):
    sd_api, calls = sd
    slept = []
    monkeypatch.setattr(sd_api.time, "sleep", lambda s: slept.append(s))
    set_responses(monkeypatch, sd_api, calls,
                  [FakeResp(429, headers={"Retry-After": "2"}, text="slow down"),
                   FakeResp(200, {"data": {"object": {"guid": "g"}}})])
    assert sd_api.get_order("g") == {"guid": "g"}
    assert slept == [2.0]                      # waited exactly Retry-After seconds
