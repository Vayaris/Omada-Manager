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
import secrets
from datetime import datetime

from flask import (
    Flask, render_template, request, session, redirect,
    url_for, jsonify, flash
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
    """Check if Java 17+ and MongoDB are installed."""
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

    return {
        "java": {"installed": java_ok, "version": java_version},
        "mongodb": {"installed": mongo_ok, "version": mongo_version}
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
      cd DATA_DIR && tar zcf omada.db.tar.gz db && cp to BACKUP_DIR
    """
    db_dir = os.path.join(OMADA_DATA_DIR, "db")
    if not os.path.isdir(db_dir):
        return {"success": False, "message": "Omada data/db directory not found"}

    os.makedirs(OMADA_BACKUP_DIR, exist_ok=True)

    try:
        # tar zcf omada.db.tar.gz db (from DATA_DIR)
        result = subprocess.run(
            ["tar", "zcf", DB_FILE_NAME, "db"],
            capture_output=True, text=True, timeout=300,
            cwd=OMADA_DATA_DIR
        )
        if result.returncode != 0:
            return {"success": False, "message": result.stderr}

        # cp -f to backup dir
        shutil.copy2(
            os.path.join(OMADA_DATA_DIR, DB_FILE_NAME),
            os.path.join(OMADA_BACKUP_DIR, DB_FILE_NAME)
        )
        os.remove(os.path.join(OMADA_DATA_DIR, DB_FILE_NAME))

        # Also backup cluster if it exists (same as prerm)
        cluster_hs = os.path.join(OMADA_DATA_DIR, "cluster", "hsConfig")
        cluster_ha = os.path.join(OMADA_DATA_DIR, "cluster", "haPersistentConfig")
        if os.path.exists(cluster_hs) or os.path.exists(cluster_ha):
            result2 = subprocess.run(
                ["tar", "zcf", CLUSTER_FILE_NAME, "cluster"],
                capture_output=True, text=True, timeout=300,
                cwd=OMADA_DATA_DIR
            )
            if result2.returncode == 0:
                shutil.copy2(
                    os.path.join(OMADA_DATA_DIR, CLUSTER_FILE_NAME),
                    os.path.join(OMADA_BACKUP_DIR, CLUSTER_FILE_NAME)
                )
                os.remove(os.path.join(OMADA_DATA_DIR, CLUSTER_FILE_NAME))

        size = os.path.getsize(os.path.join(OMADA_BACKUP_DIR, DB_FILE_NAME))
        return {"success": True, "name": DB_FILE_NAME, "size": size}
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


def restore_backup(filename):
    """Restore a native Omada backup (replicates postinst import_mongo_db)."""
    safe_name = secure_filename(filename)
    archive_path = os.path.join(OMADA_BACKUP_DIR, safe_name)
    if not os.path.isfile(archive_path):
        return {"success": False, "message": "Fichier backup introuvable"}

    try:
        subprocess.run(["systemctl", "stop", SERVICE_NAME],
                       capture_output=True, timeout=60)

        # If restoring db backup, clear existing db first
        if safe_name == DB_FILE_NAME:
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
    return jsonify({"version": v or "inconnue"})


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
    command = f'dpkg -r omadac && dpkg -i "{filepath}"'
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
    command = f'dpkg -i "{filepath}"'
    _start_terminal(sid, command, f"Installation de Omada avec {filename}")


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
