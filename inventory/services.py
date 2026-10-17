"""Business rules for raw material and finished goods stock.

The rules here are the ones a store keeper and a production supervisor
argue about, written down:

* Producing finished goods consumes raw material according to the BOM,
  including process scrap, and also for rejected pieces.
* An entry that would take stock below zero is blocked — unless someone
  gives a reason (e.g. "material arrived, GRN not entered yet"). Then it is
  allowed, recorded, and shown as an alert until stock is back above zero.
* The same GRN / batch number can't be entered twice (the classic double
  entry when two people log the same slip).
* Nothing is edited or deleted. A wrong entry is reversed as a whole, and
  the reversal itself is in the ledger.
* A physical stock count doesn't overwrite stock; it posts the difference
  as an adjustment, with a reason.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

QTY_DP = 3  # quantities kept to grams / millimetres


def plant_now() -> str:
    """Timestamp in the plant's time zone (the server may run in UTC)."""
    return datetime.now(ZoneInfo(os.environ.get("PLANT_TZ", "Asia/Kolkata"))).strftime("%Y-%m-%d %H:%M:%S")


def q(x: float) -> float:
    """Round a quantity, and turn -0.0 into 0.0."""
    return round(x, QTY_DP) + 0.0


class InventoryError(Exception):
    status = 400

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(InventoryError):
    status = 404


class Conflict(InventoryError):
    status = 409


@dataclass
class Shortage:
    code: str
    name: str
    uom: str
    needed: float
    available: float

    @property
    def short_by(self) -> float:
        return q(self.needed - self.available)


class InsufficientStock(Conflict):
    def __init__(self, shortages: list[Shortage]):
        lines = ", ".join(f"{s.code} short by {s.short_by:g} {s.uom} "
                          f"(need {s.needed:g}, have {s.available:g})" for s in shortages)
        super().__init__(f"Not enough stock: {lines}. Give an override reason to record it anyway.",
                         shortages=[{**s.__dict__, "short_by": s.short_by} for s in shortages])


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

def get_item(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM items WHERE code = ?", (code.strip().upper(),)).fetchone()
    if row is None:
        raise NotFound(f"No item with code {code!r}")
    return row


def stock_of(conn: sqlite3.Connection, item_id: int) -> float:
    (total,) = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM movements WHERE item_id = ?",
                            (item_id,)).fetchone()
    return q(total)


