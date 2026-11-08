"""Tests para helpers de identidad."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cogs.identity import IdentityCog


class TestBuildNickname:
    def test_no_prefix(self):
        assert IdentityCog._build_nickname("", "Kaizer") == "Kaizer"

    def test_with_prefix(self):
        assert IdentityCog._build_nickname("[LH]", "Kaizer") == "[LH] Kaizer"

    def test_with_long_name(self):
        name = "KaizerenriqueElEternoYLegendario"
        result = IdentityCog._build_nickname("[LH]", name)
        assert len(result) <= 32

    def test_prefix_too_long(self):
        result = IdentityCog._build_nickname("[SUPERLONG]", "X", max_len=8)
        assert len(result) <= 8

    def test_exact_32_chars(self):
        name = "A" * 26
        assert len(IdentityCog._build_nickname("[ABC]", name)) == 32

    def test_33_chars_truncates(self):
        name = "A" * 27
        result = IdentityCog._build_nickname("[ABC]", name)
        assert len(result) == 32
        assert result == f"[ABC] {'A' * 26}"