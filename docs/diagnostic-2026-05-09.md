# Diagnostic Phonon-Stage — 9 mai 2026

> Audit lecture-seule du daemon `phonon-stage` (branche `phase-1-stage-foundations`).
> Objectif : identifier les fragilités structurelles à l'origine des symptômes
> observés en production sur Pi 3B (CPU instable 5–95 %, WirePlumber émetteur
> à 25–45 %, AES67 craquelant, orphelins de bridges, BT bonds qui sortent du
> D-Bus, polkit/dbus écrasés par le panneau Services).

---

## 1. Vue d'architecture

Le daemon est une app FastAPI (`stage/src/phonon_stage/main.py`) qui charge 14
routers sous `api/` plus le service de mappings. Les modules notables, par
volume : `system.py` (880 l. : statut machine, contrôle systemctl, gauges
ressources), `aes67.py` (818 l. : RTP send/recv via snippets PipeWire,
SAP/SDP, replay d'état après restart), `bluealsa_bridge.py` (411 l. : bridges
shell-wrappés BlueALSA → null-sink PipeWire), `ptp.py` (399 l. : statut
ptp4l/phc2sys via pmc + journalctl), `ws.py` (338 l. : 4 boucles d'arrière-plan
pour la Console), `bluetooth.py` (322 l. : BlueZ via pydbus + bluetoothctl),
`update.py` (311 l. : self-update prod/dev/readonly), `mappings/service.py`
(297 l. : crée/restaure les links PipeWire). Les wrappers natifs sont sous
`pipewire/`, `bluetooth/`, `audio/`, `discovery/` (pattern Real/Fake correct).
Côté déploiement, `deploy/install.sh` (653 l.) crée users/units/sudoers,
`deploy/update.sh` (124 l.) gère le cycle pull → fast-path vs full.

---

## 2. Patterns de fragilité récurrents

### 2.1 Subprocess fan-out (chaque tick spawn N forks)

- `api/system.py:404-463` — `_service_active_user/system/_service_enabled/_service_installed/_can_sudo_systemctl` : par service et par tick, ce sont 4-5 `systemctl` + 1 `sudo systemctl` qui se forkent ; pour 10 services c'est ~50 forks toutes les 5 s en plus des 2 `pid_str` et `type_str` lancés via shell dans `collect_services` (`system.py:534-540`).
- `api/system.py:534-540` — `pid_str = await _run("systemctl … show -p MainPID …")` puis `type_str = await _run("systemctl … show -p Type …")` : 2 forks **par service** dans `collect_services`, 100 % shell (`create_subprocess_shell`), plus `_run("groups phonon")`, `_run("stat -c '%a' …")`, `_run("iptables -L")` dans `security_status`.
- `api/ws.py:117-195` — `_system_loop` invoque `collect_services()` + `_collect_resources()` toutes les 5 s, donc 50 + 10-15 forks/tick, plus PID-delta watcher.
- **Cause racine** : pas d'agrégateur. Chaque champ (active/enabled/installed/PID/type/can_sudo) part en `systemctl show -p …` séparé alors qu'un seul `systemctl show <unit> -p ActiveState,UnitFileState,MainPID,Type` retournerait tout. Et le polling n'a pas de canal d'événement (D-Bus `org.freedesktop.systemd1` expose les transitions sans coût).

### 2.2 Shell-wrapper bridges (`while true; do arecord | pacat; sleep 0.5; done`)

- `api/bluealsa_bridge.py:196-205` (playback) et `:244-258` (capture) — `bridge_cmd = f"while true; do … sleep 0.5; done"` lancé par `create_subprocess_shell`. Le PID enregistré est celui du shell `bash`, pas celui de `arecord`/`pacat`/`aplay`.
- `api/bluealsa_bridge.py:39-44` — `cleanup_stale_bridges` recourt à `pkill -f` sur 6 patterns différents (`while true.*arecord.*bluealsa`, `pacat.*bt_`, `parec.*bt_`, etc.) parce que le tracking par PID ne suffit pas : tuer le shell ne tue pas forcément les enfants si `set -m` n'est pas activé.
- `api/aes67.py:378-387` — répétition exacte du même `pkill -f` à chaque `replay_audio_state` (déclenché par tout restart de PipeWire/WirePlumber).
- **Cause racine** : auto-réparation par `while true` au lieu de supervision système. Un `systemd --user` template `phonon-bt-bridge@<mac>-<type>.service` avec `Restart=on-failure` et `ExecStop=` propre supprimerait l'ensemble de la classe « orphelins ». Pour aller plus loin, le module `module-loopback` de PipeWire fait nativement le pont null-sink ↔ device sans pipe utilisateur du tout (donc sans parec/pacat → suppression du WirePlumber 25-45 %).

