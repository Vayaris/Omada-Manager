<p align="center">
  <img src="https://img.shields.io/badge/Platform-Linux-blue?logo=linux&logoColor=white" alt="Linux">
  <img src="https://img.shields.io/badge/Python-3.10+-green?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-Open_Source-orange" alt="License">
  <img src="https://img.shields.io/badge/Lang-EN_|_FR-purple" alt="Languages">
  <img src="https://img.shields.io/badge/HTTPS-Forced-brightgreen?logo=letsencrypt&logoColor=white" alt="HTTPS">
</p>

<h1 align="center">Omada Web Manager</h1>

<p align="center">
  <strong>A lightweight web panel to install, manage, and update TP-Link Omada SDN Controller on Linux.</strong>
</p>

<p align="center">
  <a href="#-version-française">🇫🇷 Version française disponible en bas de page</a>
</p>

---

## Features

| Feature | Description |
|---------|-------------|
| **Install Omada** | Deploy Omada SDN Controller on a fresh machine by uploading a `.deb` file |
| **Service monitoring** | Real-time status indicator (running / stopped) with auto-refresh |
| **Service control** | Start, stop, restart with one click |
| **Firmware update** | Upload a new `.deb` version and update through an interactive terminal |
| **Backup & restore** | Native Omada backup mechanism (`/opt/tplink/omada_db_backup/`) |
| **Auto-backup** | Scheduled backups with configurable interval and retention policy |
| **Remote backup** | Send backups to remote storage: SCP/SFTP, SMB/CIFS, NFS, or S3-compatible |
| **Forced HTTPS** | Self-signed SSL certificate with automatic HTTP to HTTPS redirect |
| **Dependency repair** | Detect and reinstall missing dependencies (Java, MongoDB, JSVC) from the UI |
| **Uninstall Omada** | Remove Omada Controller (with or without dependencies) directly from the web UI |
| **Disk monitoring** | Visual disk usage bar — warns before you run out of space |
| **Dark / Light theme** | Toggle between themes, saved in browser |
| **EN / FR** | Full bilingual interface, auto-detects browser language |

---

## Requirements

- **Ubuntu 22.04 / 24.04 / 25.04** (or Debian-based)
- **Root** or **sudo** access
- Internet connection

> The installer **automatically** handles: **Java 17**, **MongoDB 7.0**, **Python 3** (pip + venv), **OpenSSL**, **sshpass**

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh | sudo bash
```

That's it. The script will:

1. Ask for a language (`en` / `fr`) and a port (default **30560**)
2. Install Java 17 + MongoDB 7.0 if missing
3. Download app files from GitHub into `/opt/omada-web-manager/`
4. Create a Python venv and install dependencies
5. Generate a self-signed SSL certificate
6. Set up and start a `omada-web` systemd service
7. Display the HTTPS access URL

### Manual install

```bash
wget https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh
cat install_omada_manager.sh   # review first if you want
sudo bash install_omada_manager.sh
```

---

## Login

Open your browser and go to:

```
https://<SERVER_IP>:30560
```

> A self-signed SSL certificate is generated automatically. Your browser will show a security warning on first visit — this is expected, just accept the certificate.

> **HTTP redirect**: `http://<SERVER_IP>` (port 80) automatically redirects to HTTPS.

Log in with your **Linux system credentials** (same as SSH). Authentication uses PAM.

---

## Usage

### When Omada is NOT installed

1. The dashboard shows a **"Omada Controller is not installed"** banner
2. Dependency status (Java, MongoDB, JSVC) is displayed — with a **Fix** button if something is missing
3. Upload an Omada `.deb` file via drag & drop or file picker
4. Click **"Install Omada"** — an interactive terminal opens in the page
5. Follow the prompts, the page refreshes automatically when done

