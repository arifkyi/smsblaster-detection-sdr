<p align="center">
  <img src="SDR%20Signal%20Hunt%20Over%20Cyber%20City.png" alt="smsblaster-detection-sdr" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="license MIT">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue?logo=python&logoColor=white" alt="python 3.8+">
  <img src="https://img.shields.io/badge/shell-bash-green?logo=gnubash&logoColor=white" alt="shell bash">
  <img src="https://img.shields.io/badge/platform-Linux-lightgrey" alt="platform Linux">
  <a href="https://www.youtube.com/@RifkyTheCyber"><img src="https://img.shields.io/badge/YouTube-Rifky%20The%20Cyber-red?logo=youtube&logoColor=white" alt="YouTube Rifky The Cyber"></a>
</p>

# smsblaster-detection-sdr

Receive-only fake-BTS / SMS-blaster detector for LTE downlink, built on srsRAN
`cell_search` with a USRP B210 (or LibreSDR). It learns a baseline of the real
cells around you, then watches continuously and alarms on anything that does
not belong: a new cell, a sudden power spike, a cell on a known channel with
the wrong identity. A web dashboard shows live state and hands you an EARFCN
plus downlink frequency to tune a Yagi for direction finding, and can trigger a
smart-plug siren on a critical alert.

It is receive-only. It measures and decodes the downlink only. It never
transmits.

> **YouTube:** https://www.youtube.com/@RifkyTheCyber
> **Video walkthrough:** https://www.youtube.com/@RifkyTheCyber  *(placeholder, will be replaced with the specific video link)*