### 2.3 Polling au lieu d'abonnements D-Bus / PW events

- `api/ws.py:78-87` — `_mode_loop` poll toutes les 0,5 s la variable globale `_ui_mode`.
- `api/ws.py:271-303` — `_alerts_loop` fork `vcgencmd get_throttled` toutes les 10 s (acceptable, mais doublon avec `_collect_resources`).
- `api/ws.py:107-195` — `_system_loop` toutes les 5 s déclenche un balayage PID complet pour détecter restart out-of-band de pipewire/wireplumber/pulse, alors que systemd émet `JobNew/JobRemoved` sur D-Bus.
- `api/ptp.py:158-225` — `_read_journal_state` exécute `journalctl -t ptp4l -n 2000` à chaque hit `/ptp/status`. C'est l'équivalent d'un `find` sur le journal ; sur Pi 3 c'est plusieurs centaines de ms de lecture I/O.
- `api/aes67.py:240-273` — `_sap_announce_loop` reconstruit le SDP+SAP packet et le send_to toutes les 2 s par stream actif. Acceptable, mais lit `_settings_mod.get()` à chaque itération.
- **Cause racine** : pas de bus d'événements interne. PipeWire pousse des updates structurés via `pw-mon` (ou la lib `pipewire-python`), BlueZ via `org.bluez` `PropertiesChanged`, systemd via `org.freedesktop.systemd1`. Tous les loops actuels reconstruisent un état déjà disponible en push.

### 2.4 Resync par nom (string match sur les node names PipeWire)

- `mappings/service.py:239-272` — `_reresolve_mapping_ports` : `by_name = {n.name: n for n in nodes}` puis `src = by_name.get(mapping.source_node_name)`. Si l'AES67 stream est recréé avec un nom légèrement différent (ex. `aes67-recv-x` → `aes67-recv-x.2` après collision), la résolution échoue et `_reresolve` retourne `None` ; le mapping est skippé silencieusement.
- `api/ws.py:198-268` — `_maybe_resync_stale_mappings` détecte les IDs morts mais s'appuie ensuite sur `resync_mappings()` (donc même piège nom).
- `api/aes67.py:543-545` — `_node_name(kind, name)` est purement déterministe (`aes67-{kind}-{name}`) côté création, mais la coexistence avec un stream du même nom déjà chargé par un autre snippet ou un autre stage fait que PipeWire suffixe automatiquement.
- **Cause racine** : pas de clé stable. Idéalement `mapping` devrait stocker une clé sémantique (`{"kind":"aes67_recv","name":"x"}`, `{"kind":"bt","mac":"…","type":"capture"}`) et la résoudre à la demande, pas un `node.name` brut.

### 2.5 État disque vs runtime (BT bonds, AES67 confs, mappings JSON, `_active_bridges` dict)

- BlueZ : bonds sur disque sous `/var/lib/bluetooth/<adapter>/<MAC>/info` mais hors D-Bus quand le speaker est hors ligne. `api/bluetooth.py:234-280` + `:283-322` — `_list_disk_bonds` via le helper sudo `phonon-bt-list-bonds`, puis merge avec `live` dans `list_paired_devices`. Triple source de vérité (D-Bus, disque, in-memory du daemon).
- AES67 : `api/aes67.py:454-499` `restore_existing_aes67` re-popule `_active_streams` depuis les `.conf` snippets, par regex (heuristic parse). Si quelqu'un édite un snippet à la main, ou s'il y a une version PipeWire qui change la syntaxe, le parser silently degrade (`name_match.group(1) if name_match else stream_id`).
- Mappings : `mappings/store.py` JSON sur disque ; `_active_bridges` (`bluealsa_bridge.py:22`) n'est qu'en mémoire. Un crash daemon perd `_active_bridges` mais pas les processus shell ; au redémarrage `cleanup_stale_bridges` doit `pkill` à l'aveugle.
- AES67 streams : leur conf vit sous `~/.config/pipewire/pipewire.conf.d/phonon-aes67-*.conf` (côté user), mappings sous `/var/lib/phonon/`, BT bonds sous `/var/lib/bluetooth/` (root). Trois owners, trois mécanismes de vérité, aucune couche d'abstraction commune.
- **Cause racine** : pas d'inventaire central du runtime. Chaque sous-système gère son propre cache (registre `_active_bridges`, dict `_active_streams`, JSON store, D-Bus, disque) et ils sont reconciliés ad hoc à chaque "replay" / "sync".

