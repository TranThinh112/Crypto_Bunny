from __future__ import annotations

import os
from copy import deepcopy
from unittest import TestCase

from crypto_trader import atlas_mirror
from crypto_trader.config import DEFAULT_CONFIG
from crypto_trader.storage import get_journal_state, set_journal_state


class AtlasMirrorTest(TestCase):
    def setUp(self) -> None:
        atlas_mirror._ATLAS_CLIENTS.clear()

    def _config(self, *, role: str = "primary") -> dict:
        config = deepcopy(DEFAULT_CONFIG)
        config["_atlas_test_mode"] = True
        config["runtime"]["instance_role"] = role
        config["database"]["backend"] = "atlas"
        config["database"]["atlas"]["database"] = "atlas_test_suite"
        return config

    def test_atlas_database_reuses_cached_database(self) -> None:
        config = self._config()

        first = atlas_mirror.atlas_database(config)
        second = atlas_mirror.atlas_database(config)

        self.assertIs(first, second)

    def test_primary_can_write_and_secondary_is_blocked(self) -> None:
        primary = self._config(role="primary")
        secondary = self._config(role="secondary")

        set_journal_state(primary, "alpha", "1")
        self.assertEqual(get_journal_state(primary, "alpha"), "1")

        with self.assertRaises(RuntimeError):
            set_journal_state(secondary, "beta", "2")

    def test_runtime_mode_helpers(self) -> None:
        primary = self._config(role="primary")
        secondary = self._config(role="secondary")

        self.assertTrue(atlas_mirror.atlas_backend_enabled(primary))
        self.assertTrue(atlas_mirror.atlas_runtime_is_primary(primary))
        self.assertFalse(atlas_mirror.atlas_runtime_is_read_only(primary))
        self.assertTrue(atlas_mirror.atlas_runtime_is_read_only(secondary))

    def test_env_requirements_keep_expected_names(self) -> None:
        config = self._config()
        self.assertEqual(atlas_mirror.atlas_env_requirements(config), ("MONGODB_URI", "MONGODB_DATABASE"))
