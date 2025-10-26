import os
import sqlite3
import threading
import time
import subprocess
import platform
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, jsonify, request, send_file
import pandas as pd
import io
import logging
from waitress import serve
import pytz

def convert_utc_to_ist(utc_string):
    """Convert UTC timestamp string to IST formatted string"""
    try:
        if not utc_string:
            return 'N/A'
        # Parse UTC timestamp
        utc_time = datetime.fromisoformat(utc_string.replace('Z', '+00:00'))
        # Add IST offset (5 hours 30 minutes)
        ist_offset = timedelta(hours=5, minutes=30)
        ist_time = utc_time + ist_offset
        # Return formatted string
        return ist_time.strftime('%d/%m/%Y %H:%M:%S')
    except:
        return utc_string
        
# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "devices.db")
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL", "20"))
PING_TIMEOUT_MS = int(os.getenv("PING_TIMEOUT_MS", "1000"))
PING_CONCURRENCY = int(os.getenv("PING_CONCURRENCY", "128"))

# -----------------------------------------------------------------------------
# App and logging
# -----------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("device-dashboard")

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA busy_timeout=5000;")
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ip TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL
        );
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS status_current (
            device_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('Online','Offline','Checking...')),
            last_checked TEXT NOT NULL,
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        );
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        );
        """)
        
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_device_time
        ON status_history(device_id, changed_at DESC);
        """)
        
        cur.execute("""
        CREATE TABLE IF NOT EXISTS email_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0,
            smtp_server TEXT NOT NULL DEFAULT 'smtp.gmail.com',
            smtp_port INTEGER NOT NULL DEFAULT 587,
            username TEXT NOT NULL DEFAULT '',
            password TEXT NOT NULL DEFAULT '',
            from_email TEXT NOT NULL DEFAULT '',
            to_email TEXT NOT NULL DEFAULT ''
        );
        """)
        
        cur.execute("INSERT OR IGNORE INTO email_config (id, enabled) VALUES (1, 0);")
        
        # Add UI config table for background settings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS ui_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            background_type TEXT NOT NULL DEFAULT 'color',
            background_color TEXT NOT NULL DEFAULT '#f8f9fa',
            background_image TEXT,
            background_opacity REAL NOT NULL DEFAULT 0.1
        );
        """)
        
        cur.execute("INSERT OR IGNORE INTO ui_config (id) VALUES (1);")
        
        conn.commit()
    finally:
        conn.close()

  
# -----------------------------------------------------------------------------
# Utility
# -----------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ping_host(ip: str) -> bool:
    system = platform.system().lower()
    if "windows" in system:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT_MS), ip]
    else:
        timeout_s = max(1, int(round(PING_TIMEOUT_MS / 1000)))
        cmd = ["ping", "-c", "1", "-W", str(timeout_s), ip]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception as e:
        log.warning(f"Ping failed for {ip}: {e}")
        return False

def db_execute(query: str, params: tuple = ()):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur
    finally:
        conn.close()

def db_query_all(query: str, params: tuple = ()) -> List[sqlite3.Row]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return rows
    finally:
        conn.close()

def db_query_one(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    rows = db_query_all(query, params)
    return rows[0] if rows else None

# -----------------------------------------------------------------------------
# Email functionality
# -----------------------------------------------------------------------------

def send_email_alert(device_name: str, device_ip: str, device_category: str):
    try:
        config = db_query_one("SELECT * FROM email_config WHERE id=1;")
        if not config or not config["enabled"]:
            return
        
        # Get current time in IST
        ist = pytz.timezone('Asia/Kolkata')
        ist_time = datetime.now(ist)
        
        subject = f"🚨 Device Offline Alert: {device_name}"
        body = f"""
Device Offline Notification

Device Name: {device_name}
IP Address: {device_ip}
Category: {device_category}
Time: {ist_time.strftime("%Y-%m-%d %H:%M:%S IST")}