### 2.6 Self-update — modes prod vs dev, fast vs full, file modes

- `api/update.py:36-53` — `_detect_mode` discrimine prod/dev/readonly mais le test prod tient à la présence de `/etc/phonon/repo-path` ET `/usr/local/sbin/phonon-update` ; sur un host à demi-installé (ex. `repo-path` créé mais symlink cassé) on retombe en `readonly` sans message clair.
- `deploy/update.sh:80-88` — `NEEDS_FULL` détecté par `git diff --name-only` sur `deploy/*` ou `*/pyproject.toml`. Or `chmod +x` sur les scripts deploy à `install.sh:435-447` *salit le worktree* ce qui fait que le prochain `update_status` rapporte `dirty=true` (`api/update.py:166-167`) et le `git pull --ff-only` refuse.
- `api/update.py:299` (mode dev) vs `deploy/update.sh:114-116` (mode prod) : le mode dev fait juste `git pull` et invite l'utilisateur à relancer. Le mode prod fait `pip install --force-reinstall --no-deps` puis `systemctl --user restart` via `systemd-run`. Pas de chemin "dev avec restart".
- `deploy/install.sh:152-153` + `:425` — chmod récursifs systématiques à chaque run de install.sh, `chmod -R g+rwX "${REPO_ROOT}"` modifie potentiellement des modes git-tracked (`.git/objects`, `.gitignore`'d files inclus dans le scope si l'utilisateur a déjà cloné comme un autre user).
- `deploy/install.sh:460` puis `:623` — restart `phonon-bt-agent.service` puis `user@${PHONON_UID}.service`. Le second tue le daemon `phonon-stage` lui-même sans `--no-block` ni avertissement.
- **Cause racine** : le détecteur fast/full se base sur des heuristiques de chemin de fichier, sans contrat. Et `install.sh` modifie le worktree (mode bits) au cours de son exécution, ce qui produit des cycles "install salit l'arbre, update refuse, full install nettoie et resalit".

---

## 3. Hot-spots par fichier

### `api/system.py`

- `collect_services` (`:504-563`) — pour 10 services, lance en parallèle `_service_installed` (`systemctl cat`) + `_service_active_*` (`systemctl is-active`) + `_service_enabled` (`systemctl is-enabled` × 1-2 unités) + `_can_sudo_systemctl` (`sudo -n systemctl is-active`) + `MainPID show` + `Type show`. **Justification : non.** 6 forks/service × 10 services = 60 forks, dont 10 traversent polkit (`sudo`).
- `_collect_resources` (`:754-875`) — appelle `_read_cpu_pct` (sleep 800 ms !) puis lit /proc/meminfo, /proc/stat, /proc/net/dev, fork `vcgencmd measure_temp` + `vcgencmd get_throttled` + `ip route`. **Justification partielle** : le sleep 800 ms vient explicitement (ligne 743) du besoin de stabiliser la mesure CPU bursty Pi 3 — c'est une bonne idée mais ça occupe le worker async pendant ~1 s par tick.
- `_read_cpu_pct` (`:722-751`) — sleep 800 ms exclusif, c'est en pratique 16 % du temps de la boucle WS. À déplacer dans une tâche ticker indépendante qui maintient juste un `_last_cpu_pct` à publier.

### `api/aes67.py`

