#!/usr/bin/env python3
"""Omada Web Manager - Interface web de gestion du service Omada."""

import os
import re
import pty
import select
import signal
import shutil
import struct
import fcntl
import termios
import subprocess
import json
import secrets
import threading
import tempfile
import urllib.request
import urllib.error
from datetime import datetime

from flask import (
    Flask, render_template, request, session, redirect,
    url_for, jsonify, flash, send_file
)
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

import pam

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"deb"}
SERVICE_NAME = "tpeap"
OMADA_DIR = "/opt/tplink/EAPController"
OMADA_DATA_DIR = os.path.join(OMADA_DIR, "data")

# Native Omada backup path (same as prerm/postinst scripts)
OMADA_BACKUP_DIR = "/opt/tplink/omada_db_backup"
DB_FILE_NAME = "omada.db.tar.gz"
CLUSTER_FILE_NAME = "cluster.tar.gz"

CONFIG_FILE = os.path.join(BASE_DIR, "config.txt")
DEFAULT_PORT = 30560

# Auto-backup configuration
AUTO_BACKUP_CONFIG = os.path.join(BASE_DIR, "auto_backup.json")
AUTO_BACKUP_SCRIPT = os.path.join(BASE_DIR, "auto_backup.sh")
CRON_COMMENT = "omada-web-manager-auto-backup"


