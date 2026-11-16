import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.laboratory import (
    LaboratoryError,
    prepare_laboratory,
    postgres_tools,
    _tool_env,
)


class LaboratoryTests(TestCase):
    def test_invalid_names_and_service_schemas_fail_without_connection(self):
        for name in ("postgres", "same", "template0", "bad-name", "x" * 57, ""):
            with (
                self.subTest(name=name),
                patch("pqo.laboratory.psycopg.connect") as connect,
                self.assertRaises(LaboratoryError),
            ):
                prepare_laboratory(replace(DatabaseSettings(), dbname="same"), name)
            connect.assert_not_called()
        with (
            patch("pqo.laboratory.psycopg.connect") as connect,
            self.assertRaises(LaboratoryError),
        ):
            prepare_laboratory(
                replace(DatabaseSettings(), allowed_schemas=frozenset({"pqo"})), "copy"
            )
        connect.assert_not_called()

    def test_credentials_are_only_in_child_environment_and_conflicting_pg_env_is_removed(
        self,
    ):
        settings = replace(
            DatabaseSettings(),
            password="not-a-command-argument",
            sslmode="verify-full",
            sslrootcert="ca.pem",
        )
        with patch.dict(
            os.environ,
            {"PGSERVICE": "wrong", "PGDATABASE": "other", "PGPASSWORD": "old"},
        ):
            env = _tool_env(settings)
        self.assertNotIn("PGSERVICE", env)
        self.assertEqual(env["PGDATABASE"], settings.dbname)
        self.assertEqual(env["PGPASSWORD"], settings.password)
        self.assertEqual(env["PGSSLROOTCERT"], "ca.pem")
        self.assertEqual(env["PGSSLMODE"], "verify-full")

    def test_explicit_missing_tools_do_not_fall_back_silently(self):
        with self.assertRaises(LaboratoryError):
            postgres_tools(Path("missing-postgres-client-directory"))


