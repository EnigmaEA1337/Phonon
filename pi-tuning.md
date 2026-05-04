# Optimisations système Raspberry Pi 3B pour Phonon

Ce document liste les optimisations à appliquer sur les Pi 3B qui hébergent un `phonon-stage`. Elles sont organisées par **étape de dev Phonon** : on n'applique pas tout d'un coup, on intègre les optimisations au fur et à mesure que les fonctionnalités correspondantes sont développées.

L'idée est que `install.sh` applique automatiquement les optimisations pertinentes selon le rôle de la machine et l'étape de dev en cours, plutôt que d'imposer un tuning monolithique.

---

## Vue d'ensemble : ordre d'application

| Étape Phonon | Optimisations à appliquer | Pourquoi à ce moment |
|---|---|---|
| **Étape 1** (Stage Agent fondations) | 1, 2, 3, 5, 7 | Base propre dès l'install |
| **Étape 2** (Controller) | — | Concerne le 3070, pas les Pi |
| **Étape 3** (Adoption / standalone) | — | Pas d'optimisation supplémentaire |
| **Étape 4** (Audio basique PipeWire) | 4, 8 | CPU governor performance pour audio temps réel + nice/SCHED_FIFO |
| **Étape 5** (Routing audio local) | 8 (suite) | Confirmation du tuning RT |
| **Étape 6** (Bluetooth) | — | Aucun tuning spécifique BT |
| **Étape 7** (AES67 + bridge inter-Pi) | 9 | Network buffer tuning |
| **Étape 8** (DSP) | 4 (kernel RT) | Si nécessaire pour la latence |
| **Étape 9** (Console) | — | — |

---

## Liste détaillée des optimisations

### 1. Désactiver les services systemd inutiles

**Quand** : Étape 1 (install initial).

**Pourquoi** : Bookworm Lite charge quelques services qui ne servent pas pour Phonon. Les désactiver libère ~50-80 Mo de RAM et ~2-3 % CPU constant.

**Commandes** :

```bash
sudo systemctl disable --now triggerhappy.service        # gestion touches GPIO, inutile
sudo systemctl disable --now ModemManager.service        # modems, inutile
sudo systemctl disable --now hciuart.service             # gère le BT intégré (sera désactivé en optim 2)
sudo systemctl disable --now keyboard-setup.service      # si pas de clavier branché
sudo systemctl disable --now console-setup.service       # idem

# wpa_supplicant : désactiver UNIQUEMENT si filaire uniquement
sudo systemctl disable --now wpa_supplicant.service
```

**Vérification** :

```bash
systemctl list-units --type=service --state=running | wc -l
# Avant : ~30 services
# Après : ~20-22 services

free -h
# Vérifier que la mémoire utilisée a baissé
```

### 2. Désactiver le BT intégré et le WiFi (si filaire uniquement)

**Quand** : Étape 1 (install initial).

**Pourquoi** :
- Le BT intégré (`hci0` Cypress sur UART) crée de la confusion avec les dongles USB et partage la même radio 2.4 GHz que le WiFi → interférences possibles avec les dongles BT USB.
- Le WiFi inutile sur Phonon mode filaire consomme ~5 Mo RAM et un peu de CPU constamment.

**Commandes** :

```bash
sudo nano /boot/firmware/config.txt

# Ajouter à la fin du fichier :
dtoverlay=disable-bt
dtoverlay=disable-wifi

# Reboot pour appliquer
sudo reboot
```

**Vérification** :

```bash
hciconfig -a
# Doit afficher uniquement les dongles USB (hci1, hci2...), plus de hci0 UART

ip link show
# wlan0 ne doit plus apparaître
```

### 3. CPU governor performance (latence audio meilleure)

**Quand** : Étape 1 (install initial), critique dès qu'on touche à l'audio (Étape 4).

**Pourquoi** : Par défaut le Pi utilise `ondemand` qui ajuste la fréquence CPU dynamiquement. Pour de l'audio temps réel, c'est mauvais : la fréquence change pendant un flux audio = jitter et xrun.

