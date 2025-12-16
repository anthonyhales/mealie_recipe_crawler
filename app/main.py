import os
import sqlite3
import secrets
import datetime
from urllib.parse import urlparse

import bcrypt
from bs4 import BeautifulSoup
import threading
import time

from fastapi import (
    FastAPI, Request, Form, Depends, Body, HTTPException, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

CRAWL_STATE = {
    "running": False,
    "site_id": None,
    "site_name": None,
    "start_url": None,
    "pages": 0,
    "recipes": 0,
    "started_at": None,
}

# -------------------------------------------------
# Paths / Environment
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET env var must be set")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

# -------------------------------------------------
# App
# -------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)

templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates")
)

# -------------------------------------------------
# Database
# -------------------------------------------------
def db():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        role TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        start_url TEXT NOT NULL,
        recipe_pattern TEXT NOT NULL,
        ingredients_selector TEXT NOT NULL,
        method_selector TEXT NOT NULL,
        max_concurrency INTEGER NOT NULL DEFAULT 5,
        request_delay REAL NOT NULL DEFAULT 0.5,
        max_pages INTEGER NOT NULL DEFAULT 0,
        max_recipes INTEGER NOT NULL DEFAULT 0,
        user_agent TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        site_id INTEGER,
        crawled_at TEXT,
        uploaded INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS crawl_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        level TEXT,
        message TEXT,
        url TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    conn.commit()
    conn.close()


def ensure_admin():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM users")
    if cur.fetchone()["c"] == 0:
        password = ADMIN_PASS or secrets.token_urlsafe(12)
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur.execute(
            "INSERT INTO users VALUES (NULL,?,?,?,?)",
            (ADMIN_USER, pw_hash, "admin", datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        print("Admin created")
        print("username:", ADMIN_USER)
        print("password:", password)
    conn.close()


init_db()
ensure_admin()

# -------------------------------------------------
# Auth
# -------------------------------------------------
def current_user(request: Request):
    username = request.session.get("user")
    if not username:
        raise HTTPException(status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    user = cur.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=303)
    return user


# -------------------------------------------------
# Logging
# -------------------------------------------------
def log(level, message, url=None, site_id=None):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crawl_logs VALUES (NULL,?,?,?,?,?)",
        (site_id, level, message, url, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# -------------------------------------------------
# Helpers
# -------------------------------------------------
def get_active_site(conn):
    cur = conn.cursor()

    cur.execute("SELECT value FROM settings WHERE key='active_site_id'")
    row = cur.fetchone()
    if not row:
        return None

    cur.execute("SELECT * FROM sites WHERE id=?", (row["value"],))
    return cur.fetchone()



def validate_selectors(html, ingredients_sel, method_sel):
    soup = BeautifulSoup(html, "lxml")
    return bool(soup.select(ingredients_sel)) and bool(soup.select(method_sel))
    
def crawl_worker(site):
    CRAWL_STATE["running"] = True
    CRAWL_STATE["site_id"] = site["id"]
    CRAWL_STATE["site_name"] = site["name"]
    CRAWL_STATE["start_url"] = site["start_url"]
    CRAWL_STATE["pages"] = 0
    CRAWL_STATE["recipes"] = 0
    CRAWL_STATE["started_at"] = datetime.datetime.utcnow().isoformat()

    log(
        "INFO",
        f"Crawl started for site '{site['name']}' {site['start_url']}",
        site_id=site["id"],
        url=site["start_url"],
    )

    # ---- PLACEHOLDER LOOP ----
    # This is where your real crawler logic will go
    # For now, simulate work safely

    try:
        for i in range(1, 6):
            if not CRAWL_STATE["running"]:
                log("INFO", "Crawl stopped by user", site_id=site["id"])
                return

            time.sleep(site["request_delay"])
            CRAWL_STATE["pages"] += 1

            # fake recipe found every 2 pages
            if i % 2 == 0:
                CRAWL_STATE["recipes"] += 1

        log("INFO", "Crawl finished", site_id=site["id"])
    finally:
        CRAWL_STATE["running"] = False

# -------------------------------------------------
# Pages
# -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    user = cur.fetchone()
    conn.close()
    if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        request.session["user"] = username
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Invalid credentials"}
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    # Active site
    cur.execute(
        """
        SELECT s.*
        FROM sites s
        JOIN settings st ON st.value = CAST(s.id AS TEXT)
        WHERE st.key = 'active_site_id'
        """
    )
    site = cur.fetchone()

    # Recipe counts
    cur.execute("SELECT COUNT(*) c FROM recipes")
    total = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) c FROM recipes WHERE uploaded = 1")
    uploaded = cur.fetchone()["c"]

    conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "site": site,

            # REQUIRED by dashboard.html
            "crawl": {
                "status": "idle",
                "pages": 0,
                "recipes": total,
            },
            "upload": {
                "status": "idle",
                "done": uploaded,
                "total": total,
            },

            "total_recipes": total,
            "uploaded_recipes": uploaded,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT key, value FROM settings")
    settings = {r["key"]: r["value"] for r in cur.fetchall()}

    cur.execute("SELECT * FROM sites ORDER BY id ASC")
    sites = cur.fetchall()

    cur.execute("SELECT value FROM settings WHERE key='active_site_id'")
    row = cur.fetchone()

    active_site = None
    if row and row["value"]:
        try:
            active_id = int(row["value"])
            cur.execute("SELECT * FROM sites WHERE id=?", (active_id,))
            active_site = cur.fetchone()
        except ValueError:
            active_site = None

    conn.close()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "settings": settings,
            "sites": sites,
            "active_site": active_site,
        }
    )



