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


def save_recipe(url: str, site_id: int) -> bool:
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT OR IGNORE INTO recipes (url, site_id, crawled_at, uploaded)
            VALUES (?, ?, ?, 0)
            """,
            (url, site_id, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
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
# Recipe Detection (FIX)
# -------------------------------------------------
def is_recipe_page(site, url: str, soup: BeautifulSoup) -> bool:
    # Explicit pattern match if provided
    pat = (site["recipe_pattern"] or "").strip()
    if pat and pat in urlparse(url).path:
        return True

    # Schema.org Recipe fallback
    for tag in soup.select("script[type='application/ld+json']"):
        try:
            txt = tag.string or ""
            if '"@type"' in txt and "Recipe" in txt:
                return True
        except Exception:
            pass

    return False

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

    try:
        while queue:
            if not crawl_state["running"]:
                break
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

                if is_recipe_page(site, url, soup):
                    if save_recipe(url, site["id"]):
                        crawl_state["recipes"] += 1

                for a in soup.select("a[href]"):
                    full = urljoin(site["start_url"], a["href"])
                    if full.startswith(site["start_url"]):
                        queue.append(full)

                time.sleep(delay)

            except Exception as e:
                log("ERROR", str(e), url=url, site_id=site["id"])

    finally:
        crawl_state["running"] = False
        log("INFO", "Crawl finished", site_id=site["id"])

# -------------------------------------------------
# API – Crawl
# -------------------------------------------------
@app.post("/api/crawl/start")
def crawl_start(user=Depends(current_user)):
    site = get_active_site()
    if not site:
        raise HTTPException(status_code=400, detail="No active site selected")
    log("INFO", f"Crawl started for '{site['name']}'", site_id=site["id"])
    start_crawl_worker(site)
    return {"ok": True}
