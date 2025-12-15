
# Mealie Recipe Crawler

A self-hosted web app that crawls recipe websites and imports the URLs into Mealie.

## Features

- SB Admin 2 dashboard UI
- Login + users (admin/user roles)
- SQLite storage (handles 10k+ recipes)
- Crawl first, upload later (separate controls)
- Multi-site profiles (save multiple crawl targets, switch active site)
- Best-effort pre-scan to suggest recipe URL pattern + selectors
- Export recipe URL list as TXT/CSV/Excel
- GitHub release version shown in footer (if `GITHUB_REPO` is set)

## Deployment (Docker / Portainer)

1. Set a strong secret:
   - `SESSION_SECRET` (required)

2. Deploy:
```bash
docker compose up -d --build
```

App runs on `http://YOUR_HOST:8222`

### Environment variables

- `SESSION_SECRET` (required): long random string for session cookies
- `ADMIN_USER` (optional, default `admin`): initial admin username on first run
- `ADMIN_PASS` (optional): initial admin password on first run (otherwise generated and printed in logs)
- `GITHUB_REPO` (optional): `owner/repo` to show version tag in footer (reads GitHub Releases)

## Creating GitHub releases (for version display)

1. Tag:
```bash
git tag v1.0.0
git push origin v1.0.0
```

2. Create a Release in GitHub using that tag.
The UI will display the latest release tag via the GitHub API.

## Notes

- Always respect a site's terms of service and robots.txt.
- Keep request delay conservative.
