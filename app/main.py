
import os
import json
import time
import secrets
import asyncio
import sqlite3
import datetime
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
import bcrypt
import requests

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Body, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# ----------------------------
# Config / paths
# ----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "app.db")

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET env var not set. Put it in docker-compose.yml")

DEFAULT_ADMIN_USER = os.getenv("ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.getenv("ADMIN_PASS")  # if unset, we generate once and print to logs

# Optional: GitHub repo slug "owner/repo" used for footer version display
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()

# Default crawler politeness
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_REQUEST_DELAY = 0.8  # seconds between requests per worker (polite)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Hardened controls (site-specific defaults)
DEFAULT_MAX_PAGES = 5000
DEFAULT_MAX_RECIPES = 20000
DEFAULT_RETRIES = 2

# ----------------------------
# FastAPI init
# ----------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ----------------------------
# DB helpers
# ----------------------------
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    # Users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    """)

    # Recipes
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            website TEXT NOT NULL,
            title TEXT,
            crawled_at TEXT NOT NULL,
            uploaded INTEGER NOT NULL DEFAULT 0,
            uploaded_at TEXT,
            site_id INTEGER,
            FOREIGN KEY(site_id) REFERENCES sites(id)
        )
    """)

    # Settings (global settings)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Site profiles
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_url TEXT NOT NULL,
            recipe_pattern TEXT,
            ingredients_selector TEXT,
            method_selector TEXT,
            max_concurrency INTEGER NOT NULL,
            request_delay REAL NOT NULL,
            user_agent TEXT NOT NULL,
            max_pages INTEGER NOT NULL,
            max_recipes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

def get_setting(key: str, default=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return default
    return row["value"]

def set_setting(key: str, value: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def get_settings_dict():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def ensure_admin_user():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    c = cur.fetchone()["c"]
    if c == 0:
        pwd = DEFAULT_ADMIN_PASS or secrets.token_urlsafe(16)
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (DEFAULT_ADMIN_USER, hash_pw(pwd), "admin", datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
        print("\n=== Mealie Recipe Crawler ===")
        print("Created initial admin user")
        print(f"  username: {DEFAULT_ADMIN_USER}")
        if DEFAULT_ADMIN_PASS:
            print("  password: (from ADMIN_PASS env var)")
        else:
            print(f"  password: {pwd}")
            print("TIP: Set ADMIN_PASS env var to control this on first run.\n")
    conn.close()

def ensure_defaults_and_migrate():
    """
    If older versions stored crawl settings in settings table, migrate into a default Site profile.
    """
    # Default global settings
    defaults = {
        "mealie_api_base": "",
        "mealie_api_key": "",
        "mealie_rate_limit": "2.0",
        "active_site_id": "",
        # cached progress snapshots
        "crawl_progress": "",
        "upload_progress": "",
    }
    existing = get_settings_dict()
    for k, v in defaults.items():
        if k not in existing:
            set_setting(k, v)

    conn = db()
    cur = conn.cursor()

    # If no sites exist, create one using previous settings (if available)
    cur.execute("SELECT COUNT(*) AS c FROM sites")
    if int(cur.fetchone()["c"]) == 0:
        s = existing
        start_url = (s.get("crawl_start_url") or "https://www.bbcgoodfood.com/recipes").strip()
        recipe_pattern = (s.get("recipe_pattern") or "/recipe").strip()
        ing = (s.get("ingredients_selector") or "").strip()
        met = (s.get("method_selector") or "").strip()
        max_conc = int(float(s.get("max_concurrency") or DEFAULT_MAX_CONCURRENCY))
        delay = float(s.get("request_delay") or DEFAULT_REQUEST_DELAY)
        ua = (s.get("user_agent") or DEFAULT_USER_AGENT).strip()

        cur.execute(
            """INSERT INTO sites
               (name, start_url, recipe_pattern, ingredients_selector, method_selector,
                max_concurrency, request_delay, user_agent, max_pages, max_recipes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Default Site",
                start_url,
                recipe_pattern,
                ing,
                met,
                max_conc,
                delay,
                ua,
                DEFAULT_MAX_PAGES,
                DEFAULT_MAX_RECIPES,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
        set_setting("active_site_id", str(new_id))

    # Ensure active_site_id set
    active = get_setting("active_site_id", "").strip()
    if not active:
        cur.execute("SELECT id FROM sites ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        if row:
            set_setting("active_site_id", str(row["id"]))

    conn.close()

init_db()
ensure_admin_user()
ensure_defaults_and_migrate()

# ----------------------------
# Auth dependencies
# ----------------------------
def current_user(request: Request):
    username = request.session.get("user")
    if not username:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="Not logged in")
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    user = cur.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, detail="Not logged in")
    return user

