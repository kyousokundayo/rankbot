"""環境変数から読む数値設定のfail-closed検証。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import config


class PositiveFloatEnvTest(unittest.TestCase):
    def test_requires_a_finite_positive_number(self) -> None:
        name = "WEREWOLF_TEST_POSITIVE_FLOAT"
        for invalid in ("0", "-1", "nan", "inf", "-inf", "not-a-number"):
            with self.subTest(invalid=invalid), patch.dict(
                os.environ, {name: invalid}
            ):
                with self.assertRaises(RuntimeError):
                    config._parse_positive_float_env(name, 0.7)

    def test_accepts_finite_value_and_uses_default_when_absent(self) -> None:
        name = "WEREWOLF_TEST_POSITIVE_FLOAT"
        with patch.dict(os.environ, {name: "1.25"}):
            self.assertEqual(config._parse_positive_float_env(name, 0.7), 1.25)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(name, None)
            self.assertEqual(config._parse_positive_float_env(name, 0.7), 0.7)

    def test_optional_realistic_maximum_rejects_effectively_infinite_delay(self) -> None:
        name = "WEREWOLF_TEST_POSITIVE_FLOAT"
        with patch.dict(os.environ, {name: "1e300"}):
            with self.assertRaisesRegex(RuntimeError, "60以下"):
                config._parse_positive_float_env(name, 0.7, maximum=60.0)
        with patch.dict(os.environ, {name: "60"}):
            self.assertEqual(
                config._parse_positive_float_env(name, 0.7, maximum=60.0),
                60.0,
            )


if __name__ == "__main__":
    unittest.main()
