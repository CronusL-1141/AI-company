"""Feedback to the OS maintainers on GitHub: a bug as an Issue, an idea in Discussions.

An agent drafts with the user; OS checks and cleans the text, freezes it as a draft
and shows it back; only the user's own confirmation (the MCP client's dialog, or
their click on a prefilled GitHub page) publishes it.

* ``forms``: the public format: the sections of the repository's issue and
  discussion forms, the marker, the prefilled pages;
* ``redact``: the checks and rewrites the text goes through (secrets refused,
  personal paths and names replaced, private terms refused);
* ``github``: gh, found by absolute path and run as a child process with time limits;
* ``service``: a draft's life: dedupe, rate limit, sending, reconciling, audit events.

Nothing here starts a timer or a background task: expiry and reconciliation are
decided when a row is read.
"""