def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return user

# ----------------------------
# Task state + persisted progress
# ----------------------------
crawl_task = None
upload_task = None
crawl_cancel = asyncio.Event()
upload_cancel = asyncio.Event()

def set_progress(kind: str, obj: dict):
    set_setting(f"{kind}_progress", json.dumps(obj))

def get_progress(kind: str) -> dict:
    raw = get_setting(f"{kind}_progress", "") or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {}

def reset_progress():
    set_progress("crawl", {"status": "idle", "pages": 0, "recipes_found": 0, "last_url": "", "started_at": "", "ended_at": ""})
    set_progress("upload", {"status": "idle", "total": 0, "done": 0, "last_url": "", "started_at": "", "ended_at": ""})

if not get_setting("crawl_progress"):
    reset_progress()

# ----------------------------
# Utility: recipe verification
# ----------------------------
def is_true_recipe(html: str, ingredients_selector: str = "", method_selector: str = "") -> bool:
    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            if not script.string:
                continue
            data = json.loads(script.string)

            def has_recipe(x):
                if isinstance(x, dict):
                    t = x.get("@type")
                    if isinstance(t, list):
                        return any(str(i).lower() == "recipe" for i in t)
                    return str(t).lower() == "recipe"
                return False

            if isinstance(data, list):
                if any(has_recipe(item) for item in data):
                    return True
            elif isinstance(data, dict):
                if has_recipe(data):
                    return True
                graph = data.get("@graph")
                if isinstance(graph, list) and any(has_recipe(item) for item in graph):
                    return True
        except Exception:
            continue

    if ingredients_selector and method_selector:
        ing = soup.select(ingredients_selector)
        met = soup.select(method_selector)
        if ing and met:
            return True

    return False

def extract_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    if soup.title and soup.title.string:
        return soup.title.string.strip()[:200]
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)[:200]
    return None

# ----------------------------
# Site profiles
# ----------------------------
def list_sites():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sites ORDER BY id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_active_site():
    sid = get_setting("active_site_id", "").strip()
    conn = db()
    cur = conn.cursor()
    if sid:
        cur.execute("SELECT * FROM sites WHERE id=?", (sid,))
        row = cur.fetchone()
        if row:
            conn.close()
            return dict(row)
    # fallback
    cur.execute("SELECT * FROM sites ORDER BY id ASC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def set_active_site(site_id: int):
    set_setting("active_site_id", str(site_id))

# ----------------------------
# Crawler core (hardened)
# ----------------------------
async def fetch_html(session: aiohttp.ClientSession, url: str, delay: float, retries: int = DEFAULT_RETRIES):
    # polite delay (per request)
    await asyncio.sleep(delay)
    last_exc = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, allow_redirects=True) as resp:
                ct = resp.headers.get("Content-Type", "")
                if resp.status == 200 and "text/html" in ct:
                    return await resp.text()
                # 429/5xx backoff
                if resp.status in (429, 500, 502, 503, 504):
                    await asyncio.sleep(min(5.0, (attempt + 1) * 1.5))
                    continue
                return None
        except Exception as e:
            last_exc = e
            await asyncio.sleep(min(5.0, (attempt + 1) * 1.5))
    return None

def normalize_url(u: str) -> str:
    return u.split("#")[0].split("?")[0]

def same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc

