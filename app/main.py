import os
import sqlite3
import secrets
import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import bcrypt
import requests
from bs4 import BeautifulSoup

from fastapi import (
    FastAPI, Request, Form, Depends, Body, HTTPException, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# -------------------------------------------------
# Crawl state (in-memory, per container)
# -------------------------------------------------
CRAWL_STATE = {
    "status": "idle",        # idle | running | stopped | finished
    "pages": 0,
    "recipes": 0,
    "site_id": None,
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
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS crawl_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER,
    url TEXT,
    status TEXT,              -- queued | processing | done | error
    discovered_at TEXT,
    UNIQUE(site_id, url)
    )
    """)

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
        name TEXT,
        start_url TEXT,
        recipe_pattern TEXT,
        ingredients_selector TEXT,
        method_selector TEXT,
        max_concurrency INTEGER DEFAULT 4,
        request_delay REAL DEFAULT 0.8,
        max_pages INTEGER DEFAULT 0,
        max_recipes INTEGER DEFAULT 0,
        user_agent TEXT DEFAULT 'MealieRecipeCrawler/1.0',
        created_at TEXT
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
# Helpers – Active site (single source of truth)
# -------------------------------------------------
def get_active_site():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key='active_site_id'")
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    cur.execute("SELECT * FROM sites WHERE id=?", (row["value"],))
    site = cur.fetchone()
    conn.close()
    return site


def set_active_site(site_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO settings (key,value) VALUES ('active_site_id',?)",
        (str(site_id),)
    )
    conn.commit()
    conn.close()


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
def enqueue_url(site_id, url):
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO crawl_queue
            (site_id, url, status, discovered_at)
            VALUES (?, ?, 'queued', ?)
            """,
            (site_id, url, datetime.datetime.utcnow().isoformat())
        )
        conn.commit()
    finally:
        conn.close()


