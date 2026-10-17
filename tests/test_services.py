import pytest

from inventory import services as svc
from tests.conftest import stock


def test_production_consumes_bom_including_scrap_and_rejects(conn):
    svc.receive(conn, "SHEET", 100, grn="G1")
    svc.receive(conn, "NUT", 500, grn="G2")
    svc.produce(conn, "BRK", good_qty=90, rejected_qty=10, batch="B1")
    # 100 pieces made (90 good + 10 rejected) -> 55 kg sheet, 200 nuts
    assert stock(conn, "SHEET") == 45
    assert stock(conn, "NUT") == 300
    assert stock(conn, "BRK") == 90  # only good pieces go to stock


def test_production_blocked_when_material_short(conn):
    svc.receive(conn, "SHEET", 10)
    svc.receive(conn, "NUT", 500)
    with pytest.raises(svc.InsufficientStock) as err:
        svc.produce(conn, "BRK", 100)
    short = err.value.details["shortages"][0]
    assert (short["code"], short["needed"], short["available"], short["short_by"]) == ("SHEET", 55, 10, 45)
    # Nothing was posted — no half-consumed entry
    assert stock(conn, "SHEET") == 10 and stock(conn, "NUT") == 500 and stock(conn, "BRK") == 0


def test_override_records_negative_stock_and_raises_alert(conn):
    svc.receive(conn, "NUT", 500)
    p = svc.produce(conn, "BRK", 20, override_reason="Sheet received today, GRN pending")
    assert stock(conn, "SHEET") == -11
    assert "negative" in p.warnings[0]
    levels = {(a["level"], a["item"]) for a in svc.alerts(conn)}
    assert ("error", "SHEET") in levels
    # Entering the missing GRN clears it
    svc.receive(conn, "SHEET", 100, grn="G-late")
    assert ("error", "SHEET") not in {(a["level"], a["item"]) for a in svc.alerts(conn)}


def test_blank_override_reason_does_not_count(conn):
    with pytest.raises(svc.InsufficientStock):
        svc.produce(conn, "BRK", 1, override_reason="   ")


def test_same_grn_cannot_be_entered_twice(conn):
    svc.receive(conn, "SHEET", 100, grn="GRN-77")
    with pytest.raises(svc.Conflict, match="already entered"):
        svc.receive(conn, "SHEET", 100, grn="grn-77 ")  # case / spacing doesn't matter
    assert stock(conn, "SHEET") == 100


def test_same_batch_cannot_be_produced_twice_but_can_after_reversal(conn):
    svc.receive(conn, "SHEET", 100)
    svc.receive(conn, "NUT", 500)
    p = svc.produce(conn, "BRK", 10, batch="B7")
    with pytest.raises(svc.Conflict):
        svc.produce(conn, "BRK", 10, batch="B7")
    svc.reverse(conn, p.txn_id, "Wrong quantity")
    svc.produce(conn, "BRK", 12, batch="B7")
    assert stock(conn, "BRK") == 12


def test_reversal_restores_everything_and_keeps_history(conn):
    svc.receive(conn, "SHEET", 100)
    svc.receive(conn, "NUT", 500)
    p = svc.produce(conn, "BRK", 50, batch="B1")
    svc.reverse(conn, p.txn_id, "Entered against wrong item")
    assert (stock(conn, "SHEET"), stock(conn, "NUT"), stock(conn, "BRK")) == (100, 500, 0)
    kinds = [r["kind"] for r in svc.ledger(conn, "SHEET")]
    assert kinds == ["receipt", "production", "reversal"]  # nothing deleted
    with pytest.raises(svc.Conflict, match="already reversed"):
        svc.reverse(conn, p.txn_id, "again")


def test_cannot_reverse_receipt_already_consumed_without_override(conn):
    r = svc.receive(conn, "SHEET", 100, grn="G1")
    svc.receive(conn, "NUT", 500)
    svc.produce(conn, "BRK", 100)  # uses 55 kg
    with pytest.raises(svc.InsufficientStock):
        svc.reverse(conn, r.txn_id, "Wrong GRN")
    svc.reverse(conn, r.txn_id, "Wrong GRN", override_reason="Material actually belonged to unit 2")
    assert stock(conn, "SHEET") == -55


def test_stock_count_posts_difference_with_reason(conn):
    svc.receive(conn, "SHEET", 100)
    with pytest.raises(svc.InventoryError, match="Give a reason"):
        svc.stock_count(conn, "SHEET", 96.5)
    p = svc.stock_count(conn, "SHEET", 96.5, "Weighing difference")
    assert p.movements == [{"item": "SHEET", "qty": -3.5}]
    assert stock(conn, "SHEET") == 96.5
    assert svc.stock_count(conn, "SHEET", 96.5).txn_id == 0  # matches -> nothing posted


def test_wastage_needs_reason_and_stock(conn):
    svc.receive(conn, "SHEET", 10)
    with pytest.raises(svc.InventoryError, match="reason"):
        svc.record_wastage(conn, "SHEET", 2, "")
    with pytest.raises(svc.InsufficientStock):
        svc.record_wastage(conn, "SHEET", 20, "rust")
    svc.record_wastage(conn, "SHEET", 2, "rust")
    assert stock(conn, "SHEET") == 8


def test_dispatch_has_no_override(conn):
    svc.receive(conn, "SHEET", 100)
    svc.receive(conn, "NUT", 500)
    svc.produce(conn, "BRK", 10)
    with pytest.raises(svc.InsufficientStock):
        svc.dispatch(conn, "BRK", 11, invoice="INV-1")
    svc.dispatch(conn, "BRK", 10, invoice="INV-1")
    assert stock(conn, "BRK") == 0


def test_can_build_reports_limiting_material(conn):
    svc.receive(conn, "SHEET", 55)   # enough for 100
    svc.receive(conn, "NUT", 120)    # enough for 60
    (row,) = svc.can_build(conn)
    assert (row["can_build"], row["limited_by"]) == (60, "NUT")


def test_low_stock_alert(conn):
    svc.receive(conn, "SHEET", 40)  # reorder level 50
    assert any(a["level"] == "warning" and a["item"] == "SHEET" for a in svc.alerts(conn))


def test_float_noise_does_not_create_phantom_stock(conn):
    svc.receive(conn, "SHEET", 0.3)
    for _ in range(3):
        svc.record_wastage(conn, "SHEET", 0.1, "test")
    assert stock(conn, "SHEET") == 0
    with pytest.raises(svc.InsufficientStock):
        svc.record_wastage(conn, "SHEET", 0.001, "test")


@pytest.mark.parametrize("call", [
    lambda c: svc.receive(c, "SHEET", 0),
    lambda c: svc.receive(c, "SHEET", -5),
    lambda c: svc.receive(c, "BRK", 5),                     # finished goods don't come from a GRN
    lambda c: svc.produce(c, "SHEET", 5),                   # raw material isn't produced
    lambda c: svc.produce(c, "BRK", 0, 0),
    lambda c: svc.set_bom(c, "BRK", [{"code": "BRK", "qty_per_unit": 1}]),
    lambda c: svc.set_bom(c, "SHEET", [{"code": "NUT", "qty_per_unit": 1}]),
    lambda c: svc.add_item(c, "sheet", "dup", "raw", "kg"),  # codes are case-insensitive
])
def test_bad_input_is_rejected(conn, call):
    with pytest.raises(svc.InventoryError):
        call(conn)


def test_finished_item_without_bom(conn):
    svc.add_item(conn, "NEW", "New product", "finished", "nos")
    with pytest.raises(svc.InventoryError, match="no BOM"):
        svc.produce(conn, "NEW", 5)
