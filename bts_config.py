"""
bts_config.py - machine profile. Edit this per box, then run bts_app.py.

RADIOS is a list; the app spawns one worker per enabled radio. It ships with a
single USRP B210 (the video detector). The LibreSDR entry is present but
disabled - enable it only if you ever run two radios on one box (topology A),
in which case both serials below keep them apart.

CONFIG overrides anything in bts_common.DEFAULTS.
"""

CONFIG = {
    # ---- engine -----------------------------------------------------------
    # "sim"         : no hardware, synthesized cells (for UI / logic testing)
    # "cell_search" : real srsRAN on this box
    "engine": "cell_search",
    "cell_search_bin": "/usr/src/srsRAN_4G/build/lib/examples/cell_search",
    "gain_db": 45.0,        # -g. MUST be identical for learn and monitor.
    "nof_frames": 20,      # -n

    # ---- learn ------------------------------------------------------------
    "learn_passes": 2,      # M sweeps per band
    "learn_min_seen": 2,    # must appear in >= N passes to enter baseline

    # ---- monitor ----------------------------------------------------------
    "power_delta_db": 6.0,
    # Overshadow gate: an unexpected cell (new EARFCN, or new PCI on a known
    # EARFCN) fires CRIT when strong, WARN when weak. A hot blaster trips CRIT
    # whatever PCI it uses. Raise overshadow_dbm toward 0 to make CRIT stricter.
    "overshadow_dbm": -30.0,
    "overshadow_margin_db": 3.0,
    "debounce_hits": 2,
    "debounce_window": 2,
    # Full-band discovery sweep cadence. Track windows (known cells) run every
    # cycle regardless; this only sets how often brand-new EARFCNs are hunted.
    #   ~20-30  snappy for a live demo (empty-band NEW caught within ~1 cycle)
    #   60      responsive default
    #   300+    calm for long unattended runs
    "discovery_every_s": 60,
    "monitor_track_guard": 2,

    # ---- web --------------------------------------------------------------
    "host": "0.0.0.0",      # LAN-reachable: open http://<this box IP>:8080 from the Mac
    "port": 8080,

    # ---- baseline file ----------------------------------------------------
    "baseline_path": "baseline_cells.json",

    # ---- siren ------------------------------------------------------------
    # Point alert_cmd at your siren_hook.sh to enable the plug. Empty = no hook.
    # The GUI toggle still has to be ON for it to actually fire.
    "alert_cmd": "/home/rifky/Desktop/smsblaster-detection-sdr/siren_hook.sh",            # e.g. "./siren_hook.sh"
    "siren_armed": False,       # GUI default: disarmed
}

RADIOS = [
    {
        "name": "LibreSDR",
        "serial": "7430S68",        # LibreSDR_B210mini. THE DETECTOR.
        # absolute path to the LibreSDR FPGA .bin on THIS box. Confirm it with:
        #   ls -la /home/rifky/fpgalibre/usrp_b210_fpga.bin
        "fpga": "/home/rifky/usrp_b210_fpga.bin",
        "rf_args_extra": "",
        "cwd": "",                  # fallback: set to the fpga's dir and leave fpga ""
        "enabled": True,
    },
    {
        "name": "USRP-B210",
        "serial": "8002791",        # B210 (Zhixun-wireless). Now the FAKE (srsENB), not run here.
        "fpga": "",                 # stock UHD image
        "rf_args_extra": "",
        "cwd": "",
        "enabled": False,           # the detector is the LibreSDR above
    },
]
