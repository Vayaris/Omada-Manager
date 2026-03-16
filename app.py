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
import uuid as _uuid_mod
from datetime import datetime

import paramiko

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

# SSL / HTTPS
SSL_DIR = os.path.join(BASE_DIR, "ssl")
SSL_CERT = os.path.join(SSL_DIR, "cert.pem")
SSL_KEY = os.path.join(SSL_DIR, "key.pem")

# Auto-backup configuration
AUTO_BACKUP_CONFIG = os.path.join(BASE_DIR, "auto_backup.json")
AUTO_BACKUP_SCRIPT = os.path.join(BASE_DIR, "auto_backup.sh")
CRON_COMMENT = "omada-web-manager-auto-backup"

# Remote backup configuration
REMOTE_BACKUP_CONFIG = os.path.join(BASE_DIR, "remote_backup.json")


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
        response = {"success": True, "name": db_filename, "size": size}

        # Send to remote storage if configured
        remote_cfg = _get_remote_backup_config_raw()
        if remote_cfg.get("enabled"):
            remote_result = send_backup_remote(
                os.path.join(OMADA_BACKUP_DIR, db_filename)
            )
            response["remote"] = remote_result

        return response
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

    # Build remote upload commands if remote backup is configured
    remote_line = ""
    try:
        remote_cfg = _get_remote_backup_config_raw()
        if remote_cfg.get("enabled"):
            method = remote_cfg.get("method", "")
            if method == "scp":
                s = remote_cfg.get("scp", {})
                host, port = s.get("host", ""), str(s.get("port", 22))
                user, pwd = s.get("user", ""), s.get("password", "")
                key_path = s.get("key_path", "")
                rpath = s.get("remote_path", "/backups/omada")
                if host and user:
                    if key_path:
                        remote_line = f'\n# Remote backup via SCP\nscp -o StrictHostKeyChecking=no -o ConnectTimeout=10 -P {port} -i "{key_path}" "{OMADA_BACKUP_DIR}/$BACKUP_FILE" "{user}@{host}:{rpath}/" 2>/dev/null || echo "[REMOTE] SCP send failed"'
                    elif pwd:
                        remote_line = f'\n# Remote backup via SCP\nsshpass -p \'{pwd}\' scp -o StrictHostKeyChecking=no -o ConnectTimeout=10 -P {port} "{OMADA_BACKUP_DIR}/$BACKUP_FILE" "{user}@{host}:{rpath}/" 2>/dev/null || echo "[REMOTE] SCP send failed"'
            elif method == "smb":
                s = remote_cfg.get("smb", {})
                share, user = s.get("share", ""), s.get("user", "")
                pwd, domain = s.get("password", ""), s.get("domain", "")
                rpath = s.get("remote_path", "/").strip("/")
                if share and user:
                    auth = f"{domain}/{user}%{pwd}" if domain else f"{user}%{pwd}"
                    smb_cmd = f"cd {rpath}; " if rpath else ""
                    smb_cmd += 'put "{OMADA_BACKUP_DIR}/$BACKUP_FILE"'
                    remote_line = f'\n# Remote backup via SMB\nsmbclient "{share}" -U "{auth}" -c "{smb_cmd}" 2>/dev/null || echo "[REMOTE] SMB send failed"'
            elif method == "nfs":
                s = remote_cfg.get("nfs", {})
                nfs_share = s.get("share", "")
                mount_opts = s.get("mount_options", "")
                if nfs_share:
                    mount_cmd = f'mount -t nfs -o {mount_opts} "{nfs_share}"' if mount_opts else f'mount -t nfs "{nfs_share}"'
                    remote_line = f'\n# Remote backup via NFS\nmkdir -p /tmp/omada_nfs_mount\n{mount_cmd} /tmp/omada_nfs_mount 2>/dev/null && cp -f "{OMADA_BACKUP_DIR}/$BACKUP_FILE" /tmp/omada_nfs_mount/ && umount /tmp/omada_nfs_mount || echo "[REMOTE] NFS send failed"'
            elif method == "s3":
                s = remote_cfg.get("s3", {})
                endpoint = s.get("endpoint", "")
                bucket, ak = s.get("bucket", ""), s.get("access_key", "")
                sk, prefix = s.get("secret_key", ""), s.get("prefix", "omada-backups/").strip("/")
                if bucket and ak and sk:
                    s3_path = f"s3://{bucket}/{prefix}/$BACKUP_FILE" if prefix else f"s3://{bucket}/$BACKUP_FILE"
                    ep_flag = f' --endpoint-url "{endpoint}"' if endpoint else ""
                    remote_line = f'\n# Remote backup via S3\nAWS_ACCESS_KEY_ID="{ak}" AWS_SECRET_ACCESS_KEY="{sk}" aws s3 cp "{OMADA_BACKUP_DIR}/$BACKUP_FILE" "{s3_path}"{ep_flag} 2>/dev/null || echo "[REMOTE] S3 send failed"'
    except Exception:
        pass  # If remote config can't be read, just skip remote upload in the script

    script_content = f"""#!/bin/bash
# Auto-backup script for Omada Web Manager
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="omada_db_${{TIMESTAMP}}.tar.gz"
cd "{OMADA_DATA_DIR}" 2>/dev/null || exit 1
mkdir -p "{OMADA_BACKUP_DIR}"
tar zcf "$BACKUP_FILE" --warning=no-file-changed db 2>/dev/null
cp -f "$BACKUP_FILE" "{OMADA_BACKUP_DIR}/$BACKUP_FILE"
rm -f "$BACKUP_FILE"{retention_line}{remote_line}
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


# ---------------------------------------------------------------------------
# SSL API
# ---------------------------------------------------------------------------
@app.route("/api/ssl-info")
@login_required
def api_ssl_info():
    return jsonify(get_ssl_info())


@app.route("/api/ssl/regenerate", methods=["POST"])
@login_required
def api_ssl_regenerate():
    try:
        # Remove existing certificates
        for f in (SSL_CERT, SSL_KEY):
            if os.path.isfile(f):
                os.remove(f)
        ensure_ssl_certificate()
        # Schedule a service restart so the new cert is loaded
        def restart_service():
            import time
            time.sleep(2)
            subprocess.Popen(
                ["systemctl", "restart", MANAGER_SERVICE_NAME],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        threading.Thread(target=restart_service, daemon=True).start()
        return jsonify({"success": True, "message": "Certificate regenerated. Service restarting..."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ---------------------------------------------------------------------------
# Remote Backup API
# ---------------------------------------------------------------------------
@app.route("/api/backup/remote-config")
@login_required
def api_remote_backup_config_get():
    cfg = get_remote_backup_config()
    return jsonify(mask_passwords(cfg))


@app.route("/api/backup/remote-config", methods=["POST"])
@login_required
def api_remote_backup_config_set():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    cfg = request.json
    try:
        save_remote_backup_config(cfg)
        # Regenerate auto_backup.sh if auto-backup is enabled
        auto_cfg = get_auto_backup_config()
        if auto_cfg.get("enabled"):
            set_auto_backup_config(
                auto_cfg["enabled"],
                auto_cfg["interval_days"],
                auto_cfg.get("max_backups", 0)
            )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/backup/remote-test", methods=["POST"])
@login_required
def api_remote_backup_test():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    cfg = request.json
    # Resolve masked passwords from saved config
    saved = _get_remote_backup_config_raw()
    method = cfg.get("method", "scp")
    if method in cfg and isinstance(cfg[method], dict):
        for field in ("password", "secret_key"):
            if cfg[method].get(field) == "***" and method in saved:
                cfg[method][field] = saved[method].get(field, "")
    return jsonify(test_remote_connection(cfg))


@app.route("/api/backup/remote-send", methods=["POST"])
@login_required
def api_remote_backup_send():
    if not request.is_json:
        return jsonify({"success": False, "message": "JSON requis"}), 400
    filename = secure_filename(request.json.get("filename", ""))
    if not filename:
        return jsonify({"success": False, "message": "Nom de fichier requis"}), 400
    filepath = os.path.join(OMADA_BACKUP_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"success": False, "message": "Fichier introuvable"}), 404
    return jsonify(send_backup_remote(filepath))


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
# SSL / HTTPS helpers
# ---------------------------------------------------------------------------
def ensure_ssl_certificate():
    """Generate a self-signed SSL certificate if it does not exist yet."""
    os.makedirs(SSL_DIR, exist_ok=True)
    if os.path.isfile(SSL_CERT) and os.path.isfile(SSL_KEY):
        return (SSL_CERT, SSL_KEY)
    print("[SSL] Generating self-signed certificate …")
    result = subprocess.run(
        [
            "openssl", "req", "-x509",
            "-newkey", "rsa:2048",
            "-keyout", SSL_KEY,
            "-out", SSL_CERT,
            "-days", "3650",
            "-nodes",
            "-subj", "/CN=Omada Web Manager",
        ],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"[SSL] openssl error: {result.stderr}")
        raise RuntimeError("Failed to generate SSL certificate")
    # Restrict permissions
    os.chmod(SSL_KEY, 0o600)
    os.chmod(SSL_CERT, 0o644)
    print("[SSL] Certificate generated successfully.")
    return (SSL_CERT, SSL_KEY)


def get_ssl_info():
    """Return basic information about the current SSL certificate."""
    if not os.path.isfile(SSL_CERT):
        return {"exists": False}
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", SSL_CERT, "-noout",
             "-subject", "-enddate", "-startdate"],
            capture_output=True, text=True, timeout=10
        )
        info = {"exists": True, "self_signed": True}
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("subject="):
                info["subject"] = line.split("=", 1)[1].strip()
            elif line.startswith("notAfter="):
                info["expires"] = line.split("=", 1)[1].strip()
            elif line.startswith("notBefore="):
                info["issued"] = line.split("=", 1)[1].strip()
        return info
    except Exception:
        return {"exists": True, "error": "Cannot read certificate"}


def run_http_redirect(https_port):
    """Run a tiny HTTP server on port 80 that redirects everything to HTTPS."""
    from flask import Flask as _Flask, redirect as _redirect, request as _request

    redirect_app = _Flask("http_redirect")

    @redirect_app.route("/", defaults={"path": ""})
    @redirect_app.route("/<path:path>")
    def _redirect_to_https(path):
        host = _request.host.split(":")[0]
        target = f"https://{host}:{https_port}/{path}"
        if _request.query_string:
            target += f"?{_request.query_string.decode()}"
        return _redirect(target, code=301)

    try:
        redirect_app.run(host="0.0.0.0", port=80, threaded=True)
    except OSError as e:
        print(f"[HTTP→HTTPS] Could not start redirect server on port 80: {e}")
    except Exception as e:
        print(f"[HTTP→HTTPS] Redirect server error: {e}")


# ---------------------------------------------------------------------------
# Remote backup helpers
# ---------------------------------------------------------------------------
def get_remote_backup_config():
    """Read remote backup configuration (passwords masked for API output)."""
    default = {
        "enabled": False, "method": "scp",
        "scp": {"host": "", "port": 22, "user": "", "password": "",
                "key_path": "", "remote_path": "/backups/omada"},
        "smb": {"share": "", "user": "", "password": "",
                "domain": "", "remote_path": "/omada"},
        "nfs": {"share": "", "mount_options": ""},
        "s3":  {"endpoint": "", "bucket": "", "access_key": "",
                "secret_key": "", "prefix": "omada-backups/"},
    }
    try:
        with open(REMOTE_BACKUP_CONFIG, "r") as f:
            cfg = json.load(f)
        # Merge with defaults to ensure all keys exist
        for key in default:
            if isinstance(default[key], dict):
                if key not in cfg:
                    cfg[key] = default[key]
                else:
                    for k2 in default[key]:
                        if k2 not in cfg[key]:
                            cfg[key][k2] = default[key][k2]
            elif key not in cfg:
                cfg[key] = default[key]
        return cfg
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return default


def _get_remote_backup_config_raw():
    """Read remote backup configuration with real passwords (internal use)."""
    return get_remote_backup_config()


def mask_passwords(cfg):
    """Return a copy of config with password fields masked."""
    import copy
    masked = copy.deepcopy(cfg)
    for section in ("scp", "smb", "s3"):
        if section in masked and isinstance(masked[section], dict):
            for field in ("password", "secret_key"):
                if field in masked[section] and masked[section][field]:
                    masked[section][field] = "***"
    return masked


def save_remote_backup_config(cfg):
    """Write remote backup configuration to disk."""
    # If passwords are masked (***), keep the old real passwords
    try:
        old_cfg = _get_remote_backup_config_raw()
    except Exception:
        old_cfg = {}
    for section in ("scp", "smb", "s3"):
        if section in cfg and isinstance(cfg[section], dict):
            for field in ("password", "secret_key"):
                if cfg[section].get(field) == "***" and section in old_cfg:
                    cfg[section][field] = old_cfg[section].get(field, "")
    with open(REMOTE_BACKUP_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(REMOTE_BACKUP_CONFIG, 0o600)


def send_backup_remote(filepath):
    """Send a backup file to the configured remote storage."""
    cfg = _get_remote_backup_config_raw()
    if not cfg.get("enabled"):
        return {"success": False, "message": "Remote backup not enabled"}
    method = cfg.get("method", "scp")
    dispatch = {
        "scp": _send_via_scp,
        "smb": _send_via_smb,
        "nfs": _send_via_nfs,
        "s3":  _send_via_s3,
    }
    fn = dispatch.get(method)
    if not fn:
        return {"success": False, "message": f"Unknown method: {method}"}
    try:
        return fn(filepath, cfg.get(method, {}))
    except Exception as e:
        return {"success": False, "message": str(e)}


def _send_via_scp(filepath, cfg):
    """Send file via SCP/SFTP."""
    host = cfg.get("host", "")
    port = str(cfg.get("port", 22))
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    key_path = cfg.get("key_path", "")
    remote_path = cfg.get("remote_path", "/backups/omada")

    if not host or not user:
        return {"success": False, "message": "SCP: host and user are required"}

    filename = os.path.basename(filepath)
    destination = f"{user}@{host}:{remote_path}/{filename}"

    if key_path and os.path.isfile(key_path):
        cmd = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
               "-P", port, "-i", key_path, filepath, destination]
    elif password:
        if not shutil.which("sshpass"):
            return {"success": False,
                    "message": "SCP: 'sshpass' is not installed. Install it with: apt install sshpass"}
        cmd = ["sshpass", "-p", password,
               "scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
               "-P", port, filepath, destination]
    else:
        return {"success": False, "message": "SCP: password or SSH key required"}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        return {"success": True, "message": f"Sent via SCP to {host}:{remote_path}"}
    return {"success": False, "message": f"SCP error: {result.stderr.strip()}"}


def _send_via_smb(filepath, cfg):
    """Send file via SMB/CIFS using smbclient."""
    if not shutil.which("smbclient"):
        return {"success": False,
                "message": "SMB: 'smbclient' is not installed. Install it with: apt install smbclient"}
    share = cfg.get("share", "")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    domain = cfg.get("domain", "")
    remote_path = cfg.get("remote_path", "/").strip("/")

    if not share or not user:
        return {"success": False, "message": "SMB: share and user are required"}

    filename = os.path.basename(filepath)
    auth = f"{user}%{password}"
    if domain:
        auth = f"{domain}/{auth}"

    # Build smbclient command
    smb_commands = ""
    if remote_path:
        smb_commands += f"cd {remote_path}; "
    smb_commands += f"put {filepath} {filename}"

    cmd = ["smbclient", share, "-U", auth, "-c", smb_commands]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0:
        return {"success": True, "message": f"Sent via SMB to {share}"}
    return {"success": False, "message": f"SMB error: {result.stderr.strip()}"}


def _send_via_nfs(filepath, cfg):
    """Send file via NFS (temporary mount)."""
    share = cfg.get("share", "")
    mount_options = cfg.get("mount_options", "")

    if not share:
        return {"success": False, "message": "NFS: share is required (e.g. server:/path)"}

    mount_point = "/tmp/omada_nfs_mount"
    os.makedirs(mount_point, exist_ok=True)

    try:
        # Mount
        mount_cmd = ["mount", "-t", "nfs"]
        if mount_options:
            mount_cmd += ["-o", mount_options]
        mount_cmd += [share, mount_point]

        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"success": False, "message": f"NFS mount error: {result.stderr.strip()}"}

        # Copy
        filename = os.path.basename(filepath)
        shutil.copy2(filepath, os.path.join(mount_point, filename))

        return {"success": True, "message": f"Sent via NFS to {share}"}
    except Exception as e:
        return {"success": False, "message": f"NFS error: {str(e)}"}
    finally:
        # Unmount
        subprocess.run(["umount", mount_point], capture_output=True, timeout=15)


def _send_via_s3(filepath, cfg):
    """Send file via S3-compatible storage (aws cli)."""
    if not shutil.which("aws"):
        return {"success": False,
                "message": "S3: 'aws' CLI is not installed. Install it with: apt install awscli"}
    endpoint = cfg.get("endpoint", "")
    bucket = cfg.get("bucket", "")
    access_key = cfg.get("access_key", "")
    secret_key = cfg.get("secret_key", "")
    prefix = cfg.get("prefix", "omada-backups/").strip("/")

    if not bucket or not access_key or not secret_key:
        return {"success": False, "message": "S3: bucket, access_key, and secret_key are required"}

    filename = os.path.basename(filepath)
    s3_path = f"s3://{bucket}/{prefix}/{filename}" if prefix else f"s3://{bucket}/{filename}"

    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = access_key
    env["AWS_SECRET_ACCESS_KEY"] = secret_key

    cmd = ["aws", "s3", "cp", filepath, s3_path]
    if endpoint:
        cmd += ["--endpoint-url", endpoint]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    if result.returncode == 0:
        return {"success": True, "message": f"Sent via S3 to {bucket}"}
    return {"success": False, "message": f"S3 error: {result.stderr.strip()}"}


def test_remote_connection(cfg):
    """Test the remote connection without sending a file."""
    method = cfg.get("method", "scp")
    if method == "scp":
        return _test_scp(cfg.get("scp", {}))
    elif method == "smb":
        return _test_smb(cfg.get("smb", {}))
    elif method == "nfs":
        return _test_nfs(cfg.get("nfs", {}))
    elif method == "s3":
        return _test_s3(cfg.get("s3", {}))
    return {"success": False, "message": f"Unknown method: {method}"}


def _test_scp(cfg):
    host = cfg.get("host", "")
    port = str(cfg.get("port", 22))
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    key_path = cfg.get("key_path", "")

    if not host or not user:
        return {"success": False, "message": "Host and user are required"}

    if key_path and os.path.isfile(key_path):
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
               "-p", port, "-i", key_path, f"{user}@{host}", "echo ok"]
    elif password:
        if not shutil.which("sshpass"):
            return {"success": False, "message": "'sshpass' is not installed"}
        cmd = ["sshpass", "-p", password,
               "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
               "-p", port, f"{user}@{host}", "echo ok"]
    else:
        return {"success": False, "message": "Password or SSH key required"}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0 and "ok" in result.stdout:
        return {"success": True, "message": f"SSH connection to {host} successful"}
    return {"success": False, "message": f"SSH error: {result.stderr.strip() or 'Connection failed'}"}


def _test_smb(cfg):
    if not shutil.which("smbclient"):
        return {"success": False, "message": "'smbclient' is not installed"}
    share = cfg.get("share", "")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    domain = cfg.get("domain", "")

    if not share or not user:
        return {"success": False, "message": "Share and user are required"}

    auth = f"{user}%{password}"
    if domain:
        auth = f"{domain}/{auth}"

    cmd = ["smbclient", share, "-U", auth, "-c", "ls"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode == 0:
        return {"success": True, "message": f"SMB connection to {share} successful"}
    return {"success": False, "message": f"SMB error: {result.stderr.strip()}"}


def _test_nfs(cfg):
    share = cfg.get("share", "")
    mount_options = cfg.get("mount_options", "")

    if not share:
        return {"success": False, "message": "Share is required (e.g. server:/path)"}

    mount_point = "/tmp/omada_nfs_test"
    os.makedirs(mount_point, exist_ok=True)

    mount_cmd = ["mount", "-t", "nfs"]
    if mount_options:
        mount_cmd += ["-o", mount_options]
    mount_cmd += [share, mount_point]

    try:
        result = subprocess.run(mount_cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
            return {"success": True, "message": f"NFS mount of {share} successful"}
        return {"success": False, "message": f"NFS error: {result.stderr.strip()}"}
    except Exception as e:
        subprocess.run(["umount", mount_point], capture_output=True, timeout=10)
        return {"success": False, "message": f"NFS error: {str(e)}"}


def _test_s3(cfg):
    if not shutil.which("aws"):
        return {"success": False, "message": "'aws' CLI is not installed"}
    endpoint = cfg.get("endpoint", "")
    bucket = cfg.get("bucket", "")
    access_key = cfg.get("access_key", "")
    secret_key = cfg.get("secret_key", "")

    if not bucket or not access_key or not secret_key:
        return {"success": False, "message": "Bucket, access_key, and secret_key are required"}

    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = access_key
    env["AWS_SECRET_ACCESS_KEY"] = secret_key

    cmd = ["aws", "s3", "ls", f"s3://{bucket}"]
    if endpoint:
        cmd += ["--endpoint-url", endpoint]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if result.returncode == 0:
        return {"success": True, "message": f"S3 connection to {bucket} successful"}
    return {"success": False, "message": f"S3 error: {result.stderr.strip()}"}


# ---------------------------------------------------------------------------
# Remote SSH Machines
# ---------------------------------------------------------------------------

SSH_CONFIG_FILE = os.path.join(BASE_DIR, "remote_ssh.json")
_remote_jobs: dict = {}
_remote_jobs_lock = threading.Lock()


def _load_ssh_machines() -> list:
    try:
        with open(SSH_CONFIG_FILE, "r") as f:
            return json.load(f).get("machines", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_ssh_machines(machines: list):
    with open(SSH_CONFIG_FILE, "w") as f:
        json.dump({"machines": machines}, f, indent=2)
    os.chmod(SSH_CONFIG_FILE, 0o600)


def _mask_machine(m: dict) -> dict:
    mc = dict(m)
    if mc.get("password"):
        mc["password"] = "***"
    return mc


def _ssh_connect(machine: dict) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(
        hostname=machine["host"],
        port=int(machine.get("port", 22)),
        username=machine["username"],
        timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    if machine.get("auth") == "key" and machine.get("key_path"):
        kw["key_filename"] = machine["key_path"]
    else:
        kw["password"] = machine.get("password", "")
    client.connect(**kw)
    return client


def _ssh_exec(machine: dict, command: str, timeout: int = 30):
    """Run a command via SSH. Returns (stdout, stderr, exit_code)."""
    client = _ssh_connect(machine)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        exit_code = stdout.channel.recv_exit_status()
        return (
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
            exit_code,
        )
    finally:
        client.close()


def _remote_add_log(job_id: str, line: str):
    with _remote_jobs_lock:
        if job_id in _remote_jobs:
            _remote_jobs[job_id]["logs"].append(line)


def _get_remote_job(job_id: str) -> dict:
    with _remote_jobs_lock:
        j = _remote_jobs.get(job_id)
        if not j:
            return {"running": False, "logs": [], "success": None}
        return {"running": j["running"], "logs": list(j["logs"]), "success": j["success"]}


def _remote_finish_job(job_id: str, success: bool):
    with _remote_jobs_lock:
        if job_id in _remote_jobs:
            _remote_jobs[job_id]["running"] = False
            _remote_jobs[job_id]["success"] = success


def _stream_remote_cmd(machine: dict, job_id: str, command: str, timeout: int = 600):
    """Run command on remote via PTY and stream output line by line to job log."""
    client = _ssh_connect(machine)
    try:
        _, stdout, _ = client.exec_command(command, timeout=timeout, get_pty=True)
        for raw_line in iter(stdout.readline, ""):
            stripped = raw_line.rstrip("\r\n")
            if stripped:
                _remote_add_log(job_id, stripped)
        return stdout.channel.recv_exit_status()
    finally:
        client.close()


def _run_remote_install(machine: dict, job_id: str):
    try:
        _remote_add_log(job_id, "→ Connecting to remote machine…")
        _ssh_exec(machine, "echo ok", timeout=15)
        _remote_add_log(job_id, "→ Downloading and running install script (may take several minutes)…")
        install_cmd = (
            "curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/"
            "install_omada_manager.sh | sudo bash 2>&1"
        )
        exit_code = _stream_remote_cmd(machine, job_id, install_cmd, timeout=900)
        if exit_code != 0:
            raise RuntimeError(f"Install script exited with code {exit_code}")
        _remote_add_log(job_id, "✓ Omada Controller installed successfully.")
        _remote_finish_job(job_id, True)
    except Exception as exc:
        _remote_add_log(job_id, f"✗ Error: {exc}")
        _remote_finish_job(job_id, False)


def _run_remote_uninstall(machine: dict, job_id: str):
    try:
        _remote_add_log(job_id, "→ Stopping Omada service…")
        _ssh_exec(machine, "sudo systemctl stop tpeap 2>/dev/null || true", timeout=30)
        _remote_add_log(job_id, "→ Removing omadac package…")
        out, err, _ = _ssh_exec(machine, "sudo apt-get remove -y omadac 2>&1", timeout=120)
        for line in (out + err).splitlines():
            if line.strip():
                _remote_add_log(job_id, line)
        _remote_add_log(job_id, "→ Cleaning residual files…")
        _ssh_exec(machine, "sudo rm -rf /opt/tplink 2>/dev/null || true", timeout=30)
        _remote_add_log(job_id, "✓ Omada Controller uninstalled successfully.")
        _remote_finish_job(job_id, True)
    except Exception as exc:
        _remote_add_log(job_id, f"✗ Error: {exc}")
        _remote_finish_job(job_id, False)


def _run_remote_update(machine: dict, job_id: str):
    """Re-run the install script — idempotent upgrade."""
    _run_remote_install(machine, job_id)


def _get_machine_or_404(mid: str):
    return next((m for m in _load_ssh_machines() if m["id"] == mid), None)


@app.route("/api/remote-ssh", methods=["GET"])
@login_required
def api_remote_ssh_list():
    return jsonify([_mask_machine(m) for m in _load_ssh_machines()])


@app.route("/api/remote-ssh", methods=["POST"])
@login_required
def api_remote_ssh_add():
    data = request.json or {}
    for field in ("label", "host", "username"):
        if not data.get(field):
            return jsonify({"success": False, "message": f"Field '{field}' is required"}), 400
    machine = {
        "id": str(_uuid_mod.uuid4()),
        "label": data["label"].strip(),
        "host": data["host"].strip(),
        "port": int(data.get("port", 22)),
        "username": data["username"].strip(),
        "auth": data.get("auth", "password"),
        "password": data.get("password", ""),
        "key_path": data.get("key_path", ""),
    }
    machines = _load_ssh_machines()
    machines.append(machine)
    _save_ssh_machines(machines)
    return jsonify({"success": True, "machine": _mask_machine(machine)})


@app.route("/api/remote-ssh/<mid>", methods=["PUT"])
@login_required
def api_remote_ssh_update_machine(mid):
    data = request.json or {}
    machines = _load_ssh_machines()
    machine = next((m for m in machines if m["id"] == mid), None)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    for field in ("label", "host", "username", "auth", "key_path"):
        if field in data:
            machine[field] = str(data[field]).strip()
    if "port" in data:
        machine["port"] = int(data["port"])
    if "password" in data and data["password"] != "***":
        machine["password"] = data["password"]
    _save_ssh_machines(machines)
    return jsonify({"success": True, "machine": _mask_machine(machine)})


@app.route("/api/remote-ssh/<mid>", methods=["DELETE"])
@login_required
def api_remote_ssh_delete(mid):
    machines = _load_ssh_machines()
    new_list = [m for m in machines if m["id"] != mid]
    if len(new_list) == len(machines):
        return jsonify({"success": False, "message": "Machine not found"}), 404
    _save_ssh_machines(new_list)
    return jsonify({"success": True})


@app.route("/api/remote-ssh/<mid>/test", methods=["POST"])
@login_required
def api_remote_ssh_test(mid):
    data = request.json or {}
    if mid == "new":
        machine = data
    else:
        machine = _get_machine_or_404(mid)
        if not machine:
            return jsonify({"success": False, "message": "Machine not found"}), 404
        if data.get("password") and data["password"] != "***":
            machine = dict(machine)
            machine["password"] = data["password"]
    try:
        _ssh_exec(machine, "echo ok", timeout=15)
        return jsonify({"success": True, "message": "Connection successful"})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/remote-ssh/<mid>/status", methods=["GET"])
@login_required
def api_remote_ssh_status(mid):
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    try:
        out, _, _ = _ssh_exec(machine, "systemctl is-active tpeap 2>/dev/null; true", timeout=10)
        service_status = out.strip()
        props = "/opt/tplink/EAPController/properties/omada.properties"
        out2, _, _ = _ssh_exec(machine, f"test -f '{props}' && echo yes || echo no", timeout=10)
        omada_installed = out2.strip() == "yes"
        omada_version = None
        if omada_installed:
            out3, _, _ = _ssh_exec(
                machine,
                f"grep '^app.version=' '{props}' 2>/dev/null | cut -d= -f2",
                timeout=10,
            )
            omada_version = out3.strip() or None
        return jsonify({
            "success": True, "ssh_ok": True,
            "service_status": service_status,
            "omada_installed": omada_installed,
            "omada_version": omada_version,
        })
    except Exception as exc:
        return jsonify({
            "success": True, "ssh_ok": False, "error": str(exc),
            "service_status": None, "omada_installed": False, "omada_version": None,
        })


@app.route("/api/remote-ssh/<mid>/service/<action>", methods=["POST"])
@login_required
def api_remote_ssh_service(mid, action):
    if action not in ("start", "stop", "restart"):
        return jsonify({"success": False, "message": "Invalid action"}), 400
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    try:
        out, err, code = _ssh_exec(machine, f"sudo systemctl {action} tpeap 2>&1", timeout=30)
        if code == 0:
            return jsonify({"success": True, "message": f"Service {action} successful"})
        return jsonify({"success": False, "message": (err + out).strip()})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


@app.route("/api/remote-ssh/<mid>/logs", methods=["GET"])
@login_required
def api_remote_ssh_logs(mid):
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    try:
        n = int(request.args.get("lines", 80))
        out, err, _ = _ssh_exec(
            machine,
            f"sudo journalctl -u tpeap -n {n} --no-pager --output=short 2>&1",
            timeout=20,
        )
        return jsonify({"success": True, "lines": (out + err).splitlines()})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)})


def _start_remote_job(mid: str, op: str, target_fn, machine: dict):
    job_id = f"{mid}:{op}"
    with _remote_jobs_lock:
        if _remote_jobs.get(job_id, {}).get("running"):
            return False
        _remote_jobs[job_id] = {"running": True, "logs": [], "success": None}
    threading.Thread(target=target_fn, args=(machine, job_id), daemon=True).start()
    return True


def _log_response(mid: str, op: str):
    job_id = f"{mid}:{op}"
    offset = int(request.args.get("offset", 0))
    job = _get_remote_job(job_id)
    logs = job.get("logs", [])
    return jsonify({
        "lines": logs[offset:], "total": len(logs),
        "running": job.get("running", False), "success": job.get("success"),
    })


@app.route("/api/remote-ssh/<mid>/install", methods=["POST"])
@login_required
def api_remote_ssh_install(mid):
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    started = _start_remote_job(mid, "install", _run_remote_install, machine)
    return jsonify({"started": started})


@app.route("/api/remote-ssh/<mid>/install/log", methods=["GET"])
@login_required
def api_remote_ssh_install_log(mid):
    return _log_response(mid, "install")


@app.route("/api/remote-ssh/<mid>/uninstall", methods=["POST"])
@login_required
def api_remote_ssh_uninstall(mid):
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    started = _start_remote_job(mid, "uninstall", _run_remote_uninstall, machine)
    return jsonify({"started": started})


@app.route("/api/remote-ssh/<mid>/uninstall/log", methods=["GET"])
@login_required
def api_remote_ssh_uninstall_log(mid):
    return _log_response(mid, "uninstall")


@app.route("/api/remote-ssh/<mid>/update", methods=["POST"])
@login_required
def api_remote_ssh_update_omada(mid):
    machine = _get_machine_or_404(mid)
    if not machine:
        return jsonify({"success": False, "message": "Machine not found"}), 404
    started = _start_remote_job(mid, "update", _run_remote_update, machine)
    return jsonify({"started": started})


@app.route("/api/remote-ssh/<mid>/update/log", methods=["GET"])
@login_required
def api_remote_ssh_update_log(mid):
    return _log_response(mid, "update")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    port = read_port()

    # Generate SSL certificate if needed
    cert, key = ensure_ssl_certificate()

    # Start HTTP→HTTPS redirect server on port 80 (background)
    threading.Thread(target=run_http_redirect, args=(port,), daemon=True).start()

    # Start main HTTPS server
    socketio.run(app, host="0.0.0.0", port=port, debug=False,
                 allow_unsafe_werkzeug=True,
                 ssl_context=(cert, key))
