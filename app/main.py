"""FastAPI app: JSON API plus the single-page UI."""
from __future__ import annotations

import hmac
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import db, service
from .config import Settings
from .crypto import Vault, load_key
from .extractor import Extractor, build_extractor

STATIC = Path(__file__).parent / "static"
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ManualTxn(BaseModel):
    merchant: str
    amount: float | str
    date: str
    category_id: int | None = None
    kind: str | None = None
    payment_method: str | None = None
    notes: str | None = None


class TxnPatch(BaseModel):
    merchant: str | None = None
    amount: float | str | None = None
    date: str | None = None
    currency: str | None = None
    category_id: int | None = None
    kind: str | None = None
    payment_method: str | None = None
    tax: float | str | None = None
    gstin: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    make_rule: bool = False


class CategoryIn(BaseModel):
    name: str
    parent_id: int | None = None


class RuleIn(BaseModel):
    merchant: str
    category_id: int | None = None
    kind: str | None = None


def create_app(settings: Settings | None = None, extractor: Extractor | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    vault = Vault(settings.files_dir, load_key(settings.data_dir, settings.fernet_key))
    extractor = extractor or build_extractor(settings)

    app = FastAPI(title="Receipt tracker", docs_url=None, redoc_url=None, openapi_url=None)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_token(request: Request) -> None:
        if not settings.token:
            return
        supplied = request.headers.get("x-token", "")
        if not hmac.compare_digest(supplied, settings.token):
            raise HTTPException(status_code=401, detail="Missing or wrong access token")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; frame-src blob:; object-src blob:; frame-ancestors 'none'"
            )
        return response

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    api = Depends(require_token)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config", dependencies=[api])
    def config():
        return {
            "extractor": extractor.name,
            "model": settings.model if extractor.name != "none" else None,
            "confidence_threshold": settings.confidence_threshold,
            "auth_required": bool(settings.token),
        }

    # ---- receipts

    @app.post("/api/receipts", dependencies=[api])
    async def upload(files: list[UploadFile] = File(...), conn: sqlite3.Connection = Depends(get_conn)):
        results = []
        for f in files:
            data = await f.read(settings.max_upload_bytes + 1)
            if len(data) > settings.max_upload_bytes:
                results.append({"filename": f.filename, "error": "File is larger than 15 MB"})
                continue
            try:
                txns = service.ingest_file(conn, settings, vault, extractor, data, f.filename or "upload")
                results.append({"filename": f.filename, "transactions": txns})
            except ValueError as exc:
                results.append({"filename": f.filename, "error": str(exc)})
        return {"results": results}

    @app.post("/api/transactions", dependencies=[api], status_code=201)
    def add_manual(body: ManualTxn, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            return service.create_manual(conn, body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/transactions", dependencies=[api])
    def list_transactions(month: str | None = None, status: str | None = None, q: str | None = None,
                          category_id: int | None = None, kind: str | None = None,
                          conn: sqlite3.Connection = Depends(get_conn)):
        if month and not MONTH_RE.match(month):
            raise HTTPException(422, "month must be YYYY-MM")
        return service.list_txns(conn, month, status, q, category_id, kind)

    @app.get("/api/transactions/{txn_id}", dependencies=[api])
    def read_transaction(txn_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        txn = service.get_txn(conn, txn_id)
        if txn is None:
            raise HTTPException(404, "Not found")
        return txn

    @app.patch("/api/transactions/{txn_id}", dependencies=[api])
    def edit_transaction(txn_id: str, patch: TxnPatch, conn: sqlite3.Connection = Depends(get_conn)):
        data = patch.model_dump(exclude_unset=True)
        make_rule = data.pop("make_rule", False)
        try:
            return service.update_txn(conn, txn_id, data, make_rule)
        except KeyError:
            raise HTTPException(404, "Not found")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/transactions/{txn_id}/approve", dependencies=[api])
    def approve_transaction(txn_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            return service.approve(conn, txn_id)
        except KeyError:
            raise HTTPException(404, "Not found")

    @app.delete("/api/transactions/{txn_id}", dependencies=[api], status_code=204)
    def delete_transaction(txn_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        if not service.delete_txn(conn, vault, txn_id):
            raise HTTPException(404, "Not found")
        return Response(status_code=204)

    @app.get("/api/transactions/{txn_id}/file", dependencies=[api])
    def original_file(txn_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        row = conn.execute("SELECT receipt_file, receipt_type, receipt_name FROM transactions WHERE id = ?",
                           (txn_id,)).fetchone()
        if row is None or not row["receipt_file"]:
            raise HTTPException(404, "No original file")
        try:
            data = vault.load(row["receipt_file"])
        except Exception:
            raise HTTPException(500, "Could not decrypt the stored file (wrong key?)")
        media = row["receipt_type"] or "application/octet-stream"
        if media not in ("image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf", "text/plain"):
            media = "application/octet-stream"
        return Response(data, media_type=media,
                        headers={"Content-Disposition": "inline", "Content-Security-Policy": "sandbox"})

    # ---- summaries and exports

    @app.get("/api/summary", dependencies=[api])
    def month_summary(month: str = Query(...), conn: sqlite3.Connection = Depends(get_conn)):
        if not MONTH_RE.match(month):
            raise HTTPException(422, "month must be YYYY-MM")
        return service.summary(conn, month)

    @app.get("/api/export.csv", dependencies=[api])
    def export_csv(month: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
        if month and not MONTH_RE.match(month):
            raise HTTPException(422, "month must be YYYY-MM")
        name = f"transactions-{month or 'all'}.csv"
        return PlainTextResponse(service.export_csv(conn, month), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/export.json", dependencies=[api])
    def export_json(conn: sqlite3.Connection = Depends(get_conn)):
        return JSONResponse(service.export_all(conn),
                            headers={"Content-Disposition": 'attachment; filename="my-data.json"'})

    @app.post("/api/data/wipe", dependencies=[api])
    def wipe(confirm: str = Query(...), conn: sqlite3.Connection = Depends(get_conn)):
        if confirm != "DELETE":
            raise HTTPException(422, "Pass confirm=DELETE to delete all transactions and files")
        return {"deleted_transactions": service.wipe_all(conn, vault)}

    # ---- categories and rules

    @app.get("/api/categories", dependencies=[api])
    def categories(conn: sqlite3.Connection = Depends(get_conn)):
        paths = db.category_paths(conn)
        rows = conn.execute("SELECT id, name, parent_id FROM categories").fetchall()
        return sorted(({**dict(r), "path": paths[r["id"]]} for r in rows), key=lambda c: c["path"])

    @app.post("/api/categories", dependencies=[api], status_code=201)
    def add_category(body: CategoryIn, conn: sqlite3.Connection = Depends(get_conn)):
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "name is required")
        if body.parent_id is not None:
            parent = conn.execute("SELECT parent_id FROM categories WHERE id = ?", (body.parent_id,)).fetchone()
            if parent is None:
                raise HTTPException(422, "unknown parent")
            if parent["parent_id"] is not None:
                raise HTTPException(422, "categories can be nested one level deep")
        try:
            cur = conn.execute("INSERT INTO categories(name, parent_id) VALUES (?, ?)", (name, body.parent_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That category already exists")
        conn.commit()
        return {"id": cur.lastrowid, "name": name, "parent_id": body.parent_id}

    @app.patch("/api/categories/{cid}", dependencies=[api])
    def rename_category(cid: int, body: CategoryIn, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            cur = conn.execute("UPDATE categories SET name = ? WHERE id = ?", (body.name.strip(), cid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That category already exists")
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        return {"id": cid, "name": body.name.strip()}

    @app.delete("/api/categories/{cid}", dependencies=[api], status_code=204)
    def delete_category(cid: int, conn: sqlite3.Connection = Depends(get_conn)):
        cur = conn.execute("DELETE FROM categories WHERE id = ?", (cid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        return Response(status_code=204)

    @app.get("/api/rules", dependencies=[api])
    def rules(conn: sqlite3.Connection = Depends(get_conn)):
        paths = db.category_paths(conn)
        return [{**dict(r), "category": paths.get(r["category_id"])}
                for r in conn.execute("SELECT * FROM merchant_rules ORDER BY pattern")]

    @app.post("/api/rules", dependencies=[api], status_code=201)
    def add_rule(body: RuleIn, conn: sqlite3.Connection = Depends(get_conn)):
        from .classify import merchant_key
        key = merchant_key(body.merchant)
        if not key:
            raise HTTPException(422, "merchant is required")
        if body.kind is not None and body.kind not in service.KINDS:
            raise HTTPException(422, "kind must be business or personal")
        if body.category_id is None and body.kind is None:
            raise HTTPException(422, "a rule needs a category or a kind")
        if body.category_id is not None and body.category_id not in db.category_paths(conn):
            raise HTTPException(422, "unknown category")
        conn.execute(
            """INSERT INTO merchant_rules (pattern, category_id, kind, created_at) VALUES (?,?,?,?)
               ON CONFLICT(pattern) DO UPDATE SET category_id = excluded.category_id, kind = excluded.kind""",
            (key, body.category_id, body.kind, service.now()),
        )
        conn.commit()
        return {"pattern": key}

    @app.delete("/api/rules/{rid}", dependencies=[api], status_code=204)
    def delete_rule(rid: int, conn: sqlite3.Connection = Depends(get_conn)):
        cur = conn.execute("DELETE FROM merchant_rules WHERE id = ?", (rid,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        return Response(status_code=204)

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn app.main:app_factory --factory`."""
    return create_app()
