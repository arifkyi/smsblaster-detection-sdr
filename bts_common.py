"""
bts_common.py - shared band plan, LTE EARFCN math, cell_search parsing, defaults.

One source of truth for:
  - the LTE band table (DL EARFCN ranges + frequency mapping, FDD and TDD)
  - cell_search stdout parsing (progress lines + Found CELL lines)
  - decode-noise filters (valid PRB / ports / PCI)
  - detection defaults (all overridable from a profile)

Receive-only. This module touches no hardware and spawns nothing.
"""

import re
from dataclasses import dataclass, field, asdict

# --------------------------------------------------------------------------
# LTE band plan (3GPP TS 36.101). Downlink only, which is what cell_search scans.
#   dl_low_mhz   : F_DL_low, the downlink frequency at the first EARFCN of the band
#   earfcn_off   : N_Offs-DL, the EARFCN at dl_low_mhz
#   earfcn_low / earfcn_high : inclusive DL EARFCN range cell_search sweeps for -b
#   duplex       : FDD or TDD (display only; cell_search auto-detects frame type)
#   note         : short label for the GUI
# Frequency:  F_DL = dl_low_mhz + 0.1 * (EARFCN - earfcn_off)
# --------------------------------------------------------------------------
BANDS = {
    1:  {"dl_low_mhz": 2110.0, "earfcn_off": 0,     "earfcn_low": 0,     "earfcn_high": 599,   "duplex": "FDD", "note": "2100"},
    3:  {"dl_low_mhz": 1805.0, "earfcn_off": 1200,  "earfcn_low": 1200,  "earfcn_high": 1949,  "duplex": "FDD", "note": "1800"},
    5:  {"dl_low_mhz": 869.0,  "earfcn_off": 2400,  "earfcn_low": 2400,  "earfcn_high": 2649,  "duplex": "FDD", "note": "850"},
    7:  {"dl_low_mhz": 2620.0, "earfcn_off": 2750,  "earfcn_low": 2750,  "earfcn_high": 3449,  "duplex": "FDD", "note": "2600"},
    8:  {"dl_low_mhz": 925.0,  "earfcn_off": 3450,  "earfcn_low": 3450,  "earfcn_high": 3799,  "duplex": "FDD", "note": "900"},
    28: {"dl_low_mhz": 758.0,  "earfcn_off": 9210,  "earfcn_low": 9210,  "earfcn_high": 9659,  "duplex": "FDD", "note": "700"},
    38: {"dl_low_mhz": 2570.0, "earfcn_off": 37750, "earfcn_low": 37750, "earfcn_high": 38249, "duplex": "TDD", "note": "2600 TDD"},
    39: {"dl_low_mhz": 1880.0, "earfcn_off": 38250, "earfcn_low": 38250, "earfcn_high": 38649, "duplex": "TDD", "note": "1900 TDD"},
    40: {"dl_low_mhz": 2300.0, "earfcn_off": 38650, "earfcn_low": 38650, "earfcn_high": 39649, "duplex": "TDD", "note": "2300 TDD"},
    41: {"dl_low_mhz": 2496.0, "earfcn_off": 39650, "earfcn_low": 39650, "earfcn_high": 41589, "duplex": "TDD", "note": "2500 TDD"},
}

# Order the GUI shows bands in. Core 5 first, then the optional rows.
BAND_DISPLAY_ORDER = [1, 3, 5, 8, 40, 7, 28, 38, 39, 41]

# Bands ticked on first load.
DEFAULT_TICKED_BANDS = [3]


def earfcn_to_freq_mhz(band, earfcn):
    """DL centre frequency (MHz) for an EARFCN in a given band. None if unknown band."""
    b = BANDS.get(band)
    if not b:
        return None
    return round(b["dl_low_mhz"] + 0.1 * (earfcn - b["earfcn_off"]), 1)


def band_for_earfcn(earfcn):
    """Which band an absolute DL EARFCN falls in, or None."""
    for bid, b in BANDS.items():
        if b["earfcn_low"] <= earfcn <= b["earfcn_high"]:
            return bid
    return None


# --------------------------------------------------------------------------
# Decode-noise guards (learned from real Band 40 data: 150 PRB, flickering PCI).
# --------------------------------------------------------------------------
VALID_PRB = {6, 15, 25, 50, 75, 100}   # the only bandwidths LTE MIB can encode
VALID_PORTS = {1, 2, 4}                 # antenna ports
PHYID_MIN, PHYID_MAX = 0, 503           # PCI range


def is_plausible_cell(earfcn, phyid, prb, ports):
    """
    True only if every decoded field is standard-legal. A row that fails this
    is a PBCH decode artifact (e.g. 150 PRB) and must be discarded before it
    ever reaches the baseline or the anomaly logic.
    """
    if prb not in VALID_PRB:
        return False
    if ports not in VALID_PORTS:
        return False
    if not (PHYID_MIN <= phyid <= PHYID_MAX):
        return False
    return True


