# DSP plugin inserts

> Référence interne pour le système de plugins LADSPA sur les Outputs
> du mixer. Pour le code, voir `stage/src/phonon_stage/dsp/`,
> `stage/src/phonon_stage/mixer/filter_chain.py`,
> `stage/src/phonon_stage/api/dsp.py`, et `static/standalone/index.html`
> (section "DSP plugin inserts").

## Architecture

```
[source] --pw-link--> phonon_master --capture--> [filter-chain.fx] --playback--> [output sink]
                                                       │
                                       LADSPA plugin loaded inline
                                       (e.g. LSP comp_delay_stereo)
```

* **phonon_master** est un null-sink pactl chargé par le mixer. Tous les
  sources y écrivent via pw-link. Tous les outputs y lisent via :
    * `module-loopback` quand l'output n'a pas de plugin (delay/latency via `latency_msec`)
    * `module-filter-chain` quand l'output a un `PluginInsert` actif
* **filter-chain.service** est un user systemd unit qui héberge une
  instance pipewire dédiée pour les chaînes filter-chain. Sa conf est
  chargée via les drop-ins `~/.config/pipewire/filter-chain.conf.d/*.conf`.
* Chaque chaîne créée par le mixer écrit un fichier
  `phonon-phonon_fx_<output_id>.conf` dans ce dossier.

## Scope (v1)

Les plugins ne sont déployés que sur **stage-x99** (Optiplex 3070,
x86_64) — les Pi 3B n'ont pas la puissance CPU pour ajouter du LADSPA
au-dessus de l'I/O audio + bluealsa bridges. Voir
`/capabilities.plugins_available` (gated sur `platform.machine()`).

## Comment ajouter un plugin au catalogue

1. **Backend** (`api/dsp.py`) — ajouter une entrée à `_V1_CATALOG` :
   ```python
   {"backend": "ladspa", "library": "lsp-plugins-ladspa",
    "label": "http://lsp-plug.in/plugins/ladspa/<plugin_label>",
    "name": "Friendly Name", "category": "..."}
   ```
2. **Frontend** (`static/standalone/index.html` → `PLUGIN_LAYOUTS`) —
   définir le layout des sections (Signal / Time / etc.), le
   `monitoring` block, et la fonction `computeMonitoring(values)` si
   le plugin expose des metering values qui doivent être dérivées
   côté client (voir "Monitoring" plus bas).
3. Pas besoin de hardcoder les controls — l'introspection LADSPA
   les remonte automatiquement et l'UI les rend en knobs/toggles/selects.

## Gotchas PipeWire filter-chain

### Naming des nodes

Le module crée **trois nodes** par chaîne :
* Un node "fictif" nommé comme le `node.name` de la conf (`phonon_fx_<id>`)
  — pas toujours visible dans `pw-dump`.
* `input.<chain>` (Stream/Input/Audio) — le côté capture
* `output.<chain>` (Stream/Output/Audio) — le côté playback

Les **controls du plugin sont exposés sur le stream node** (`input.<chain>`),
pas sur le node fictif. `RealPipeWireBackend._resolve_filter_chain_node`
gère la résolution par fallback : essaie d'abord le nom littéral,
puis `input.<chain>`, puis `output.<chain>`.

### Préfixe `fx:` sur tous les controls

PipeWire préfixe chaque control du filter-graph par le `name` du node
dans le graph. Notre conf utilise `name = fx`, donc tous les controls
LSP apparaissent comme `fx:Mode`, `fx:Time (ms)`, etc. dans la
spa-pod de `Props.params`.

* Pour `set-param`, il **faut envoyer le préfixe** : `{ params = [ "fx:Time (ms)" 80.0 ] }`.
  Sans le préfixe, pw-cli renvoie rc=0 mais le set est silently no-op.
* Pour la lecture, le parser strip le préfixe avant de remonter le dict
  au reste du code (UI / mixer service).

### Output ports LADSPA non surfacés

PW `module-filter-chain` **n'expose pas les ports de sortie de control**
du plugin (les "meters" comme `Delay time (ms)`, `Delay samples`, etc.).
Seuls les ports d'entrée settable sont visibles via `Props.params`.

