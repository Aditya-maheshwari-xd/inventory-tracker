"""HTTP API + a single-page UI for the store and the shop floor."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import services as svc
from .db import connect

app = FastAPI(title="Inventory Tracker", version="1.0")
STATIC = Path(__file__).parent / "static"


def get_conn() -> Iterator:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(svc.InventoryError)
def inventory_error(_: Request, exc: svc.InventoryError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message, **exc.details})


def posted(p: svc.Posted) -> dict:
    return {"txn_id": p.txn_id, "kind": p.kind, "movements": p.movements, "warnings": p.warnings}


# ---- request bodies --------------------------------------------------------

class ItemIn(BaseModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1)
    kind: str
    uom: str = Field(min_length=1)
    reorder_level: float = 0


class BomLine(BaseModel):
    code: str
    qty_per_unit: float
    scrap_pct: float = 0


class BomIn(BaseModel):
    components: list[BomLine]


class ReceiptIn(BaseModel):
    code: str
    qty: float
    grn: str | None = None
    supplier: str | None = None
    user: str | None = None


class ProductionIn(BaseModel):
    code: str
    good_qty: float
    rejected_qty: float = 0
    batch: str | None = None
    user: str | None = None
    override_reason: str | None = None


class WastageIn(BaseModel):
    code: str
    qty: float
    reason: str
    user: str | None = None


class DispatchIn(BaseModel):
    code: str
    qty: float
    invoice: str | None = None
    user: str | None = None


class CountIn(BaseModel):
    code: str
    counted: float
    reason: str | None = None
    user: str | None = None


class ReverseIn(BaseModel):
    reason: str
    user: str | None = None
    override_reason: str | None = None


# ---- routes ----------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/items")
def list_items(conn=Depends(get_conn)):
    return svc.stock_report(conn)


@app.post("/api/items", status_code=201)
def create_item(body: ItemIn, conn=Depends(get_conn)):
    svc.add_item(conn, body.code, body.name, body.kind, body.uom, body.reorder_level)
    return {"code": body.code.strip().upper()}


@app.put("/api/items/{code}/bom")
def put_bom(code: str, body: BomIn, conn=Depends(get_conn)):
    svc.set_bom(conn, code, [c.model_dump() for c in body.components])
    return {"code": code.upper(), "components": len(body.components)}


@app.get("/api/items/{code}/bom")
def get_bom(code: str, conn=Depends(get_conn)):
    item = svc.get_item(conn, code)
    return [{"code": b["code"], "name": b["name"], "uom": b["uom"], "qty_per_unit": b["qty_per_unit"],
             "scrap_pct": b["scrap_pct"]} for b in svc.bom_for(conn, item["id"])]


@app.get("/api/items/{code}/required")
def required(code: str, units: float, conn=Depends(get_conn)):
    """Preview: what will producing `units` pieces consume, and is there enough?"""
    out = []
    for n in svc.material_required(conn, code, units):
        have = svc.stock_of(conn, n["item_id"])
        out.append({"code": n["code"], "uom": n["uom"], "needed": n["qty"], "available": have,
                    "enough": have >= n["qty"]})
    return out


@app.get("/api/items/{code}/ledger")
def item_ledger(code: str, conn=Depends(get_conn)):
    return svc.ledger(conn, code)


@app.get("/api/stock")
def stock(conn=Depends(get_conn)):
    return {"items": svc.stock_report(conn), "can_build": svc.can_build(conn), "alerts": svc.alerts(conn)}


@app.post("/api/receipts", status_code=201)
def post_receipt(body: ReceiptIn, conn=Depends(get_conn)):
    return posted(svc.receive(conn, body.code, body.qty, grn=body.grn, supplier=body.supplier, user=body.user))


@app.post("/api/production", status_code=201)
def post_production(body: ProductionIn, conn=Depends(get_conn)):
    return posted(svc.produce(conn, body.code, body.good_qty, body.rejected_qty, batch=body.batch,
                              user=body.user, override_reason=body.override_reason))


@app.post("/api/wastage", status_code=201)
def post_wastage(body: WastageIn, conn=Depends(get_conn)):
    return posted(svc.record_wastage(conn, body.code, body.qty, body.reason, user=body.user))


@app.post("/api/dispatch", status_code=201)
def post_dispatch(body: DispatchIn, conn=Depends(get_conn)):
    return posted(svc.dispatch(conn, body.code, body.qty, invoice=body.invoice, user=body.user))


@app.post("/api/stock-count", status_code=201)
def post_count(body: CountIn, conn=Depends(get_conn)):
    return posted(svc.stock_count(conn, body.code, body.counted, body.reason, user=body.user))


@app.post("/api/txns/{txn_id}/reverse", status_code=201)
def post_reverse(txn_id: int, body: ReverseIn, conn=Depends(get_conn)):
    return posted(svc.reverse(conn, txn_id, body.reason, user=body.user, override_reason=body.override_reason))


@app.get("/api/txns")
def recent_txns(limit: int = 30, conn=Depends(get_conn)):
    rows = conn.execute("SELECT * FROM txns ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall()
    out = []
    for t in rows:
        moves = conn.execute("""SELECT i.code, m.qty FROM movements m JOIN items i ON i.id = m.item_id
                                WHERE m.txn_id = ?""", (t["id"],)).fetchall()
        out.append({**dict(t), "movements": [dict(m) for m in moves]})
    return out
