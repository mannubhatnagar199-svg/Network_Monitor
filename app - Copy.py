from flask import Flask, render_template, jsonify, request, send_file
import csv
import os
import subprocess
import threading
import time
import pandas as pd
import io
import logging
from typing import Dict, List, Optional
from threading import Lock
from functools import wraps
from datetime import datetime
import platform
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

CSV_FILE = "cameras.csv"
MONITOR_INTERVAL = 60
PING_TIMEOUT = 1000  # milliseconds

# Thread-safe storage
device_status_lock = Lock()
device_status: List[Dict] = []
last_status: Dict[str, str] = {}


def with_file_lock(func):
    lock = Lock()

    @wraps(func)
    def wrapper(*args, **kwargs):
        with lock:
            return func(*args, **kwargs)

    return wrapper


def ping(ip: str) -> bool:
    try:
        system = platform.system().lower()
        if system == "windows":
            cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT), ip]
        else:
            # Linux/Unix: -c 1 (count), -W 1 (timeout in seconds)
            cmd = ["ping", "-c", "1", "-W", "1", ip]
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,  # Overall timeout for the subprocess
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning(f"Ping timeout for IP: {ip}")
        return False
    except Exception as e:
        logger.error(f"Error pinging {ip}: {str(e)}")
        return False


# Email alert configuration (fill with your details)
MAIL_ALERT_ENABLED = True  # Set to False to disable
MAIL_SMTP_SERVER = 'smtp.gmail.com'  # e.g., 'smtp.gmail.com'
MAIL_SMTP_PORT = 587
MAIL_USERNAME = 'mannubhatnagar199@gmail.com'
MAIL_PASSWORD = 'jtxt nupv jfjx mmpi'
MAIL_FROM = 'deepanshu.bhatnagar@outlook.com'
MAIL_TO = 'laphospitalrdr@gmail.com'  # Your mail id


def send_mail_alert(device_name: str, device_ip: str, device_category: str):
    if not MAIL_ALERT_ENABLED:
        return
    try:
        subject = f"Device Offline Alert: {device_name} ({device_ip})"
        body = f"The device '{device_name}' (IP: {device_ip}, Category: {device_category}) has gone OFFLINE."
        msg = MIMEMultipart()
        msg['From'] = MAIL_FROM
        msg['To'] = MAIL_TO
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(MAIL_SMTP_SERVER, MAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)
            server.sendmail(MAIL_FROM, MAIL_TO, msg.as_string())
        logger.info(f"Mail alert sent for device {device_name} ({device_ip})")
    except Exception as e:
        logger.error(f"Failed to send mail alert: {str(e)}")


def monitor_devices() -> None:
    global device_status
    while True:
        try:
            devices = read_devices()
            with device_status_lock:
                if not device_status:
                    device_status = devices.copy()
                for cam in devices:
                    ip = cam["ip"]
                    prev_status = last_status.get(ip, "Unknown")
                    status = "Online" if ping(ip) else "Offline"
                    cam["status"] = status
                    # Alert if status changed from Online to Offline
                    if prev_status == "Online" and status == "Offline":
                        send_mail_alert(cam["name"], cam["ip"], cam["category"])
                    last_status[ip] = status
                    # Update device_status
                    for d in device_status:
                        if d["ip"] == ip:
                            d["status"] = status
                            d["last_checked"] = datetime.now().isoformat()
                            break
            logger.info(f"Completed monitoring cycle. Devices checked: {len(devices)}")
        except Exception as e:
            logger.error(f"Error in monitor thread: {str(e)}")
        time.sleep(MONITOR_INTERVAL)