# --------------------------------------------------------------------------
# cell_search stdout parsing.
#
# Progress line:
#   [ 95/100]: EARFCN 38845 Freq. 2319.50 MHz looking for PSS.
# Result line (authoritative):
#   Found CELL 2310.0 MHz, EARFCN=38750, PHYID=185, 15 PRB, 1 ports, PSS power=-13.1 dBm
# Summary header:   Found 4 cells
# End of run:       Bye
# The inline "Found CELL ID 108. 100 PRB, 2 ports" is ignored (the summary line
# is a superset and carries EARFCN + power).
# --------------------------------------------------------------------------
_RE_PROGRESS = re.compile(
    r"\[\s*(\d+)\s*/\s*(\d+)\s*\]:\s*EARFCN\s+(\d+)\s+Freq\.\s+([\d.]+)\s*MHz"
)
_RE_FOUND_CELL = re.compile(
    r"Found CELL\s+([\d.]+)\s*MHz,\s*"
    r"EARFCN=(\d+),\s*"
    r"PHYID=(\d+),\s*"
    r"(\d+)\s*PRB,\s*"
    r"(\d+)\s*ports,\s*"
    r"PSS power=\s*(-?[\d.]+)\s*dBm",
    re.IGNORECASE,
)
_RE_FOUND_SUMMARY = re.compile(r"Found\s+(\d+)\s+cells", re.IGNORECASE)


def parse_progress(line):
    """Return (idx, total, earfcn, freq_mhz) from a progress line, or None."""
    m = _RE_PROGRESS.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))


def parse_found_cell(line):
    """Return a dict for a 'Found CELL' result line, or None."""
    m = _RE_FOUND_CELL.search(line)
    if not m:
        return None
    return {
        "freq_mhz": float(m.group(1)),
        "earfcn": int(m.group(2)),
        "phyid": int(m.group(3)),
        "prb": int(m.group(4)),
        "ports": int(m.group(5)),
        "pss_dbm": float(m.group(6)),
    }


def parse_found_summary(line):
    """Return N from 'Found N cells', or None."""
    m = _RE_FOUND_SUMMARY.search(line)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Detection defaults. A profile (bts_config.py) may override any of these.
# --------------------------------------------------------------------------
DEFAULTS = {
    # engine
    "engine": "sim",                 # "cell_search" on the real box, "sim" for testing
    "cell_search_bin": "/usr/src/srsRAN_4G/build/lib/examples/cell_search",
    "gain_db": 70.0,                 # -g, MUST match between learn and monitor
    "nof_frames": 100,               # -n

    # learn
    "learn_passes": 2,               # M full sweeps per band during learn
    "learn_min_seen": 2,             # N: must appear in >= N passes to be promoted

    # monitor
    "power_delta_db": 6.0,           # PSS rise over baseline (known cell) = spike
    # Overshadow gate: an UNEXPECTED cell (new EARFCN, or new PCI on a known
    # EARFCN) is escalated NEW/PCI -> CRIT when it is strong, else WARN.
    "overshadow_dbm": -30.0,         # absolute: this loud anywhere = strong
    "overshadow_margin_db": 3.0,     # relative: within this of the real cell = strong
    "crit_floor_dbm": -47.0,         # relative check only counts at/above this level
    "debounce_hits": 2,              # consecutive confirmations before an alert fires
    "debounce_window": 2,            # out of this many recent observations (N-of-M)
    # MISSING is noisier than NEW/POWER: weak cells flicker out of a single
    # narrow-window scan, so flagging one gone needs more consecutive misses.
    # This keeps the fake-BTS CRIT snappy (debounce_hits) while MISSING stays calm.
    "missing_hits": 3,               # a baseline cell must be absent this many cycles
    "discovery_enabled": True,       # full-band discovery sweep on/off (GUI toggle)
    "discovery_every_s": 300,        # full-band discovery sweep cadence (seconds)
    "monitor_track_guard": 2,        # +/- EARFCN padding around a tracked cell's window

    # web
    "host": "0.0.0.0",               # LAN-reachable; view from another device via box IP
    "port": 8080,

    # siren
    "alert_cmd": "",                 # path to siren_hook.sh; empty = disarmed
    "siren_armed": False,            # GUI toggle default: off
}

# Severity levels, and which anomaly types map to which.
SEV_CRIT = "CRIT"
SEV_WARN = "WARN"
ANOMALY_SEVERITY = {
    "NEW":        SEV_CRIT,   # unknown (EARFCN, PHYID) appears
    "POWER":      SEV_CRIT,   # known cell jumps > power_delta_db
    "PCI":        SEV_WARN,   # PHYID changed / extra PHYID on a known EARFCN
    "PRB_PORTS":  SEV_WARN,   # bandwidth or antenna ports changed on a known cell
    "MISSING":    SEV_WARN,   # baseline cell no longer seen
}


@dataclass
class CellRecord:
    """One decoded cell from a single scan (post-filter)."""
    earfcn: int
    phyid: int
    band: int
    freq_mhz: float
    prb: int
    ports: int
    pss_dbm: float

    @property
    def key(self):
        return (self.earfcn, self.phyid)

    def to_dict(self):
        return asdict(self)


@dataclass
class BaselineCell:
    """A confirmed reference cell, keyed (earfcn, phyid). Persisted to JSON."""
    earfcn: int
    phyid: int
    band: int
    freq_mhz: float
    prb: int
    ports: int
    pss_mean: float
    pss_min: float
    pss_max: float
    pss_last: float
    seen_count: int          # how many learn passes it appeared in
    total_passes: int        # out of how many
    first_seen: str          # ISO timestamp
    source: str = "scan"     # "scan" or "manual"

    @property
    def key(self):
        return (self.earfcn, self.phyid)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        return BaselineCell(**d)
