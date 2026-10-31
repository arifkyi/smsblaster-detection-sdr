#!/bin/bash
# siren_hook.sh - siren trigger for the SMS blaster detector.
#
# The detector calls this via --alert-cmd on every WARN/CRIT. We gate to CRIT
# by default, send an explicit ON (never toggle), and LATCH so a burst of CRITs
# does not hammer the plug. The siren stays on until you acknowledge with
# --reset. LAN control only (local port 9999), no cloud dependency.
#
# Arm the detector with it (point --alert-cmd at this file):
#   sudo python3 smsblaster_detection_signaling.py --iface lo0 \
#       --tshark /Applications/Wireshark.app/Contents/MacOS/tshark \
#       --alert-cmd ./siren_hook.sh
#
# Silence / acknowledge after an event (use sudo if the detector ran under sudo,
# because the latch file is then owned by root):
#   sudo ./siren_hook.sh --reset
#
# Config via env (or edit the defaults below):
#   SIREN_IP    plug IP on the LAN           (default 192.168.1.3)
#   TPLINK      path to your TP-Link script  (default: SAME directory as this hook)
#   SIREN_ON    CRIT or WARN                 (default CRIT)
#   SIREN_LATCH latch file path              (default: SAME directory as this hook)
#
# NOTE on sudo: the detector runs under sudo, so this hook runs as root. Root's
# $HOME is /var/root, not your home, so paths must NOT rely on $HOME or they
# resolve to a place that does not exist and the plug call fails silently. That
# is why both the TP-Link script and the latch are resolved relative to THIS
# file's own directory (SCRIPT_DIR), which works the same under sudo or not.
#
# Safety: this script never blocks the detector. The detector Popen's it
# fire-and-forget, and we background the plug call as well, so a dead or
# unplugged siren can never stall detection or crash the parent.

# Directory this hook lives in. Resolved the same way whether run directly or
# under sudo, so every path below is relative to it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SIREN_IP="${SIREN_IP:-192.168.1.3}"

# --- TP-Link control script location ----------------------------------------
# Default assumes Tplinkcomprehensivescript.py sits in the SAME directory as
# this hook (the common case: all three scripts in one folder).
TPLINK="${TPLINK:-$SCRIPT_DIR/Tplinkcomprehensivescript.py}"
# If you keep the TP-Link script somewhere else, comment the line above and use
# an absolute path instead (kept here as a format reference):
#TPLINK="${TPLINK:-/Users/ahmadrifky/Desktop/smsblaster/Tplinkcomprehensivescript.py}"

SIREN_ON="${SIREN_ON:-CRIT}"
LATCH="${SIREN_LATCH:-$SCRIPT_DIR/smsblaster_siren.latch}"

plug() {  # $1 = 1 (on) or 0 (off); errors swallowed so a dead plug is silent
    python3 "$TPLINK" "$SIREN_IP" "$1" >/dev/null 2>&1
}

# ---- manual reset / silence -------------------------------------------------
case "$1" in
    --reset|reset|off)
        if rm -f "$LATCH" 2>/dev/null; then
            echo "[siren] reset: OFF, latch cleared ($LATCH)"
        else
            echo "[siren] WARN: could not remove $LATCH (owned by root? try: sudo $0 --reset)"
        fi
        plug 0
        exit 0
        ;;
esac

# ---- severity gate ----------------------------------------------------------
fire=0
[ "$SMSBLASTER_SEVERITY" = "CRIT" ] && fire=1
[ "$SIREN_ON" = "WARN" ] && [ "$SMSBLASTER_SEVERITY" = "WARN" ] && fire=1

# ---- latch: sound once per episode, then hold until --reset -----------------
if [ "$fire" = "1" ] && [ ! -e "$LATCH" ]; then
    : > "$LATCH"
    plug 1 &          # background so we return instantly no matter what
fi
exit 0
