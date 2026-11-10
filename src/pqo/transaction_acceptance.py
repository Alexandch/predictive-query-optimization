"""Fixed cross-database acceptance matrix, using only newly created lab fixtures.

No existing application database is opened or cloned. This is a diagnostic of
the guarded replay engine, not model training, production load or a holdout.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg import sql

from .analysis_control import AnalysisControl
from .config import DatabaseSettings
from .index_actions import IndexAction
from .transaction_mixed_recommendation import MixedReplayPolicy
from .transaction_replay import run_transaction_replay, state_digest
from .transaction_replay_guard import prepare_replay, replay_catalog
from .transaction_select_rewrite import (
    generate_transaction_select_rewrites,
    run_transaction_select_rewrite,
)


@dataclass(frozen=True)
class AcceptanceDomain:
    schema: str
    table: str
    key: str
    filter_key: str
    value: str
    rows: int

    @property
    def relation(self):
        return f'{self.schema}.{self.table}'

    @property
    def action(self):
        return IndexAction.create(self.schema, self.table, (self.filter_key,), (self.value,))

    def index_sql(self):
        queries = [f"UPDATE {self.relation} SET status='reviewed' WHERE {self.key}=1 RETURNING {self.key},status"]
        queries += [f'SELECT COUNT(*) AS n,SUM({self.value}) AS total FROM {self.relation} WHERE {self.filter_key}={value}'
                    for value in (17, 98, 245, 356, 419, 521, 647, 793)]
        return 'BEGIN;\n' + ';\n'.join(queries) + ';\nCOMMIT;'

    def negative_sql(self):
        return (f"BEGIN; UPDATE {self.relation} SET status='reviewed' WHERE {self.key}=1; "
                f'SELECT {self.key},status FROM {self.relation} WHERE {self.key}=1 ORDER BY {self.key}; COMMIT;')

    def rewrite_sql(self):
        return (f"BEGIN; UPDATE {self.relation} SET status='reviewed' WHERE {self.key}=1 RETURNING {self.key},status; "
                f'SELECT q.status,COUNT(*) AS n,SUM(q.{self.value}) AS total FROM ('
                f'SELECT {self.key},status,{self.value},payload FROM {self.relation} '
                f'ORDER BY payload DESC,{self.key}) AS q GROUP BY q.status ORDER BY q.status; '
                f'SELECT {self.key},status FROM {self.relation} WHERE {self.key}=1 ORDER BY {self.key}; COMMIT;')


DOMAINS = (
    AcceptanceDomain('retail', 'orders', 'order_id', 'customer_id', 'amount', 80_000),
    AcceptanceDomain('logistics', 'shipments', 'shipment_id', 'carrier_id', 'shipping_cost', 65_000),
    AcceptanceDomain('university', 'enrollments', 'enrollment_id', 'student_id', 'score', 72_000),
)


def _connect(settings, database, *, autocommit=True):
    return psycopg.connect(**replace(settings, dbname=database).connection_kwargs(), autocommit=autocommit,
                          application_name='pqo_transaction_acceptance',
                          options='-c search_path=pg_catalog -c statement_timeout=30000 -c lock_timeout=1000')


class AcceptanceFixture:
    """One random owned fixture. Never drop by prefix, FORCE or terminate sessions.

    An uncertain CREATE or identity mismatch is recorded for manual inspection.
    The recovery file is written before DDL and contains no passwords or rows.
    """
    def __init__(self, settings, domain, output):
        self.settings, self.domain = settings, domain
        self.name = 'pqo_mvpfixture_' + uuid4().hex
        self.identity = None
        self.create_attempted = False
        self.removed = False
        self.manifest = output / (self.name + '.json')

    def record(self):
        data = {'database': self.name, 'identity': self.identity, 'create_attempted': self.create_attempted,
                'removed': self.removed, 'host': self.settings.host, 'port': self.settings.port,
                'note': 'Owned lab fixture only; no automatic recovery or deletion by prefix.'}
        temporary = self.manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(self.manifest)

    def create(self):
        with _connect(self.settings, 'postgres') as connection:
            if connection.execute('SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s', (self.name,)).fetchone():
                raise FileExistsError('Random fixture name already exists; no database changed.')
            self.create_attempted = True
            self.record()
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.name)))
            self.identity = tuple(connection.execute(
                'SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (self.name,)).fetchone())
            self.record()
            connection.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(self.name)))
        d = self.domain
        with _connect(self.settings, self.name, autocommit=False) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(d.schema)))
            connection.execute(sql.SQL('CREATE TABLE {}.{} ({} integer PRIMARY KEY,{} integer NOT NULL,'
                                       'status text NOT NULL,{} numeric NOT NULL,payload text)').format(
                sql.Identifier(d.schema), sql.Identifier(d.table), sql.Identifier(d.key),
                sql.Identifier(d.filter_key), sql.Identifier(d.value)))
            connection.execute(sql.SQL("INSERT INTO {}.{} SELECT i,i%%1000,'open',i%%5000,"
                                       "repeat(md5(i::text),6) FROM generate_series(1,%s) i").format(
                sql.Identifier(d.schema), sql.Identifier(d.table)), (d.rows,))
            # An untouched table must also participate in the final-state check.
            connection.execute(sql.SQL('CREATE TABLE {}.sentinel(id integer PRIMARY KEY,note text)').format(sql.Identifier(d.schema)))
            connection.execute(sql.SQL("INSERT INTO {}.sentinel VALUES(1,'unchanged'),(2,NULL)").format(sql.Identifier(d.schema)))
            connection.execute(sql.SQL('ANALYZE {}.{}').format(sql.Identifier(d.schema), sql.Identifier(d.table)))

    def remove(self):
        if not self.create_attempted:
            return True
        with _connect(self.settings, 'postgres') as connection:
            actual = connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (self.name,)).fetchone()
            if actual is not None:
                if self.identity is None or tuple(actual) != self.identity:
                    return False
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.name)))
        with _connect(self.settings, 'postgres') as connection:
            self.removed = connection.execute('SELECT NOT EXISTS(SELECT 1 FROM pg_catalog.pg_database WHERE datname=%s)',
                                              (self.name,)).fetchone()[0]
        self.record()
        return bool(self.removed)

    def fingerprint(self):
        with _connect(self.settings, self.name) as connection:
            program = prepare_replay(self.domain.index_sql(), self.settings)
            tables = replay_catalog(connection, self.settings, program)
            state = state_digest(connection, tables, self.settings, time.monotonic()+30, AnalysisControl())
            indexes = connection.execute('SELECT schemaname,indexname,indexdef FROM pg_catalog.pg_indexes '
                                         'WHERE schemaname=%s ORDER BY 1,2', (self.domain.schema,)).fetchall()
        return {'state': [asdict(item) for item in state], 'indexes': indexes}


def acceptance_checks(report, expectation):
    """Correctness/cleanup gates are separate from timing observations."""
    checks = {'cleanup_verified': report.cleanup_verified and not report.leftover_databases,
              'no_false_recommendation': not report.recommendation.recommended
                  if expectation not in ('equivalent',) else True}
    if expectation in ('equivalent', 'no_benefit'):
        checks.update(completed=report.status == 'completed', initial=report.initial_states_equal,
                      outputs=report.results_equal, final=report.final_states_equal,
                      correctness=report.correctness_verified, complete_series=len(report.samples) == 6)
    elif expectation == 'different_output':
        checks.update(completed=report.status == 'completed', initial=report.initial_states_equal,
                      different_output=not report.results_equal, same_final=report.final_states_equal,
                      correctness_refused=not report.correctness_verified)
    elif expectation in ('cancelled', 'error', 'declined'):
        checks.update(expected_status=report.status == expectation, correctness_refused=not report.correctness_verified)
        if expectation == 'declined':
            checks['no_clones'] = not report.created_databases
    else:
        raise ValueError('Unknown acceptance expectation')
    return checks


def run_acceptance(output, settings, *, laboratory_confirmed=False, progress=print):
    """Fixed matrix, no retries, threshold tuning or dropping existing databases."""
    if not laboratory_confirmed:
        raise ValueError('Explicit confirmation of a trusted disposable laboratory server is required.')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    policy = MixedReplayPolicy()  # fixed before observations: 5 ms AND 5%, two series
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'purpose': 'Synthetic multi-database acceptance; not independent holdout or production benchmark.',
              'policy': asdict(policy), 'series': 2, 'domains': [asdict(d) for d in DOMAINS],
              'engine_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                for name in ('transaction_acceptance.py', 'transaction_replay.py', 'transaction_replay_guard.py',
                                             'transaction_select_rewrite.py', 'transaction_mixed_recommendation.py')},
              'databases': [], 'cases': []}

    def checkpoint():
        temporary = output / 'report.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(output / 'report.json')

    def measure(fixture, name, operation, expectation):
        progress(f'{fixture.domain.schema}: {name}')
        item = {'domain': fixture.domain.schema, 'case': name, 'expectation': expectation, 'status': 'running'}
        report['cases'].append(item)
        checkpoint()
        started = time.monotonic()
        measured = operation()
        checks = acceptance_checks(measured, expectation)
        # A new independent admin connection checks the exact names, not a prefix.
        with _connect(settings, 'postgres') as connection:
            remaining = connection.execute('SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s)',
                                            (list(measured.created_databases),)).fetchall()
        checks['independent_absence'] = not remaining
        item.update(status='passed' if all(checks.values()) else 'failed', checks=checks,
                    elapsed_seconds=time.monotonic()-started, measurement=asdict(measured))
        checkpoint()
        if not checks['cleanup_verified'] or not checks['independent_absence']:
            raise RuntimeError('Clone removal is unverified; further acceptance runs stopped.')

    checkpoint()
    try:
        with _connect(settings, 'postgres') as connection:
            report['postgres_version'] = connection.execute('SHOW server_version').fetchone()[0]
        for domain in DOMAINS:
            # No engine connection can target an existing application database:
            # this sentinel source name does not exist and is never opened.
            source = replace(settings, dbname='pqo_mvp_source_not_opened', allowed_schemas=frozenset({domain.schema}))
            fixture = AcceptanceFixture(source, domain, output)
            db_item = {'domain': domain.schema, 'database': fixture.name, 'cleanup_verified': False}
            report['databases'].append(db_item)
            before = None
            try:
                fixture.create()
                before = fixture.fingerprint()
                db_item['before'] = before
                common = dict(series=2, test_copy_confirmed=True, disposable_databases_confirmed=True,
                              policy=policy, recovery_directory=output / 'recovery')
                index_run = lambda text, actions, **kwargs: run_transaction_replay(
                    text, source, fixture.name, actions, **{**common, **kwargs})
                measure(fixture, 'selective-index', lambda: index_run(domain.index_sql(), (domain.action,)), 'equivalent')
                measure(fixture, 'redundant-pk-index', lambda: index_run(domain.negative_sql(),
                    (IndexAction.create(domain.schema, domain.table, (domain.key,)),)), 'no_benefit')
                options = generate_transaction_select_rewrites(domain.rewrite_sql(), source)
                option = next(o for o in options if o.number == 2 and o.rule_id == 'subquery-order-without-limit')
                rewrite_run = lambda variant, **kwargs: run_transaction_select_rewrite(
                    domain.rewrite_sql(), source, fixture.name, 2, variant, **{**common, **kwargs}).measurement
                measure(fixture, 'remove-inner-sort', lambda: rewrite_run(option.variant_sql), 'equivalent')
                wrong = option.variant_sql.replace('GROUP BY', "WHERE q.status <> 'reviewed' GROUP BY", 1)
                measure(fixture, 'wrong-select-after-update', lambda: rewrite_run(wrong), 'different_output')
                if domain == DOMAINS[0]:
                    control = AnalysisControl()
                    control.progress = lambda message: control.cancel() if message.startswith('Последовательное выполнение: 2') else None
                    measure(fixture, 'cancel-after-write', lambda: index_run(domain.index_sql(), (domain.action,), control=control), 'cancelled')
                    failing = domain.negative_sql().replace(
                        f'SELECT {domain.key},status',
                        f'INSERT INTO {domain.relation}({domain.key},{domain.filter_key},status,{domain.value},payload) '
                        f"VALUES(1,17,'duplicate',0,NULL); SELECT {domain.key},status", 1)
                    measure(fixture, 'duplicate-key-after-write', lambda: index_run(failing, (domain.action,)), 'error')
                    # Unsupported SQL must be rejected locally, before a reference/clone is opened.
                    blocked = domain.negative_sql().replace('SELECT ', 'SELECT now(),', 1)
                    try:
                        prepare_replay(blocked, source)
                    except ValueError:
                        report['cases'].append({'domain': domain.schema, 'case': 'unsupported-function', 'status': 'passed'})
                    else:
                        report['cases'].append({'domain': domain.schema, 'case': 'unsupported-function', 'status': 'failed'})
                    # A table DEFAULT is a catalog refusal, not an optimization failure.
                    with _connect(source, fixture.name) as connection:
                        connection.execute(sql.SQL('ALTER TABLE {}.{} ALTER COLUMN status SET DEFAULT {}').format(
                            sql.Identifier(domain.schema), sql.Identifier(domain.table), sql.Literal('open')))
                    try:
                        measure(fixture, 'unsupported-catalog', lambda: index_run(domain.index_sql(), (domain.action,)), 'declined')
                    finally:
                        with _connect(source, fixture.name) as connection:
                            connection.execute(sql.SQL('ALTER TABLE {}.{} ALTER COLUMN status DROP DEFAULT').format(
                                sql.Identifier(domain.schema), sql.Identifier(domain.table)))
                db_item['after'] = fixture.fingerprint()
                db_item['unchanged'] = db_item['after'] == before
            finally:
                db_item['cleanup_verified'] = fixture.remove()
                checkpoint()
            if not db_item['cleanup_verified'] or not db_item.get('unchanged'):
                raise RuntimeError('Fixture data/catalog changed or removal unverified; stopped, no automatic recovery.')
        report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['cases']) else 'failed'
    except BaseException as exc:
        report['status'] = 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'error'
        # SQL error details may contain row values; do not persist exception text.
        report['error_type'] = type(exc).__name__
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New output directory; existing data are never overwritten.')
    parser.add_argument('--confirm-trusted-laboratory-server', action='store_true')
    args = parser.parse_args()
    report = run_acceptance(args.output, DatabaseSettings.from_env(),
                            laboratory_confirmed=args.confirm_trusted_laboratory_server)
    print(f"Acceptance: {report['status']}; cases: {len(report['cases'])}; report: {args.output / 'report.json'}")
    return 0 if report['status'] == 'passed' else 3


if __name__ == '__main__':
    raise SystemExit(main())
