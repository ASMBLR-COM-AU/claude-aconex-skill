"""Loads the skill's script (aconex/scripts/aconex.py) as `aconex_skill`
and keeps every test away from real keys. Everything here is offline."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "aconex" / "scripts" / "aconex.py"
_spec = importlib.util.spec_from_file_location("aconex_skill", _SCRIPT)
aconex = importlib.util.module_from_spec(_spec)
sys.modules["aconex_skill"] = aconex  # dataclasses look the module up here
_spec.loader.exec_module(aconex)

_KEY_VARS = {"ACONEX_KEY_FILE", "ACONEX_CLIENT_ID", "ACONEX_CLIENT_SECRET"} | {
    name for names in aconex.SETTING_NAMES.values() for name in names
}


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch, tmp_path):
    for name in _KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(aconex, "DEFAULT_KEY_FILE", tmp_path / "home.aconex.env")
    monkeypatch.setattr(aconex, "BUNDLED_KEY_FILE", tmp_path / "bundled.aconex.env")
