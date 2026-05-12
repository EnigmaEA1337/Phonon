#!/usr/bin/env bash
# phonon-net — privileged helper for the network write path.
#
# Subcommands:
#   apply-iface <yaml-body> <timeout-s>
#       Backup the current /etc/netplan, drop the new YAML in place,
#       netplan generate + networkctl reload, schedule a systemd-run
#       rollback that fires after <timeout-s>. Idempotent — if a
#       previous pending apply is active, it is cancelled (rolled back)
#       before the new one starts.
#
#   confirm
#       Cancel the pending rollback timer + drop the backup. The
#       config is now permanent.
#
#   cancel
#       Cancel the pending rollback timer + immediately run the
#       rollback (restore from backup). Used for "Revert now".
#
#   status
#       Print JSON: { pending: bool, expires_at: <epoch>, backup_dir: <path> }
#       Doesn't need root (the daemon reads this directly), included
#       here so callers can pipe through one path.
#
#   rollback <backup-dir>
#       Internal — called by the timer. Restore /etc/netplan from the
#       backup dir + netplan generate + reload.
#
# Why not `netplan try` directly?
# `netplan try` is interactive — it reads stdin to accept the change
# and times out otherwise. From a non-interactive sudo path it's
# fragile (FIFO redirection, PID tracking across sudo boundaries).
# Re-implementing the try semantic with systemd-run gives us the same
# auto-revert guarantee with explicit state files and proper logging,
# all from a script we control.

set -euo pipefail

NETPLAN_DIR=/etc/netplan
PHONON_FILE_NAME=99-phonon-managed.yaml
PHONON_FILE="${NETPLAN_DIR}/${PHONON_FILE_NAME}"
STATE_DIR=/run/phonon-net
PENDING_FILE="${STATE_DIR}/pending.json"
BACKUP_ROOT=/var/lib/phonon/netplan-backups
ROLLBACK_UNIT=phonon-netplan-rollback
LOG=/var/log/phonon/net.log

mkdir -p "$(dirname "$LOG")" "$STATE_DIR" "$BACKUP_ROOT"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

# Drop any pending rollback timer. Quiet because the unit may not
# exist on a clean apply path — that's normal.
cancel_timer() {
    systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl stop "${ROLLBACK_UNIT}.service" 2>/dev/null || true
    # `systemd-run` creates transient units that systemctl reset-failed
    # cleans up — without this they linger as "failed" in `systemctl list-units`.
    systemctl reset-failed "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "${ROLLBACK_UNIT}.service" 2>/dev/null || true
}