This is the SDR / over-the-air counterpart to
[**smsblaster-detection-signaling**](https://github.com/arifkyi/smsblaster-detection-signaling),
which detects the same threat from a rooted phone at the signaling layer. See
*Limitations* below for how the two compare.

---

## How it works

**Learn** runs `cell_search -b <band>` across each ticked band, a few passes,
and keeps only cells that appear in at least N of the passes. Decode junk is
dropped two ways: a PRB sanity filter removes non-standard bandwidths (e.g. the
150-PRB artifact a strong cell produces), and the N-of-M confirmation drops any
identity that does not repeat across passes. Confirmed cells, with averaged PSS
power, are written to `baseline_cells.json`.

**Monitor** re-scans continuously:
- narrow `-s/-e` track windows around each known cell, every cycle, to catch
  power / PCI / PRB changes
- a slower full-band discovery sweep to catch brand-new EARFCNs, including on
  bands that were empty at learn. A newly found cell is promoted to a track
  window and confirmed over the next cycles before it alarms.

Anomalies latch on the dashboard until you acknowledge them.

### Anomaly types and severity

Severity is driven by **power**, not just by which identity bucket a cell falls
in. An *unexpected* cell (a brand-new EARFCN, or a new PCI on a known EARFCN)
is escalated to **CRIT when it is strong** and left at **WARN when it is weak**.
This is what catches a careless SMS blaster: it reuses a real channel with
whatever PCI its config happens to hold and runs hot, so it overshadows the
real cell and trips CRIT regardless of the PCI it uses. A weak unexpected cell
(a distant tower the learn pass missed, or decode flicker) stays WARN.

| Anomaly | Meaning | Severity |
|---|---|---|
| NEW | unknown (EARFCN, PHYID) appears | CRIT if strong, WARN if weak |
| PCI | known EARFCN, wrong/extra PHYID (impersonation on a real channel) | CRIT if strong, WARN if weak |
| POWER | a known cell's PSS jumps > `power_delta_db` | CRIT |
| PRB_PORTS | a known cell's bandwidth or antenna ports changed | WARN |
| MISSING | a baseline cell is no longer seen this cycle | WARN |

"Strong" means either loud in absolute terms (PSS at or above `overshadow_dbm`,
good for an empty EARFCN with no real cell to compare against) or loud relative
to the real cell on that EARFCN (within `overshadow_margin_db` of it, or
louder). The siren fires only on **CRIT**.

---

## Requirements

- srsRAN 4G built, with `cell_search` at the path in `bts_config.py`
- UHD + a USRP B210 (first-class) or LibreSDR (needs its `fpga=` image path)
- Python 3.9+ and Flask (`pip install -r requirements.txt`)

TDD bands (B40 etc.) decode on a stock srsRAN build, confirmed on hardware.
HackRF and RTL-SDR are not supported for this mode (decode reliability and
frequency range).

### Antenna ports (B210 / LibreSDR)

| Role | Device | Port |
|---|---|---|
| Detector (cell_search, RX only) | USRP | **RX2 A** |
| Fake BTS (srsENB, TX) | LibreSDR | **TX/RX A** |

cell_search receives on channel A, default antenna RX2. Confirm by scanning a
known-strong cell and checking it is found. srsENB also opens an RX channel for
uplink on RX2 A, but you do not need an antenna there for detection.

---

## Run

1. Edit `bts_config.py`: set `engine` to `cell_search`, confirm the radio
   serial, gain, and (LibreSDR only) the absolute `fpga=` path on this box.
2. Start it (with your venv active):

   ```
   python3 bts_app.py
   ```

3. Open the dashboard from any device on the LAN:

   ```
   http://<this box IP>:8080
   ```

Tick the bands, click **Learn baseline**, wait for it to finish, then
**Start monitor**. Monitor runs from a saved `baseline_cells.json` without
re-learning, so Stop/Start monitor as often as you like; only click Learn when
you want a fresh reference.

> Run `bts_app.py` locally on the box, or in `tmux`/`screen` over SSH. A plain
> SSH session that drops will send SIGHUP and kill the server mid-scan.

### Testing with no hardware

Set `engine` to `sim`. It synthesizes cells (stable ones plus decode noise) so
you can exercise the whole pipeline and the UI with no radio. In sim mode the
dashboard shows a **Sim test - inject fake BTS** panel to make a strong cell
appear and watch it alarm.

---

## Quick test (real hardware, cell_search)

The fast path to a live demo once srsRAN + UHD + the USRP are ready. Do the
slow part (learn) once, up front, so the demo itself is instant.

**1. Set these in `bts_config.py`:**

```python
"engine": "cell_search",   # real srsRAN on this box
"gain_db": 45.0,           # dense-area value; raise in a quiet area
"debounce_hits": 1,        # fire on the first detection (snappy)
```

**2. Start and open the dashboard:**

```
python3 bts_app.py
```
then `http://<this box IP>:8080`.

**3. Secure the baseline (do this before recording).** Tick your band (e.g.
B3), click **Learn baseline**, wait for "learn done". This writes
`baseline_cells.json`. Monitor runs from that file without re-learning, so you
only pay the learn time once. Back it up:

```
cp baseline_cells.json baseline_cells.json.bak
```

If overload shows (scans crawl, garbage PCIs, the same cell under two PCIs),
lower `gain_db` and re-learn. See *Tuning* below.

**4. Start monitor.** With `debounce_hits: 1` an anomaly fires on the first
detection, no waiting.

**5. Trigger a fake.** Bring up your own test cell (srsENB) on a chosen EARFCN.
On a real EARFCN it must out-power the real cell to be decoded (overshadow); on
an empty EARFCN it fires a clean CRIT / NEW, but is first caught by the
discovery sweep, so lower `discovery_every_s` (e.g. 20) for a quick catch. See
*Detecting a fake BTS*.

**6. Suppress / re-trigger.** **Acknowledge** clears and re-arms (and resets the
siren). Fake off then on for a fresh take.

---

## Tuning for your environment

The defaults shipped here (`gain_db` 45, `nof_frames` 20) were tuned on a dense
urban band 3 (Jakarta). Adjust for your site:

- **Gain is the first thing to get right.** Too high in a dense area overloads
  the front end: scans crawl (the radio stalls at every strong EARFCN) and
  decodes turn to garbage (wrong PCIs, repeated PCI 0, the same cell reported
  under two PCIs). If you see that, lower `gain_db` (dense city ~40-50, quiet
  area higher). Healthy PSS readings land roughly -45 to -60 dBm; values pinned
  near -15 to -20 across the band mean overload.
- **Speed vs coverage.** A full dense band is slow (band 3 is ~749 EARFCNs). To
  go faster, lower `nof_frames` (shorter dwell per EARFCN) and/or narrow the
  sweep with `scan_ranges` (below). A full 2-pass band-3 learn at gain 45 /
  nof_frames 20 takes roughly 15 minutes.
- **Confirmation.** `learn_passes` 2 with `learn_min_seen` 2 (2-of-2) filters
  flicker. Use 1 pass only for a quick look, and set `learn_min_seen` to 1 if
  you do, or nothing qualifies and the baseline comes back empty.

### scan_ranges (narrow a band)

By default each ticked band is swept in full. To sweep only part of a band,
add a `scan_ranges` entry mapping the band to a `(start_earfcn, end_earfcn)`
window. It applies to both learn and the discovery sweep.

```python
"scan_ranges": {3: (1300, 1500)},   # only EARFCN 1300-1500 on band 3
```

Narrowing is the main dial for a fast demo: scan the slice around the cells you
care about instead of the whole band. Leave it out (or empty) for full sweeps.

---

## Detecting a fake BTS (bench method)

A fake on a **real** EARFCN competes with the real cell. cell_search reports
only the strongest cell per EARFCN, so the fake must *dominate* the real one at
your detector antenna to be decoded and flagged. Two consequences:

- Put an antenna on the fake's TX/RX A port (a bare connector radiates almost
  nothing). At a few metres, enough tx_gain and the overshadow margin is large
  and the CRIT is reliable; too small a margin (a few dB) and the lock flips
  between fake and real cycle to cycle, so detection is intermittent.
