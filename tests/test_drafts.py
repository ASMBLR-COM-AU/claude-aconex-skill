"""Drafts: what gets sent to Aconex, and the human-in-the-loop guarantees.
The script may save drafts but must never be able to send one, and the
draft commands must only preview until a person approves (--save)."""

import base64
import email
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import aconex_skill as aconex

_SCRIPT = Path(aconex.__file__)

_SCHEMA = b"""<MailSchema><EntityCreationSchemaFields>
  <MultiValueSchemaField><Identifier>ResponseRequired</Identifier><SchemaValues>
    <SchemaValue><Id>10</Id></SchemaValue></SchemaValues></MultiValueSchemaField>
  <MultiValueSchemaField><Identifier>MailTypeId</Identifier><SchemaValues>
    <SchemaValue><Id>2154</Id><Value>General Correspondence</Value></SchemaValue>
    <SchemaValue><Id>23</Id><Value>Transmittal</Value></SchemaValue>
  </SchemaValues></MultiValueSchemaField>
  <MultiValueSchemaField><Identifier>ReasonForIssue</Identifier><SchemaValues>
    <SchemaValue><Id>2</Id><Value>Issued for Construction</Value></SchemaValue>
  </SchemaValues></MultiValueSchemaField>
</EntityCreationSchemaFields></MailSchema>"""

_DIRECTORY = b"""<DirectorySearch TotalResults="3"><SearchResults>
  <Directory><OrganizationName>Acme Builders</OrganizationName><UserId>u-1</UserId>
    <UserName>Jordan Smith</UserName></Directory>
  <Directory><OrganizationName>Acme Builders</OrganizationName><UserId>u-2</UserId>
    <UserName>Jordan Smith</UserName></Directory>
  <Directory><OrganizationName>Northside Engineering</OrganizationName><UserId>u-3</UserId>
    <UserName>Sam Lee</UserName><JobTitle>Engineer</JobTitle></Directory>
</SearchResults></DirectorySearch>"""

_ORIGINAL = b"""<Mail><MailNo>TX-8</MailNo><Subject>Transmittal Update</Subject>
  <FromUserDetails><Name>Sam Lee</Name><UserId>u-3</UserId></FromUserDetails>
  <ToUsers><Recipient><DistributionType>TO</DistributionType><Name>Jordan Smith</Name>
    <UserId>u-1</UserId></Recipient></ToUsers></Mail>"""


def _draft(**kw):
    values = dict(type_id="2154", subject="Hello & welcome", body_html="<p>Hi</p>",
                  to=["u-1"], cc=[], bcc=[], doc_ids=[], files=[])
    values.update(kw)
    return aconex.Draft(**values)


