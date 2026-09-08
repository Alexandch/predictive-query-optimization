# Pagila provenance

- upstream: https://github.com/devrimgunduz/pagila
- version: `v3.1.0`;
- commit: `fef9675714cfba1756df4719b5e36075a7ddf90e`;
- license: MIT-style license in `LICENSE.txt`;
- source files: `pagila-schema.sql` and `pagila-data.sql`.

The vendored initialization files were mechanically adapted for this project:

1. qualified identifiers `public.*` were changed to `pagila.*`;
2. `CREATE SCHEMA IF NOT EXISTS pagila` replaced ownership of `public`;
3. dump-specific `OWNER TO postgres` statements were removed;
4. vendored functions received a fixed `pagila, pg_temp, pg_catalog` search path;
5. tables, constraints, indexes, views, functions, triggers and data were kept.

Resulting files:

- `database/init/050_pagila_schema.sql`, SHA-256
  `E758C99B5884D77834EB17E10A05CDAFD1CFE9D173EFA68E422F5BFDBB706329`;
- `database/init/051_pagila_data.sql`, SHA-256
  `6C43B604DEA8AE3E03B110E17BC4518D433131F4D41E2882337C0F3536AB1E73`.

Pagila 3.1 was selected because it works with the project's PostgreSQL 17
container without the PostgreSQL 18 UUID and pgvector requirements introduced
by the newer upstream branch.
