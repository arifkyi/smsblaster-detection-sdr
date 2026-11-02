"""
bts_scanner.py - the detection engine.

Responsibilities:
  - Scan source: spawn srsRAN cell_search per band (real) OR synthesize output (sim).
  - Learn: run each ticked band M times, confirm cells seen in >= N passes,
    average their PSS power, write baseline_cells.json.
  - Monitor: narrow track-windows around each baseline cell every cycle, plus a
    slower full-band discovery sweep; diff against baseline; debounce; latch
    anomalies; fire the alert hook on confirmed CRIT.
  - Radio-list architecture: one worker per radio. Ships with one USRP profile,
    but the structure supports two (serial-pinned) with no rework.

Receive-only. cell_search measures and decodes the downlink; nothing transmits.
"""

import json
import os
import re
import shlex
import subprocess
import threading
import time
import random
import statistics
from collections import deque, defaultdict
from datetime import datetime, timezone

import bts_common as C
from bts_common import CellRecord, BaselineCell


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ==========================================================================
# Scan sources
# ==========================================================================
class ScanSource:
    """Interface: run one cell_search pass over a band (optionally narrowed)."""

    def scan(self, band, earfcn_start=None, earfcn_end=None, on_progress=None):
        """
        Return list[CellRecord] for one pass. on_progress(idx,total,earfcn,freq)
        is called as the sweep advances. Rows failing is_plausible_cell are
        dropped here so nothing downstream sees decode artifacts.
        """
        raise NotImplementedError

    def stop(self):
        pass


class CellSearchSource(ScanSource):
    """Real srsRAN cell_search. One subprocess per band per pass."""

    def __init__(self, radio, cfg):
        self.radio = radio          # dict: name, serial, fpga, rf_args_extra, cwd
        self.cfg = cfg
        self._proc = None
        self._stop = False

    def _build_args(self, band, earfcn_start, earfcn_end):
        a = [self.cfg["cell_search_bin"], "-b", str(band)]
        if earfcn_start is not None and earfcn_end is not None:
            a += ["-s", str(earfcn_start), "-e", str(earfcn_end)]
        a += ["-g", str(self.cfg["gain_db"]), "-n", str(self.cfg["nof_frames"])]

        # -a device args: serial pin (+ fpga for LibreSDR) + any extra keys.
        kv = []
        if self.radio.get("serial"):
            kv.append("serial=%s" % self.radio["serial"])
        if self.radio.get("fpga"):
            kv.append("fpga=%s" % self.radio["fpga"])
        if self.radio.get("rf_args_extra"):
            kv.append(self.radio["rf_args_extra"])
        if kv:
            a += ["-a", ",".join(kv)]
        return a

    def scan(self, band, earfcn_start=None, earfcn_end=None, on_progress=None):
        self._stop = False
        args = self._build_args(band, earfcn_start, earfcn_end)
        log = getattr(self, "log", None)
        t0 = time.time()
        if log:
            log.scan("RUN  " + " ".join(shlex.quote(a) for a in args))
        cwd = self.radio.get("cwd") or None
        cells = []
        in_summary = False
        progressed = False
        tail = deque(maxlen=6)          # last output lines, for the error text
        try:
            self._proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=cwd,
            )
        except FileNotFoundError:
            raise RuntimeError("cell_search not found at %s" % self.cfg["cell_search_bin"])

        for line in self._proc.stdout:
            if self._stop:
                break
            if line.strip():
                tail.append(line.strip())
            prog = C.parse_progress(line)
            if prog:
                progressed = True
                if on_progress:
                    on_progress(*prog)
                continue
            if C.parse_found_summary(line) is not None:
                in_summary = True
                continue
            cell = C.parse_found_cell(line)
            if cell:
                b = C.band_for_earfcn(cell["earfcn"]) or band
                ok = C.is_plausible_cell(cell["earfcn"], cell["phyid"],
                                         cell["prb"], cell["ports"])
                if log:
                    log.scan(("CELL " if ok else "DROP ") + _fmt_cell(cell))
                if ok:
                    cells.append(CellRecord(
                        earfcn=cell["earfcn"], phyid=cell["phyid"], band=b,
                        freq_mhz=cell["freq_mhz"], prb=cell["prb"],
                        ports=cell["ports"], pss_dbm=cell["pss_dbm"],
                    ))
                # implausible rows (e.g. 150 PRB) silently dropped
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
        # cell_search printed no "[ n/N]: EARFCN ..." line at all: it never
        # scanned (radio busy, unplugged, FPGA load failure). Raise instead of
        # returning [], so a dead radio can never look like "all cells missing".
        if not progressed and not self._stop:
            msg = "radio error, cell_search did not scan: %s" % (
                " | ".join(tail) or "no output")
            if log:
                log.scan("END  ERROR " + msg)
            raise RuntimeError(msg)
        if log:
            log.scan("END  %d cells, %.1f s%s" % (
                len(cells), time.time() - t0, " (stopped)" if self._stop else ""))
        return cells

    def stop(self):
        self._stop = True
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass


