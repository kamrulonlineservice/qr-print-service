from flask import Flask, jsonify, render_template, request, redirect, url_for, session, send_from_directory, send_file
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy
from db import init_db, connect_db, Row
import pymupdf as fitz
import base64
from PIL import Image
import os
import qrcode
import io
import hashlib
import secrets
from io import BytesIO

# PDF page conversion
from pdf2image import convert_from_path

app = Flask(__name__)
# Use a stable secret key from the environment in production.
# Set FLASK_SECRET_KEY to a long, random value before deploying.
app.config['SECRET_KEY'] = os.environ.get(
    'FLASK_SECRET_KEY',
    'local-development-only-change-this-secret-key-before-deployment-9f4c2a7b'
)
app.secret_key = app.config['SECRET_KEY']
# Neon PostgreSQL in production; local SQLite remains available for development.
_database_url = os.environ.get('DATABASE_URL', '').strip()
if _database_url.startswith('postgres://'):
    _database_url = _database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif _database_url.startswith('postgresql://'):
    _database_url = _database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _database_url or 'sqlite:///qr_print.db'
db = SQLAlchemy(app)

# চ্যাট মেসেজ টেবিল (SQLAlchemy)
class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.String(50), nullable=False)
    sender = db.Column(db.String(20), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()
    try:
        init_db()
    except Exception as e:
        print("db.py init_db error (can be ignored if handled):", e)


# ===================== INTEGRATED PRINT AGENT API =====================
# This API is built directly into app.py; no separate print_agent_api.py needed.
def _agent_db():
    conn = connect_db(os.path.join(app.root_path, "database.db"), timeout=15)
    conn.row_factory = Row
    return conn


def _ensure_agent_tables():
    conn = _agent_db()
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


def _authenticated_agent_shop():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token_hash = hashlib.sha256(header[7:].strip().encode("utf-8")).hexdigest()
    conn = _agent_db()
    row = conn.execute(
        "SELECT shop_id FROM shop_agent_tokens WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE shop_agent_tokens SET last_seen=datetime('now','localtime') WHERE shop_id=?",
            (row["shop_id"],)
        )
        conn.commit()
    conn.close()
    return int(row["shop_id"]) if row else None


_ensure_agent_tables()


def _ensure_payment_settings_columns():
    """Add per-shop payment controls without disturbing existing shop data."""
    conn = _agent_db()
    existing = {r[1] for r in conn.execute("PRAGMA table_info(shops)").fetchall()}
    additions = {
        "online_payment_enabled": "INTEGER NOT NULL DEFAULT 1",
        "cash_payment_enabled": "INTEGER NOT NULL DEFAULT 1",
        "payment_gateway": "TEXT NOT NULL DEFAULT 'upi'",
        "razorpay_key_id": "TEXT",
        "razorpay_key_secret": "TEXT",
        "phonepe_merchant_id": "TEXT",
        "phonepe_salt_key": "TEXT",
        "payment_merchant_name": "TEXT",
    }
    for column, definition in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE shops ADD COLUMN {column} {definition}")
    conn.commit()
    conn.close()


_ensure_payment_settings_columns()


@app.route("/api/agent/token", methods=["POST"])
def create_agent_token():
    shop_id = session.get("shop_id")
    if not shop_id:
        return jsonify(error="Shop login required"), 401
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = _agent_db()
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


@app.route("/api/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    shop_id = _authenticated_agent_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    return jsonify(ok=True, shop_id=shop_id)


@app.route("/api/agent/next", methods=["POST"])
def agent_next_job():
    shop_id = _authenticated_agent_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    conn = _agent_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("""
            SELECT id, file_name, copies, print_type, pages_input
            FROM print_jobs
            WHERE shop_id=? AND status IN ('Online Paid','Cash Received')
            ORDER BY id ASC LIMIT 1
        """, (shop_id,)).fetchone()
        if not job:
            conn.commit()
            return jsonify(job=None)
        changed = conn.execute("""
            UPDATE print_jobs SET status='Printing'
            WHERE id=? AND shop_id=? AND status IN ('Online Paid','Cash Received')
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


@app.route("/api/agent/file/<int:job_id>", methods=["GET"])
def agent_download_job_file(job_id):
    shop_id = _authenticated_agent_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    conn = _agent_db()
    row = conn.execute("""
        SELECT file_name FROM print_jobs
        WHERE id=? AND shop_id=? AND status='Printing'
    """, (job_id, shop_id)).fetchone()
    conn.close()
    if not row:
        return jsonify(error="Job not found or not claimed"), 404
    return send_from_directory(app.config['UPLOAD_FOLDER'], row["file_name"], as_attachment=True)


@app.route("/api/agent/status/<int:job_id>", methods=["POST"])
def agent_update_job_status(job_id):
    shop_id = _authenticated_agent_shop()
    if not shop_id:
        return jsonify(error="Unauthorized"), 401
    data = request.get_json(silent=True) or {}
    status = str(data.get("status", "")).strip().lower()
    if status not in {"printed", "failed"}:
        return jsonify(error="Status must be printed or failed"), 400
    final_status = "Printed" if status == "printed" else "Failed"
    conn = _agent_db()
    changed = conn.execute("""
        UPDATE print_jobs SET status=?
        WHERE id=? AND shop_id=? AND status='Printing'
    """, (final_status, job_id, shop_id)).rowcount
    conn.commit()
    conn.close()
    if not changed:
        return jsonify(error="Job not found or not currently Printing"), 404
    return jsonify(ok=True, job_id=job_id, status=final_status)
# =================== END INTEGRATED PRINT AGENT API ===================

# আপলোড ফোল্ডার পাথ কনফিগারেশন
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
QR_FOLDER = 'static/qrs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

# Upload folder-er sob file 24 ghonta purono hole auto delete hobe.
# Database-er print order/history delete hobe na.
import threading
import time

_FILE_CLEANUP_STARTED = False
_FILE_CLEANUP_LOCK = threading.Lock()

def cleanup_old_uploaded_files():
    """Delete physical uploaded/generated files older than 24 hours."""
    cutoff = time.time() - (24 * 60 * 60)
    upload_dir = os.path.abspath(app.config['UPLOAD_FOLDER'])
    try:
        for entry in os.scandir(upload_dir):
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.remove(entry.path)
                    print(f"Auto-deleted file older than 24 hours: {entry.name}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                print(f"Could not delete old upload {entry.name}: {exc}")
    except OSError as exc:
        print(f"Upload cleanup error: {exc}")

def start_upload_cleanup_worker():
    global _FILE_CLEANUP_STARTED
    with _FILE_CLEANUP_LOCK:
        if _FILE_CLEANUP_STARTED:
            return
        _FILE_CLEANUP_STARTED = True
    def worker():
        while True:
            cleanup_old_uploaded_files()
            time.sleep(5 * 60)  # প্রতি ৫ মিনিটে পরীক্ষা
    threading.Thread(target=worker, name='upload-file-cleanup', daemon=True).start()

# App start হলে cleanup চালু হবে; এরপর প্রতি ৫ মিনিটে পুরোনো ফাইল পরীক্ষা করবে।
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    start_upload_cleanup_worker()

# আপলোড ফাইল দেখার রাউট
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# PDF-এর নির্দিষ্ট পেজকে PNG ছবিতে রূপান্তর করার Route
@app.route('/pdf_page_image/<int:job_id>/<int:page_no>')
def pdf_page_image(job_id, page_no):

    try:
        # Flask-SQLAlchemy-এর একই ডেটাবেস থেকে Job খোঁজা
        conn = connect_db(
          os.path.join(app.root_path, 'database.db')
        )
        cursor = conn.cursor()

        cursor.execute(
          "SELECT file_name FROM print_jobs WHERE id = ?",
           (job_id,)
        )

        job = cursor.fetchone()
        conn.close()

        if not job:
            return "Job not found", 404

        filename = job[0]

        pdf_path = os.path.join(
            app.root_path,
            app.config['UPLOAD_FOLDER'],
            filename
        )

        if not os.path.exists(pdf_path):
            return "PDF file not found: " + pdf_path, 404

        doc = fitz.open(pdf_path)

        if page_no < 1 or page_no > len(doc):
            doc.close()
            return "PDF page not found", 404

        page = doc.load_page(page_no - 1)

        pix = page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            alpha=False
        )

        image_bytes = pix.tobytes("png")
        doc.close()

        return send_file(
            BytesIO(image_bytes),
            mimetype='image/png'
        )

    except Exception as e:
        print("PDF PAGE IMAGE ERROR:", str(e))
        return "PDF Error: " + str(e), 500

@app.route('/')
def index():
    return redirect(url_for('shop_login'))



   # ============================================================= ADMIN ROUTES ===========================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if username == "kamruli077@gmail.com" and password == "kamrul123":
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            return "à¦­à§à¦² à¦‡à¦‰à¦œà¦¾à¦°à¦¨à§‡à¦® à¦¬à¦¾ à¦ªà¦¾à¦¸à¦“à¦¯à¦¼à¦¾à¦°à§à¦¡!"
            
    return render_template('admin_login.html')




@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    conn = connect_db("database.db")
    cursor = conn.cursor()
    
    # শপের ডেটা কুয়েরি করা
    cursor.execute("""
        SELECT id, shop_name, email, plan_name, subscription_end_date, recharge_amount, status, utr_number, plan_name 
        FROM shops
    """)
    rows = cursor.fetchall()
    
    shops = []
    for row in rows:
        shop_id = row[0]
        
        # ── [নতুন ফিচার: প্রতিটি দোকানের আজকের মোট ইনকাম (Today's Revenue) হিসাব করা] ──
        # ডেটাবেজে যদি আজকের তারিখের প্রিন্ট জব বা পেমেন্ট রেকর্ড থাকে তা যোগ করা
        today_date = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("""
            SELECT SUM(amount) FROM print_jobs 
            WHERE shop_id = ? AND DATE(timestamp) = ?
        """, (shop_id, today_date))
        rev_row = cursor.fetchone()
        todays_revenue = rev_row[0] if rev_row and rev_row[0] else 0.0
        # --------------------------------------------------------------------------

        shops.append({
            'id': shop_id,
            'shop_name': row[1],
            'email': row[2],
            'plan_name': row[3],
            'subscription_end_date': row[4],
            'recharge_amount': row[5],
            'status': row[6],
            'pending_utr': row[7],     # UTR নম্বর
            'pending_plan': row[8],    # প্ল্যানের নাম
            'todays_revenue': todays_revenue  # আজকের রেভিনিউ যুক্ত করা হলো
        })
        
    conn.close()
    return render_template("admin_dashboard.html", shops=shops)

@app.route('/admin/shop/toggle/<int:shop_id>')
def toggle_shop_status(shop_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT status FROM shops WHERE id = ?", (shop_id,))
    result = cursor.fetchone()
    
    if result:
        current_status = result[0]
        # à¦¯à¦¦à¦¿ à¦¡à¦¾à¦Ÿà¦¾à¦¬à§‡à¦œà§‡ à¦¸à§à¦Ÿà§à¦¯à¦¾à¦Ÿà¦¾à¦¸ à¦¨à¦¾ à¦¥à¦¾à¦•à§‡, à¦¡à¦¿à¦«à¦²à§à¦Ÿà¦­à¦¾à¦¬à§‡ 'active' à¦§à¦°à§‡ à¦¨à§‡à¦¬
        if not current_status:
            current_status = 'active'
            
        new_status = 'inactive' if current_status == 'active' else 'active'
        
        cursor.execute("UPDATE shops SET status = ? WHERE id = ?", (new_status, shop_id))
        conn.commit()
        
    conn.close()
    return redirect(url_for('admin_dashboard'))




@app.route('/admin/add_days/<int:shop_id>')
def admin_add_days(shop_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    # ১. প্রথমে চেক করা যে এই শপের কোনো পেন্ডিং রিচার্জ বা UTR রিকোয়েস্ট আছে কি না
    cursor.execute('''
        SELECT plan_name, amount 
        FROM recharge_history 
        WHERE shop_id = ? AND status = 'Pending' 
        ORDER BY id DESC LIMIT 1
    ''', (shop_id,))
    recharge_info = cursor.fetchone()
    
    # যদি কোনো পেন্ডিং রিচার্জ রিকোয়েস্ট না থাকে, তবে অ্যাডমিনকে অ্যালার্ট দিয়ে আটকে দেওয়া হবে
    if not recharge_info:
        conn.close()
        return "<script>alert('এই শপের কোনো পেন্ডিং রিচার্জ বা UTR রিকোয়েস্ট নেই! রিচার্জ রিকোয়েস্ট ছাড়া মেয়াদ বাড়ানো যাবে না।'); window.location.href='/admin/dashboard';</script>"
        
    plan_name = recharge_info[0]
    amount = recharge_info[1]
    
    # ২. প্ল্যান অনুযায়ী দিন নির্ধারণ করা (১ মাসের জন্য ৩০ দিন, ৩ মাসের জন্য ৯০ দিন)
    days_to_add = 90 if '3 Month' in str(plan_name) else 30
    
    # ৩. শপের বর্তমান সাবস্ক্রিপশন শেষ তারিখ কত আছে তা চেক করা
    cursor.execute('SELECT subscription_end_date FROM shops WHERE id = ?', (shop_id,))
    row = cursor.fetchone()
    current_end_str = row[0] if row else None
    
    # ৪. বর্তমান মেয়াদের সাথে সঠিক দিনগুলো যোগ করার লজিক (Cumulative Addition)
    if current_end_str:
        try:
            current_end = datetime.strptime(current_end_str.split()[0], '%Y-%m-%d')
            if current_end > datetime.now():
                new_end = current_end + timedelta(days=days_to_add)
            else:
                new_end = datetime.now() + timedelta(days=days_to_add)
        except:
            new_end = datetime.now() + timedelta(days=days_to_add)
    else:
        new_end = datetime.now() + timedelta(days=days_to_add)
        
    # ৫. shops টেবিলে সঠিক মেয়াদ, প্ল্যানের নাম ও স্ট্যাটাস আপডেট করা
    cursor.execute('''
        UPDATE shops 
        SET subscription_end_date = ?, 
            status = 'Active',
            plan_name = ?,
            recharge_amount = ?,
            utr_number = NULL
        WHERE id = ?
    ''', (new_end.strftime('%Y-%m-%d'), plan_name, amount, shop_id))
    
    # ৬. recharge_history এর স্ট্যাটাস 'Pending' থেকে 'Approved' করে দেওয়া
    cursor.execute('''
        UPDATE recharge_history 
        SET status = 'Approved' 
        WHERE shop_id = ? AND status = 'Pending'
    ''', (shop_id,))
    
    conn.commit()
    conn.close()
    
    return "<script>alert('রিচার্জ সফলভাবে অনুমোদিত হয়েছে এবং মেয়াদ বাড়িয়ে দেওয়া হয়েছে!'); window.location.href='/admin/dashboard';</script>"


@app.route('/admin/shop/detail/<int:shop_id>')
def admin_shop_detail(shop_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    # শপের তথ্য আনা
    cursor.execute("""
        SELECT id, shop_name, owner_name, phone, email, country, state, district, village, pincode,
               registration_date, trial_end_date, subscription_end_date,
               status, recharge_amount
        FROM shops WHERE id = ?
    """, (shop_id,))
    shop = cursor.fetchone()
    
    # ওই দোকানের সব রিচার্জ হিস্টরি ডেটাবেজ থেকে নিয়ে আসা
    cursor.execute("""
        SELECT plan_name, amount, utr_number, status, timestamp 
        FROM recharge_history WHERE shop_id = ? ORDER BY id DESC
    """, (shop_id,))
    recharge_histories = cursor.fetchall()
    
    conn.close()
    
    if not shop:
        return "Shop not found!", 404
        
    # সঠিক ইনডেক্স সিকোয়েন্স অনুযায়ী ডিকশনারি ম্যাপিং
    shop_details = {
        'id': shop[0],
        'shop_name': shop[1],
        'owner_name': shop[2],
        'phone': shop[3],
        'email': shop[4],
        'country': shop[5],
        'state': shop[6],
        'district': shop[7],
        'village': shop[8],
        'pincode': shop[9],
        'registration_date': shop[10],
        'trial_end_date': shop[11],
        'subscription_end_date': shop[12],
        'status': shop[13],
        'recharge_amount': shop[14]
    }
    
    # recharge_histories ভেরিয়েবলটি রেন্ডার টেমপ্লেটে পাস করা হলো
    return render_template('shop_detail.html', shop=shop_details, recharge_histories=recharge_histories)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route("/admin/approve_shop/<int:shop_id>")
def approve_shop(shop_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
        
    conn = connect_db("database.db")
    cursor = conn.cursor()
    
    # ১. recharge_history থেকে সর্বশেষ পেন্ডিং প্ল্যানের তথ্য চেক করা
    cursor.execute("""
        SELECT plan_name, amount, utr_number 
        FROM recharge_history 
        WHERE shop_id = ? AND status = 'Pending' 
        ORDER BY id DESC LIMIT 1
    """, (shop_id,))
    recharge_info = cursor.fetchone()
    
    # যদি কোনো পেন্ডিং UTR রিকোয়েস্ট বা রিচার্জ না থাকে, তবে অ্যাডমিনকে অ্যালার্ট দিয়ে ফিরিয়ে দেওয়া যেতে পারে
    # (যদি আপনি রিকোয়েস্ট বাধ্যতামূলক করতে চান)
    # if not recharge_info:
    #     conn.close()
    #     return "<script>alert('এই শপের কোনো পেন্ডিং রিচার্জ বা UTR রিকোয়েস্ট নেই!'); window.location.href='/admin/dashboard';</script>"

    plan_name = recharge_info[0] if recharge_info else "1 Month Plan"
    amount = recharge_info[1] if recharge_info else 129.0
    
    # ২. শপের বর্তমান সাবস্ক্রিপশন শেষ তারিখ বের করা (যাতে আগের মেয়াদের সাথে দিন যোগ হয়)
    cursor.execute("SELECT subscription_end_date FROM shops WHERE id = ?", (shop_id,))
    shop_row = cursor.fetchone()
    current_end = shop_row[0] if shop_row else None
    
    # ৩. বর্তমান মেয়াদের সাথে ৩০ বা ৯০ দিন যোগ করার লজিক
    days_to_add = 90 if '3 Month' in str(plan_name) else 30
    
    if current_end:
        try:
            end_date_obj = datetime.strptime(current_end.split()[0], '%Y-%m-%d')
            if end_date_obj > datetime.now():
                new_end_date = (end_date_obj + timedelta(days=days_to_add)).strftime('%Y-%m-%d')
            else:
                new_end_date = (datetime.now() + timedelta(days=days_to_add)).strftime('%Y-%m-%d')
        except:
            new_end_date = (datetime.now() + timedelta(days=days_to_add)).strftime('%Y-%m-%d')
    else:
        new_end_date = (datetime.now() + timedelta(days=days_to_add)).strftime('%Y-%m-%d')

    # ৪. shops টেবিলে স্ট্যাটাস Active করা এবং নতুন বর্ধিত ডেট সেভ করা
    cursor.execute("""
        UPDATE shops 
        SET status = 'Active', 
            subscription_end_date = ?,
            plan_name = ?,
            recharge_amount = ?,
            utr_number = NULL
        WHERE id = ?
    """, (new_end_date, plan_name, amount, shop_id))
    
    # ৫. recharge_history স্ট্যাটাস Approved করা (যদি পেন্ডিং থাকে)
    cursor.execute("""
        UPDATE recharge_history 
        SET status = 'Approved' 
        WHERE shop_id = ? AND status = 'Pending'
    """, (shop_id,))
    
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))
@app.route('/admin/chats')
def admin_chat_inbox():
    if not session.get('admin_logged_in'):  # এখানে 'is_admin'-এর বদলে 'admin_logged_in' দিতে হবে
        return redirect(url_for('shop_login'))
    
    subquery = db.session.query(ChatMessage.shop_id, db.func.max(ChatMessage.timestamp).label('max_time')).group_by(ChatMessage.shop_id).subquery()
    latest_chats = db.session.query(ChatMessage).join(subquery, db.and_(ChatMessage.shop_id == subquery.c.shop_id, ChatMessage.timestamp == subquery.c.max_time)).order_by(ChatMessage.timestamp.desc()).all()

    return render_template('admin_chat_inbox.html', chats=latest_chats)


@app.route('/admin/chat/<shop_id>', methods=['GET', 'POST'])
def admin_chat_detail(shop_id):
    if not session.get('admin_logged_in'):  # এখানেও পরিবর্তন করুন
        return redirect(url_for('shop_login'))
    
    if request.method == 'POST':
        reply_msg = request.form.get('message')
        if reply_msg:
            new_msg = ChatMessage(shop_id=shop_id, sender='admin', message=reply_msg, is_read=True)
            db.session.add(new_msg)
            db.session.commit()
            return redirect(url_for('admin_chat_detail', shop_id=shop_id))

    ChatMessage.query.filter_by(shop_id=shop_id, sender='shopkeeper').update({ChatMessage.is_read: True})
    db.session.commit()

    messages = ChatMessage.query.filter_by(shop_id=shop_id).order_by(ChatMessage.timestamp.asc()).all()
    return render_template('admin_chat_detail.html', messages=messages, shop_id=shop_id)


@app.route('/admin/api/check-notifications')
def check_notifications():
    if not session.get('admin_logged_in'):  # এখানেও পরিবর্তন করুন
        return jsonify({'unread_count': 0})
    
    unread_count = ChatMessage.query.filter_by(sender='shopkeeper', is_read=False).count()
    return jsonify({'unread_count': unread_count})


   # ============================================================= SHOPKEEPER ROUTES ===========================================================================
       
# 👉 আপনার রেজিস্টার রাউটটি এখানে যুক্ত করে দেওয়া হলো (যাতে নতুন অ্যাড্রেস ফিল্ডগুলো কাজ করে)
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        shop_name = request.form["shop_name"]
        owner_name = request.form["owner_name"]
        phone = request.form["phone"]
        email = request.form["email"]
        password = request.form["password"]
        
        # নতুন অ্যাড্রেস ফিল্ডসমূহ রিসিভ করা
        country = request.form.get("country", "India")
        state = request.form.get("state")
        district = request.form.get("district")
        
        selected_village = request.form.get("village")
        if selected_village == "Other":
            village = request.form.get("other_village", "")
        else:
            village = selected_village
            
        pincode = request.form.get("pincode")

        conn = connect_db("database.db")
        cursor = conn.cursor()

        # ১. একই ইমেইল বা ফোন নম্বর দিয়ে আগে অ্যাকাউন্ট আছে কি না চেক করা
        cursor.execute(
            "SELECT id FROM shops WHERE email = ? OR phone = ?", (email, phone)
        )
        existing_shop = cursor.fetchone()

        if existing_shop:
            conn.close()
            return render_template(
                "register.html",
                error="এই ইমেইল বা ফোন নম্বর দিয়ে ইতিমধ্যে একটি অ্যাকাউন্ট রেজিস্টার করা রয়েছে!"
            )

        # ২. ৩ দিনের ফ্রি ট্রায়ালের মেয়াদ হিসাব করা
        reg_date = datetime.now()
        trial_end = reg_date + timedelta(days=3)

        # ৩. ডাটাবেজে নতুন শপ সেভ করা
        # ৩. ডাটাবেজে নতুন শপ সেভ করা
        cursor.execute(
            """
            INSERT INTO shops (shop_name, owner_name, phone, email, password, country, state, district, village, pincode, registration_date, trial_end_date, subscription_end_date, status) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                shop_name,
                owner_name,
                phone,
                email,
                password,
                country,
                state,
                district,
                village,
                pincode,
                reg_date,
                trial_end,
                trial_end,
                "Active",
            ),
        )

        conn.commit()
        conn.close()
        return redirect(url_for("shop_login"))

    # এটি শুধুমাত্র GET রিকোয়েস্টের জন্য কাজ করবে (ফাংশনের আন্ডারে সঠিকভাবে ইন্ডেন্ট করা)
    return render_template("register.html")



@app.route('/login', methods=['GET', 'POST'])
def shop_login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        conn = connect_db('database.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM shops WHERE email = ? AND password = ?", (email, password))
        shop = cursor.fetchone()
        conn.close()
        if shop:
            session.clear()
            session['shop_id'] = int(shop[0])
            return redirect(url_for('shop_dashboard', shop_id=shop[0]))
        else:
            return render_template('login.html', error="Invalid Email or Password")
    return render_template('login.html')

from datetime import datetime


@app.route("/dashboard/<int:shop_id>")
def shop_dashboard(shop_id):
    # A shop owner may access only the dashboard belonging to their login.
    if not session.get('shop_id'):
        return redirect(url_for('shop_login'))
    if int(session['shop_id']) != shop_id:
        return 'আপনার এই দোকানের Dashboard খোলার অনুমতি নেই।', 403

    conn = connect_db("database.db")
    conn.row_factory = Row  # কলামের নাম দিয়ে ডেটা অ্যাক্সেস করার জন্য এটি জরুরি
    cursor = conn.cursor()
    
    # দোকানদারের তথ্য ফেচ করা
    cursor.execute("SELECT * FROM shops WHERE id = ?", (shop_id,))
    shop_row = cursor.fetchone()
    
    if not shop_row:
        conn.close()
        return "Shop not found!", 404

    from datetime import datetime
    sub_end_date_str = shop_row['subscription_end_date'] if 'subscription_end_date' in shop_row.keys() else None
    
    is_active = False
    days_left = 0
    hours_left = 0
    minutes_left = 0
    
    if sub_end_date_str:
        try:
            end_date = datetime.strptime(str(sub_end_date_str).split()[0], '%Y-%m-%d')
            now = datetime.now()
            if end_date >= now.replace(hour=0, minute=0, second=0, microsecond=0):
                remaining = end_date - now
                total_seconds = int(remaining.total_seconds())
                if total_seconds > 0:
                    days_left = total_seconds // 86400
                    hours_left = (total_seconds % 86400) // 3600
                    minutes_left = (total_seconds % 3600) // 60
                is_active = True
        except Exception as e:
            print("Date Error:", e)
            is_active = True

    shop = {
        'id': shop_row['id'],
        'shop_name': shop_row['shop_name'],
        'owner_name': shop_row['owner_name'] if 'owner_name' in shop_row.keys() else "",
        'phone': shop_row['phone'] if 'phone' in shop_row.keys() else "",
        'email': shop_row['email'] if 'email' in shop_row.keys() else "",  # ইমেইল যোগ করা হলো
        # নতুন অ্যাড্রেস ফিল্ডসমূহ
        'country': shop_row['country'] if 'country' in shop_row.keys() else "India",
        'state': shop_row['state'] if 'state' in shop_row.keys() else "",
        'district': shop_row['district'] if 'district' in shop_row.keys() else "",
        'village': shop_row['village'] if 'village' in shop_row.keys() else "",
        'pincode': shop_row['pincode'] if 'pincode' in shop_row.keys() else "",
        
        'upi_id': shop_row['upi_id'] if 'upi_id' in shop_row.keys() else "",
        'bw_rate': shop_row['bw_rate'] if 'bw_rate' in shop_row.keys() else 5.0,
        'color_rate': shop_row['color_rate'] if 'color_rate' in shop_row.keys() else 10.0,
        'bw_pages': shop_row['bw_pages'] if 'bw_pages' in shop_row.keys() and shop_row['bw_pages'] else 0,        
        'color_pages': shop_row['color_pages'] if 'color_pages' in shop_row.keys() and shop_row['color_pages'] else 0,    
        'subscription_end_date': sub_end_date_str, 
        'is_active': is_active,
        'days_left': days_left,
        'hours_left': hours_left,
        'minutes_left': minutes_left,
    }

    # রিচার্জ হিস্টরি ফেচ করা
    cursor.execute("SELECT plan_name, amount, utr_number, status, timestamp FROM recharge_history WHERE shop_id = ? ORDER BY id DESC", (shop_id,))
    recharge_histories = cursor.fetchall()
    
    # প্রিন্ট জবস ফেচ করা (ডিক্স ফরম্যাটে)
   # প্রিন্ট জবস ফেচ করা
    cursor.execute("""SELECT id, customer_phone, file_name, copies, print_type, amount, status, timestamp, pages_input
        FROM print_jobs WHERE shop_id = ? AND amount > 0
        AND status IN ('Pending Cash','Pending Verification','Payment Rejected','Online Paid','Cash Received','Printing','Printed','Failed')
        ORDER BY id DESC""", (shop_id,))
    jobs = cursor.fetchall()
    
    today = datetime.now().strftime('%Y-%m-%d')
    # Revenue and print counters are strictly today's completed print jobs.
    cursor.execute("""SELECT COALESCE(SUM(amount),0), COUNT(*)
        FROM print_jobs WHERE shop_id=? AND amount>0 AND status='Printed'
        AND date(timestamp)=?""", (shop_id, today))
    metric_row = cursor.fetchone()
    todays_revenue = metric_row[0] or 0
    printed_today = metric_row[1] or 0

    # Page count: expand the selected page expression, then multiply by copies.
    def _selected_page_count(value):
        value = str(value or '1').strip().lower()
        if value in ('all', '', '0'):
            return None
        count = 0
        try:
            for part in value.split(','):
                part = part.strip()
                if '-' in part:
                    a, b = [int(x.strip()) for x in part.split('-', 1)]
                    if b < a or b-a > 10000: return 0
                    count += b-a+1
                else:
                    int(part); count += 1
            return count
        except Exception:
            return 0

    cursor.execute("""SELECT print_type, copies, pages_input, file_name
        FROM print_jobs WHERE shop_id=? AND status='Printed' AND date(timestamp)=?""", (shop_id, today))
    bw_total_pages = 0
    color_total_pages = 0
    for row in cursor.fetchall():
        selected = _selected_page_count(row['pages_input'])
        if selected is None:
            # For 'all', use PDF page count when available; images count as one page.
            try:
                fpath = os.path.join(app.config['UPLOAD_FOLDER'], row['file_name'])
                selected = len(fitz.open(fpath)) if os.path.isfile(fpath) and fpath.lower().endswith('.pdf') else 1
            except Exception:
                selected = 1
        pages = max(1, int(row['copies'] or 1)) * max(0, selected)
        if str(row['print_type'] or '').lower() == 'color': color_total_pages += pages
        else: bw_total_pages += pages

    # Print agent is online only when a valid authenticated heartbeat arrived recently.
    agent_conn = _agent_db()
    agent_row = agent_conn.execute(
        "SELECT last_seen FROM shop_agent_tokens WHERE shop_id=?", (shop_id,)
    ).fetchone()
    agent_conn.close()
    agent_online = False
    if agent_row and agent_row['last_seen']:
        try:
            seen = datetime.strptime(agent_row['last_seen'], '%Y-%m-%d %H:%M:%S')
            agent_online = (datetime.now() - seen).total_seconds() <= 90
        except Exception:
            agent_online = False

    shop['bw_pages'] = bw_total_pages
    shop['color_pages'] = color_total_pages
    shop['agent_online'] = agent_online
    conn.close()

    return render_template("dashboard.html", 
        shop=shop, 
        jobs=jobs, 
        shop_id=shop_id, 
        recharge_histories=recharge_histories,
        total_orders=printed_today,
        todays_revenue=todays_revenue)
 

@app.route('/shop/logout')
def shop_logout():
    session.pop('shop_id', None)
    return redirect(url_for('shop_login'))


@app.route('/shop/payment/verify/<int:job_id>', methods=['POST'])
def verify_customer_payment(job_id):
    """Shop owner confirms only after checking that the money arrived."""
    shop_id = session.get('shop_id')
    if not shop_id:
        return redirect(url_for('shop_login'))

    conn = connect_db('database.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE print_jobs SET status = 'Online Paid', pay_mode = 'online' "
        "WHERE id = ? AND shop_id = ? AND status = 'Pending Verification'",
        (job_id, shop_id)
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if not changed:
        return 'এই পেমেন্টটি পাওয়া যায়নি, অথবা এটি আর Pending Verification অবস্থায় নেই।', 404
    return redirect(url_for('shop_dashboard', shop_id=shop_id))


@app.route('/shop/payment/cash-received/<int:job_id>', methods=['POST'])
def confirm_cash_received(job_id):
    """Approve a cash job only after the shop owner physically receives cash."""
    shop_id = session.get('shop_id')
    if not shop_id:
        return redirect(url_for('shop_login'))

    conn = connect_db('database.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE print_jobs SET status = 'Cash Received', pay_mode = 'counter' "
        "WHERE id = ? AND shop_id = ? AND status = 'Pending Cash'",
        (job_id, shop_id)
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if not changed:
        return 'এই Cash অর্ডাৰটো পোৱা নগ’ল, অথবা আগতেই approve কৰা হৈছে।', 404
    return redirect(url_for('shop_dashboard', shop_id=shop_id))


@app.route('/shop/payment/reject/<int:job_id>', methods=['POST'])
def reject_customer_payment(job_id):
    """Reject a payment claim only for the logged-in shop's own job."""
    shop_id = session.get('shop_id')
    if not shop_id:
        return redirect(url_for('shop_login'))

    conn = connect_db('database.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE print_jobs SET status = 'Payment Rejected' "
        "WHERE id = ? AND shop_id = ? AND status = 'Pending Verification'",
        (job_id, shop_id)
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if not changed:
        return 'এই পেমেন্টটি পাওয়া যায়নি, অথবা এটি আর Pending Verification অবস্থায় নেই।', 404
    return redirect(url_for('shop_dashboard', shop_id=shop_id))


@app.route('/generate_qr')
def generate_qr():
    upi_id = "7907205717@ikwik"  # à¦à¦–à¦¾à¦¨à§‡ à¦†à¦ªà¦¨à¦¾à¦° à¦†à¦¸à¦² à¦‡à¦‰à¦ªà¦¿à¦†à¦‡ à¦†à¦‡à¦¡à¦¿ à¦¬à¦¸à¦¾à¦¬à§‡à¦¨
    amount = request.args.get('amount', '50')
    
    # à¦¦à§‹à¦•à¦¾à¦¨à§‡à¦° à¦¨à¦¾à¦® à¦à¦–à¦¾à¦¨à§‡ à¦¸à¦°à¦¾à¦¸à¦°à¦¿ à¦†à¦ªà¦¨à¦¾à¦° à¦•à¦¾à¦™à§à¦•à§à¦·à¦¿à¦¤ à¦¨à¦¾à¦®à§‡ à¦¸à§‡à¦Ÿ à¦•à¦°à§‡ à¦¦à¦¿à¦¤à§‡ à¦ªà¦¾à¦°à§‡à¦¨
    shop_name = "Kamrul Online Service"  # à¦…à¦¥à¦¬à¦¾ {{ shop.shop_name }} à¦¥à§‡à¦•à§‡ à¦¡à¦¾à¦‡à¦¨à¦¾à¦®à¦¿à¦• à¦ªà¦¾à¦ à¦¾à¦¤à§‡ à¦ªà¦¾à¦°à§‡à¦¨
    
    upi_url = f"upi://pay?pa={upi_id}&pn={shop_name}&am={amount}&cu=INR"
    
    img = qrcode.make(upi_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    
    return send_file(buf, mimetype='image/png')



@app.route('/submit_payment', methods=['POST'])
def submit_payment():
    shop_id = request.form.get('shop_id')
    plan_name = request.form.get('plan_name')
    amount = request.form.get('amount')
    utr_number = request.form.get('utr_number')
    
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    # ১. শপের স্ট্যাটাস ও ইউটিআর আপডেট করা
    cursor.execute(
        "UPDATE shops SET status = 'Pending Verification', utr_number = ?, plan_name = ? WHERE id = ?",
        (utr_number, plan_name, shop_id)
    )
    
    # ২. হিস্টরি টেবিলে নতুন পেমেন্ট রেকর্ড এন্ট্রি করা
    cursor.execute(
        "INSERT INTO recharge_history (shop_id, plan_name, amount, utr_number, status, timestamp) VALUES (?, ?, ?, ?, 'Pending', datetime('now', 'localtime'))",
        (shop_id, plan_name, amount, utr_number)
    )
    
    conn.commit()
    conn.close()
    
    return f"<script>alert('Payment submitted successfully! Admin will verify soon.'); window.location.href='/dashboard/{shop_id}';</script>"
@app.route('/shop/pricing/<int:shop_id>', methods=['GET', 'POST'])
def shop_pricing(shop_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    if request.method == 'POST':
        bw_rate = request.form.get('bw_rate')
        color_rate = request.form.get('color_rate')
        cursor.execute("UPDATE shops SET bw_rate = ?, color_rate = ? WHERE id = ?", (bw_rate, color_rate, shop_id))
        conn.commit()
        conn.close()
        return redirect(url_for('shop_pricing', shop_id=shop_id))
    
    cursor.execute("SELECT shop_name, bw_rate, color_rate FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    conn.close()
    
    shop = {'id': shop_id, 'shop_name': shop_info[0], 'bw_rate': shop_info[1], 'color_rate': shop_info[2]}
    return render_template('pricing.html', shop=shop, shop_id=shop_id)


@app.route('/shop/qrcode/<int:shop_id>')
def shop_qrcode(shop_id):
    conn = connect_db('database.db')
    conn.row_factory = Row  # কলামের নাম দিয়ে ডেটা অ্যাক্সেস করার জন্য এটি জরুরি
    cursor = conn.cursor()
    
    # নতুন অ্যাড্রেস কলামগুলোসহ কুয়েরি ফেচ করা
    cursor.execute("SELECT shop_name, country, state, district, village, pincode FROM shops WHERE id = ?", (shop_id,))
    shop_data_row = cursor.fetchone()
    conn.close()

    if not shop_data_row:
        return "Shop not found!", 404

    # ডিকশনারিতে ডেটাগুলো সঠিকভাবে ম্যাপিং করে দেওয়া হলো
    shop_data = {
        'id': shop_id,
        'shop_name': shop_data_row['shop_name'] if shop_data_row['shop_name'] else "",
        'country': shop_data_row['country'] if shop_data_row['country'] else "",
        'state': shop_data_row['state'] if shop_data_row['state'] else "",
        'district': shop_data_row['district'] if shop_data_row['district'] else "",
        'village': shop_data_row['village'] if shop_data_row['village'] else "",
        'pincode': shop_data_row['pincode'] if shop_data_row['pincode'] else "",
    }
    
    return render_template('shop_qr.html', shop=shop_data)



@app.route('/shop/qr_poster/<int:shop_id>')
def shop_qr_poster(shop_id):
    conn = connect_db('database.db')
    conn.row_factory = Row
    cursor = conn.cursor()
    
    # পুরনো address এর বদলে নতুন কলামগুলো দিয়ে কুয়েরি করুন
    cursor.execute("SELECT shop_name, country, state, district, village, pincode FROM shops WHERE id = ?", (shop_id,))
    shop_row = cursor.fetchone()
    conn.close()
    
    if not shop_row:
        return "Shop not found", 404
        
    shop = {
        'shop_name': shop_row['shop_name'],
        'country': shop_row['country'],
        'state': shop_row['state'],
        'district': shop_row['district'],
        'village': shop_row['village'],
        'pincode': shop_row['pincode']
    }
    
    return render_template('shop_qr.html', shop=shop, shop_id=shop_id)


@app.route('/shop/settings/<int:shop_id>', methods=['GET', 'POST'])
def shop_settings(shop_id):
    conn = connect_db('database.db')
    conn.row_factory = Row
    cursor = conn.cursor()
    
    if request.method == 'POST':
        shop_name = request.form.get('shop_name')
        upi_id = request.form.get('upi_id')
        country = request.form.get('country')
        state = request.form.get('state')
        district = request.form.get('district')
        village = request.form.get('village')
        pincode = request.form.get('pincode')
        
        # ডাটাবেজে নতুন ফিল্ডগুলো আপডেট করা
        cursor.execute("""
            UPDATE shops 
            SET shop_name = ?, upi_id = ?, country = ?, state = ?, district = ?, village = ?, pincode = ? 
            WHERE id = ?
        """, (shop_name, upi_id, country, state, district, village, pincode, shop_id))
        
        conn.commit()
        conn.close()
        return redirect(url_for('shop_settings', shop_id=shop_id))
    
    # নতুন কলামগুলো সিলেক্ট করা
    cursor.execute("SELECT shop_name, owner_name, email, country, state, district, village, pincode, upi_id FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    conn.close()
    
    if not shop_info:
        return "Shop not found", 404
        
    shop = {
        'id': shop_id, 
        'shop_name': shop_info['shop_name'], 
        'owner_name': shop_info['owner_name'], 
        'email': shop_info['email'], 
        'country': shop_info['country'],
        'state': shop_info['state'],
        'district': shop_info['district'],
        'village': shop_info['village'],
        'pincode': shop_info['pincode'],
        'upi_id': shop_info['upi_id']
    }
    
    return render_template('settings.html', shop=shop, shop_id=shop_id)


@app.route('/shop/payment-setup/<int:shop_id>', methods=['GET', 'POST'])
def payment_setup(shop_id):
    if not session.get('shop_id') or int(session['shop_id']) != shop_id:
        return 'Shop login required', 403
    conn = _agent_db()
    conn.row_factory = Row
    if request.method == 'POST':
        online_enabled = 1 if request.form.get('online_payment_enabled') == 'on' else 0
        cash_enabled = 1 if request.form.get('cash_payment_enabled') == 'on' else 0
        gateway = request.form.get('payment_gateway', 'upi').strip().lower()
        if gateway not in ('upi', 'razorpay', 'phonepe'): gateway = 'upi'
        upi_id = request.form.get('upi_id', '').strip()
        conn.execute("""UPDATE shops SET online_payment_enabled=?, cash_payment_enabled=?,
            upi_id=?, payment_gateway=?, payment_merchant_name=?, razorpay_key_id=?, razorpay_key_secret=?,
            phonepe_merchant_id=?, phonepe_salt_key=? WHERE id=?""",
            (online_enabled, cash_enabled, upi_id, gateway,
             request.form.get('payment_merchant_name','').strip(),
             request.form.get('razorpay_key_id','').strip(),
             request.form.get('razorpay_key_secret','').strip(),
             request.form.get('phonepe_merchant_id','').strip(),
             request.form.get('phonepe_salt_key','').strip(), shop_id))
        conn.commit()
        conn.close()
        return redirect(url_for('payment_setup', shop_id=shop_id, saved='1'))
    row = conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    conn.close()
    if not row: return 'Shop not found', 404
    shop = dict(row)
    return render_template('payment_setup.html', shop=shop, shop_id=shop_id,
                           saved=request.args.get('saved') == '1')

# Contact Us à¦ªà§‡à¦œ à¦°à¦¾à¦‰à¦Ÿ


@app.route('/shop/contact/<int:shop_id>')
def shop_contact(shop_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT shop_name, owner_name, email, country, state, district, village, pincode FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    conn.close()
    
    # à¦†à¦ªà¦¨à¦¾à¦° à¦®à§‹à¦¬à¦¾à¦‡à¦² à¦¨à¦®à§à¦¬à¦° à¦à¦–à¦¾à¦¨à§‡ à¦¦à¦¿à§Ÿà§‡ à¦¦à¦¿à¦¨ (à¦¯à§‡à¦®à¦¨: 91XXXXXXXXXX)
    mobile_number = "7907205717" # à¦†à¦ªà¦¨à¦¾à¦° à¦†à¦¸à¦² à¦¨à¦®à§à¦¬à¦°à¦Ÿà¦¿ à¦à¦–à¦¾à¦¨à§‡ à¦¬à¦¸à¦¿à§Ÿà§‡ à¦¨à§‡à¦¬à§‡à¦¨
    
    shop = {
        'id': shop_id,
        'shop_name': shop_info[0] if shop_info else "Shop",
        'email': shop_info[2] if shop_info else "Kamruli077t@Gmail.com.com",
        'mobile': mobile_number
    }
    return render_template('contact.html', shop=shop, shop_id=shop_id)


@app.route('/shop/chat/<shop_id>', methods=['GET', 'POST'])
def shop_chat(shop_id):
    if request.method == 'POST':
        message_text = request.form.get('message')
        if message_text:
            # নতুন SQLAlchemy মডেল ব্যবহার করে মেসেজ সেভ করা হচ্ছে
            new_msg = ChatMessage(shop_id=str(shop_id), sender='shopkeeper', message=message_text, is_read=False)
            db.session.add(new_msg)
            db.session.commit()
            return redirect(url_for('shop_chat', shop_id=shop_id))

    # ওই দোকানদারের সমস্ত চ্যাট মেসেজ রিট্রাইভ করা
    messages = ChatMessage.query.filter_by(shop_id=str(shop_id)).order_by(ChatMessage.timestamp.asc()).all()
    
    # দোকানের নাম বা তথ্য বের করার জন্য যদি আপনার কুয়েরি থাকে তা রাখতে পারেন, অথবা সিম্পল ডিকশনারি দিতে পারেন
    shop_data = {'id': shop_id, 'shop_name': 'Shop'}
    
    return render_template('chat.html', messages=messages, shop=shop_data, shop_id=shop_id) 


# ========================================================== CUSTOMER UPLOAD & PRINT FLOW ============================================================
           

@app.route('/shop/<int:shop_id>', methods=['GET', 'POST'])
def customer_upload(shop_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    # ── [নতুন ফিচার: স্ট্যাটাস এবং সাবস্ক্রিপশন ডেট উভয়ই চেক করার লজিক] ──
    cursor.execute("SELECT status, subscription_end_date FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    
    is_expired = False
    if shop_info:
        shop_status = shop_info[0]
        sub_end_date_str = shop_info[1]
        
        # সাবস্ক্রিপশন ডেট পার হয়ে গেছে কিনা চেক করা
        if sub_end_date_str:
            try:
                sub_end_date = datetime.strptime(sub_end_date_str.split()[0], '%Y-%m-%d')
                if sub_end_date < datetime.now():
                    is_expired = True
            except:
                pass
                
        # যদি স্ট্যাটাস inactive হয় অথবা সাবস্ক্রিপশনের মেয়াদ শেষ হয়ে যায় (expired)
        if (shop_status and shop_status.lower() == 'inactive') or is_expired:
            conn.close()
            return "<h2 style='text-align:center; color:red; margin-top:50px;'>This shop's subscription is currently inactive or expired. Please contact the administrator Kamrul Online Service Ph-7907205717.</h2>"
    else:
        conn.close()
        return "<h2 style='text-align:center; color:red; margin-top:50px;'>Shop not found!</h2>"
    # -------------------------------------------------------------

    if request.method == 'POST':
        file = request.files.get('file')
        customer_phone = request.form.get('customer_phone', '')
        if file and file.filename != '':
            original_filename = file.filename
            
            # ফাইলের নামের স্পেস বা অন্য কোনো সমস্যা এড়াতে সেটিকে পরিচ্ছন্ন করা বা সিকিউর নাম দেওয়া
            import uuid
            ext = os.path.splitext(original_filename)[1]
            filename = f"{uuid.uuid4().hex}_{original_filename.replace(' ', '_')}"
            
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            
            # পিডিএফ হলে এখানে আপনার ইমেজ কনভার্ট বা পেজ রেন্ডার করার কোড থাকবে...
            
            cursor.execute("INSERT INTO print_jobs (shop_id, customer_phone, file_name, copies, print_type, amount, status) VALUES (?, ?, ?, 1, 'bw', 0, 'Draft')", 
                           (shop_id, customer_phone, filename))
            conn.commit()
            job_id = cursor.lastrowid
            conn.close()
            return redirect(url_for('edit_job', job_id=job_id))
            
    cursor.execute("SELECT shop_name, country, state, district, village, pincode, bw_rate, color_rate FROM shops WHERE id = ?", (shop_id,))
    shop = cursor.fetchone()
    conn.close()
    
    shop_data = {
        'id': shop_id,
        'shop_name': shop[0] if shop else "Shop",
        'country': shop[1] if shop else "",
        'state': shop[2] if shop else "",
        'district': shop[3] if shop else "",
        'village': shop[4] if shop else "",
        'pincode': shop[5] if shop else "",
        'bw_rate': shop[6] if shop else 5.0,
        'color_rate': shop[7] if shop else 10.0
    }
    return render_template('index.html', shop=shop_data)
@app.route('/edit/<int:job_id>', methods=['GET', 'POST'])
def edit_job(job_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    if request.method == 'POST':
        # ইউজারের দেওয়া পেজ ইনপুট (যেমন: 1,3 বা 1-5) সংগ্রহ করা
        pages_input = request.form.get('pages_input', '1')
        
        # ডেটাবেজে পেজ ইনপুট সেভ করে রাখা যাতে ব্যাকে আসলেও মুছে না যায়
        cursor.execute("UPDATE print_jobs SET pages_input = ? WHERE id = ?", (pages_input, job_id))
        conn.commit()
        conn.close()
        return redirect(url_for('preview_job', job_id=job_id))
    
    # ডাটাবেজ থেকে জব এবং শপের তথ্য ফেচ করা
    cursor.execute("SELECT id, shop_id, file_name, copies, print_type, status, pages_input FROM print_jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    
    # পুরনো address এর পরিবর্তে নতুন ঠিকানা ফিল্ডগুলো সিলেক্ট করা হচ্ছে
    cursor.execute("SELECT shop_name, country, state, district, village, pincode, upi_id, bw_rate, color_rate FROM shops WHERE id = ?", (job[1],))
    shop_info = cursor.fetchone()
    
    conn.close()
    
    shop = {
        'shop_name': shop_info[0] if shop_info else "Shop",
        'country': shop_info[1] if shop_info else "",
        'state': shop_info[2] if shop_info else "",
        'district': shop_info[3] if shop_info else "",
        'village': shop_info[4] if shop_info else "",
        'pincode': shop_info[5] if shop_info else "",
        'upi_id': shop_info[6] if shop_info else "kamrul@okaxis",
        'bw_rate': shop_info[7] if shop_info else 5.0,
        'color_rate': shop_info[8] if shop_info else 10.0
    }
    
    return render_template('edit.html', shop=shop, job=job)



@app.route('/crop_and_merge/<int:job_id>', methods=['POST'])
def crop_and_merge_action(job_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT file_name, shop_id FROM print_jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    
    if job:
        filename = job[0]
        shop_id = job[1]
        
        try:
            # ফ্রন্ট ও ব্যাক পেজের ক্রপ করা বেসড সোর্স ডাটা (Base64) ফ্রন্টএন্ড থেকে রিসিভ করা
            front_crop_data = request.form.get('front_image')
            back_crop_data = request.form.get('back_image')
            
            if front_crop_data and back_crop_data:
                # Base64 থেকে ইমেজে কনভার্ট করা
                header, encoded1 = front_crop_data.split(",", 1)
                img1_data = base64.b64decode(encoded1)
                img1 = Image.open(io.BytesIO(img1_data))
                
                header, encoded2 = back_crop_data.split(",", 1)
                img2_data = base64.b64decode(encoded2)
                img2 = Image.open(io.BytesIO(img2_data))
                
           # A4 Portrait Canvas
            a4_width = 1240
            a4_height = 1754

            a4_canvas = Image.new(
              'RGB',
              (a4_width, a4_height),
              (255, 255, 255)
            )

       # Voter ID Card Size: 87 x 54.6 mm
            target_width = 514
            target_height = 322

       # Front and Back Resize
            img1_resized = img1.convert("RGB").resize(
            (target_width, target_height),
            Image.Resampling.LANCZOS
            )

            img2_resized = img2.convert("RGB").resize(
            (target_width, target_height),
             Image.Resampling.LANCZOS
            )

     # Center Position
            x_pos = (a4_width - target_width) // 2

            gap = 100
            total_height = (target_height * 2) + gap

            y1 = (a4_height - total_height) // 2
            y2 = y1 + target_height + gap

    # Paste Front and Back
            a4_canvas.paste(img1_resized, (x_pos, y1))
            a4_canvas.paste(img2_resized, (x_pos, y2))
                
                # নতুন ফাইল হিসেবে সেভ করা
            merged_filename = f"manual_merged_{filename.replace('.pdf', '')}.pdf"
            merged_path = os.path.join(UPLOAD_FOLDER, merged_filename)
            a4_canvas.save(merged_path, "PDF", resolution=150.0)
                
                # ডাটাবেজে আপডেট করা
                

            cursor.execute("UPDATE print_jobs SET file_name = ? WHERE id = ?", (merged_filename, job_id))
            conn.commit()
            print("Manual crop and merge successful!")
                
        except Exception as e:
            print(f"MANUAL CROP ERROR: {e}")
            
    conn.close()
    return redirect(url_for('edit_job', job_id=job_id))


import fitz  # PyMuPDF
import os

# প্রিভিউ এবং পেজ সিলেকশন রাউট
@app.route('/preview/<int:job_id>', methods=['GET', 'POST'])
def preview_job(job_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, shop_id, file_name, copies, print_type, amount, pages_input FROM print_jobs WHERE id = ?", (job_id,))
    db_job = cursor.fetchone()
    if not db_job:
        conn.close()
        return "Preview Not Found", 404
        
    shop_id = db_job[1]
    filename = db_job[2]
    saved_copies = db_job[3] or 1
    print_type_val = db_job[4] or 'bw'
    pages_input = db_job[6] if len(db_job) > 6 and db_job[6] else "1"
    
    # পেজ পার্সিং লজিক (যেমন: 1,3,4,6,8 বা রেঞ্জ হ্যান্ডেল করা)
    page_numbers = []
    try:
        if '-' in pages_input:
            parts = pages_input.split('-')
            page_numbers = list(range(int(parts[0].strip()), int(parts[1].strip()) + 1))
        elif ',' in pages_input:
            page_numbers = [int(p.strip()) for p in pages_input.split(',') if p.strip().isdigit()]
        else:
            page_numbers = [int(pages_input.strip())]
    except:
        page_numbers = [1]
        
    page_count = len(page_numbers) if page_numbers else 1

    # PyMuPDF দিয়ে নির্দিষ্ট পেজগুলোর প্রিভিউ ইমেজ জেনারেট করা
    preview_images = []
    if filename and filename.lower().endswith('.pdf'):
        pdf_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(pdf_path):
            try:
                doc = fitz.open(pdf_path)
                for p_num in page_numbers:
                    if 1 <= p_num <= len(doc):
                        page = doc[p_num - 1]
                        pix = page.get_pixmap(dpi=150)
                        img_name = f"preview_job_{job_id}_page_{p_num}.png"
                        img_path = os.path.join(app.config['UPLOAD_FOLDER'], img_name)
                        pix.save(img_path)
                        preview_images.append(img_name)
            except Exception as e:
                print("PDF Render Error:", e)

    if request.method == 'POST':
        print_type = request.form.get('print_type', 'bw')
        try:
            copies = int(request.form.get('copies', 1))
        except:
            copies = 1
        
        cursor.execute("SELECT bw_rate, color_rate FROM shops WHERE id = ?", (shop_id,))
        rates = cursor.fetchone()
        rate = rates[0] if (rates and print_type == 'bw') else (rates[1] if rates and len(rates) > 1 else 5.0)
        
        amount = float(copies) * float(page_count) * float(rate)
        
        cursor.execute("UPDATE print_jobs SET copies = ?, print_type = ?, amount = ? WHERE id = ?", 
                       (copies, print_type, amount, job_id))
        conn.commit()
        conn.close()
        return redirect(url_for('payment_page', job_id=job_id))

    # পুরনো address এর পরিবর্তে নতুন ঠিকানা ফিল্ডগুলো সিলেক্ট করা হচ্ছে
    cursor.execute("SELECT shop_name, country, state, district, village, pincode, bw_rate, color_rate FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    conn.close()
    
    shop = {
        'shop_name': shop_info[0] if shop_info else "Shop",
        'country': shop_info[1] if shop_info else "",
        'state': shop_info[2] if shop_info else "",
        'district': shop_info[3] if shop_info else "",
        'village': shop_info[4] if shop_info else "",
        'pincode': shop_info[5] if shop_info else "",
        'bw_rate': shop_info[6] if shop_info and len(shop_info) > 6 else 5.0,
        'color_rate': shop_info[7] if shop_info and len(shop_info) > 7 else 10.0
    }
    
    rate = shop['bw_rate'] if print_type_val == 'bw' else shop['color_rate']
    default_amount = float(saved_copies) * float(page_count) * float(rate)
    
    return render_template('preview.html', 
                           shop=shop, 
                           shop_id=shop_id, 
                           job_id=job_id,
                           filename=filename, 
                           preview_images=preview_images,
                           copies=saved_copies,
                           print_type=print_type_val,
                           page_count=page_count,
                           total_amount=default_amount)

@app.route('/payment/<int:job_id>', methods=['GET', 'POST'])
def payment_page(job_id):
    conn = connect_db('database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, shop_id, file_name, copies, print_type, amount, pages_input FROM print_jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    
    if not job:
        conn.close()
        return "Job not found", 404
        
    shop_id = job[1]
    
    # ডেটাবেজ থেকে কপি সংখ্যা নিরাপদভাবে তুলে নেওয়া হলো
    copies_val = job[3] if job[3] is not None else 1
    
    if request.method == 'POST':
        payment_mode = request.form.get('payment_mode')
        
        if payment_mode == 'cash':
            pay_conn = connect_db('database.db')
            pay_enabled = pay_conn.execute("SELECT COALESCE(cash_payment_enabled,1) FROM shops WHERE id=?", (shop_id,)).fetchone()
            pay_conn.close()
            if not pay_enabled or not pay_enabled[0]: return 'Cash payment is disabled by this shop.', 403
            cursor.execute("UPDATE print_jobs SET pay_mode = 'counter', status = 'Pending Cash', shop_id = ? WHERE id = ?", (shop_id, job_id))
            conn.commit()
            conn.close()
            return render_template('cash_waiting.html', shop_id=shop_id, job_id=job_id)
            
        elif payment_mode == 'online':
            pay_conn = connect_db('database.db')
            pay_enabled = pay_conn.execute("SELECT COALESCE(online_payment_enabled,1) FROM shops WHERE id=?", (shop_id,)).fetchone()
            pay_conn.close()
            if not pay_enabled or not pay_enabled[0]: return 'Online payment is disabled by this shop.', 403
            cursor.execute("UPDATE print_jobs SET pay_mode = 'online', status = 'Pending Online Payment', shop_id = ? WHERE id = ?", (shop_id, job_id))
            conn.commit()
            conn.close()
            return jsonify(ok=True, message='Online payment selected')

    # পুরনো address এর পরিবর্তে নতুন ঠিকানা ফিল্ডগুলো সিলেক্ট করা হচ্ছে
    cursor.execute("SELECT shop_name, country, state, district, village, pincode, upi_id, bw_rate, color_rate FROM shops WHERE id = ?", (shop_id,))
    shop_info = cursor.fetchone()
    conn.close()
    
    pages_input = job[6] or "1"  # job[6] হলো ইউজারের দেওয়া পেজ ইনপুট
    page_count = 1

    try:
        if ',' in pages_input:
            pages_list = [p.strip() for p in pages_input.split(',') if p.strip()]
            page_count = len(pages_list)
        elif '-' in pages_input:
            parts = pages_input.split('-')
            page_count = (int(parts[1].strip()) - int(parts[0].strip())) + 1
        else:
            page_count = int(pages_input.strip())
    except:
        page_count = 1

    shop = {
        'shop_name': shop_info[0] if shop_info else "Shop",
        'country': shop_info[1] if shop_info else "",
        'state': shop_info[2] if shop_info else "",
        'district': shop_info[3] if shop_info else "",
        'village': shop_info[4] if shop_info else "",
        'pincode': shop_info[5] if shop_info else "",
        'upi_id': shop_info[6] if shop_info else "kamrul@okaxis",
        'bw_rate': shop_info[7] if shop_info else 5.0,
        'color_rate': shop_info[8] if shop_info else 10.0
    }

    rate = shop['bw_rate'] if job[4] == 'bw' else shop['color_rate']
    total_amount = round(page_count * copies_val * float(rate or 0), 2)

    # Payment page-এর হিসাব ডেটাবেজে রাখি; Draft অবস্থায় dashboard-এ দেখানো হবে না।
    save_conn = connect_db('database.db')
    save_conn.execute("UPDATE print_jobs SET amount = ? WHERE id = ? AND status IN ('Draft','Pending','Pending Online Payment')", (total_amount, job_id))
    save_conn.commit()
    save_conn.close()

    return render_template('payment.html', shop=shop, shop_id=shop_id, job_id=job_id, filename=job[2], copies=copies_val, print_type=job[4], total_amount=total_amount, page_count=page_count)

@app.route('/success/<int:job_id>', methods=['POST'])
def success_job(job_id):
    """Customer payment claim only; this does NOT confirm money was received."""
    conn = connect_db('database.db')
    cursor = conn.cursor()

    cursor.execute("SELECT id, shop_id, status FROM print_jobs WHERE id = ?", (job_id,))
    job = cursor.fetchone()
    if not job:
        conn.close()
        return "Job not found!", 404

    # Customer claim only after choosing Pay Online; never accept an untouched Draft.
    if job[2] not in ('Pending Online Payment', 'Pending Verification'):
        conn.close()
        return 'আগে Pay Online বাটনটি নির্বাচন করুন।', 400

    # Customer claim is queued for shop-owner verification, never marked Paid here.
    if job[2] != 'Online Paid':
        cursor.execute(
            "UPDATE print_jobs SET pay_mode = 'online', status = 'Pending Verification' WHERE id = ?",
            (job_id,)
        )
        conn.commit()

    cursor.execute("SELECT shop_name FROM shops WHERE id = ?", (job[1],))
    shop = cursor.fetchone()
    conn.close()

    return (
        "<!doctype html><html lang='bn'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Payment Verification Pending</title>"
        "<body style='font-family:sans-serif;max-width:520px;margin:60px auto;padding:20px;text-align:center'>"
        "<h2>পেমেন্ট যাচাইয়ের অপেক্ষায়</h2>"
        f"<p>{shop[0] if shop else 'দোকান'}-এর কাছে আপনার পেমেন্টের দাবি পাঠানো হয়েছে।</p>"
        "<p>দোকানদার টাকা পাওয়ার পর পেমেন্ট নিশ্চিত করবেন। এখনো পেমেন্ট Paid হিসেবে নিশ্চিত হয়নি।</p>"
        "</body></html>",
        200,
        {'Content-Type': 'text/html; charset=utf-8'}
    )



if __name__ == "__main__":
  init_db() 
  app.run(debug=True, host="0.0.0.0", port=5000)
