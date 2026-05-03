# Phonon Stage Standalone — Documentation v1

> Documentation du Stage Agent autonome tel que déployé sur Raspberry Pi 3B `stage-x01`.
> Date : 3 mai 2026 | Branche : `phase-1-stage-foundations`

---

## Vue d'ensemble

Le Stage Standalone est un daemon audio FastAPI qui tourne sur un Raspberry Pi 3B (ou tout Linux arm64/x86_64). Il permet de :

- **Router de l'audio** entre des sources et des sorties via PipeWire
- **Gérer le Bluetooth** : scanner, pairer, connecter des enceintes et recevoir l'audio de téléphones
- **Contrôler le gain, le delay (phasing)** et le mute par mapping
- **Piloter le tout** depuis une interface web (smartphone, tablette, PC)

### Architecture

```
┌─ Téléphone ─┐     BT A2DP      ┌─ Pi 3B (stage-x01) ─────────────────────────┐
│  Spotify    │ ──────────────── │  bluealsa (capture) → PipeWire → bluealsa    │
│  YouTube    │                  │     ↓ arecord|pacat      ↓ parec|aplay       │
└─────────────┘                  │  null-sink (source)  → null-sink (output)    │
                                 │     gain / delay / mute / routing            │
┌─ JBL Xtreme ┐     BT A2DP     │                                              │
│  (enceinte) │ ←─────────────── │  phonon-stage :8401                          │
└──────────────┘                 │  standalone UI :8401/standalone               │
                                 └──────────────────────────────────────────────┘
```

---

## API Endpoints

### Core

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/health` | GET | Liveness check (status, uptime, stage_id) |
| `/capabilities` | GET | Audio devices ALSA + BT controllers |
| `/standalone/` | GET | Mini-UI web (HTML/CSS/JS) |

### PipeWire

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/pipewire/nodes` | GET | Lister les nœuds audio (sinks, sources, BT) |
| `/pipewire/ports` | GET | Lister les ports (filtre `?node_id=N`) |
| `/pipewire/links` | GET | Lister les liens actifs |

### Mappings (routing audio)

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/mappings` | GET | Lister les mappings actifs |
| `/mappings` | POST | Créer un mapping (source→sink + gain/pan/mute/delay) |
| `/mappings/{id}` | PATCH | Modifier gain/pan/mute/delay |
| `/mappings/{id}` | DELETE | Supprimer un mapping |

### Bluetooth

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/bluetooth/{addr}/power` | POST | Power on/off un adaptateur (rfkill + hciconfig) |
| `/bluetooth/{addr}/role` | POST | Configurer Receiver (discoverable) ou Transmitter |
| `/bluetooth/scan` | GET | Scanner les devices BT (`?controller_address=XX&timeout=10`) |
| `/bluetooth/pair` | POST | Pairer un device (D-Bus trust + pair) |
| `/bluetooth/connect` | POST | Connecter A2DP (D-Bus Device1.Connect) |
| `/bluetooth/disconnect` | DELETE | Déconnecter |
| `/bluetooth/unpair` | DELETE | Supprimer un appairage (+ cleanup bridges) |
| `/bluetooth/devices` | GET | Lister les devices pairés par contrôleur |

### BlueALSA Bridges

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/bluealsa/sync` | POST | Auto-créer les bridges PipeWire pour tous les devices BT (`?buffer_ms=50`) |
| `/bluealsa/bridges` | GET | Lister les bridges actifs |

### Système

| Endpoint | Méthode | Description |
|----------|---------|-------------|
| `/system/status` | GET | CPU, RAM, USB buses, process status |
| `/system/security` | GET | Firewall, SSH, TLS, permissions (lecture seule) |
| `/browse/stages` | GET | Découvrir les autres Stages via mDNS |

---

## Interface Web Standalone

Accessible sur `http://<ip>:8401/standalone/`

### Sections

1. **System** — Barres segmentées style audio pour CPU, Memory, USB bus (refresh 5s)
2. **Audio Devices** — Cartes ALSA + BT avec codec/latence/sample rate
3. **Patch Bay** — Sources à gauche, Sinks à droite, clic pour créer un mapping (max 8)
4. **Active Mappings** — VU meters L/R animés, gain slider (-90/+12 dB), delay slider (0-50ms), mute, delete
5. **Bluetooth** — Dropdown contrôleur, cartes Receiver/Transmitter, scan, pair, connect, unpair, Sync Audio, buffer slider
6. **Network Stages** — Autres Stages découverts via mDNS
7. **Topbar** — Logo Phonon, badge STANDALONE, About modal, Security modal, horloge

### Thème

Palette **Cryogenic** héritée du mockup Console v4.4 :
- Backgrounds : `#0a0f14` → `#243042`
- Text : `#f0f7fa` (primary) → `#6a8294` (fade)
- Accents : cyan `#06b6d4`, vert `#34d399`, magenta `#e879f9`, rouge `#f87171`
- Fonts : Barlow Condensed (display) + IBM Plex Mono (data)

---

## Stack technique déployée

