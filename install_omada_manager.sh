#!/bin/bash
# ===================================================================
# Omada Web Manager - Standalone installation script
# ===================================================================
# Downloads and installs Omada Web Manager from GitHub.
# Checks and installs dependencies (Java 17, MongoDB 7.0).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh | sudo bash
#
# ===================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Configuration
INSTALL_DIR="/opt/omada-web-manager"
SERVICE_NAME="omada-web"
DEFAULT_PORT=30560
GITHUB_RAW="https://raw.githubusercontent.com/Vayaris/Omada-Manager/main"
RESERVED_PORTS=(80 443 8088 8043 8843 29810 29811 29812 29813 29814 29815 29816 29817 27001 27217)

# -------------------------------------------------------------------
# Helper: read input (works with pipe and terminal)
# -------------------------------------------------------------------
read_input() {
    if [ -t 0 ]; then
        read -p "$1" REPLY
    else
        read -p "$1" REPLY < /dev/tty || REPLY=""
    fi
    echo "$REPLY"
}

# -------------------------------------------------------------------
# Language selection
# -------------------------------------------------------------------
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Omada Web Manager - Installation          ${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo -e "  Language / Langue :"
echo -e "    ${CYAN}en${NC} - English (default)"
echo -e "    ${CYAN}fr${NC} - Français"
echo ""

LANG_INPUT=$(read_input "Choose language / Choisir la langue [en] : ")
LANG_INPUT=$(echo "$LANG_INPUT" | tr '[:upper:]' '[:lower:]')

if [ "$LANG_INPUT" = "fr" ]; then
    L="fr"
else
    L="en"
fi

# -------------------------------------------------------------------
# i18n strings
# -------------------------------------------------------------------
if [ "$L" = "fr" ]; then
    MSG_ROOT_ERR="Erreur : Ce script doit être exécuté en root (sudo)."
    MSG_PORT_TITLE="Port de l'interface web"
    MSG_PORT_DEFAULT="Port par défaut"
    MSG_PORT_RESERVED="Ports réservés (Omada/système)"
    MSG_PORT_PROMPT="Port souhaité"
    MSG_PORT_NAN="Erreur : Le port doit être un nombre."
    MSG_PORT_RANGE="Erreur : Le port doit être entre 1024 et 65535."
    MSG_PORT_RESERVED_ERR="Erreur : Le port {0} est réservé par Omada ou le système."
    MSG_PORT_USED="Attention : Le port {0} est déjà utilisé."
    MSG_PORT_CONTINUE="Continuer quand même ? [o/N]"
    MSG_CANCELLED="Installation annulée."
    MSG_PORT_OK="Port sélectionné"
    MSG_STEP1="Installation des dépendances système..."
    MSG_STEP1_OK="Dépendances système installées."
    MSG_STEP2="Vérification de Java 17..."
    MSG_JAVA_OK="Java déjà installé"
    MSG_JAVA_OLD="Java {0} trouvé, mais Java 17+ requis. Installation..."
    MSG_JAVA_INSTALL="Installation de openjdk-17-jre-headless..."
    MSG_JAVA_DONE="Java 17 installé."
    MSG_STEP3="Vérification de MongoDB 7.0..."
    MSG_MONGO_OK="MongoDB déjà installé"
    MSG_MONGO_INSTALL="MongoDB non trouvé. Installation de MongoDB 7.0..."
    MSG_MONGO_DONE="MongoDB 7.0 installé et démarré."
    MSG_STEP4="Préparation du répertoire d'installation..."
    MSG_STEP4_OK="Répertoire créé"
    MSG_STEP5="Téléchargement des fichiers depuis GitHub..."
    MSG_STEP5_OK="Fichiers téléchargés."
    MSG_DL_FAIL="Échec"
    MSG_STEP6="Création de l'environnement virtuel Python..."
    MSG_STEP6_OK="Environnement virtuel et packages installés."
    MSG_STEP7="Écriture de la configuration..."
    MSG_STEP7_OK="Port configuré"
    MSG_STEP8="Création du script de démarrage..."
    MSG_STEP8_OK="Script de démarrage créé."
    MSG_STEP9="Installation du service systemd..."
    MSG_STEP9_OK="Service systemd installé et activé."
    MSG_STEP10="Démarrage du service..."
    MSG_SUCCESS="Installation terminée avec succès !"
    MSG_ACCESS="Interface web accessible sur :"
    MSG_LOGIN="Connectez-vous avec vos identifiants système Linux."
    MSG_COMMANDS="Commandes utiles :"
    MSG_CMD_STATUS="Voir le statut"
    MSG_CMD_RESTART="Redémarrer"
    MSG_CMD_STOP="Arrêter"
    MSG_INSTALLDIR="Répertoire d'installation"
    MSG_FAIL="Erreur : Le service n'a pas pu démarrer."
    MSG_LOGS="Consultez les logs"
else
    MSG_ROOT_ERR="Error: This script must be run as root (sudo)."
    MSG_PORT_TITLE="Web interface port"
    MSG_PORT_DEFAULT="Default port"
    MSG_PORT_RESERVED="Reserved ports (Omada/system)"
    MSG_PORT_PROMPT="Desired port"
    MSG_PORT_NAN="Error: Port must be a number."
    MSG_PORT_RANGE="Error: Port must be between 1024 and 65535."
    MSG_PORT_RESERVED_ERR="Error: Port {0} is reserved by Omada or the system."
    MSG_PORT_USED="Warning: Port {0} is already in use."
    MSG_PORT_CONTINUE="Continue anyway? [y/N]"
    MSG_CANCELLED="Installation cancelled."
    MSG_PORT_OK="Selected port"
    MSG_STEP1="Installing system dependencies..."
    MSG_STEP1_OK="System dependencies installed."
    MSG_STEP2="Checking Java 17..."
    MSG_JAVA_OK="Java already installed"
    MSG_JAVA_OLD="Java {0} found, but Java 17+ required. Installing..."
    MSG_JAVA_INSTALL="Installing openjdk-17-jre-headless..."
    MSG_JAVA_DONE="Java 17 installed."
    MSG_STEP3="Checking MongoDB 7.0..."
    MSG_MONGO_OK="MongoDB already installed"
    MSG_MONGO_INSTALL="MongoDB not found. Installing MongoDB 7.0..."
    MSG_MONGO_DONE="MongoDB 7.0 installed and started."
    MSG_STEP4="Preparing installation directory..."
    MSG_STEP4_OK="Directory created"
    MSG_STEP5="Downloading files from GitHub..."
    MSG_STEP5_OK="Files downloaded."
    MSG_DL_FAIL="Failed"
    MSG_STEP6="Creating Python virtual environment..."
    MSG_STEP6_OK="Virtual environment and packages installed."
    MSG_STEP7="Writing configuration..."
    MSG_STEP7_OK="Port configured"
    MSG_STEP8="Creating startup script..."
    MSG_STEP8_OK="Startup script created."
    MSG_STEP9="Installing systemd service..."
    MSG_STEP9_OK="Systemd service installed and enabled."
    MSG_STEP10="Starting service..."
    MSG_SUCCESS="Installation completed successfully!"
    MSG_ACCESS="Web interface available at:"
    MSG_LOGIN="Log in with your Linux system credentials."
    MSG_COMMANDS="Useful commands:"
    MSG_CMD_STATUS="View status"
    MSG_CMD_RESTART="Restart"
    MSG_CMD_STOP="Stop"
    MSG_INSTALLDIR="Installation directory"
    MSG_FAIL="Error: The service could not start."
    MSG_LOGS="Check logs"
fi

echo ""

# -------------------------------------------------------------------
# Root check
# -------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}${MSG_ROOT_ERR}${NC}"
    exit 1
fi

# -------------------------------------------------------------------
# Port selection
# -------------------------------------------------------------------
echo -e "${CYAN}${MSG_PORT_TITLE}${NC}"
echo -e "  ${MSG_PORT_DEFAULT} : ${GREEN}${DEFAULT_PORT}${NC}"
echo -e "  ${MSG_PORT_RESERVED} : ${RED}${RESERVED_PORTS[*]}${NC}"
echo ""

USER_PORT=$(read_input "${MSG_PORT_PROMPT} [${DEFAULT_PORT}] : ")
PORT=${USER_PORT:-$DEFAULT_PORT}

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo -e "${RED}${MSG_PORT_NAN}${NC}"
    exit 1
fi

if [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    echo -e "${RED}${MSG_PORT_RANGE}${NC}"
    exit 1
fi

for rp in "${RESERVED_PORTS[@]}"; do
    if [ "$PORT" -eq "$rp" ]; then
        echo -e "${RED}${MSG_PORT_RESERVED_ERR/\{0\}/$PORT}${NC}"
        exit 1
    fi
done

if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
    echo -e "${YELLOW}${MSG_PORT_USED/\{0\}/$PORT}${NC}"
    CONTINUE=$(read_input "${MSG_PORT_CONTINUE} : ")
    if [ "$L" = "fr" ]; then
        if [ "$CONTINUE" != "o" ] && [ "$CONTINUE" != "O" ]; then
            echo "$MSG_CANCELLED"
            exit 1
        fi
    else
        if [ "$CONTINUE" != "y" ] && [ "$CONTINUE" != "Y" ]; then
            echo "$MSG_CANCELLED"
            exit 1
        fi
    fi
fi

echo -e "${GREEN}  -> ${MSG_PORT_OK} : ${PORT}${NC}"
echo ""

# -------------------------------------------------------------------
# Step 1/10: System dependencies (Python)
# -------------------------------------------------------------------
echo -e "${YELLOW}[1/10] ${MSG_STEP1}${NC}"
apt update -qq
apt install -y python3-pip python3-venv curl gnupg > /dev/null 2>&1
echo -e "${GREEN}  -> ${MSG_STEP1_OK}${NC}"

# -------------------------------------------------------------------
# Step 2/10: Java 17
# -------------------------------------------------------------------
echo -e "${YELLOW}[2/10] ${MSG_STEP2}${NC}"
JAVA_OK=false
if type java >/dev/null 2>&1; then
    JAVA_VERSION=$(java -version 2>&1 | head -1)
    JAVA_MAJOR=$(echo "$JAVA_VERSION" | grep -oP '(?:version ")?\K\d+' | head -1)
    if [ -n "$JAVA_MAJOR" ] && [ "$JAVA_MAJOR" -ge 17 ] 2>/dev/null; then
        echo -e "${GREEN}  -> ${MSG_JAVA_OK} : ${JAVA_VERSION}${NC}"
        JAVA_OK=true
    else
        echo -e "${YELLOW}  -> ${MSG_JAVA_OLD/\{0\}/$JAVA_MAJOR}${NC}"
    fi
fi

if [ "$JAVA_OK" = false ]; then
    apt install -y openjdk-17-jre-headless > /dev/null 2>&1
    echo -e "${GREEN}  -> ${MSG_JAVA_DONE}${NC}"
fi

# -------------------------------------------------------------------
# Step 3/10: MongoDB 7.0
# -------------------------------------------------------------------
echo -e "${YELLOW}[3/10] ${MSG_STEP3}${NC}"
if type mongod >/dev/null 2>&1; then
    MONGO_VERSION=$(mongod --version 2>&1 | head -1)
    echo -e "${GREEN}  -> ${MSG_MONGO_OK} : ${MONGO_VERSION}${NC}"
else
    echo -e "${YELLOW}  -> ${MSG_MONGO_INSTALL}${NC}"

    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
        gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg 2>/dev/null

    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list

    apt update -qq
    apt install -y mongodb-org > /dev/null 2>&1
    systemctl start mongod
    systemctl enable mongod

    echo -e "${GREEN}  -> ${MSG_MONGO_DONE}${NC}"
fi

# -------------------------------------------------------------------
# Step 4/10: Installation directory
# -------------------------------------------------------------------
echo -e "${YELLOW}[4/10] ${MSG_STEP4}${NC}"
mkdir -p "${INSTALL_DIR}/templates"
mkdir -p "${INSTALL_DIR}/static"
mkdir -p "${INSTALL_DIR}/uploads"
echo -e "${GREEN}  -> ${MSG_STEP4_OK} : ${INSTALL_DIR}${NC}"

# -------------------------------------------------------------------
# Step 5/10: Download files from GitHub
# -------------------------------------------------------------------
echo -e "${YELLOW}[5/10] ${MSG_STEP5}${NC}"

download_file() {
    local remote_path="$1"
    local local_path="$2"
    if curl -fsSL "${GITHUB_RAW}/${remote_path}" -o "${local_path}"; then
        echo -e "  ${GREEN}✓${NC} ${remote_path}"
    else
        echo -e "  ${RED}✗ ${MSG_DL_FAIL} : ${remote_path}${NC}"
        exit 1
    fi
}

download_file "app.py" "${INSTALL_DIR}/app.py"
download_file "requirements.txt" "${INSTALL_DIR}/requirements.txt"
download_file "templates/login.html" "${INSTALL_DIR}/templates/login.html"
download_file "templates/index.html" "${INSTALL_DIR}/templates/index.html"
download_file "static/style.css" "${INSTALL_DIR}/static/style.css"

echo -e "${GREEN}  -> ${MSG_STEP5_OK}${NC}"

# -------------------------------------------------------------------
# Step 6/10: Python virtual environment
# -------------------------------------------------------------------
echo -e "${YELLOW}[6/10] ${MSG_STEP6}${NC}"
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
echo -e "${GREEN}  -> ${MSG_STEP6_OK}${NC}"

# -------------------------------------------------------------------
# Step 7/10: Configuration
# -------------------------------------------------------------------
echo -e "${YELLOW}[7/10] ${MSG_STEP7}${NC}"
cat > "${INSTALL_DIR}/config.txt" <<EOF
PORT=${PORT}
EOF
echo -e "${GREEN}  -> ${MSG_STEP7_OK} : ${PORT}${NC}"

# -------------------------------------------------------------------
# Step 8/10: Startup script
# -------------------------------------------------------------------
echo -e "${YELLOW}[8/10] ${MSG_STEP8}${NC}"
cat > "${INSTALL_DIR}/start.sh" <<EOF
#!/bin/bash
cd "${INSTALL_DIR}"
exec "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/app.py"
EOF
chmod +x "${INSTALL_DIR}/start.sh"
echo -e "${GREEN}  -> ${MSG_STEP8_OK}${NC}"

# -------------------------------------------------------------------
# Step 9/10: Systemd service
# -------------------------------------------------------------------
echo -e "${YELLOW}[9/10] ${MSG_STEP9}${NC}"
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Omada Web Manager
After=network.target

[Service]
Type=simple
User=root
ExecStart=/bin/bash "${INSTALL_DIR}/start.sh"
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service
echo -e "${GREEN}  -> ${MSG_STEP9_OK}${NC}"

# -------------------------------------------------------------------
# Step 10/10: Start
# -------------------------------------------------------------------
echo -e "${YELLOW}[10/10] ${MSG_STEP10}${NC}"
systemctl restart ${SERVICE_NAME}.service
sleep 3

if systemctl is-active --quiet ${SERVICE_NAME}.service; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  ${MSG_SUCCESS}${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  ${MSG_ACCESS}"
    echo -e "  ${CYAN}http://${IP}:${PORT}${NC}"
    echo ""
    echo -e "  ${MSG_LOGIN}"
    echo ""
    echo -e "  ${MSG_COMMANDS}"
    echo -e "    systemctl status ${SERVICE_NAME}   - ${MSG_CMD_STATUS}"
    echo -e "    systemctl restart ${SERVICE_NAME}  - ${MSG_CMD_RESTART}"
    echo -e "    systemctl stop ${SERVICE_NAME}     - ${MSG_CMD_STOP}"
    echo ""
    echo -e "  ${MSG_INSTALLDIR} : ${INSTALL_DIR}"
    echo ""
else
    echo -e "${RED}${MSG_FAIL}${NC}"
    echo -e "${MSG_LOGS} : journalctl -u ${SERVICE_NAME} -n 30"
    exit 1
fi
