"""Deployment-identity tooling (feature 137): keep the tracked tree free of real identifiers.

``tokens`` builds the private identifier set (denylist + 134 mapping + config-only harvest);
``guard`` scans tracked files and reports value-free findings; ``scrub`` replaces real values with
their mapping fakes and archives originals privately; ``callsign`` curates radio/mesh fakes;
``moved`` lists the values moved from source into config; ``parity`` proves the live host behaves
the same after the move; ``hook`` is the pre-commit entry point.
"""
