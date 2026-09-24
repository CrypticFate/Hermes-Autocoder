# Phase 2: GitHub Access

Map each owner to a secret file, enumerate all accessible repository pages,
filter ownership, archived/fork/empty/excluded repositories, and track access
revocation and renames. Controller-only authenticated git; no token in remotes.

Acceptance: pagination, owner filtering, existing PR reconciliation, bounded
rate-limit handling and ambiguous write timeout tests. Live account coverage
must be checked against the selected token's actual permissions.