def _parts(body: bytes, boundary: str):
    msg = email.message_from_bytes(
        f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'.encode() + body)
    return msg.get_payload()


def test_draft_body_is_one_v3_xml_part_then_base64_files():
    body = aconex.build_draft_body(
        _draft(cc=["u-2"], doc_ids=["doc-9"], files=[("a/b;c.pdf", b"%PDF-1.7")],
               reason_id="2", replace_id="draft-1", body_html="<p>x ]]> y</p>"),
        "BOUNDARY")
    xml_part, file_part = _parts(body, "BOUNDARY")
    assert xml_part.get_content_type() == "application/vnd.aconex.mail.v3+xml"
    mail = ET.fromstring(xml_part.get_payload())
    # Fields go in the schema's alphabetical order.
    assert [el.tag for el in mail] == [
        "AttachedDocId", "CcUserId", "ExistingMailId", "MailBody", "MailSubject",
        "MailTypeId", "ReasonForIssue", "RichMailText", "TotalAttachmentCount", "ToUserId",
    ]
    assert mail.findtext("MailBody") == "<p>x ]]> y</p>"  # CDATA survives "]]>"
    assert mail.findtext("MailSubject") == "Hello & welcome"
    assert mail.findtext("TotalAttachmentCount") == "2"  # register docs + files
    assert file_part["X-Filename"] == "bc.pdf"
    assert base64.b64decode(file_part.get_payload()) == b"%PDF-1.7"


def test_plain_text_bodies_become_escaped_paragraphs():
    assert aconex.text_to_html("Hi Sam,\n\nSee <attached>.\nThanks") == (
        "<p>Hi Sam,</p><p>See &lt;attached&gt;.<br/>Thanks</p>")


class _Response:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.content


def _client(monkeypatch, reply=b'<SendMailResult sendStatus="Drafted"><NewMailId>m-9</NewMailId></SendMailResult>'):
    client = aconex.Aconex(aconex.Keys("eDp5", "", "https://aconex.test/api", "https://login.test", "test"))
    calls = []

    def fake_open(path, params=None, accept=None, data=None, content_type=""):
        calls.append({"path": path, "params": params, "data": data, "content_type": content_type})
        return _Response(reply)

    monkeypatch.setattr(client, "open", fake_open)
    return client, calls


@pytest.mark.parametrize("reply_to, path", [("", "/projects/p-1/mail"),
                                            ("m-1", "/projects/p-1/mail/m-1/reply")])
def test_saving_always_asks_aconex_for_a_draft(monkeypatch, reply_to, path):
    client, calls = _client(monkeypatch)
    assert client.save_draft("p-1", b"body", "B", reply_to=reply_to) == "m-9"
    (call,) = calls
    assert call["path"] == path
    assert call["params"] == {"is_draft": "true"}
    assert call["content_type"] == 'multipart/mixed; boundary="B"'


def test_anything_but_a_confirmed_draft_is_an_error(monkeypatch):
    client, _ = _client(monkeypatch, reply=b'<SendMailResult sendStatus="Sent"><NewMailId>m-9</NewMailId></SendMailResult>')
    with pytest.raises(aconex.AconexError, match="didn't confirm a draft"):
        client.save_draft("p-1", b"body", "B")


def test_the_script_has_no_way_to_send_mail():
    source = _SCRIPT.read_text(encoding="utf-8")
    # The one write, save_draft, fixes is_draft=true; nothing else posts.
    assert source.count('"is_draft": "true"') == 1
    assert "is_draft=false" not in source and '"is_draft": "false"' not in source
    assert source.count("get_xml_post(") == 2  # its definition and save_draft's call
    assert not re.search(r"def \w*send\w*\(", source)


class _FakeAconex:
    """Stands in for the client in the command tests; records saves."""
    saved = []

    def __init__(self, keys, raw=False):
        self.keys = keys

    def get_xml(self, path, params=None, accept=None):
        if path.endswith("/mail/schema"):
            return ET.fromstring(_SCHEMA)
        if path.endswith("/directory"):
            return ET.fromstring(_DIRECTORY)
        if path.endswith("/mail/m-9"):
            return ET.fromstring(b"<Mail><MailNo>DRAFT-000009</MailNo></Mail>")
        if path.endswith("/mail/m-8"):
            return ET.fromstring(b"""<Mail><MailNo>DRAFT-8</MailNo><Subject>Old</Subject>
              <Attachments><LocalFileAttachment attachmentId="att-1">
                <FileName>confidential-bid.pdf</FileName></LocalFileAttachment></Attachments></Mail>""")
        if path.endswith("/metadata"):
            return ET.fromstring(b"""<RegisterDocument DocumentId="doc-9">
              <DocumentNumber>S-1100</DocumentNumber><Revision>C1</Revision>
              <Title>LEVEL 02 PLAN</Title></RegisterDocument>""")
        return ET.fromstring(_ORIGINAL)

    def save_draft(self, project, body, boundary, reply_to=""):
        self.saved.append({"project": project, "body": body, "boundary": boundary,
                           "reply_to": reply_to})
        return "m-9"


@pytest.fixture
def fake(monkeypatch):
    _FakeAconex.saved = []
    monkeypatch.setattr(aconex, "Aconex", _FakeAconex)
    monkeypatch.setenv("ACONEX_CREDENTIALS_B64", "eDp5")
    return _FakeAconex


def _to_ids(saved):
    (xml_part, *_) = _parts(saved["body"], saved["boundary"])
    mail = ET.fromstring(xml_part.get_payload())
    return ([e.text for e in mail.iter("ToUserId")], [e.text for e in mail.iter("CcUserId")],
            mail.findtext("MailSubject"))


def test_draft_only_previews_until_the_user_approves(fake, capsys):
    args = ["draft", "p-1", "--to", "Sam Lee", "--subject", "Slab pour",
            "--type", "general", "--body", "Hi Sam"]
    assert aconex.main(args) == 0
    out = capsys.readouterr().out
    assert "PREVIEW - nothing has been saved" in out and "To: Sam Lee (Northside Engineering)" in out
    assert fake.saved == []
    code = re.search(r"--save --confirm (\w+)", out).group(1)

    assert aconex.main(args + ["--save", "--confirm", code]) == 0
    out = capsys.readouterr().out
    assert "Saved as DRAFT-000009" in out and "NOT been sent" in out
    (saved,) = fake.saved
    assert _to_ids(saved) == (["u-3"], [], "Slab pour")


def _approved(args, capsys):
    """`args` as a person has approved them: with --save and its preview's code."""
    assert aconex.main(args) == 0
    code = re.search(r"--save --confirm (\w+)", capsys.readouterr().out).group(1)
    return args + ["--save", "--confirm", code]


_NOTE = ["draft", "p-1", "--to", "u-3", "--subject", "Slab pour", "--type", "2154", "--body", "Hi"]


@pytest.mark.parametrize("save", [[], ["--confirm", "00000000"]])
def test_saving_needs_the_code_from_a_preview(fake, capsys, save):
    assert aconex.main(_NOTE + ["--save"] + save) == 1
    captured = capsys.readouterr()
    assert "Nothing saved" in captured.err and "PREVIEW - nothing has been saved" in captured.out
    assert fake.saved == []


@pytest.mark.parametrize("old, new", [
    ("Hi", "Hi, and wire the funds"),
    ("u-3", "u-1"),
    ("Slab pour", "Slab pour tomorrow"),
    ("2154", "23"),
])
def test_a_code_only_works_for_the_draft_it_was_previewed_for(fake, capsys, old, new):
    *_, code = _approved(_NOTE, capsys)
    changed = [new if part == old else part for part in _NOTE]
    assert aconex.main(changed + ["--save", "--confirm", code]) == 1
    assert "Nothing saved" in capsys.readouterr().err
    assert fake.saved == []


def test_a_code_covers_extra_recipients_attachments_and_replacing(fake, capsys, tmp_path):
    *_, code = _approved(_NOTE, capsys)
    attachment = tmp_path / "notes.pdf"
    attachment.write_bytes(b"%PDF-1.7")
    for extra in (["--cc", "u-1"], ["--bcc", "u-1"], ["--attach", str(attachment)], ["--replace", "m-7"]):
        assert aconex.main(_NOTE + extra + ["--save", "--confirm", code]) == 1
        assert "Nothing saved" in capsys.readouterr().err
    assert fake.saved == []


def test_a_code_covers_what_an_attached_file_holds(fake, capsys, tmp_path):
    attachment = tmp_path / "notes.pdf"
    attachment.write_bytes(b"%PDF-1.7 first")
    args = _NOTE + ["--attach", str(attachment)]
    *_, code = _approved(args, capsys)
    attachment.write_bytes(b"%PDF-1.7 swapped")
    assert aconex.main(args + ["--save", "--confirm", code]) == 1
    assert fake.saved == []


def test_reply_defaults_to_the_sender_and_re_subject(fake, capsys):
    assert aconex.main(_approved(["draft-reply", "p-1", "m-1", "--type", "2154",
                                  "--body", "Thanks"], capsys)) == 0
    (saved,) = fake.saved
    assert saved["reply_to"] == "m-1"
    assert _to_ids(saved) == (["u-3"], [], "RE: Transmittal Update")


def test_a_code_for_one_mail_doesnt_approve_a_reply_to_another(fake, capsys):
    *_, code = _approved(["draft-reply", "p-1", "m-1", "--type", "2154", "--body", "Thanks"], capsys)
    assert aconex.main(["draft-reply", "p-1", "m-2", "--type", "2154", "--body", "Thanks",
                        "--save", "--confirm", code]) == 1
    assert fake.saved == []


def test_reply_all_adds_everyone_the_mail_went_to(fake, capsys):
    assert aconex.main(_approved(["draft-reply", "p-1", "m-1", "--all", "--type", "2154",
                                  "--body", "Thanks"], capsys)) == 0
    assert _to_ids(fake.saved[0])[0] == ["u-3", "u-1"]


@pytest.mark.parametrize("extra, message", [
    (["--to", "Jordan Smith"], "2 people match"),
    (["--to", "Nobody Here"], "no one in this project's directory"),
    (["--to", "u-3", "--type", "memo"], "matches none"),
    (["--to", "u-3", "--attach-doc", "doc-9"], "only attaches register documents to Transmittal"),
    (["--to", "u-3", "--type", "Transmittal", "--attach", "notes.pdf"], "doesn't allow local files"),
])
def test_unclear_drafts_stop_with_a_reason_and_save_nothing(fake, capsys, extra, message):
    args = ["draft", "p-1", "--subject", "s", "--type", "General Correspondence",
            "--body", "b"] + extra + ["--save"]
    assert aconex.main(args) == 1
    assert message in capsys.readouterr().err
    assert fake.saved == []


def test_register_documents_go_on_transmittals(fake, capsys):
    assert aconex.main(_approved(["draft", "p-1", "--to", "u-3", "--subject", "IFC", "--type",
                                  "Transmittal", "--reason", "construction", "--attach-doc", "doc-9",
                                  "--body", "Attached."], capsys)) == 0
    (xml_part, *_) = _parts(fake.saved[0]["body"], fake.saved[0]["boundary"])
    mail = ET.fromstring(xml_part.get_payload())
    assert (mail.findtext("AttachedDocId"), mail.findtext("MailTypeId"),
            mail.findtext("ReasonForIssue")) == ("doc-9", "23", "2")


def test_preview_names_the_register_documents_it_attaches(fake, capsys):
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "IFC", "--type", "Transmittal",
                        "--attach-doc", "doc-9", "--body", "Attached."]) == 0
    assert "S-1100 rev C1 LEVEL 02 PLAN (DOCUMENT_ID doc-9)" in capsys.readouterr().out
    assert fake.saved == []