@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    user=Depends(current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=10, le=200),
):
    offset = (page - 1) * page_size

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM recipes")
    total = cur.fetchone()["c"]

    cur.execute(
        "SELECT * FROM recipes ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    recipes = cur.fetchall()
    conn.close()

    total_pages = max(1, (total + page_size - 1) // page_size)

    return templates.TemplateResponse(
        "recipes.html",
        {
            "request": request,
            "user": user,
            "recipes": recipes,
            "page": page,
            "total_pages": total_pages,
        },
    )


@app.get("/crawl-logs", response_class=HTMLResponse)
def crawl_logs_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM crawl_logs ORDER BY id DESC LIMIT 300")
    logs = cur.fetchall()[::-1]
    conn.close()
    return templates.TemplateResponse(
        "crawl_logs.html", {"request": request, "user": user, "logs": logs}
    )


# -------------------------------------------------
# API – Sites
# -------------------------------------------------
@app.get("/api/sites/load")
def api_sites_load(user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT value FROM settings WHERE key='active_site_id'")
    row = cur.fetchone()
    active_id = int(row["value"]) if row else None

    site = None
    if active_id:
        cur.execute("SELECT * FROM sites WHERE id=?", (active_id,))
        site = cur.fetchone()

    conn.close()

    return {
        "ok": True,
        "active_site_id": active_id,
        "site": dict(site) if site else None,
    }

@app.get("/api/sites/list")
def api_sites_list(user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM sites ORDER BY id ASC")
    sites = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "sites": sites}

@app.post("/api/sites/save")
def api_sites_save(payload: dict = Body(...), user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sites (
            name, start_url, recipe_pattern,
            ingredients_selector, method_selector,
            max_concurrency, request_delay,
            max_pages, max_recipes, user_agent, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        payload["name"],
        payload["start_url"],
        payload["recipe_pattern"],
        payload["ingredients_selector"],
        payload["method_selector"],
        int(payload.get("max_concurrency", 5)),
        float(payload.get("request_delay", 0.5)),
        int(payload.get("max_pages", 0)),
        int(payload.get("max_recipes", 0)),
        payload.get("user_agent", "MealieRecipeCrawler/1.0"),
        datetime.datetime.utcnow().isoformat(),
    ))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/sites/delete")
def api_sites_delete(payload: dict = Body(...), user=Depends(current_user)):
    site_id = payload.get("site_id")
    if not site_id:
        raise HTTPException(400, "site_id required")

    active = get_active_site()
    if active and active["id"] == site_id:
        raise HTTPException(400, "Cannot delete active site")

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sites WHERE id=?", (site_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/sites/set-active")
def api_sites_set_active(payload: dict = Body(...), user=Depends(current_user)):
    raw = payload.get("site_id")
    if raw is None or str(raw).strip() == "":
        raise HTTPException(status_code=400, detail="site_id required")

    try:
        site_id = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="site_id must be an integer")

    conn = db()
    cur = conn.cursor()

    # Verify the site exists
    cur.execute("SELECT id, name, start_url FROM sites WHERE id=?", (site_id,))
    site = cur.fetchone()
    if not site:
        conn.close()
        raise HTTPException(status_code=404, detail="site not found")

    # Save active site id
    cur.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('active_site_id', ?)",
        (str(site_id),)
    )
    conn.commit()
    conn.close()

    log("INFO", f"Active site set: {site['name']} (id={site_id})", url=site["start_url"], site_id=site_id)
    return {"ok": True, "active_site_id": site_id}


@app.post("/api/sites/prescan")
def api_sites_prescan(payload: dict = Body(...), user=Depends(current_user)):
    return {
        "ok": True,
        "recipe_pattern": "/recipe",
        "ingredients_selector": ".ingredients li",
        "method_selector": ".method li",
    }


# -------------------------------------------------
# API – Crawl (wired to active site)
# -------------------------------------------------
@app.post("/api/crawl/start")
def crawl_start(user=Depends(current_user)):
    if CRAWL_STATE["running"]:
        raise HTTPException(status_code=400, detail="Crawl already running")

    conn = db()
    site = get_active_site(conn)
    conn.close()

    if not site:
        raise HTTPException(status_code=400, detail="No active site selected")

    t = threading.Thread(target=crawl_worker, args=(site,), daemon=True)
    t.start()

    return {"ok": True}
    
@app.post("/api/crawl/stop")
def crawl_stop(user=Depends(current_user)):
    if not CRAWL_STATE["running"]:
        return {"ok": True}

    CRAWL_STATE["running"] = False
    return {"ok": True}


@app.get("/api/progress")
def api_progress(user=Depends(current_user)):
    return {
        "crawl": {
            "status": "running" if CRAWL_STATE["running"] else "idle",
            "pages": CRAWL_STATE["pages"],
            "recipes": CRAWL_STATE["recipes"],
            "site": CRAWL_STATE["site_name"],
        },
        "upload": {
            "status": "idle",
            "done": 0,
            "total": 0,
        },
    }

    
@app.get("/api/crawl/logs")
def api_crawl_logs(
    after_id: int = Query(0),
    user=Depends(current_user)
):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM crawl_logs
        WHERE id > ?
        ORDER BY id ASC
        """,
        (after_id,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "logs": rows}



# -------------------------------------------------
# API – Upload (stub)
# -------------------------------------------------
@app.post("/api/upload/start")
def upload_start(user=Depends(current_user)):
    log("INFO", "Upload started")
    return {"ok": True}


# -------------------------------------------------
# API – Meta
# -------------------------------------------------
@app.get("/api/meta")
def api_meta():
    return {
        "name": "Mealie Recipe Crawler",
        "version": os.getenv("APP_VERSION", "dev"),
    }
