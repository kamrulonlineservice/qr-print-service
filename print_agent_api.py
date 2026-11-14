"""
Print Agent API for Live QR Print (SQLite version).
Add to app.py:
    from print_agent_api import agent_api
    app.register_blueprint(agent_api)

Agent authentication: Authorization: Bearer <shop_api_token>
"""
import hashlib
import os
import secrets
import sqlite3
from flask import Blueprint, jsonify, request, session, send_from_directory, current_app

agent_api = Blueprint("agent_api", __name__, url_prefix="/api/agent")


def _db():
    conn = sqlite3.connect(os.path.join(current_app.root_path, "database.db"), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


@agent_api.before_app_request
def _ensure_agent_tables():
    conn = _db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shop_agent_tokens (
            shop_id INTEGER PRIMARY KEY,
            token_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            last_seen TEXT
        )
    """)
    conn.commit()
    conn.close()


def _authenticated_shop():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token_hash = hashlib.sha256(header[7:].strip().encode("utf-8")).hexdigest()
    conn = _db()
    row = conn.execute(
        "SELECT shop_id FROM shop_agent_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE shop_agent_tokens SET last_seen = datetime('now','localtime') WHERE shop_id = ?",
            (row["shop_id"],)
        )
        conn.commit()
    conn.close()
    return int(row["shop_id"]) if row else None


@agent_api.route("/token", methods=["POST"])
def create_or_rotate_token():
    shop_id = session.get("shop_id")
    if not shop_id:
        return jsonify(error="Shop login required"), 401
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = _db()
    conn.execute("""
        INSERT INTO shop_agent_tokens (shop_id, token_hash, created_at, last_seen)
        VALUES (?, ?, datetime('now','localtime'), NULL)
        ON CONFLICT(shop_id) DO UPDATE SET
            token_hash=excluded.token_hash,
            created_at=excluded.created_at, last_seen=NULL
    """, (shop_id, token_hash))
    conn.commit()
    conn.close()
    return jsonify(shop_id=shop_id, token=token,
                    warning="Copy this token into the shop's Print Agent config. It is shown only once.")


@agent_api.route("/heartbeat", methods=["POST"])
def heartbeat():
    shop_id = _authenticated_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    return jsonify(ok=True, shop_id=shop_id)


@agent_api.route("/next", methods=["POST"])
def next_job():
    shop_id = _authenticated_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("""
            SELECT id, file_name, copies, print_type, pages_input
            FROM print_jobs
            WHERE shop_id=? AND status='Online Paid'
            ORDER BY id ASC LIMIT 1
        """, (shop_id,)).fetchone()
        if not job:
            conn.commit()
            return jsonify(job=None)
        changed = conn.execute("""
            UPDATE print_jobs SET status='Printing'
            WHERE id=? AND shop_id=? AND status='Online Paid'
        """, (job["id"], shop_id)).rowcount
        if changed != 1:
            conn.rollback()
            return jsonify(job=None)
        conn.commit()
        return jsonify(job={
            "id": job["id"], "file_name": job["file_name"],
            "copies": job["copies"] or 1,
            "print_type": job["print_type"] or "bw",
            "pages_input": job["pages_input"] or "all",
            "download_url": f"/api/agent/file/{job['id']}"
        })
    finally:
        conn.close()


@agent_api.route("/file/<int:job_id>", methods=["GET"])
def download_job_file(job_id):
    shop_id = _authenticated_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    conn = _db()
    row = conn.execute("""
        SELECT file_name FROM print_jobs
        WHERE id=? AND shop_id=? AND status='Printing'
    """, (job_id, shop_id)).fetchone()
    conn.close()
    if not row:
        return jsonify(error="Job not found or not claimed"), 404
    upload_dir = current_app.config.get(
        "UPLOAD_FOLDER", os.path.join(current_app.root_path, "uploads")
    )
    return send_from_directory(upload_dir, row["file_name"], as_attachment=True)


@agent_api.route("/status/<int:job_id>", methods=["POST"])
def update_job_status(job_id):
    shop_id = _authenticated_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in {"printed", "failed"}:
        return jsonify(error="Status must be printed or failed"), 400
    final_status = "Printed" if status == "printed" else "Failed"
    conn = _db()
    changed = conn.execute("""
        UPDATE print_jobs SET status=?
        WHERE id=? AND shop_id=? AND status='Printing'
    """, (final_status, job_id, shop_id)).rowcount
    conn.commit()
    conn.close()
    if not changed:
        return jsonify(error="Job not found or not currently Printing"), 404
    return jsonify(ok=True, job_id=job_id, status=final_status)