- For a clean, repeatable demo, aim the fake at an **empty EARFCN** (one not in
  your baseline). With no real cell to fight, the fake decodes every cycle and
  fires a crisp CRIT / NEW.

**Note on live bands:** transmitting on an operator's live EARFCN radiates into
licensed spectrum. Keep tests brief and low-power, use a cable with attenuation
(>= 30 dB) between the two SDRs for a conducted test, or use spectrum you are
authorised to transmit on.

### Suppress and re-trigger loop

- **Acknowledge** (or **Acknowledge all**) clears the alert card, drops it from
  tracking, and calls the siren hook's `--reset` (plug off, latch cleared). It
  is the single action that both silences and re-arms.
- After acknowledging, the same anomaly can fire fresh again. With the fake
  still transmitting it re-confirms over the debounce window and re-latches; for
  a clean take, stop the fake, let the card clear, then start it again.

---

## Siren (optional)

Point `alert_cmd` in `bts_config.py` at the absolute path of `siren_hook.sh`,
then arm the **Siren** toggle on the dashboard. On a confirmed **CRIT** the
detector runs the hook with `SMSBLASTER_SEVERITY=CRIT`; the hook latches a
TP-Link smart plug on (once per episode) and holds it until reset.
Acknowledging the alert calls the hook's `--reset`. WARN never sounds it.

Both gates must be true: `alert_cmd` set in the config **and** the dashboard
toggle armed. `alert_cmd` is config-only (it runs a command on the box, so it
is not changeable from the web UI); arm/disarm is the live GUI toggle.

Manual control of the hook:
```
SMSBLASTER_SEVERITY=CRIT ./siren_hook.sh   # fire (test)
./siren_hook.sh --reset                    # silence + clear latch
```

`siren_hook.sh` and `Tplinkcomprehensivescript.py` are included. The plug is
LAN-controlled (XOR cipher, local port 9999, no cloud); keep the detector and
plug on the same trusted LAN.

### Recording-day notes

- The siren depends on the LAN. **Running the USRP on USB 3.0 emits noise
  around 2.4 GHz and can knock out 2.4 GHz WiFi on the same box**, which drops
  the plug link. Put the box on **wired ethernet** (or 5 GHz WiFi) for a stable
  siren.
- Keep the siren **disarmed** during setup and rehearsals (full detection still
  shows on screen), and arm only for the take. Acknowledge quickly to cut the
  sound. A flashing light on the plug often films better than a continuous
  blare.

---

## Config reference (`bts_config.py`)