class RunLog:
    """
    Optional plain-text logs (enabled with `python bts_app.py --log` or
    log_enabled=True). Two daily files in ./logs next to this script:
      cellsearch_YYYY-MM-DD.log  every cell_search run: RUN / CELL / DROP / END
      anomalies_YYYY-MM-DD.log   START / STOP / CRIT / WARN / ACK / SIREN / ERROR
    Disabled = every call is a no-op.
    """

    def __init__(self, enabled, folder=None):
        self.enabled = bool(enabled)
        self.folder = folder or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs")
        self._lock = threading.Lock()
        if self.enabled:
            os.makedirs(self.folder, exist_ok=True)

    def _write(self, kind, text):
        if not self.enabled:
            return
        now = time.localtime()
        path = os.path.join(self.folder, "%s_%s.log" % (
            kind, time.strftime("%Y-%m-%d", now)))
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", now), text)
        try:
            with self._lock, open(path, "a") as f:
                f.write(line)
        except OSError:
            pass   # logging must never stop detection

    def scan(self, text):
        self._write("cellsearch", text)

    def event(self, text):
        self._write("anomalies", text)


def _fmt_cell(c):
    return "EARFCN=%d PHYID=%-3d %3d PRB %d ports PSS=%.1f dBm" % (
        c["earfcn"], c["phyid"], c["prb"], c["ports"], c["pss_dbm"])


class SimSource(ScanSource):
    """
    Synthesizes cell_search-like output with no hardware.

    Models two things the real world showed us:
      - stable "real" cells that appear every pass with steady power
      - weak-signal decode noise: ghost cells that flicker and misdecode
        (these are generated as implausible rows and get filtered, exactly
        as they would from the real binary)
    A fake-BTS cell can be injected at runtime to exercise the alert path.
    """

    # a few stable truth cells per band (earfcn, phyid, prb, ports, base_dbm)
    TRUTH = {
        3:  [(1275, 108, 100, 2, -36.0), (1700, 275, 100, 4, -41.0)],
        1:  [(74, 2, 15, 1, -27.0), (300, 150, 50, 2, -22.0)],
        8:  [],   # empty band (no cells)
        40: [(38752, 2, 25, 1, -17.0), (38843, 0, 100, 4, -26.0)],
        5:  [(2500, 60, 50, 2, -30.0)],
    }

    def __init__(self, radio, cfg):
        self.radio = radio
        self.cfg = cfg
        self._stop = False
        self.injected = {}   # band -> list of (earfcn, phyid, prb, ports, dbm)

    def inject_fake_bts(self, band, earfcn, phyid, prb=6, ports=1, dbm=-20.0):
        self.injected.setdefault(band, []).append((earfcn, phyid, prb, ports, dbm))

    def clear_injections(self):
        self.injected = {}

    def scan(self, band, earfcn_start=None, earfcn_end=None, on_progress=None):
        self._stop = False
        b = C.BANDS.get(band)
        if not b:
            return []
        lo = earfcn_start if earfcn_start is not None else b["earfcn_low"]
        hi = earfcn_end if earfcn_end is not None else b["earfcn_high"]
        span = max(1, hi - lo)
        total = min(span + 1, 60)  # cap simulated progress steps for speed

        # emit progress quickly
        for i in range(total):
            if self._stop:
                break
            ea = lo + int(i * span / total)
            if on_progress:
                on_progress(i, total, ea, C.earfcn_to_freq_mhz(band, ea) or 0.0)
            time.sleep(0.004)
        # A narrow track window on real hardware costs a few seconds (radio
        # open + dwell). Mimic that so sim cycles run at a realistic pace.
        if span <= 10:
            for _ in range(15):
                if self._stop:
                    break
                time.sleep(0.1)

        cells = []
        in_range = lambda e: lo <= e <= hi
        # stable truth cells
        for (ea, pci, prb, ports, base) in self.TRUTH.get(band, []):
            if not in_range(ea):
                continue
            dbm = round(base + random.uniform(-1.2, 1.2), 1)   # fading wobble
            cells.append(CellRecord(ea, pci, band, C.earfcn_to_freq_mhz(band, ea),
                                    prb, ports, dbm))
        # injected fake BTS (strong, stable)
        for (ea, pci, prb, ports, base) in self.injected.get(band, []):
            if not in_range(ea):
                continue
            dbm = round(base + random.uniform(-0.4, 0.4), 1)
            cells.append(CellRecord(ea, pci, band, C.earfcn_to_freq_mhz(band, ea),
                                    prb, ports, dbm))
        # decode ghosts: ~30% of passes spawn a flickering implausible row that
        # is filtered out (proves the filter + N-of-M in the pipeline).
        if random.random() < 0.3:
            ghost_ea = random.randint(lo, hi)
            # implausible PRB -> dropped by filter, same as real 150-PRB garbage
            pass  # intentionally not added: it would be filtered; we model the
                  # *flicker* via unstable PCI below instead
        if random.random() < 0.03:     # occasional, like real side-locks
            ghost_ea = random.randint(lo, hi)
            cells.append(CellRecord(ghost_ea, random.randint(0, 503), band,
                                    C.earfcn_to_freq_mhz(band, ghost_ea),
                                    random.choice([6, 15]), 1,
                                    # weak, like real side-locks (-48..-59 seen
                                    # on hardware): at most a WARN, never CRIT
                                    round(random.uniform(-60, -52), 1)))
        return cells

    def stop(self):
        self._stop = True


