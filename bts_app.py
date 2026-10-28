#!/usr/bin/env python3
"""
bts_app.py - Flask + SSE web server for the SMS-blaster / fake-BTS detector.

Runs on the box with the radios. Binds 0.0.0.0 so you open the dashboard from
another device on the LAN:  http://<this box IP>:8080

Endpoints:
  GET  /                 dashboard
  GET  /stream           Server-Sent Events: a state snapshot ~2x/sec
  GET  /bands            band plan for the GUI
  POST /learn            {bands:[...]}        start learn mode
  POST /monitor          {bands:[...]}        start monitor mode (uses saved baseline)
  POST /stop             stop current job
  POST /siren            {armed:bool}         arm/disarm the siren hook
  POST /ack              {id:<n>|"all"}       acknowledge/clear an anomaly (+siren reset)
  POST /manual           {earfcn,phyid,band,prb?,ports?,pss_dbm?}  baseline-fill
  POST /inject           {band,earfcn,phyid,prb?,ports?,dbm?}  sim-only fake BTS
  POST /clear_inject     sim-only: remove injected fake BTS
"""

import json
import os
import time

from flask import Flask, Response, request, jsonify, send_from_directory

import bts_common as C
from bts_scanner import Engine

try:
    import bts_config as _cfgmod
    CONFIG = dict(_cfgmod.CONFIG)
    RADIOS = list(_cfgmod.RADIOS)
except Exception:
    CONFIG, RADIOS = {}, [{"name": "sim", "serial": None, "enabled": True}]

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
engine = Engine(CONFIG, RADIOS)


@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "bts.html")


@app.route("/bands")
def bands():
    out = []
    for bid in C.BAND_DISPLAY_ORDER:
        b = C.BANDS[bid]
        out.append({
            "band": bid, "note": b["note"], "duplex": b["duplex"],
            "earfcn_low": b["earfcn_low"], "earfcn_high": b["earfcn_high"],
            "f_low": C.earfcn_to_freq_mhz(bid, b["earfcn_low"]),
            "f_high": C.earfcn_to_freq_mhz(bid, b["earfcn_high"]),
            "ticked": bid in C.DEFAULT_TICKED_BANDS,
        })
    return jsonify({"bands": out, "radios": [
        {"name": r.get("name"), "serial": r.get("serial"),
         "enabled": r.get("enabled", True)} for r in RADIOS]})


def _req_bands():
    data = request.get_json(force=True, silent=True) or {}
    bands = data.get("bands") or []
    return [int(b) for b in bands if int(b) in C.BANDS]


@app.route("/learn", methods=["POST"])
def learn():
    bands = _req_bands()
    if not bands:
        return jsonify({"ok": False, "error": "no bands selected"}), 400
    engine.start_learn(bands)
    return jsonify({"ok": True, "bands": bands})


@app.route("/monitor", methods=["POST"])
def monitor():
    bands = _req_bands()
    engine.start_monitor(bands)
    return jsonify({"ok": True, "bands": bands})


@app.route("/stop", methods=["POST"])
def stop():
    engine.stop()
    return jsonify({"ok": True})


@app.route("/siren", methods=["POST"])
def siren():
    data = request.get_json(force=True, silent=True) or {}
    engine.arm_siren(bool(data.get("armed")))
    return jsonify({"ok": True, "armed": engine.siren_armed})


@app.route("/ack", methods=["POST"])
def ack():
    data = request.get_json(force=True, silent=True) or {}
    engine.ack(data.get("id", "all"))
    return jsonify({"ok": True})


@app.route("/manual", methods=["POST"])
def manual():
    d = request.get_json(force=True, silent=True) or {}
    try:
        earfcn = int(d["earfcn"]); phyid = int(d["phyid"]); band = int(d["band"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "earfcn, phyid, band required"}), 400
    if band not in C.BANDS or not (C.PHYID_MIN <= phyid <= C.PHYID_MAX):
        return jsonify({"ok": False, "error": "band or phyid out of range"}), 400
    prb = d.get("prb"); ports = d.get("ports"); pss = d.get("pss_dbm")
    engine.baseline.add_manual(
        earfcn, phyid, band,
        int(prb) if prb not in (None, "") else None,
        int(ports) if ports not in (None, "") else None,
        float(pss) if pss not in (None, "") else None,
    )
    return jsonify({"ok": True})


@app.route("/inject", methods=["POST"])
def inject():
    if engine.cfg.get("engine") != "sim":
        return jsonify({"ok": False, "error": "inject is sim-only"}), 400
    d = request.get_json(force=True, silent=True) or {}
    try:
        engine.inject_fake_bts(
            int(d["band"]), int(d["earfcn"]), int(d["phyid"]),
            int(d.get("prb", 6)), int(d.get("ports", 1)), float(d.get("dbm", -20.0)))
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "band, earfcn, phyid required"}), 400
    return jsonify({"ok": True})


@app.route("/clear_inject", methods=["POST"])
def clear_inject():
    engine.clear_fake_bts()
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    def gen():
        while True:
            payload = json.dumps(engine.snapshot())
            yield "data: %s\n\n" % payload
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    host = CONFIG.get("host", "0.0.0.0")
    port = int(CONFIG.get("port", 8080))
    print("SMS-blaster detector on http://%s:%d  (engine=%s)" % (
        host, port, engine.cfg["engine"]))
    app.run(host=host, port=port, threaded=True, debug=False)
