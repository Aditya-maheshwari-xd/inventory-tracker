import pytest

from inventory import services as svc
from inventory.db import connect


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "test.db"))
    svc.add_item(c, "SHEET", "CRCA sheet", "raw", "kg", reorder_level=50)
    svc.add_item(c, "NUT", "Nut M8", "consumable", "nos", reorder_level=100)
    svc.add_item(c, "BRK", "Bracket", "finished", "nos")
    # 0.5 kg sheet per bracket + 10% scrap = 0.55 kg; 2 nuts each
    svc.set_bom(c, "BRK", [{"code": "SHEET", "qty_per_unit": 0.5, "scrap_pct": 10},
                           {"code": "NUT", "qty_per_unit": 2}])
    yield c
    c.close()


def stock(conn, code):
    return svc.stock_of(conn, svc.get_item(conn, code)["id"])