**Commandes** :

```bash
# Test immédiat
echo "performance" | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# Persistance via systemd unit
sudo tee /etc/systemd/system/cpu-performance.service > /dev/null <<'EOF'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now cpu-performance.service
```

**Vérification** :

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
# Doit afficher : performance

cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
# Doit afficher 1200000 (1.2 GHz, fréquence max du Pi 3B)
```

**Coût** : +1-2 W de consommation, le Pi chauffe un peu plus. OK avec un dissipateur ou un boîtier ventilé.

### 4. Kernel real-time PREEMPT_RT (latence audio quasi-pro)

**Quand** : Étape 8 (DSP) **ou** étape 4 si on veut le tuning audio dès le départ. Pas avant l'étape 4 dans tous les cas, ça peut révéler des bugs latents dans des drivers BT/USB.

**Pourquoi** : Le kernel standard a une latence d'interruption parfois élevée (~1-5 ms pire-cas). Le kernel `PREEMPT_RT` réduit le pire-cas à <100 µs, ce qui permet de descendre le buffer PipeWire à 64 samples sans xrun.

**Commandes** :

```bash
sudo apt update
sudo apt install linux-image-rt-arm64

# Au prochain reboot, le menu de boot proposera le kernel RT
sudo reboot
```

**Vérification** :

```bash
uname -a
# Doit afficher : Linux ... 6.6.x-rt-arm64 ...
```

**Coût** : ~5 % CPU en plus pour le scheduler RT, mais largement compensé par la stabilité.

⚠️ **Attention** : à activer uniquement quand `phonon-stage` est stable. Le kernel RT peut révéler des bugs latents dans des drivers (BT en particulier).

### 5. Limiter la mémoire GPU (libère RAM pour le système)

**Quand** : Étape 1 (install initial).

**Pourquoi** : Le Pi 3B alloue par défaut 64 Mo au GPU. Pour un Pi headless qui ne fait pas de vidéo, c'est gaspillé. Sur 1 Go total, libérer 32-48 Mo c'est significatif.

**Commandes** :

```bash
sudo nano /boot/firmware/config.txt

# Ajouter (ou modifier si déjà présent) :
gpu_mem=32

# Reboot
sudo reboot
```

⚠️ **Attention** : si tu utilises la sortie HDMI audio (`vc4hdmi card 2`), garde **au moins** `gpu_mem=32`. À 16 Mo, le HDMI audio peut planter. Pour un Pi qui ne sort jamais en HDMI : `gpu_mem=16`.

**Vérification** :

```bash
vcgencmd get_mem gpu
# Doit afficher : gpu=32M

free -h
# RAM totale utilisable légèrement augmentée
```

### 6. SD card optimisée + filesystem tuning

**Quand** : Étape 1 (install initial), à appliquer **avant** que le filesystem ne soit trop rempli.

**Pourquoi** : La SD card est le goulot d'étranglement principal du Pi 3 (lecture/écriture lente vs disque SSD). Le tuning filesystem réduit les écritures inutiles, ce qui :
- Améliore la durée de vie de la SD (×2-3)
- Améliore la réactivité (~20-30 %)

**Commandes** :

```bash
sudo nano /etc/fstab
```

Modifier la ligne du `/` pour ajouter `noatime,nodiratime,commit=120` :

```
PARTUUID=xxxxxxxx-01 / ext4 defaults,noatime,nodiratime,commit=120 0 1
```

**Vérification** :

```bash
mount | grep " / "
# Doit afficher noatime nodiratime dans les options
```

**Choix de SD recommandé** :
- **A2 mandatoire** (random IOPS), pas A1
- Marques fiables : **SanDisk Industrial**, **Samsung Pro Endurance**, **Kingston Industrial**
- 32-64 Go (pas plus, inutile)
- Éviter absolument les SD no-name (mort en 6 mois sur un Pi qui écrit en continu)

### 7. Désactiver le swap

**Quand** : Étape 1 (install initial), si on est sûr que la consommation RAM reste sous 1 Go.

**Pourquoi** : Le Pi 3 avec 1 Go RAM crée par défaut un swap de 100 Mo dans `/var/swap`. Si Phonon Stage Agent + ses dépendances tournent à ~150-200 Mo, on a une marge confortable. Le swap actif provoque des écritures sur la SD, réduisant sa durée de vie.

**Commandes** :

```bash
sudo dphys-swapfile swapoff
sudo dphys-swapfile uninstall
sudo systemctl disable dphys-swapfile
```

**Vérification** :

```bash
swapon --show
# Ne doit rien afficher