> **Where to get the `.deb`?** Download from the [official TP-Link website](https://www.tp-link.com/en/support/download/omada-software-controller/).

### When Omada IS installed

- **Version badge** in the header (e.g. `v6.2.0.12`)
- **Status indicator**: green = running, red = stopped (auto-refresh every 10s)
- **Control buttons**: Start / Restart / Stop
- **Service details**: expandable section with full `systemctl status` output

### Updating Omada

1. Upload the new `.deb` file
2. Click **"Update"** next to the file
3. The terminal runs two steps:
   - `dpkg -r omadac` — uninstalls current version (offers **native backup**)
   - `dpkg -i <new.deb>` — installs new version (offers **restore from backup**)
4. You can interact with the terminal (answer yes/no to prompts)
5. Status turns **green** (success) or **red** (error) when complete

### Backups

Uses Omada's **native backup system** (same as the uninstall script). Stored in `/opt/tplink/omada_db_backup/`.

- **Create** — archives the MongoDB database
- **Restore** — stops the service, restores data, restarts the service
- **Download** — download a backup archive to your computer
- **Delete** — removes a backup to free disk space

#### Auto-backup

Enable scheduled backups with a configurable interval (1, 3, 7, 14, or 30 days) and retention policy (keep N most recent backups or unlimited). Backups run automatically at 3 AM via cron.

#### Remote backup storage

Send backups to a remote destination automatically. Four methods are supported:

| Method | Description | Requires |
|--------|-------------|----------|
| **SCP / SFTP** | SSH-based transfer (password or SSH key) | `sshpass` (for password auth) |
| **SMB / CIFS** | Windows network shares | `smbclient` |
| **NFS** | Linux network file system | `nfs-common` |
| **S3 Compatible** | AWS S3, MinIO, Wasabi, Backblaze B2... | `awscli` |

The UI provides a visual card-based selector with pre-filled defaults, field descriptions, and dependency hints. You can test the connection before saving, and manually send any existing backup to the configured remote storage.

### File management

Uploaded `.deb` files are stored in `uploads/`. You can:
- **Install/Update** using the button next to each file
- **Delete** files to free disk space

### Disk usage

A usage bar is displayed above the upload zone:
- **Blue** = normal | **Orange** = >75% | **Red** = >90%
- Shows used / total / free space

### Language & Theme

- **EN / FR** — click the language button in the header (auto-detects browser language)
- **Dark / Light** — click the sun/moon icon
- Preferences are saved in the browser

---

## Architecture

```
/opt/omada-web-manager/
├── app.py                     # Flask backend (REST API + WebSocket)
├── requirements.txt           # Python dependencies
├── config.txt                 # Port config (generated at install)
├── start.sh                   # Startup script (generated at install)
├── venv/                      # Isolated Python environment
├── uploads/                   # Uploaded .deb files
├── ssl/                       # Auto-generated SSL certificate
│   ├── cert.pem               # Self-signed certificate
│   └── key.pem                # Private key
├── templates/
│   ├── login.html             # Login page
│   └── index.html             # Main dashboard
└── static/
    └── style.css              # Styles (dark/light themes)
```

### Systemd service

```bash
systemctl status omada-web      # View status
sudo systemctl restart omada-web # Restart
sudo systemctl stop omada-web    # Stop
journalctl -u omada-web -n 50   # View logs
```

### Port reference

| Port | Used by |
|------|---------|
| 80 | HTTP redirect (auto-redirect to HTTPS) |
| 30560 (default) | Omada Web Manager (HTTPS) |
| 8088 | Omada Controller (HTTP) |
| 8043 | Omada Controller (HTTPS) |
| 8843 | Omada Controller (HTTPS portal) |
| 29810-29817 | Omada Controller (device comm.) |
| 27001, 27217 | MongoDB |

### Security

- **Forced HTTPS** with auto-generated self-signed SSL certificate (RSA 2048-bit, 10-year validity)
- **Automatic HTTP to HTTPS redirect** on port 80
- **PAM authentication** (Linux system accounts)
- Random session secret (Flask)
- Remote backup passwords are masked in API responses and never logged
- Filename sanitization on upload
- Only `.deb` files accepted (max **500 MB**)

---

## Uninstall

```bash
sudo systemctl stop omada-web
sudo systemctl disable omada-web
sudo rm /etc/systemd/system/omada-web.service
sudo systemctl daemon-reload
sudo rm -rf /opt/omada-web-manager
```

> This does **not** remove Omada Controller or its data.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Service won't start | `journalctl -u omada-web -n 30` to check logs |
| SSL certificate warning | This is normal for self-signed certificates — accept the exception in your browser |
| "No space left on device" | Delete old `.deb` files or backups from the web interface |
| Can't log in | Use **Linux system** credentials (not Omada Controller credentials). Check `systemctl status omada-web` |
| Omada install fails | Check Java 17 + MongoDB in the Dependencies section. Check disk space |
| Remote backup fails | Ensure the required package is installed (`sshpass`, `smbclient`, `nfs-common`, or `awscli`) |

---

---

<h1 align="center" id="-version-française">🇫🇷 Version Française</h1>

<p align="center">
  <strong>Interface web légère pour installer, gérer et mettre à jour TP-Link Omada SDN Controller sous Linux.</strong>
</p>

---

## Fonctionnalités

| Fonctionnalité | Description |
|----------------|-------------|
| **Installer Omada** | Déployer Omada SDN Controller sur une machine vierge en uploadant un fichier `.deb` |
| **Surveillance du service** | Indicateur de statut en temps réel (actif / arrêté) avec rafraîchissement automatique |
| **Contrôle du service** | Démarrer, arrêter, redémarrer en un clic |
| **Mise à jour firmware** | Uploader une nouvelle version `.deb` et mettre à jour via un terminal interactif |
| **Backup & restauration** | Mécanisme de sauvegarde natif Omada (`/opt/tplink/omada_db_backup/`) |
| **Backup automatique** | Sauvegardes planifiées avec intervalle et politique de rétention configurables |
| **Backup distant** | Envoi des backups vers un stockage distant : SCP/SFTP, SMB/CIFS, NFS ou S3 |
| **HTTPS forcé** | Certificat SSL auto-signé avec redirection automatique HTTP vers HTTPS |
| **Réparation des dépendances** | Détection et réinstallation des dépendances manquantes (Java, MongoDB, JSVC) depuis l'interface |
| **Désinstaller Omada** | Supprimer Omada Controller (avec ou sans dépendances) directement depuis l'interface web |
| **Surveillance du disque** | Barre visuelle d'utilisation du disque — alerte avant de manquer d'espace |
| **Thème sombre / clair** | Basculer entre les thèmes, sauvegardé dans le navigateur |
| **FR / EN** | Interface entièrement bilingue, détecte automatiquement la langue du navigateur |

---

## Prérequis

- **Ubuntu 22.04 / 24.04 / 25.04** (ou compatible Debian)
- Accès **root** ou **sudo**
- Connexion internet

> L'installateur gère **automatiquement** : **Java 17**, **MongoDB 7.0**, **Python 3** (pip + venv), **OpenSSL**, **sshpass**

---

## Installation rapide

```bash
curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh | sudo bash
```

C'est tout. Le script va :

1. Demander la langue (`en` / `fr`) et le port (par défaut **30560**)
2. Installer Java 17 + MongoDB 7.0 si absents
3. Télécharger les fichiers depuis GitHub dans `/opt/omada-web-manager/`
4. Créer un environnement Python virtuel et installer les dépendances
5. Générer un certificat SSL auto-signé
6. Configurer et démarrer un service systemd `omada-web`
7. Afficher l'URL d'accès HTTPS

### Installation manuelle

```bash
wget https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh
cat install_omada_manager.sh   # vérifier le contenu si souhaité
sudo bash install_omada_manager.sh
```

---

## Connexion

Ouvrez votre navigateur et allez sur :

```
https://<IP_DU_SERVEUR>:30560
```

> Un certificat SSL auto-signé est généré automatiquement. Votre navigateur affichera un avertissement de sécurité à la première visite — c'est normal, il suffit d'accepter le certificat.

> **Redirection HTTP** : `http://<IP_DU_SERVEUR>` (port 80) redirige automatiquement vers HTTPS.

Connectez-vous avec vos **identifiants Linux** (les mêmes que pour SSH). L'authentification utilise PAM.

---

## Utilisation

### Quand Omada n'est PAS installé

1. Le tableau de bord affiche un bandeau **"Omada Controller n'est pas installé"**
2. L'état des dépendances (Java, MongoDB, JSVC) est affiché — avec un bouton **Réparer** si quelque chose manque
3. Uploadez un fichier `.deb` Omada par glisser-déposer ou sélection
4. Cliquez sur **"Installer Omada"** — un terminal interactif s'ouvre dans la page
5. Suivez les instructions, la page se rafraîchit automatiquement à la fin

> **Où trouver le `.deb` ?** Téléchargez depuis le [site officiel TP-Link](https://www.tp-link.com/fr/support/download/omada-software-controller/).

### Quand Omada EST installé

- **Badge de version** dans le header (ex: `v6.2.0.12`)
- **Indicateur de statut** : vert = actif, rouge = arrêté (rafraîchissement auto toutes les 10s)
- **Boutons de contrôle** : Démarrer / Redémarrer / Arrêter
- **Détails du service** : section dépliable avec la sortie complète de `systemctl status`

### Mise à jour d'Omada

1. Uploadez le nouveau fichier `.deb`
2. Cliquez sur **"Mettre à jour"** à côté du fichier
3. Le terminal exécute deux étapes :
   - `dpkg -r omadac` — désinstalle la version actuelle (propose un **backup natif**)
   - `dpkg -i <nouveau.deb>` — installe la nouvelle version (propose de **restaurer le backup**)
4. Vous pouvez interagir avec le terminal (répondre yes/no aux questions)
5. Le statut passe en **vert** (succès) ou **rouge** (erreur) à la fin

### Sauvegardes

Utilise le **système de backup natif d'Omada** (le même que le script de désinstallation). Stocké dans `/opt/tplink/omada_db_backup/`.

- **Créer** — archive la base de données MongoDB
- **Restaurer** — arrête le service, restaure les données, relance le service
- **Télécharger** — télécharge une archive de backup sur votre ordinateur
- **Supprimer** — supprime une sauvegarde pour libérer de l'espace

#### Backup automatique

Activez les sauvegardes planifiées avec un intervalle configurable (1, 3, 7, 14 ou 30 jours) et une politique de rétention (garder les N plus récents ou illimité). Les backups s'exécutent automatiquement à 3h du matin via cron.

#### Stockage distant

Envoyez vos backups automatiquement vers un stockage distant. Quatre méthodes supportées :

| Méthode | Description | Nécessite |
|---------|-------------|-----------|
| **SCP / SFTP** | Transfert SSH (mot de passe ou clé SSH) | `sshpass` (pour l'auth par mot de passe) |
| **SMB / CIFS** | Partage réseau Windows | `smbclient` |
| **NFS** | Système de fichiers réseau Linux | `nfs-common` |
| **S3 Compatible** | AWS S3, MinIO, Wasabi, Backblaze B2... | `awscli` |

L'interface propose un sélecteur visuel sous forme de cartes avec des valeurs pré-remplies, des descriptions de champs et des indications de dépendances. Vous pouvez tester la connexion avant de sauvegarder, et envoyer manuellement n'importe quel backup existant vers le stockage distant configuré.

### Gestion des fichiers

Les fichiers `.deb` uploadés sont conservés dans `uploads/`. Vous pouvez :
- **Installer/Mettre à jour** via le bouton correspondant
- **Supprimer** les fichiers pour libérer de l'espace disque

### Espace disque

Une barre d'utilisation est affichée au-dessus de la zone d'upload :
- **Bleu** = normal | **Orange** = >75% | **Rouge** = >90%
- Affiche l'espace utilisé / total / libre

### Langue et thème

- **FR / EN** — cliquez sur le bouton de langue dans le header (détecte automatiquement la langue du navigateur)
- **Sombre / Clair** — cliquez sur l'icône soleil/lune
- Les préférences sont sauvegardées dans le navigateur

---

## Architecture

```
/opt/omada-web-manager/
├── app.py                     # Backend Flask (API REST + WebSocket)
├── requirements.txt           # Dépendances Python
├── config.txt                 # Configuration du port (généré à l'install)
├── start.sh                   # Script de démarrage (généré à l'install)
├── venv/                      # Environnement Python isolé
├── uploads/                   # Fichiers .deb uploadés
├── ssl/                       # Certificat SSL auto-généré
│   ├── cert.pem               # Certificat auto-signé
│   └── key.pem                # Clé privée
├── templates/
│   ├── login.html             # Page de connexion
│   └── index.html             # Tableau de bord principal
└── static/
    └── style.css              # Styles (thèmes sombre/clair)
```

### Service systemd

```bash
systemctl status omada-web          # Voir le statut
sudo systemctl restart omada-web    # Redémarrer
sudo systemctl stop omada-web       # Arrêter
journalctl -u omada-web -n 50      # Voir les logs
```

### Référence des ports

| Port | Utilisé par |
|------|-------------|
| 80 | Redirection HTTP (redirige auto vers HTTPS) |
| 30560 (défaut) | Omada Web Manager (HTTPS) |
| 8088 | Omada Controller (HTTP) |
| 8043 | Omada Controller (HTTPS) |
| 8843 | Omada Controller (HTTPS portail) |
| 29810-29817 | Omada Controller (comm. appareils) |
| 27001, 27217 | MongoDB |

### Sécurité

- **HTTPS forcé** avec certificat SSL auto-signé généré automatiquement (RSA 2048-bit, validité 10 ans)
- **Redirection automatique HTTP vers HTTPS** sur le port 80
- **Authentification PAM** (comptes système Linux)
- Secret de session aléatoire (Flask)
- Les mots de passe du backup distant sont masqués dans les réponses API et jamais loggés
- Nettoyage des noms de fichiers uploadés
- Seuls les fichiers `.deb` sont acceptés (max **500 Mo**)

---

## Désinstallation

```bash
sudo systemctl stop omada-web
sudo systemctl disable omada-web
sudo rm /etc/systemd/system/omada-web.service
sudo systemctl daemon-reload
sudo rm -rf /opt/omada-web-manager
```

> Cela ne touche **pas** à Omada Controller ni à ses données.

---

## Dépannage

| Problème | Solution |
|----------|----------|
| Le service ne démarre pas | `journalctl -u omada-web -n 30` pour voir les logs |
| Avertissement de certificat SSL | C'est normal pour un certificat auto-signé — acceptez l'exception dans votre navigateur |
| "No space left on device" | Supprimer les anciens `.deb` ou backups depuis l'interface web |
| Impossible de se connecter | Utiliser les identifiants **Linux** (pas ceux d'Omada Controller). Vérifier `systemctl status omada-web` |
| L'installation d'Omada échoue | Vérifier Java 17 + MongoDB dans la section Dépendances. Vérifier l'espace disque |
| Le backup distant échoue | Vérifier que le paquet requis est installé (`sshpass`, `smbclient`, `nfs-common` ou `awscli`) |

---

<p align="center">
  <strong>Made with purpose. Open source.</strong>
</p>
