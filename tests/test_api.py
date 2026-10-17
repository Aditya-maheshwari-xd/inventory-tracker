import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_DB", str(tmp_path / "api.db"))
    from inventory.api import app
    from inventory.db import connect
    from inventory.seed import seed
    seed(connect())
    return TestClient(app)


def test_home_page_serves(client):
    r = client.get("/")
    assert r.status_code == 200 and "Inventory Tracker" in r.text


def test_seeded_stock_and_alerts(client):
    d = client.get("/api/stock").json()
    stock = {i["code"]: i for i in d["items"]}
    assert stock["BRK-220"]["stock"] == 400          # 2400 made - 2000 dispatched
    assert stock["PWD-BLK"]["status"] == "low"
    assert stock["NUT-M8"]["status"] == "low"
    assert {b["code"] for b in d["can_build"]} == {"BRK-220", "HNG-310"}


def test_shortage_returns_409_with_details_then_override_works(client):
    body = {"code": "BRK-220", "good_qty": 5000, "batch": "B-X"}
    r = client.post("/api/production", json=body)
    assert r.status_code == 409
    codes = {s["code"] for s in r.json()["shortages"]}
    assert "CRCA-12" in codes
    r = client.post("/api/production", json={**body, "override_reason": "GRN pending"})
    assert r.status_code == 201 and r.json()["warnings"]


def test_duplicate_grn_is_409(client):
    r = client.post("/api/receipts", json={"code": "CRCA-12", "qty": 10, "grn": "GRN-1041"})
    assert r.status_code == 409 and "already entered" in r.json()["error"]


def test_preview_required_material(client):
    rows = client.get("/api/items/HNG-310/required", params={"units": 100}).json()
    nut = next(r for r in rows if r["code"] == "NUT-M8")
    assert nut["needed"] == 200 and nut["enough"] is True


def test_unknown_item_is_404(client):
    assert client.post("/api/receipts", json={"code": "NOPE", "qty": 1}).status_code == 404


def test_reverse_from_api(client):
    txns = client.get("/api/txns").json()
    dispatch = next(t for t in txns if t["kind"] == "dispatch")
    r = client.post(f"/api/txns/{dispatch['id']}/reverse", json={"reason": "Truck didn't leave"})
    assert r.status_code == 201
    stock = {i["code"]: i["stock"] for i in client.get("/api/items").json()}
    assert stock["BRK-220"] == 2400
