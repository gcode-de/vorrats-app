from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Tuple
import sqlite3, os, httpx, asyncio
from datetime import date
import datetime
import hashlib
import hmac
import re
import secrets
import time
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager

DB_PATH = None  # Wird dynamisch gesetzt
OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{barcode}.json"

scheduler = AsyncIOScheduler()
password_hasher = PasswordHasher(type=Type.ID)
LEGACY_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_COOKIE_NAME = "vorrat_session"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "43200"))
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
MAX_USERNAME_LENGTH = 64

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.add_job(send_weekly_report, CronTrigger(day_of_week=6, hour=12))  # 6=Sonntag, 12 Uhr
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()

app = FastAPI(title="Vorratsverwaltung", lifespan=lifespan)

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at)")
    con.commit()
    con.close()

def hash_password(password: str) -> str:
    return password_hasher.hash(password)

def verify_password(stored_hash: str, password: str) -> Tuple[bool, bool]:
    """Return whether the password matches and whether its hash needs upgrading."""
    if LEGACY_SHA256_PATTERN.fullmatch(stored_hash):
        legacy_hash = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(stored_hash, legacy_hash), True

    try:
        matches = password_hasher.verify(stored_hash, password)
    except (InvalidHashError, VerifyMismatchError):
        return False, False
    return matches, password_hasher.check_needs_rehash(stored_hash)

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
    try:
        row = con.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return False

        matches, needs_rehash = verify_password(row[0], password)
        if matches and needs_rehash:
            con.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
            con.commit()
        return matches
    finally:
        con.close()

def user_exists(username: str) -> bool:
    init_users_db()
    con = sqlite3.connect(get_users_db_path())
    row = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    con.close()
    return row is not None

def hash_session_token(token: str) -> str:
    # Session tokens have high entropy, so a fast hash is appropriate here.
    return hashlib.sha256(token.encode()).hexdigest()

def create_session(username: str) -> str:
    init_users_db()
    token = secrets.token_urlsafe(32)
    now_timestamp = int(time.time())
    con = sqlite3.connect(get_users_db_path())
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_timestamp,))
        con.execute("DELETE FROM sessions WHERE username = ?", (username,))
        con.execute(
            "INSERT INTO sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
            (hash_session_token(token), username, now_timestamp + SESSION_TTL_SECONDS),
        )
        con.commit()
    finally:
        con.close()
    return token

def get_session_user(token: Optional[str]) -> Optional[str]:
    if not token:
        return None

    init_users_db()
    now_timestamp = int(time.time())
    con = sqlite3.connect(get_users_db_path())
    try:
        row = con.execute(
            "SELECT username, expires_at FROM sessions WHERE token_hash = ?",
            (hash_session_token(token),),
        ).fetchone()
        if not row:
            return None
        if row[1] <= now_timestamp:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),))
            con.commit()
            return None
        return row[0]
    finally:
        con.close()

def delete_session(token: Optional[str]) -> None:
    if not token:
        return
    init_users_db()
    con = sqlite3.connect(get_users_db_path())
    try:
        con.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),))
        con.commit()
    finally:
        con.close()