def generate_backup_filename():
    """Generate a timestamped backup filename."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"omada_db_{timestamp}.tar.gz"


# Self-update configuration
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/Vayaris/Omada-Manager/main"
MANAGER_SERVICE_NAME = "omada-web"
UPDATE_FILES = [
    ("app.py", "app.py"),
    ("requirements.txt", "requirements.txt"),
    ("templates/index.html", "templates/index.html"),
    ("templates/login.html", "templates/login.html"),
    ("static/style.css", "static/style.css"),
    ("VERSION", "VERSION"),
]


def read_port():
    try:
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PORT="):
                    return int(line.split("=", 1)[1])
    except (FileNotFoundError, ValueError):
        pass
    return DEFAULT_PORT


app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_hex(32)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB

socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

terminal_processes = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_service_status():
    """Return the status of the Omada service."""
    # Reload systemd to pick up any new/changed unit files (after install/uninstall)
    try:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True, text=True, timeout=10
        )
        state = result.stdout.strip()
    except Exception:
        state = "unknown"

    detail = ""
    try:
        result = subprocess.run(
            ["systemctl", "status", SERVICE_NAME],
            capture_output=True, text=True, timeout=10
        )
        detail = result.stdout + result.stderr
    except Exception:
        pass

    return {"state": state, "detail": detail}


def get_omada_version():
    """Return the installed Omada version, or None if not installed."""
    try:
        result = subprocess.run(
            ["dpkg", "-l", "omadac"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if line.startswith("ii"):
                parts = line.split()
                if len(parts) >= 3:
                    return parts[2]
    except Exception:
        pass
    return None


def is_omada_installed():
    """Check if Omada is installed."""
    return get_omada_version() is not None


def check_dependencies():
    """Check if Java 17+, MongoDB and JSVC are installed."""
    java_ok = False
    java_version = None
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stderr + result.stdout
        for line in output.splitlines():
            if "version" in line.lower():
                java_version = line.strip()
                match = re.search(r'"?(\d+)', line)
                if match and int(match.group(1)) >= 17:
                    java_ok = True
                break
    except Exception:
        pass

    mongo_ok = False
    mongo_version = None
    try:
        result = subprocess.run(
            ["mongod", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            mongo_ok = True
            for line in result.stdout.splitlines():
                if "db version" in line.lower():
                    mongo_version = line.strip()
                    break
    except Exception:
        pass

    jsvc_ok = False
    jsvc_version = None
    try:
        result = subprocess.run(
            ["dpkg", "-l", "jsvc"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if line.startswith("ii"):
                jsvc_ok = True
                parts = line.split()
                if len(parts) >= 3:
                    jsvc_version = f"jsvc {parts[2]}"
                else:
                    jsvc_version = "jsvc (Apache Commons Daemon)"
                break
    except Exception:
        pass

    return {
        "java": {"installed": java_ok, "version": java_version},
        "mongodb": {"installed": mongo_ok, "version": mongo_version},
        "jsvc": {"installed": jsvc_ok, "version": jsvc_version}
    }


def get_disk_usage():
    """Return disk usage for the root partition."""
    try:
        stat = os.statvfs("/")
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free
        return {"total": total, "used": used, "free": free}
    except Exception:
        return {"total": 0, "used": 0, "free": 0}


def get_system_stats():
    """Return CPU usage, RAM usage, and uptime from /proc."""
    stats = {"cpu_percent": 0, "ram_total": 0, "ram_used": 0, "ram_percent": 0, "uptime_seconds": 0}
    # Uptime
    try:
        with open("/proc/uptime", "r") as f:
            stats["uptime_seconds"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    # RAM from /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024  # kB to bytes
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = total - available
        stats["ram_total"] = total
        stats["ram_used"] = used
        stats["ram_percent"] = round((used / total) * 100, 1) if total > 0 else 0
    except Exception:
        pass
    # CPU from /proc/stat (instant snapshot — idle vs total)
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        parts = line.split()
        if parts[0] == "cpu":
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            total = sum(vals)
            # Store first reading
            if not hasattr(get_system_stats, "_prev"):
                get_system_stats._prev = (idle, total)
                import time
                time.sleep(0.1)
                with open("/proc/stat", "r") as f:
                    line = f.readline()
                parts = line.split()
                vals = [int(x) for x in parts[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                total = sum(vals)
            prev_idle, prev_total = get_system_stats._prev
            get_system_stats._prev = (idle, total)
            diff_idle = idle - prev_idle
            diff_total = total - prev_total
            if diff_total > 0:
                stats["cpu_percent"] = round((1 - diff_idle / diff_total) * 100, 1)
    except Exception:
        pass
    return stats


def get_manager_version():
    """Return the locally installed Omada Web Manager version."""
    try:
        with open(VERSION_FILE, "r") as f:
            return f.read().strip()
    except (FileNotFoundError, IOError):
        return "0.0.0"


def get_remote_manager_version():
    """Fetch the latest Omada Web Manager version from GitHub."""
    url = f"{GITHUB_RAW_BASE}/VERSION"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OmadaWebManager"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return None


def compare_versions(local, remote):
    """Compare two semver strings. Returns True if remote > local."""
    try:
        local_parts = [int(x) for x in local.split(".")]
        remote_parts = [int(x) for x in remote.split(".")]
        return remote_parts > local_parts
    except (ValueError, AttributeError):
        return False


def perform_self_update():
    """Download updated files from GitHub and prepare for service restart."""
    tmp_dir = tempfile.mkdtemp(prefix="omada_update_")
    try:
        # Phase 1: Download all files to temp directory
        for remote_path, local_rel_path in UPDATE_FILES:
            url = f"{GITHUB_RAW_BASE}/{remote_path}"
            local_tmp = os.path.join(tmp_dir, local_rel_path)
            os.makedirs(os.path.dirname(local_tmp) or tmp_dir, exist_ok=True)

            req = urllib.request.Request(url, headers={"User-Agent": "OmadaWebManager"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(local_tmp, "wb") as f:
                f.write(data)

        # Phase 2: All downloads succeeded — copy to install dir
        for remote_path, local_rel_path in UPDATE_FILES:
            src = os.path.join(tmp_dir, local_rel_path)
            dst = os.path.join(BASE_DIR, local_rel_path)
            os.makedirs(os.path.dirname(dst) or BASE_DIR, exist_ok=True)
            shutil.copy2(src, dst)

        # Phase 3: Reinstall pip requirements
        venv_pip = os.path.join(BASE_DIR, "venv", "bin", "pip")
        req_file = os.path.join(BASE_DIR, "requirements.txt")
        if os.path.isfile(venv_pip) and os.path.isfile(req_file):
            subprocess.run(
                [venv_pip, "install", "--quiet", "-r", req_file],
                capture_output=True, timeout=120
            )

        return {"success": True, "message": "Update downloaded successfully"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_service_action(action):
    """Run a systemctl action on the Omada service."""
    if action not in ("start", "stop", "restart"):
        return {"success": False, "message": "Action non valide"}
    try:
        result = subprocess.run(
            ["systemctl", action, SERVICE_NAME],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            return {"success": True, "message": f"Service {action} OK"}
        else:
            return {"success": False, "message": result.stderr or result.stdout}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "Timeout"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def authenticate(username, password):
    """Authenticate a user against PAM (system accounts)."""
    p = pam.pam()
    return p.authenticate(username, password)


def login_required(f):
    """Decorator to require login."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated


