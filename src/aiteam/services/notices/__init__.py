"""User-facing notices: catalog, rendering, ledger and detectors.

Design: docs/user-notice-design.md. The API decides what to show and renders it;
exit hooks only print what ``POST /api/notices/pending`` returns.
"""