The device has gone offline and requires attention.
"""
        
        msg = MIMEMultipart()
        msg['From'] = config["from_email"]
        msg['To'] = config["to_email"]
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["username"], config["password"])
            server.sendmail(config["from_email"], config["to_email"], msg.as_string())
        
        log.info(f"Email alert sent for {device_name} ({device_ip})")
    except Exception as e:
        log.error(f"Failed to send email alert: {e}")


# -----------------------------------------------------------------------------
# Email configuration endpoints
# -----------------------------------------------------------------------------
@app.route("/email/config", methods=["GET"])
def get_email_config():
    row = db_query_one("SELECT * FROM email_config WHERE id=1;")
    if row:
        config = dict(row)
        config['password'] = '********' if config['password'] else ''
        return jsonify(config)
    return jsonify({"enabled": 0, "smtp_server": "smtp.gmail.com", "smtp_port": 587})

@app.route("/email/config", methods=["POST"])
def update_email_config():
    data = request.get_json(force=True)
    enabled = 1 if data.get("enabled") else 0
    smtp_server = data.get("smtp_server", "smtp.gmail.com")
    smtp_port = int(data.get("smtp_port", 587))
    username = data.get("username", "")
    password = data.get("password", "")
    from_email = data.get("from_email", "")
    to_email = data.get("to_email", "")
    
    if password == '********':
        db_execute("""
            UPDATE email_config SET 
            enabled=?, smtp_server=?, smtp_port=?, username=?, from_email=?, to_email=?
            WHERE id=1;
        """, (enabled, smtp_server, smtp_port, username, from_email, to_email))
    else:
        db_execute("""
            UPDATE email_config SET 
            enabled=?, smtp_server=?, smtp_port=?, username=?, password=?, from_email=?, to_email=?
            WHERE id=1;
        """, (enabled, smtp_server, smtp_port, username, password, from_email, to_email))
    
    return jsonify({"ok": True})

# -----------------------------------------------------------------------------
# Device CRUD
# -----------------------------------------------------------------------------
@app.route("/devices", methods=["GET"])
def list_devices():
    rows = db_query_all("SELECT id, name, ip, category FROM devices ORDER BY id ASC;")
    devices = [dict(r) for r in rows]
    return jsonify({"devices": devices})

@app.route("/devices", methods=["POST"])
def add_device():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    ip = data.get("ip", "").strip()
    category = data.get("category", "").strip() or "Default"
    if not name or not ip:
        return jsonify({"error": "name and ip required"}), 400
    try:
        cur = db_execute(
            "INSERT INTO devices(name, ip, category) VALUES (?,?,?);",
            (name, ip, category)
        )
        device_id = cur.lastrowid
        db_execute(
            "INSERT OR REPLACE INTO status_current(device_id, status, last_checked) VALUES (?,?,?);",
            (device_id, "Checking...", now_iso())
        )
        return jsonify({"id": device_id, "name": name, "ip": ip, "category": category})
    except sqlite3.IntegrityError:
        return jsonify({"error": "IP must be unique"}), 409

@app.route("/devices/<int:device_id>", methods=["PUT"])
def update_device(device_id: int):
    data = request.get_json(force=True)
    name = data.get("name")
    ip = data.get("ip")
    category = data.get("category")
    row = db_query_one("SELECT id FROM devices WHERE id=?;", (device_id,))
    if not row:
        return jsonify({"error": "not found"}), 404
    try:
        sets = []
        vals = []
        if name is not None:
            sets.append("name=?")
            vals.append(name)
        if ip is not None:
            sets.append("ip=?")
            vals.append(ip)
        if category is not None:
            sets.append("category=?")
            vals.append(category)
        if not sets:
            return jsonify({"ok": True})
        vals.append(device_id)
        db_execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?;", tuple(vals))
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"error": "IP must be unique"}), 409

@app.route("/devices/<int:device_id>", methods=["DELETE"])
def delete_device(device_id: int):
    db_execute("DELETE FROM devices WHERE id=?;", (device_id,))
    return jsonify({"ok": True})

# -----------------------------------------------------------------------------
# Import/Export CSV and Excel
# -----------------------------------------------------------------------------
@app.route("/devices/import", methods=["POST"])
def import_devices():
    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400
    file = request.files["file"]
    try:
        df = pd.read_csv(file)
    except Exception:
        file.stream.seek(0)
        df = pd.read_csv(file, sep=None, engine="python")
    required = {"name", "ip", "category"}
    missing = required - set(x.lower() for x in df.columns)
    if missing:
        return jsonify({"error": f"missing columns: {', '.join(sorted(missing))}"}), 400

    cols = {c.lower(): c for c in df.columns}
    name_col = cols.get("name")
    ip_col = cols.get("ip")
    cat_col = cols.get("category")

    added = 0
    updated = 0
    for _, row in df.iterrows():
        name = str(row[name_col]).strip()
        ip = str(row[ip_col]).strip()
        category = str(row[cat_col]).strip() or "Default"
        if not name or not ip:
            continue
        existing = db_query_one("SELECT id FROM devices WHERE ip=?;", (ip,))
        if existing:
            db_execute("UPDATE devices SET name=?, category=? WHERE id=?;", (name, category, existing["id"]))
            updated += 1
        else:
            cur = db_execute("INSERT INTO devices(name, ip, category) VALUES (?,?,?);", (name, ip, category))
            device_id = cur.lastrowid
            db_execute(
                "INSERT OR REPLACE INTO status_current(device_id, status, last_checked) VALUES (?,?,?);",
                (device_id, "Checking...", now_iso())
            )
            added += 1
    return jsonify({"added": added, "updated": updated})

@app.route("/devices/export", methods=["GET"])
def export_devices_csv():
    rows = db_query_all("SELECT name, ip, category FROM devices ORDER BY id ASC;")
    df = pd.DataFrame([dict(r) for r in rows], columns=["name", "ip", "category"])
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    filename = f"devices_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name=filename)

@app.route("/devices/export_excel", methods=["GET"])
def export_devices_excel():
    rows = db_query_all("SELECT name, ip, category FROM devices ORDER BY id ASC;")
    df = pd.DataFrame([dict(r) for r in rows], columns=["name", "ip", "category"])
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Devices")
    out.seek(0)
    filename = f"devices_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=filename)




# -----------------------------------------------------------------------------
# Status and summaries for frontend
# -----------------------------------------------------------------------------
def build_status_payload(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    devices = []
    summary_by_category: Dict[str, Dict[str, int]] = {}
    online_count = 0
    offline_count = 0
    checking_count = 0
    for r in rows:
        devices.append({
            "name": r["name"],
            "ip": r["ip"],
            "category": r["category"],
            "status": r["status"],
            "last_checked": r["last_checked"],
        })
        cat = r["category"]
        if cat not in summary_by_category:
            summary_by_category[cat] = {"Online": 0, "Offline": 0, "Checking...": 0}
        summary_by_category[cat][r["status"]] += 1
        if r["status"] == "Online":
            online_count += 1
        elif r["status"] == "Offline":
            offline_count += 1
        else:
            checking_count += 1

    overall = {
        "online": online_count,
        "offline": offline_count,
        "checking": checking_count,
        "total": online_count + offline_count + checking_count
    }
    return {
        "devices": devices,
        "summary_by_category": summary_by_category,
        "overall": overall
    }

@app.route("/status", methods=["GET"])
def get_status():
    rows = db_query_all("""
        SELECT d.name, d.ip, d.category, sc.status, sc.last_checked
        FROM devices d
        JOIN status_current sc ON sc.device_id = d.id
        ORDER BY d.id ASC;
    """)
    payload = build_status_payload(rows)
    return jsonify(payload)

# -----------------------------------------------------------------------------
# Event manager: last 10 devices that went Offline
# -----------------------------------------------------------------------------
@app.route("/events/recent", methods=["GET"])
def recent_events():
    rows = db_query_all("""
        SELECT h.changed_at, d.name, d.ip
        FROM status_history h
        JOIN devices d ON d.id = h.device_id
        WHERE h.status = 'Offline'
        ORDER BY h.changed_at DESC
        LIMIT 10;
    """)
    events = [{
        "changed_at": r["changed_at"],
        "name": r["name"],
        "ip": r["ip"]
    } for r in rows]
    return jsonify({"events": events})

# -----------------------------------------------------------------------------
# Monitor worker
# -----------------------------------------------------------------------------
_monitor_stop = threading.Event()

def compute_status(ok: bool) -> str:
    return "Online" if ok else "Offline"

def monitor_once():
    dev_rows = db_query_all("SELECT id, ip, name, category FROM devices ORDER BY id ASC;")
    if not dev_rows:
        return
    
    results: Dict[int, tuple] = {}
    with ThreadPoolExecutor(max_workers=PING_CONCURRENCY) as pool:
        futures = {pool.submit(ping_host, r["ip"]): r for r in dev_rows}
        for fut in as_completed(futures):
            dev_row = futures[fut]
            ok = False
            try:
                ok = fut.result()
            except Exception as e:
                log.warning(f"Ping error for device {dev_row['id']}: {e}")
            results[dev_row["id"]] = (ok, dev_row)

    ts = now_iso()
    for dev_id, (ok, dev_row) in results.items():
        current = db_query_one("SELECT status FROM status_current WHERE device_id=?;", (dev_id,))
        new_status = compute_status(ok)
        
        if current is None:
            db_execute("INSERT INTO status_current(device_id, status, last_checked) VALUES (?,?,?);",
                       (dev_id, new_status, ts))
            db_execute("INSERT INTO status_history(device_id, status, changed_at) VALUES (?,?,?);",
                       (dev_id, new_status, ts))
        else:
            old_status = current["status"]
            if old_status != new_status:
                db_execute("UPDATE status_current SET status=?, last_checked=? WHERE device_id=?;",
                           (new_status, ts, dev_id))
                db_execute("INSERT INTO status_history(device_id, status, changed_at) VALUES (?,?,?);",
                           (dev_id, new_status, ts))
                
                if old_status == "Online" and new_status == "Offline":
                    send_email_alert(dev_row["name"], dev_row["ip"], dev_row["category"])
            else:
                db_execute("UPDATE status_current SET last_checked=? WHERE device_id=?;", (ts, dev_id))

def monitor_loop():
    log.info("Monitor loop started")
    while not _monitor_stop.is_set():
        start = time.time()
        try:
            monitor_once()
        except Exception as e:
            log.exception(f"Monitor cycle failed: {e}")
        elapsed = time.time() - start
        sleep_for = max(1.0, MONITOR_INTERVAL - elapsed)
        _monitor_stop.wait(sleep_for)
    log.info("Monitor loop stopped")

# Get background config
@app.route("/ui/background", methods=["GET"])
def get_background_config():
    row = db_query_one("SELECT * FROM ui_config WHERE id=1;")
    if row:
        return jsonify(dict(row))
    return jsonify({"background_type": "color", "background_color": "#f8f9fa", "background_opacity": 1.0})

# Update background config
@app.route("/ui/background", methods=["POST"])
def update_background_config():
    data = request.get_json(force=True)
    bg_type = data.get("background_type", "color")
    bg_color = data.get("background_color", "#f8f9fa")
    bg_image = data.get("background_image")
    bg_opacity = float(data.get("background_opacity", 1.0))
    
    db_execute("""
        UPDATE ui_config SET 
        background_type=?, background_color=?, background_image=?, background_opacity=?
        WHERE id=1;
    """, (bg_type, bg_color, bg_image, bg_opacity))
    
    return jsonify({"ok": True})

# Upload background image
@app.route("/ui/background/upload", methods=["POST"])
def upload_background_image():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400
    
    # Check file type
    allowed_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        return jsonify({"error": "Invalid file type. Use JPG, PNG, GIF, or WEBP"}), 400
    
    # Save to static/backgrounds folder
    upload_folder = os.path.join("static", "backgrounds")
    os.makedirs(upload_folder, exist_ok=True)
    
    # Generate unique filename
    filename = f"bg_{int(time.time())}{file_ext}"
    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)
    
    # Return relative URL
    return jsonify({"url": f"/backgrounds/{filename}"})
    
# Export Online devices only
@app.route("/devices/export_excel_online", methods=["GET"])
def export_devices_excel_online():
    print("=== ONLINE EXPORT CALLED ===")
    try:
        rows = db_query_all("""
            SELECT d.name, d.ip, d.category, sc.status, sc.last_checked
            FROM devices d
            JOIN status_current sc ON sc.device_id = d.id
            WHERE sc.status = 'Online'
            ORDER BY d.id ASC;
        """)
        print(f"Found {len(rows)} online devices")
        
        if len(rows) == 0:
            return jsonify({"error": "No online devices found"}), 404
        
        data_list = []
        for r in rows:
            utc_str = r['last_checked']
            try:
                if utc_str:
                    dt = datetime.strptime(utc_str.replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
                    ist_dt = dt + timedelta(hours=5, minutes=30)
                    ist_formatted = ist_dt.strftime('%d/%m/%Y %H:%M:%S')
                else:
                    ist_formatted = 'N/A'
            except:
                ist_formatted = utc_str
            
            data_list.append({
                'name': r['name'],
                'ip': r['ip'],
                'category': r['category'],
                'status': r['status'],
                'last_checked_ist': ist_formatted
            })
        
        df = pd.DataFrame(data_list)
        df.columns = ["Device Name", "IP Address", "Category", "Status", "Last Checked (IST)"]
        
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Online Devices")
            worksheet = writer.sheets["Online Devices"]
            worksheet.set_column(0, 0, 20)
            worksheet.set_column(1, 1, 15)
            worksheet.set_column(2, 2, 15)
            worksheet.set_column(3, 3, 10)
            worksheet.set_column(4, 4, 20)
        
        out.seek(0)
        filename = f"online_devices_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return send_file(out, 
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        as_attachment=True, 
                        download_name=filename)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/devices/export_excel_offline", methods=["GET"])
def export_devices_excel_offline():
    print("=== OFFLINE EXPORT CALLED ===")
    try:
        rows = db_query_all("""
            SELECT d.name, d.ip, d.category, sc.status, sc.last_checked
            FROM devices d
            JOIN status_current sc ON sc.device_id = d.id
            WHERE sc.status = 'Offline'
            ORDER BY d.id ASC;
        """)
        print(f"Found {len(rows)} offline devices")
        
        if len(rows) == 0:
            return jsonify({"error": "No offline devices found"}), 404
        
        # Convert to list and format timestamps DIRECTLY
        data_list = []
        for r in rows:
            # Get the timestamp and convert it inline
            utc_str = r['last_checked']
            print(f"Original timestamp from DB: {utc_str}")  # Debug
            
            try:
                # Parse and convert to IST
                if utc_str:
                    dt = datetime.strptime(utc_str.replace('Z', ''), '%Y-%m-%dT%H:%M:%S')
                    ist_dt = dt + timedelta(hours=5, minutes=30)
                    ist_formatted = ist_dt.strftime('%d/%m/%Y %H:%M:%S')
                    print(f"Converted to IST: {ist_formatted}")  # Debug
                else:
                    ist_formatted = 'N/A'
            except Exception as e:
                print(f"Conversion error: {e}")
                ist_formatted = utc_str  # Use original if error
            
            data_list.append({
                'name': r['name'],
                'ip': r['ip'],
                'category': r['category'],
                'status': r['status'],
                'last_checked_ist': ist_formatted
            })
        
        df = pd.DataFrame(data_list)
        df.columns = ["Device Name", "IP Address", "Category", "Status", "Last Checked (IST)"]
        
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Offline Devices")
            worksheet = writer.sheets["Offline Devices"]
            worksheet.set_column(0, 0, 20)  # Device Name
            worksheet.set_column(1, 1, 15)  # IP
            worksheet.set_column(2, 2, 15)  # Category
            worksheet.set_column(3, 3, 10)  # Status
            worksheet.set_column(4, 4, 20)  # Last Checked
        
        out.seek(0)
        filename = f"offline_devices_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return send_file(out, 
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        as_attachment=True, 
                        download_name=filename)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



# -----------------------------------------------------------------------------
# App start
# -----------------------------------------------------------------------------
@app.route("/")
def root():
    from flask import send_from_directory
    return send_from_directory("static", "index.html")

def start_monitor_in_thread():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    return t

if __name__ == "__main__":
    init_db()
    rows = db_query_all("""
        SELECT d.id FROM devices d
        LEFT JOIN status_current s ON s.device_id = d.id
        WHERE s.device_id IS NULL;
    """)
    for r in rows:
        db_execute(
            "INSERT OR REPLACE INTO status_current(device_id, status, last_checked) VALUES (?,?,?);",
            (r["id"], "Checking...", now_iso())
        )
    
    #start_monitor_in_thread()
    #app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
   

if __name__ == "__main__":
    init_db()
    start_monitor_in_thread()
    serve(app, listen="0.0.0.0:5000")