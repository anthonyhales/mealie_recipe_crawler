import os
import json
import sqlite3
import secrets
import datetime
import asyncio
from urllib.parse import urljoin, urlparse

import aiohttp
import bcrypt
import requests
from bs4 import BeautifulSoup

from fastapi import (
    FastAPI, Request, Form, Depends, Body,
    HTTPException, status, Query
)
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


# -----------------------------
# Paths & environment
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET env var must be set")

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122 Safari/537.36"
)

# -----------------------------
# App setup
# -----------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static"
)

templates = Jinja2Templates(
    directory=os.path.join(BASE_DIR, "templates")
)


# -----------------------------
# Database helpers
# -----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        start_url TEXT NOT NULL,
        recipe_pattern TEXT,
        ingredients_selector TEXT,
        method_selector TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE NOT NULL,
        site_id INTEGER,
        crawled_at TEXT NOT NULL,
        uploaded INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS crawl_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        url TEXT,
        created_at TEXT NOT NULL
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
        pw_hash = bcrypt.hashpw(
            password.encode(), bcrypt.gensalt()
        ).decode()

        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (ADMIN_USER, pw_hash, "admin", datetime.datetime.utcnow().isoformat())
        )
        conn.commit()
        print("Created admin user")
        print("username:", ADMIN_USER)
        print("password:", password)

    conn.close()


init_db()
ensure_admin()


# -----------------------------
# Auth helpers
# -----------------------------
def current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (user,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=303)
    return row


# -----------------------------
# Logging
# -----------------------------
def log(level, message, url=None, site_id=None):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crawl_logs (site_id, level, message, url, created_at) VALUES (?,?,?,?,?)",
        (site_id, level, message, url, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


# -----------------------------
# Routes
# -----------------------------
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
    u = cur.fetchone()
    conn.close()

    if u and bcrypt.checkpw(password.encode(), u["password_hash"].encode()):
        request.session["user"] = username
        return RedirectResponse("/dashboard", status_code=303)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid credentials"}
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user)):
    crawl = {
        "status": "idle",
        "pages": 0,
        "recipes_found": 0,
    }
    upload = {
        "status": "idle",
        "done": 0,
        "total": 0,
    }

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "crawl": crawl,
            "upload": upload,
        }
    )


@app.get("/crawl-logs", response_class=HTMLResponse)
def crawl_logs_page(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT level, message, url, created_at FROM crawl_logs ORDER BY id DESC LIMIT 200"
    )
    logs = cur.fetchall()[::-1]
    conn.close()

    return templates.TemplateResponse(
        "crawl_logs.html",
        {"request": request, "user": user, "logs": logs}
    )


@app.get("/api/crawl/logs")
def api_logs(after_id: int = 0, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, level, message, url, created_at FROM crawl_logs WHERE id>? ORDER BY id ASC",
        (after_id,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"logs": rows}

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user)):
    settings = {
        "mealie_api_base": "",
        "mealie_api_key": "",
        "mealie_rate_limit": "2.0",
    }

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
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY id ASC"
    )
    users = cur.fetchall()
    conn.close()

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "user": user,
            "users": users,
        }
    )
    
@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(
    request: Request,
    user=Depends(current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=200),
):
    offset = (page - 1) * page_size

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM recipes")
    total = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT id, url, site_id, crawled_at, uploaded
        FROM recipes
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



# -----------------------------
# Pre-scan
# -----------------------------
COMMON_PATTERNS = ["/recipe", "/recipes"]

@app.post("/api/sites/prescan")
async def prescan(payload: dict = Body(...), user=Depends(current_user)):
    start_url = payload.get("start_url", "").strip()
    if not start_url.startswith("http"):
        start_url = "https://" + start_url

    async with aiohttp.ClientSession(headers={"User-Agent": DEFAULT_UA}) as session:
        async with session.get(start_url) as r:
            html = await r.text()

    soup = BeautifulSoup(html, "lxml")
    links = [
        urljoin(start_url, a["href"])
        for a in soup.find_all("a", href=True)
    ]

    pattern = "/recipe"
    for p in COMMON_PATTERNS:
        if any(p in urlparse(l).path for l in links):
            pattern = p
            break

    return {
        "ok": True,
        "recipe_pattern": pattern,
        "ingredients_selector": ".ingredients li",
        "method_selector": ".method li"
    }


# -----------------------------
# Health
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True}