Conséquence : la section **Monitoring** de l'UI calcule les valeurs
côté client à partir des inputs lus depuis PW + une formule physique
définie dans le `PLUGIN_LAYOUTS[<label>].computeMonitoring(values)`.
Mathématiquement identique à ce que l'engine calcule (même formule,
même précision), juste dupliqué côté JS.

### Reload de filter-chain.service ⇒ wipe pactl

Le restart du service `filter-chain.service` cascade dans
`pipewire-pulse` et décharge **TOUS les modules pactl** :
`phonon_master`, `airplay_in`, AES67 send-sinks, n'importe quoi
chargé via `pactl load-module`.

* `_cleanup_orphan_chains` au boot ne reload **plus aveuglément** —
  il ne touche au service que si la diff des confs est non vide.
* `_apply_filter_chain_diff` (sur attach/detach plugin) reload une
  fois et appelle ensuite `_resync_all_chain_controls`.
* Si tu vois `phonon_master` ou `airplay_in` disparaître inopinément,
  c'est probablement qu'un reload a passé là (vérifie les logs
  `mixer.filter_chain_reconciled`). Self-heal dans `_reconcile` rattrape.

### Reload ⇒ controls reviennent aux défauts LADSPA

Quand la chaîne redémarre, le plugin perd toutes les valeurs courantes
et revient aux defaults du descriptor LADSPA. Sans le resync, l'UI
afficherait des valeurs persistées que l'engine n'applique pas.

`_resync_chain_controls_for_output` :
1. Poll `list_nodes()` toutes les 200ms jusqu'à voir `input.<chain>`
   apparaître (timeout ~3s)
2. Pour chaque control persisté, appelle `set_filter_node_control`