def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )

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
    data_dir = os.path.abspath(get_data_dir())
    db_path = os.path.abspath(os.path.join(data_dir, f"{user}.db"))
    if os.path.commonpath((data_dir, db_path)) != data_dir:
        raise ValueError("Invalid user database path")
    return db_path

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
    con.execute("PRAGMA foreign_keys = ON;")
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
            name       TEXT,
            unit       TEXT,
            FOREIGN KEY(item_id) REFERENCES items(id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user       TEXT NOT NULL UNIQUE,
            telegram_token TEXT,
            telegram_chat_id TEXT
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
    try:
        con.execute("ALTER TABLE items ADD COLUMN location TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        con.execute("ALTER TABLE stock_events ADD COLUMN name TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        con.execute("ALTER TABLE stock_events ADD COLUMN unit TEXT")
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

class SettingsUpdate(BaseModel):
    telegram_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row_to_dict(row):
    if row is None:
        return None
    return dict(zip(row.keys(), row))

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def validate_login_data(data: LoginData) -> None:
    username = data.user
    username_is_valid = (
        username == username.strip()
        and 1 <= len(username) <= MAX_USERNAME_LENGTH
        and all(character.isalnum() or character in {" ", "_", "-"} for character in username)
    )
    if not username_is_valid:
        raise HTTPException(
            status_code=400,
            detail="Der Benutzername darf nur Buchstaben, Zahlen, Leerzeichen, _ und - enthalten.",
        )
    if not MIN_PASSWORD_LENGTH <= len(data.password) <= MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Das Passwort muss zwischen {MIN_PASSWORD_LENGTH} und {MAX_PASSWORD_LENGTH} Zeichen lang sein.",
        )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root(request: Request):
    user = get_session_user(request.cookies.get(SESSION_COOKIE_NAME))
    if user:
        init_db(user)
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
    validate_login_data(data)
    user = data.user
    password = data.password
    if verify_user(user, password):
        pass
    elif user_exists(user):
        raise HTTPException(status_code=401, detail="Falsches Passwort.")
    else:
        create_user(user, password)

    response = JSONResponse({"success": True})
    set_session_cookie(response, create_session(user))
    return response

@app.post("/api/logout", status_code=204)
def logout(request: Request):
    delete_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = Response(status_code=204)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return response

@app.get("/api/session")
def session_details(request: Request):
    return {"user": validate_user(request)}

def validate_user(request: Request):
    user = get_session_user(request.cookies.get(SESSION_COOKIE_NAME))
    if not user:
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

@app.post("/api/items/bulk-update")
def bulk_update_items(body: dict, request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    item_ids = body.get("item_ids", [])
    store = body.get("store")
    location = body.get("location")
    if not item_ids:
        con.close()
        raise HTTPException(400, "No item_ids provided")
    updates = {}
    if store is not None:
        updates["store"] = store
    if location is not None:
        updates["location"] = location
    if not updates:
        con.close()
        raise HTTPException(400, "No fields to update")
    sets = ", ".join(f"{k}=?" for k in updates)
    placeholders = ", ".join("?" for _ in item_ids)
    con.execute(f"UPDATE items SET {sets} WHERE id IN ({placeholders})", (*updates.values(), *item_ids))
    con.commit()
    con.close()
    return {"updated": len(item_ids)}

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

@app.post("/api/bulk-update")
def bulk_update_items(body: dict, request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    item_ids = body.get("item_ids", [])
    store = body.get("store")
    location = body.get("location")
    if not item_ids:
        con.close()
        raise HTTPException(400, "No item_ids provided")
    updates = {}
    if store is not None:
        updates["store"] = store
    if location is not None:
        updates["location"] = location
    if not updates:
        con.close()
        raise HTTPException(400, "No fields to update")
    sets = ", ".join(f"{k}=?" for k in updates)
    placeholders = ", ".join("?" for _ in item_ids)
    con.execute(f"UPDATE items SET {sets} WHERE id IN ({placeholders})", (*updates.values(), *item_ids))
    con.commit()
    con.close()
    return {"updated": len(item_ids)}

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
    con.execute("INSERT INTO stock_events (item_id,delta,note,created_at,name,unit) VALUES (?,?,?,?,?,?)",
                (item_id, body.delta, body.note, now(), item["name"], item["unit"]))
    con.commit()
    row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    con.close()
    return row_to_dict(row)

@app.delete("/api/items/{item_id}", status_code=204)
def delete_item(item_id: int, request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    item = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if item:
        con.execute("INSERT INTO stock_events (item_id,delta,note,created_at,name,unit) VALUES (?,?,?,?,?,?)",
                    (item_id, -item["qty"], "deleted", now(), item["name"], item["unit"]))
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    # Keep stock_events
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

@app.get("/api/recently_taken")
def get_recently_taken(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    # Berechne den Montag der aktuellen Woche (00:00 Uhr)
    monday = con.execute("""
        SELECT datetime('now', 'start of day', '-' || ((strftime('%w', 'now') + 6) % 7) || ' days') AS monday
    """).fetchone()["monday"]
    rows = con.execute("""
        SELECT COALESCE(i.name, se.name) as name, COALESCE(i.unit, se.unit) as unit, COALESCE(i.cat, 'food') as cat, COALESCE(i.expiry, '') as expiry, COALESCE(i.price, 0) as price, COALESCE(i.store, '') as store, COALESCE(i.location, '') as location, i.barcode, i.created_at, MAX(se.created_at) as last_taken,
        CASE WHEN SUM(se.delta) < 0 THEN -SUM(se.delta) ELSE 0 END as total_taken
        FROM stock_events se
        LEFT JOIN items i ON i.id = se.item_id
        WHERE se.created_at >= ?
        GROUP BY se.item_id
        HAVING SUM(se.delta) < 0
        ORDER BY last_taken DESC
    """, (monday,)).fetchall()
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

@app.get("/api/settings")
def get_settings(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    row = con.execute("SELECT telegram_token, telegram_chat_id FROM settings WHERE user = ?", (user,)).fetchone()
    con.close()
    if row:
        return {"telegram_token": row["telegram_token"], "telegram_chat_id": row["telegram_chat_id"]}
    return {"telegram_token": None, "telegram_chat_id": None}

@app.put("/api/settings")
def update_settings(settings: SettingsUpdate, request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    con.execute("""
        INSERT INTO settings (user, telegram_token, telegram_chat_id)
        VALUES (?, ?, ?)
        ON CONFLICT(user) DO UPDATE SET
            telegram_token = excluded.telegram_token,
            telegram_chat_id = excluded.telegram_chat_id
    """, (user, settings.telegram_token, settings.telegram_chat_id))
    con.commit()
    con.close()
    return {"success": True}

@app.post("/api/test_telegram")
async def test_telegram(request: Request):
    user = validate_user(request)
    init_db(user)
    con = get_db(user)
    row = con.execute("SELECT telegram_token, telegram_chat_id FROM settings WHERE user = ?", (user,)).fetchone()
    con.close()
    if not row or not row["telegram_token"] or not row["telegram_chat_id"]:
        raise HTTPException(400, "Telegram settings not configured")
    await send_weekly_report_for_user(user)
    return {"success": True}

# ---------------------------------------------------------------------------
# Telegram and Scheduler
# ---------------------------------------------------------------------------

async def send_weekly_report():
    # Get all users with telegram settings
    users_db = sqlite3.connect(get_users_db_path())
    users_db.row_factory = sqlite3.Row
    users = users_db.execute("SELECT username FROM users").fetchall()
    users_db.close()
    for user_row in users:
        user = user_row["username"]
        con = get_db(user)
        row = con.execute("SELECT telegram_token, telegram_chat_id FROM settings WHERE user = ?", (user,)).fetchone()
        con.close()
        if row and row["telegram_token"] and row["telegram_chat_id"]:
            await send_weekly_report_for_user(user)

async def send_weekly_report_for_user(user: str):
    init_db(user)
    con = get_db(user)
    monday = con.execute("""
        SELECT datetime('now', 'start of day', '-' || ((strftime('%w', 'now') + 6) % 7) || ' days') AS monday
    """).fetchone()["monday"]
    rows = con.execute("""
        SELECT COALESCE(i.name, se.name) as name, 
        CASE WHEN SUM(se.delta) < 0 THEN -SUM(se.delta) ELSE 0 END as total_taken, 
        COALESCE(i.unit, se.unit) as unit
        FROM stock_events se
        LEFT JOIN items i ON i.id = se.item_id
        WHERE se.created_at >= ?
        GROUP BY se.item_id
        HAVING SUM(se.delta) < 0
        ORDER BY total_taken DESC
    """, (monday,)).fetchall()
    con.close()
    
    if not rows:
        message = "🥫Vorrats-App:\nKeine Entnahmen diese Woche."
    else:
        message = "🥫Vorrats-App\nDiese Woche entnommen:\n"
        for row in rows:
            message += f"- {row['name']}: {row['total_taken']} {row['unit']}\n"
    
    # Get settings
    con = get_db(user)
    row = con.execute("SELECT telegram_token, telegram_chat_id FROM settings WHERE user = ?", (user,)).fetchone()
    con.close()
    if not row or not row["telegram_token"] or not row["telegram_chat_id"]:
        return
    
    token = row["telegram_token"]
    chat_id = row["telegram_chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": message})

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="./static"), name="static")