def create_backup():
    """Create a native Omada backup to /opt/tplink/omada_db_backup.

    Replicates the logic from omadac.prerm do_backup():
      cd DATA_DIR && tar zcf <timestamped>.tar.gz db && cp to BACKUP_DIR
    """
    db_dir = os.path.join(OMADA_DATA_DIR, "db")
    if not os.path.isdir(db_dir):
        return {"success": False, "message": "Omada data/db directory not found"}

    os.makedirs(OMADA_BACKUP_DIR, exist_ok=True)
    db_filename = generate_backup_filename()

    try:
        # tar zcf omada_db_YYYYMMDD_HHMMSS.tar.gz db (from DATA_DIR)
        # --warning=no-file-changed: MongoDB writes to db/diagnostic.data
        # continuously; exit code 1 means "files changed" but archive is valid
        result = subprocess.run(
            ["tar", "zcf", db_filename, "--warning=no-file-changed", "db"],
            capture_output=True, text=True, timeout=300,
            cwd=OMADA_DATA_DIR
        )
        if result.returncode not in (0, 1):
            return {"success": False, "message": result.stderr}

        # cp -f to backup dir
        shutil.copy2(
            os.path.join(OMADA_DATA_DIR, db_filename),
            os.path.join(OMADA_BACKUP_DIR, db_filename)
        )
        os.remove(os.path.join(OMADA_DATA_DIR, db_filename))

        # Also backup cluster if it exists (same as prerm)
        cluster_hs = os.path.join(OMADA_DATA_DIR, "cluster", "hsConfig")
        cluster_ha = os.path.join(OMADA_DATA_DIR, "cluster", "haPersistentConfig")
        if os.path.exists(cluster_hs) or os.path.exists(cluster_ha):
            result2 = subprocess.run(
                ["tar", "zcf", CLUSTER_FILE_NAME,
                 "--warning=no-file-changed", "cluster"],
                capture_output=True, text=True, timeout=300,
                cwd=OMADA_DATA_DIR
            )
            if result2.returncode in (0, 1):
                shutil.copy2(
                    os.path.join(OMADA_DATA_DIR, CLUSTER_FILE_NAME),
                    os.path.join(OMADA_BACKUP_DIR, CLUSTER_FILE_NAME)
                )
                os.remove(os.path.join(OMADA_DATA_DIR, CLUSTER_FILE_NAME))

        # Purge old backups if retention is configured
        cfg = get_auto_backup_config()
        if cfg.get("max_backups", 0) > 0:
            purge_old_backups(cfg["max_backups"])

        size = os.path.getsize(os.path.join(OMADA_BACKUP_DIR, db_filename))
        return {"success": True, "name": db_filename, "size": size}
    except Exception as e:
        return {"success": False, "message": str(e)}


def list_backups():
    """List backup files in Omada native backup directory."""
    backups = []
    if not os.path.isdir(OMADA_BACKUP_DIR):
        return backups
    for f in sorted(os.listdir(OMADA_BACKUP_DIR), reverse=True):
        if f.endswith(".tar.gz"):
            fpath = os.path.join(OMADA_BACKUP_DIR, f)
            size = os.path.getsize(fpath)
            mtime = os.path.getmtime(fpath)
            backups.append({
                "name": f,
                "size": size,
                "date": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            })
    return backups


def get_auto_backup_config():
    """Read auto-backup configuration."""
    default = {"enabled": False, "interval_days": 7, "max_backups": 0}
    try:
        with open(AUTO_BACKUP_CONFIG, "r") as f:
            cfg = json.load(f)
            return {
                "enabled": bool(cfg.get("enabled", False)),
                "interval_days": int(cfg.get("interval_days", 7)),
                "max_backups": int(cfg.get("max_backups",
                                           cfg.get("retention_days", 0)))
            }
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return default


def purge_old_backups(max_backups):
    """Keep only the N most recent backup files. 0 means no limit."""
    if max_backups <= 0 or not os.path.isdir(OMADA_BACKUP_DIR):
        return
    backups = []
    for f in os.listdir(OMADA_BACKUP_DIR):
        if f.endswith(".tar.gz") and f != CLUSTER_FILE_NAME:
            fpath = os.path.join(OMADA_BACKUP_DIR, f)
            backups.append((fpath, os.path.getmtime(fpath)))
    # Sort newest first, delete everything beyond max_backups
    backups.sort(key=lambda x: x[1], reverse=True)
    for fpath, _ in backups[max_backups:]:
        os.remove(fpath)