- `replay_audio_state` (`:340-425`) — séquence destructive : `pkill -f` × 4 patterns, `asyncio.sleep(0.3)`, recreate chaque bridge, puis 3 retries de `resync_mappings` avec sleeps 1.5+1.0+1.0 s. Total : potentiellement 4 s d'I/O bloquant + N forks shell. Justifié seulement comme remède post-restart, pas comme chemin chaud.
- `sync_bt_state` (`:292-337`) — version allégée : `sync_bridges_impl` puis 3 retries `resync_mappings`. Lance ~1 fork par PCM bluealsa + 1 par bridge à recréer. Justifié si appelé seulement sur événement (bt connect, bluealsa restart) — ce qui est le cas — mais le coût se cumule avec le `_system_loop` PID-delta watcher.
- `_restart_pipewire` (`:428-451`) — `systemctl --user restart pipewire wireplumber pipewire-pulse` + sleep 2.5 s + replay. C'est appelé sur **chaque** create/delete AES67 stream (`:571`, `:599`). Coût : 3-5 s d'audio coupé + replay complet. **Non justifié** : `pw-cli load-module` natif ferait le job sans restart (le commentaire ligne 9-11 le reconnaît : « worth replacing with native protocol load-module later »).
- `_sap_announce_loop` (`:240-273`) — coût négligeable (UDP send-to local), justifié.

### `api/bluealsa_bridge.py`

- `create_bridge` (`:131-272`) — chaque création : `pactl list modules short` (fork shell) → loop python pour matcher → `pactl unload-module` (fork) si conflit → `pactl load-module module-null-sink` (fork) → `create_subprocess_shell(while true …)` (fork). **Justifié** sur le chemin "ajout", **gaspilleur** dans `replay_audio_state` qui boucle dessus pour N bridges.
- `sync_bridges_impl` (`:295-327`) — pour chaque PCM listé : `create_bridge` complet. Si N PCMs et K bridges actifs, c'est O(N) appels, chacun avec 3-4 forks pactl. À 5 BT devices, ~20 forks par sync.
- `_list_bluealsa_pcms` (`:58-128`) — un seul `bluealsa-aplay --list-pcms` puis parsing pure-Python. Justifié.

### `api/ws.py`

- `_levels_loop` (`:37-75`) — toutes les 250 ms (4 Hz), pour chaque bridge **capture**, fork `parec` (`api/levels.py:30-39`) qui tourne 20 ms puis `proc.kill()`. À 3 captures actives : **12 forks/sec** dont chacun crée un nouveau client PW (qui force WirePlumber à recalculer la graph). C'est très probablement le coup principal du WirePlumber 25-45 % côté émetteur.
- `_system_loop` (`:107-195`) — déjà couvert : 60+ forks toutes les 5 s.
- `_maybe_resync_stale_mappings` (`:198-268`) — appelle `pw_dump` (fork) toutes les 5 s, alors que le cache pw-dump TTL est 5 s (`pipewire/cli.py:70`) — pile à la limite, donc presque toujours un fork frais. Justifié *si* la détection stale est rare ; ce qui rate, c'est de pousser ce chemin sur l'événement `pw-mon` au lieu d'un poll.
- `_alerts_loop` (`:271-303`) — 1 fork `vcgencmd` toutes les 10 s, alerts publiés seulement au changement. Justifié.

### `api/bluetooth.py`

- `factory_reset` (`:200-231`) — un seul `sudo phonon-bt-reset` détaché (start_new_session=True). Justifié.
- `list_paired_devices` (`:283-322`) — D-Bus `GetManagedObjects` + `_list_disk_bonds` qui fork `sudo phonon-bt-list-bonds`. C'est le merge disque + D-Bus. **Non justifiable durablement** : le merge devrait être le boulot du backend D-Bus, pas du router.
- `scan` (`:111-130`) — délégué au backend (`bt_backend.start_scan`). Le coût réel est dans `bluetooth/real.py` (D-Bus + bluetoothctl interactif).

### `api/ptp.py`

- `_query_pmc_state` (`:84-155`) — 2 forks `sudo pmc` par hit `/ptp/status`. Court (timeout 2 s), justifié à la fréquence d'un hit UI (~5 s).
- `_read_journal_state` (`:158-225`) — fork `journalctl -t ptp4l -n 2000` à chaque fallback. **Non justifié** : 2000 lignes c'est plusieurs MB de lecture journal, sur Pi 3 c'est lent (commentaire ligne 168 admet « ~2-3 hours of subscriber spam »). Devrait soit s'abonner à `sd_journal_seek_tail` soit utiliser uniquement pmc avec retry.