| Key | Shipped | Meaning |
|---|---|---|
| engine | cell_search | `sim` for no-hardware testing |
| cell_search_bin | /usr/src/... | path to the srsRAN cell_search binary |
| gain_db | 45 | `-g`. Must match between learn and monitor. Lower in dense areas. |
| nof_frames | 20 | `-n`. Lower = faster per EARFCN, slightly less decode reliability. |
| learn_passes | 2 | sweeps per band during learn |
| learn_min_seen | 2 | must appear in >= this many passes to enter the baseline |
| power_delta_db | 6 | PSS rise over baseline (known cell) that counts as a spike |
| overshadow_dbm | -30 | absolute level at/above which an unexpected cell is CRIT |
| overshadow_margin_db | 3 | within this of the real cell on that EARFCN is also CRIT |
| debounce_hits / window | 2 / 2 | confirmations needed before an alarm fires (N-of-M) |
| discovery_every_s | 60 | full-band discovery cadence (lower for a snappy demo) |
| monitor_track_guard | 2 | +/- EARFCN padding around each tracked cell |
| scan_ranges | (unset) | optional per-band `{band: (start, end)}` sweep limit |
| host / port | 0.0.0.0 / 8080 | LAN-reachable web server |
| alert_cmd | (path) | siren hook to run on CRIT; empty disables the hook |

`RADIOS` is a list; the app spawns one worker per enabled radio. It ships with
one USRP B210 enabled and a LibreSDR entry disabled (enable both, with distinct
serials, only for two-radio use on one box).

Manual baseline-fill: add a known-good cell the scan missed via the dashboard
form (EARFCN + PCI + band required; PRB / ports / PSS optional for tighter
change-detection).

---

## Limitations (and how this compares to an Android detector)

cell_search decodes down to the **MIB (PBCH)** only, not SIB1. So on LTE this
tool gives you PHY-layer identity and power, and not network identity:

- **No TAC, no Cell ID / eNB ID, no PLMN (MCC/MNC).** Those live in SIB1, which
  this tool does not decode. An Android-based detector (reading the modem via a
  NetMonster-type app) does see them, so it can say "this cell claims my
  operator but with an unknown TAC/CID".
- **No neighbour lists, no reject causes, no forced-downgrade visibility.** A
  handset sees the signalling the network sends it; this is an RF-side observer,
  so it infers from spectrum, not from attach/reject messages.

What the SDR side does that a phone cannot:

- **Sweeps the band independently of a SIM or modem**, so it sees cells your
  phone would never camp on, and is not limited by chipset or carrier.
- **PSS power per cell for direction finding** with a Yagi, which a phone does
  not expose.
- **No rooted phone or chipset quirks**; it is a measurement instrument.

The two are complementary: Android reads the identity/signalling layer, the SDR
reads the RF/power layer and does DF. Closing the TAC/CID gap on LTE is the
SIB1 stage-2 decode on the roadmap.

---

## Files

| File | Role |
|---|---|
| `bts_common.py` | band plan, EARFCN math, cell_search parsing, decode filters, defaults |
| `bts_scanner.py` | engine: learn, monitor, anomaly detection, overshadow gate, baseline, alert hook |
| `bts_app.py` | Flask + SSE web server and control endpoints |
| `static/bts.html` | dashboard |
| `bts_config.py` | per-box profile: engine, radios, tunables |
| `siren_hook.sh`, `Tplinkcomprehensivescript.py` | optional smart-plug siren |

---

## Roadmap

2G/GSM engine (grgsm_scanner, which gives CID / LAC / MCC / MNC directly),
SIB1 stage-2 decode for TAC / eNB-ID / PLMN, adjacent-EARFCN collapse to merge
one carrier's side-locks into a single baseline entry, live two-radio split,
watch-trap manual entries, and web auth.

---

## Disclaimer

For research and defensive detection only. This tool is receive-only and never
transmits. Running your own test cell (fake BTS) to exercise it, however, does
transmit on licensed spectrum and is regulated. Do it cabled and attenuated
(>= 30 dB) or in a shielded environment, on a test PLMN with your own SIMs. Only
sweep and detect on spectrum you are authorised to receive. You are responsible
for complying with local law.

---

## License

MIT. Add a `LICENSE` file to the repo for the badge above to link to.

---

## Support

If this is useful to you, you can support the work:

**[Support on Ko-fi](https://ko-fi.com/rifkythecyber)**

Or scan the QR code below:

<img src="https://github.com/user-attachments/assets/a6529b25-06eb-4072-9077-6682aad0807a" alt="Donate" width="200">

**YouTube:** https://www.youtube.com/@RifkyTheCyber
