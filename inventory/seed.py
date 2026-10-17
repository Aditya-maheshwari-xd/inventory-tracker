"""Load a small fictional sheet-metal plant so the app has something to show.

python -m inventory.seed            (uses INVENTORY_DB or ./inventory.db)
python -m inventory.seed --fresh    (deletes the database first)
"""

from __future__ import annotations

import argparse
import os

from . import services as svc
from .db import connect, db_path

ITEMS = [
    # code, name, kind, uom, reorder level
    ("CRCA-12", "CRCA sheet 1.2 mm", "raw", "kg", 500),
    ("MSROD-8", "MS round rod 8 mm", "raw", "kg", 100),
    ("NUT-M8", "Hex nut M8", "consumable", "nos", 2000),
    ("PWD-BLK", "Powder coat, black", "consumable", "kg", 25),
    ("BOX-50", "Corrugated box (50 pcs)", "consumable", "nos", 40),
    ("BRK-220", "Mounting bracket BRK-220", "finished", "nos", 0),
    ("HNG-310", "Door hinge HNG-310", "finished", "nos", 0),
]

BOMS = {
    # 0.35 kg sheet per bracket, 8% lost as offcuts in blanking
    "BRK-220": [{"code": "CRCA-12", "qty_per_unit": 0.35, "scrap_pct": 8},
                {"code": "PWD-BLK", "qty_per_unit": 0.02, "scrap_pct": 10},
                {"code": "BOX-50", "qty_per_unit": 0.02}],
    "HNG-310": [{"code": "CRCA-12", "qty_per_unit": 0.18, "scrap_pct": 6},
                {"code": "MSROD-8", "qty_per_unit": 0.05, "scrap_pct": 2},
                {"code": "NUT-M8", "qty_per_unit": 2}],
}


def seed(conn) -> None:
    for code, name, kind, uom, reorder in ITEMS:
        svc.add_item(conn, code, name, kind, uom, reorder)
    for code, comps in BOMS.items():
        svc.set_bom(conn, code, comps)

    svc.receive(conn, "CRCA-12", 2000, grn="GRN-1041", supplier="Jindal Steel stockist", user="store")
    svc.receive(conn, "MSROD-8", 300, grn="GRN-1042", supplier="Local trader", user="store")
    svc.receive(conn, "NUT-M8", 5000, grn="GRN-1043", supplier="Fastener House", user="store")
    svc.receive(conn, "PWD-BLK", 60, grn="GRN-1044", supplier="Coatings Co", user="store")
    svc.receive(conn, "BOX-50", 120, grn="GRN-1045", supplier="Packwell", user="store")

    svc.produce(conn, "BRK-220", 2400, 60, batch="B-2026-1014-A", user="press supervisor")
    svc.produce(conn, "HNG-310", 1500, 20, batch="B-2026-1014-B", user="press supervisor")
    svc.record_wastage(conn, "CRCA-12", 35, "Rusted sheets found at bottom of stack", user="store")
    svc.dispatch(conn, "BRK-220", 2000, invoice="INV/26-27/0412", user="dispatch")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--fresh", action="store_true", help="delete the database first")
    args = p.parse_args()
    path = db_path()
    if args.fresh and os.path.exists(path):
        os.remove(path)
    conn = connect(path)
    if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
        print(f"{path} already has data — use --fresh to start over")
        return
    seed(conn)
    print(f"Seeded {path}")


if __name__ == "__main__":
    main()
