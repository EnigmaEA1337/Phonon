# CLAUDE.md — Contexte Phonon pour Claude Code

> Ce fichier est lu automatiquement par Claude Code au démarrage de chaque session. Il contient le contexte minimal nécessaire pour intervenir efficacement sur le projet Phonon.

---

## Identité du projet

**Phonon** est un routeur audio distribué multi-Stages destiné à un usage personnel mobile (style flight case home-cinéma + soirée). Le nom est emprunté à la physique : un phonon est un quantum d'énergie acoustique (du grec φωνή — phōnḗ — "voix, son").

- **Auteur / mainteneur** : Serge (Gap, France)
- **Statut** : projet exploratoire et pédagogique, pas un produit commercial
- **Public cible** : moi-même + intérêt académique pour PipeWire, AES67, PTP, OSC, MPD
- **Licence** : non décidée (par défaut : tous droits réservés tant que rien n'est publié)

## Mission du projet

Construire un **lab audio sur réseau IP** capable de :

1. Recevoir des sources hétérogènes (Spotify Connect via librespot/shairport-sync, Bluetooth A2DP via BlueZ + ofono, AirPlay via shairport-sync, MPD, CD Audio, mic, ligne, AES67)
2. Les router dynamiquement vers des sorties hétérogènes (SPDIF, USB-Bluetooth, AES67 stream) avec une **chaîne DSP par destination** (LSP plugins via `mod-host`)
3. Synchroniser plusieurs Stages via **PTP** (IEEE 1588) et transporter l'audio en **AES67** entre eux
4. Être pilotable depuis **n'importe quelle Console web** (PC, smartphone, tablette) — voire plusieurs simultanément

C'est un terrain pour **apprendre par la pratique** : chaque techno (PipeWire, SystemD, BlueZ DBus, AES67, mDNS, OSC, FastAPI, SQLite WAL, mDNS-SD) est utilisée volontairement plutôt que d'aller au plus simple.

---

## Architecture (résumé)

Phonon suit un pattern **Stage / Controller / Console** inspiré d'UniFi Network et Home Assistant :

```
┌─ STAGE(s) ────────────────────────┐    ┌─ CONTROLLER ─────┐    ┌─ CONSOLE(s) ────┐
│                                   │    │                  │    │                  │
│ phonon-stage (FastAPI + zeroconf) │←──→│ phonon-controllerd ←──→ │ navigateur web   │
│ pipewire / wireplumber            │    │ (FastAPI + WS    │    │ (claude.ai-style │
│ mod-host (LSP plugins)            │    │  + SQLite WAL)   │    │  React-ish HTML) │
│ pulseaudio-bluetooth (BlueZ)      │    │                  │    │                  │
│ aes67-daemon (futur)              │    │ Strip Graph      │    │ multi-instance,  │
│                                   │    │ Compiler         │    │ synchro via WS   │
└───────────────────────────────────┘    └──────────────────┘    └──────────────────┘
       data plane (audio AES67)              control plane             vue / pilotage
```

**Stage** : machine physique (Optiplex SFF, Raspberry Pi 3…) qui héberge des **capabilities** (entrées et sorties physiques + DSP). Autonome — si le Controller meurt, l'audio continue. Peut aussi tourner en **mode standalone** (pas de Controller du tout) avec une mini-UI locale pour faire du routing simple — voir section dédiée plus bas.

**Controller** : daemon central (un seul actif). Gère l'état global (strips, routing, scènes, profils DSP) en SQLite. Compile le **strip graph** en commandes de bas niveau pour chaque Stage concerné. Un seul singleton sur le réseau, reconnaissance via mDNS-SD.

**Console** : page web statique (HTML + CSS + vanilla JS) qui parle au Controller en REST + WebSocket. Plusieurs Consoles peuvent être ouvertes simultanément, leurs états sont synchronisés via le Controller.

Détails complets : voir `phonon-deploiement-v4.3.md` (référence canonique de l'architecture).

---

## Modèle Strip (cœur fonctionnel)

Une **strip** est l'objet métier central. 7 types coexistent :

| Type | Rôle | Couleur Cryogenic |
|------|------|-------------------|
| `source` | Entrée audio (Spotify, AirPlay, BT-In, mic, ligne) | vert `#34d399` |
| `output` | Sortie physique (SPDIF, UD100, AK1, BT-Out, AES67) | magenta `#e879f9` |
| `bus` | Regroupement intermédiaire avec DSP commun | cyan `#06b6d4` |
| `master` | Sommation finale (limiteur, dither, LUFS) | rouge `#dc2626` |
| `monitor` | Pré-écoute casque (PFL) | jaune `#fbbf24` |
| `vca` | Contrôle de gain groupé sans audio | violet `#a855f7` |
| `network` | Stream AES67 entrant ou sortant | indigo `#1e40af` |

Chaque strip a : entrée(s), inserts (chaîne DSP), profile DSP, sortie(s), gain, mute/solo/listen/edit. Les bus, master, vca sont configurables dynamiquement par l'utilisateur.

Patching dynamique : clic sur l'**en-tête** d'une strip → modale de sélection des entrées. Clic sur le **footer** → modale des sorties.

Modes globaux :
- **PLAY** : lecture, structure verrouillée, faders/mute/solo réactifs
- **EDIT** : reconfiguration possible (ajout strip, modification routing, profile DSP)
- **ADMIN** : configuration de l'infra (Stages, network, profiles, users, backups, logs) — distinct des modes de mixage

---

## Modes opérationnels d'un Stage

Un Stage peut tourner dans 3 modes :

| Mode | Contexte | UI utilisée | Source de vérité |
|------|----------|-------------|------------------|
| **ADOPTED** | Un Controller gère ce Stage | Console web complète | DB du Controller |
| **STANDALONE** | Pas de Controller (jamais adopté ou reset effectué) | Mini-UI locale du Stage | `standalone.conf.json` |
| **FALLBACK** | Adopté avant, mais Controller injoignable depuis > timeout | Mini-UI locale (banner d'alerte) | `controller.conf.json` (dernière connue) |

### Algorithme de boot d'un Stage

```python
def boot():
    if controller_visible_via_mdns(timeout=10s):
        # Mode adopté
        config = load("controller.conf.json")
        mode = ADOPTED
    else:
        if exists("controller.conf.json"):
            # On avait été adopté, mais Controller absent → fallback
            config = load("controller.conf.json")
            mode = FALLBACK
            ui_show_banner("Controller absent — mode dégradé. Reset pour passer en standalone.")
        else:
            # Vraiment standalone (jamais adopté ou reset effectué)
            config = load("standalone.conf.json", default={})
            mode = STANDALONE
```

### Mini-UI locale du Stage (mode standalone)

Servie par `phonon-stage` lui-même sur `http://<stage>.phonon.local:8401/standalone`. Volontairement minimale, pas de DSP, pas de plugin-window, pas de Player Bar. Elle expose :
- Liste des **capabilities locales** (entrées et sorties physiques détectées)
- Liste des **autres Stages visibles** sur le réseau via mDNS-SD
- Un **patch bay simple** : "envoyer cette source → vers ce Stage / cette sortie"
- Réglages par mapping : gain, pan, mute (pas de profile DSP, pas de chaîne LSP)
- État : sample rate, latence PTP, paquets perdus, état des liens

Limites volontaires de la v1 standalone :
- Pas de DSP (pas de plugin LSP chargé)
- Pas de scènes, pas de profils, pas de groupes
- Maximum **8 mappings simultanés** par Stage (sécurité, simplicité)
- Pas de garantie stricte de latence sans Controller orchestrateur PTP — best-effort

Cas d'usage typiques :
- Test rapide avec 2 Pi sur table (sans booter le Controller)
- Mobilité légère : 2 Pi dans un sac, mini-switch, bridge BT → carte son d'un Pi à l'autre
- Backup d'urgence si le Controller plante en cours d'événement (la mini-UI reste accessible même quand un Controller est ou était présent)

### Workflow de reset (FALLBACK → STANDALONE)

Quand un Stage est en FALLBACK et que l'utilisateur clique "Reset → mode standalone" :

1. Confirmation dans la mini-UI ("Tu vas perdre la conf Controller actuelle. Elle sera archivée et restituée à la prochaine adoption.")
2. `controller.conf.json` est **renommé** en `pending-archive.json` (avec timestamp d'archivage)
3. Les mappings AES67 en cours sont stoppés, capabilities libérées
4. `standalone.conf.json` est chargé (vide au premier reset)
5. Le Stage bascule en `STANDALONE`, mini-UI pleinement éditable
6. **`pending-archive.json` reste sur le Pi**, jamais écrasé tant que le Controller d'origine ne réapparaît pas

### Workflow de retour (réapparition d'un Stage divergent)

Quand un Stage en STANDALONE rejoint le réseau d'un Controller qui le connaît déjà :

1. Stage s'annonce via mDNS-SD avec un flag `dirty` dans son hello :
   ```json
   { "stage_id": "stage-7c1944", "adopted_by": "core-3f9a1b8e",
     "current_mode": "STANDALONE", "has_pending_archive": true,
     "archived_at": "2026-04-25T18:42:00Z" }
   ```
2. Controller détecte la divergence par UUID match dans sa DB
3. Console affiche notification dans le panneau Stages avec **3 choix** :

| Choix | Action |
|-------|--------|
| **Repousser ma conf** | Le Controller renvoie sa config officielle au Stage. Le Stage écrase `standalone.conf.json` (gardé comme `standalone.archive.json` au cas où) et réactive `pending-archive.json` comme `controller.conf.json`. Repasse en `ADOPTED`. |
| **Adopter la conf actuelle** | Le Controller récupère la config standalone du Stage via API et l'importe dans sa DB. La `standalone.conf.json` du Stage est promue en `controller.conf.json`. L'ancienne `pending-archive.json` est définitivement supprimée. Repasse en `ADOPTED`. |
| **Oublier ce Stage** | Le Controller archive sa propre config pour ce Stage dans la table `stages_archive`. Le Stage est retiré de la liste active mais continue à tourner en STANDALONE. Côté Stage rien n'est touché. |

### Archivage et suppression manuelle (côté Controller)

Dans **Admin > Archives**, l'utilisateur voit l'historique des Stages oubliés et peut :
- **Voir le détail** de la config archivée
- **Restaurer** un Stage (le re-déclarer actif si le Stage est encore joignable)
- **Supprimer définitivement** : double confirmation + saisie du nom du Stage à supprimer (pattern courant des opérations destructives)

### Stockage côté Stage (3 fichiers JSON)

```
/var/lib/phonon/
├── controller.conf.json     # config héritée du Controller (peut être absente)
├── standalone.conf.json     # config faite localement via mini-UI
└── pending-archive.json     # ancienne config Controller mise de côté lors d'un reset
```

Mode 0600, owner `phonon:phonon`. Voir SECURITY.md §3.6 pour les implications (présence potentielle de tokens d'auth dans ces fichiers).

### Stockage côté Controller (DB)

```sql
CREATE TABLE stages (
  id TEXT PRIMARY KEY,           -- 'stage-7c1944'
  hostname TEXT,
  ip TEXT,
  adopted_at TEXT,
  last_seen TEXT,
  mode TEXT,                     -- ADOPTED, STANDALONE, FALLBACK, DISCOVERED
  config_json TEXT,
  has_dirty_flag INTEGER DEFAULT 0,
  pending_archive_at TEXT
);

CREATE TABLE stages_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stage_id TEXT,
  archived_at TEXT,
  archived_reason TEXT,           -- 'oubli_manuel', 'decommissioning', etc.
  config_snapshot_json TEXT,
  archived_by TEXT
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  type TEXT,                      -- 'stage.adopted', 'stage.archived', 'stage.dirty_detected', etc.
  stage_id TEXT,
  details_json TEXT
);
```

### Principes de design à respecter

- **Pas de fusion magique** : c'est toujours OU l'une OU l'autre des configs qui est active sur un Stage
- **Pas de perte silencieuse** : aucun reset ne supprime jamais de données, tout est archivé
- **L'utilisateur décide explicitement** au retour à la maison (pas de "merge auto")
- **Symétrique** : Stage archive sa conf Controller au reset (`pending-archive`), Controller archive la conf du Stage à l'oubli (`stages_archive`), les deux côtés communiquent par contrat clair (`has_pending_archive`, `dirty_flag`)
- **La suppression définitive est manuelle** : pas de garbage collector qui efface l'historique automatiquement

---

## Hardware ciblé

### v1 — Mono-host

Toute l'infrastructure tourne sur **une seule machine** : Controller + Console + Stage Core + DSP, tout au même endroit.

| Rôle v1 | Machine | Spec |
|------|---------|------|
| Controller + Console + Stage + DSP (tout-en-un) | Dell OptiPlex 3070 SFF | Voir détails ci-dessous |

**Caractéristiques OptiPlex 3070 SFF (source Dell, regulatory model D11S, type D11S004)** :

- **Chipset** : Intel H370
- **Processeur supporté (selon CPU installé)** : Intel Core i5-9400 / i5-9500 (6 cores, 9 MB cache, 6 threads, 2.9-4.4 GHz, 65W) ou autres options 9th gen Coffee Lake Refresh. CPU réel à vérifier au boot avec `lscpu`.
- **Mémoire** : DDR4 non-ECC, 2666 MHz sur i5/i7 (2400 MHz sur i3/Pentium/Celeron). 2 slots DIMM, jusqu'à **32 GB max** (config typique : 8 ou 16 GB)
- **Stockage** : 1 baie 3.5"/2.5", 1 slot M.2 socket 3 pour SSD SATA/NVMe, 1 SATA 3.0, 1 SATA 2.0
- **Audio onboard** : Realtek ALC3234 HD Audio (utilisé par défaut, mais on s'en moque puisqu'on utilise X-Fi HD et AK1 en USB)
- **Graphics** : Intel UHD 630 (8th/9th gen) intégré CPU. Optionnel : carte additionnelle (AMD Radeon RX 550, R5 430, NVIDIA GT 730)
- **Réseau onboard** : Realtek RTL8111HSD-CG Gigabit Ethernet (10/100/1000), driver Linux `r8169`
- **Sans-fil optionnel** : Qualcomm QCA9377 / QCA61x4A ou Intel Wireless-AC 9560 (slot M.2 socket 1)
- **Slots PCIe** (SFF) : 1× PCIe x16 + 1× PCIe x1 + 1× M.2 SSD + 1× M.2 WiFi/BT
- **Ports USB** : 2× USB 3.1 Gen 1 + 2× USB 2.0 en façade, 2× USB 3.1 Gen 1 + 2× USB 2.0 à l'arrière (avec Smart Power On). Total 8 USB onboard.
- **Ports vidéo** : 1× DisplayPort 1.2, 1× HDMI 1.4 (3e port optionnel : VGA, DP, HDMI 2.0b)
- **Audio jack** : 1× Universal Audio Jack 3.5mm en façade, 1× Line-out à l'arrière
- **Alimentation** : 200W APFC Bronze ou Platinum
- **Dimensions** : 29 × 9.26 × 29.2 cm (H × W × D), 5.26 kg, volume 7.8 L
- **OS supportés** : Windows 10 (Home/Pro), **Ubuntu 18.04 LTS**, Neokylin (Chine). Ubuntu Studio 26.04 LTS fonctionne très bien (supporté de fait, drivers stables).
- **TPM** : 2.0 discret onboard

Sur cette machine on fait tourner :
- `phonon-controllerd` (FastAPI + SQLite WAL, sert aussi la Console statique sur `:8400`)
- `phonon-stage` (capabilities, mod-host, plugins LSP)
- PTP grandmaster (pas indispensable en mono-host mais ça prépare la v2)

Tous les périphériques sont branchés sur ce seul host.

**Audio I/O** :
- 1× X-Fi HD (SPDIF, sortie principale)
- 1× AK1 (mic XLR + monitoring casque)
- 2× UD100 Sena (dongles BT classiques HCI/USB → BlueZ, sorties BT A2DP vers JBL)
- 3× DG60 Avantree (récepteurs BT « tout-en-un » qui s'exposent en **carte son USB bidirectionnelle** côté Linux, pas en HCI BlueZ → utilisés en **entrée** par convention pour les téléphones invités). Le pairing se fait avec le bouton physique du dongle, le codec A2DP est négocié par le firmware du DG60 lui-même, et Linux voit juste un device ALSA avec capture + playback. Le côté playback du DG60 est techniquement utilisable (firmware pousse en BT vers une enceinte pairée tout seul) mais on ne s'en sert pas par défaut car on n'a aucun contrôle sur le pairing ni le codec — pour pousser de l'audio en BT depuis Phonon, on passe par les UD100 où BlueZ donne tout le contrôle.

**Cartes additionnelles dans l'OptiPlex 3070 SFF** :
- **Carte USB 3.0 PCIe** dans le slot PCIe x1 ou x16 : ajoute un contrôleur USB indépendant sur son propre bus PCIe — critique pour isoler les cartes son de l'isochronous des dongles BT
- **Carte SFP** (PCIe x1) : interface fibre/cuivre additionnelle, utile pour isolation électrique et préparation v2 (lien dédié au lab Phonon, séparé du trafic domestique)

**Distribution USB recommandée** (3 dongles BT classiques + 3 récepteurs BT-as-USB-audio + 2 cartes son = 8 périphériques USB) :

| Bus | Périphériques | Backend Linux | Pourquoi |
|-----|---------------|---------------|----------|
| Carte USB3 PCIe (bus dédié) | X-Fi HD, AK1 | ALSA/PipeWire | Audio isochronous → bande passante fiable, pas de concurrence |
| USB carte mère bus 1 | 2× UD100 (sorties BT JBL) | BlueZ HCI | BT tolère la concurrence (buffers internes) |
| USB carte mère bus 2 | 3× DG60 (entrées BT téléphone) | ALSA/PipeWire (carte son USB) | Idem, comportement identique aux cartes son |

Logique : les **cartes son** vont sur le bus PCIe le plus dédié (l'isochronous est sensible aux glitches). Les **UD100** sur la carte mère parce qu'ils ont leurs propres mécanismes BT de resync et tolèrent mieux le partage. Les **DG60** côtoient les UD100 car ils se comportent comme des cartes son ALSA standard, pas comme du BT HCI.

**Speakers** : 4× JBL Pulse 3 (Connect+), 2× JBL Xtreme 4 (PartyBoost), HC system

**Réseau** :
- UniFi USW-16-POE comme switch principal du lab (vérifier les ports SFP+ disponibles)
- GL.iNet Slate 7 Pro (BE3600) pour le Wi-Fi mobile et l'isolation des invités
- Lab isolé du réseau domestique : `10.100.0.0/24`, multicast AES67 sur `239.69.0.0/16`, PTP domain 0
- Si SFP+ utilisé, brancher l'OptiPlex en fibre sur un port SFP+ libère un RJ45 et garantit que le lien Phonon ne rivalise pas avec d'autre trafic

**Règles udev obligatoires** : après chaque reboot, les `hci0..hciN` peuvent être permutés. Il faut donc des règles udev persistantes pour donner des noms stables aux dongles BT et aux cartes son. À versionner dans `deploy/udev/99-phonon.rules` :

```
# Exemple — à adapter avec les vraies adresses MAC des dongles
SUBSYSTEM=="hci", ATTR{address}=="aa:bb:cc:dd:ee:01", SYMLINK+="phonon-bt-out-pulse3-1"
SUBSYSTEM=="hci", ATTR{address}=="aa:bb:cc:dd:ee:11", SYMLINK+="phonon-bt-in-invite-1"
```

Sans ça, le routing BT n'est pas reproductible d'un boot à l'autre.

### v2 — Distribué (pour plus tard)

L'architecture Stage / Controller / Console est conçue pour le distribué dès le départ — c'est juste qu'on ne l'exerce pas en v1. Quand on passera au multi-host, le hardware déjà disponible :

| Rôle v2 | Machine | Spec |
|---------|---------|------|
| Stage DSP (chaînes lourdes en parallèle) | Dell OptiPlex 3040 SFF | Voir détails ci-dessous |
| Controller + Stage Core (sources principales) | Dell OptiPlex 3070 SFF | Garde le Controller et l'acquisition des sources |
| Stages mobiles | Raspberry Pi 3 (B ou B+) + POE→USB | Cortex-A53 quad-core 1.2 GHz ARMv8 64-bit, 1 GB RAM, USB 2.0 ×4, WiFi/BT 4.1 intégrés. Modèle B+ : Gigabit Ethernet (limité à ~300 Mbps via USB 2.0). Déploiements légers, mode standalone activable, peut faire du DSP léger (EQ, gain, pan) si besoin. **Sortie HDMI utilisée comme carte son numérique** via extracteur HDMI→SPDIF/RCA (15-25 €) au lieu d'une carte son USB dédiée : qualité numérique pure (I²S → HDMI), zéro perte si on garde le SPDIF, et économie d'un port USB (utile vu que les 4 USB partagent un bus 480 Mbps avec l'Ethernet via le LAN9514). Configuration recommandée dans `/boot/firmware/config.txt` : `hdmi_force_hotplug=1`, `hdmi_drive=2`, `hdmi_blanking=0` (à appliquer par `install.sh` au provisioning). |

**Caractéristiques OptiPlex 3040 SFF (source Dell, regulatory model D11S)** :

- **Chipset** : Intel H110 (gen plus ancienne que le 3070)
- **Processeur supporté** : Intel 6th gen (Skylake) — Core i5 Quad Core 65W, Core i3 Dual Core, Pentium ou Celeron Dual Core. Socket LGA 1151 (Socket H4). À noter : le 3040 ne supporte officiellement que jusqu'à i5 quad-core ; si un i7 a été monté, c'est probablement par upgrade compatible socket. **CPU réel à vérifier au boot avec `lscpu`.**
- **Mémoire** : DDR3L-1600, 2 slots DIMM, **jusqu'à 16 GB max** (limite plus basse que le 3070)
- **Réseau onboard** : Realtek RTL8111HSD Gigabit Ethernet
- **Ports USB** : 4× USB 3.0 + 4× USB 2.0 = 8 ports total
- **Ports vidéo** : DisplayPort 1.2 + HDMI 1.4 (+ VGA optionnel)
- **Alimentation** : 180W (plus faible que 3070)
- **OS supportés** : Windows 10 (Home/Pro), Windows 8.1, Windows 7 SP1, **Ubuntu**, Neokylin (Chine). Ubuntu Studio 26.04 LTS fonctionne (drivers Skylake stables depuis longtemps).

Bénéfices visés en v2 :
- Soulager le bus USB en répartissant les dongles BT entre les deux Optiplex
- DSP lourd (chaînes LSP de 8+ plugins) sur le 3040 en parallèle de l'acquisition sur le 3070
- Bridge AES67 entre Stages (les deux peuvent dialoguer en fibre via leurs SFP)
- Mobilité légère (sac à dos, 2 Pi + mini-switch) avec mini-UI standalone des Pi

**Note sur le choix v2** : on garde le 3070 (i5 9th gen) comme Core (meilleure perf single-thread + mémoire DDR4 jusqu'à 32 GB, important pour la latence audio temps réel et la cache de profiles DSP) et on déplace le DSP lourd sur le 3040 (qui a moins de RAM mais peut paralléliser sur plusieurs threads). À ré-évaluer une fois qu'on aura les benchmarks réels.

Le code v1 doit donc être écrit **comme si on était déjà multi-host** (séparation claire `phonon-stage` vs `phonon-controllerd`, communication uniquement par REST/WS/mDNS, pas de raccourcis "même process"), même si en pratique en v1 les deux daemons tournent sur la même machine.

---

## Stack technique

**Côté Stage** (Python 3.12+) :
- `fastapi` + `uvicorn` — API REST/WebSocket
- `zeroconf` — annonce mDNS-SD `_phonon-stage._tcp.local`
- `python-osc` — OSC bidirectionnel pour MIDI controller learn
- `pydbus` — interface BlueZ pour gestion BT
- `pipewire` (système, pas Python) + `wireplumber` policy custom
- `mod-host` — chargeur de plugins LV2 (LSP suite)
- `aes67-daemon` (futur) — Ravenna/AES67 stack

**Côté Controller** (Python 3.12+) :
- `fastapi` + `uvicorn` (workers=1, le Controller est singleton)
- `sqlite3` avec `journal_mode=WAL` — état persistant
- `zeroconf` — discovery des Stages
- WebSocket pour push d'état vers les Consoles

**Côté Console** :
- HTML5 + CSS3 + vanilla JS (pas de framework pour le mockup actuel)
- Si on passe à un build, candidat : Vite + React/Preact + TypeScript

**OS** : Ubuntu Studio 26.04 LTS "Resolute Raccoon" (sortie 17 avril 2026, supportée jusqu'avril 2029) sur les Optiplex avec kernel lowlatency, JACK/PipeWire 1.4+ pré-configurés, systemd 259 avec cgroup v2, audio config réécrit en Python GTK4/Qt6. Raspberry Pi OS 64-bit Bookworm (Debian 12) sur les Pi avec PipeWire installé via backports. Optionnellement, sur les Optiplex avec CPU récents (i5 9th gen, i7 6th gen), activer les paquets `x86-64-v3` pour de meilleures perfs CPU.

---

## Conventions de naming

Strict, ne pas dévier :

- Daemons SystemD : `phonon-controllerd`, `phonon-stage`, `phonon-cli`
- Hostnames : `core-<UUID>`, `stage-<8 hex chars>`, `controller-<n>`
- mDNS service types : `_phonon-stage._tcp.local`, `_phonon-controller._tcp.local`
- Domaine local : `*.phonon.local`
- Port API REST/WS : `8400` (Controller) et `8401` (Stages)
- Port OSC : `9000` (in), `9001` (out)
- Configs : `/etc/phonon/` (controller.yaml, stage.yaml)
- DB : `/var/lib/phonon/state.db` (côté Controller uniquement)
- Logs : `/var/log/phonon/` + journald
- Snake_case dans les noms Python (`strip_graph_compiler.py`)
- kebab-case dans les filenames Markdown (`phonon-deploiement-v4.3.md`)
- camelCase dans le JS du frontend
- UPPERCASE dans les constantes Python (`STRIP_TYPES = ["source", "output", ...]`)

---

## Layout du dépôt (cible, pas encore en place)

```
phonon/
├── stage/                    # Stage Agent (Python)
│   ├── pyproject.toml
│   ├── src/phonon_stage/
│   │   ├── main.py
│   │   ├── api/              # FastAPI routers
│   │   ├── audio/            # PipeWire / mod-host wrappers
│   │   ├── bluetooth/        # BlueZ via pydbus
│   │   ├── discovery/        # mDNS-SD avec zeroconf
│   │   └── osc/              # MIDI/OSC bridge
│   └── tests/
├── controller/               # Controller daemon (Python)
│   ├── pyproject.toml
│   ├── src/phonon_controller/
│   │   ├── main.py
│   │   ├── api/              # FastAPI routers + WebSocket
│   │   ├── db/               # SQLite schema + migrations
│   │   ├── compiler/         # Strip Graph Compiler
│   │   ├── discovery/        # Suivi des Stages
│   │   └── ws/               # Pub/sub WebSocket
│   └── tests/
├── console/                  # Frontend web
│   ├── index.html            # Console v4.x
│   ├── src/
│   │   ├── strips/
│   │   ├── plugins/
│   │   ├── player/
│   │   └── settings/
│   └── assets/
│       ├── phonon-logo.svg
│       └── styles/
├── profiles/                 # Profils DSP YAML
│   ├── jbl_pulse3.yaml
│   ├── jbl_xtreme4.yaml
│   ├── casque_neutre.yaml
│   ├── hc_linear.yaml
│   └── master_glue.yaml
├── deploy/                   # Provisioning des Stages et du Controller (tout natif, pas de Docker)
│   ├── install.sh            # Script d'install one-liner : users, venvs, systemd, configs
│   ├── systemd/              # Units .service (phonon-stage, phonon-controllerd)
│   ├── udev/                 # Règles udev pour stabiliser les noms des dongles USB
│   ├── ansible/              # Playbooks de provisioning multi-hôte (v2)
│   └── pi-image/             # Build script SD card RPi3 avec Phonon préinstallé
├── docs/
│   ├── phonon-deploiement-v4.3.md   # ARCHITECTURE CANONIQUE
│   ├── CLAUDE.md             # ce fichier
│   ├── SECURITY.md
│   ├── osc-dictionary.md
│   ├── api-reference.md
│   ├── test-plans/           # Test plans manuels niveau 4 (un .md par scénario)
│   │   ├── tp-01-stage-bootstrap.md
│   │   ├── tp-04-aes67-bridge-2-pi.md
│   │   └── ...
│   └── images/
└── README.md
```

---

## Workflow de dev

**Lancer un Stage en local** (dev) :
```bash
cd stage/
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
phonon-stage --config dev/stage-local.yaml
```

**Lancer le Controller** :
```bash
cd controller/
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
phonon-controllerd --config dev/controller-local.yaml
```

**Lancer la Console** (statique pour le mockup) :
```bash
cd console/
python -m http.server 8080
# Ouvrir http://localhost:8080/index.html
```

**Tests** :
```bash
# Dans chaque sous-projet (stage/, controller/) :
pytest -v
pytest --cov=phonon_stage --cov-report=html
```

**Linting / formatting** :
```bash
ruff check . --fix
ruff format .
mypy src/
```

Cibler **Python 3.12+**. Pas de support back-compat 3.10/3.11 pour un projet perso, on prend les nouveautés.

---

## Style de code

- **Type hints** partout côté Python (`mypy --strict` à terme)
- **f-strings** pour le formatting, jamais `.format()` ou `%`
- **`pathlib.Path`** au lieu de `os.path`
- **`async/await`** systématique côté FastAPI
- **`structlog`** pour les logs (JSON + correlation IDs)
- **`pydantic`** v2 pour tous les modèles (request, response, config)
- **Imports** : groupes séparés par ligne vide (stdlib / third-party / local)
- **Docstrings** : style Google avec sections `Args:`, `Returns:`, `Raises:`
- **`# noqa`** doit être justifié en commentaire si utilisé

Côté JS (Console) : pas encore de framework, vanilla. Si on passe à TypeScript : `strict: true`, pas de `any`.

---

## Stratégie de tests

Phonon manipule du matériel (cartes son, dongles BT, multicast réseau, PTP), ce qui rend les tests délicats — on ne peut pas tout tester en CI sans hardware. La stratégie est une **pyramide à 4 niveaux**, du plus rapide/le plus utile au plus lent/le plus situationnel.

### Niveau 1 — Tests unitaires (rapides, en CI)

**Objectif** : valider la logique pure, pas les I/O. Pas de réseau, pas de DB, pas de subprocess.

**Cibles** :
- Le **Strip Graph Compiler** (Controller) : prend un état de strips + routing, produit la liste de commandes pour les Stages. Logique pure → testable à 100 %.
- Les **modèles Pydantic** : validation, sérialisation, edge cases.
- Les **fonctions de calcul** : conversion dB↔linéaire, mapping fader position↔gain, calcul latence AES67 en fonction du packet time, etc.
- L'**audio safety guard** (limiteur master non bypassable, gain master plafonné à 0 dB) : tests qui vérifient qu'on ne peut PAS désactiver le limiteur, qu'on ne peut PAS pousser le master au-delà de 0 dB.

**Stack** : `pytest`, `pytest-cov`, `hypothesis` (property-based testing pour les calculs).

**Volume cible** : 100-200 tests, exécution < 5 secondes.

### Niveau 2 — Tests d'intégration sans hardware (rapides, en CI)

**Objectif** : tester l'API REST/WS, la DB, la communication entre composants. Toujours sans matériel.

**Cibles** :
- Endpoints REST du Controller (`POST /strips`, `GET /stages`, etc.) et du Stage (`GET /capabilities`, `POST /audio/route`, etc.)
- **Workflow d'adoption complet** : création stage → adoption → déconnexion → fallback → reset → réapparition → archivage. C'est la phase où les tests sont les plus critiques.
- Persistance SQLite : reload après "redémarrage", migrations
- WebSocket : réception d'événements, reconnexion

**Astuces de mock** :
- `TestClient` de FastAPI ou `httpx.AsyncClient` → pas besoin de vrai serveur
- SQLite en mémoire (`sqlite:///:memory:`) → DB jetable, ultra rapide
- **Pattern Backend abstrait** : créer une interface `phonon_stage.audio.PipeWireBackend` avec deux implémentations :
  - `RealPipeWireBackend` (en prod, parle au vrai PipeWire)
  - `FakePipeWireBackend` (en test, retourne des devices fictifs et accepte les commandes sans rien faire)
  Idem pour `BluetoothBackend` (Real/Fake), `AES67Backend` (Real/Fake). Le choix de l'implémentation se fait par injection de dépendance au démarrage du daemon (real par défaut, fake en test).
- Mock zeroconf : `python-zeroconf` permet d'utiliser une instance locale sans vrai multicast.

**Stack additionnelle** : `httpx`, `respx` (mock HTTP outbound), `freezegun` (manipuler le temps pour tester les timeouts).

**Volume cible** : 50-100 tests, exécution < 30 secondes.

### Niveau 3 — Tests d'intégration avec hardware (lent, à la main avant un merge)

**Objectif** : valider que le `RealBackend` parle vraiment au vrai PipeWire / au vrai BlueZ. Ces tests tournent uniquement **sur une machine avec le matériel branché** (le 3070 ou un Pi).

**Cibles** :
- Découverte effective des cartes son
- Création d'un link PipeWire entre deux ports, vérification via `pw-link -l`
- Pairing BT scriptable (avec un device de test, idéalement un dongle dédié)
- Démarrage/arrêt d'un stream AES67 et vérification multicast via `tcpdump` ou `iperf`

**Approche pratique** :
- Marquer ces tests `@pytest.mark.hardware` pour les exclure des runs CI
- Lancement explicite : `pytest -m hardware`
- Documentation : « avant chaque merge sur main, lancer ces tests sur le 3070 »

**Volume cible** : 20-30 tests, lents (5-30 sec chacun à cause du matériel).

### Niveau 4 — Tests end-to-end (manuels, mais checklists)

**Objectif** : valider des scénarios complets utilisateur. Pas automatisés (pas raisonnable), mais documentés en checklist.

**Format** : un dossier `docs/test-plans/` avec un fichier Markdown par scénario. Exemples :

```
docs/test-plans/
├── tp-01-stage-bootstrap.md
├── tp-02-controller-discovery.md
├── tp-03-adoption-cycle.md
├── tp-04-aes67-bridge-2-pi.md       # le cas d'usage canonique : 2 Pi qui se bridgent
├── tp-05-bluetooth-routing.md
├── tp-06-dsp-chain.md
└── tp-99-soiree-blanc.md            # le grand test : routing complet + scènes
```

Chaque test plan contient : pré-requis matériels, étapes à dérouler, critères de succès. **Pas un substitut aux tests automatisés mais un complément** pour ce qui demande de l'audio audible et un humain qui écoute.

### Configuration CI (GitHub Actions)

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install
        run: |
          pip install -e stage/[dev]
          pip install -e controller/[dev]
      - name: Lint + format
        run: |
          ruff check .
          ruff format --check .
      - name: Type check
        run: mypy stage/src controller/src
      - name: Unit + integration tests
        run: pytest -m "not hardware" --cov=phonon_stage --cov=phonon_controller
      - name: Coverage gate
        run: coverage report --fail-under=70
```

**Cibles CI strictes** :
- Lint + format Ruff : 100 % obligatoire
- Type check (mypy strict) : 100 % obligatoire à terme
- Tests niveau 1+2 : tous doivent passer
- Coverage : 70 % minimum sur le code métier (compiler, workflows, validation). Le code purement I/O est exclu (testé en niveau 3 manuellement).

### Conséquence pour le design du code

Le besoin de testabilité oriente fortement l'architecture, dès le début :

1. **Pattern Backend abstrait obligatoire** pour tout ce qui touche au matériel ou au système (PipeWire, BlueZ, AES67, mDNS, SSH provisioning). Une interface + une implémentation Real + une implémentation Fake. L'implémentation est injectée au démarrage du daemon, pas hardcodée.

2. **Pas de `subprocess.run()` éparpillé dans le code métier**. Si on doit appeler `pw-cli` ou `bluetoothctl` ou `ip link`, ça passe par un module dédié (`phonon_stage.audio.pipewire_cli` par exemple) qui peut être mocké en bloc.

3. **Time injection** : pas de `datetime.now()` dans le code métier. Toujours `clock.now()` où `clock` est injecté → testable avec `freezegun`.

4. **Aucun import de `pyzeroconf` directement dans le code métier**. Toujours via une couche `phonon_stage.discovery` qui expose des méthodes haut niveau (`announce_service`, `browse_services`).

Ces contraintes ajoutent ~10-15 % de code (les interfaces + les Fake) mais permettent ensuite d'écrire des tests rapides et fiables, et facilitent les refactorings ultérieurs.

### Volume de tests par étape de dev

À chaque étape, on vise au minimum :

| Étape | Tests niveau 1 | Tests niveau 2 | Tests niveau 3 | Test plans niveau 4 |
|-------|---------------|---------------|---------------|---------------------|
| 1. Stage fondations | 5-10 | 5-10 | 0 | TP-01 |
| 2. Controller fondations | 10-15 | 10 | 0 | TP-02 |
| 3. Adoption / standalone / fallback | 30+ | 20+ | 0 | TP-03 |
| 4. Audio basique PipeWire | 10 | 10 | 5 | — |
| 5. Routing audio local | 15 | 15 | 5 | — |
| 6. Bluetooth | 15 | 15 | 5 | TP-05 |
| 7. AES67 + bridge inter-Stage | 20 | 15 | 5 | TP-04 |
| 8. DSP mod-host + LSP | 10 | 10 | 5 | TP-06 |
| 9. Console web | minimal | playwright si voulu | manuel | TP-99 |

Les tests sont écrits **en même temps** que la feature, pas après. Aucune fonctionnalité n'est mergée sur `main` sans ses tests des niveaux 1 et 2 qui passent.

---

## Étapes de développement

Le dev se fait par paliers incrémentaux. À chaque étape on a un système qui **fonctionne et qui se teste**, même si limité. On ne passe à l'étape suivante qu'après validation manuelle du palier précédent. Pas de "je code pendant 3 semaines puis je teste tout".

### Ordre fixe des étapes

| Étape | Sujet | Cible matérielle primaire |
|-------|-------|--------------------------|
| **1** | **Stage Agent fondations** (health, capabilities, mDNS, systemd) | **Pi 3B `stage-x01`** d'abord, puis 3070 pour valider portabilité |
| 2 | Controller daemon fondations (DB, discovery des Stages) | 3070 |
| 3 | Workflow d'adoption (adopt / fallback / standalone / archive) + mini-UI standalone | Pi `stage-x01` + 3070 + un 2e Pi si dispo |
| 4 | Audio basique (énumération PipeWire, devices stables via udev) | Pi (DG60 + UD100) puis 3070 (X-Fi HD + AK1) |
| 5 | Routing audio bout-en-bout local (source → output, sans DSP) | 3070 d'abord (plus simple, devices riches), puis Pi |
| 6 | Bluetooth I/O (BlueZ pour UD100, ALSA pour DG60) | Pi (cas d'usage canonique) |
| 7 | AES67 + bridge inter-Stage (le scénario 2 Pi qui se bridgent) | 2 Pi simultanés |
| 8 | DSP avec mod-host + plugins LSP | 3070 (CPU et RAM nécessaires) |
| 9 | Console web complète (mockup HTML branché sur l'API) | 3070 |

### Pourquoi commencer par le Pi et pas le 3070

L'étape 1 cible les Pi en premier intentionnellement :

1. **Hardware le plus contraint** : arm64, 1 Go RAM, USB partagé avec Ethernet via le LAN9514. Si le code tourne propre dessus, il tournera partout. L'inverse n'est pas vrai (du code qui marche bien sur un x86_64 8 Go peut planter ou être lent sur un Pi).

2. **Validation packaging arm64 d'emblée** : on vérifie tout de suite que les wheels Python disponibles, le venv, les services systemd, fonctionnent sur Raspberry Pi OS Bookworm 64-bit. Pas de mauvaise surprise plus tard.

3. **Le `install.sh` doit être multi-arch dès l'origine** : x86_64 (Optiplex Ubuntu Studio) + arm64 (Pi Bookworm). Tester sur la cible la plus exotique d'abord oblige à ne pas hardcoder de chemins ou de paquets x86-spécifiques.

4. **Le 3070 viendra juste après comme test de portabilité** : même `install.sh`, même unit systemd, juste un OS et une arch différents. Si l'étape 1 passe sur le Pi, le 3070 sera trivial.

### Workflow de validation à chaque étape

À chaque étape, le cycle est :

1. Claude Code livre le code dans une branche dédiée (ex: `phase-1-stage-foundations`)
2. L'utilisateur clone/pull, déploie sur la cible matérielle, suit la checklist de validation
3. Tests automatisés niveau 1+2 doivent passer (CI verte)
4. Tests manuels niveau 3 (avec hardware) sur la cible réelle
5. Test plan niveau 4 si applicable (ex: TP-01 pour l'étape 1)
6. ✅ → merge sur `main`, on passe à l'étape suivante
7. ❌ → correction, retour en étape 2

Aucune étape n'est skippée même si elle paraît "facile". Aucune étape n'est mergée sans ses tests qui passent.

### Contraintes spécifiques par étape

**Étape 1** :
- Pas de logique d'adoption (vient en étape 3)
- Pas de mini-UI HTML (vient en étape 3)
- Mode `STANDALONE` par défaut, hardcodé
- Endpoints minimaux : `GET /health`, `GET /capabilities`, mDNS-SD
- Tester sur Pi `stage-x01` ET sur 3070 avant de merger

**Étape 4** :
- Le DG60 est traité comme une carte son ALSA standard, pas comme un device BT (cf. point 6 de "Choses à connaître")
- L'UD100 est traité via BlueZ uniquement, jamais en mélange avec ALSA
- Les noms udev doivent être stables avant cette étape (règles `99-phonon.rules` finalisées en étape 1 ou 4)

**Étape 7** :
- C'est l'étape qui valide le **cas d'usage canonique** : 2 Pi standalone qui se bridgent en AES67 sans Controller
- TP-04 (test plan niveau 4) est obligatoire pour valider cette étape
- Si AES67 n'est pas robuste à l'étape 7, on **retarde l'étape 8** (DSP) pour stabiliser AES67 d'abord

### État courant

Étape en cours : **Étape 1 (Stage Agent fondations)**. Cible : Pi 3B `stage-x01`.

---

## Documents canoniques

À lire en premier pour comprendre quelque chose en profondeur :

| Document | Rôle |
|----------|------|
| `docs/phonon-deploiement-v4.3.md` | Architecture, hardware, réseau, stack, phases de déploiement, OSC, profiles YAML, glossaire — **document de référence** |
| `docs/SECURITY.md` | Modèle de menace, hardening, secrets |
| `docs/osc-dictionary.md` | Toutes les adresses OSC supportées |
| `docs/api-reference.md` | API REST + WS du Controller et Stages |
| `console/index.html` | Mockup actuel de la Console (référence pour le design) |

En cas de conflit entre ce que dit `phonon-deploiement-v4.3.md` et un autre document : **le déploiement v4.3 fait foi**, l'autre doc est en retard.

---

## Choses à connaître pour ne pas se planter

1. **Le Controller est un singleton.** Ne jamais lancer deux Controllers sur le même réseau. La détection mDNS doit être stricte ; en cas de conflit, le second refuse de démarrer.

2. **Les Stages sont autonomes.** Si le Controller meurt, l'audio doit continuer à tourner. Toute modification de routing nécessite cependant un Controller actif.

3. **AES67 = wired only.** Le Wi-Fi est interdit pour le data plane audio. Seul le control plane (REST/WS/OSC) peut passer par Wi-Fi.

4. **Multicast non routé** entre subnets dans la version actuelle. Tout doit être sur le même VLAN/subnet pour AES67.

5. **PTP grandmaster** : un seul sur le réseau. En général c'est le Stage Core. Vérifier `pmc` ou équivalent.

6. **Bluetooth standard A2DP, mais deux familles de dongles distinctes.**

    - **UD100 Sena** : dongle BT classique HCI USB. Géré par BlueZ via DBus (`pydbus` ou `dbus-next`). Pairing scriptable, codec A2DP négocié côté Linux (SBC obligatoire, AAC/aptX si dispo). Stack : BlueZ + module BT PipeWire / WirePlumber policy BT.
    - **DG60 Avantree** : récepteur BT « tout-en-un » qui s'expose comme **carte son USB** côté Linux (`aplay -l`). Le pairing se fait avec le bouton physique sur le dongle, le codec A2DP est négocié par le firmware du DG60 lui-même, Linux ne voit qu'un device ALSA classique avec un flux audio dedans. Stack : ALSA / PipeWire, **pas BlueZ**.

    Pas de Connect+ JBL ni autre protocole proprio à gérer côté Phonon. Pour le multi-room entre enceintes, on s'appuie sur PartyBoost (JBL Xtreme 4) qui est géré côté hardware par les enceintes elles-mêmes. Le BT intégré des Pi 3B (chip Cypress sur UART, `hci0`) est désactivé via `dtoverlay=disable-bt` dans `/boot/firmware/config.txt` pour éviter la confusion (on ne s'appuie que sur les dongles USB).

7. **OSC tree centré sur `/strips/<id>/`** : pas de routes plates type `/fader1` ou `/mute1`. Toujours scope par strip ID.

8. **Profiles DSP YAML** : versionnés en repo, chargés par le Controller, poussés aux Stages au démarrage et à la modification. Format : `name`, `description`, `target` (type de speaker), `chain` (liste de plugins LSP avec params).

9. **Le type `network` (AES67 stream) est actif dès la v1.** Cela implique : un daemon AES67 fonctionnel sur chaque Stage (`aes67-daemon` ou équivalent type `pipewire-aes67`), discovery via SAP/SDP (multicast `239.255.255.255:9875`), grandmaster PTP désigné (en général le Stage Core), IGMP snooping activé sur le switch, validation de latence < 10 ms bout-en-bout. Une strip de type `network` peut être :
    - **inbound** : reçoit un stream AES67 multicast et l'expose comme une source pour le routing interne
    - **outbound** : prend une source interne et l'émet en multicast AES67 (utile pour partager un mix entre Stages, ou avec un device tiers compatible Ravenna/AES67)
    Les noms des streams suivent la convention `phonon-<type>-<stripid>` annoncés en SAP.

    **Cas d'usage majeur en v1** : 2 Pi en mode STANDALONE qui se bridgent en AES67 sans Controller. Exemple typique : Pi #1 a deux dongles BT (1 entrée téléphone invité + 1 sortie JBL), Pi #2 a une AK1 (mic + sortie HC). Les deux Pi font un bridge bidirectionnel via AES67 :
    - Audio téléphone (DG60 sur Pi #1) → AES67 stream → AK1 sortie HC sur Pi #2
    - Mic AK1 (Pi #2) → AES67 stream → UD100 vers JBL sur Pi #1
    Tout ça sans Controller, juste 2 Pi + un mini-switch, configuré via la mini-UI standalone de chaque Pi. C'est précisément ce que l'AES67 est fait pour : transport audio multicast entre machines distantes physiquement.

    **Conséquence pour le mode standalone** : la mini-UI standalone doit supporter **dès l'Étape 3 du dev** la création de mappings AES67 inter-Stage (pas seulement de routing audio local). Découverte des autres Stages via mDNS-SD, découverte des streams AES67 via SAP, création de mappings outbound (capability locale → stream multicast) et inbound (stream multicast → capability output locale).

10. **Mode standalone non négociable.** Chaque Stage embarque une mini-UI locale et peut tourner sans Controller. Voir la section "Modes opérationnels d'un Stage" plus haut. Quand tu touches au code du Stage, garde toujours en tête que :
    - Le Stage doit pouvoir booter et router de l'audio même sans Controller joignable
    - Les configs `controller.conf.json` et `standalone.conf.json` sont mutuellement exclusives, jamais fusionnées
    - Aucun reset ne supprime jamais de données silencieusement (tout est archivé)
    - La mini-UI standalone n'a **pas de DSP en v1**, juste du routing/gain/pan/mute (plafond : 8 mappings simultanés). Le hardware Pi 3 (Cortex-A53 quad-core 1.2 GHz, ARMv8 64-bit) supporterait du DSP léger (EQ paramétrique simple, trim) sans souci, mais on garde ça pour la v2 par souci de simplicité d'UX en mode mobilité.

11. **Multihoming réseau (Optiplex avec SFP en plus du RJ45).** Les Optiplex ont au moins deux interfaces réseau actives possibles : RJ45 carte mère + SFP additionnelle. Le `phonon-stage` ne doit **jamais** binder ses sockets AES67 sur `0.0.0.0` (ce qui exposerait le multicast sur l'interface domestique si elle est connectée par accident). Toujours utiliser la conf `bind_address` explicite dans `stage.yaml` qui désigne l'interface lab uniquement. Idem pour mDNS-SD.

12. **Règles udev obligatoires pour les périphériques USB.** Les dongles BT (`/dev/hci0..hci4`) et les cartes son sont permutables d'un boot à l'autre selon l'ordre d'énumération USB. Sans règles udev persistantes (basées sur l'adresse MAC du dongle ou le serial USB), le routing BT et la liaison capability → device n'est pas reproductible. À versionner dans `deploy/udev/99-phonon.rules`.

13. **Pas de Docker, tout natif.** Phonon tourne directement sur l'hôte Ubuntu Studio 26.04 LTS. Pas de containerisation, ni du Controller, ni du Stage, ni de la Console. Les raisons :
    - Audio temps réel + PipeWire + JACK + realtime scheduling se mariant mal avec Docker (overhead, permissions, friction USB/DBus/PTP)
    - Le projet vise l'apprentissage des technos sous-jacentes (systemd, BlueZ, mod-host, AES67), pas leur masquage par une couche d'abstraction
    - Mono-host en v1, donc l'isolation Docker n'apporte rien en pratique
    - Les `--privileged --network=host` qu'il faudrait pour faire tourner correctement annulent les bénéfices de Docker

    Packaging : un script `install.sh` qui crée les users `phonon`, `phonon-controller`, déploie les fichiers, installe les venvs Python, configure les units systemd. Pas de paquets `.deb` au début (envisageable plus tard si le projet se stabilise et que plusieurs machines tournent).

14. **`install.sh` à dual usage : bootstrap manuel ET provisioning à distance.** Le même script `deploy/install.sh` fonctionne dans deux contextes :

    **Mode A — bootstrap manuel** (première machine, le 3070) : l'utilisateur lance manuellement
    ```bash
    curl -sSL https://raw.githubusercontent.com/<owner>/phonon/main/deploy/install.sh | sudo bash
    ```
    Le script détecte le contexte, pose les users, installe Controller + Stage, configure systemd. Mode par défaut : `controller+stage`.

    **Mode B — provisioning à distance** (Pi 3, Stages additionnels) : le Controller existant exécute `install.sh` à distance via SSH après adoption depuis la Console (panneau Admin > Stages > Add Stage). Le script reçoit des flags qui pré-configurent la machine en mode `ADOPTED` immédiatement, sans passer par STANDALONE.

    Flags supportés :
    ```bash
    install.sh                                  # auto-détection (controller + stage)
    install.sh --role=stage-only                # Stage seul
    install.sh --role=controller-only           # Controller seul
    install.sh --role=stage-only \              # Stage adopté directement (mode B)
      --adopted-by=core-3f9a1b8e \
      --controller-url=http://10.100.0.10:8400 \
      --controller-token=<bootstrap_token>
    ```

    Le Controller a un module `phonon_controller.provisioning` (asyncssh ou paramiko) qui orchestre :
    - Connexion SSH avec password initial (saisi dans la modale Add Stage)
    - Pousser la clé publique `phonon-deploy` dans `~/.ssh/authorized_keys` de la cible
    - Upload de `install.sh` via SCP (ou `curl` depuis la cible vers GitHub si Internet dispo)
    - Exécution avec les bons flags, streaming stdout/stderr vers la Console via WebSocket
    - Gestion d'erreurs (timeout, permission denied, distro incompatible)

    `install.sh` doit donc être :
    - **Idempotent** (re-lançable sans casser ce qui marche)
    - **Auto-détectant la distro** (Ubuntu, Debian, Raspberry Pi OS, dérivés)
    - **Robuste** au mode non-interactif (pas de prompt, tout vient des flags ou de l'env)
    - **Loggé** : sortie verbose et structurée pour que le Controller puisse parser la progression et l'afficher en `[N/M] Étape...` dans la Console

15. **Tests obligatoires à chaque étape, pas après.** Voir la section "Stratégie de tests" plus haut. Aucune fonctionnalité n'est mergée sur `main` sans ses tests niveau 1 et 2 qui passent. Le pattern Backend abstrait (Real / Fake) est imposé pour tout ce qui touche au matériel ou au système. Pas de `subprocess.run()` ou `datetime.now()` éparpillés dans le code métier : tout passe par des modules dédiés et injectables. Les tests sont écrits **en même temps** que la feature, pas après. CI GitHub Actions exigeante : lint + format + mypy + tests + coverage 70 % minimum.

16. **Pi-first pour le développement, le 3070 suit.** L'ordre des étapes est fixe (cf. section "Étapes de développement"). En particulier l'étape 1 vise les Raspberry Pi 3 d'abord, pas le 3070. Raisons : (a) hardware le plus contraint, donc si ça marche dessus ça marche partout ; (b) packaging arm64 validé d'emblée ; (c) le `install.sh` est multi-arch (Ubuntu Studio x86_64 + Raspberry Pi OS arm64) dès le début, pas adapté ensuite. **Ne jamais commencer une étape par tester uniquement sur x86_64**, ça mène à du code qui plante sur Pi en silence et qu'on ne découvre que tard.

---

## État au 2 mai 2026

**Phase de design terminée.** CLAUDE.md, SECURITY.md, et le mockup HTML de la Console (v4.4) sont stables.

**Étape de dev en cours : Étape 1 — Stage Agent fondations**, ciblée Pi 3B `stage-x01`.

Inventaire en place :
- [x] Architecture v4.3 figée et documentée
- [x] Branding Phonon défini (logo Hex Crystal, palette Cryogenic, typographie Barlow Condensed + IBM Plex Mono)
- [x] Console mockup v4.4 (HTML/CSS/JS standalone) avec strips, faders style PreSonus, meters Ardour-style, Player Bar avec MPD, Settings modal avec color picker, plugin-windows multi
- [x] Hardware identifié : 3070 i5 9th gen (8 Go), 3040 i7 6th gen (16 Go, v2), 2× Pi 3B v1.2
- [x] Inventaire I/O : 2× UD100 (BlueZ HCI), 3× DG60 (ALSA cartes son), X-Fi HD, AK1
- [x] Premier Pi `stage-x01` configuré : DG60 + UD100 vus, hardware testé OK

Reste à faire :
- [ ] **Étape 1** : Stage Agent Python (~400 lignes) avec ses tests niveau 1+2, ciblé Pi d'abord puis 3070
- [ ] CI GitHub Actions : lint Ruff, format, mypy strict, pytest, coverage gate 70%
- [ ] Backend abstrait (interfaces Real / Fake) pour PipeWire, BlueZ, AES67, mDNS, SSH provisioning
- [ ] Mini-UI standalone embarquée dans phonon-stage (HTML/JS, ~200 lignes, sert sur :8401/standalone) — étape 3
- [ ] Controller daemon Python avec Strip Graph Compiler (~600 lignes attendues) avec ses tests niveau 1+2 — étape 2
- [ ] Workflow d'adoption : script bootstrap Controller (one-liner curl), adoption SSH des Stages depuis l'UI Admin — étape 3
- [ ] Workflow standalone/fallback : machine à états, fichiers controller.conf / standalone.conf / pending-archive — étape 3
- [ ] Console : panneau Admin > Stages avec actions Adopter / Repousser ma conf / Adopter la conf actuelle / Oublier — étape 9
- [ ] Console : panneau Admin > Archives avec restauration et suppression manuelle (double confirmation) — étape 9
- [ ] SD image build script pour Raspberry Pi 3 — plus tard
- [ ] udev rules : noms stables pour UD100 (`hciN` BlueZ) et DG60 (cartes son ALSA) — étape 1 ou 4
- [ ] Systemd units (`.service`, `.socket`, `.target`) — étape 1
- [ ] OSC layout JSON (mapping AK1 surface → /strips/.../...) — plus tard
- [ ] MIDI→OSC bridge pour les contrôleurs externes — plus tard
- [ ] Profiles YAML pour chaque speaker (JBL Pulse 3, Xtreme 4, casque, HC, master) — étape 8
- [ ] Test plans manuels niveau 4 dans `docs/test-plans/` (au moins TP-01 à TP-99) — au fil des étapes

---

## Comment Claude Code doit aborder ce projet

- **Lire `CLAUDE.md` (ce fichier) et `SECURITY.md` avant tout travail substantiel.** C'est la spec et les contraintes.
- **Respecter l'ordre des étapes** (cf. section "Étapes de développement"). Pas de saut. Étape 1 = Stage Agent, ciblé Pi en premier.
- **Respecter les conventions de naming** ci-dessus, ne pas inventer.
- **Travailler par branche dédiée** : une branche `phase-N-<sujet>` par étape. Merge sur `main` uniquement après validation manuelle de l'étape par l'utilisateur.
- **Préférer petits diffs** plutôt que rewrites massifs. Le projet évolue par itérations.
- **Pas de magie cachée** : si un comportement n'est pas évident depuis le code, ajouter un commentaire ou une docstring.
- **Tests obligatoires en même temps que la feature** (cf. point 15 de "Choses à connaître"). Pattern Backend abstrait Real/Fake imposé.
- **CI GitHub Actions dès l'étape 1** : lint Ruff, format, mypy, pytest, coverage. Aucun merge possible si la CI rouge.
- **Documenter au fur et à mesure** : si tu introduis un concept nouveau (ex: nouvelle adresse OSC, nouveau type de capability), mettre à jour CLAUDE.md ou ouvrir un nouveau doc daté.
- **Pas de dépendance lourde sans validation.** Avant d'ajouter une lib qui pèse 50 Mo de wheels, demande à l'utilisateur.
- **Tone des commits** : français ou anglais OK, courts, à l'impératif (`feat: ajout endpoint /capabilities`, `fix: handle BlueZ DBus disconnect`).

---

## Contact

Serge — projet perso, échange direct dans Claude Code ou par message.