free -h
# Ligne Swap: 0B 0B 0B
```

⚠️ **Coût** : si jamais Phonon déborde la RAM, OOM killer au lieu de swap. À surveiller via le monitoring (mémoire utilisée doit rester < 80 % de 1 Go).

### 8. Nice / SCHED_FIFO pour Phonon (priorité kernel)

**Quand** : Étape 4 (audio basique), à intégrer dans la systemd unit `phonon-stage.service`.

**Pourquoi** : Quand `phonon-stage` tourne et fait de l'audio, ses threads doivent passer devant les autres processus pour éviter les xrun audio sous charge réseau ou disque.

**Configuration systemd** dans `/etc/systemd/system/phonon-stage.service` :

```ini
[Service]
...
Nice=-10
IOSchedulingPriority=1
LimitRTPRIO=99
LimitMEMLOCK=infinity
```

**Configuration côté code Python** : les threads audio peuvent demander `SCHED_FIFO` :

```python
import os

def upgrade_to_realtime():
    """Appelé par les threads audio critiques au démarrage."""
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
    except PermissionError:
        # Le user phonon doit avoir RTPRIO via /etc/security/limits.d/
        pass
```

Et dans `/etc/security/limits.d/99-phonon.conf` :

```
phonon  -  rtprio   99
phonon  -  memlock  unlimited
phonon  -  nice     -10
```

**Vérification** :

```bash
systemctl status phonon-stage
# Vérifier "CPU Priority: -10" et "RT Priority: 99"

ps -eo pid,nice,cls,rtprio,comm | grep phonon
# Vérifier que les threads ont bien CLS=FF (FIFO) et RTPRIO élevé
```

### 9. Network buffer tuning (utile pour AES67)

**Quand** : Étape 7 (AES67 + bridge inter-Stage).

**Pourquoi** : AES67 envoie/reçoit des paquets RTP rapides à intervalle fixe (1ms typiquement). Augmenter les buffers réseau évite les pertes de paquets sous charge.

**Commandes** :

```bash
sudo tee /etc/sysctl.d/99-phonon-network.conf > /dev/null <<'EOF'
# Buffers réseau augmentés pour AES67
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.rmem_default = 4194304
net.core.wmem_default = 4194304
net.ipv4.udp_mem = 65536 131072 262144

# Multicast (AES67 + mDNS-SD)
net.ipv4.igmp_max_memberships = 50

# Désactiver reverse path filtering pour le multicast
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
EOF

sudo sysctl -p /etc/sysctl.d/99-phonon-network.conf
```

**Vérification** :

```bash
sysctl net.core.rmem_max
# Doit afficher : net.core.rmem_max = 16777216

