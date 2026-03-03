# Omada Web Manager

Interface web pour installer, gérer et mettre à jour **TP-Link Omada SDN Controller** sur un serveur Linux.

---

## Qu'est-ce que c'est ?

Omada Web Manager est un panneau de contrôle web léger qui permet de :

- **Installer** Omada SDN Controller sur une machine vierge (via upload d'un fichier `.deb`)
- **Surveiller** l'état du service Omada en temps réel (actif / arrêté / en cours de démarrage)
- **Contrôler** le service : démarrer, arrêter, redémarrer en un clic
- **Mettre à jour** le contrôleur en uploadant une nouvelle version `.deb`
- **Sauvegarder et restaurer** la configuration du contrôleur (backup natif Omada)
- **Superviser l'espace disque** disponible sur le serveur

L'interface est disponible en **français** et en **anglais**, avec un thème **clair** et **sombre**.

---

## Prérequis

- Un serveur sous **Ubuntu 22.04 LTS** (ou compatible Debian)
- Un accès **root** ou **sudo**
- Une connexion internet (pour télécharger les dépendances)

Le script d'installation se charge automatiquement d'installer :
- **Java 17** (OpenJDK 17 JRE Headless)
- **MongoDB 7.0**
- **Python 3** avec pip et venv

---

## Installation

### Méthode rapide (une seule commande)

```bash
curl -fsSL https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh | sudo bash
```

C'est tout. Le script :
1. Vous demande un port (par défaut **30560**)
2. Installe Java 17 et MongoDB 7.0 s'ils ne sont pas présents
3. Télécharge les fichiers de l'application depuis GitHub
4. Crée un environnement Python isolé avec les dépendances
5. Configure et démarre un service systemd (`omada-web`)
6. Affiche l'URL d'accès à la fin

### Méthode manuelle

```bash
# Télécharger le script
wget https://raw.githubusercontent.com/Vayaris/Omada-Manager/main/install_omada_manager.sh

# Vérifier le contenu si souhaité
cat install_omada_manager.sh

# Exécuter
sudo bash install_omada_manager.sh
```

---

## Accès à l'interface

Une fois installé, ouvrez un navigateur et allez sur :

```
http://<IP_DU_SERVEUR>:30560
```

Remplacez `<IP_DU_SERVEUR>` par l'adresse IP de votre machine et `30560` par le port choisi lors de l'installation.

### Connexion

Utilisez les **identifiants du système Linux** (le compte utilisateur de la machine). L'authentification se fait via PAM (le même mécanisme que SSH).

---

## Utilisation

### Cas 1 : Omada n'est pas encore installé

Lorsque le contrôleur Omada SDN n'est pas détecté sur la machine :

1. L'interface affiche un bandeau **"Omada Controller n'est pas installé"**
2. L'état des dépendances (Java, MongoDB) est affiché pour vérifier qu'elles sont prêtes
3. La zone d'upload est visible : **glissez-déposez** un fichier `.deb` d'Omada ou cliquez pour le sélectionner
4. Cliquez sur **"Installer Omada"** à côté du fichier uploadé
5. Un terminal interactif s'ouvre dans la page : vous voyez l'installation en direct et pouvez répondre aux questions si nécessaire
6. Une fois terminé, la page se rafraîchit automatiquement et passe au mode normal

> **Où trouver le .deb ?** Téléchargez la dernière version de Omada SDN Controller depuis le [site officiel TP-Link](https://www.tp-link.com/fr/support/download/omada-software-controller/).

### Cas 2 : Omada est déjà installé

L'interface affiche :

- **Version actuelle** d'Omada (dans le bandeau en haut, ex: `v6.2.0.12`)
- **État du service** : indicateur vert (actif) / rouge (arrêté) avec rafraîchissement automatique toutes les 10 secondes
- **Boutons de contrôle** :
  - **Démarrer** : lance le service s'il est arrêté
  - **Redémarrer** : relance le service
  - **Arrêter** : stoppe le service
- **Détails du service** : section dépliable montrant la sortie complète de `systemctl status`

### Mise à jour d'Omada

1. Uploadez le nouveau fichier `.deb` dans la zone de dépôt
2. Cliquez sur **"Mettre à jour"** à côté du fichier
3. Le processus se déroule en 2 étapes dans le terminal intégré :
   - **Désinstallation** de l'ancienne version (`dpkg -r omadac`) — le script natif d'Omada vous propose de **sauvegarder la configuration**
   - **Installation** de la nouvelle version (`dpkg -i`) — le script natif vous propose de **restaurer la configuration** sauvegardée
4. Vous pouvez interagir avec le terminal (répondre yes/no aux questions)
5. Le statut passe à **"Terminé avec succès"** (vert) ou **"Terminé avec erreurs"** (rouge)

### Sauvegardes

Les sauvegardes utilisent le **mécanisme natif d'Omada** (le même que celui utilisé lors de la désinstallation). Elles sont stockées dans `/opt/tplink/omada_db_backup/`.

- **Créer un backup** : cliquez sur le bouton "Créer un backup" — archive la base de données MongoDB d'Omada
- **Restaurer** : cliquez sur "Restaurer" à côté d'une sauvegarde — le service est arrêté, la base restaurée, puis le service est relancé
- **Supprimer** : cliquez sur "Supprimer" pour libérer de l'espace

### Gestion des fichiers uploadés

Les fichiers `.deb` uploadés sont conservés sur le serveur dans le dossier `uploads/`. Vous pouvez :
- Les **installer/mettre à jour** en cliquant sur le bouton correspondant
- Les **supprimer** avec le bouton "Supprimer" à côté de chaque fichier, pour libérer de l'espace disque

### Espace disque

Une barre d'utilisation du disque est affichée au-dessus de la zone d'upload. Elle indique :
- L'espace **utilisé**, **total** et **libre**
- La barre passe en **orange** au-dessus de 75% d'utilisation et en **rouge** au-dessus de 90%

Cela permet de vérifier qu'il y a assez de place avant d'uploader un fichier `.deb` (souvent 200+ Mo) ou de créer un backup.

### Langue et thème

- **FR / EN** : cliquez sur le bouton de langue dans le bandeau en haut à droite
- **Clair / Sombre** : cliquez sur l'icône soleil/lune dans le bandeau
- Les préférences sont sauvegardées dans le navigateur

---

## Architecture technique

```
/opt/omada-web-manager/        # Répertoire d'installation (créé par le script)
├── app.py                     # Backend Flask (API REST + WebSocket)
├── requirements.txt           # Dépendances Python
├── config.txt                 # Port configuré (généré à l'installation)
├── start.sh                   # Script de démarrage (généré à l'installation)
├── venv/                      # Environnement Python isolé
├── uploads/                   # Fichiers .deb uploadés
├── templates/
│   ├── login.html             # Page de connexion
│   └── index.html             # Dashboard principal
└── static/
    └── style.css              # Styles (thèmes clair/sombre)
```

### Service systemd

Le service s'appelle `omada-web` et s'exécute en tant que **root** (nécessaire pour `dpkg` et `systemctl`).

```bash
# Voir le statut
systemctl status omada-web

# Redémarrer
sudo systemctl restart omada-web

# Arrêter
sudo systemctl stop omada-web

# Voir les logs
journalctl -u omada-web -n 50
```

### Ports

| Port | Utilisé par |
|------|-------------|
| 30560 (défaut) | Omada Web Manager |
| 8088 | Omada Controller (HTTP) |
| 8043 | Omada Controller (HTTPS) |
| 8843 | Omada Controller (HTTPS portal) |
| 29810-29817 | Omada Controller (communication appareils) |
| 27001, 27217 | MongoDB |

Le port de Omada Web Manager est configurable lors de l'installation. Les ports d'Omada Controller et MongoDB sont réservés et ne peuvent pas être utilisés.

### Sécurité

- L'authentification utilise **PAM** (les comptes système Linux)
- Les sessions sont gérées par Flask avec un secret aléatoire
- Les noms de fichiers uploadés sont nettoyés (sanitization)
- Seuls les fichiers `.deb` sont acceptés
- La taille maximale d'upload est de **500 Mo**

---

## Désinstallation

```bash
# Arrêter et désactiver le service
sudo systemctl stop omada-web
sudo systemctl disable omada-web

# Supprimer le fichier service
sudo rm /etc/systemd/system/omada-web.service
sudo systemctl daemon-reload

# Supprimer les fichiers de l'application
sudo rm -rf /opt/omada-web-manager
```

Cela ne touche **pas** à Omada Controller ni à ses données.

---

## Dépannage

### Le service ne démarre pas

```bash
# Vérifier les logs
journalctl -u omada-web -n 30

# Vérifier que Python et le venv sont OK
/opt/omada-web-manager/venv/bin/python --version
```

### Erreur "No space left on device"

L'espace disque est insuffisant. Solutions :
- Supprimer les anciens fichiers `.deb` via l'interface (bouton Supprimer)
- Supprimer les anciens backups via l'interface
- Vérifier l'espace avec `df -h /`

### Impossible de se connecter

- Vérifiez que vous utilisez les identifiants **du système Linux** (pas ceux d'Omada Controller)
- Vérifiez que le service tourne : `systemctl status omada-web`
- Vérifiez que le port est accessible (pare-feu)

### L'installation d'Omada échoue

- Vérifiez que Java 17 et MongoDB 7.0 sont installés (affichés dans la section Dépendances)
- Vérifiez l'espace disque disponible (affiché dans la barre d'espace disque)
- Consultez la sortie du terminal intégré pour les erreurs détaillées

---

## Licence

Ce projet est open source.