@with_file_lock
def read_devices() -> List[Dict]:
    devices = []
    try:
        if os.path.exists(CSV_FILE):
            with open(CSV_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    devices.append(
                        {
                            "name": row["name"],
                            "ip": row["ip"],
                            "category": row["category"],
                            "status": "Checking...",
                        }
                    )
    except Exception as e:
        logger.error(f"Error reading devices: {str(e)}")
    return devices


@with_file_lock
def write_devices(devices: List[Dict]) -> None:
    try:
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["name", "ip", "category", "status"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(devices)
    except Exception as e:
        logger.error(f"Error writing devices: {str(e)}")
        raise


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    global device_status
    grouped_summary = {}
    for cam in device_status:
        cat = cam["category"]
        stat = cam["status"]
        if cat not in grouped_summary:
            grouped_summary[cat] = {"total": 0, "online": 0, "offline": 0}
        grouped_summary[cat]["total"] += 1
        if stat == "Online":
            grouped_summary[cat]["online"] += 1
        elif stat == "Offline":
            grouped_summary[cat]["offline"] += 1
    return jsonify({"status": device_status, "summary": grouped_summary})


# CSV Export
@app.route("/export")
def export_csv():
    return send_file(CSV_FILE, as_attachment=True)


# CSV Import
@app.route("/import", methods=["POST"])
def import_csv():
    file = request.files["file"]
    if file:
        file.save(CSV_FILE)
        # After import, sync device_status
        global device_status
        device_status = []
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                device_status.append(
                    {
                        "name": row["name"],
                        "ip": row["ip"],
                        "category": row["category"],
                        "status": last_status.get(row["ip"], "Checking..."),
                    }
                )
        return "CSV Imported", 200
    return "No file", 400


def create_excel_response(filtered_devices, filename):
    df = pd.DataFrame(filtered_devices)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/download_online_excel")
def download_online_excel():
    online_devices = [d for d in device_status if d["status"] == "Online"]
    return create_excel_response(online_devices, "online_devices.xlsx")


@app.route("/download_offline_excel")
def download_offline_excel():
    offline_devices = [d for d in device_status if d["status"] == "Offline"]
    return create_excel_response(offline_devices, "offline_devices.xlsx")


@app.route("/device/add", methods=["POST"])
def add_device():
    data = request.json
    required_fields = {"name", "ip", "category", "status"}
    if not data or not required_fields.issubset(data.keys()):
        return jsonify({"error": "Missing required fields"}), 400

    devices = read_devices()
    if any(d["ip"] == data["ip"] for d in devices):
        return jsonify({"error": "Device with this IP already exists"}), 400

    devices.append(
        {
            "name": data["name"],
            "ip": data["ip"],
            "category": data["category"],
            "status": data["status"],
        }
    )
    write_devices(devices)

    global device_status
    device_status = []
    for d in devices:
        device_status.append(
            {
                "name": d["name"],
                "ip": d["ip"],
                "category": d["category"],
                "status": last_status.get(d["ip"], "Checking..."),
            }
        )

    return jsonify({"message": "Device added successfully"})


@app.route("/device/edit", methods=["POST"])
def edit_device():
    data = request.json
    required_fields = {"name", "ip", "category", "status"}
    if not data or not required_fields.issubset(data.keys()):
        return jsonify({"error": "Missing required fields"}), 400

    devices = read_devices()
    updated = False
    for d in devices:
        if d["ip"] == data["ip"]:
            d["name"] = data["name"]
            d["category"] = data["category"]
            d["status"] = data["status"]
            updated = True
            break

    if not updated:
        return jsonify({"error": "Device not found"}), 404

    write_devices(devices)

    global device_status
    device_status = []
    for d in devices:
        device_status.append(
            {
                "name": d["name"],
                "ip": d["ip"],
                "category": d["category"],
                "status": last_status.get(d["ip"], "Checking..."),
            }
        )

    return jsonify({"message": "Device updated successfully"})


@app.route("/device/delete", methods=["POST"])
def delete_device():
    data = request.json
    if not data or "ip" not in data:
        return jsonify({"error": "Missing IP field"}), 400

    devices = read_devices()
    new_devices = [d for d in devices if d["ip"] != data["ip"]]

    if len(new_devices) == len(devices):
        return jsonify({"error": "Device not found"}), 404

    write_devices(new_devices)

    global device_status
    device_status = []
    for d in new_devices:
        device_status.append(
            {
                "name": d["name"],
                "ip": d["ip"],
                "category": d["category"],
                "status": last_status.get(d["ip"], "Checking..."),
            }
        )

    return jsonify({"message": "Device deleted successfully"})


def start_background_thread() -> None:
    if not getattr(app, "monitor_thread_started", False):
        logger.info("Starting monitoring thread")
        t = threading.Thread(target=monitor_devices, daemon=True)
        t.start()
        app.monitor_thread_started = True


if __name__ == "__main__":
    start_background_thread()
    from waitress import serve

    logger.info("Starting server on port 8080")
    serve(app, host="0.0.0.0", port=8080, threads=16)
else:
    start_background_thread()
