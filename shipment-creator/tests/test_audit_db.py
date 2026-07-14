import audit_db


def test_posting_run_round_trip(tmp_path, monkeypatch):
    db = tmp_path / "audit.db"
    monkeypatch.setenv("SC_AUDIT_DB", str(db))

    run_id = audit_db.start_run(
        profile_id="kelly", profile_name="Kelly", action="post_all",
        attempted_orders=2, attempted_units=3, duplicate_units=4,
    )
    item_id = audit_db.start_item(
        run_id, action="create", shipment_number="A12345",
        vins=["vin1", "VIN2"], pickup="Austin, TX 78701",
        delivery="Reno, NV 89501", price=900, inspection_type="advanced",
    )
    audit_db.finish_item(item_id, status="success", sd_guid="guid-1")
    audit_db.finish_run(
        run_id, status="partial", posted_orders=1, posted_units=2, failed_orders=1,
    )

    data = audit_db.list_runs()
    assert data["total"] == 1
    assert data["summary"] == {
        "posted_units": 2, "duplicate_units": 4,
        "failed_orders": 1, "dispatchers": 1, "runs": 1,
    }
    run = data["runs"][0]
    assert run["profile_name"] == "Kelly"
    assert run["duplicate_units"] == 4
    assert run["status"] == "partial"
    assert run["items"][0]["vins"] == ["VIN1", "VIN2"]
    assert run["items"][0]["inspection_type"] == "advanced"
    assert run["items"][0]["sd_guid"] == "guid-1"


def test_failed_item_error_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_AUDIT_DB", str(tmp_path / "audit.db"))
    run_id = audit_db.start_run(
        profile_id="duka", profile_name="Duka", action="single",
        attempted_orders=1, attempted_units=1,
    )
    item_id = audit_db.start_item(
        run_id, action="create", shipment_number="A9", vins=["V"],
    )
    audit_db.finish_item(item_id, status="failed", error="x" * 5000)
    audit_db.finish_run(
        run_id, status="failed", posted_orders=0, posted_units=0, failed_orders=1,
    )

    item = audit_db.list_runs()["runs"][0]["items"][0]
    assert item["status"] == "failed"
    assert len(item["error"]) == 1000
