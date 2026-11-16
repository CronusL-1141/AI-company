"""Multi-host sync: one node per machine, one center among them.

A node is an API process with its own database. In the default ``standalone`` role
nothing here changes behaviour: rows carry an empty ``origin_node``, change capture
is off and ids are random. The pieces are:

* ``tables``    which tables sync, on which face (shared, session, local);
* ``hlc``       hybrid logical clock values and the SQL that ticks them;
* ``node``      the node identity file and the governance scope it implies;
* ``changelog`` change-capture, field-clock and tombstone triggers, the clock floor;
* ``surface``   which face each API route belongs to;
* ``context``   the operation, origin and role the running code acts under;
* ``protocol``  versions, timings and ports two nodes agree on;
* ``routing``   matching a request to its route and face, running one in-process;
* ``rows``      rows of synced tables as they travel: shape, key, stored form;
* ``replica``   an edge's replica of the shared face, written as the center has it;
* ``merge``     how the center merges a write made against an older state;
* ``center``    the center, made of ``operations`` (each exactly once), ``intake``
                (the edges' session faces), ``feed`` (handshake, change log, rows),
                ``conflicts`` and ``wakes``;
* ``effects``   one writer per origin: an operation's session writes go back to its node;
* ``edge``      an edge's write path (forward or stage) and its worker process;
* ``ledger``    an edge's bookkeeping: its link to the center, its queue;
* ``upload``    an edge's session face going up, batch by batch;
* ``worker``    the edge's worker: link state, replay, upload, replica, settling;
* ``status``    this node's sync health; ``cli`` the operator's commands.

Reproducible row ids live in ``aiteam.ids``, below ``aiteam.types``.
"""