def test_preview_says_what_a_replaced_draft_holds_now(fake, capsys):
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "New", "--type", "2154",
                        "--body", "b", "--replace", "m-7"]) == 0
    assert ("Replaces TX-8 (MAIL_ID m-7), now to Jordan Smith: Transmittal Update"
            in capsys.readouterr().out)
    assert fake.saved == []


def test_preview_shows_the_attachments_a_replaced_draft_keeps(fake, capsys):
    assert aconex.main(_NOTE + ["--replace", "m-8"]) == 0
    assert "already on the draft, and stays on it: confidential-bid.pdf" in capsys.readouterr().out


def test_a_code_covers_the_attachments_a_replaced_draft_keeps():
    draft = _draft(replace_id="m-8")
    assert aconex.approval_code("p-1", "", draft, ["a.pdf"]) != aconex.approval_code("p-1", "", draft, [])


def test_a_message_is_plain_text_so_a_link_cant_hide_in_the_preview(fake, capsys):
    with pytest.raises(SystemExit):
        aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                     "--body", "<a href='https://evil.example'>the drawing</a>", "--html"])
    capsys.readouterr()
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                        "--body", "<a href='https://evil.example'>the drawing</a>"]) == 0
    assert "<a href='https://evil.example'>the drawing</a>" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["aconex.env", ".env", ".env.local", ".aconex-work.env"])
