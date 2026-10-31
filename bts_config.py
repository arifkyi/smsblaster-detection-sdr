"""
bts_config.py - machine profile. Edit this per box, then run bts_app.py.

RADIOS is a list of the SDRs on this box. Exactly one should be enabled: that
radio is the detector. Here the LibreSDR is the detector and the USRP B210 entry
is disabled (it runs the test cell on another machine). Swap the two `enabled`
flags to use the B210 as the detector instead. The serials keep them apart.

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
    # CRIT floor: the 'overshadows real cell' check only escalates to CRIT at or
    # above this level. Faint neighbours and side-lock ghosts on busy EARFCNs
    # stay WARN. Set it between your loudest false CRIT and your fake's level.
    "crit_floor_dbm": -47.0,
    "debounce_hits": 1,
    "debounce_window": 2,
    # MISSING is noisier than the fake-BTS alerts: weak real cells flicker
    # out of one narrow scan, so a baseline cell must be absent this many cycles
    # before MISSING fires. Keeps the fake-BTS CRIT snappy (debounce_hits=1) while
    # MISSING stays calm. Raise it if you still see MISSING flicker.
    "missing_hits": 3,
    # Full-band discovery sweep cadence. Track windows (known cells) run every
    # cycle regardless; this only sets how often brand-new EARFCNs are hunted.
    # Set it LONGER than one full sweep of your ticked bands (B3 at nof_frames 20
    # takes ~12-13 min), or every cycle becomes a full sweep and known-cell
    # tracking slows to one check per sweep.
    #   1800    track cycles under a minute, full sweep every 30 min
    "discovery_every_s": 1800,
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
        #   ls -la /home/rifky/usrp_b210_fpga.bin
        "fpga": "/home/rifky/usrp_b210_fpga.bin",
        "rf_args_extra": "",
        "cwd": "",                  # fallback: set to the fpga's dir and leave fpga ""
        "enabled": True,
    },
    {
        "name": "USRP-B210",
        "serial": "8002791",        # B210 (Zhixun-wireless). Runs the test cell (srsENB) elsewhere.
        "fpga": "",                 # stock UHD image
        "rf_args_extra": "",
        "cwd": "",
        "enabled": False,           # the detector is the LibreSDR above
    },
]
