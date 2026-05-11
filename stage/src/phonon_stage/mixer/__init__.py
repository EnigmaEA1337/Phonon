"""Mix console — strip-based audio router with master bus.

Replaces the legacy per-mapping model. The mixer exposes three kinds
of strips (Sources / Master / Outputs) and a routing matrix; per-
output configuration (delay/gain/HW) is set once and applies to every
source flowing through that output. See README in models.py.
"""