| Composant | Version | Rôle |
|-----------|---------|------|
| Python | 3.11.2 | Runtime |
| FastAPI | 0.115+ | API REST |
| uvicorn | 0.32+ | ASGI server |
| PipeWire | 1.2.7 | Audio graph |
| WirePlumber | 0.5.8 (backports) | Session manager |
| BlueZ | 5.66 | Bluetooth stack |
| bluealsa | 4.0.0 | BT A2DP ↔ ALSA bridge |
| dbus-fast | 2.24+ | D-Bus async client |
| zeroconf | 0.136+ | mDNS-SD |
| structlog | 24.4+ | JSON logging |
| pydantic | 2.9+ | Validation |

---

## Bugs rencontrés et solutions

### 1. PipeWire bluez5 ne charge pas (WirePlumber)

**Symptôme** : BT devices se connectent puis déconnectent immédiatement. `a2dp-sink profile connect failed: Protocol not available`.

**Cause** : WirePlumber 0.4.13 (Bookworm stable) est incompatible avec PipeWire 1.2.7 (backport RPi). Le monitor bluez5 ne démarre pas car la feature `monitor.bluez.seat-monitoring` requiert logind, absent pour un user system.

**Solution** :
- Installer WirePlumber 0.5.8 depuis bookworm-backports
- Désactiver le monitor bluez de WirePlumber (on utilise bluealsa à la place)
- Config : `/var/lib/phonon/.config/wireplumber/wireplumber.conf.d/90-phonon-bluetooth.conf`

### 2. bluealsa-aplay bloque le PCM capture

**Symptôme** : `arecord` retourne `Device or resource busy`. Le téléphone est connecté mais aucun audio n'est capturé.

**Cause** : Le service `bluealsa-aplay.service` (installé par défaut) capture TOUS les streams BT entrants pour les jouer sur le device ALSA par défaut. Il verrouille le PCM.

**Solution** : `systemctl disable --now bluealsa-aplay` — Phonon gère le routing via ses propres bridges.

### 3. rfkill soft-block sur les adaptateurs BT

**Symptôme** : `bluetoothctl power on` échoue. `hciconfig hci1 up` retourne `Operation not possible due to RF-kill`.

**Cause** : Le Bluetooth est soft-blocked par rfkill au boot.

**Solution** : Service `phonon-bt-unblock.service` qui exécute `rfkill unblock bluetooth` avant `bluetooth.service`.

### 4. bluetoothctl en mode non-interactif perd le contexte

**Symptôme** : `bluetoothctl select X && bluetoothctl power off` éteint le mauvais adaptateur.

**Cause** : Chaque appel `bluetoothctl` est une session indépendante. Le `select` ne persiste pas.

