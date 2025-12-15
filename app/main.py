import os
import sqlite3
import secrets
import datetime
from typing import Optional
from urllib.parse import urlparse

import bcrypt
import requests
from bs4 import BeautifulSoup

from fastapi import (
    FastAPI, Request, Form, Depends, Body, HTTPException, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse
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
    conn.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "crawl": {"status": "idle", "recipes_found": total},
            "upload": {"status": "idle", "done": uploaded},
            "total_recipes": total,
            "uploaded_recipes": uploaded,
        },
    )

# -----------------------------
# API – Crawl / Progress
# -----------------------------
@app.get("/api/progress")
def api_progress(user=Depends(current_user)):
    return {
        "crawl": {
            "status": "idle",
            "pages": 0,
            "recipes": 0,
        },
        "upload": {
            "status": "idle",
            "done": 0,
            "total": 0,
        }
    }
    
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    settings = {r["key"]: r["value"] for r in cur.fetchall()}
    conn.close()

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "settings": settings,
        }
    )


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users")
    users = cur.fetchall()
    conn.close()
    return templates.TemplateResponse(
        "users.html", {"request": request, "user": user, "users": users}
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
        }
    )



@app.get("/crawl-logs", response_class=HTMLResponse)
def crawl_logs_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM crawl_logs ORDER BY id DESC LIMIT 200")
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
    cur.execute("SELECT * FROM sites ORDER BY id ASC LIMIT 1")
    site = cur.fetchone()
    conn.close()
    return {"ok": True, "site": dict(site) if site else None}


@app.post("/api/sites/save")
def api_sites_save(payload: dict = Body(...), user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO sites (
            id,
            name,
            start_url,
            recipe_pattern,
            ingredients_selector,
            method_selector,

            max_concurrency,
            request_delay,
            max_pages,
            max_recipes,
            user_agent,

            created_at
        ) VALUES (
            (SELECT id FROM sites ORDER BY id ASC LIMIT 1),
            ?,?,?,?,?,?,?,?,?,?,?
        )
    """, (
        payload.get("name", "Default Site"),
        payload.get("start_url", ""),
        payload.get("recipe_pattern", ""),
        payload.get("ingredients_selector", ""),
        payload.get("method_selector", ""),

        int(payload.get("max_concurrency", 5)),      # polite parallelism
        float(payload.get("request_delay", 0.5)),   # rate limiting
        int(payload.get("max_pages", 0)),            # 0 = unlimited
        int(payload.get("max_recipes", 0)),          # 0 = unlimited
        payload.get("user_agent", "MealieRecipeCrawler/1.0"),

        datetime.datetime.utcnow().isoformat(),
    ))

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
    
@app.get("/api/sites/list")
def api_sites_list(user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, start_url FROM sites ORDER BY id ASC")
    sites = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"ok": True, "sites": sites}
    
@app.post("/api/sites/set-active")
def api_sites_set_active(payload: dict = Body(...), user=Depends(current_user)):
    site_id = payload.get("site_id")
    if not site_id:
        raise HTTPException(status_code=400, detail="site_id required")

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('active_site_id', ?)",
        (str(site_id),)
    )
    conn.commit()
    conn.close()

    log("INFO", f"Active site set to {site_id}")
    return {"ok": True}
    
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
        "site": dict(site) if site else None
    }


# -------------------------------------------------
# API – Crawl (stubbed)
# -------------------------------------------------
@app.post("/api/crawl/start")
def crawl_start(user=Depends(current_user)):
    log("INFO", "Crawl started")
    return {"ok": True}


@app.post("/api/crawl/stop")
def crawl_stop(user=Depends(current_user)):
    log("INFO", "Crawl stopped")
    return {"ok": True}


@app.get("/api/crawl/status")
def crawl_status(user=Depends(current_user)):
    return {"status": "idle"}

# -------------------------------------------------
# API – Upload (stubbed)
# -------------------------------------------------
@app.post("/api/upload/start")
def upload_start(user=Depends(current_user)):
    log("INFO", "Upload started")
    return {"ok": True}


@app.get("/api/upload/status")
def upload_status(user=Depends(current_user)):
    return {"status": "idle"}

# -----------------------------
# API – Meta / Info
# -----------------------------
@app.get("/api/meta")
def api_meta():
    return {
        "name": "Mealie Recipe Crawler",
        "version": os.getenv("APP_VERSION", "dev"),
    }


# -------------------------------------------------
# API – Settings
# -------------------------------------------------
@app.get("/api/settings/load")
def settings_load(user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT key,value FROM settings")
    data = {r["key"]: r["value"] for r in cur.fetchall()}
    conn.close()
    return {"ok": True, "settings": data}


@app.post("/api/settings/save")
def settings_save(payload: dict = Body(...), user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    for k, v in payload.items():
        cur.execute(
            "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, v)
        )
    conn.commit()
    conn.close()
    log("INFO", "Settings saved")
    return {"ok": True}


@app.post("/api/settings/test")
def settings_test(user=Depends(current_user)):
    return {"ok": True, "message": "Connection OK"}
