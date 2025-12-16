import os
import sqlite3
import secrets
import datetime
import threading
import time
from collections import deque
from urllib.parse import urlparse, urljoin

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
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
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
# Active Site Helper
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

# -------------------------------------------------
# Crawl State
# -------------------------------------------------
crawl_state = {
    "running": False,
    "pages": 0,
    "recipes": 0,
}

# -------------------------------------------------
# Crawl Worker
# -------------------------------------------------
def start_crawl_worker(site):
    if crawl_state["running"]:
        return
    t = threading.Thread(target=crawl_worker, args=(site,), daemon=True)
    t.start()


def crawl_worker(site):
    crawl_state["running"] = True
    crawl_state["pages"] = 0
    crawl_state["recipes"] = 0

    max_pages = int(site["max_pages"] or 0)
    max_recipes = int(site["max_recipes"] or 0)
    delay = float(site["request_delay"] or 0.5)

    queue = deque([site["start_url"]])
    seen = set()

    conn = db()
    cur = conn.cursor()

    try:
        while queue:
            if max_pages and crawl_state["pages"] >= max_pages:
                break
            if max_recipes and crawl_state["recipes"] >= max_recipes:
                break

            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)

            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": site["user_agent"]},
                    timeout=15,
                )
                crawl_state["pages"] += 1

                soup = BeautifulSoup(resp.text, "html.parser")

                cur.execute(
                    "INSERT OR IGNORE INTO recipes (url, site_id, crawled_at) VALUES (?,?,?)",
                    (url, site["id"], datetime.datetime.utcnow().isoformat()),
                )
                if cur.rowcount:
                    crawl_state["recipes"] += 1

                for a in soup.select("a[href]"):
                    href = a["href"]
                    full = urljoin(site["start_url"], href)
                    if full.startswith(site["start_url"]):
                        queue.append(full)

                conn.commit()
                time.sleep(delay)

            except Exception as e:
                log("ERROR", str(e), url=url, site_id=site["id"])

    finally:
        conn.close()
        crawl_state["running"] = False
        log("INFO", "Crawl finished", site_id=site["id"])

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


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) c FROM recipes")
    total = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) c FROM recipes WHERE uploaded=1")
    uploaded = cur.fetchone()["c"]
    conn.close()

    site = get_active_site()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "crawl": {
                "status": "running" if crawl_state["running"] else "idle",
                "pages": crawl_state["pages"],
                "recipes": crawl_state["recipes"],
            },
            "upload": {"status": "idle"},
            "total_recipes": total,
            "uploaded_recipes": uploaded,
            "active_site": site,
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
        },
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
        """
        SELECT * FROM recipes
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
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
        },
    )

@app.get("/crawl-logs", response_class=HTMLResponse)
def crawl_logs_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM crawl_logs ORDER BY id DESC LIMIT 500")
    logs = cur.fetchall()[::-1]

    conn.close()

    return templates.TemplateResponse(
        "crawl_logs.html",
        {
            "request": request,
            "user": user,
            "logs": logs,
        },
    )


# -------------------------------------------------
# API – Progress
# -------------------------------------------------
@app.get("/api/progress")
def api_progress(user=Depends(current_user)):
    return {
        "crawl": {
            "status": "running" if crawl_state["running"] else "idle",
            "pages": crawl_state["pages"],
            "recipes": crawl_state["recipes"],
        },
        "upload": {
            "status": "idle",
            "done": 0,
            "total": 0,
        },
    }

# -------------------------------------------------
# API – Crawl
# -------------------------------------------------
@app.post("/api/crawl/start")
def crawl_start(user=Depends(current_user)):
    site = get_active_site()
    if not site:
        raise HTTPException(status_code=400, detail="No active site selected")
    log(
        "INFO",
        f"Crawl started for '{site['name']}'",
        site_id=site["id"],
        url=site["start_url"],
    )
    start_crawl_worker(site)
    return {"ok": True}


@app.post("/api/crawl/stop")
def crawl_stop(user=Depends(current_user)):
    crawl_state["running"] = False
    log("INFO", "Crawl stopped")
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