def get_next_url(site_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, url FROM crawl_queue
        WHERE site_id=? AND status='queued'
        ORDER BY id ASC
        LIMIT 1
        """,
        (site_id,)
    )
    row = cur.fetchone()

    if row:
        cur.execute(
            "UPDATE crawl_queue SET status='processing' WHERE id=?",
            (row["id"],)
        )
        conn.commit()

    conn.close()
    return row

def mark_url_done(row_id):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE crawl_queue SET status='done' WHERE id=?",
        (row_id,)
    )
    conn.commit()
    conn.close()
    
def get_active_site():
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT value FROM settings WHERE key='active_site_id'")
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    cur.execute("SELECT * FROM sites WHERE id=?", (row["value"],))
    site = cur.fetchone()
    conn.close()
    return site

def crawl_worker():
    site = get_active_site()
    if not site:
        log("ERROR", "No active site set")
        CRAWL_STATE["status"] = "idle"
        return

    site_id = site["id"]
    start_url = site["start_url"]
    domain = urlparse(start_url).netloc

    CRAWL_STATE.update({
        "status": "running",
        "pages": 0,
        "recipes": 0,
        "site_id": site_id,
    })

    log("INFO", f"Crawl started for '{site['name']}' {start_url}", site_id=site_id)

    enqueue_url(site_id, start_url)

    max_pages = site["max_pages"] or 0
    max_recipes = site["max_recipes"] or 0

    while CRAWL_STATE["status"] == "running":
        row = get_next_url(site_id)
        if not row:
            break

        qid = row["id"]
        url = row["url"]

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": site["user_agent"]},
                timeout=15
            )
            soup = BeautifulSoup(resp.text, "html.parser")

            CRAWL_STATE["pages"] += 1

            # Recipe detection
            if site["recipe_pattern"] and site["recipe_pattern"] in url:
                CRAWL_STATE["recipes"] += 1
                conn = db()
                conn.execute(
                    "INSERT OR IGNORE INTO recipes (url, site_id, crawled_at) VALUES (?,?,?)",
                    (url, site_id, datetime.datetime.utcnow().isoformat())
                )
                conn.commit()
                conn.close()

            # Discover links
            for a in soup.select("a[href]"):
                href = a["href"]
                if href.startswith("/"):
                    href = f"https://{domain}{href}"
                if domain in href:
                    enqueue_url(site_id, href)

            mark_url_done(qid)

            if max_pages and CRAWL_STATE["pages"] >= max_pages:
                log("INFO", "Max pages reached", site_id=site_id)
                break

            if max_recipes and CRAWL_STATE["recipes"] >= max_recipes:
                log("INFO", "Max recipes reached", site_id=site_id)
                break

            delay = float(site["request_delay"] or 0)
            if delay:
                time.sleep(delay)

        except Exception as e:
            log("ERROR", str(e), url=url, site_id=site_id)
            mark_url_done(qid)

    CRAWL_STATE["status"] = "finished"
    log("INFO", "Crawl finished", site_id=site_id)

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

    cur.execute("SELECT COUNT(*) c FROM recipes")
    total = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) c FROM recipes WHERE uploaded=1")
    uploaded = cur.fetchone()["c"]

    cur.execute("SELECT id, name FROM sites ORDER BY name")
    sites = [dict(r) for r in cur.fetchall()]

    active_site = get_active_site()

    conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "sites": sites,
            "active_site": dict(active_site) if active_site else None,
            "crawl": {"status": "idle"},
            "upload": {"status": "idle"},
            "total_recipes": total,
            "uploaded_recipes": uploaded,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT key,value FROM settings")
    settings = {r["key"]: r["value"] for r in cur.fetchall()}

    cur.execute("SELECT * FROM sites ORDER BY name")
    sites = cur.fetchall()

    active_site = get_active_site()

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
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }
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
@app.get("/api/sites/list")
def api_sites_list(user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id,name,start_url FROM sites ORDER BY name")
    sites = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "sites": sites}


@app.post("/api/sites/set-active")
def api_sites_set_active(payload: dict = Body(...), user=Depends(current_user)):
    site_id = payload.get("site_id")
    if not site_id:
        raise HTTPException(status_code=400, detail="site_id required")
    set_active_site(int(site_id))
    site = get_active_site()
    log(
        "INFO",
        f"Active site set: {site['name']} (id={site['id']}) {site['start_url']}",
        site_id=site["id"],
        url=site["start_url"],
    )
    return {"ok": True}


@app.post("/api/sites/save")
def api_sites_save(payload: dict = Body(...), user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    site_id = payload.get("id")

    fields = (
        payload.get("name", "New Site"),
        payload.get("start_url", ""),
        payload.get("recipe_pattern", ""),
        payload.get("ingredients_selector", ""),
        payload.get("method_selector", ""),
        int(payload.get("max_concurrency", 4)),
        float(payload.get("request_delay", 0.8)),
        int(payload.get("max_pages", 0)),
        int(payload.get("max_recipes", 0)),
        payload.get("user_agent", "MealieRecipeCrawler/1.0"),
        datetime.datetime.utcnow().isoformat(),
    )

    if site_id:
        cur.execute("""
            UPDATE sites SET
              name=?, start_url=?, recipe_pattern=?,
              ingredients_selector=?, method_selector=?,
              max_concurrency=?, request_delay=?,
              max_pages=?, max_recipes=?, user_agent=?
            WHERE id=?
        """, fields[:-1] + (site_id,))
    else:
        cur.execute("""
            INSERT INTO sites VALUES
            (NULL,?,?,?,?,?,?,?,?,?,?,?)
        """, fields)

    conn.commit()
    conn.close()

    log("INFO", "Site saved", url=payload.get("start_url"))
    return {"ok": True}


@app.post("/api/sites/prescan")
def api_sites_prescan(payload: dict = Body(...), user=Depends(current_user)):
    return {
        "ok": True,
        "recipe_pattern": "/recipe",
        "ingredients_selector": ".ingredients li",
        "method_selector": ".method li",
    }
    
@app.get("/api/crawl/logs")
def api_crawl_logs(
    after_id: int = Query(0, ge=0),
    user=Depends(current_user)
):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT id, site_id, level, message, url, created_at
        FROM crawl_logs
        WHERE id > ?
        ORDER BY id ASC
        LIMIT 200
        """,
        (after_id,)
    )

    logs = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "ok": True,
        "logs": logs,
        "last_id": logs[-1]["id"] if logs else after_id,
    }

# -------------------------------------------------
# API – Crawl (safe stub)
# -------------------------------------------------
import threading

@app.post("/api/crawl/start")
def crawl_start(user=Depends(current_user)):
    if CRAWL_STATE["status"] == "running":
        return {"ok": False, "message": "Crawl already running"}

    t = threading.Thread(target=crawl_worker, daemon=True)
    t.start()

    return {"ok": True}



@app.get("/api/progress")
def api_progress(user=Depends(current_user)):
    return {
        "crawl": {
            "status": CRAWL_STATE["status"],
            "pages": CRAWL_STATE["pages"],
            "recipes": CRAWL_STATE["recipes"],
        },
        "upload": {
            "status": "idle",
            "done": 0,
            "total": 0,
        },
    }



@app.get("/api/meta")
def api_meta():
    return {
        "name": "Mealie Recipe Crawler",
        "version": os.getenv("APP_VERSION", "dev"),
    }