async def crawl_run():
    site = get_active_site()
    if not site:
        set_progress("crawl", {"status": "error", "pages": 0, "recipes_found": 0, "last_url": "No site configured", "started_at": "", "ended_at": datetime.datetime.utcnow().isoformat()})
        return

    start_url = site["start_url"].strip()
    recipe_pattern = (site.get("recipe_pattern") or "").strip()
    ingredients_selector = (site.get("ingredients_selector") or "").strip()
    method_selector = (site.get("method_selector") or "").strip()
    max_conc = int(site.get("max_concurrency") or DEFAULT_MAX_CONCURRENCY)
    delay = float(site.get("request_delay") or DEFAULT_REQUEST_DELAY)
    ua = (site.get("user_agent") or DEFAULT_USER_AGENT).strip()
    max_pages = int(site.get("max_pages") or DEFAULT_MAX_PAGES)
    max_recipes = int(site.get("max_recipes") or DEFAULT_MAX_RECIPES)

    crawl_cancel.clear()

    prog = {
        "status": "running",
        "pages": 0,
        "recipes_found": 0,
        "last_url": "",
        "started_at": datetime.datetime.utcnow().isoformat(),
        "ended_at": "",
        "site_id": site["id"],
        "site_name": site["name"],
    }
    set_progress("crawl", prog)

    visited = set()
    queued = set()
    q = asyncio.Queue()
    await q.put(start_url)
    queued.add(start_url)

    # SSL relax (some sites)
    import ssl
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=max_conc, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": ua}

    conn = db()
    cur = conn.cursor()

    def update_recipe_count():
        cur.execute("SELECT COUNT(*) AS c FROM recipes")
        return int(cur.fetchone()["c"])

    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
        sem = asyncio.Semaphore(max_conc)

        async def handle_page(url: str):
            nonlocal prog
            if crawl_cancel.is_set():
                return
            if prog["pages"] >= max_pages:
                crawl_cancel.set()
                return
            if prog["recipes_found"] >= max_recipes:
                crawl_cancel.set()
                return

            async with sem:
                html = await fetch_html(session, url, delay)

            prog["pages"] += 1
            prog["last_url"] = url
            set_progress("crawl", prog)

            if not html:
                return

            soup = BeautifulSoup(html, "lxml")

            for a in soup.find_all("a", href=True):
                link = normalize_url(urljoin(url, a["href"]))
                if not link.startswith("http"):
                    continue
                if not same_host(start_url, link):
                    continue
                if link in visited:
                    continue

                # candidate recipe?
                is_candidate = False
                path = urlparse(link).path
                if recipe_pattern:
                    is_candidate = recipe_pattern in path
                else:
                    pl = path.lower()
                    is_candidate = ("/recipe" in pl) or ("/recipes" in pl)

                if is_candidate:
                    async with sem:
                        rhtml = await fetch_html(session, link, delay)
                    if rhtml and is_true_recipe(rhtml, ingredients_selector, method_selector):
                        title = extract_title(rhtml)
                        website = urlparse(link).netloc
                        try:
                            cur.execute(
                                "INSERT OR IGNORE INTO recipes (url, website, title, crawled_at, uploaded, site_id) VALUES (?,?,?,?,0,?)",
                                (link, website, title, datetime.datetime.utcnow().isoformat(), site["id"]),
                            )
                            conn.commit()
                        except Exception:
                            pass
                        prog["recipes_found"] = update_recipe_count()
                        set_progress("crawl", prog)
                        continue

                if link not in queued and link not in visited:
                    queued.add(link)
                    await q.put(link)

        async def worker():
            while not crawl_cancel.is_set():
                try:
                    url = await asyncio.wait_for(q.get(), timeout=1)
                except asyncio.TimeoutError:
                    break
                if url in visited:
                    q.task_done()
                    continue
                visited.add(url)
                try:
                    await handle_page(url)
                finally:
                    q.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(max_conc)]
        await q.join()
        for w in workers:
            w.cancel()

    conn.close()
    prog["status"] = "stopped" if crawl_cancel.is_set() else "done"
    prog["ended_at"] = datetime.datetime.utcnow().isoformat()
    set_progress("crawl", prog)

