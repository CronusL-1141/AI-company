"""Multi-host sync groundwork: one node per machine, one center among them.

A node is an API process with its own database. In the default ``standalone`` role
nothing here changes behaviour: rows carry an empty ``origin_node``, change capture
is off and ids are random. The pieces are:

* ``tables``    which tables sync, on which face (shared, session, local);
* ``hlc``       hybrid logical clock values and the SQL that ticks them;
* ``node``      the node identity file and the governance scope it implies;
* ``changelog`` change-capture triggers, their suppression and reinstallation;
* ``surface``   which face each API route belongs to.

Reproducible row ids live in ``aiteam.ids``, below ``aiteam.types``.
"""
