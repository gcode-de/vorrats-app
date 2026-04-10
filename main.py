from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, httpx, asyncio
from datetime import date, datetime

DB_PATH = os.environ.get("DB_PATH", "./vorrat.db")
OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"

app = FastAPI(title="Vorratsverwaltung")

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = get_db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            qty       INTEGER NOT NULL DEFAULT 1,
            unit      TEXT NOT NULL DEFAULT 'Stk',
            cat       TEXT NOT NULL DEFAULT 'food',
            expiry    TEXT,
            barcode   TEXT,
            price     REAL,
            store     TEXT,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL,
            delta      INTEGER NOT NULL,
            note       TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(item_id) REFERENCES items(id)
        )
    """)
    # Add missing columns if they don't exist
    try:
        con.execute("ALTER TABLE items ADD COLUMN price REAL")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        con.execute("ALTER TABLE items ADD COLUMN store TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    con.commit()
    con.close()

init_db()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ItemCreate(BaseModel):
    name: str
    qty: int = 1
    unit: str = "Stk"
    cat: str = "food"
    expiry: Optional[str] = None
    barcode: Optional[str] = None
    price: Optional[float] = None
    store: Optional[str] = None

class ItemUpdate(BaseModel):
    name: Optional[str] = None
    qty: Optional[int] = None
    unit: Optional[str] = None
    cat: Optional[str] = None
    expiry: Optional[str] = None
    barcode: Optional[str] = None
    price: Optional[float] = None
    store: Optional[str] = None

class QtyChange(BaseModel):
    delta: int
    note: Optional[str] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row_to_dict(row):
    return dict(row)

def now():
    return datetime.utcnow().isoformat()

# ---------------------------------------------------------------------------
# Routes – items
# ---------------------------------------------------------------------------
@app.get("/api/items")
def get_items():
    con = get_db()
    rows = con.execute("SELECT * FROM items ORDER BY name").fetchall()
    con.close()
    return [row_to_dict(r) for r in rows]
@app.get("/api/stores")
def list_stores():
    con = get_db()
    rows = con.execute("SELECT DISTINCT store FROM items WHERE store IS NOT NULL ORDER BY store").fetchall()
    con.close()
    return [row["store"] for row in rows]

@app.post("/api/items", status_code=201)
def create_item(item: ItemCreate):
    con = get_db()
    cur = con.execute(
        "INSERT INTO items (name,qty,unit,cat,expiry,barcode,price,store,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item.name, item.qty, item.unit, item.cat, item.expiry or None, item.barcode or None, item.price, item.store, now())
    )
    con.commit()
    row = con.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return row_to_dict(row)

@app.put("/api/items/{item_id}")
def update_item(item_id: int, item: ItemUpdate):
    con = get_db()
    existing = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not existing:
        con.close()
        raise HTTPException(404, "Item not found")
    fields = {k: v for k, v in item.dict().items() if v is not None}
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))
        con.commit()
    row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    con.close()
    return row_to_dict(row)

@app.post("/api/items/{item_id}/qty")
def change_qty(item_id: int, body: QtyChange):
    con = get_db()
    item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        con.close()
        raise HTTPException(404, "Item not found")
    new_qty = max(0, item["qty"] + body.delta)
    con.execute("UPDATE items SET qty=? WHERE id=?", (new_qty, item_id))
    con.execute("INSERT INTO stock_events (item_id,delta,note,created_at) VALUES (?,?,?,?)",
                (item_id, body.delta, body.note, now()))
    con.commit()
    row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    con.close()
    return row_to_dict(row)

@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int):
    con = get_db()
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    con.execute("DELETE FROM stock_events WHERE item_id=?", (item_id,))
    con.commit()
    con.close()

@app.get("/api/items/{item_id}/history")
def item_history(item_id: int):
    con = get_db()
    rows = con.execute(
        "SELECT * FROM stock_events WHERE item_id=? ORDER BY created_at DESC LIMIT 50",
        (item_id,)
    ).fetchall()
    con.close()
    return [row_to_dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Barcode lookup – OpenFoodFacts
# ---------------------------------------------------------------------------

@app.get("/api/barcode/{barcode}")
async def lookup_barcode(barcode: str):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(OPENFOODFACTS_URL.format(barcode=barcode))
            data = r.json()
        if data.get("status") != 1:
            return {"found": False}
        p = data["product"]
        return {
            "found": True,
            "barcode": barcode,
            "name": p.get("product_name_de") or p.get("product_name") or "",
            "quantity": p.get("quantity", ""),
            "brands": p.get("brands", ""),
            "image": p.get("image_front_small_url", ""),
        }
    except Exception:
        return {"found": False}

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

# app.mount("/static", StaticFiles(directory="./static"), name="static")

app.mount("/static", StaticFiles(directory="./static"), name="static")

@app.get("/")
def root():
    return FileResponse("./static/index.html")

# ---------------------------------------------------------------------------
# Install requirements
# ---------------------------------------------------------------------------

os.system("pip install -r requirements.txt")