# ----------------------------
# Upload core (Mealie)
# ----------------------------
def mealie_import_endpoint(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/api"):
        return b + "/recipes/import"
    if "/api/" in b:
        return b.rstrip("/") + "/recipes/import"
    return b + "/api/recipes/import"

async def upload_run():
    upload_cancel.clear()
    base = (get_setting("mealie_api_base", "") or "").strip()
    key = (get_setting("mealie_api_key", "") or "").strip()
    rate = float(get_setting("mealie_rate_limit", "2.0") or 2.0)

    prog = {"status": "running", "total": 0, "done": 0, "last_url": "", "started_at": datetime.datetime.utcnow().isoformat(), "ended_at": ""}
    set_progress("upload", prog)

    if not base or not key:
        prog["status"] = "error"
        prog["ended_at"] = datetime.datetime.utcnow().isoformat()
        prog["last_url"] = "Missing Mealie API base or key (set in Settings)"
        set_progress("upload", prog)
        return

    endpoint = mealie_import_endpoint(base)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM recipes WHERE uploaded=0")
    total = int(cur.fetchone()["c"])
    prog["total"] = total
    set_progress("upload", prog)

    cur.execute("SELECT id, url FROM recipes WHERE uploaded=0 ORDER BY id ASC")
    rows = cur.fetchall()

    for r in rows:
        if upload_cancel.is_set():
            break

        rid = r["id"]
        url = r["url"]
        prog["last_url"] = url
        set_progress("upload", prog)

        ok = False
        try:
            resp = requests.post(endpoint, headers=headers, json={"url": url}, timeout=30)
            ok = resp.status_code in (200, 201, 202)
        except Exception:
            ok = False

        if ok:
            cur.execute("UPDATE recipes SET uploaded=1, uploaded_at=? WHERE id=?", (datetime.datetime.utcnow().isoformat(), rid))
            conn.commit()

        prog["done"] += 1
        set_progress("upload", prog)

        await asyncio.sleep(rate)

    conn.close()
    prog["status"] = "stopped" if upload_cancel.is_set() else "done"
    prog["ended_at"] = datetime.datetime.utcnow().isoformat()
    set_progress("upload", prog)

# ----------------------------
# Pre-scan (best-effort heuristic)
# ----------------------------
COMMON_PATTERNS = ["/recipes/", "/recipe/", "/recipe-", "/recipes-", "/dish/", "/cook/", "/food/"]

def guess_pattern(urls: list[str]) -> str:
    score = {p: 0 for p in COMMON_PATTERNS}
    for u in urls:
        path = urlparse(u).path.lower()
        for p in COMMON_PATTERNS:
            if p in path:
                score[p] += 1
    best = max(score.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "/recipe"

def guess_selectors(html: str):
    soup = BeautifulSoup(html, "lxml")
    if is_true_recipe(html):
        return "", ""

    def find_list_after(keyword: str):
        for h in soup.find_all(["h1","h2","h3","h4","h5"]):
            t = h.get_text(" ", strip=True).lower()
            if keyword in t:
                nxt = h.find_next(["ul","ol"])
                if nxt:
                    sel = nxt.name
                    if nxt.get("id"):
                        sel += f"#{nxt['id']}"
                    elif nxt.get("class"):
                        sel += "." + ".".join(nxt.get("class")[:2])
                    return sel + " li"
        return ""

    ing_sel = find_list_after("ingredient")
    met_sel = find_list_after("method") or find_list_after("instruction") or find_list_after("direction") or find_list_after("steps")

    if not ing_sel:
        cand = soup.select("section.ingredients ul li, .ingredients li, [class*='ingredient'] li")
        if cand:
            ing_sel = ".ingredients li"
    if not met_sel:
        cand = soup.select("section.method ol li, .method li, [class*='method'] li, [class*='instruction'] li")
        if cand:
            met_sel = ".method li"

    return ing_sel, met_sel

async def prescan_run(start_url: str):
    import ssl
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context, limit=4, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers, connector=connector) as session:
        html = await fetch_html(session, start_url, 0.2, retries=1)
        if not html:
            return {"ok": False, "message": "Could not fetch start URL"}
        soup = BeautifulSoup(html, "lxml")
        links = []
        for a in soup.find_all("a", href=True):
            link = normalize_url(urljoin(start_url, a["href"]))
            if not link.startswith("http"):
                continue
            if not same_host(start_url, link):
                continue
            links.append(link)
        links = list(dict.fromkeys(links))[:200]

        pattern = guess_pattern(links)

        sample = None
        for u in links:
            if pattern in urlparse(u).path.lower():
                sample = u
                break
        if not sample and links:
            sample = links[0]

        ing_sel, met_sel = "", ""
        if sample:
            shtml = await fetch_html(session, sample, 0.2, retries=1)
            if shtml:
                ing_sel, met_sel = guess_selectors(shtml)

        return {"ok": True, "recipe_pattern": pattern, "ingredients_selector": ing_sel, "method_selector": met_sel, "sample_url": sample}

# ----------------------------
# Routes: pages
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse("/dashboard", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=?", (username,))
    u = cur.fetchone()
    conn.close()
    if u and verify_pw(password, u["password_hash"]):
        request.session["user"] = username
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM recipes")
    total = int(cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) AS c FROM recipes WHERE uploaded=1")
    uploaded = int(cur.fetchone()["c"])
    conn.close()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": dict(user),
            "total_recipes": total,
            "uploaded_recipes": uploaded,
            "crawl": get_progress("crawl"),
            "upload": get_progress("upload"),
            "active_site": get_active_site(),
            "github_repo": GITHUB_REPO,
        },
    )

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user=Depends(current_user)):
    settings = get_settings_dict()
    sites = list_sites()
    active_site = get_active_site()
    return templates.TemplateResponse("settings.html", {"request": request, "user": dict(user), "settings": settings, "sites": sites, "active_site": active_site, "github_repo": GITHUB_REPO})

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
    total = int(cur.fetchone()["c"])
    cur.execute("SELECT id, url, website, title, crawled_at, uploaded, uploaded_at FROM recipes ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    total_pages = max(1, (total + page_size - 1) // page_size)
    # Build pagination window
    win = 2
    start = max(1, page - win)
    end = min(total_pages, page + win)

    return templates.TemplateResponse(
        "recipes.html",
        {
            "request": request,
            "user": dict(user),
            "recipes": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "page_range": list(range(start, end + 1)),
            "github_repo": GITHUB_REPO,
        },
    )

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(require_admin)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, username, role, created_at FROM users ORDER BY id ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse("users.html", {"request": request, "user": dict(user), "users": rows, "github_repo": GITHUB_REPO})

# ----------------------------
# API: version (for UI footer)
# ----------------------------
@app.get("/api/meta")
def api_meta():
    return {"github_repo": GITHUB_REPO, "server_time": datetime.datetime.utcnow().isoformat()}

# ----------------------------
# API: global settings
# ----------------------------
@app.post("/api/settings/save")
def api_settings_save(payload: dict = Body(...), user=Depends(current_user)):
    allowed = {"mealie_api_base", "mealie_api_key", "mealie_rate_limit"}
    for k, v in payload.items():
        if k in allowed:
            set_setting(k, str(v).strip())
    return {"ok": True}

@app.post("/api/settings/test")
def api_settings_test(payload: dict = Body(...), user=Depends(current_user)):
    base = (payload.get("mealie_api_base") or get_setting("mealie_api_base", "")).strip()
    key = (payload.get("mealie_api_key") or get_setting("mealie_api_key", "")).strip()
    if not base or not key:
        return JSONResponse({"ok": False, "message": "Missing Mealie API base or key"}, status_code=400)

    headers = {"Authorization": f"Bearer {key}"}
    candidates = [
        base.rstrip("/") + "/api/app/about",
        base.rstrip("/") + "/api/health",
        base.rstrip("/") + "/api/users/self",
    ]
    last_err = ""
    for url in candidates:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code in (200, 204):
                return {"ok": True, "message": f"Success ({url})"}
            last_err = f"{url} -> {r.status_code}"
        except Exception as e:
            last_err = str(e)

    return JSONResponse({"ok": False, "message": f"Failed to validate API. Last error: {last_err}"}, status_code=400)

# ----------------------------
# API: site profiles
# ----------------------------
@app.get("/api/sites")
def api_sites(user=Depends(current_user)):
    return {"ok": True, "sites": list_sites(), "active_site_id": get_setting("active_site_id", "")}

@app.post("/api/sites/set-active")
def api_sites_set_active(payload: dict = Body(...), user=Depends(current_user)):
    sid = int(payload.get("site_id"))
    # Ensure it exists
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM sites WHERE id=?", (sid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return JSONResponse({"ok": False, "message": "Site not found"}, status_code=400)
    set_active_site(sid)
    return {"ok": True}

@app.post("/api/sites/save")
def api_sites_save(payload: dict = Body(...), user=Depends(current_user)):
    # Anyone logged-in can manage sites (change to admin-only if you prefer)
    sid = payload.get("id")
    name = (payload.get("name") or "").strip() or "Unnamed Site"
    start_url = (payload.get("start_url") or "").strip()
    if not start_url:
        return JSONResponse({"ok": False, "message": "Start URL is required"}, status_code=400)

    recipe_pattern = (payload.get("recipe_pattern") or "").strip()
    ingredients_selector = (payload.get("ingredients_selector") or "").strip()
    method_selector = (payload.get("method_selector") or "").strip()

    try:
        max_concurrency = int(payload.get("max_concurrency") or DEFAULT_MAX_CONCURRENCY)
        request_delay = float(payload.get("request_delay") or DEFAULT_REQUEST_DELAY)
        max_pages = int(payload.get("max_pages") or DEFAULT_MAX_PAGES)
        max_recipes = int(payload.get("max_recipes") or DEFAULT_MAX_RECIPES)
    except Exception:
        return JSONResponse({"ok": False, "message": "Invalid numeric site settings"}, status_code=400)

    user_agent = (payload.get("user_agent") or DEFAULT_USER_AGENT).strip()

    conn = db()
    cur = conn.cursor()
    if sid:
        cur.execute(
            """UPDATE sites SET
               name=?, start_url=?, recipe_pattern=?, ingredients_selector=?, method_selector=?,
               max_concurrency=?, request_delay=?, user_agent=?, max_pages=?, max_recipes=?
               WHERE id=?""",
            (
                name, start_url, recipe_pattern, ingredients_selector, method_selector,
                max_concurrency, request_delay, user_agent, max_pages, max_recipes, int(sid),
            ),
        )
    else:
        cur.execute(
            """INSERT INTO sites
               (name, start_url, recipe_pattern, ingredients_selector, method_selector,
                max_concurrency, request_delay, user_agent, max_pages, max_recipes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name, start_url, recipe_pattern, ingredients_selector, method_selector,
                max_concurrency, request_delay, user_agent, max_pages, max_recipes,
                datetime.datetime.utcnow().isoformat(),
            ),
        )
        sid = cur.lastrowid
    conn.commit()
    conn.close()

    # Set active to saved site for convenience
    set_active_site(int(sid))
    return {"ok": True, "id": int(sid)}

@app.post("/api/sites/delete")
def api_sites_delete(payload: dict = Body(...), user=Depends(current_user)):
    sid = int(payload.get("site_id"))
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM sites")
    c = int(cur.fetchone()["c"])
    if c <= 1:
        conn.close()
        return JSONResponse({"ok": False, "message": "You must keep at least one site profile"}, status_code=400)
    cur.execute("DELETE FROM sites WHERE id=?", (sid,))
    conn.commit()
    # pick a new active site if needed
    active = get_setting("active_site_id", "")
    if active and int(active) == sid:
        cur.execute("SELECT id FROM sites ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        if row:
            set_active_site(int(row["id"]))
    conn.close()
    return {"ok": True}

@app.post("/api/sites/prescan")
async def api_sites_prescan(payload: dict = Body(...), user=Depends(current_user)):
    start_url = (payload.get("start_url") or "").strip()
    if not start_url:
        return JSONResponse({"ok": False, "message": "Missing start URL"}, status_code=400)
    res = await prescan_run(start_url)
    if not res.get("ok"):
        return JSONResponse({"ok": False, "message": res.get("message", "Scan failed")}, status_code=400)
    return res

# ----------------------------
# API: crawl/upload control
# ----------------------------
@app.post("/api/crawl/start")
async def api_crawl_start(user=Depends(current_user)):
    global crawl_task
    if crawl_task and not crawl_task.done():
        return {"ok": False, "message": "Crawl already running"}
    crawl_task = asyncio.create_task(crawl_run())
    return {"ok": True}

@app.post("/api/crawl/stop")
def api_crawl_stop(user=Depends(current_user)):
    crawl_cancel.set()
    return {"ok": True}

@app.post("/api/upload/start")
async def api_upload_start(user=Depends(current_user)):
    global upload_task
    if upload_task and not upload_task.done():
        return {"ok": False, "message": "Upload already running"}
    upload_task = asyncio.create_task(upload_run())
    return {"ok": True}

@app.post("/api/upload/stop")
def api_upload_stop(user=Depends(current_user)):
    upload_cancel.set()
    return {"ok": True}

@app.get("/api/progress")
def api_progress(user=Depends(current_user)):
    crawl = get_progress("crawl")
    upload = get_progress("upload")
    recent = get_recent_recipes(15)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM recipes")
    total = int(cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) AS c FROM recipes WHERE uploaded=1")
    uploaded = int(cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) AS c FROM recipes WHERE uploaded=0")
    pending = int(cur.fetchone()["c"])
    conn.close()
    return {"crawl": crawl, "upload": upload, "counts": {"total": total, "uploaded": uploaded, "pending": pending}, "recent": recent, "active_site": get_active_site()}

def get_recent_recipes(limit=20):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, url, website, title, crawled_at, uploaded FROM recipes ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

# ----------------------------
# API: users (admin)
# ----------------------------
@app.post("/api/users/add")
def api_users_add(payload: dict = Body(...), admin=Depends(require_admin)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    role = (payload.get("role") or "user").strip()
    if not username or not password:
        return JSONResponse({"ok": False, "message": "Username and password required"}, status_code=400)
    if role not in ("admin", "user"):
        role = "user"
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (username, hash_pw(password), role, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return JSONResponse({"ok": False, "message": "User already exists"}, status_code=400)
    conn.close()
    return {"ok": True}

@app.post("/api/users/delete")
def api_users_delete(payload: dict = Body(...), admin=Depends(require_admin)):
    uid = payload.get("id")
    if not uid:
        return JSONResponse({"ok": False, "message": "Missing id"}, status_code=400)
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE id=? AND username<>?", (uid, DEFAULT_ADMIN_USER))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.post("/api/users/resetpw")
def api_users_resetpw(payload: dict = Body(...), admin=Depends(require_admin)):
    uid = payload.get("id")
    password = payload.get("password") or ""
    if not uid or not password:
        return JSONResponse({"ok": False, "message": "Missing id or password"}, status_code=400)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_pw(password), uid))
    conn.commit()
    conn.close()
    return {"ok": True}

# ----------------------------
# Downloads
# ----------------------------
@app.get("/api/recipes/download/{fmt}")
def api_download(fmt: str, user=Depends(current_user)):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT url FROM recipes ORDER BY id ASC")
    urls = [r["url"] for r in cur.fetchall()]
    conn.close()

    if fmt == "txt":
        content = "\n".join(urls)
        return StreamingResponse(
            iter([content]),
            media_type="text/plain",
            headers={"Content-Disposition": "attachment; filename=recipes.txt"},
        )

    if fmt == "csv":
        import csv
        import io
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["url"])
        for u in urls:
            w.writerow([u])
        data = sio.getvalue()
        return StreamingResponse(
            iter([data]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=recipes.csv"},
        )

    if fmt in ("xlsx", "excel"):
        from openpyxl import Workbook
        import io
        wb = Workbook()
        ws = wb.active
        ws.title = "recipes"
        ws.append(["url"])
        for u in urls:
            ws.append([u])
        bio = io.BytesIO()
        wb.save(bio)
        bio.seek(0)
        return StreamingResponse(
            bio,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=recipes.xlsx"},
        )

    return JSONResponse({"ok": False, "message": "Unknown format"}, status_code=400)