def set_auto_backup_config(enabled, interval_days, max_backups=0):
    """Write auto-backup configuration and update crontab accordingly."""
    interval_days = max(1, min(interval_days, 90))
    max_backups = max(0, min(max_backups, 100))
    cfg = {"enabled": enabled, "interval_days": interval_days, "max_backups": max_backups}
    with open(AUTO_BACKUP_CONFIG, "w") as f:
        json.dump(cfg, f)

    # Create the backup shell script with timestamped filenames
    retention_line = ""
    if max_backups > 0:
        retention_line = f"""
# Retention: keep only the {max_backups} most recent backups
cd "{OMADA_BACKUP_DIR}"
ls -1t *.tar.gz 2>/dev/null | grep -v '^cluster\\.tar\\.gz$' | tail -n +{max_backups + 1} | while read f; do rm -f "$f"; done"""

    script_content = f"""#!/bin/bash
# Auto-backup script for Omada Web Manager
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="omada_db_${{TIMESTAMP}}.tar.gz"
cd "{OMADA_DATA_DIR}" 2>/dev/null || exit 1
mkdir -p "{OMADA_BACKUP_DIR}"
tar zcf "$BACKUP_FILE" --warning=no-file-changed db 2>/dev/null
cp -f "$BACKUP_FILE" "{OMADA_BACKUP_DIR}/$BACKUP_FILE"
rm -f "$BACKUP_FILE"{retention_line}
"""
    with open(AUTO_BACKUP_SCRIPT, "w") as f:
        f.write(script_content)
    os.chmod(AUTO_BACKUP_SCRIPT, 0o755)

    # Update crontab
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10
        )
        existing = result.stdout if result.returncode == 0 else ""

        # Remove old entry
        lines = [l for l in existing.splitlines()
                 if CRON_COMMENT not in l]

        if enabled:
            cron_line = f"0 3 */{interval_days} * * {AUTO_BACKUP_SCRIPT} # {CRON_COMMENT}"
            lines.append(cron_line)

        new_crontab = "\n".join(lines) + "\n" if lines else ""
        proc = subprocess.run(
            ["crontab", "-"], input=new_crontab,
            capture_output=True, text=True, timeout=10
        )
        if proc.returncode != 0:
            return {"success": False, "message": proc.stderr}
    except Exception as e:
        return {"success": False, "message": str(e)}

    return {"success": True, "enabled": enabled, "interval_days": interval_days}


