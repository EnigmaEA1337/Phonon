# SECURITY.md — Modèle de menace et hardening Phonon

> Ce document décrit le modèle de menace, les contrôles de sécurité et les bonnes pratiques pour le projet Phonon. Il est destiné au développeur (moi-même) et à Claude Code, qui devra respecter ces contraintes lors de toute modification du code ou de la configuration.

**Dernière mise à jour** : avril 2026.

---

## 1. Périmètre et hypothèses

### 1.1 Périmètre

Couvert par ce document :
- Le Controller daemon (`phonon-controllerd`)
- Les Stage Agents (`phonon-stage`)
- La Console web (HTML statique + JS)
- Le réseau de lab dédié (`10.100.0.0/24`)
- Les hôtes Linux (Ubuntu Studio 26.04 LTS, Raspberry Pi OS 64-bit)

Hors périmètre :
- Le réseau domestique principal (gestion via UniFi UDM Pro HA)
- Les services audio externes (Spotify, AirPlay) — on consomme leur stream sans les sécuriser
- Le firmware des dongles Bluetooth, des cartes son USB, des speakers JBL
- L'OS hôte au-delà de ce que les services Phonon configurent (ex : SSH, sudo, kernel hardening — c'est la responsabilité du sysadmin, pas de Phonon)

### 1.2 Hypothèses

