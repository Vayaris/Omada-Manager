#!/bin/bash
# ===================================================================
# Omada Web Manager - Script d'installation autonome
# ===================================================================
# Ce script télécharge et installe Omada Web Manager depuis GitHub.
# Il vérifie et installe les dépendances (Java 17, MongoDB 7.0).
#
# Usage :
#   curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh | sudo bash
#
# ===================================================================

set -e

# Couleurs
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

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Omada Web Manager - Installation          ${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""

# -------------------------------------------------------------------
# Vérification root
# -------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Erreur : Ce script doit être exécuté en root (sudo).${NC}"
    exit 1
fi

# -------------------------------------------------------------------
# Étape 0 : Choix du port
# -------------------------------------------------------------------
echo -e "${CYAN}Port de l'interface web${NC}"
echo -e "  Port par défaut : ${GREEN}${DEFAULT_PORT}${NC}"
echo -e "  Ports réservés (Omada/système) : ${RED}${RESERVED_PORTS[*]}${NC}"
echo ""

# Si lancé via pipe (curl | bash), lire depuis /dev/tty
if [ -t 0 ]; then
    read -p "Port souhaité [${DEFAULT_PORT}] : " USER_PORT
else
    read -p "Port souhaité [${DEFAULT_PORT}] : " USER_PORT < /dev/tty || USER_PORT=""
fi
PORT=${USER_PORT:-$DEFAULT_PORT}

# Validation du port
if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
    echo -e "${RED}Erreur : Le port doit être un nombre.${NC}"
    exit 1
fi

if [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    echo -e "${RED}Erreur : Le port doit être entre 1024 et 65535.${NC}"
    exit 1
fi

for rp in "${RESERVED_PORTS[@]}"; do
    if [ "$PORT" -eq "$rp" ]; then
        echo -e "${RED}Erreur : Le port ${PORT} est réservé par Omada ou le système.${NC}"
        exit 1
    fi
done

# Vérifier si le port est déjà utilisé (sauf si c'est notre propre service)
if ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
    echo -e "${YELLOW}Attention : Le port ${PORT} est déjà utilisé.${NC}"
    if [ -t 0 ]; then
        read -p "Continuer quand même ? [o/N] : " CONTINUE
    else
        read -p "Continuer quand même ? [o/N] : " CONTINUE < /dev/tty || CONTINUE="N"
    fi
    if [ "$CONTINUE" != "o" ] && [ "$CONTINUE" != "O" ]; then
        echo "Installation annulée."
        exit 1
    fi
fi

echo -e "${GREEN}  -> Port sélectionné : ${PORT}${NC}"
echo ""

# -------------------------------------------------------------------
# Étape 1/10 : Dépendances système (Python)
# -------------------------------------------------------------------
echo -e "${YELLOW}[1/10] Installation des dépendances système...${NC}"
apt update -qq
apt install -y python3-pip python3-venv curl gnupg > /dev/null 2>&1
echo -e "${GREEN}  -> Dépendances système installées.${NC}"

# -------------------------------------------------------------------
# Étape 2/10 : Java 17 (requis par Omada)
# -------------------------------------------------------------------
echo -e "${YELLOW}[2/10] Vérification de Java 17...${NC}"
JAVA_OK=false
if type java >/dev/null 2>&1; then
    JAVA_VERSION=$(java -version 2>&1 | head -1)
    JAVA_MAJOR=$(echo "$JAVA_VERSION" | grep -oP '(?:version ")?\K\d+' | head -1)
    if [ -n "$JAVA_MAJOR" ] && [ "$JAVA_MAJOR" -ge 17 ] 2>/dev/null; then
        echo -e "${GREEN}  -> Java déjà installé : ${JAVA_VERSION}${NC}"
        JAVA_OK=true
    else
        echo -e "${YELLOW}  -> Java ${JAVA_MAJOR} trouvé, mais Java 17+ requis. Installation...${NC}"
    fi
fi

if [ "$JAVA_OK" = false ]; then
    apt install -y openjdk-17-jre-headless > /dev/null 2>&1
    echo -e "${GREEN}  -> Java 17 installé.${NC}"
fi

# -------------------------------------------------------------------
# Étape 3/10 : MongoDB 7.0 (requis par Omada)
# -------------------------------------------------------------------
echo -e "${YELLOW}[3/10] Vérification de MongoDB 7.0...${NC}"
if type mongod >/dev/null 2>&1; then
    MONGO_VERSION=$(mongod --version 2>&1 | head -1)
    echo -e "${GREEN}  -> MongoDB déjà installé : ${MONGO_VERSION}${NC}"
else
    echo -e "${YELLOW}  -> MongoDB non trouvé. Installation de MongoDB 7.0...${NC}"

    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
        gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg 2>/dev/null

    echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list

    apt update -qq
    apt install -y mongodb-org > /dev/null 2>&1
    systemctl start mongod
    systemctl enable mongod

    echo -e "${GREEN}  -> MongoDB 7.0 installé et démarré.${NC}"
fi

# -------------------------------------------------------------------
# Étape 4/10 : Création du répertoire
# -------------------------------------------------------------------
echo -e "${YELLOW}[4/10] Préparation du répertoire d'installation...${NC}"
mkdir -p "${INSTALL_DIR}/templates"
mkdir -p "${INSTALL_DIR}/static"
mkdir -p "${INSTALL_DIR}/uploads"
echo -e "${GREEN}  -> Répertoire créé : ${INSTALL_DIR}${NC}"

# -------------------------------------------------------------------
# Étape 5/10 : Téléchargement des fichiers depuis GitHub
# -------------------------------------------------------------------
echo -e "${YELLOW}[5/10] Téléchargement des fichiers depuis GitHub...${NC}"

download_file() {
    local remote_path="$1"
    local local_path="$2"
    if curl -fsSL "${GITHUB_RAW}/${remote_path}" -o "${local_path}"; then
        echo -e "  ${GREEN}✓${NC} ${remote_path}"
    else
        echo -e "  ${RED}✗ Échec : ${remote_path}${NC}"
        exit 1
    fi
}

download_file "app.py" "${INSTALL_DIR}/app.py"
download_file "requirements.txt" "${INSTALL_DIR}/requirements.txt"
download_file "templates/login.html" "${INSTALL_DIR}/templates/login.html"
download_file "templates/index.html" "${INSTALL_DIR}/templates/index.html"
download_file "static/style.css" "${INSTALL_DIR}/static/style.css"

echo -e "${GREEN}  -> Fichiers téléchargés.${NC}"

# -------------------------------------------------------------------
# Étape 6/10 : Environnement virtuel Python
# -------------------------------------------------------------------
echo -e "${YELLOW}[6/10] Création de l'environnement virtuel Python...${NC}"
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
echo -e "${GREEN}  -> Environnement virtuel et packages installés.${NC}"

# -------------------------------------------------------------------
# Étape 7/10 : Configuration
# -------------------------------------------------------------------
echo -e "${YELLOW}[7/10] Écriture de la configuration...${NC}"
cat > "${INSTALL_DIR}/config.txt" <<EOF
PORT=${PORT}
EOF
echo -e "${GREEN}  -> Port configuré : ${PORT}${NC}"

# -------------------------------------------------------------------
# Étape 8/10 : Script de démarrage
# -------------------------------------------------------------------
echo -e "${YELLOW}[8/10] Création du script de démarrage...${NC}"
cat > "${INSTALL_DIR}/start.sh" <<EOF
#!/bin/bash
cd "${INSTALL_DIR}"
exec "${INSTALL_DIR}/venv/bin/python" "${INSTALL_DIR}/app.py"
EOF
chmod +x "${INSTALL_DIR}/start.sh"
echo -e "${GREEN}  -> Script de démarrage créé.${NC}"

# -------------------------------------------------------------------
# Étape 9/10 : Service systemd
# -------------------------------------------------------------------
echo -e "${YELLOW}[9/10] Installation du service systemd...${NC}"
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
echo -e "${GREEN}  -> Service systemd installé et activé.${NC}"

# -------------------------------------------------------------------
# Étape 10/10 : Démarrage
# -------------------------------------------------------------------
echo -e "${YELLOW}[10/10] Démarrage du service...${NC}"
systemctl restart ${SERVICE_NAME}.service
sleep 3

if systemctl is-active --quiet ${SERVICE_NAME}.service; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Installation terminée avec succès !       ${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo -e "  Interface web accessible sur :"
    echo -e "  ${CYAN}http://${IP}:${PORT}${NC}"
    echo ""
    echo -e "  Connectez-vous avec vos identifiants système Linux."
    echo ""
    echo -e "  Commandes utiles :"
    echo -e "    systemctl status ${SERVICE_NAME}   - Voir le statut"
    echo -e "    systemctl restart ${SERVICE_NAME}  - Redémarrer"
    echo -e "    systemctl stop ${SERVICE_NAME}     - Arrêter"
    echo ""
    echo -e "  Répertoire d'installation : ${INSTALL_DIR}"
    echo ""
else
    echo -e "${RED}Erreur : Le service n'a pas pu démarrer.${NC}"
    echo -e "Consultez les logs : journalctl -u ${SERVICE_NAME} -n 30"
    exit 1
fi
