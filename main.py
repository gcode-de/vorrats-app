from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, httpx, asyncio
from datetime import date, datetime

DB_PATH = None  # Wird dynamisch gesetzt
OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"

app = FastAPI(title="Vorratsverwaltung")

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_db(user: str):
    global DB_PATH
    DB_PATH = f"./{user}.db"
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db(user: str):
    os.makedirs(os.path.dirname(f"./{user}.db"), exist_ok=True)
    con = get_db(user)
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
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root(request: Request):
    user = request.query_params.get("user")
    if not user:
        # Zeige User-Auswahl-Seite
        return HTMLResponse("""
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vorratsverwaltung - User auswählen</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; text-align: center; padding: 50px; background: #f8f8f6; color: #1a1a1a; }
        input { padding: 10px; margin: 10px; width: 200px; border: 1px solid #e0dfd8; border-radius: 6px; }
        button { padding: 10px 20px; background: #0f6e56; color: white; border: none; border-radius: 6px; cursor: pointer; }
        button:hover { background: #085041; }
    </style>
</head>
<body>
    <h1>🥫 Vorratsverwaltung</h1>
    <p>Gib deinen Namen ein, um deine Vorräte zu verwalten:</p>
    <input type="text" id="username" placeholder="Dein Name" />
    <br>
    <button onclick="selectUser()">Weiter</button>
    <script>
        // Cookie lesen
        function getCookie(name) {
            const value = `; ${document.cookie}`;
            const parts = value.split(`; ${name}=`);
            if (parts.length === 2) return parts.pop().split(';').shift();
            return null;
        }
        // Cookie setzen (1 Jahr Laufzeit)
        function setCookie(name, value, days) {
            const date = new Date();
            date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
            document.cookie = `${name}=${value}; expires=${date.toUTCString()}; path=/`;
        }
        // Beim Laden prüfen, ob Cookie vorhanden
        const savedUser = getCookie('vorrat_user');
        if (savedUser) {
            window.location.href = `/?user=${encodeURIComponent(savedUser)}`;
        }
        function selectUser() {
            const user = document.getElementById('username').value.trim();
            if (user) {
                setCookie('vorrat_user', user, 365); // 1 Jahr
                window.location.href = `/?user=${encodeURIComponent(user)}`;
            }
        }
        document.getElementById('username').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') selectUser();
        });
    </script>
</body>
</html>
        """)
    else:
        # Initialisiere DB für User
        init_db(user)
        # Serviere die Haupt-App
        return FileResponse("./static/index.html")

@app.get("/api/items")
def get_items(request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
    rows = con.execute("SELECT * FROM items ORDER BY name").fetchall()
    con.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stores")
def list_stores(request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
    rows = con.execute("SELECT DISTINCT store FROM items WHERE store IS NOT NULL ORDER BY store").fetchall()
    con.close()
    return [row["store"] for row in rows]

@app.post("/api/items", status_code=201)
def create_item(item: ItemCreate, request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
    cur = con.execute(
        "INSERT INTO items (name,qty,unit,cat,expiry,barcode,price,store,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item.name, item.qty, item.unit, item.cat, item.expiry or None, item.barcode or None, item.price, item.store, now())
    )
    con.commit()
    row = con.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    con.close()
    return row_to_dict(row)

@app.put("/api/items/{item_id}")
def update_item(item_id: int, item: ItemUpdate, request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
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
def change_qty(item_id: int, body: QtyChange, request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
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
def delete_item(item_id: int, request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    con.execute("DELETE FROM stock_events WHERE item_id=?", (item_id,))
    con.commit()
    con.close()

@app.get("/api/items/{item_id}/history")
def item_history(item_id: int, request: Request):
    user = request.query_params.get("user")
    if not user:
        raise HTTPException(status_code=400, detail="User required")
    init_db(user)
    con = get_db(user)
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

app.mount("/static", StaticFiles(directory="./static"), name="static")
