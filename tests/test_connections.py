import json
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

from pqo.config import DatabaseSettings
from pqo.connections import (
    ConnectionProfile,
    SERVICE_SCRIPTS,
    decode_profiles,
    discover_schemas,
    encode_profiles,
    endpoint,
    prepare_service_schema,
    target_context,
    create_service_database,
)


class ConnectionTests(unittest.TestCase):
    def test_profiles_roundtrip_without_secrets_and_with_remote_ssl(self):
        settings = DatabaseSettings(
            host="db.example.invalid",
            port=5432,
            dbname="application",
            password="DO-NOT-SAVE",
            allowed_schemas=frozenset({"sales", "crm"}),
            sslmode="verify-full",
            sslrootcert="C:/certs/root.crt",
        )
        profile = ConnectionProfile.from_settings("Удалённая БД", settings)
        raw = encode_profiles([profile])
        self.assertNotIn("DO-NOT-SAVE", raw)
        self.assertNotIn("password", raw)
        decoded = decode_profiles(raw)[0]
        self.assertEqual(decoded.settings("DO-NOT-SAVE"), settings)
        self.assertEqual(decoded.settings().password, "")
        self.assertEqual(settings.connection_kwargs()["sslmode"], "verify-full")
        self.assertNotIn("DO-NOT-SAVE", json.dumps(target_context(settings)))
        self.assertEqual(
            endpoint(settings), endpoint(replace(settings, password="other"))
        )

    def test_malformed_profiles_rejected(self):
        valid = ConnectionProfile.from_settings(
            "local", DatabaseSettings()
        ).public_dict()
        for value in (
            {},
            [],
            [valid, valid],
            [dict(valid, password="secret")],
            [dict(valid, schemas="public")],
            [dict(valid, port=0)],
            [dict(valid, schemas=[])],
            [dict(valid, name=None)],
            [dict(valid, sslmode="unverified")],
            [{}],
        ):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                decode_profiles(json.dumps(value))

    def test_discovery_only_reads_metadata_and_uses_requested_endpoint(self):
        settings = DatabaseSettings(
            host="remote.invalid", port=6543, dbname="remote", sslmode="require"
        )
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("remote",)
        connection.execute.return_value.fetchall.return_value = [
            ("public", 12),
            ("sales", 7),
        ]
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            self.assertEqual(
                discover_schemas(settings), ("remote", (("public", 12), ("sales", 7)))
            )
        connect.assert_called_once_with(**settings.connection_kwargs())
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(statements[0], "SET TRANSACTION READ ONLY")
        self.assertFalse(any("CREATE " in s or "ANALYZE" in s for s in statements))

    def test_service_install_is_explicit_and_whitelisted(self):
        with patch("psycopg.connect") as connect, self.assertRaises(ValueError):
            prepare_service_schema(DatabaseSettings())
        connect.assert_not_called()
        self.assertEqual(len(SERVICE_SCRIPTS), 12)
        self.assertTrue(
            all(
                not any(
                    x in name
                    for x in ("aviation", "retail", "logistics", "pagila", "seed")
                )
                for name in SERVICE_SCRIPTS
            )
        )

    def test_unrecognized_service_schema_not_overwritten(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (True, False)
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            with self.assertRaisesRegex(ValueError, "unrecognized"):
                prepare_service_schema(DatabaseSettings(), confirmed=True)
        self.assertEqual(connection.execute.call_count, 2)

    def test_create_service_needs_confirmation_and_never_replaces_existing(self):
        with patch("psycopg.connect") as connect, self.assertRaises(ValueError):
            create_service_database(DatabaseSettings())
        connect.assert_not_called()
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (1,)
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            create_service_database(
                DatabaseSettings(dbname="pqo_service"), confirmed=True
            )
        self.assertEqual(connection.execute.call_count, 2)
        self.assertEqual(connect.call_args.kwargs["dbname"], "postgres")