# Pendant un test AES67 actif, surveiller les pertes :
netstat -su | grep -i "packet receive errors"
# Doit rester à 0 (ou très bas)
```

### 10. Boot rapide (cosmétique)

**Quand** : Étape 1 si tu veux, ou plus tard. Pas critique.

**Pourquoi** : Un Pi qui boote en 10-15 sec au lieu de 30 c'est plus agréable, surtout si tu redémarres souvent en phase de dev.

**Commandes** :

```bash
sudo systemctl disable systemd-networkd-wait-online.service
sudo systemctl disable apt-daily.service apt-daily.timer
sudo systemctl disable apt-daily-upgrade.service apt-daily-upgrade.timer
```

Optionnel — réduire les logs au boot dans `/boot/firmware/cmdline.txt` :

```
... quiet splash loglevel=3
```

### 11. Overclock léger (optionnel, à vos risques)

**Quand** : Jamais en prod, à n'envisager que si tu manques vraiment de CPU à l'étape 8 (DSP) sur Pi.

**Pourquoi** : Le Pi 3B tourne à 1.2 GHz par défaut. On peut pousser à 1.35 GHz (+12 %) sans souci si bien refroidi.

**Commandes** dans `/boot/firmware/config.txt` :

```
arm_freq=1350
over_voltage=2
core_freq=500
```

⚠️ **Pas recommandé** : mieux vaut optimiser le code que pousser le hardware. À ne faire qu'en dernier recours.

---

## Application via `install.sh`

Le script `install.sh` doit appliquer automatiquement les optimisations selon les paramètres :

```bash
# Optimisations appliquées par défaut (toujours)
install.sh --role=stage-only
# → Optimisations 1, 2, 3, 5, 6, 7, 10

# Optimisations audio (depuis l'étape 4)
install.sh --role=stage-only --enable-audio-tuning
# → + Optimisation 8 (nice/SCHED_FIFO + limits.conf)

# Optimisations réseau AES67 (depuis l'étape 7)
install.sh --role=stage-only --enable-network-tuning
# → + Optimisation 9 (sysctl buffers réseau)

# Kernel RT (depuis l'étape 8)
install.sh --role=stage-only --enable-rt-kernel
# → + Optimisation 4 (apt install linux-image-rt-arm64)

# Tout activer (production)
install.sh --role=stage-only --enable-all-tuning
# → Toutes les optimisations sauf overclock
```

Chaque optimisation appliquée doit :
1. Être **idempotente** (re-lançable sans casser)
2. Être **réversible** (un flag `--disable-X` pour annuler)
3. Logger ce qu'elle fait (pour que le Controller puisse afficher la progression)
4. Ne pas appliquer une optimisation si elle l'a déjà été (vérifier l'état avant)

---

## Tests de validation

À chaque étape, après application des optimisations correspondantes, vérifier :

| Étape | Test | Résultat attendu |
|-------|------|-----------------|
| 1 | `systemctl list-units --type=service --state=running` | < 22 services |
| 1 | `free -h` | Mémoire utilisée < 250 Mo au repos |
| 1 | `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` | `performance` |
| 1 | `vcgencmd get_mem gpu` | `gpu=32M` |
| 4 | Lancer `phonon-stage` et `pw-top` | xrun count = 0 sous charge réseau légère |
| 7 | `tcpdump -i eth0 udp port 5004` pendant un stream AES67 | Pas de paquets perdus, jitter < 1ms |
| 8 | `cyclictest -p99 -D60 -t4` (kernel RT actif) | Max latency < 200 µs |

---

## Notes pour Claude Code

- **N'appliquer aucune optimisation hors étape** : si on est à l'étape 1, ne pas pousser le tuning audio (étape 4) même si c'est tentant. Chaque optimisation doit être validée à son étape pour qu'on puisse identifier la cause si quelque chose plante.
- **Documenter chaque optimisation appliquée** dans `/var/log/phonon/install.log` avec timestamp.
- **Permettre la désactivation** : un flag `--disable-tuning` doit pouvoir tout désactiver pour debug ou comparaison de perf.
- **Tester sur Pi avant 3070** : ces optimisations sont spécifiques aux Pi. Sur le 3070 (Ubuntu Studio 26.04), beaucoup ne s'appliquent pas (CPU governor déjà OK, swap géré différemment, etc.). Voir un futur `optiplex-tuning.md` pour les optimisations x86_64.
- **Documenter dans CLAUDE.md** le pointeur vers ce fichier sous "Optimisations système" dans la section Hardware ou Workflow de dev.