backup_netplan() {
    local stamp dst
    stamp="$(date +%Y%m%d-%H%M%S)"
    dst="${BACKUP_ROOT}/${stamp}"
    mkdir -p "$dst"
    if compgen -G "${NETPLAN_DIR}/*.yaml" > /dev/null; then
        cp -p "${NETPLAN_DIR}"/*.yaml "$dst/"
    fi
    echo "$dst"
}

write_pending() {
    local backup="$1"
    local expires_at="$2"
    cat > "$PENDING_FILE" <<EOF
{"backup_dir":"$backup","expires_at":$expires_at}
EOF
}

cmd_apply_iface() {
    local timeout="${1:-120}"
    # YAML body comes via stdin — passing multi-line bodies as a
    # CLI argument trips sudoers (wildcards forbidden when the arg
    # contains newlines), so the API writes the body to our stdin
    # and we read it here.
    local body
    body="$(cat)"
    if [ -z "$body" ]; then
        log "apply-iface: empty body on stdin — refusing"
        return 2
    fi
    case "$timeout" in
        ''|*[!0-9]*) log "apply-iface: bad timeout: $timeout"; return 2 ;;
    esac
    if [ "$timeout" -lt 30 ] || [ "$timeout" -gt 600 ]; then
        log "apply-iface: timeout out of range (30-600): $timeout"
        return 2
    fi

    # Roll back any previous pending apply BEFORE we save a new backup,
    # otherwise the new "backup" would capture the about-to-be-undone state.
    if [ -f "$PENDING_FILE" ]; then
        local prev_backup
        prev_backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        log "apply-iface: prior pending detected, rolling back to $prev_backup first"
        cancel_timer
        if [ -n "$prev_backup" ] && [ -d "$prev_backup" ]; then
            cmd_rollback "$prev_backup"
        fi
    fi

    local backup
    backup="$(backup_netplan)"
    log "apply-iface: backup → $backup"

    # Write the new YAML atomically. tempfile + rename so the netplan
    # parser never sees half a file.
    local tmp
    tmp="$(mktemp --tmpdir="$NETPLAN_DIR" phonon-net.XXXXXX.yaml)"
    printf '%s' "$body" > "$tmp"
    chown root:root "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$PHONON_FILE"
    log "apply-iface: wrote $PHONON_FILE ($(wc -l <"$PHONON_FILE") lines)"

    # netplan generate → /run/systemd/network → networkctl reload.
    # If generate fails, rollback right away so the OS isn't in a
    # half-configured state.
    if ! netplan generate 2>>"$LOG"; then
        log "apply-iface: netplan generate failed — rolling back immediately"
        cmd_rollback "$backup"
        return 3
    fi
    if ! networkctl reload 2>>"$LOG"; then
        log "apply-iface: networkctl reload failed — rolling back immediately"
        cmd_rollback "$backup"
        return 3
    fi
    log "apply-iface: applied OK, scheduling rollback in ${timeout}s"

    local expires_at=$(( $(date +%s) + timeout ))
    write_pending "$backup" "$expires_at"

    # systemd-run --on-active=Ns runs our rollback subcommand once,
    # after N seconds. The unit name is stable so confirm/cancel can
    # find it. Quiet=true keeps the spawn line out of the log.
    systemd-run \
        --unit="${ROLLBACK_UNIT}" \
        --on-active="${timeout}s" \
        --quiet \
        /usr/local/sbin/phonon-net rollback "$backup" \
        2>>"$LOG"

    return 0
}

cmd_confirm() {
    cancel_timer
    if [ -f "$PENDING_FILE" ]; then
        local backup
        backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        if [ -n "$backup" ] && [ -d "$backup" ]; then
            rm -rf "$backup"
            log "confirm: dropped backup $backup"
        fi
        rm -f "$PENDING_FILE"
        log "confirm: change made permanent"
    else
        log "confirm: nothing pending"
    fi
}

cmd_cancel() {
    cancel_timer
    if [ -f "$PENDING_FILE" ]; then
        local backup
        backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        if [ -n "$backup" ] && [ -d "$backup" ]; then
            cmd_rollback "$backup"
        else
            log "cancel: pending state has no valid backup — manual cleanup needed"
            rm -f "$PENDING_FILE"
        fi
    else
        log "cancel: nothing pending"
    fi
}

cmd_rollback() {
    local backup="$1"
    if [ -z "$backup" ] || [ ! -d "$backup" ]; then
        log "rollback: backup dir invalid: $backup"
        return 2
    fi
    log "rollback: restoring from $backup"
    # Remove our managed file first so it doesn't shadow the original.
    rm -f "$PHONON_FILE"
    # Restore every yaml that was in the backup — if the user had no
    # /etc/netplan/*.yaml before (fresh system), the backup dir is
    # empty and this is a no-op.
    if compgen -G "$backup/*.yaml" > /dev/null; then
        cp -p "$backup"/*.yaml "$NETPLAN_DIR/"
    fi
    netplan generate 2>>"$LOG" || log "rollback: netplan generate failed (continuing)"
    networkctl reload 2>>"$LOG" || log "rollback: networkctl reload failed (continuing)"
    rm -rf "$backup"
    rm -f "$PENDING_FILE"
    log "rollback: done"
}

cmd_status() {
    if [ -f "$PENDING_FILE" ]; then
        cat "$PENDING_FILE"
        echo
    else
        echo '{"pending":false}'
    fi
}

cmd_apply_macvlans() {
    # JSON body on stdin: {name: {parent, mac, dhcp4, addresses4, gateway4, dns, mtu}, ...}
    # We write 2 files per macvlan: <prefix><name>.netdev (the device
    # declaration) and <prefix><name>.network (its IP config), plus
    # ONE attach file per parent that lists the children via the
    # MACVLAN= key — without that the device is created but never
    # attached to the parent. systemd-networkd reads the whole
    # /etc/systemd/network/ tree on reload, so we wipe stale
    # phonon-prefixed files first.
    local body
    body="$(cat)"
    if [ -z "$body" ]; then
        log "apply-macvlans: empty body — refusing"
        return 2
    fi

    local managed_dir=/etc/systemd/network
    local prefix=30-phonon-macvlan-
    # Legacy: pre-fix builds wrote a sibling .network file to attach
    # children to their parent, but systemd-networkd only applies the
    # FIRST .network matching a given iface — netplan's
    # 10-netplan-<parent>.network in /run/ wins, our 35-...-attach
    # was ignored. Switched to drop-ins under <basename>.network.d/
    # which systemd-networkd merges into the matched file regardless
    # of which dir (etc / run / lib) hosts the base. The old prefix
    # is still tracked so we wipe stale sibling files on upgrade.
    local attach_prefix=35-phonon-macvlan-attach-
    mkdir -p "$managed_dir"

    # Generate the file bodies with python3 — bash JSON parsing is
    # painful, python ships with every distro we deploy on.
    local plan
    plan=$(echo "$body" | python3 -c '
import sys, json, base64, collections
data = json.load(sys.stdin)
attach_by_parent = collections.defaultdict(list)
for name, cfg in data.items():
    parent = cfg["parent"]
    mac = cfg.get("mac", "")
    dhcp4 = bool(cfg.get("dhcp4", True))
    addrs = cfg.get("addresses4") or []
    gw = cfg.get("gateway4", "")
    dns = cfg.get("dns") or []
    mtu = cfg.get("mtu") or 0
    nd = ["# Managed by phonon-stage. Edits here are overwritten.",
          "[NetDev]", f"Name={name}", "Kind=macvlan"]
    if mac:
        nd.append(f"MACAddress={mac}")
    if mtu:
        nd.append(f"MTUBytes={mtu}")
    nd += ["", "[MACVLAN]", "Mode=bridge"]
    netdev = "\n".join(nd) + "\n"
    nw = ["# Managed by phonon-stage. Edits here are overwritten.",
          "[Match]", f"Name={name}", "", "[Network]"]
    if dhcp4:
        nw.append("DHCP=ipv4")
    for a in addrs:
        nw.append(f"Address={a}")
    if gw:
        nw.append(f"Gateway={gw}")
    for d in dns:
        nw.append(f"DNS={d}")
    network = "\n".join(nw) + "\n"
    print(f"D\t{name}\t{parent}\t{base64.b64encode(netdev.encode()).decode()}\t{base64.b64encode(network.encode()).decode()}")
    attach_by_parent[parent].append(name)
for parent, children in attach_by_parent.items():
    # Drop-in body: extends the parent .network with MACVLAN= keys
    # WITHOUT overriding the rest of the [Network] section. A drop-in
    # only needs the section header + the keys being added.
    body = ["# Managed by phonon-stage. Drop-in: attaches macvlan children to " + parent + ".",
            "[Network]"]
    for c in children:
        body.append(f"MACVLAN={c}")
    print(f"A\t{parent}\t{base64.b64encode((chr(10).join(body) + chr(10)).encode()).decode()}")
')
    if [ -z "$plan" ] && [ "$body" != "{}" ]; then
        log "apply-macvlans: parse failed"
        return 3
    fi

    # Collect wanted child + parent attach names from the plan
    # before doing any writes. Then sweep stale phonon-prefixed
    # files that aren't in the wanted set.
    local wanted_children=""
    local wanted_parents=""
    while IFS=$'\t' read -r kind a b _c _d; do
        case "$kind" in
            D) wanted_children="${wanted_children} ${a}" ;;
            A) wanted_parents="${wanted_parents} ${a}" ;;
        esac
    done <<< "$plan"

    # Sweep stale macvlan-child files
    for f in "${managed_dir}/${prefix}"*.netdev "${managed_dir}/${prefix}"*.network; do
        [ -f "$f" ] || continue
        local fname iface_name keep
        fname=$(basename "$f")
        iface_name=$(echo "$fname" | sed -E "s/^${prefix}//; s/\.(netdev|network)$//")
        keep=0
        for w in $wanted_children; do
            if [ "$w" = "$iface_name" ]; then keep=1; break; fi
        done
        if [ "$keep" -eq 0 ]; then
            log "apply-macvlans: removing stale child $f"
            rm -f "$f"
            # Best-effort kernel cleanup. The child may have been
            # auto-removed when systemd-networkd noticed its .netdev
            # disappeared, but `ip link delete` here is idempotent.
            ip link delete "$iface_name" 2>/dev/null || true
        fi
    done
    # Sweep stale LEGACY parent-attach sibling files (pre-drop-in fix)
    for f in "${managed_dir}/${attach_prefix}"*.network; do
        [ -f "$f" ] || continue
        log "apply-macvlans: removing legacy sibling attach $f (now using drop-ins)"
        rm -f "$f"
    done
    # Sweep stale drop-ins: any /etc/systemd/network/*.network.d/30-phonon-macvlan.conf
    # whose parent isn't in the wanted set anymore. We can't enumerate
    # by parent name directly — the dir is named after the .network
    # basename. Walk every *.network.d/ in managed_dir and drop the
    # phonon file when stale.
    local dropin_base=30-phonon-macvlan.conf
    for d in "${managed_dir}"/*.network.d; do
        [ -d "$d" ] || continue
        local conf="${d}/${dropin_base}"
        [ -f "$conf" ] || continue
        # The drop-in mentions the parent iface in its header comment;
        # also the .network it extends is .network.d's parent dir.
        # Derive parent by looking at the .network file's [Match] Name
        # — but for netplan's `10-netplan-<iface>.network`, the iface
        # is in the dir name. Easier: parse `Name=` from the matched
        # .network file if present, or fall back to the dir-name
        # heuristic for netplan.
        local base_network parent_name keep
        base_network="${d%.d}"     # /etc/systemd/network/10-netplan-enp1s0.network
        if [ -f "$base_network" ]; then
            parent_name=$(grep -E '^Name=' "$base_network" 2>/dev/null | head -1 | cut -d= -f2)
        fi
        # /run/ baseline (netplan): the file is in /run/systemd/network/.
        if [ -z "$parent_name" ] && [ -f "/run/systemd/network/$(basename "$base_network")" ]; then
            parent_name=$(grep -E '^Name=' "/run/systemd/network/$(basename "$base_network")" 2>/dev/null | head -1 | cut -d= -f2)
        fi
        keep=0
        for w in $wanted_parents; do
            if [ "$w" = "$parent_name" ]; then keep=1; break; fi
        done
        if [ "$keep" -eq 0 ]; then
            log "apply-macvlans: removing stale drop-in $conf (parent=$parent_name)"
            rm -f "$conf"
            # Empty drop-in dir → remove it.
            rmdir "$d" 2>/dev/null || true
        fi
    done

    # Write the wanted files atomically.
    while IFS=$'\t' read -r kind a b c d; do
        case "$kind" in
            D)
                local name="$a"
                local netdev_path="${managed_dir}/${prefix}${name}.netdev"
                local network_path="${managed_dir}/${prefix}${name}.network"
                local tmp_netdev tmp_network
                tmp_netdev="$(mktemp --tmpdir="$managed_dir" ${prefix}${name}.XXXXXX.netdev)"
                tmp_network="$(mktemp --tmpdir="$managed_dir" ${prefix}${name}.XXXXXX.network)"
                echo "$c" | base64 -d > "$tmp_netdev"
                echo "$d" | base64 -d > "$tmp_network"
                chmod 0644 "$tmp_netdev" "$tmp_network"
                mv -f "$tmp_netdev" "$netdev_path"
                mv -f "$tmp_network" "$network_path"
                log "apply-macvlans: wrote child ${name} on ${b}"
                ;;
            A)
                local parent="$a"
                # Drop-in approach: locate the .network currently
                # managing the parent (typically netplan's
                # /run/systemd/network/10-netplan-<parent>.network)
                # and write our additive [Network] block into
                # /etc/systemd/network/<basename>.d/30-phonon-macvlan.conf.
                # systemd-networkd merges drop-ins into the base file,
                # so MACVLAN= adds to whatever else netplan has set
                # without colliding on which .network wins the match.
                local parent_network_file basename_only
                parent_network_file=$(networkctl status "$parent" --no-pager 2>/dev/null \
                    | grep -oP 'Network File:\s*\K\S+' | head -1)
                if [ -z "$parent_network_file" ]; then
                    # No managed file → can't drop-in. Write a sibling
                    # .network as a last resort (numbered HIGHER than
                    # netplan's 10- so it doesn't win the first-match
                    # contest — only works on systems where there IS
                    # no other .network for the parent).
                    local fallback_path="${managed_dir}/${attach_prefix}${parent}.network"
                    local tmp_fb
                    tmp_fb="$(mktemp --tmpdir="$managed_dir" ${attach_prefix}${parent}.XXXXXX.network)"
                    echo "$b" | base64 -d > "$tmp_fb"
                    # The drop-in body has [Network] only; for a
                    # standalone file we need [Match] too — splice in.
                    sed -i "1a [Match]\nName=${parent}\n" "$tmp_fb"
                    chmod 0644 "$tmp_fb"
                    mv -f "$tmp_fb" "$fallback_path"
                    log "apply-macvlans: parent ${parent} has no .network, wrote standalone ${fallback_path}"
                else
                    basename_only=$(basename "$parent_network_file")
                    local dropin_dir="${managed_dir}/${basename_only}.d"
                    local dropin_path="${dropin_dir}/30-phonon-macvlan.conf"
                    mkdir -p "$dropin_dir"
                    local tmp_dropin
                    tmp_dropin="$(mktemp --tmpdir="$dropin_dir" 30-phonon-macvlan.XXXXXX.conf)"
                    echo "$b" | base64 -d > "$tmp_dropin"
                    chmod 0644 "$tmp_dropin"
                    mv -f "$tmp_dropin" "$dropin_path"
                    log "apply-macvlans: wrote drop-in ${dropin_path} for parent ${parent} (base=${parent_network_file})"
                fi
                ;;
        esac
    done <<< "$plan"

    # Tell networkd to pick up the new files. `reload` reads the new
    # config files BUT doesn't always re-apply them to already-active
    # devices — that's `reconfigure`'s job. We reconfigure each
    # parent that has wanted children so the kernel actually creates
    # the macvlan devices (which needs MACVLAN= from the parent's
    # drop-in to be honoured).
    networkctl reload >>"$LOG" 2>&1 || log "apply-macvlans: networkctl reload reported errors (continuing)"
    for parent in $wanted_parents; do
        networkctl reconfigure "$parent" >>"$LOG" 2>&1 \
            || log "apply-macvlans: networkctl reconfigure $parent failed (continuing)"
    done
    log "apply-macvlans: done"
}

cmd_apply_qos() {
    # Single arg: "1" to enable (load nft table), "0" to clear.
    local mode="${1:-0}"
    local nft_file=/etc/nftables.d/phonon-qos.nft
    mkdir -p "$(dirname "$nft_file")"
    if [ "$mode" = "1" ]; then
        cat > "${nft_file}.tmp" <<'NFT'
# Generated by phonon-stage. Edits here are overwritten.
#
# Marks PTP control + AES67 audio RTP packets with DSCP EF (46) at
# egress. Switches with DSCP-aware QoS (UniFi USW, Cisco, etc.)
# then bump these into the high-priority queue, protecting them
# from generic bulk traffic on the same wire.
#
# Counters at the end of each rule let `nft list table inet
# phonon_qos` show hit counts — the UI uses these to show
# "X PTP marked, Y AES67 marked" so you can see it's actually
# matching live traffic.
table inet phonon_qos {
    chain output {
        type filter hook output priority mangle; policy accept;
        # PTP — UDP 319 (event) + 320 (general). Both ptp4l and
        # nqptp speak this. Match either source OR destination
        # port because outbound replies use the source port.
        meta l4proto udp udp sport { 319, 320 } ip dscp set ef counter
        meta l4proto udp udp dport { 319, 320 } ip dscp set ef counter
        # AES67 RTP audio — multicast in the 239.x.x.x range with
        # the typical audio port window. Narrow to the dynamic-port
        # range so we don't catch unrelated mDNS / SSDP / etc.
        ip daddr 239.0.0.0/8 meta l4proto udp udp dport >= 5004 udp dport <= 6004 ip dscp set ef counter
    }
}
NFT
        chmod 0644 "${nft_file}.tmp"
        mv -f "${nft_file}.tmp" "$nft_file"
        # Reload the table. Strategy: delete the existing one (if
        # any) so the new rules don't stack; ignore the error when
        # it doesn't exist yet.
        nft delete table inet phonon_qos 2>/dev/null || true
        if nft -f "$nft_file" 2>>"$LOG"; then
            log "apply-qos: enabled (PTP + AES67 → EF)"
        else
            log "apply-qos: nft load failed — see log"
            return 3
        fi
        # Persist across reboot via a systemd oneshot unit. We own
        # the unit file so re-applying is just `daemon-reload` if it
        # already exists.
        local unit=/etc/systemd/system/phonon-qos.service
        if [ ! -f "$unit" ]; then
            cat > "$unit" <<EOF
[Unit]
Description=Phonon DSCP marking (PTP + AES67 → EF)
After=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/sbin/nft -f /etc/nftables.d/phonon-qos.nft
ExecStop=/usr/sbin/nft delete table inet phonon_qos
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
            systemctl daemon-reload
            log "apply-qos: installed phonon-qos.service unit"
        fi
        systemctl enable phonon-qos.service 2>>"$LOG" || true
    else
        nft delete table inet phonon_qos 2>/dev/null || true
        rm -f "$nft_file"
        systemctl disable phonon-qos.service 2>/dev/null || true
        log "apply-qos: disabled (table cleared, persistence removed)"
    fi
}

cmd_qos_status() {
    # Dump the table's current state — counters included so the
    # caller can show per-rule hit counts.
    nft list table inet phonon_qos 2>/dev/null || echo "(no table)"
}

cmd_diag_sockets() {
    # Print all UDP listeners on PTP ports (319 + 320). Run as root so
    # the process column is populated (ss needs CAP_NET_ADMIN to see
    # other users' procs). Output goes straight to stdout for the
    # caller (API) to parse.
    ss -tulnp 2>/dev/null | grep -E ':(319|320)\s' || true
}

case "${1:-}" in
    apply-iface)    shift; cmd_apply_iface "${1:-}" ;;
    confirm)        cmd_confirm ;;
    cancel)         cmd_cancel ;;
    rollback)       shift; cmd_rollback "${1:-}" ;;
    status)         cmd_status ;;
    apply-macvlans) cmd_apply_macvlans ;;
    diag-sockets)   cmd_diag_sockets ;;
    apply-qos)      shift; cmd_apply_qos "${1:-0}" ;;
    qos-status)     cmd_qos_status ;;
    *)
        echo "usage: $0 {apply-iface <timeout-s> | confirm | cancel | rollback <backup-dir> | status | apply-macvlans | diag-sockets | apply-qos <0|1> | qos-status}" >&2
        exit 1
        ;;
esac
