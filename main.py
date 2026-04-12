from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, os, httpx, asyncio
from datetime import date, datetime
import hashlib

DB_PATH = None  # Wird dynamisch gesetzt
OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"

app = FastAPI(title="Vorratsverwaltung")

# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

def init_users_db():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    con = sqlite3.connect(get_users_db_path())
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_user(username: str, password: str):
    init_users_db()
    con = sqlite3.connect(get_users_db_path())
    try:
        con.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hash_password(password)))
        con.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="User already exists")
    finally:
        con.close()

def verify_user(username: str, password: str) -> bool:
    init_users_db()
    con = sqlite3.connect(get_users_db_path())
    row = con.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    con.close()
    if row:
        return row[0] == hash_password(password)
    return False

def user_exists(username: str) -> bool:
    init_users_db()
    con = sqlite3.connect(get_users_db_path())
    row = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    con.close()
    return row is not None

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def get_data_dir():
    # Prüfe, ob /data beschreibbar ist (für Container), sonst ./data (für lokale Entwicklung)
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        return "/data"
    else:
        return "./data"

def get_db_path(user: str):
    return f"{get_data_dir()}/{user}.db"

def get_users_db_path():
    return f"{get_data_dir()}/users.db"

def get_db(user: str):
    con = sqlite3.connect(get_db_path(user))
    con.row_factory = sqlite3.Row
    return con

def init_db(user: str):
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
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
    location: Optional[str] = None

class ItemUpdate(BaseModel):
    name: Optional[str] = None
    qty: Optional[int] = None
    unit: Optional[str] = None
    cat: Optional[str] = None
    expiry: Optional[str] = None
    barcode: Optional[str] = None
    price: Optional[float] = None
    store: Optional[str] = None
    location: Optional[str] = None

class QtyChange(BaseModel):
    delta: int
    note: Optional[str] = None

class LoginData(BaseModel):
    user: str
    password: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row_to_dict(row):
    if row is None:
        return None
    return dict(zip(row.keys(), row))

def now():
    return datetime.utcnow().isoformat()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root(request: Request):
    cookie_user = request.cookies.get("vorrat_user")
    if cookie_user and user_exists(cookie_user):
        init_db(cookie_user)
        return FileResponse("./static/index.html")
    else:
        return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vorratsverwaltung - Anmelden</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; text-align: center; padding: 50px; background: #f8f8f6; color: #1a1a1a; }}
        input {{ padding: 10px; margin: 10px; width: 200px; border: 1px solid #e0dfd8; border-radius: 6px; }}
        button {{ padding: 10px 20px; background: #0f6e56; color: white; border: none; border-radius: 6px; cursor: pointer; }}
        button:hover {{ background: #085041; }}
        .error {{ color: #a32d2d; font-size: 14px; margin-top: 10px; }}
    </style>
</head>
<body>
    <h1>🥫 Vorratsverwaltung</h1>
    <p>Melde dich an oder erstelle ein neues Konto:</p>
    <input type="text" id="username" placeholder="Dein Name" value="" />
    <br>
    <input type="password" id="password" placeholder="Passwort" />
    <br>
    <button onclick="login()">Anmelden / Registrieren</button>
    <div id="error" class="error" style="display: none;"></div>
    <script>
        // Cookie lesen
        function getCookie(name) {{
            const value = `; ${{document.cookie}}`;
            const parts = value.split(`; ${{name}}=`);
            if (parts.length === 2) return parts.pop().split(';').shift();
            return null;
        }}
        // Cookie setzen (1 Jahr Laufzeit)
        function setCookie(name, value, days) {{
            const date = new Date();
            date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
            document.cookie = `${{name}}=${{value}}; expires=${{date.toUTCString()}}; path=/`;
        }}
        async function login() {{
            const user = document.getElementById('username').value.trim();
            const pass = document.getElementById('password').value;
            if (!user || !pass) {{
                showError('Bitte Name und Passwort eingeben.');
                return;
            }}
            try {{
                const response = await fetch('/api/login', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ user: user, password: pass }})
                }});
                if (response.ok) {{
                    setCookie('vorrat_user', user, 365);
                    window.location.href = '/';
                }} else {{
                    const data = await response.json();
                    showError(data.detail || 'Anmeldung fehlgeschlagen.');
                }}
            }} catch (e) {{
                showError('Netzwerkfehler.');
            }}
        }}
        function showError(msg) {{
            document.getElementById('error').textContent = msg;
            document.getElementById('error').style.display = 'block';
        }}
        document.getElementById('username').addEventListener('keypress', function(e) {{
            if (e.key === 'Enter') document.getElementById('password').focus();
        }});
        document.getElementById('password').addEventListener('keypress', function(e) {{
            if (e.key === 'Enter') login();
        }});
    </script>
</body>
</html>
""")

@app.post("/api/login")
def login(data: LoginData):
    user = data.user
    password = data.password
    if verify_user(user, password):
        return {"success": True}
    elif user_exists(user):
        raise HTTPException(status_code=401, detail="Falsches Passwort.")
    else:
        create_user(user, password)
        return {"success": True}

def validate_user(request: Request):
    user = request.cookies.get("vorrat_user")
    if not user or not user_exists(user):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user

@app.get("/api/items")
def get_items(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    rows = con.execute("SELECT * FROM items ORDER BY name").fetchall()
    con.close()
    return [row_to_dict(r) for r in rows]

@app.get("/api/stores")
def list_stores(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    rows = con.execute("SELECT DISTINCT store FROM items WHERE store IS NOT NULL ORDER BY store").fetchall()
    con.close()
    return [row["store"] for row in rows]

@app.get("/api/locations")
def list_locations(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    rows = con.execute("SELECT DISTINCT location FROM items WHERE location IS NOT NULL ORDER BY location").fetchall()
    con.close()
    return [row["location"] for row in rows]

@app.post("/api/items", status_code=201)
def create_item(item: ItemCreate, request: Request):
    user = validate_user(request)
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
    user = validate_user(request)
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
    user = validate_user(request)
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
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    con.execute("DELETE FROM stock_events WHERE item_id=?", (item_id,))
    con.commit()
    con.close()

@app.get("/api/items/{item_id}/history")
def item_history(item_id: int, request: Request):
    user = validate_user(request)
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