Important : appelé après **tout** reload (`_cleanup_orphan_chains`,
`_apply_filter_chain_diff`, l'endpoint admin `cleanup-orphan-chain`).

## Gotchas LSP plugins

### `Dry/Wet balance (%)` est inversé

* `balance = 100%` → 100% **wet** (signal délayé pur)
* `balance = 0%`  → 100% **dry** (signal direct, **PAS de delay**)

Pour un delay pur audible, mettre balance=100. Pour un blend, ajuster
entre 0 et 100. Cas du delay compensator : balance=100, Wet amount=1,
Dry amount=0, Mode=Time.

### `Mode` switch active une section, ignore les autres

Le plugin a 3 unités de delay (Samples / Distance / Time). `Mode`
sélectionne **laquelle est active** :
* Mode=0 → `Samples (samp)` drive le delay, les autres ignorés
* Mode=1 → `Meters (m)` + `Centimeters (cm)` + `Temperature (°C)`
  drivent le delay (Distance / vitesse du son)
* Mode=2 → `Time (ms)` drive le delay

Le knob `Time (ms)` peut être à 1000ms mais si Mode=0, le plugin
utilise `Samples=0` → delay = 0. L'UI met en surbrillance cyan la
section active correspondant au Mode courant.

### `Ramping` recommandé pour zéro-clic

`Ramping = 1` active l'interpolation du delay lors des changements de
valeur. Sans Ramping, modifier `Time (ms)` produit un clic audible
(saut abrupt du delay). Avec Ramping, le plugin interpole sur
quelques quanta — change inaudible.

Note : LSP ne déclare PAS `PORT_HINT_TOGGLED` pour Ramping (ni Bypass
ni Phase Invert L/R), donc l'UI les rend comme knobs au lieu de
toggles. Set à 1 manuellement.

### `comp_delay_stereo` = "Compensation Delay" (pas Compressor)

Malgré le nom de l'URI, c'est le delay de compensation générique,
pas un sous-bloc de compresseur. Range max 1000ms (1 seconde).

### lsp-plugins-ladspa.so dans le dir multiarch Debian

Sur Ubuntu Studio 26.04, la lib LSP est dans
`/usr/lib/x86_64-linux-gnu/ladspa/` (Debian multiarch convention).
LADSPA_PATH par défaut (`/usr/lib/ladspa`) ne couvre pas le multiarch.

`RealLadspaIntrospector` seed `LADSPA_PATH` avec
`/usr/lib/ladspa:/usr/lib/x86_64-linux-gnu/ladspa:/usr/local/lib/ladspa`
pour résoudre la lib indépendamment du layout distro.

## Autres gotchas du Stage

### `wpctl set-mute` ne silence pas les sinks ALSA

Sur ce build (PW 1.6.2 / WirePlumber 0.5.x sur Ubuntu Studio 26),
`wpctl set-mute <id> 1` renvoie rc=0 mais l'audio passe quand même
vers le speaker. Pas une exception qu'on puisse catcher — silent
fail au niveau wpctl.

Solution dans `_reconcile` : on passe par
`set_node_channel_volumes(node_name, [0.0, 0.0])` qui hit
`pactl set-sink-volume` (le post-mix gain stage). Fiable.
Le `set_node_mute` reste appelé par défensive mais ne fait rien sur
ces sinks.

### `analyseplugin` écrit sur stderr

L'outil LADSPA SDK dump son descriptor sur **stderr**, pas stdout
(c'est un outil de diagnostic, design choice). `cli.run_command`
ne capture que stdout, donc le parser voyait toujours vide.

`RealLadspaIntrospector` fait son propre `create_subprocess_exec`
qui combine stdout + stderr avant de passer au parser.

## Endpoints utiles

### `/dsp/plugins`
Catalogue v1. Liste les plugins disponibles avec backend/library/label.

### `/dsp/plugins/schema?library=...&label=...`
Renvoie le descriptor LADSPA introspecté (ports, ranges, hints).
404 si le plugin est inconnu, 503 si l'introspector n'est pas câblé
(Pi).

### `GET /mixer/outputs/{id}/insert/monitoring`
Lit les controls live du moteur via `pw-cli enum-params Props`.
Renvoie les valeurs avec le préfixe `fx:` strippé.

### `GET /mixer/outputs/{id}/insert/monitoring?debug=1`
Mode diagnostic : ajoute le raw output `pw-cli enum-params`, le
dernier appel `set-param` capturé, et le log de `wpctl set-mute`
par node-id. Utile quand un control ne propage pas.

### `POST /mixer/admin/cleanup-orphan-chain?filename=<conf>`
Supprime un fichier de conf filter-chain par nom (pas restreint au
préfixe `phonon-`), reload le service, re-ensure phonon_master,
reconcile. Pour nettoyer des confs de test ad-hoc.

## Diagnostic d'un problème de plugin

Quand l'utilisateur dit "le delay ne marche pas" :

1. **Lire l'état UI vs engine** :
   ```bash
   curl /mixer/outputs/<id>/insert/monitoring
   curl /mixer | jq '.outputs[] | select(.id=="<id>") | .insert.controls'
   ```
   Comparer chaque control. Si désync, problème de resync ou de set-param.

2. **Vérifier les liens du sink cible** :
   ```bash
   curl /pipewire/links + /pipewire/nodes + /pipewire/ports
   ```
   Cherche tous les liens qui arrivent sur le sink. Si plus d'un node
   feed le sink (autre filter-chain orphelin, loopback en parallèle),
   audio doublé → écho ou comportement bizarre.

3. **Vérifier les chaînes loaded** :
   ```bash
   ls ~/.config/pipewire/filter-chain.conf.d/
   ```
   Toutes les confs `phonon-phonon_fx_*.conf` doivent matcher des
   outputs avec `insert.enabled=True`. Toute autre conf est orpheline.

4. **Vérifier `pw-cli set-param` directement** :
   ```bash
   curl -X PATCH /mixer/outputs/<id>/insert/controls/Time%20%28ms%29 -d '{"value": 500}'
   curl /mixer/outputs/<id>/insert/monitoring?debug=1 | jq .last_set_param
   ```
   Si rc=0 mais l'engine n'a pas la valeur, c'est probablement le
   préfixe `fx:` (déjà fixé) ou un type-mismatch (Float envoyé pour
   un param Int — pas confirmé comme problème jusqu'ici, mais à
   surveiller pour les prochains plugins).

5. **Vérifier que la chaîne est en place côté audio** :
   La sink_input du filter-chain doit être linkée aux ports de sortie
   du sink ALSA. Si manquant, le chain produit du son qui ne va
   nulle part.