@skipUnless(
    os.getenv("PQO_CONNECTION_INTEGRATION_TESTS") == "1",
    "Own laboratory fixtures and CREATEDB required",
)
class LaboratoryPostgresTests(TestCase):
    def setUp(self):
        self.settings = replace(
            DatabaseSettings.from_env(),
            dbname="pqo_labsource_" + uuid4().hex[:16],
            allowed_schemas=frozenset({"demo"}),
        )
        self.created = []
        self.addCleanup(self.cleanup)
        with self.connect("postgres", autocommit=True) as c:
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(self.settings.dbname)
                )
            )
            self.created.append(
                (
                    self.settings.dbname,
                    *c.execute(
                        "SELECT oid,datdba FROM pg_database WHERE datname=%s",
                        (self.settings.dbname,),
                    ).fetchone(),
                )
            )
        with self.connect() as c:
            c.execute("CREATE SCHEMA demo")
            c.execute(
                "CREATE TABLE demo.items(id serial PRIMARY KEY,value integer DEFAULT 7)"
            )
            c.execute("INSERT INTO demo.items DEFAULT VALUES")
            c.execute("CREATE SCHEMA unselected")
            c.execute(
                "CREATE TABLE unselected.secret(value text);INSERT INTO unselected.secret VALUES('not copied')"
            )

    def connect(self, database=None, **kwargs):
        return psycopg.connect(
            **replace(
                self.settings, dbname=database or self.settings.dbname
            ).connection_kwargs(),
            **kwargs,
        )

    def cleanup(self):
        with self.connect("postgres", autocommit=True) as c:
            for name, oid, owner in reversed(self.created):
                if c.execute(
                    "SELECT oid,datdba FROM pg_database WHERE datname=%s", (name,)
                ).fetchone() == (oid, owner):
                    c.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

    def run_copy(self, name, **kwargs):
        try:
            result = prepare_laboratory(self.settings, name, **kwargs)
        except LaboratoryError as exc:
            self.created.extend(exc.created)
            raise
        self.created.extend(result.identities)
        return result

    def test_logical_copy_works_with_source_session_open_and_isolates_work_from_baseline(
        self,
    ):
        name = "pqo_labtest_" + uuid4().hex[:12]
        with self.connect() as active:
            before = active.execute("SELECT * FROM demo.items ORDER BY id").fetchall()
            result = self.run_copy(name)
            self.assertEqual(
                active.execute("SELECT * FROM demo.items ORDER BY id").fetchall(),
                before,
            )
        self.assertEqual(result.schemas, ("demo",))
        for database in (result.reference_database, result.working_database):
            with self.connect(database) as c:
                self.assertEqual(
                    c.execute("SELECT * FROM demo.items ORDER BY id").fetchall(),
                    [(1, 7)],
                )
                self.assertEqual(
                    c.execute(
                        "SELECT last_value,is_called FROM demo.items_id_seq"
                    ).fetchone(),
                    (1, True),
                )
                self.assertFalse(
                    c.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='unselected')"
                    ).fetchone()[0]
                )
        with self.connect(result.working_database) as c:
            c.execute("UPDATE demo.items SET value=9")
        with self.connect(result.reference_database) as c:
            self.assertEqual(c.execute("SELECT value FROM demo.items").fetchone()[0], 7)
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT value FROM demo.items").fetchone()[0], 7)
        with self.assertRaises(LaboratoryError):
            self.run_copy(name)

    def test_selected_public_schema_restores_without_overwriting_existing_database(
        self,
    ):
        with self.connect() as c:
            c.execute(
                "CREATE TABLE public.extra(id integer PRIMARY KEY);INSERT INTO public.extra VALUES(3)"
            )
        self.settings = replace(self.settings, allowed_schemas=frozenset({"public"}))
        result = self.run_copy("pqo_labpublic_" + uuid4().hex[:10])
        with self.connect(result.working_database) as c:
            self.assertEqual(c.execute("SELECT id FROM public.extra").fetchone()[0], 3)
            self.assertFalse(
                c.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='demo')"
                ).fetchone()[0]
            )

    def test_cancellation_before_dump_creates_no_databases(self):
        control = AnalysisControl()
        control.cancel()
        with self.assertRaises(LaboratoryError) as caught:
            self.run_copy("pqo_cancel_" + uuid4().hex[:10], control=control)
        self.assertFalse(caught.exception.created)

    def test_restore_error_is_not_registered_and_partial_reference_is_reported(self):
        with self.connect() as c:
            c.execute("CREATE TABLE unselected.parent(id integer PRIMARY KEY)")
            c.execute(
                "CREATE TABLE demo.child(id integer PRIMARY KEY,parent integer REFERENCES unselected.parent(id))"
            )
        with self.assertRaises(LaboratoryError) as caught:
            self.run_copy("pqo_failed_" + uuid4().hex[:10])
        self.assertEqual(len(caught.exception.created), 1)
        self.assertTrue(caught.exception.created[0][0].endswith("_base"))

    def test_cancel_during_restore_retains_only_the_reported_new_reference(self):
        control = AnalysisControl()
        control.progress = lambda message: (
            control.cancel() if "Восстанавливаю" in message else None
        )
        name = "pqo_cancel_restore_" + uuid4().hex[:10]
        with self.assertRaises(LaboratoryError) as caught:
            self.run_copy(name, control=control)
        self.assertEqual(len(caught.exception.created), 1)
        self.assertEqual(caught.exception.created[0][0], name + "_base")
        self.assertIn("отменена", str(caught.exception))
        with self.connect("postgres") as c:
            self.assertFalse(
                c.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (name,)
                ).fetchone()
            )
        with self.connect() as c:
            self.assertEqual(c.execute("SELECT value FROM demo.items").fetchone()[0], 7)

    def test_destination_uses_its_own_role_and_password(self):
        role = "pqo_labrole_" + uuid4().hex[:10]
        password = uuid4().hex
        with self.connect("postgres", autocommit=True) as c:
            c.execute(
                sql.SQL("CREATE ROLE {} LOGIN CREATEDB PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            oid = c.execute(
                "SELECT oid FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()[0]
        try:
            destination = replace(
                self.settings, user=role, password=password, dbname="postgres"
            )
            result = self.run_copy(
                "pqo_destination_" + uuid4().hex[:10], destination_settings=destination
            )
            self.assertTrue(all(owner == oid for _, _, owner in result.identities))
            with psycopg.connect(
                **replace(
                    destination, dbname=result.working_database
                ).connection_kwargs()
            ) as c:
                self.assertEqual(c.execute("SELECT current_user").fetchone()[0], role)
                self.assertEqual(
                    c.execute("SELECT value FROM demo.items").fetchone()[0], 7
                )
            with self.connect() as c:
                self.assertEqual(
                    c.execute("SELECT value FROM demo.items").fetchone()[0], 7
                )
        finally:
            self.cleanup()
            with self.connect("postgres", autocommit=True) as c:
                if c.execute(
                    "SELECT oid FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone() == (oid,):
                    c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
