"""The panel page and its assets.

The page comes from files that ship inside the package, so the panel works on a
host without outbound network: no CDN, no remote font, no inline script. Only
the files named in the table are exposed, which keeps the directory itself from
becoming a listing.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

ADMIN_PATH = "/admin"
ADMIN_ASSET_PREFIX = "/admin/assets"
ASSETS_DIR = Path(__file__).parent / "admin_assets"

#: Only these files are served, each with its declared type.
_ASSET_TYPES: dict[str, str] = {
    "admin.css": "text/css",
    "admin.js": "application/javascript",
}

#: The page loads nothing but its own files, and no one may frame it.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def install_admin_ui(app: FastAPI) -> None:
    """Serve the panel shell and its static files."""

    @app.get(ADMIN_PATH, include_in_schema=False)
    async def admin_page() -> HTMLResponse:
        page = (ASSETS_DIR / "admin.html").read_text(encoding="utf-8")
        return HTMLResponse(
            page,
            headers={
                "Content-Security-Policy": CONTENT_SECURITY_POLICY,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(f"{ADMIN_ASSET_PREFIX}/{{filename}}", include_in_schema=False)
    async def admin_asset(filename: str) -> FileResponse:
        media_type = _ASSET_TYPES.get(filename)
        target = ASSETS_DIR / filename
        if media_type is None or not target.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(target, media_type=media_type)