@pytest.mark.parametrize("option", ["--body-file", "--attach"])
def test_a_key_file_never_reaches_a_preview_or_a_draft(fake, capsys, tmp_path, option, name):
    key_file = tmp_path / name
    key_file.write_text("ACONEX_CLIENT_SECRET=the-secret\n")
    body = ["--body", "b"] if option == "--attach" else []
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                        option, str(key_file), *body, "--save"]) == 1
    captured = capsys.readouterr()
    assert "looks like a key file" in captured.err
    assert "the-secret" not in captured.out + captured.err
    assert fake.saved == []


@pytest.mark.parametrize("name", ["aconex.env.", "aconex.env ", "aconex.env::$DATA", "ACONEX.ENV"])
@pytest.mark.parametrize("option", ["--body-file", "--attach"])
def test_a_key_file_cant_slip_through_on_how_its_name_is_written(fake, capsys, tmp_path, option, name):
    key_file = tmp_path / "aconex.env"
    key_file.write_text("ACONEX_CLIENT_SECRET=the-secret\n")
    body = ["--body", "b"] if option == "--attach" else []
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                        option, str(tmp_path) + "/" + name, *body, "--save"]) == 1
    captured = capsys.readouterr()
    assert "the-secret" not in captured.out + captured.err
    assert fake.saved == []


@pytest.mark.parametrize("option", ["--body-file", "--attach"])
@pytest.mark.parametrize("content", ["ACONEX_CLIENT_SECRET=the-secret-value",
                                     'ACONEX_CREDENTIALS_B64="ZXhhbXBsZTpleGFtcGxl"',
                                     "ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64 = ZXhhbXBsZTpleGFtcGxl"])
def test_keys_copied_into_another_file_never_reach_a_draft(fake, capsys, tmp_path, option, content):
    copy = tmp_path / "notes.txt"
    copy.write_text(f"Some notes\n{content}\n")
    body = ["--body", "b"] if option == "--attach" else []
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                        option, str(copy), *body, "--save"]) == 1
    captured = capsys.readouterr()
    assert "has Aconex keys in it" in captured.err
    assert "ZXhhbXBs" not in captured.out + captured.err and "the-secret" not in captured.out
    assert fake.saved == []


def test_nothing_from_where_tools_keep_keys_reaches_a_draft(fake, capsys, tmp_path):
    secret = tmp_path / ".ssh" / "id_rsa"
    secret.parent.mkdir()
    secret.write_text("the-secret")
    assert aconex.main(["draft", "p-1", "--to", "u-3", "--subject", "s", "--type", "2154",
                        "--attach", str(secret), "--body", "b", "--save"]) == 1
    assert "Won't use" in capsys.readouterr().err
    assert fake.saved == []


def test_schema_values_skip_unnamed_entries_and_pick_by_name_or_id():
    types = aconex.parse_schema_values(ET.fromstring(_SCHEMA), "MailTypeId")
    assert types == {"General Correspondence": "2154", "Transmittal": "23"}
    assert aconex.pick(types, "23", "Mail type") == ("23", "Transmittal")
    assert aconex.pick(types, "general", "Mail type") == ("2154", "General Correspondence")
