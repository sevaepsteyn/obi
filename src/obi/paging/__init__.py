"""The PlayaPage paging egress channel (feature 102).

obi's third egress channel after Slack DMs and radio TX: text pages to real pagers carried
by BRC ESD responders, via PlayaPage's uAPI. Exactly TWO paths may send a page, both outside
any reasoning-model surface (constitution 1.15.0, the paging egress gate):

- a human typing recipients at the CLI (``obi page send``), and
- the deterministic 095 dash-pager transport (:mod:`obi.paging.alerts`): statically
  configured person IDs, off by default, hard hourly cap.

No paging capability exists in the shared LLM tool registry — the model can never initiate,
address, or compose a page. Modules: :mod:`~obi.paging.client` (uAPI transport),
:mod:`~obi.paging.roster` (operator-local recipient snapshot + strict name resolution),
:mod:`~obi.paging.alerts` (the dash transport).
"""