### `api/update.py`

- `update_status` (`:119-196`) — 5-6 forks `git` par appel (`rev-parse`, `log -1`, `fetch --quiet`, `rev-list --count`, `status --porcelain`). L'UI tab Système hit cet endpoint à chaque visite. Justifié sauf le `fetch` qui est réseau et peut bloquer 10 s. À TTL-cacher.
- `apply` (`:247-311`) — chemin prod : 1 fork `sudo phonon-update`. Chemin dev : 3-4 forks git. Justifié à la demande.

### `api/levels.py`

- `_read_peak` (`:23-65`) — fork `parec` + `proc.stdout.read(N bytes)` avec timeout 0.3 s + `proc.kill()` + struct.unpack pour 1920 samples. **Le hot path absolu** quand `_levels_loop` tourne : 4 fois par sec × N captures. Chaque parec ouvre un client PW, qui demande à WirePlumber un nouveau lien sur `bt_X.monitor`. C'est ce qui mange le WirePlumber émetteur.

### `mappings/service.py`

- `resync_mappings` (`:206-237`) — pour chaque mapping non-mute, `_reresolve_mapping_ports` (1 `pw-dump` via cache) + `_create_links` (N forks `pw-link`) + `_apply_volume` (`wpctl set-volume` + `wpctl set-mute`). Pour 10 mappings : ~30+ forks. Justifié comme one-shot post-restart, mais l'appeler via 3 retries dans `replay_audio_state` (`api/aes67.py:413-419`) triple la facture.
- `_apply_volume` (`:285-297`) — 2 forks `wpctl` (volume + unmute) **à chaque update de mapping**. Acceptable au PATCH UI, doublonné par chaque resync.

---

## 4. Ce que je couperais pour soulager le Pi 3

1. **Couper `_levels_loop`** (`api/ws.py:37-75`) ou le ramener à 1 Hz uniquement sur le bridge actuellement focus dans l'UI.
   - Suppression : ~12 forks/sec parec + autant de re-routages WP.
   - Ce qui souffre : le VU meter en temps réel sur 3+ entrées BT capturées simultanément. La doc CLAUDE.md mentionne « meters Ardour-style » ; à 1 Hz ça bouge encore mais c'est moins fluide.
   - Alternative propre : un seul `pw-cat --record --raw` ou `pw-link` + un buffer ring partagé, OU lire les niveaux depuis le module-meter de PipeWire (qui exporte par paramètre, sans subprocess).

2. **Remplacer `_system_loop` par un agrégateur D-Bus systemd**.
   - Suppression : 60+ forks toutes les 5 s côté `collect_services` + le PID-delta watcher (qui compare `s.pid` entre ticks). systemd émet `JobNew`, `Reloading`, `PropertiesChanged` sur `org.freedesktop.systemd1`. Une seule connexion D-Bus tient le rôle.
   - Ce qui souffre : rien, le résultat est strictement plus exact (les transitions sont vues immédiatement, pas à la prochaine tick 5 s).
   - Quick win intermédiaire : passer `_service_active_*` + `_service_enabled` + `_service_installed` + `MainPID show` + `Type show` à un seul `systemctl show <unit> -p ActiveState,UnitFileState,LoadState,MainPID,Type` (1 fork au lieu de 5).

3. **Supprimer le shell-wrapper `while true; do … done` au profit de `module-loopback` natif PipeWire** (ou des templates systemd `--user`).
   - Suppression : la chaîne `parec | pacat` (côté ws + côté bridge) est purement du contournement ; PipeWire sait connecter `bluealsa.X.monitor` → `null-sink-Y` en interne via `pw-link` ou `module-loopback`. Plus de `pkill -f`, plus d'orphelins, plus de WirePlumber qui recalcule la graph à chaque parec lifecycle.
   - Ce qui souffre : il faut investir 1 session pour valider que le WirePlumber policy file laisse passer ces liens (peut nécessiter un `[matches]` rule). Le wrapper actuel marche, c'est juste cher.

