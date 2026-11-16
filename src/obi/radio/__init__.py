"""Walkie-talkie radio station (feature 088).

obi as a station on the BRC IT ops channel via the criticalrf USB interface:
a C-Media USB audio codec (RX in / TX out) and an FTDI FT232R whose modem-control
lines key the transmitter.

Hardware constants and safety constraints throughout this package derive from the
measured findings of the ~/src/criticalrf reference work (2026-08-19 calibration):
the open()-keys-TX transient, both-DTR-and-RTS-key behavior, the sole-holder
de-key guarantee (and the second-fd stuck-PTT hazard it implies), the absence of a
COS line, the -0.2 dBFS PTT click, and the RX/TX level calibration. Those
measurements are normative; see specs/088-radio/ for the spec, plan, and the
deterministic TX-gate contract (contracts/tx-gate.md, G1-G12).

The model never touches the PTT: LLM output is text; the deterministic TxExecutor
(tx.py) owns the only code path that keys the radio.
"""