- Le lab Phonon est **isolé** du LAN domestique. Pas de routage entre `10.100.0.0/24` et `192.168.x.x`.
- Aucun accès depuis Internet. Pas de port forwarding, pas de DDNS, pas de reverse proxy public.
- Les Stages et Controller sont **physiquement protégés** : ils sont à mon domicile, dans des Optiplex SFF placés dans un meuble fermé.
- L'utilisateur principal (moi) est de confiance.
- Un éventuel utilisateur secondaire (invité accédant à la Console depuis un téléphone le temps d'une soirée) est **partiellement de confiance** : il peut modifier le mix mais ne doit pas pouvoir compromettre les Stages.
- Les artefacts (profiles YAML, plugins LSP) sont audités à la première installation puis figés (pas d'auto-update auquel on accorde sa confiance aveugle).

---

## 2. Modèle de menace

### 2.1 Acteurs et motivations

| Acteur | Capacités | Motivation plausible |
|--------|-----------|---------------------|
| Utilisateur invité (téléphone connecté au Wi-Fi de soirée) | Accès à la Console web | Trolling : muter tout, monter le master à +6 dB, écouter le micro de monitoring |
| Voisin / autre malveillant à portée Wi-Fi | Sniffing, association au lab Wi-Fi (si SSID activé) | Curiosité, tester ses outils |
| Attaquant supply-chain (paquet pip, plugin LSP malveillant) | Exécution arbitraire si paquet compromis installé | Cryptomining, exfiltration |
| Attaquant interne logique (script propre exécuté par erreur sur le Controller) | Exécution arbitraire | Bug de mon propre code, scope creep d'un script de test |
| Attaquant physique (cambriolage) | Accès physique aux machines | Vol de matériel ; les données ne sont pas une cible |

### 2.2 Scénarios redoutés (par ordre de priorité)

**S1 — Audio surge / accident** : un mauvais routing ou un bug fait que tous les outputs poussent à +12 dB simultanément, endommageant les speakers et / ou les oreilles présentes. **C'est le risque principal.**

**S2 — Invité malveillant via Console** : un invité utilise la Console pour saboter le mix, écouter une source non destinée à lui (ex : monitoring d'un mic personnel via PFL), ou capturer le stream et le rediffuser.

**S3 — Compromission d'un Stage** : exécution arbitraire sur le Stage, qui peut alors :
- Exfiltrer des sources audio vers Internet
- Devenir un point de pivot vers le réseau domestique si le filtrage est mal configuré
- Devenir un point d'amplification pour des attaques externes

**S4 — Compromission du Controller** : moins critique en termes d'audio (les Stages restent autonomes en cas de chute) mais expose la **base SQLite** (toutes les configurations, profils DSP, scènes, historique d'événements).

**S5 — Déni de service** : flood d'AES67/PTP, attaque sur l'API REST qui sature le Controller, flood ARP/multicast.

**S6 — Vol physique** : machines volées, mais les données qu'elles contiennent (configs DSP, historique d'écoute via MPD) ne sont pas sensibles à part la liste de fichiers musicaux du NAS.

### 2.3 Hors scope (assumés)

- Cryptanalyse d'AES67 (pas de chiffrement audio dans la version actuelle, on assume le réseau filaire de confiance)
- Attaques side-channel sur le hardware (Spectre, Rowhammer)
- Attaques quantiques sur la crypto utilisée (TLS 1.3 entre Console et Controller — futur)

---

## 3. Architecture sécurité

### 3.1 Segmentation réseau

```
                  ┌────────────────┐
                  │   Internet     │
                  └────────┬───────┘
                           │
                  ┌────────▼───────┐
                  │  UDM Pro HA    │  ← LAN domestique standard
                  │   192.168.x    │
                  └────────┬───────┘
                           │ (LAN trunk)
                  ┌────────▼─────────┐
                  │  USW-16-POE      │
                  │                  │
                  │  ports 1-8 :     │
                  │   LAN domestique │
                  │                  │
                  │  ports 9-16 :    │
                  │   VLAN 100 lab   │  ← isolé, pas de routage L3 vers LAN
                  └─────────┬────────┘
                            │
                       ┌────▼─────┐
                       │  Lab     │  10.100.0.0/24
                       │  Phonon  │  multicast 239.69.0.0/16
                       └──────────┘
```

**Règles** :
- VLAN 100 (lab) **n'a pas de gateway routée** vers le LAN domestique
- Pas de DHCP relay, pas de mDNS reflector entre VLANs
- Une éventuelle Console depuis le LAN domestique doit passer par un **port forwarding explicite** (port 8400 vers Controller) ou par un VLAN trunk maîtrisé
- En cas d'usage mobile (WiFi via Slate 7 Pro) : le SSID du lab est **WPA3-SAE**, mot de passe long (≥ 32 caractères), pas de WPS

### 3.2 Bluetooth

Les dongles BT (UD100, DG60) sont utilisés pour :
- **Sortie** vers JBL (Connect+, PartyBoost) — peering manuel, pas d'auto-pair
- **Entrée** depuis téléphones invités — pairing à la demande, déconnexion automatique après la soirée

**Règles** :
- Pas de pairing automatique (`KillSignal=SIGTERM` sur `bluetoothd` ; pairing toujours manuel via `bluetoothctl pair`)
- Mode **discoverable** activé uniquement pendant les fenêtres de pairing (mode "soirée"), désactivé ensuite
- Les services BT non utilisés (HID, FTP, NAP, PAN) sont désactivés au niveau de `main.conf`
- Les couches BLE ne sont pas exposées (le projet n'en a pas besoin)

### 3.3 AES67 / PTP

AES67 transporte de l'audio brut en multicast. Pas de chiffrement.

**Le type de strip `network` est actif dès la v1**, donc Phonon expose et consomme du multicast AES67 sur le réseau lab.

**Règles** :
- Multicast strictement scopé à `239.69.0.0/16` (RFC 2365 admin scope)
- IGMP snooping activé sur le switch UniFi pour ne pas flooder
- PTP : un seul grandmaster (configuration manuelle, pas de BMCA en production permanente). En général c'est le Stage Core.
- Filtrage strict en entrée du subnet sur les ports UDP 5004 (RTP), 319-320 (PTP), 9875 (SAP)
- Discovery via SAP (Session Announcement Protocol) sur multicast `239.255.255.255:9875` — restreint au lab par filtrage L2
- Aucun stream AES67 ne traverse le routeur entre VLANs (le multicast n'est pas routé)
- Toute déclaration de strip `network` outbound passe par le Controller, pas directement initiée par un Stage (autorisation centrale)
- Les noms de streams suivent la convention `phonon-<stripid>` pour éviter collisions avec d'autres devices Ravenna/AES67 sur le réseau lab
- Quand un Stage perd la sync PTP, ses strips `network` outbound sont mutées automatiquement et les inbound passent en silence (pas de glitch audio)

### 3.4 API REST + WebSocket

#### 3.4.1 Authentification

**Phase 1 (lab perso, courant)** : pas d'authentification. Le réseau lab est de confiance.

**Phase 2 (dès qu'un invité a accès)** :
- **Token bearer** par session, généré côté Controller, présenté en `Authorization: Bearer <token>`
- 2 niveaux : `admin` (toutes opérations, mode EDIT permis) et `guest` (PLAY only, pas de modification de routing, pas d'accès aux strips de monitoring)
- Token lié à une IP source pour éviter le replay sur autre device
- Expiration configurable (par défaut 4h pour `guest`, 24h pour `admin`)

**Phase 3 (futur, si on expose au LAN domestique)** :
- TLS 1.3 obligatoire (certificat auto-signé via mkcert, ou via une mini-PKI)
- mTLS optionnel pour les Stages parlant au Controller

#### 3.4.2 Autorisation par capacité

Les opérations sensibles (qui peuvent impacter S1 ou S2) sont protégées :

| Opération | Niveau requis |
|-----------|---------------|
| Lecture état (`GET /strips`, `GET /system/state`) | guest |
| Modification fader / mute / solo / pan | guest |
| Création / suppression / re-routing de strip | admin |
| Modification du master (gain, limiteur threshold, profile) | admin |
| Activation Listen (PFL) sur une strip | admin (le PFL peut écouter n'importe quoi) |
| Activation mode EDIT | admin |
| Endpoints `/system/restart`, `/system/shutdown`, `/system/reset` | admin |
| Endpoints `/debug/*` (`/debug/dump_state`, etc.) | admin uniquement, en plus protégés par token spécifique |

#### 3.4.3 Validation des inputs

- **Pydantic v2** pour tous les modèles d'entrée (REST + WS), `model_config = ConfigDict(extra="forbid")` partout
- Limites strictes sur les valeurs numériques (gain en dB ∈ [-90, +12], pan ∈ [-1, +1], etc.)
- Sanitisation des noms (strip name, profile name) : alphanumérique + tiret + underscore + espace, max 64 caractères
- Pas de chemins de fichiers reçus depuis l'API (un `profile_name` n'est jamais transformé en path directement, on lookup dans une whitelist)

#### 3.4.4 Rate limiting

- Sur les endpoints de mutation : 100 req/sec par token, 10 req/sec par IP source non authentifiée
- Sur les endpoints WebSocket : un seul WS par token actif, fermeture des connexions excédentaires
- En cas de dépassement, réponse `429 Too Many Requests` avec `Retry-After`

#### 3.4.5 Mini-UI standalone (Stage en mode STANDALONE / FALLBACK)

La mini-UI servie par chaque Stage sur `:8401/standalone` présente un cas d'auth particulier :

- **Phase 1 (lab perso)** : pas d'auth, le réseau est de confiance. La mini-UI accepte n'importe quelle connexion locale.
- **Phase 2** : auth par PIN à 4 chiffres affiché physiquement sur le Pi (LED matrix ou écran OLED) à la première connexion d'un device. PIN renouvelé après 24h ou sur reboot.
- **Phase 3** : token bearer comme pour la Console principale, géré localement par le Stage (pas de fédération avec le Controller en standalone).

Spécifique au mode FALLBACK (Stage adopté avant, Controller injoignable) :
- La mini-UI affiche un **banner persistant** "Mode dégradé — Controller absent depuis Xs"
- Toutes les modifications de config **sont autorisées** (cas d'urgence) mais journalisées avec un flag `fallback_modification: true`
- Au retour du Controller, ces modifications apparaissent dans le diff de divergence (cf. §3.4.5 du workflow d'adoption)

La mini-UI ne doit **jamais** exposer :
- Le contenu raw des fichiers de config (`controller.conf.json`, `pending-archive.json`)
- Les tokens d'auth, même hashés
- Les logs verbose (DEBUG) qui pourraient contenir des secrets

### 3.5 Limites audio (anti-S1)

**Critique.** Hardcodé au niveau du DSP, pas modifiable depuis l'API :

- Le master strip a **toujours** un limiteur (LSP Limiter Stereo) en dernier insert avec `threshold ≤ -0.3 dBTP`. Pas de bypass possible depuis l'API.
- Le gain master a une **limite supérieure absolue de 0 dB** (pas de boost au-delà du signal d'entrée).
- Les outputs casque/monitor (PFL) ont leur propre limiteur indépendant (`threshold ≤ -6 dBTP`) pour protéger les oreilles.
- Tout changement de profile DSP du master nécessite un **délai de transition fade-in 500 ms** côté Controller pour éviter les pops.
- Les outputs reliés à des HC ou speakers gros volume ont leur gain plafonné à `0 dB` au niveau de leur strip output, configurable mais avec un soft cap qui demande confirmation.

**Implémentation** : un `audio_safety_guard.py` côté Controller valide chaque mutation entrante. Les valeurs out-of-bounds sont **rejetées** (HTTP 400), pas clampées silencieusement.

### 3.6 Secrets management

- Pas de secret hardcodé dans le code source. Jamais.
- Tokens d'authentification : générés au boot, stockés dans `/var/lib/phonon/secrets/` avec mode 0600, owner `phonon:phonon`
- Mot de passe SSH des hôtes : géré au niveau OS, pas par Phonon
- Pas de secrets dans les profils YAML (les profils sont versionnés en clair en repo)
- Variables d'environnement pour la config locale (overrides) — JAMAIS pour les secrets en prod
- Les credentials des services tiers (Spotify Connect token via librespot, par exemple) sont stockés dans des fichiers dédiés `/var/lib/phonon/credentials/<service>.json` mode 0600

#### Fichiers de configuration Stage (mode standalone / adoption)

Les trois fichiers JSON décrits dans CLAUDE.md (section "Modes opérationnels") doivent être protégés strictement :

| Fichier | Mode | Owner | Contenu sensible |
|---------|------|-------|-----------------|
| `/var/lib/phonon/controller.conf.json` | 0600 | `phonon:phonon` | Token d'auth Stage→Controller, identité du Controller adoptant, mappings actifs |
| `/var/lib/phonon/standalone.conf.json` | 0600 | `phonon:phonon` | Mappings locaux (pas de token, mais visibilité du routing) |
| `/var/lib/phonon/pending-archive.json` | 0600 | `phonon:phonon` | **Snapshot complet de l'ancienne config Controller**, donc potentiellement un ancien token d'auth |

**Règles strictes** :
- Ces fichiers ne doivent jamais être loggés en clair (même en DEBUG)
- À chaque adoption ou reset, la signature numérique du fichier précédent doit être enregistrée dans le journal d'événements pour audit
- Le `pending-archive.json` peut contenir un token Controller révoqué — le Controller, lors d'une nouvelle adoption, **invalide systématiquement les anciens tokens** liés au Stage, peu importe le choix utilisateur (Repousser / Adopter / Oublier)
- La mini-UI standalone n'expose **jamais** ces fichiers via une route REST (pas de `GET /local/config-raw` ou équivalent). Seules les vues "métier" (mappings, statut) sont exposées

---

## 4. Hardening hôte

### 4.1 Stages (Optiplex SFF, Raspberry Pi 3)

Au-delà des bonnes pratiques classiques (firewall, SSH par clé, fail2ban), spécifique à Phonon :

- Run `phonon-stage` sous user dédié `phonon`, pas root
- `phonon` ajouté à `audio` (pour PipeWire / mod-host) mais pas à `bluetooth` (passe par DBus avec policy)
- DBus policy custom (`/etc/dbus-1/system.d/phonon.conf`) qui n'autorise que les interfaces strictement nécessaires sur `org.bluez`
- Plus de `pulseaudio` parallel à PipeWire (un seul serveur audio actif)
- SystemD units avec :
  - `User=phonon`, `Group=phonon`
  - `NoNewPrivileges=yes`
  - `ProtectSystem=strict`
  - `ProtectHome=yes`
  - `PrivateTmp=yes`
  - `ReadWritePaths=/var/lib/phonon /var/log/phonon /etc/phonon`
  - `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`
  - `RestrictNamespaces=yes`
  - `LockPersonality=yes`
  - `MemoryDenyWriteExecute=yes` si possible (mais à vérifier que `mod-host` et plugins LSP fonctionnent encore)
  - `SystemCallFilter=@system-service @audio` (avec ajustements)
- `apparmor` profile dédié (futur)

### 4.2 Controller

- User dédié `phonon-controller`
- DB SQLite `/var/lib/phonon/state.db`, mode 0600, owner `phonon-controller:phonon-controller`
- WAL files (`state.db-wal`, `state.db-shm`) protégés idem
- SystemD durci comme pour Stage, plus :
  - `CapabilityBoundingSet=` (vide, pas besoin)
  - `RestrictRealtime=yes`
- Pas d'écriture dans `/etc/phonon` au runtime — la config est lue au boot, modifs via reload SIGHUP
- Backup automatique de la DB toutes les heures via `sqlite3 .backup` vers `/var/lib/phonon/backups/`, retention 24h glissante

### 4.3 Console (frontend statique)

- CSP stricte dans le `<meta http-equiv>` : `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self' wss:`
- Pas de CDN externe (tout en self-hosted, y compris les fonts)
- Pas de `eval()`, pas de `Function()`, pas de `innerHTML` avec contenu user-supplied (si `innerHTML` est utilisé, le contenu est généré côté JS uniquement, jamais issu directement de l'API)
- Headers HTTP servis par le Controller :
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: no-referrer`
  - `Permissions-Policy: microphone=(), camera=(), geolocation=(), interest-cohort=()`

---

## 5. Audit & logging

### 5.1 Quoi logger

- Toute mutation d'état (création, modification, suppression de strip, changement de routing, changement de profile DSP, mode PLAY/EDIT)
- Toute connexion WebSocket (ouverture, fermeture, déconnexion anormale)
- Toute requête API authentifiée (avec token ID, IP source, endpoint, statut HTTP)
- Tous les événements de découverte mDNS (Stage joining/leaving)
- Tout événement audio remarquable (xrun > seuil, perte PTP sync, déconnexion BT)
- Tout audit-relevant : tentative d'accès non autorisé, depassement rate limit, validation Pydantic échouée

### 5.2 Comment logger

- Format **JSON structuré** via `structlog`
- Champs minimums : `timestamp` (ISO 8601 UTC), `level`, `correlation_id`, `actor` (token id ou `system`), `action`, `resource`, `result`, `details`
- Sortie : journald + fichier `/var/log/phonon/{controller,stage}.log` rotaté par `logrotate` (7j retention, compress)
- Pas de log de mots de passe, tokens, données audio brutes
- Niveau par défaut : `INFO`. `DEBUG` ne doit jamais être actif en "prod" (= mes vraies soirées)

### 5.3 Métriques

Si on instrumente (Prometheus exporter) :
- Pas de cardinalité explosive (pas de label `strip_id` libre — utiliser `strip_type`)
- Pas de PII / données invité dans les labels

---

## 6. Mises à jour & supply chain

### 6.1 Dépendances Python

- `pyproject.toml` avec versions épinglées (`==`) en prod, `>=` autorisé en dev
- `pip-audit` ou `safety check` lancé manuellement avant chaque release
- Pas de `pip install -r requirements.txt --upgrade` automatique
- Vérification SHA des wheels critiques (FastAPI, pydantic, sqlalchemy si utilisé)
- Pas de paquets obscurs (< 1000 downloads/jour) sans audit manuel

### 6.2 Plugins LV2 (LSP)

Téléchargés depuis le repo officiel LSP (`lsp-plug.in`) ou la version packagée Ubuntu Studio. Pas de plugins tiers non audités.

### 6.3 OS

- Mises à jour système manuelles ou via `unattended-upgrades` configuré pour les `security` repos uniquement
- Kernel : pas d'auto-update sans test (la latence audio peut changer entre versions)
- Pas de PPA tiers sauf nécessité justifiée

---

## 7. Sauvegardes

- DB Controller : sauvegarde toutes les heures (cron) vers NAS via NFS, chiffrée (`age` ou `gpg`)
- Profiles YAML : versionnés en git (auto-backup via push origin)
- Configs `/etc/phonon/` : sauvegardées hebdo via Ansible playbook qui pull et commit
- Logs : non sauvegardés long-terme, retention 7j locale

En cas de perte du Controller, le re-déploiement reconstruit l'état depuis les sauvegardes DB + profils YAML versionnés.

---

## 8. Réponse à incident

### 8.1 Détection

- Surveillance des logs critiques (xrun, déconnexion PTP, échec d'auth) via journald (`journalctl -u phonon-controllerd -p err`)
- Alertes mail (sendmail local) pour les événements `CRITICAL` (limiteur clipping continu > 5s, perte de Stage > 30s, échec d'auth répété)
- Pas d'intégration SIEM dans cette version. Possibilité future : forward syslog vers un collecteur externe si besoin.

### 8.2 Procédures

| Incident | Action |
|----------|--------|
| Audio incontrôlé (surge) | Couper l'alimentation des amplis (geste physique). Ensuite : `systemctl stop phonon-controllerd phonon-stage` sur tous les hôtes. Investiguer logs DSP. |
| Compromission Stage suspectée | Isoler le Stage du réseau (débrancher câble Ethernet). Snapshot état (`sysctl ; netstat ; ss -tulnp ; ps auxf`) puis re-flash depuis image SD propre. |
| Compromission Controller suspectée | Idem mais en plus : récupérer la dernière sauvegarde DB pré-incident, comparer schéma et contenu, rejouer sur instance fraîche. |
| Perte de Controller | Les Stages continuent à tourner avec leur dernier état. Déployer nouveau Controller depuis backup + git. |

### 8.3 Post-mortem

Pour tout incident notable, créer un fichier `docs/incidents/<date>-<slug>.md` avec :
- Résumé exécutif
- Chronologie
- Cause racine
- Impact
- Mitigation
- Actions correctives à long terme

(Reflexe utile pour comprendre ce qui s'est passé.)

---

## 9. Roadmap sécurité

| Priorité | Item |
|---------|------|
| P0 | Limiteur master non-bypassable (S1 mitigation) |
| P0 | Audio safety guard côté Controller (validation des gains entrants) |
| P0 | Protection mode 0600 + owner phonon des fichiers de config Stage |
| P1 | Authentification token admin/guest sur l'API Console |
| P1 | Auth PIN à 4 chiffres sur la mini-UI standalone du Stage (Phase 2) |
| P1 | Hardening systemd des unités Stage et Controller |
| P1 | Journal d'événements `fallback_modification` au retour d'adoption |
| P2 | TLS interne (mTLS Stage ↔ Controller) |
| P2 | Invalidation systématique des anciens tokens lors d'une adoption |
| P2 | DBus policy stricte pour BlueZ |
| P3 | AppArmor profile pour `phonon-stage` et `phonon-controllerd` |
| P3 | Rate limiting fin sur l'API |
| P4 | Audit log centralisé (forwarding vers un collecteur syslog externe) |
| P4 | Isolation namespace pour `mod-host` (capabilities limitées) |

---

## 10. Comment Claude Code doit traiter ce fichier

- Toute PR / modif **importante** (nouvelle endpoint API, nouvelle fonctionnalité de routing, élévation de privilège, ouverture de port, nouvelle dépendance) doit être lue à l'aune de ce document.
- Si une modification semble en conflit avec une règle ici, **demande-moi avant** plutôt que de proposer une exception.
- Si le code que tu écris devrait modifier un comportement de sécurité (ex : augmenter une limite de gain, désactiver un check), tu dois le marquer explicitement (`# SECURITY: relaxes audio safety guard, see SECURITY.md §3.5`).
- Pas de bypass silencieux des contrôles. Une exception doit être documentée et l'auteur doit pouvoir l'expliquer.

---

## 11. Contact

Serge — pour toute question / découverte de vuln ne pas hésiter à m'en parler directement.