def make_source(radio, cfg):
    if cfg.get("engine") == "cell_search":
        return CellSearchSource(radio, cfg)
    return SimSource(radio, cfg)


# ==========================================================================
# Baseline persistence
# ==========================================================================
class Baseline:
    def __init__(self, path="baseline_cells.json"):
        self.path = path
        self.cells = {}          # (earfcn, phyid) -> BaselineCell
        self.meta = {}           # gain, bands, created, engine

    def load(self):
        if not os.path.exists(self.path):
            return False
        with open(self.path) as f:
            data = json.load(f)
        self.meta = data.get("meta", {})
        self.cells = {}
        for d in data.get("cells", []):
            bc = BaselineCell.from_dict(d)
            self.cells[bc.key] = bc
        return True

    def save(self):
        data = {
            "meta": self.meta,
            "cells": [c.to_dict() for c in self.cells.values()],
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def add_manual(self, earfcn, phyid, band, prb=None, ports=None, pss_dbm=None):
        """Baseline-fill: a known-good cell the scan missed. Treated as normal."""
        freq = C.earfcn_to_freq_mhz(band, earfcn) or 0.0
        p = pss_dbm if pss_dbm is not None else 0.0
        bc = BaselineCell(
            earfcn=earfcn, phyid=phyid, band=band, freq_mhz=freq,
            prb=prb if prb is not None else 0,
            ports=ports if ports is not None else 0,
            pss_mean=p, pss_min=p, pss_max=p, pss_last=p,
            seen_count=0, total_passes=0, first_seen=_now_iso(),
            source="manual",
        )
        self.cells[bc.key] = bc
        self.save()
        return bc


# ==========================================================================
# Anomaly record
# ==========================================================================
class Anomaly:
    _seq = 0

    def __init__(self, atype, cell, detail, severity):
        Anomaly._seq += 1
        self.id = Anomaly._seq
        self.atype = atype                 # NEW / POWER / PCI / PRB_PORTS / MISSING
        self.severity = severity           # CRIT / WARN
        self.earfcn = cell.earfcn if cell else None
        self.phyid = cell.phyid if cell else None
        self.band = cell.band if cell else None
        self.freq_mhz = cell.freq_mhz if cell else None
        self.pss_dbm = cell.pss_dbm if cell else None
        self.detail = detail
        self.first_ts = _now_iso()
        self.last_ts = self.first_ts
        self.count = 1
        self.acked = False

    def bump(self, cell=None, detail=None):
        self.last_ts = _now_iso()
        self.count += 1
        if cell is not None:
            self.pss_dbm = cell.pss_dbm   # keep live power for the Yagi needle
        if detail:
            self.detail = detail

    def to_dict(self):
        return {
            "id": self.id, "atype": self.atype, "severity": self.severity,
            "earfcn": self.earfcn, "phyid": self.phyid, "band": self.band,
            "freq_mhz": self.freq_mhz, "pss_dbm": self.pss_dbm,
            "detail": self.detail, "first_ts": self.first_ts,
            "last_ts": self.last_ts, "count": self.count, "acked": self.acked,
        }


# ==========================================================================
# Engine: drives learn + monitor, owns shared state for the web layer
# ==========================================================================
class Engine:
    def __init__(self, cfg, radios):
        self.cfg = dict(C.DEFAULTS)
        self.cfg.update(cfg or {})
        self.radios = radios                      # list of radio dicts
        # The sim keeps its own baseline file, so a sim learn can never overwrite
        # the real-hardware baseline. Switching engine is then a one-line change.
        if self.cfg.get("engine") == "cell_search":
            bpath = self.cfg.get("baseline_path", "baseline_cells.json")
        else:
            bpath = self.cfg.get("sim_baseline_path", "baseline_sim.json")
        self.baseline = Baseline(bpath)
        self.baseline.load()

        self._lock = threading.Lock()
        self._worker = None
        self._stopflag = threading.Event()

        # shared state (read by the web layer every tick)
        self.mode = "idle"                        # idle / learning / monitoring
        self.status_line = "idle"
        self.progress = {"band": None, "idx": 0, "total": 0, "earfcn": 0,
                         "freq_mhz": 0.0, "pass": 0, "passes": 0}
        self.live_cells = []                      # last monitor cycle's finds
        self.anomalies = {}                       # id -> Anomaly (latched)
        self.cycle = 0
        self.last_cycle_ts = None
        self.siren_armed = bool(self.cfg.get("siren_armed"))
        # Discovery sweep on/off. Off = track scans only (baseline EARFCNs +/-
        # guard): fast cycles, but a fake on an EARFCN outside the baseline is
        # never seen. Switchable live from the GUI.
        self.discovery_enabled = bool(self.cfg.get("discovery_enabled", True))
        self.log = RunLog(self.cfg.get("log_enabled", False))

        # debounce bookkeeping: anomaly-key -> deque of recent hit booleans
        _win = max(int(self.cfg["debounce_window"]),
                   int(self.cfg.get("missing_hits", 3)))
        self._obs = defaultdict(lambda: deque(maxlen=_win))
        self._active_anoms = {}                   # anomaly-key -> Anomaly id
        self._last_discovery = 0
        # cells discovered during monitoring get promoted to track windows so
        # subsequent cycles re-scan and confirm them (lifecycle: discovered ->
        # tracked). (band, earfcn) -> cycle last seen.
        self._dynamic_tracks = {}

        # one sim source kept addressable so the GUI can inject a fake BTS.
        # injections live on the Engine so they survive source re-creation.
        self._sim = None
        self._injections = []   # list of (band, earfcn, phyid, prb, ports, dbm)

    # ----- source helpers -------------------------------------------------
    def _primary_radio(self):
        for r in self.radios:
            if r.get("enabled", True):
                return r
        return self.radios[0] if self.radios else {"name": "sim", "serial": None}

    def _source_for(self, radio):
        src = make_source(radio, self.cfg)
        src.log = self.log
        self._active_src = src      # so Stop can terminate an in-flight scan
        if isinstance(src, SimSource):
            self._sim = src
            # re-apply any standing fake-BTS injections to the fresh source
            for (band, ea, pci, prb, ports, dbm) in self._injections:
                src.inject_fake_bts(band, ea, pci, prb, ports, dbm)
        return src

    # ----- public control -------------------------------------------------
    def start_learn(self, bands):
        self._spawn(self._run_learn, (bands,), "learning")

    def start_monitor(self, bands):
        self._spawn(self._run_monitor, (bands,), "monitoring")

    def stop(self):
        self._stopflag.set()
        # Terminate the scan in flight. Without this a full-band sweep keeps
        # running after Stop, holds the radio, and the next run's scans fail.
        src = getattr(self, "_active_src", None)
        if src is not None:
            src.stop()
        w = self._worker
        if w and w.is_alive():
            w.join(timeout=15)
        self.mode = "idle"
        self.status_line = "stopped"

    def _spawn(self, target, args, mode):
        self.stop()
        self._stopflag.clear()
        self.mode = mode
        self._worker = threading.Thread(target=self._guard(target), args=args, daemon=True)
        self._worker.start()

    def _guard(self, target):
        def wrapped(*a):
            try:
                target(*a)
            except Exception as e:
                self.status_line = "ERROR: %s" % e
                self.mode = "idle"
                self.log.event("ERROR %s" % e)
        return wrapped

    def arm_siren(self, on):
        self.siren_armed = bool(on)
        self.log.event("SIREN %s" % ("armed" if on else "disarmed"))

    def set_discovery(self, on):
        self.discovery_enabled = bool(on)
        self.log.event("DISCOVERY %s" % ("on" if on else "off (track only)"))

    def ack(self, anom_id):
        with self._lock:
            if anom_id == "all":
                n = sum(1 for a in self.anomalies.values() if not a.acked)
                self.log.event("ACK  all, %d alerts" % n)
                for a in self.anomalies.values():
                    a.acked = True
                self._active_anoms.clear()
                self._obs.clear()
            else:
                a = self.anomalies.get(int(anom_id))
                if a:
                    a.acked = True
                    self.log.event("ACK  %s %s EARFCN=%s PCI=%s" % (
                        a.severity, a.atype, a.earfcn, a.phyid))
                    # drop it from active tracking so it can re-fire later
                    for k, aid in list(self._active_anoms.items()):
                        if aid == a.id:
                            self._active_anoms.pop(k, None)
                            self._obs.pop(k, None)
        self._maybe_reset_siren()

    def inject_fake_bts(self, band, earfcn, phyid, prb=6, ports=1, dbm=-20.0):
        """Sim only: make a strong NEW cell appear, to exercise detection."""
        self._injections.append((band, earfcn, phyid, prb, ports, dbm))
        self.log.event("INJECT sim fake B%d EARFCN=%d PCI=%d %.0f dBm" % (
            band, earfcn, phyid, dbm))
        if self._sim is not None:
            self._sim.inject_fake_bts(band, earfcn, phyid, prb, ports, dbm)
        return True

    def clear_fake_bts(self):
        if self._injections:
            self.log.event("INJECT cleared")
        self._injections = []
        if self._sim:
            self._sim.clear_injections()

    # ----- learn ----------------------------------------------------------
    def _run_learn(self, bands):
        M = int(self.cfg["learn_passes"])
        N = int(self.cfg["learn_min_seen"])
        radio = self._primary_radio()
        src = self._source_for(radio)
        self.log.event("START learn, engine=%s, bands=%s, gain %s, %d of %d passes" % (
            self.cfg["engine"], ",".join(map(str, bands)), self.cfg["gain_db"], N, M))

        # accumulate per (earfcn, phyid): list of CellRecord across passes
        seen = defaultdict(list)
        self.progress["passes"] = M
        for p in range(1, M + 1):
            if self._stopflag.is_set():
                break
            self.progress["pass"] = p
            for band in bands:
                if self._stopflag.is_set():
                    break
                self.status_line = "learn pass %d/%d - band %d" % (p, M, band)
                self.progress["band"] = band

                def _prog(idx, total, earfcn, freq, band=band):
                    self.progress.update({"idx": idx, "total": total,
                                          "earfcn": earfcn, "freq_mhz": freq})
                found = src.scan(band, on_progress=_prog)
                for rec in found:
                    seen[rec.key].append(rec)
                # surface partial finds so the operator sees the sweep working
                self.live_cells = [r.to_dict() for r in found]

        # promote cells seen in >= N passes
        promoted = {}
        for key, recs in seen.items():
            if len(recs) < N:
                continue   # decode flicker, not a real cell
            powers = [r.pss_dbm for r in recs]
            last = recs[-1]
            bc = BaselineCell(
                earfcn=last.earfcn, phyid=last.phyid, band=last.band,
                freq_mhz=last.freq_mhz, prb=last.prb, ports=last.ports,
                pss_mean=round(statistics.mean(powers), 1),
                pss_min=round(min(powers), 1), pss_max=round(max(powers), 1),
                pss_last=round(last.pss_dbm, 1),
                seen_count=len(recs), total_passes=M,
                first_seen=_now_iso(), source="scan",
            )
            promoted[key] = bc

        with self._lock:
            # keep any manual baseline-fill cells already present
            for k, v in self.baseline.cells.items():
                if v.source == "manual" and k not in promoted:
                    promoted[k] = v
            self.baseline.cells = promoted
            self.baseline.meta = {
                "created": _now_iso(), "gain_db": self.cfg["gain_db"],
                "engine": self.cfg["engine"], "bands": list(bands),
                "learn_passes": M, "learn_min_seen": N,
            }
            self.baseline.save()

        self.status_line = "learn done: %d cells in baseline (gain %.0f dB)" % (
            len(promoted), self.cfg["gain_db"])
        self.log.event("STOP learn: %s" % self.status_line)
        self.mode = "idle"

    # ----- monitor --------------------------------------------------------
    def _run_monitor(self, bands):
        if not self.baseline.cells and not bands:
            self.status_line = "no baseline and no bands - run learn first"
            self.mode = "idle"
            return
        radio = self._primary_radio()
        src = self._source_for(radio)
        guard = int(self.cfg["monitor_track_guard"])
        self.log.event(
            "START monitor, engine=%s, bands=%s, baseline=%s (%d cells), gain %s, "
            "discovery %s, floor %s, margin %s" % (
                self.cfg["engine"], ",".join(map(str, bands)) or "-",
                self.baseline.meta.get("created", "?"), len(self.baseline.cells),
                self.cfg["gain_db"], "on" if self.discovery_enabled else "off",
                self.cfg.get("crit_floor_dbm"), self.cfg.get("overshadow_margin_db")))
        cycle0 = self.cycle
        self._last_discovery = 0
        self._dynamic_tracks = {}

        while not self._stopflag.is_set():
            self.cycle += 1
            cycle_cells = []

            # ---- 1. track windows: narrow scan per known baseline cell ----
            #        plus any cell discovered earlier (promoted to tracked).
            windows = self._track_windows(guard)
            if not windows and not self.discovery_enabled:
                self.status_line = ("discovery off and nothing to track - "
                                    "learn a baseline or turn discovery on")
                for _ in range(10):
                    if self._stopflag.is_set():
                        break
                    time.sleep(0.1)
                continue
            for (band, lo, hi) in windows:
                if self._stopflag.is_set():
                    break
                self.status_line = "monitor cycle %d - track band %d [%d-%d]" % (
                    self.cycle, band, lo, hi)
                self.progress["band"] = band

                def _prog(idx, total, earfcn, freq, band=band):
                    self.progress.update({"idx": idx, "total": total,
                                          "earfcn": earfcn, "freq_mhz": freq,
                                          "pass": 0, "passes": 0})
                cycle_cells += src.scan(band, lo, hi, on_progress=_prog)

            # ---- 2. discovery sweep: full ticked bands, on a slower timer --
            now = time.time()
            if (self.discovery_enabled and
                    now - self._last_discovery >= self.cfg["discovery_every_s"]):
                self._last_discovery = now
                for band in bands:
                    if self._stopflag.is_set():
                        break
                    self.status_line = "monitor cycle %d - DISCOVERY band %d" % (
                        self.cycle, band)
                    self.progress["band"] = band

                    def _prog(idx, total, earfcn, freq, band=band):
                        self.progress.update({"idx": idx, "total": total,
                                              "earfcn": earfcn, "freq_mhz": freq})
                    cycle_cells += src.scan(band, on_progress=_prog)

            # ---- 3. diff + debounce + latch -------------------------------
            # A Stop mid-cycle leaves a partial scan. Evaluating it would raise
            # alerts (and false MISSINGs) from incomplete data, so discard it.
            if self._stopflag.is_set():
                break
            self._evaluate(cycle_cells)
            self.live_cells = [r.to_dict() for r in self._dedupe(cycle_cells)]
            self.last_cycle_ts = _now_iso()
            self.status_line = "monitor cycle %d done - %d live, %d alerts" % (
                self.cycle, len(self.live_cells), self._unacked_count())

            # small breather between cycles
            for _ in range(10):
                if self._stopflag.is_set():
                    break
                time.sleep(0.1)

        self.log.event("STOP monitor, %d cycles" % (self.cycle - cycle0))
        self.mode = "idle"

    def _track_windows(self, guard):
        """
        Narrow (band, lo, hi) scan windows: one per baseline cell, plus one per
        cell discovered during this monitor run (so a freshly-found NEW cell is
        re-scanned every cycle and can confirm through the debounce).
        """
        by_band = defaultdict(list)
        for bc in self.baseline.cells.values():
            by_band[bc.band].append(bc.earfcn)
        for (band, earfcn) in self._dynamic_tracks:
            by_band[band].append(earfcn)
        windows = []
        for band, earfcns in by_band.items():
            for ea in sorted(set(earfcns)):
                b = C.BANDS.get(band)
                if not b:
                    continue
                lo = max(b["earfcn_low"], ea - guard)
                hi = min(b["earfcn_high"], ea + guard)
                windows.append((band, lo, hi))
        return windows

    @staticmethod
    def _dedupe(cells):
        """Keep the strongest reading per (earfcn, phyid) within a cycle."""
        best = {}
        for r in cells:
            k = r.key
            if k not in best or r.pss_dbm > best[k].pss_dbm:
                best[k] = r
        return list(best.values())

    def _is_strong(self, rec, same_earfcn):
        """
        Decide whether an unexpected cell is 'strong' enough to be an overshadow
        attempt (CRIT) vs a weak/distant cell or flicker (WARN). Returns
        (bool, reason_str). Two tests, either triggers:
          - absolute: PSS >= overshadow_dbm (loud anywhere, e.g. an empty EARFCN)
          - relative: PSS within overshadow_margin_db of, or above, the loudest
                      legit cell already known on that EARFCN, AND at or above
                      crit_floor_dbm. The floor stops faint neighbour cells and
                      side-lock ghosts on busy EARFCNs from raising CRIT.
        """
        abs_floor = self.cfg.get("overshadow_dbm", -30.0)
        margin = self.cfg.get("overshadow_margin_db", 3.0)
        crit_floor = self.cfg.get("crit_floor_dbm", -47.0)
        if rec.pss_dbm >= abs_floor:
            return True, "loud: %.1f >= %.1f dBm" % (rec.pss_dbm, abs_floor)
        if same_earfcn:
            ref = max(b.pss_mean for b in same_earfcn)
            if rec.pss_dbm >= ref - margin:
                if rec.pss_dbm >= crit_floor:
                    return True, "overshadows real cell (%.1f vs %.1f dBm)" % (
                        rec.pss_dbm, ref)
                return False, "near real cell but below floor (%.1f < %.1f dBm)" % (
                    rec.pss_dbm, crit_floor)
        return False, "weak %.1f dBm" % rec.pss_dbm

    def _evaluate(self, cycle_cells):
        """Compare this cycle to baseline, debounce, latch anomalies, fire siren."""
        cells = self._dedupe(cycle_cells)
        seen_keys = set()
        hits_this_cycle = {}   # anomaly-key -> (atype, cell, detail, severity)

        for rec in cells:
            seen_keys.add(rec.key)
            bc = self.baseline.cells.get(rec.key)
            if bc is None:
                # Not a baseline cell: promote to a track window so the next
                # cycle re-scans it (lets a discovery-only find reach debounce).
                self._dynamic_tracks[(rec.band, rec.earfcn)] = self.cycle
                # Is this EARFCN known under a different PHYID? -> PCI conflict.
                same_earfcn = [b for b in self.baseline.cells.values()
                               if b.earfcn == rec.earfcn]
                # Severity is driven by POWER, not just the identity bucket.
                # A careless blaster reuses a real EARFCN with a mismatched PCI
                # and runs hot: an unexpected cell that is strong (loud in
                # absolute terms, or as loud as / louder than the real cell on
                # that EARFCN) is an overshadow attempt -> CRIT. A weak one
                # (distant real cell the learn pass missed, or flicker) -> WARN.
                strong, why = self._is_strong(rec, same_earfcn)
                sev = C.SEV_CRIT if strong else C.SEV_WARN
                if same_earfcn:
                    k = ("PCI", rec.earfcn, rec.phyid)
                    hits_this_cycle[k] = ("PCI", rec,
                        "EARFCN %d expected PHYID %s, saw %d  [%s, %.1f dBm]" % (
                            rec.earfcn, "/".join(str(b.phyid) for b in same_earfcn),
                            rec.phyid, why, rec.pss_dbm), sev)
                else:
                    k = ("NEW", rec.earfcn, rec.phyid)
                    hits_this_cycle[k] = ("NEW", rec,
                        "new cell: EARFCN %d PHYID %d, %d PRB, %d ports, %.1f dBm  [%s]" % (
                            rec.earfcn, rec.phyid, rec.prb, rec.ports, rec.pss_dbm, why),
                        sev)
                continue

            # known cell: power spike?
            if rec.pss_dbm - bc.pss_mean >= self.cfg["power_delta_db"]:
                k = ("POWER", rec.earfcn, rec.phyid)
                hits_this_cycle[k] = ("POWER", rec,
                    "power %.1f dBm vs baseline mean %.1f (+%.1f dB)" % (
                        rec.pss_dbm, bc.pss_mean, rec.pss_dbm - bc.pss_mean),
                    C.ANOMALY_SEVERITY["POWER"])
            # PRB / ports change? (only if baseline recorded them)
            if bc.prb and bc.ports and (rec.prb != bc.prb or rec.ports != bc.ports):
                k = ("PRB_PORTS", rec.earfcn, rec.phyid)
                hits_this_cycle[k] = ("PRB_PORTS", rec,
                    "shape %dPRB/%dport vs baseline %dPRB/%dport" % (
                        rec.prb, rec.ports, bc.prb, bc.ports),
                    C.ANOMALY_SEVERITY["PRB_PORTS"])

        # MISSING: baseline cell not seen this cycle (only meaningful if we
        # actually scanned its window, i.e. every cycle covers track windows).
        for key, bc in self.baseline.cells.items():
            if key not in seen_keys:
                k = ("MISSING", bc.earfcn, bc.phyid)
                # synthesize a pseudo-cell carrying identity for display
                pseudo = CellRecord(bc.earfcn, bc.phyid, bc.band, bc.freq_mhz,
                                    bc.prb, bc.ports, bc.pss_mean)
                hits_this_cycle[k] = ("MISSING", pseudo,
                    "baseline cell not seen this cycle", C.ANOMALY_SEVERITY["MISSING"])

        # prune dynamic tracks that have gone quiet and never latched, so the
        # track set doesn't grow without bound from transient ghosts.
        stale = []
        for k, last in self._dynamic_tracks.items():
            key_new = ("NEW", k[1], None)
            active = any(a.earfcn == k[1] and not a.acked
                         for a in self.anomalies.values())
            if self.cycle - last > 4 and not active:
                stale.append(k)
        for k in stale:
            self._dynamic_tracks.pop(k, None)

        self._debounce_and_latch(hits_this_cycle)

    def _debounce_and_latch(self, hits):
        need = int(self.cfg["debounce_hits"])
        need_missing = int(self.cfg.get("missing_hits", 3))
        fired_crit = False
        with self._lock:
            all_keys = set(self._obs.keys()) | set(hits.keys())
            for k in all_keys:
                self._obs[k].append(k in hits)
                recent = sum(1 for x in self._obs[k] if x)
                # MISSING needs more consecutive misses than NEW/POWER/PCI, so
                # weak cells flickering for a cycle don't raise a false alarm.
                threshold = need_missing if k[0] == "MISSING" else need
                if k in hits and recent >= threshold:
                    atype, cell, detail, sev = hits[k]
                    aid = self._active_anoms.get(k)
                    if aid and aid in self.anomalies:
                        self.anomalies[aid].bump(cell, detail)
                    else:
                        a = Anomaly(atype, cell, detail, sev)
                        self.anomalies[a.id] = a
                        self._active_anoms[k] = a.id
                        self.log.event("%s %-7s EARFCN=%s PCI=%s PSS=%s  %s" % (
                            sev, atype, a.earfcn, a.phyid,
                            "%.1f dBm" % a.pss_dbm if a.pss_dbm is not None else "-",
                            detail))
                        if sev == C.SEV_CRIT:
                            fired_crit = True
        if fired_crit:
            self._fire_siren()

    def _unacked_count(self):
        return sum(1 for a in self.anomalies.values() if not a.acked)

    def _crit_unacked(self):
        return any(a.severity == C.SEV_CRIT and not a.acked
                   for a in self.anomalies.values())

    # ----- siren hook -----------------------------------------------------
    def _fire_siren(self):
        cmd = self.cfg.get("alert_cmd")
        if not cmd or not self.siren_armed:
            return
        env = dict(os.environ)
        env["SMSBLASTER_SEVERITY"] = "CRIT"
        self.log.event("SIREN fire")
        try:
            # fire-and-forget: never block or crash the detector on a dead plug
            subprocess.Popen([cmd], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _maybe_reset_siren(self):
        """If nothing CRIT is unacked, call the hook's --reset to drop the latch."""
        cmd = self.cfg.get("alert_cmd")
        if not cmd:
            return
        if not self._crit_unacked():
            if self.siren_armed:
                self.log.event("SIREN reset")
            try:
                subprocess.Popen([cmd, "--reset"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # ----- snapshot for the web layer ------------------------------------
    def snapshot(self):
        with self._lock:
            anoms = sorted(self.anomalies.values(),
                           key=lambda a: (a.acked, {"CRIT": 0, "WARN": 1}[a.severity],
                                          -a.id))
            return {
                "mode": self.mode,
                "status": self.status_line,
                "cycle": self.cycle,
                "progress": dict(self.progress),
                "siren_armed": self.siren_armed,
                "siren_configured": bool(self.cfg.get("alert_cmd")),
                "discovery_enabled": self.discovery_enabled,
                "log_enabled": self.log.enabled,
                "injections": [
                    {"band": b, "earfcn": e, "phyid": p, "prb": r, "dbm": d}
                    for (b, e, p, r, _ports, d) in self._injections],
                "engine": self.cfg["engine"],
                "gain_db": self.cfg["gain_db"],
                "baseline_meta": self.baseline.meta,
                "baseline": [c.to_dict() for c in sorted(
                    self.baseline.cells.values(), key=lambda c: (c.band, c.earfcn))],
                "live": self.live_cells,
                "anomalies": [a.to_dict() for a in anoms],
                "last_cycle_ts": self.last_cycle_ts,
            }
