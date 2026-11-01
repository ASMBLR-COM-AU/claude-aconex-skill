"""The setup page: it's up to date, ships nothing private, and the zip its own
JavaScript builds is one the skill can read keys from."""

import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import aconex_skill as aconex

_ROOT = Path(__file__).resolve().parents[1]
_PAGE = _ROOT / "docs" / "index.html"

_spec = importlib.util.spec_from_file_location("setup_build", _ROOT / "setup" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _script() -> str:
    return re.search(r"<script>(.*)</script>", _PAGE.read_text(encoding="utf-8"), re.S).group(1)


def _run_page(expression: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    # Through stdin: the script is too long for a Windows command line.
    code = _script() + f"\nprocess.stdout.write(String({expression}));"
    return subprocess.run([node, "-"], input=code, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


def test_page_is_up_to_date_with_the_skill():
    page = _PAGE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert page == build.build_page().replace("\r\n", "\n"), "run: python setup/build.py"


def test_github_pages_serves_the_page_untouched():
    # Without this file, GitHub Pages runs the folder through Jekyll first.
    assert (_ROOT / "docs" / ".nojekyll").exists()


def test_page_carries_the_skill_and_nothing_private():
    names = {name for name, _ in build.skill_files()}
    assert {"aconex/SKILL.md", "aconex/scripts/aconex.py"} <= names
    assert not [n for n in names if n.endswith(".env") or "references/api" in n
                or "__pycache__" in n]


def test_page_gives_browsers_nothing_to_offer_to_save():
    # No password field and no form being submitted, so no save-password prompt.
    page = _PAGE.read_text(encoding="utf-8")
    assert 'type="password"' not in page and "<form" not in page
    assert '<input id="credentials" class="masked"' in page
    assert '<input id="client-secret" class="masked"' in page


def test_page_blocks_network_access_and_loads_nothing_external():
    page = _PAGE.read_text(encoding="utf-8")
    assert "default-src 'none'" in page
    assert "connect-src" not in page
    assert not re.search(r"""<(?:script|link|img|iframe)[^>]*(?:src|href)=["']https?:""", page)


@pytest.mark.parametrize("secret", ["client-secret", "s:with:colons", 'we"ird#se=cret\''])
def test_zip_built_by_the_page_gives_the_skill_working_keys(tmp_path, secret):
    credentials = base64.b64encode(f"client-id:{secret}".encode()).decode()
    args = json.dumps(["user@example.test", credentials])[1:-1]
    b64 = _run_page(f"Buffer.from(buildSkillZip({args})).toString('base64')")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as z:
        assert z.testzip() is None
        z.extractall(tmp_path)
        names = set(z.namelist())
    assert names == {name for name, _ in build.skill_files()} | {"aconex/aconex.env"}

    key_file = tmp_path / "aconex" / "aconex.env"
    keys = aconex.keys_from_values(aconex.read_key_file(key_file), "test")
    assert keys.credentials_b64 == credentials
    assert keys.username == "user@example.test"
    assert (tmp_path / "aconex" / "SKILL.md").read_bytes().startswith(b"---")


_GOOD = base64.b64encode(b"client-id:client-secret").decode()


def test_zip_can_name_which_aconex_account_to_use(tmp_path):
    args = json.dumps(["user@example.test", _GOOD, "123", "https://au1.aconex.com"])[1:-1]
    b64 = _run_page(f"Buffer.from(buildSkillZip({args})).toString('base64')")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as z:
        z.extractall(tmp_path)
    keys = aconex.keys_from_values(aconex.read_key_file(tmp_path / "aconex" / "aconex.env"), "test")
    assert (keys.user_id, keys.user_site) == ("123", "https://au1.aconex.com")


@pytest.mark.parametrize("user_id, user_site, message", [
    ("123", "", "both"),
    ("", "https://au1.aconex.com", "both"),
    ("12a", "https://au1.aconex.com", "only have numbers"),
    ("123", "au1.aconex.com", "should look like"),
    ("123", "https://evil.example", "should look like"),
    ("123", "https://au1.aconex.com.evil.io", "should look like"),
    ("123", 'https://au1.aconex.com"\nACONEX_TOKEN_URL="https://evil.example', "should look like"),
])
def test_page_catches_an_aconex_account_that_cant_be_right(user_id, user_site, message):
    args = json.dumps(["user@example.test", _GOOD, user_id, user_site])[1:-1]
    assert message in _run_page(f"keyProblem({args})")


def test_page_accepts_a_good_aconex_account():
    args = json.dumps(["user@example.test", _GOOD, "123", "https://uk1.aconex.co.uk"])[1:-1]
    assert _run_page(f"keyProblem({args})") == ""


@pytest.mark.parametrize("username, credentials, message", [
    ("", _GOOD, "Aconex email"),
    ("user", "", "base64 credentials"),
    ("has space", _GOOD, "email has a space"),
    ("has\x1ccontrol", _GOOD, "email has a space"),
    ("has\x85control", _GOOD, "email has a space"),
    ("user", "not base64!", "aren't valid base64"),
    ("user", _GOOD[:-1], "aren't valid base64"),
    ("user", base64.b64encode(b"no-colon").decode(), "aren't valid base64"),
    ("user", base64.b64encode(b"id:").decode(), "aren't valid base64"),
])
def test_page_catches_keys_that_cant_be_right(username, credentials, message):
    args = json.dumps([username, credentials])[1:-1]
    assert message in _run_page(f"keyProblem({args})")


@pytest.mark.parametrize("secret", ["client-secret", "s:with:colons", "ünï-cödé"])
def test_converter_matches_what_the_script_builds(monkeypatch, secret):
    args = json.dumps(["client-id", secret])[1:-1]
    monkeypatch.setenv("ACONEX_CLIENT_ID", "client-id")
    monkeypatch.setenv("ACONEX_CLIENT_SECRET", secret)
    assert _run_page(f"encodeCredentials({args})") == aconex.load_keys().credentials_b64


@pytest.mark.parametrize("client_id, secret, message", [
    ("", "s", "client ID"),
    ("c", "", "client secret"),
    ("has:colon", "s", "space or colon"),
    ("c", "has space", "client secret has a space"),
    ("same", "same", "the same"),
])
def test_converter_catches_values_that_cant_be_right(client_id, secret, message):
    args = json.dumps([client_id, secret])[1:-1]
    assert message in _run_page(f"clientProblem({args})")


def test_page_accepts_good_keys():
    args = json.dumps(["user@example.test", _GOOD])[1:-1]
    assert _run_page(f"keyProblem({args})") == ""