4. **Couper `_read_cpu_pct` du chemin chaud** (`api/system.py:722-751`).
   - Suppression : le sleep 800 ms exclusif dans `_collect_resources` qui bloque la coroutine de la boucle WS toutes les 5 s. L'UI doit attendre 800 ms après chaque update.
   - Remplacement : un ticker indépendant qui sample /proc/stat toutes les 1 s en background et publie une moyenne glissante (3 s window). `_collect_resources` lit la moyenne courante, retour instantané.
   - Ce qui souffre : rien.

5. **Désactiver `_read_journal_state` et garder seulement pmc** (`api/ptp.py:158-225`).
   - Suppression : `journalctl -t ptp4l -n 2000` à chaque fallback pmc rate. Sur Pi 3 c'est plusieurs centaines de ms.
   - Remplacement : si pmc échoue, retourner `role="unknown"` immédiatement et compter sur le retry suivant (pmc échoue rarement quand le sudoers est posé).
   - Ce qui souffre : un dev qui lance ptp4l à la main hors systemd ne verra plus son rôle dans l'UI. Cas marginal.

---

## 5. Ce qui est design-level vs incidental

| # | Issue | Catégorie | Pourquoi |
|---|-------|-----------|----------|
| 1 | WirePlumber émetteur 25-45 % CPU | **design — needs rework** | Conséquence directe de §2.2 (shell-wrapper bridges) + §3 `_levels_loop`. Tant qu'on fork un parec/pacat/aplay par bridge, WP recalcule sa graph. La cure est `module-loopback` ou template systemd. |
| 2 | CPU swing 5-95 % en 200 ms | **patchable fragility** | `_read_cpu_pct` mesure 800 ms sample exact, et le bursty est le polling lui-même. Couper §4-1 + §4-2 + §4-4 lisse à 30 % steady-state. |
| 3 | AES67 craquelle / drift d'horloge | **design — needs rework** | Le receveur a `recv_buffer_ms=200` sans correction de clock domain. Sans phc2sys (pas de PHC sur Pi 3), le clock système dérive vs le sender. Solution propre = WirePlumber alignment via `clock.quantum` synchrones et `module-rtp-source` configuré avec `sess.media_clock` : actuellement le snippet (`api/aes67.py:158-185`) ne pose pas `sess.ts-direct`/`sess.ts-refclk`. À regarder en priorité. |
| 4 | Bridges shells produisent des orphelins | **design — needs rework** | Cf. §2.2. C'est l'auto-réparation `while true` qui crée les orphelins, pas un bug à patcher dans le `pkill`. |
| 5 | BT speakers droppent du D-Bus | **patchable fragility** | BlueZ standard behavior — speakers passent en standby. Le merge disque (`_list_disk_bonds`) est la bonne approche, à factoriser proprement dans `bluetooth/real.py.list_paired_devices` au lieu d'être dans le router. |
| 6 | /system/services spawn 60+ forks/5s | **design — needs rework** (mais quick win possible) | Cf. §3 `system.py`. Refactor 1-fork-par-service via `systemctl show -p A,B,C,…` est un quick win immédiat ; le passage D-Bus event-driven est le rework long. |
| 7 | Self-update cycles (chmod salit l'arbre, etc.) | **patchable fragility** | Cf. §2.6. Trois corrections ponctuelles : (a) `install.sh` ne `chmod +x` que si bit absent, (b) `update.sh` ignore explicitement les changements de mode bit pour le diff `dirty`, (c) `_detect_mode` distinct entre "prod broken" et "readonly". |
| 8 | Mappings stale après PW restart, name mismatch | **design — needs rework** | Cf. §2.4. La clé doit être sémantique, pas un node.name brut. Petit modèle : `mapping.source_ref = {kind: "aes67_recv", name: "x"}` résolu à la demande. |
| 9 | Disk vs runtime state (BT bonds, AES67, mappings, _active_bridges) | **design — needs rework** | Cf. §2.5. Propose d'extraire un `RuntimeInventory` (un seul module) qui possède le mapping {persistent ref ↔ runtime handle} pour BT, AES67 streams, mappings. Toutes les "replay/sync/resync" passent par lui. |
| 10 | `_restart_pipewire` à chaque create/delete AES67 | **design — needs rework** | Cf. §3 `aes67.py`. C'est l'origine du commentaire "PipeWire restart is required after each create/delete because the runtime `pw-cli load-module` path is fragile". À 3-5 s par opération + replay complet, c'est une UX désastreuse. Native `pw-cli load-module module-rtp-sink ...` est faisable, ou `pw-config rescan`. |

**Rewrites qui paient le plus** :
- (a) Remplacer le bridge shell par `module-loopback` PipeWire — supprime à la fois (1), (4), et améliore (2). **Le seul rewrite qui paie sur 3 axes simultanément.**
- (b) Regrouper le polling systemd via D-Bus events — supprime (6), améliore (2).
- (c) Stocker les refs sémantiques dans les mappings — supprime (8), simplifie (9).

---

## 6. Quick wins vs deep refactors (ordonnés)

| # | Action | Type | Impact | Risque | Durée |
|---|--------|------|--------|--------|-------|
| 1 | `collect_services` : fusionner les 5 forks en 1 `systemctl show -p ActiveState,UnitFileState,LoadState,MainPID,Type` | quick | -40 forks/5s | bas (parser change mais formats stables) | < 1h |
| 2 | `_levels_loop` : passer de 4 Hz à 1 Hz, et seulement sur le bridge focus UI (envoyé via WS message du client) | quick | -75 % parec/sec, tape direct sur WP émetteur | bas | < 1h |
| 3 | `_read_cpu_pct` : extraire dans un ticker background indépendant, `_collect_resources` retourne la moyenne courante | quick | -800 ms latence par /system/resources hit | bas | < 1h |
| 4 | `update_status` : TTL-cacher le `git fetch` (60 s) | quick | -1 fork réseau par hit UI | bas | < 30 min |
| 5 | `_read_journal_state` : retourner `unknown` si pmc échoue, supprimer le journalctl 2000 | quick | -300 ms par /ptp/status | bas (UX mineur sur dev hors-systemd) | < 30 min |
| 6 | `install.sh` : ne chmod +x que si nécessaire (`stat -c '%a'` check), ne pas `chmod -R g+rwX` à blanc | quick | supprime cycle dirty/full-install | moyen (régressions possibles si modes existants foireux) | 1-2 h |
| 7 | Mapping refs sémantiques (`{kind, name|mac|stream_id}`) au lieu de `node.name` | refactor | supprime resync skip silencieux | moyen (migration JSON store) | 1 session |
| 8 | Bridges via `module-loopback` PipeWire (et/ou `systemd --user` template) | refactor | -25-45 % WP CPU émetteur, plus d'orphelins | moyen-haut (validation WP policy) | 2 sessions |
| 9 | Polling systemctl → D-Bus event subscription (`org.freedesktop.systemd1`) | refactor | -50+ forks/5s, latence transition immédiate | moyen (gérer reconnect D-Bus) | 1-2 sessions |
| 10 | `_restart_pipewire` → `pw-cli load-module module-rtp-{sink,source} …` natif | refactor | -3-5 s coupure audio par AES67 op + suppression replay | haut (chemin signalé fragile dans le code) | 2-3 sessions |
| 11 | AES67 clock domain : ajouter `sess.ts-refclk=ptp=domain.0` côté send et recv | refactor | corrige craquelures / drift | moyen (à valider sur 2 Pi en bridge) | 1 session |
| 12 | `RuntimeInventory` central pour réconcilier disque/D-Bus/mémoire | refactor | élimine la classe entière de "stale state" | haut (touche 6 modules) | 3-4 sessions |

L'enchaînement le plus rentable :
**1 → 2 → 3 → 5 → 4 → 6** en une session (gain immédiat sur tous les symptômes
CPU mesurés), puis **8 → 11** sur deux sessions ciblées audio (le vrai problème
de qualité AES67 se résoudra avec 11 + un bridge propre). **9 et 12** sont des
investissements à plus long terme qui n'ont de sens qu'une fois 1-8 stables.

---

## Notes de portée

- Audit lecture-seule. Aucune modification de code.
- Les références file:line ci-dessus sont le code courant sur la branche `phase-1-stage-foundations` au moment de la lecture.
- Les estimations CPU/% sont basées sur l'analyse statique + les symptômes rapportés ; à confirmer avec un `perf top` ou `py-spy` sur le Pi en charge réelle. Le candidat numéro 1 à profiler est WirePlumber pendant que `_levels_loop` tourne, pour confirmer que c'est bien l'arrivée/départ du parec qui domine.