def bom_for(conn: sqlite3.Connection, finished_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.*, i.code, i.name, i.uom FROM bom b JOIN items i ON i.id = b.component_id
           WHERE b.finished_id = ? ORDER BY i.code""", (finished_id,)).fetchall()


# --------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------

def add_item(conn, code: str, name: str, kind: str, uom: str, reorder_level: float = 0) -> int:
    code = code.strip().upper()
    if kind not in ("raw", "consumable", "finished"):
        raise InventoryError(f"Unknown item kind {kind!r}")
    if reorder_level < 0:
        raise InventoryError("Reorder level can't be negative")
    try:
        cur = conn.execute("INSERT INTO items (code, name, kind, uom, reorder_level) VALUES (?,?,?,?,?)",
                           (code, name.strip(), kind, uom.strip(), reorder_level))
    except sqlite3.IntegrityError:
        raise Conflict(f"Item {code} already exists")
    conn.commit()
    return cur.lastrowid


def set_bom(conn, finished_code: str, components: list[dict]) -> None:
    """Replace the BOM of a finished item. components: [{code, qty_per_unit, scrap_pct}]"""
    fin = get_item(conn, finished_code)
    if fin["kind"] != "finished":
        raise InventoryError(f"{fin['code']} is not a finished item, it can't have a BOM")
    if not components:
        raise InventoryError("A BOM needs at least one component")
    rows = []
    for c in components:
        comp = get_item(conn, c["code"])
        if comp["id"] == fin["id"]:
            raise InventoryError("An item can't be a component of itself")
        if comp["kind"] == "finished":
            raise InventoryError(f"{comp['code']} is a finished item; only raw/consumable items can be components")
        if c["qty_per_unit"] <= 0:
            raise InventoryError(f"Quantity per unit for {comp['code']} must be more than zero")
        rows.append((fin["id"], comp["id"], c["qty_per_unit"], c.get("scrap_pct", 0)))
    with conn:
        conn.execute("DELETE FROM bom WHERE finished_id = ?", (fin["id"],))
        conn.executemany("INSERT INTO bom VALUES (?,?,?,?)", rows)


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------

@dataclass
class Posted:
    txn_id: int
    kind: str
    movements: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _check_duplicate_ref(conn, kind: str, ref: str | None) -> None:
    if not ref:
        return
    row = conn.execute(
        """SELECT id, created_at FROM txns WHERE kind = ? AND UPPER(ref) = UPPER(?)
           AND reversed_by_txn_id IS NULL""", (kind, ref.strip())).fetchone()
    if row:
        raise Conflict(f"{kind.title()} {ref!r} was already entered (entry #{row['id']} on "
                       f"{row['created_at']}). If that entry was wrong, reverse it first.",
                       existing_txn_id=row["id"])


def _shortages(conn, needs: dict[int, float]) -> list[Shortage]:
    out = []
    for item_id, needed in needs.items():
        have = stock_of(conn, item_id)
        if q(have - needed) < 0:
            it = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            out.append(Shortage(it["code"], it["name"], it["uom"], q(needed), have))
    return out


def _post(conn, kind: str, lines: list[tuple[int, float]], *, ref=None, note=None, user=None,
          override_reason=None, reverses=None) -> Posted:
    """Write one transaction and its movements atomically."""
    with conn:
        cur = conn.execute(
            """INSERT INTO txns (kind, ref, note, user, override_reason, reverses_txn_id, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (kind, ref.strip() if ref else None, note, user, override_reason, reverses, plant_now()))
        txn_id = cur.lastrowid
        moves = []
        for item_id, qty in lines:
            if q(qty) == 0:
                continue
            conn.execute("INSERT INTO movements (txn_id, item_id, qty) VALUES (?,?,?)", (txn_id, item_id, q(qty)))
            code = conn.execute("SELECT code FROM items WHERE id = ?", (item_id,)).fetchone()["code"]
            moves.append({"item": code, "qty": q(qty)})
        if reverses:
            conn.execute("UPDATE txns SET reversed_by_txn_id = ? WHERE id = ?", (txn_id, reverses))
    return Posted(txn_id, kind, moves)


def _require_positive(qty: float, what: str = "Quantity") -> None:
    if qty is None or not math.isfinite(qty) or qty <= 0:
        raise InventoryError(f"{what} must be more than zero")


def receive(conn, code: str, qty: float, *, grn: str | None = None, supplier: str | None = None,
            user: str | None = None) -> Posted:
    """Goods receipt (GRN) of raw material / consumables."""
    _require_positive(qty)
    item = get_item(conn, code)
    if item["kind"] == "finished":
        raise InventoryError(f"{item['code']} is a finished item — it comes from production, not a GRN")
    _check_duplicate_ref(conn, "receipt", grn)
    return _post(conn, "receipt", [(item["id"], qty)], ref=grn,
                 note=f"Supplier: {supplier}" if supplier else None, user=user)


def material_required(conn, finished_code: str, units: float) -> list[dict]:
    """How much of each component `units` pieces will consume (scrap included)."""
    fin = get_item(conn, finished_code)
    bom = bom_for(conn, fin["id"])
    if not bom:
        raise InventoryError(f"{fin['code']} has no BOM set up, so material consumption can't be worked out")
    return [{"item_id": b["component_id"], "code": b["code"], "uom": b["uom"],
             "qty": q(units * b["qty_per_unit"] * (1 + b["scrap_pct"] / 100))} for b in bom]


def produce(conn, code: str, good_qty: float, rejected_qty: float = 0, *, batch: str | None = None,
            user: str | None = None, override_reason: str | None = None) -> Posted:
    """Record production: add good pieces to finished stock, consume BOM material.

    Rejected pieces still used material, so consumption is for good + rejected.
    """
    if good_qty is None or good_qty < 0 or rejected_qty is None or rejected_qty < 0:
        raise InventoryError("Quantities can't be negative")
    if good_qty + rejected_qty <= 0:
        raise InventoryError("Nothing produced — good and rejected are both zero")
    fin = get_item(conn, code)
    if fin["kind"] != "finished":
        raise InventoryError(f"{fin['code']} is not a finished item")
    _check_duplicate_ref(conn, "production", batch)

    needs = material_required(conn, code, good_qty + rejected_qty)
    shortages = _shortages(conn, {n["item_id"]: n["qty"] for n in needs})
    if shortages and not (override_reason and override_reason.strip()):
        raise InsufficientStock(shortages)

    lines = [(n["item_id"], -n["qty"]) for n in needs] + [(fin["id"], good_qty)]
    note = f"Good {good_qty:g}, rejected {rejected_qty:g}"
    posted = _post(conn, "production", lines, ref=batch, note=note, user=user,
                   override_reason=override_reason.strip() if shortages else None)
    posted.warnings = [f"{s.code} is now negative ({q(s.available - s.needed):g} {s.uom}) — "
                       f"override: {override_reason}" for s in shortages]
    return posted


def record_wastage(conn, code: str, qty: float, reason: str, *, user: str | None = None) -> Posted:
    _require_positive(qty)
    if not reason or not reason.strip():
        raise InventoryError("Wastage needs a reason (rust, damage in handling, QC reject...)")
    item = get_item(conn, code)
    shortages = _shortages(conn, {item["id"]: qty})
    if shortages:
        raise InsufficientStock(shortages)
    return _post(conn, "wastage", [(item["id"], -qty)], note=reason.strip(), user=user)


def dispatch(conn, code: str, qty: float, *, invoice: str | None = None, user: str | None = None) -> Posted:
    _require_positive(qty)
    item = get_item(conn, code)
    if item["kind"] != "finished":
        raise InventoryError(f"{item['code']} is not a finished item")
    _check_duplicate_ref(conn, "dispatch", invoice)
    shortages = _shortages(conn, {item["id"]: qty})
    if shortages:
        raise InsufficientStock(shortages)  # no override: you can't ship what isn't there
    return _post(conn, "dispatch", [(item["id"], -qty)], ref=invoice, user=user)


def stock_count(conn, code: str, counted: float, reason: str | None = None, *,
                user: str | None = None) -> Posted:
    """Physical count. Posts the difference as an adjustment; doesn't overwrite history."""
    if counted is None or counted < 0:
        raise InventoryError("Counted quantity can't be negative")
    item = get_item(conn, code)
    diff = q(counted - stock_of(conn, item["id"]))
    if diff == 0:
        return Posted(0, "stock_count", [], ["Count matches system stock — nothing to adjust"])
    if not reason or not reason.strip():
        raise InventoryError(f"Count differs from system by {diff:g} {item['uom']}. Give a reason for the adjustment.",
                             difference=diff)
    return _post(conn, "adjustment", [(item["id"], diff)], note=reason.strip(), user=user)


def reverse(conn, txn_id: int, reason: str, *, user: str | None = None,
            override_reason: str | None = None) -> Posted:
    """Undo a whole entry by posting its opposite. History stays intact."""
    if not reason or not reason.strip():
        raise InventoryError("Say why this entry is being reversed")
    txn = conn.execute("SELECT * FROM txns WHERE id = ?", (txn_id,)).fetchone()
    if txn is None:
        raise NotFound(f"No entry #{txn_id}")
    if txn["kind"] == "reversal":
        raise InventoryError("A reversal can't be reversed. Enter the transaction again instead.")
    if txn["reversed_by_txn_id"]:
        raise Conflict(f"Entry #{txn_id} was already reversed by #{txn['reversed_by_txn_id']}")
    moves = conn.execute("SELECT item_id, qty FROM movements WHERE txn_id = ?", (txn_id,)).fetchall()
    # Reversing a receipt whose material was already used would push stock negative.
    takes = {m["item_id"]: m["qty"] for m in moves if m["qty"] > 0}
    shortages = _shortages(conn, takes)
    if shortages and not (override_reason and override_reason.strip()):
        raise InsufficientStock(shortages)
    return _post(conn, "reversal", [(m["item_id"], -m["qty"]) for m in moves],
                 ref=f"#{txn_id}", note=reason.strip(), user=user,
                 override_reason=override_reason if shortages else None, reverses=txn_id)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def stock_report(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT i.*, COALESCE(SUM(m.qty), 0) AS stock FROM items i
           LEFT JOIN movements m ON m.item_id = i.id GROUP BY i.id ORDER BY i.kind DESC, i.code""").fetchall()
    out = []
    for r in rows:
        stock = q(r["stock"])
        status = "negative" if stock < 0 else "low" if stock <= r["reorder_level"] and r["reorder_level"] > 0 else "ok"
        out.append({"code": r["code"], "name": r["name"], "kind": r["kind"], "uom": r["uom"],
                    "stock": stock, "reorder_level": r["reorder_level"], "status": status})
    return out


def can_build(conn) -> list[dict]:
    """For each finished item: how many more can be made from stock on hand, and what limits it."""
    out = []
    for fin in conn.execute("SELECT * FROM items WHERE kind = 'finished' ORDER BY code").fetchall():
        bom = bom_for(conn, fin["id"])
        if not bom:
            out.append({"code": fin["code"], "name": fin["name"], "can_build": None,
                        "limited_by": None, "note": "No BOM"})
            continue
        best, limiter = None, None
        for b in bom:
            per_unit = b["qty_per_unit"] * (1 + b["scrap_pct"] / 100)
            n = max(0, math.floor(q(stock_of(conn, b["component_id"]) / per_unit)))
            if best is None or n < best:
                best, limiter = n, b["code"]
        out.append({"code": fin["code"], "name": fin["name"], "can_build": best,
                    "limited_by": limiter, "note": None})
    return out


def alerts(conn) -> list[dict]:
    out = []
    for s in stock_report(conn):
        if s["status"] == "negative":
            out.append({"level": "error", "item": s["code"],
                        "message": f"{s['code']} is negative ({s['stock']:g} {s['uom']}). An override was used — "
                                   f"enter the missing GRN or check the BOM."})
        elif s["status"] == "low":
            out.append({"level": "warning", "item": s["code"],
                        "message": f"{s['code']} is at {s['stock']:g} {s['uom']}, at or below reorder level "
                                   f"{s['reorder_level']:g}. Raise a purchase order."})
    for t in conn.execute("""SELECT * FROM txns WHERE override_reason IS NOT NULL AND reversed_by_txn_id IS NULL
                             ORDER BY id DESC LIMIT 20""").fetchall():
        out.append({"level": "info", "item": None,
                    "message": f"Entry #{t['id']} ({t['kind']} {t['ref'] or ''}) was forced through: "
                               f"{t['override_reason']}"})
    return out


def ledger(conn, code: str) -> list[dict]:
    """Every movement for one item, oldest first, with a running balance."""
    item = get_item(conn, code)
    rows = conn.execute(
        """SELECT m.qty, t.id AS txn_id, t.kind, t.ref, t.note, t.user, t.created_at,
                  t.reversed_by_txn_id, t.override_reason
           FROM movements m JOIN txns t ON t.id = m.txn_id
           WHERE m.item_id = ? ORDER BY m.id""", (item["id"],)).fetchall()
    bal, out = 0.0, []
    for r in rows:
        bal = q(bal + r["qty"])
        out.append({**dict(r), "balance": bal})
    return out