**Solution** :
- Power on/off : `hciconfig hciN up/down` + `rfkill block/unblock N` (par adaptateur)
- Scan : D-Bus `Adapter1.StartDiscovery` (cible l'adaptateur directement)
- Pair/unpair : D-Bus `Device1.Pair` / `Adapter1.RemoveDevice`
- Connect/disconnect : D-Bus `Device1.Connect` (avec agent permanent)

### 5. Pas d'agent BlueZ → connexions A2DP échouent

**Symptôme** : Le pairing réussit mais la connexion A2DP tombe immédiatement.

**Cause** : Sans agent BlueZ enregistré, les requêtes `AuthorizeService` et `RequestConfirmation` ne sont pas gérées.

**Solution** : Service `phonon-bt-agent.service` — agent Python permanent (NoInputNoOutput) qui auto-accepte et auto-trust tous les devices.

### 6. D-Bus policy BlueZ insuffisante pour user phonon

**Symptôme** : `DBusError: Failed` lors de `Set Powered=True`.

**Cause** : La policy BlueZ par défaut ne permet qu'à root de modifier les propriétés des adaptateurs.

**Solution** : `/etc/dbus-1/system.d/phonon-bluetooth.conf` — autorise l'user phonon pour toutes les interfaces BlueZ + ProfileManager.

### 7. pw-dump streaming bloque (ne retourne jamais)

**Symptôme** : `/pipewire/nodes` retourne `[]`. `pw-dump` ne termine pas.

**Cause** : `pw-dump` stream en continu (mode monitoring). Le `asyncio.wait_for` timeout et le process est tué.

**Solution** : Lire par chunks de 64KB avec un timeout de 2s. Quand aucune donnée n'arrive pendant 2s, parser le JSON accumulé. Gérer le JSON tronqué en trouvant le dernier `]` valide.

### 8. pw-dump rate/channels retourne un dict au lieu d'un int

**Symptôme** : `int({'default': 48000, 'min': 8000, 'max': 192000})` → TypeError → nodes list vide.

**Cause** : Certains nodes PipeWire retournent `rate` comme un objet `{default, min, max}` au lieu d'un entier.

**Solution** : `rate_raw.get("default", 0) if isinstance(rate_raw, dict) else rate_raw`

### 9. Bridges bluealsa meurent au pause/resume

**Symptôme** : Pause sur le téléphone → plus de son au resume.

**Cause** : `arecord | pacat` se ferme quand le stream A2DP s'arrête.

**Solution** : Wrapper les bridges dans `while true; do ...; sleep 1; done` pour auto-restart. Plus auto-sync toutes les 10s depuis l'UI.

### 10. Bridges fantômes après restart du service

**Symptôme** : Devices BT en double dans le Patch Bay après un restart.

**Cause** : Les modules `pactl module-null-sink` persistent dans PipeWire même quand phonon-stage redémarre. Le dict `_active_bridges` en mémoire est perdu.

**Solution** : `cleanup_stale_bridges()` au démarrage — unload tous les `bt_*` null-sink modules. Vérification du PID des bridges avant de retourner "already_exists".

### 11. CSS bar-graph invisible (flex:1 dans flex-direction:column)

**Symptôme** : Toutes les barres graphes (VU, CPU, RAM, USB) sont noires/invisibles.

**Cause** : `.bar-graph { flex: 1 }` dans un parent `flex-direction: column` contrôle la **hauteur**, pas la largeur. La largeur tombe à 0.

**Solution** : `width: 100%` au lieu de `flex: 1` sur `.bar-graph` et `.vu-meter`.

### 12. CSR dongle (UD100) ne supporte pas power off via BlueZ

**Symptôme** : `bluetoothctl power off` retourne "succeeded" mais l'adaptateur reste powered.

**Cause** : Le chipset Cambridge Silicon Radio ne supporte pas le HCI power off.

**Solution** : Utiliser `rfkill block <index>` pour forcer l'arrêt au niveau kernel.

---

## Fichiers de configuration sur le Pi

| Fichier | Rôle |
|---------|------|
| `/etc/phonon/stage.yaml` | Config du Stage (bind_address, port, log_level) |
| `/var/lib/phonon/standalone.conf.json` | Mappings persistés |
| `/var/lib/phonon/.config/wireplumber/wireplumber.conf.d/90-phonon-bluetooth.conf` | Désactive WP bluez5 |
| `/var/lib/phonon/.config/systemd/user/phonon-stage.service` | Service user systemd |
| `/etc/dbus-1/system.d/phonon-bluetooth.conf` | Policy D-Bus BlueZ |
| `/etc/dbus-1/system.d/bluealsa.conf` | Policy D-Bus bluealsa |
| `/etc/systemd/system/phonon-bt-agent.service` | Agent BT permanent |
| `/etc/systemd/system/phonon-bt-unblock.service` | rfkill unblock au boot |
| `/etc/sudoers.d/phonon` | rfkill + hciconfig sans mot de passe |
| `/opt/phonon/bt-agent.py` | Script agent Python |
| `/boot/firmware/config.txt` | `dtoverlay=disable-bt` (désactive BT onboard Pi) |

---

## Services systemd

| Service | Type | Rôle |
|---------|------|------|
| `user@995.service` (phonon) | System | Session user pour PipeWire |
| `phonon-stage.service` | User (phonon) | FastAPI daemon |
| `bluetooth.service` | System | BlueZ daemon |
| `bluealsa.service` | System | BlueALSA A2DP bridge (-p a2dp-source -p a2dp-sink) |
| `phonon-bt-agent.service` | System | Agent BT auto-accept |
| `phonon-bt-unblock.service` | System (oneshot) | rfkill unblock au boot |
| `pipewire.service` | User (phonon) | Audio server |
| `wireplumber.service` | User (phonon) | Session manager |
| `avahi-daemon.service` | System | mDNS-SD |

---

## Hardware testé

| Device | Type | Interface | Usage |
|--------|------|-----------|-------|
| Raspberry Pi 3B | Host | — | Stage standalone |
| UD100 Sena (CSR) | Dongle BT USB | HCI, BlueZ | Transmitter (enceintes) |
| ASUS BCM20702A0 | Dongle BT USB | HCI, BlueZ | Receiver (téléphones) |
| JBL Xtreme 3 | Enceinte BT | A2DP SBC | Sortie audio |
| Sennheiser Sport TW | Ecouteurs BT | A2DP aptX | Sortie audio |
| Smartphone "1337" | Source audio | A2DP SBC | Entrée audio |
| bcm2835 Headphones | Sortie jack 3.5mm | ALSA | Sortie monitoring |

---

## Tests

- **85 tests unitaires + intégration** (pytest)
- **Coverage : 73%**
- Lint : ruff (clean)
- Type check : mypy strict (clean)
- CI : GitHub Actions (ruff + mypy + pytest + coverage gate 70%)

---

## Prochaines étapes

- [ ] VU meters avec vrais peaks (client PipeWire natif)
- [ ] DSP : mod-host + plugins LSP (égaliseur, limiteur, compresseur)
- [ ] Profiles DSP YAML par type d'enceinte
- [ ] Scènes (sauvegarder/restaurer des configurations de routing)
- [ ] AES67 bridge inter-Stage
- [ ] Controller daemon (étape 2)
- [ ] Console web complète (étape 9)
