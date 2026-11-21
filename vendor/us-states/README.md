# Offline US registered states

Reduced from the FCC Universal Licensing System public Amateur Radio weekly
license archive, snapshot 2026-11-15:
https://data.fcc.gov/download/pub/uls/complete/l_amat.zip

The FCC dataset is published as US government work:
https://catalog.data.gov/dataset/fcc-universal-licensing-system-uls
https://www.usa.gov/government-copyright

Source SHA-256:
4eb29c69441a273997a7c52c3b45462fbfb756e37a365bb5f05778f867089ba0

The SQLite states table contains only active license callsigns and their
registered two-letter state/territory codes: 821,787 pairs. It contains no
names, street addresses, ZIP codes, email addresses, phone numbers or FRNs.
Only HD active Amateur records and EN licensee records are used. Metadata
records source, snapshot date, checksum and record count. Rebuild with:

    python3 tools/build_us_states.py l_amat.zip vendor/us-states/states.sqlite3

State labels describe the registered license address, not a verified current
operating position. Portable, POTA and SOTA stations may operate elsewhere.
Unmapped mainland US calls show USA-??; call-area digits are not used to
invent a state. CTY's specific US entities can supply Alaska/Hawaii/territory
codes when an exact FCC record is unavailable. Foreign operating prefixes
retain their CTY entity rather than showing a US home state.

Optional replacement database: ~/.config/icomzero/us-states.sqlite3.
Restart ICOMzero to clear its lookup cache after replacing it.