def restore_backup(filename):
    """Restore a native Omada backup (replicates postinst import_mongo_db)."""
    safe_name = secure_filename(filename)
    archive_path = os.path.join(OMADA_BACKUP_DIR, safe_name)
    if not os.path.isfile(archive_path):
        return {"success": False, "message": "Fichier backup introuvable"}

    try:
        subprocess.run(["systemctl", "stop", SERVICE_NAME],
                       capture_output=True, timeout=60)

        # If restoring db backup (not cluster), clear existing db first
        if safe_name != CLUSTER_FILE_NAME:
            db_dir = os.path.join(OMADA_DATA_DIR, "db")
            if os.path.isdir(db_dir):
                shutil.rmtree(db_dir)

        result = subprocess.run(
            ["tar", "zxf", archive_path, "-C", OMADA_DATA_DIR],
            capture_output=True, text=True, timeout=300
        )

        # Fix ownership (same as postinst)
        subprocess.run(
            ["chown", "-RH", "omada:omada", OMADA_DATA_DIR],
            capture_output=True, timeout=30
        )

        subprocess.run(["systemctl", "start", SERVICE_NAME],
                       capture_output=True, timeout=120)

        if result.returncode == 0:
            return {"success": True, "message": "Backup restauré avec succès"}
        else:
            return {"success": False, "message": result.stderr}
    except Exception as e:
        subprocess.run(["systemctl", "start", SERVICE_NAME],
                       capture_output=True, timeout=120)
        return {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if authenticate(username, password):
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("index"))
        else:
            flash("Identifiants incorrects", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(get_service_status())


@app.route("/api/version")
@login_required
def api_version():
    v = get_omada_version()
    mv = get_manager_version()
    return jsonify({"version": v or "inconnue", "manager_version": mv})


@app.route("/api/omada-installed")
@login_required
def api_omada_installed():
    v = get_omada_version()
    return jsonify({"installed": v is not None, "version": v})


@app.route("/api/dependencies")
@login_required
def api_dependencies():
    return jsonify(check_dependencies())


@app.route("/api/service/<action>", methods=["POST"])
@login_required
def api_service(action):
    return jsonify(run_service_action(action))


@app.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "Aucun fichier envoyé"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "message": "Aucun fichier sélectionné"}), 400

    if not allowed_file(file.filename):
        return jsonify({"success": False, "message": "Seuls les fichiers .deb sont acceptés"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    return jsonify({
        "success": True,
        "message": filename,
        "filename": filename,
        "filepath": filepath
    })


@app.route("/api/uploaded-files")
@login_required
def api_uploaded_files():
    files = []
    upload_dir = app.config["UPLOAD_FOLDER"]
    if os.path.isdir(upload_dir):
        for f in sorted(os.listdir(upload_dir), reverse=True):
            if f.endswith(".deb"):
                fpath = os.path.join(upload_dir, f)
                size = os.path.getsize(fpath)
                files.append({"name": f, "size": size})
    return jsonify(files)


@app.route("/api/disk-usage")
@login_required
def api_disk_usage():
    return jsonify(get_disk_usage())


@app.route("/api/system-stats")
@login_required
def api_system_stats():
    return jsonify(get_system_stats())


@app.route("/api/uploaded-files/delete", methods=["POST"])
@login_required
def api_delete_uploaded_file():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    filename = secure_filename(request.json.get("filename", ""))
    if not filename:
        return jsonify({"success": False, "message": "Nom de fichier requis"}), 400
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    if os.path.isfile(filepath):
        os.remove(filepath)
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Fichier introuvable"}), 404


@app.route("/api/backup/create", methods=["POST"])
@login_required
def api_backup_create():
    return jsonify(create_backup())


@app.route("/api/backup/list")
@login_required
def api_backup_list():
    return jsonify(list_backups())


@app.route("/api/backup/restore", methods=["POST"])
@login_required
def api_backup_restore():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    filename = request.json.get("filename", "")
    if not filename:
        return jsonify({"success": False, "message": "Nom de fichier requis"}), 400
    return jsonify(restore_backup(filename))


@app.route("/api/backup/delete", methods=["POST"])
@login_required
def api_backup_delete():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    filename = secure_filename(request.json.get("filename", ""))
    filepath = os.path.join(OMADA_BACKUP_DIR, filename)
    if os.path.isfile(filepath):
        os.remove(filepath)
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Fichier introuvable"}), 404


@app.route("/api/backup/download/<filename>")
@login_required
def api_backup_download(filename):
    safe_name = secure_filename(filename)
    filepath = os.path.join(OMADA_BACKUP_DIR, safe_name)
    if not os.path.isfile(filepath):
        return jsonify({"success": False, "message": "Fichier introuvable"}), 404
    return send_file(filepath, as_attachment=True)


@app.route("/api/backup/auto-config")
@login_required
def api_auto_backup_config():
    return jsonify(get_auto_backup_config())


@app.route("/api/backup/auto-config", methods=["POST"])
@login_required
def api_auto_backup_set():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    enabled = request.json.get("enabled", False)
    interval_days = request.json.get("interval_days", 7)
    max_backups = request.json.get("max_backups", 0)
    try:
        interval_days = int(interval_days)
    except (TypeError, ValueError):
        interval_days = 7
    try:
        max_backups = int(max_backups)
    except (TypeError, ValueError):
        max_backups = 0
    return jsonify(set_auto_backup_config(enabled, interval_days, max_backups))


@app.route("/api/manager-version")
@login_required
def api_manager_version():
    return jsonify({"version": get_manager_version()})


@app.route("/api/manager-update-check")
@login_required
def api_manager_update_check():
    local = get_manager_version()
    remote = get_remote_manager_version()
    if remote is None:
        return jsonify({"success": False, "message": "Could not reach GitHub"})
    update_available = compare_versions(local, remote)
    return jsonify({
        "success": True,
        "local_version": local,
        "remote_version": remote,
        "update_available": update_available
    })


@app.route("/api/manager-update", methods=["POST"])
@login_required
def api_manager_update():
    result = perform_self_update()
    if result["success"]:
        def restart_service():
            import time
            time.sleep(2)
            subprocess.Popen(
                ["systemctl", "restart", MANAGER_SERVICE_NAME],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        threading.Thread(target=restart_service, daemon=True).start()
        return jsonify({
            "success": True,
            "message": "Update applied. Service restarting..."
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# WebSocket Terminal
# ---------------------------------------------------------------------------
def _start_terminal(sid, command, label=""):
    """Start a PTY process with the given command and stream output."""
    if sid in terminal_processes:
        try:
            os.kill(terminal_processes[sid]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(terminal_processes[sid]["fd"])
        except OSError:
            pass
        del terminal_processes[sid]

    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 30, 120, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()

    if pid == 0:
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["DEBIAN_FRONTEND"] = "readline"
        env["COLUMNS"] = "120"
        env["LINES"] = "30"

        os.execvpe("/bin/bash", ["/bin/bash", "-c", command], env)
    else:
        os.close(slave_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        terminal_processes[sid] = {"pid": pid, "fd": master_fd}

        if label:
            socketio.emit("terminal_output",
                          {"data": f"\033[1m=== {label} ===\033[0m\r\n\r\n"},
                          to=sid)

        socketio.start_background_task(read_terminal, sid, master_fd, pid)


@socketio.on("start_install")
def handle_start_install(data):
    """Update Omada: dpkg -r omadac (native backup prompt) then dpkg -i."""
    if not session.get("logged_in"):
        emit("terminal_output", {"data": "Non authentifié.\r\n"})
        return

    filename = data.get("filename", "")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))

    if not os.path.isfile(filepath):
        emit("terminal_output", {"data": f"Fichier introuvable: {filename}\r\n"})
        return

    sid = request.sid
    # Ensure jsvc is installed (required dependency for Omada)
    command = (
        'apt-get install -y jsvc 2>/dev/null; '
        f'dpkg -r omadac && dpkg -i "{filepath}" && '
        'systemctl daemon-reload && systemctl start tpeap'
    )
    _start_terminal(sid, command, f"Mise à jour Omada avec {filename}")


@socketio.on("start_omada_install")
def handle_start_omada_install(data):
    """Fresh install Omada (no dpkg -r needed)."""
    if not session.get("logged_in"):
        emit("terminal_output", {"data": "Non authentifié.\r\n"})
        return

    filename = data.get("filename", "")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))

    if not os.path.isfile(filepath):
        emit("terminal_output", {"data": f"Fichier introuvable: {filename}\r\n"})
        return

    sid = request.sid
    # Ensure jsvc is installed (required dependency for Omada)
    command = (
        'apt-get install -y jsvc 2>/dev/null; '
        f'dpkg -i "{filepath}" && '
        'systemctl daemon-reload && systemctl start tpeap'
    )
    _start_terminal(sid, command, f"Installation de Omada avec {filename}")


@socketio.on("start_fix_deps")
def handle_start_fix_deps(data):
    """Reinstall missing dependencies (Java, MongoDB, JSVC)."""
    if not session.get("logged_in"):
        emit("terminal_output", {"data": "Non authentifié.\r\n"})
        return

    sid = request.sid
    deps = check_dependencies()
    commands = []

    if not deps["jsvc"]["installed"]:
        commands.append('echo "=== Installing JSVC ===" && apt-get install -y jsvc')

    if not deps["java"]["installed"]:
        commands.append('echo "=== Installing Java 17 ===" && apt-get install -y openjdk-17-jre-headless')

    if not deps["mongodb"]["installed"]:
        commands.append(
            'echo "=== Installing MongoDB 7.0 ===" && '
            "curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | "
            "gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg 2>/dev/null && "
            'echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] '
            'https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" '
            "> /etc/apt/sources.list.d/mongodb-org-7.0.list && "
            "apt-get update -qq && "
            "apt-get install -y mongodb-org && "
            "systemctl start mongod && "
            "systemctl enable mongod"
        )

    if not commands:
        emit("terminal_output", {"data": "\033[32mAll dependencies are already installed.\033[0m\r\n"})
        emit("install_complete", {"exit_code": 0})
        return

    full_command = "apt-get update -qq && " + " && ".join(commands)
    _start_terminal(sid, full_command, "Reinstallation des dependances")


@socketio.on("start_uninstall_omada")
def handle_start_uninstall_omada(data):
    """Completely uninstall Omada Controller and optionally all dependencies."""
    if not session.get("logged_in"):
        emit("terminal_output", {"data": "Non authentifié.\r\n"})
        return

    remove_deps = data.get("remove_deps", False)
    sid = request.sid

    parts = []

    # Stop Omada service
    parts.append('echo "=== Arret du service Omada ===" && systemctl stop tpeap 2>/dev/null || true')

    # Remove omadac package
    parts.append('echo "=== Suppression de Omada Controller (omadac) ===" && dpkg --purge omadac 2>/dev/null || dpkg -r omadac 2>/dev/null || true')

    if remove_deps:
        # Remove MongoDB
        parts.append(
            'echo "=== Suppression de MongoDB ===" && '
            "systemctl stop mongod 2>/dev/null || true && "
            "systemctl disable mongod 2>/dev/null || true && "
            "apt-get purge -y mongodb-org mongodb-org-server mongodb-org-shell "
            "mongodb-org-mongos mongodb-org-tools mongodb-org-database "
            "mongodb-org-database-tools-extra 2>/dev/null || true && "
            "apt-get autoremove -y 2>/dev/null || true"
        )

        # Remove Java
        parts.append(
            'echo "=== Suppression de Java ===" && '
            "apt-get purge -y openjdk-17-jre-headless 2>/dev/null || true && "
            "apt-get autoremove -y 2>/dev/null || true"
        )

        # Remove JSVC
        parts.append(
            'echo "=== Suppression de JSVC ===" && '
            "apt-get purge -y jsvc 2>/dev/null || true && "
            "apt-get autoremove -y 2>/dev/null || true"
        )

    parts.append('echo "=== Desinstallation terminee ==="')

    full_command = " && ".join(parts)
    _start_terminal(sid, full_command, "Desinstallation de Omada Controller")


def read_terminal(sid, fd, pid):
    """Background task to read from the PTY and emit to the client."""
    output_buffer = ""
    try:
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(fd, 4096)
                        if not data:
                            break
                        decoded = data.decode("utf-8", errors="replace")
                        output_buffer += decoded
                        socketio.emit("terminal_output", {"data": decoded}, to=sid)
                    except OSError:
                        break
                else:
                    result = os.waitpid(pid, os.WNOHANG)
                    if result[0] != 0:
                        try:
                            while True:
                                data = os.read(fd, 4096)
                                if not data:
                                    break
                                decoded = data.decode("utf-8", errors="replace")
                                output_buffer += decoded
                                socketio.emit("terminal_output", {"data": decoded}, to=sid)
                        except OSError:
                            pass

                        exit_code = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1
                        success = (exit_code == 0 or "Started successfully" in output_buffer)

                        if success:
                            socketio.emit("terminal_output",
                                          {"data": "\r\n\033[32m=== Installation terminée avec succès ===\033[0m\r\n"},
                                          to=sid)
                            socketio.emit("install_complete", {"exit_code": 0}, to=sid)
                        else:
                            socketio.emit("terminal_output",
                                          {"data": f"\r\n\033[31m=== Terminé avec erreurs (code: {exit_code}) ===\033[0m\r\n"},
                                          to=sid)
                            socketio.emit("install_complete", {"exit_code": exit_code}, to=sid)
                        break
            except (ValueError, OSError):
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if sid in terminal_processes:
            del terminal_processes[sid]


@socketio.on("terminal_input")
def handle_terminal_input(data):
    if not session.get("logged_in"):
        return
    sid = request.sid
    if sid in terminal_processes:
        try:
            os.write(terminal_processes[sid]["fd"], data["data"].encode("utf-8"))
        except OSError:
            pass


@socketio.on("terminal_resize")
def handle_terminal_resize(data):
    if not session.get("logged_in"):
        return
    sid = request.sid
    if sid in terminal_processes:
        rows = data.get("rows", 30)
        cols = data.get("cols", 120)
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(terminal_processes[sid]["fd"], termios.TIOCSWINSZ, winsize)
        except OSError:
            pass


@socketio.on("disconnect")
def handle_disconnect():
    sid = request.sid
    if sid in terminal_processes:
        try:
            os.kill(terminal_processes[sid]["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(terminal_processes[sid]["fd"])
        except OSError:
            pass
        del terminal_processes[sid]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    port = read_port()
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
