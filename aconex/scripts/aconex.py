#!/usr/bin/env python3
"""Aconex lookups and human-approved drafts for the `aconex` Claude skill.

Searches a project's document register, mail and directory, shows a
document's revision history, reads mail in full (body, To/Cc/Bcc,
attachments), downloads files, and saves mail drafts, all as whichever
Aconex user the keys belong to.

Drafts are the only thing this script writes, and it cannot send them.
Every write goes through `Aconex.save_draft`, which always asks Aconex for
a draft (`is_draft=true`); there is no send path to call. A person opens
the draft in Aconex, checks it and presses Send. On top of that, the draft
commands only preview unless given --save, so the person can approve what
is saved before anything reaches Aconex.

Python standard library only, so it runs as-is wherever Python 3.8+ does
(Claude Code, Cowork) with nothing to install.

Keys are looked for in this order; the first place that has them wins:
  1. --key-file PATH (or ACONEX_KEY_FILE=PATH)
  2. environment variables: ACONEX_CLIENT_ID + ACONEX_CLIENT_SECRET, or
     ACONEX_CREDENTIALS_B64 (base64 of client_id:secret)
  3. ~/.aconex.env
  4. aconex.env in the skill's own folder (a personal skill made with the
     setup page)
  5. none: log in without sending keys, which works when Claude Code on
     the web holds them as a network secret on the login server
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit

# Aconex's shared API host. A USERBOUND token is issued for this host, so
# API calls go here even when the Aconex website is a regional one like
# au1.aconex.com. Override with ACONEX_API_URL.
DEFAULT_BASE_URL = "https://api.aconex.com/api"
DEFAULT_TOKEN_ENDPOINT = (
    "https://constructionandengineering.oraclecloud.com/auth/token"
)
DEFAULT_KEY_FILE = Path.home() / ".aconex.env"
BUNDLED_KEY_FILE = Path(__file__).resolve().parents[1] / "aconex.env"

# Each setting's accepted names, preferred name first. The ACONEX_USERBOUND_*
# names are the ones Aconex integrations commonly use already.
SETTING_NAMES = {
    "credentials_b64": ("ACONEX_CREDENTIALS_B64", "ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64"),
    "username": ("ACONEX_USERNAME", "ACONEX_USERBOUND_LOBBY_USERNAME"),
    "base_url": ("ACONEX_API_URL", "ACONEX_USERBOUND_BASE_URL"),
    "token_endpoint": ("ACONEX_TOKEN_URL", "ACONEX_USERBOUND_TOKEN_ENDPOINT"),
    "user_id": ("ACONEX_USER_ID",),
    "user_site": ("ACONEX_USER_SITE",),
}

ACCEPT_XML = "application/xml"
ACCEPT_MAIL = "application/vnd.aconex.mail.v2+xml"
ACCEPT_FILE = "application/octet-stream"
MAIL_V3_PART = "application/vnd.aconex.mail.v3+xml"  # the XML part of a draft

REGISTER_FIELDS = (
    "docno,title,revision,statusid,doctype,discipline,revisiondate,filename"
)
# Mail's number field is `docno` too (it comes back as <MailNo>); `mailno`
# is rejected.
MAIL_FIELDS = "docno,subject,sentdate,corrtypeid,fromUserDetails"
MAIL_BOXES = {"inbox": "Inbox", "sent": "Sentbox", "all": "Allbox", "drafts": "Draftbox"}
# The register schema marks title as not sortable (sorting by it is a 400).
DOC_SORTS = {"number": "docno", "date": "revisiondate"}
# PAGED searches only accept page sizes that are multiples of 25, up to 500.
PAGE_STEP, PAGE_MAX = 25, 500
ATTACHMENT_KINDS = {
    "RegisteredDocumentAttachment": "register",
    "LocalFileAttachment": "file",
    "FileAttachment": "file",
    "MailAttachment": "mail",
}
# Mail detail lists every recipient under <ToUsers>; each one's
# <DistributionType> says whether it is TO, CC or BCC.
DISTRIBUTION_TYPES = ("TO", "CC", "BCC")

TIMEOUT_S = 180  # a mail with thousands of attachments takes ~50 s to serve
THROTTLE_WAITS_S = (5, 15)  # Aconex answers 503 while throttling
MAX_BODY_CHARS = 20_000

KEY_FILE_TEMPLATE = """\
# Aconex keys for Claude's "aconex" skill.
#
# Fill in the credentials of your Aconex API client (a USERBOUND client,
# bound to the Aconex user Claude should act as) as one base64 value of
# client_id:secret, and the email that user signs in to Aconex with. Save this
# file, then ask Claude to check the connection. Keep this file private: don't share it,
# commit it to git, or paste what's in it into a chat.

ACONEX_CREDENTIALS_B64=
ACONEX_USERNAME=

# Or, if you have the client ID and secret separately, put them here instead
# and leave ACONEX_CREDENTIALS_B64 empty:
# ACONEX_CLIENT_ID=
# ACONEX_CLIENT_SECRET=

# Optional: only if your Aconex administrator gives you different servers.
# ACONEX_API_URL=https://api.aconex.com/api
# ACONEX_TOKEN_URL=https://constructionandengineering.oraclecloud.com/auth/token

# Optional: only if your Lobby account is linked to more than one Aconex account.
# ACONEX_USER_ID=
# ACONEX_USER_SITE=https://au1.aconex.com
"""


class AconexError(Exception):
    """A problem to tell the user about; the message says what to do."""


# The only servers the keys and login token may go to: Aconex's own hosts and
# Oracle's Aconex login servers, by exact name. Other oraclecloud.com names
# belong to other Oracle customers, so that domain as a whole isn't trusted.
ACONEX_HOST_SUFFIXES = (".aconex.com", ".aconex.co.uk", ".aconexasia.com")
ORACLE_LOGIN_HOSTS = {
    "constructionandengineering.oraclecloud.com",  # every commercial instance
    "constructionandengineering-au2.oraclecloud.com",  # AU2, Australian government
    "constructionandengineering-ea.oraclecloud.com",  # Early Access
}


def is_aconex_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return (parts.scheme == "https" and bool(re.fullmatch(r"[a-z0-9.-]+", host))
            and (host in ORACLE_LOGIN_HOSTS or host.endswith(ACONEX_HOST_SUFFIXES)))


# ── Keys ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Keys:
    credentials_b64: str  # base64(client_id:secret); "" = network secret
    username: str
    base_url: str
    token_endpoint: str
    source: str
    # Which Aconex account, when a Lobby account is linked to more than one.
    user_id: str = ""
    user_site: str = ""


def read_key_file(path: Path) -> dict:
    """KEY=value lines, as in a .env file; comments and blanks skipped. Only a
    newline ends a line: splitlines() also splits at characters like \\x85,
    which would let a crafted value add a setting of its own."""
    values = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def setting(values, name: str) -> str:
    """One setting, under whichever of its accepted names is filled in."""
    for env_name in SETTING_NAMES[name]:
        value = values.get(env_name, "").strip()
        if value:
            return value
    return ""


def _server(values, name: str, default: str, source: str) -> str:
    """A server address from the settings, which is where the keys will go."""
    url = setting(values, name) or default
    if not is_aconex_url(url):
        raise AconexError(f"{source}: {url} isn't an Aconex server, so the keys won't be sent there.")
    return url


def _keys(values, credentials_b64: str, source: str) -> Keys:
    user_id, user_site = setting(values, "user_id"), setting(values, "user_site").rstrip("/")
    if bool(user_id) != bool(user_site):
        raise AconexError(f"{source}: fill in both ACONEX_USER_ID and ACONEX_USER_SITE.")
    if user_site and not is_aconex_url(user_site):
        raise AconexError(f"{source}: ACONEX_USER_SITE should look like https://au1.aconex.com.")
    return Keys(
        credentials_b64=credentials_b64,
        username=setting(values, "username"),
        base_url=_server(values, "base_url", DEFAULT_BASE_URL, source).rstrip("/"),
        token_endpoint=_server(values, "token_endpoint", DEFAULT_TOKEN_ENDPOINT, source),
        source=source,
        user_id=user_id,
        user_site=user_site,
    )


def keys_from_values(values, source: str) -> Keys | None:
    """Keys from one place (environment or key file), or None if it has none."""
    b64 = setting(values, "credentials_b64")
    client_id = values.get("ACONEX_CLIENT_ID", "").strip()
    secret = values.get("ACONEX_CLIENT_SECRET", "").strip()
    if not b64 and (client_id or secret):
        if not (client_id and secret):
            raise AconexError(
                f"{source}: fill in both ACONEX_CLIENT_ID and ACONEX_CLIENT_SECRET."
            )
        b64 = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    if not b64:
        return None
    try:
        base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise AconexError(
            f"{source}: the combined credentials value isn't valid base64."
        ) from None
    return _keys(values, b64, source)


def profile_key_file(profile: str) -> Path:
    """~/.aconex-NAME.env: one key file per Aconex account (say a live
    account and a test one), picked with --profile NAME or ACONEX_PROFILE."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile):
        raise AconexError("A profile name uses only letters, digits, - and _.")
    return DEFAULT_KEY_FILE.with_name(f".aconex-{profile}.env")


def load_keys(key_file: str | None = None, profile: str | None = None) -> Keys:
    profile = profile or os.environ.get("ACONEX_PROFILE", "")
    if profile and not key_file:
        key_file = str(profile_key_file(profile))
    explicit = key_file or os.environ.get("ACONEX_KEY_FILE", "")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise AconexError(f"Key file not found: {path}")
        keys = keys_from_values(read_key_file(path), f"key file {path}")
        if keys is None:
            raise AconexError(f"{path} has no keys filled in yet.")
        return keys
    keys = keys_from_values(os.environ, "environment variables")
    if keys:
        return keys
    for path in (DEFAULT_KEY_FILE, BUNDLED_KEY_FILE):
        if path.is_file():
            keys = keys_from_values(read_key_file(path), f"key file {path}")
            if keys:
                return keys
    # Nothing local: log in without keys, in case the environment's network
    # proxy adds them (a Claude Code network secret on the login server).
    return _keys(os.environ, "", "network secret")


def _no_keys(code: int) -> str:
    return (
        "No Aconex keys found. Looked for environment variables "
        "ACONEX_CLIENT_ID + ACONEX_CLIENT_SECRET (or ACONEX_CREDENTIALS_B64), "
        "the key file ~/.aconex.env, an aconex.env in the skill's folder, and a "
        f"network secret on the Aconex login server (it refused a keyless login: "
        f"HTTP {code}). See the skill's references/setup.md."
    )


# ── Client ──────────────────────────────────────────────────────────


def _error_text(err: urllib.error.HTTPError) -> str:
    try:
        text = err.read(400).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — the body is only a hint
        text = ""
    finally:
        err.close()
    return " ".join(text.split())[:200] or str(err.reason)


def _http_problem(err: urllib.error.HTTPError, path: str) -> str:
    hint = {
        400: "Aconex didn't accept the request (for a search, check the query)",
        403: "this Aconex user isn't allowed to see that (it may not be on "
             "that project)",
        404: "not found: check the project, document, mail or attachment ID",
        503: "Aconex is throttling requests: wait a minute and try again",
    }.get(err.code, "Aconex returned an error")
    return f"{hint}. HTTP {err.code} for {path}: {_error_text(err)}"


def _unreachable(url: str, reason) -> str:
    host = urlsplit(url).hostname
    return (
        f"Couldn't reach {host} ({reason}). In Cowork or another sandbox, "
        f"{host} has to be on the allowed network domains."
    )


class Aconex:
    """One Aconex account: reads, plus saving drafts. Nothing here sends mail."""

    def __init__(self, keys: Keys, raw: bool = False) -> None:
        self.keys = keys
        self.raw = raw
        self._token: str | None = None

    def _login(self) -> None:
        form = {"grant_type": "client_credentials"}
        if self.keys.user_id:
            form.update(user_id=self.keys.user_id, user_site=self.keys.user_site)
        request = urllib.request.Request(
            self.keys.token_endpoint,
            data=urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        if self.keys.credentials_b64:
            # Unredirected: urllib repeats ordinary headers to whichever host
            # a redirect names, and these keys must only reach this one.
            request.add_unredirected_header("Authorization", f"Basic {self.keys.credentials_b64}")
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                token = json.loads(resp.read().decode("utf-8")).get("access_token")
        except urllib.error.HTTPError as err:
            if not self.keys.credentials_b64:
                err.close()
                raise AconexError(_no_keys(err.code)) from None
            text = _error_text(err)
            if "aconex_account" in text or "user_site" in text:
                raise AconexError(
                    f"Aconex can't tell which account to log in to ({text}). A Lobby account "
                    "linked to more than one Aconex account needs ACONEX_USER_ID and "
                    "ACONEX_USER_SITE: see the skill's references/setup.md."
                ) from None
            raise AconexError(
                f"Aconex refused the login (HTTP {err.code}: {text}). "
                "Check the client ID and secret; the API client may have been "
                "revoked or rotated."
            ) from None
        except urllib.error.URLError as err:
            raise AconexError(_unreachable(self.keys.token_endpoint, err.reason)) from None
        except (ValueError, AttributeError):
            raise AconexError("The Aconex login server sent back something unexpected.") from None
        if not token:
            raise AconexError("The Aconex login server sent back no access token.")
        self._token = token

    def open(self, path: str, params: dict | None = None, accept: str = ACCEPT_XML,
             data: bytes | None = None, content_type: str = ""):
        """GET an API path (or POST `data`) and return the open response.
        Logs in again once on 401, and for GETs waits out Aconex throttling
        (503) before giving up. A POST is never repeated after a 503, so a
        draft can't be saved twice."""
        url = self.keys.base_url + path
        if params:
            url += "?" + urlencode(params)
        return self._request(url, path, accept, data, content_type)

    def open_url(self, url: str, accept: str = ACCEPT_XML):
        """GET a full URL (one the caller has checked is an Aconex host)."""
        return self._request(url, url, accept, None, "")

    def _request(self, url: str, path: str, accept: str, data: bytes | None, content_type: str):
        waits = list(THROTTLE_WAITS_S) if data is None else []
        logged_in_again = False
        while True:
            if self._token is None:
                self._login()
            headers = {"Accept": accept}
            if content_type:
                headers["Content-Type"] = content_type
            request = urllib.request.Request(
                url, data=data, headers=headers, method="GET" if data is None else "POST"
            )
            # Unredirected, as for the keys in _login.
            request.add_unredirected_header("Authorization", f"Bearer {self._token}")
            try:
                return urllib.request.urlopen(request, timeout=TIMEOUT_S)
            except urllib.error.HTTPError as err:
                if err.code == 401 and not logged_in_again:
                    err.close()
                    logged_in_again, self._token = True, None
                    continue
                if err.code == 503 and waits:
                    err.close()
                    time.sleep(waits.pop(0))
                    continue
                raise AconexError(_http_problem(err, path)) from None
            except urllib.error.URLError as err:
                raise AconexError(_unreachable(self.keys.base_url, err.reason)) from None

    def get_xml(self, path: str, params: dict | None = None, accept: str = ACCEPT_XML):
        with self.open(path, params, accept) as resp:
            return self._xml(resp.read())

    def _xml(self, content: bytes):
        if self.raw:
            print(content.decode("utf-8", "replace"), end="\n\n")
        try:
            return ET.fromstring(content)
        except ET.ParseError as err:
            raise AconexError(f"Aconex sent back something that isn't XML ({err}).") from None

    def save_draft(self, project: str, body: bytes, boundary: str,
                   reply_to: str = "") -> str:
        """Save a mail DRAFT and return its MAIL_ID. The only write in this
        script. `is_draft=true` is fixed here and there is no way to send:
        a person must open the draft in Aconex and press Send."""
        path = f"/projects/{_seg(project)}/mail"
        if reply_to:
            path += f"/{_seg(reply_to)}/reply"
        root = self.get_xml_post(path, {"is_draft": "true"}, body,
                                 f'multipart/mixed; boundary="{boundary}"')
        status = root.get("sendStatus", "")
        mail_id = _text(root, "NewMailId")
        if status != "Drafted" or not mail_id:
            raise AconexError(
                f"Aconex didn't confirm a draft (sendStatus={status or 'none'}). "
                "Check Mail > Drafts and Mail > Sent in Aconex."
            )
        return mail_id

    def get_xml_post(self, path: str, params: dict, data: bytes, content_type: str):
        try:
            with self.open(path, params, ACCEPT_MAIL, data, content_type) as resp:
                return self._xml(resp.read())
        except AconexError as err:
            if "HTTP 403" in str(err) or "HTTP 405" in str(err):
                raise AconexError(
                    f"{err} This Aconex user or API client may not be allowed to "
                    "create mail on this project: ask the project's Aconex "
                    "administrator to allow it."
                ) from None
            raise


# ── Parsing (XML shapes as the live API sends them) ─────────────────


# Characters that could steer a terminal, in whatever came back from Aconex.
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _text(el, path: str) -> str:
    return _CONTROL_CHARS.sub("", el.findtext(path) or "").strip()


def _first(el, *paths: str) -> str:
    for path in paths:
        value = _text(el, path)
        if value:
            return value
    return ""


def _person(el) -> str:
    """'Name (Organisation)' from a user-details element."""
    if el is None:
        return ""
    name = _first(el, "Name", "FullName", "UserName")
    org = _text(el, "OrganizationName")
    return f"{name} ({org})" if name and org else name or org


def parse_projects(root) -> list:
    projects = []
    for p in root.iter("Project"):
        project_id = _text(p, "ProjectId")
        if project_id:
            projects.append({
                "id": project_id,
                "name": _text(p, "ProjectName"),
                "code": _first(p, "ProjectCode", "ProjectShortName"),
            })
    return projects


def parse_documents(root) -> list:
    """Register search rows. The live register names its elements
    DocumentNumber, DocumentStatus, DocumentType and Filename; mail
    attachments use DocumentNo and FileName, kept here as fallbacks."""
    return [
        {
            "document_id": (d.get("DocumentId") or _text(d, "DocumentId")).strip(),
            "number": _first(d, "DocumentNumber", "DocumentNo"),
            "revision": _text(d, "Revision"),
            "title": _text(d, "Title"),
            "status": _first(d, "DocumentStatus", "StatusId", "Status"),
            "date": _text(d, "RevisionDate"),
            "discipline": _text(d, "Discipline"),
            "type": _first(d, "DocumentType", "DocType"),
            "file": _first(d, "Filename", "FileName"),
        }
        for d in root.iter("Document")
    ]


def parse_statuses(schema_root) -> dict:
    """{status name: status ID} from the register schema. A status search
    needs the ID: statusid:"For Review" matches nothing, statusid:"358" works."""
    return parse_schema_values(schema_root, "statusid")


def parse_mail_list(root) -> list:
    mails = []
    for m in root.findall(".//SearchResults/Mail"):
        mail_id = (m.get("MailId") or _text(m, "MailId")).strip()
        if mail_id:
            mails.append({
                "mail_id": mail_id,
                "number": _text(m, "MailNo"),
                "sent": _text(m, "SentDate"),
                "type": _first(m, "CorrespondenceType", "CorrespondenceTypeId"),
                "subject": _text(m, "Subject"),
                "from": _person(m.find("FromUserDetails")),
            })
    return mails


def parse_attachment(a) -> dict:
    return {
        "attachment_id": (
            a.get("attachmentId") or a.get("AttachmentId") or _text(a, "AttachmentId")
        ).strip(),
        "kind": ATTACHMENT_KINDS.get(a.tag, a.tag),
        "number": _text(a, "DocumentNo"),
        "revision": _text(a, "Revision"),
        "title": _text(a, "Title"),
        "file": _text(a, "FileName"),
        "size": _text(a, "FileSize"),
        "document_id": _text(a, "DocumentId"),
    }


def parse_recipients(root) -> dict:
    """{"TO": [...], "CC": [...], "BCC": [...]}, each a list of
    {"name": 'Name (Organisation)', "user_id"}. Live mail lists everyone
    under <ToUsers> and marks each recipient's <DistributionType>; separate
    <CcUsers>/<BccUsers> blocks are read too, in case a project sends them."""
    out = {kind: [] for kind in DISTRIBUTION_TYPES}
    for parent, default in (("ToUsers", "TO"), ("CcUsers", "CC"), ("BccUsers", "BCC")):
        for r in root.findall(f".//{parent}/*"):
            kind = _text(r, "DistributionType").upper() or default
            name = _person(r)
            if name:
                out.setdefault(kind, []).append({"name": name, "user_id": _text(r, "UserId")})
    return out


def parse_mail(root, mail_id: str) -> dict:
    body = root.find(".//MailData")
    recipients = parse_recipients(root)
    return {
        "mail_id": mail_id,
        "number": _text(root, ".//MailNo"),
        "type": _text(root, ".//CorrespondenceType"),
        "subject": _text(root, ".//Subject"),
        "sent": _text(root, ".//SentDate"),
        "from": _person(root.find(".//FromUserDetails")),
        "from_user_id": _text(root, ".//FromUserDetails/UserId"),
        "to": recipients["TO"],
        "cc": recipients["CC"],
        "bcc": recipients["BCC"],
        "reason": _text(root, ".//ReasonForIssue"),
        "body": html_to_text("".join(body.itertext())) if body is not None else "",
        "attachments": [parse_attachment(a) for a in root.findall(".//Attachments/*")],
    }


def parse_directory(root) -> list:
    """Project directory: everyone who can be sent mail on the project."""
    return [
        {
            "user_id": _text(d, "UserId"),
            "name": _text(d, "UserName"),
            "org": _first(d, "OrganizationName", "TradingName"),
            "job": _text(d, "JobTitle"),
            "email": _text(d, "Email"),
        }
        for d in root.iter("Directory")
        if _text(d, "UserId")
    ]


def parse_schema_values(schema_root, identifier: str) -> dict:
    """{name: id} for one field of a mail schema (e.g. MailTypeId). The same
    identifier can appear in several sections; the first with named values
    wins. Values listed without a name are skipped."""
    for field in schema_root.iter():
        if _text(field, "Identifier") != identifier:
            continue
        values = {
            _text(v, "Value"): _text(v, "Id")
            for v in field.iter("SchemaValue")
            if _text(v, "Value") and _text(v, "Id")
        }
        if values:
            return values
    return {}


class _HTMLText(HTMLParser):
    _BLOCKS = {"p", "div", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
    _HIDDEN = {"style", "script", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self._hidden += 1
        elif tag in self._BLOCKS or tag == "br":  # <br/> also ends here, once
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Mail bodies are HTML; plain text passes through unchanged."""
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return _CONTROL_CHARS.sub("", re.sub(r"\n{3,}", "\n\n", text)).strip()


# ── Queries ─────────────────────────────────────────────────────────


def lucene_phrase(value: str) -> str:
    """Escape a value for a quoted Lucene phrase: only `"` and `\\`. Escaping
    more breaks matching against the live register (see agent/aconex.py)."""
    return re.sub(r'(["\\])', r"\\\1", value)


def register_query(words=(), number="", title="", discipline="", doc_type="",
                   status="", extra="", content="") -> str:
    """Lucene for the register. Free words match the number or the title. Text
    to find inside files has no field name: a field clause never reaches them."""
    clauses = [
        f'(docno:"{lucene_phrase(w)}" OR title:"{lucene_phrase(w)}")' for w in words
    ]
    if number:
        clauses.append(f'docno:"{lucene_phrase(number)}"')
    clauses += [f'title:"{lucene_phrase(w)}"' for w in title.split()]
    for field, value in (("discipline", discipline), ("doctype", doc_type),
                         ("statusid", status)):
        if value:
            clauses.append(f'{field}:"{lucene_phrase(value)}"')
    if extra:
        clauses.append(f"({extra})")
    if content:
        # Last on purpose: live, `"text" AND docno:"X"` finds nothing, but
        # `docno:"X" AND "text"` finds the document.
        clauses.append(f'"{lucene_phrase(content)}"')
    return " AND ".join(clauses)


def api_page_size(wanted: int) -> int:
    """The smallest page size Aconex accepts that holds `wanted` rows."""
    return min(PAGE_MAX, max(PAGE_STEP, -(-wanted // PAGE_STEP) * PAGE_STEP))


def resolve_status(client: "Aconex", project: str, status: str) -> str:
    """A status ID for the register search, from an ID or a status name."""
    if status.strip().isdigit():
        return status.strip()
    statuses = parse_statuses(client.get_xml(f"/projects/{_seg(project)}/register/schema"))
    for name, status_id in statuses.items():
        if name.casefold() == status.strip().casefold():
            return status_id
    raise AconexError(
        f"No status called {status!r} on this project. Its statuses: "
        + (", ".join(sorted(statuses)) or "(none listed)") + "."
    )


def mail_query(words=(), sender="", to="", subject="", body="", kind="", since="",
               until="", extra="") -> str:
    """Lucene for mail, so Aconex searches the whole mailbox. Free words have no
    field name, which searches the whole mail, message text included."""
    clauses = []
    for value, fields in ((sender, ("fromuserfullname", "fromtradingname")),
                          (to, ("touserfullname", "totradingname"))):
        if value.strip():
            phrase = lucene_phrase(value.strip())
            clauses.append("(" + " OR ".join(f'{f}:"{phrase}"' for f in fields) + ")")
    clauses += [f'subject:"{lucene_phrase(w)}"' for w in subject.split()]
    for field, value in (("corrdata", body), ("corrtypename", kind)):
        if value.strip():
            clauses.append(f'{field}:"{lucene_phrase(value.strip())}"')
    if since or until:
        # Live, `[20260811 TO *]` finds nothing, so an open end gets a far-off date.
        start, end = since.replace("-", "") or "00010101", until.replace("-", "") or "99991231"
        clauses.append(f"sentdate:[{start} TO {end}]")
    if extra:
        clauses.append(f"({extra})")
    # Last on purpose, for the same reason as in register_query.
    clauses += [f'"{lucene_phrase(w)}"' for w in words]
    return " AND ".join(clauses)


# ── Output ──────────────────────────────────────────────────────────


def clip(value, width: int | None = None) -> str:
    value = " ".join(str(value or "").split())
    if width and len(value) > width:
        return value[: width - 3] + "..."
    return value


def print_table(headers: list, rows: list, widths: dict | None = None) -> None:
    widths = widths or {}
    cells = [[clip(v, widths.get(h)) for h, v in zip(headers, row)] for row in rows]
    cols = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    for values in [headers, ["-" * c for c in cols]] + cells:
        print("  ".join(v.ljust(c) for v, c in zip(values, cols)).rstrip())


def day(stamp: str) -> str:
    return stamp[:10]


def when(stamp: str) -> str:
    if len(stamp) < 16:
        return stamp
    return stamp[:16].replace("T", " ") + (" UTC" if stamp.endswith("Z") else "")


def human_size(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _page_numbers(root) -> tuple:
    return root.get("CurrentPage") or root.get("PageNumber"), root.get("TotalPages")


def paging(root) -> str:
    total = root.get("TotalResults")
    if total is None:
        return ""
    page, pages = _page_numbers(root)
    return f"{total} in total" + (f", page {page} of {pages}" if page and pages else "")


def more_pages(root, shown: int, limit: int) -> bool:
    """From Aconex's page counts when it sends them, else a full page."""
    page, pages = _page_numbers(root)
    if (page or "").isdigit() and (pages or "").isdigit():
        return int(page) < int(pages)
    return shown >= limit


def _people(names: list, most: int = 20) -> str:
    shown = "; ".join(names[:most])
    return shown + (f"; and {len(names) - most} more" if len(names) > most else "")


# ── Downloads ───────────────────────────────────────────────────────

# File names come from whoever sent the file, and end up in commands, so only
# these characters survive: none a shell acts on, none that disguise a name.
_UNSAFE_CHARS = re.compile(r"[^\w .()+,#=\[\]@~-]")
# Names tools act on when they find them in a folder, and Windows' device names.
_RESERVED_NAME = re.compile(
    r"(?:(?:claude(?:\.local)?|agents|gemini|skill)\.md$|(?:settings|mcp)\.json$"
    r"|(?:con|prn|aux|nul|com\d|lpt\d)(?:\.|$))",
    re.IGNORECASE,
)
# Types that run, or that a tool would import or read as keys, when found in a
# folder. A download keeps its content but never gets to be one of these.
_ACTIVE_SUFFIXES = {
    ".bash", ".bat", ".cjs", ".cmd", ".com", ".dll", ".env", ".exe", ".hta", ".jar", ".js",
    ".lnk", ".mjs", ".msi", ".ps1", ".psm1", ".py", ".pyc", ".pyw", ".scr", ".sh", ".ts",
    ".url", ".vbe", ".vbs", ".wsf", ".wsh", ".zsh",
}
# Where tools keep settings, instructions and keys. Claude Code's own worktrees
# live under .claude/worktrees, and those are ordinary working folders.
_PROTECTED_FOLDERS = {
    ".aws", ".claude", ".config", ".cursor", ".docker", ".git", ".github", ".gnupg", ".husky",
    ".idea", ".kube", ".ssh", ".vscode",
}


def safe_filename(name: str, fallback: str) -> str:
    """A name that stays in its folder, can't act as a command or a program,
    and can't pass for a key file or a tool's instructions."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name).strip(" .-") or _UNSAFE_CHARS.sub("_", fallback)
    if _RESERVED_NAME.match(name):
        name = f"downloaded-{name}"
    if Path(name).suffix.casefold() in _ACTIVE_SUFFIXES:
        name += ".txt"
    return name


def checked_folder(folder: Path) -> Path:
    """`folder`, unless it's on another computer, or where tools keep settings
    or keys. Files from a project mustn't leave this machine by being saved."""
    if str(folder).startswith(("\\\\", "//")):
        raise AconexError(f"Won't use {folder}: it's on another computer. Save it here first.")
    parts = [part.casefold() for part in folder.resolve().parts]
    for here, after in zip(parts, parts[1:] + [""]):
        if here in _PROTECTED_FOLDERS and (here, after) != (".claude", "worktrees"):
            raise AconexError(f"Won't use {folder}: tools keep settings and keys in {here}.")
    return folder


def unique_path(path: Path) -> Path:
    """`path`, or `name (1).ext` and so on, so nothing is overwritten."""
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def save_download(client: Aconex, api_path: str, out_dir: str, fallback_stem: str):
    out = checked_folder(Path(out_dir).expanduser())
    out.mkdir(parents=True, exist_ok=True)
    with client.open(api_path, accept=ACCEPT_FILE) as resp:
        content_type = resp.headers.get_content_type()
        # Aconex percent-encodes even the plain filename="..." value.
        name = unquote(resp.headers.get_filename() or "") or (
            fallback_stem + (mimetypes.guess_extension(content_type) or "")
        )
        target = unique_path(out / safe_filename(name, fallback_stem))
        partial = target.with_name(target.name + ".part")
        try:
            with open(partial, "wb") as f:
                shutil.copyfileobj(resp, f, 1 << 20)
            os.replace(partial, target)
        finally:
            if partial.exists():
                partial.unlink()
    return target.resolve(), content_type


# ── Drafts ──────────────────────────────────────────────────────────


@dataclass
class Draft:
    type_id: str
    subject: str
    body_html: str
    to: list  # UserIds
    cc: list
    bcc: list
    doc_ids: list  # register DocumentIds (transmittal types only)
    files: list  # (file name, bytes)
    reason_id: str = ""
    replace_id: str = ""  # an existing draft's MAIL_ID, to overwrite it


def text_to_html(text: str) -> str:
    """Plain text as HTML: blank lines make paragraphs, newlines breaks."""
    paragraphs = re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n"))
    return "".join(
        "<p>" + "<br/>".join(xml_escape(line) for line in p.split("\n")) + "</p>"
        for p in paragraphs if p.strip()
    )


def xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cdata(value: str) -> str:
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def draft_file_name(name: str) -> str:
    """A file name safe for the X-Filename header: no folders, no line
    breaks, and no semicolons (Aconex drops them silently)."""
    name = re.sub(r"[;\r\n]", "", Path(name).name).strip()
    return name or "attachment"


def build_draft_body(draft: Draft, boundary: str) -> bytes:
    """The multipart/mixed body Aconex's Mail v3 create/reply takes: one
    XML part (fields in the schema's alphabetical order), then one base64
    part per local file."""
    def each(tag, values):
        return "".join(f"<{tag}>{xml_escape(str(v))}</{tag}>" for v in values if v)

    fields = [
        each("AttachedDocId", draft.doc_ids),
        each("BccUserId", draft.bcc),
        each("CcUserId", draft.cc),
        each("ExistingMailId", [draft.replace_id]),
        f"<MailBody>{_cdata(draft.body_html)}</MailBody>",
        each("MailSubject", [draft.subject]),
        each("MailTypeId", [draft.type_id]),
        each("ReasonForIssue", [draft.reason_id]),
        "<RichMailText>true</RichMailText>",
        f"<TotalAttachmentCount>{len(draft.doc_ids) + len(draft.files)}</TotalAttachmentCount>",
        each("ToUserId", draft.to),
    ]
    xml = '<?xml version="1.0" encoding="UTF-8"?><Mail>' + "".join(fields) + "</Mail>"
    parts = [f"--{boundary}\r\nContent-Type: {MAIL_V3_PART}\r\n\r\n".encode() + xml.encode()]
    for name, data in draft.files:
        parts.append(f"--{boundary}\r\nX-Filename: {draft_file_name(name)}\r\n\r\n".encode()
                     + base64.b64encode(data))
    return b"\r\n".join(parts) + f"\r\n--{boundary}--\r\n".encode()


def pick(options: dict, wanted: str, what: str) -> tuple:
    """(id, name) from {name: id}, by ID, exact name, or a unique partial
    name, ignoring case. Otherwise an error that lists the options."""
    wanted = wanted.strip()
    by_id = {v: k for k, v in options.items()}
    if wanted in by_id:
        return wanted, by_id[wanted]
    folded = wanted.casefold()
    exact = [k for k in options if k.casefold() == folded]
    partial = exact or [k for k in options if folded in k.casefold()]
    if len(partial) == 1:
        return options[partial[0]], partial[0]
    listing = "; ".join(f"{k} ({v})" for k, v in sorted(options.items())) or "none listed"
    which = "more than one" if partial else "none"
    raise AconexError(f"{what} {wanted!r} matches {which}. This project's options: {listing}.")


def pick_people(directory: list, wanted: list, field: str) -> list:
    """Directory entries for each name or USER_ID in `wanted`. A name must
    match exactly one person: by full name, else by every word appearing in
    their name or organisation."""
    chosen = []
    for item in wanted:
        item = item.strip()
        if not item:
            continue
        matches = [p for p in directory if p["user_id"] == item]
        if not matches:
            folded = item.casefold()
            matches = [p for p in directory if p["name"].casefold() == folded] or [
                p for p in directory
                if all(w in f"{p['name']} {p['org']}".casefold() for w in folded.split())
            ]
        if len(matches) != 1:
            shown = "; ".join(f"{p['name']} ({p['org']}) USER_ID {p['user_id']}"
                              for p in matches[:10])
            raise AconexError(
                f"{field} {item!r}: " + (
                    f"{len(matches)} people match: {shown}. Use a fuller name or the USER_ID."
                    if matches else
                    "no one in this project's directory matches. Run `directory` to look."
                )
            )
        if matches[0] not in chosen:
            chosen.append(matches[0])
    return chosen


def _person_label(p: dict) -> str:
    return f"{p['name']} ({p['org']})" if p["org"] else p["name"]


# ── Commands ────────────────────────────────────────────────────────


def _seg(value: str) -> str:
    return quote(str(value).strip(), safe="")


def cmd_check(client: Aconex, args) -> None:
    keys = client.keys
    projects = parse_projects(client.get_xml("/projects"))
    print(f"Login OK (keys from {keys.source}).")
    if keys.username:
        print(f"Aconex user: {keys.username}")
    print(f"API: {keys.base_url}")
    print(f"{len(projects)} project(s) visible:")
    for p in projects[:10]:
        print(f"  {p['id']}  {p['name']}")
    if len(projects) > 10:
        print(f"  ... and {len(projects) - 10} more (run `projects`)")


def cmd_projects(client: Aconex, args) -> None:
    projects = parse_projects(client.get_xml("/projects"))
    words = " ".join(args.text).lower().split()
    shown = [
        p for p in projects
        if all(w in f"{p['name']} {p['code']} {p['id']}".lower() for w in words)
    ]
    print(f"{len(shown)} of {len(projects)} project(s) visible to this Aconex user:")
    print_table(["PROJECT_ID", "NAME", "CODE"], [[p["id"], p["name"], p["code"]] for p in shown])


def cmd_docs(client: Aconex, args) -> None:
    status = resolve_status(client, args.project, args.status) if args.status else ""
    content = (args.content or "").strip()
    query = register_query(
        words=" ".join(args.text).split(), number=args.number or "",
        title=args.title or "", discipline=args.discipline or "",
        doc_type=args.type or "", status=status, extra=args.query or "",
        content=content,
    )
    sort_field = DOC_SORTS[args.sort]
    page_size = api_page_size(args.limit)
    params = {
        "return_fields": REGISTER_FIELDS,
        "search_type": "PAGED",
        "page_size": str(page_size),
        "page_number": str(args.page),
        "show_document_history": "true" if args.all_revisions else "false",
        "sort_field": sort_field,
        "sort_direction": "DESC" if sort_field == "revisiondate" else "ASC",
    }
    if query:
        params["search_query"] = query
    if content:
        params["content_search"] = "true"
    root = client.get_xml(f"/projects/{_seg(args.project)}/register", params)
    page = parse_documents(root)
    docs = page[: args.limit]
    revisions = "all revisions" if args.all_revisions else "current revisions only"
    inside = ", inside files" if content else ""
    print(f"Search: {query or '(whole register)'} [{revisions}{inside}]")
    print(f"{paging(root) or str(len(page)) + ' result(s)'}.")
    print_table(
        ["DOCUMENT_ID", "NUMBER", "REV", "TITLE", "STATUS", "DATE", "DISCIPLINE", "TYPE", "FILE"],
        [[d["document_id"], d["number"], d["revision"], d["title"], d["status"],
          day(d["date"]), d["discipline"], d["type"], d["file"]] for d in docs],
        widths={"TITLE": 60, "FILE": 40},
    )
    if len(page) > len(docs):
        print(f"Showing the first {len(docs)} of this page's {len(page)}: "
              "raise --limit to see more.")
    elif more_pages(root, len(page), page_size):
        print(f"More results: add --page {args.page + 1}.")


def cmd_history(client: Aconex, args) -> None:
    # One docno search with history returns just this document's revisions.
    params = {
        "search_query": f'docno:"{lucene_phrase(args.number)}"',
        "return_fields": REGISTER_FIELDS,
        "page_size": "100",
        "show_document_history": "true",
    }
    docs = parse_documents(client.get_xml(f"/projects/{_seg(args.project)}/register", params))
    wanted = args.number.strip().lower()
    revisions = [d for d in docs if d["number"].lower() == wanted]
    if not revisions:
        if docs:
            print(f"No document numbered exactly {args.number}. Similar numbers:")
            print_table(["NUMBER", "REV", "TITLE"],
                        [[d["number"], d["revision"], d["title"]] for d in docs],
                        widths={"TITLE": 60})
        else:
            print(f"No document numbered {args.number} in project {args.project}.")
        return
    revisions.sort(key=lambda d: (d["date"], d["revision"]))
    print(f"{args.number}: {len(revisions)} revision(s), oldest first (last row = latest).")
    print_table(
        ["DOCUMENT_ID", "REV", "DATE", "STATUS", "TITLE", "FILE"],
        [[d["document_id"], d["revision"], day(d["date"]), d["status"], d["title"], d["file"]]
         for d in revisions],
        widths={"TITLE": 60, "FILE": 40},
    )


def cmd_mail(client: Aconex, args) -> None:
    query = mail_query(
        words=" ".join(args.text).split(), sender=args.sender or "", to=args.to or "",
        subject=args.subject or "", body=args.body or "", kind=args.type or "",
        since=args.since or "", until=args.until or "", extra=args.query or "",
    )
    page_size = api_page_size(args.limit)
    params = {
        "mail_box": MAIL_BOXES[args.box],
        "search_type": "PAGED",
        "page_size": str(page_size),
        "sort_field": "sentdate",
        "sort_direction": "DESC",
        "return_fields": MAIL_FIELDS,
    }
    if query:
        params["search_query"] = query
    found, page, total = [], args.page, ""
    while True:
        root = client.get_xml(
            f"/projects/{_seg(args.project)}/mail",
            dict(params, page_number=str(page)), ACCEPT_MAIL,
        )
        total = total or paging(root)
        batch = parse_mail_list(root)
        found += batch
        page += 1
        if len(found) >= args.limit or not more_pages(root, len(batch), page_size):
            break
    box = {"inbox": "Inbox", "sent": "Sent mail", "all": "Inbox and sent mail",
           "drafts": "Drafts"}[args.box]
    if query:
        print(f"Search: {query}")
    print(f"{box}, newest first ({total or str(len(found)) + ' shown'}).")
    print_table(
        ["MAIL_ID", "NUMBER", "SENT", "TYPE", "FROM", "SUBJECT"],
        [[m["mail_id"], m["number"], day(m["sent"]), m["type"], m["from"], m["subject"]]
         for m in found[: args.limit]],
        widths={"FROM": 40, "SUBJECT": 70},
    )
    if len(found) > args.limit:
        print(f"Showing the first {args.limit} of these {len(found)}: "
              "raise --limit to see more.")
    elif more_pages(root, len(batch), page_size):
        print(f"More results: raise --limit, or add --page {page}.")


def cmd_mail_show(client: Aconex, args) -> None:
    root = client.get_xml(
        f"/projects/{_seg(args.project)}/mail/{_seg(args.mail_id)}", accept=ACCEPT_MAIL
    )
    mail = parse_mail(root, args.mail_id)
    draft = mail["number"].upper().startswith("DRAFT")
    print(f"Mail {mail['number'] or mail['mail_id']} (MAIL_ID {mail['mail_id']})"
          + (" - an unsent draft" if draft else ""))
    names = {kind: [p["name"] for p in mail[kind]] for kind in ("to", "cc", "bcc")}
    for label, value in (
        ("Type", mail["type"]), ("Reason", mail["reason"]), ("Subject", mail["subject"]),
        ("Saved" if draft else "Sent", when(mail["sent"])), ("From", mail["from"]),
        ("To", _people(names["to"])), ("Cc", _people(names["cc"])), ("Bcc", _people(names["bcc"])),
    ):
        if value:
            print(f"{label}: {value}")
    body = mail["body"]
    print("\n--- Message ---")
    if len(body) > MAX_BODY_CHARS:
        print(body[:MAX_BODY_CHARS])
        print(f"[... {len(body) - MAX_BODY_CHARS} more characters; --raw shows everything]")
    else:
        print(body or "(no message text)")
    attachments = mail["attachments"]
    print(f"\n--- Attachments ({len(attachments)}) ---")
    if attachments:
        print_table(
            ["ATTACHMENT_ID", "KIND", "NUMBER", "REV", "TITLE", "FILE", "SIZE"],
            [[a["attachment_id"], a["kind"], a["number"], a["revision"], a["title"],
              a["file"], human_size(a["size"])] for a in attachments[: args.max_attachments]],
            widths={"TITLE": 50, "FILE": 40},
        )
        if len(attachments) > args.max_attachments:
            print(f"... and {len(attachments) - args.max_attachments} more "
                  "(add --max-attachments)")


def cmd_download_doc(client: Aconex, args) -> None:
    target, content_type = save_download(
        client, f"/projects/{_seg(args.project)}/register/{_seg(args.document_id)}",
        args.out, f"aconex-document-{args.document_id}",
    )
    print(f"Saved {target} ({human_size(target.stat().st_size)}, {content_type})")


def cmd_download_attachment(client: Aconex, args) -> None:
    target, content_type = save_download(
        client,
        f"/projects/{_seg(args.project)}/mail/{_seg(args.mail_id)}"
        f"/attachments/{_seg(args.attachment_id)}",
        args.out, f"aconex-attachment-{args.attachment_id}",
    )
    print(f"Saved {target} ({human_size(target.stat().st_size)}, {content_type})")


ACCEPTS = {
    "xml": ACCEPT_XML,
    "mail": ACCEPT_MAIL,
    "workflow": "application/vnd.aconex.workflow.v1+xml",
    "json": "application/json",
}


def api_url(path: str) -> str:
    """An API path ("projects/1/..." or "/projects/1/...") on the configured
    API, or a full https URL on an Aconex server. The leading slash is
    optional because Git Bash on Windows rewrites "/projects/..." into a file
    path before the script sees it."""
    if "://" not in path:
        if re.match(r"[A-Za-z]:[\\/]", path):
            raise AconexError(
                f"{path!r} looks like a file path. Write the API path without "
                "the leading slash, e.g. projects/ID/register (Git Bash turns "
                "/projects/... into a file path)."
            )
        return "/" + path.lstrip("/")
    if not is_aconex_url(path):
        raise AconexError(
            "`api` only calls Aconex: give a path like /projects/ID/... or an "
            "https URL on an Aconex server."
        )
    return path


def pretty_xml(content: bytes) -> str:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return content.decode("utf-8", "replace")
    if hasattr(ET, "indent"):  # Python 3.9+
        ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def cmd_api(client: Aconex, args) -> None:
    """GET any Aconex endpoint: the way to reach everything without a
    dedicated command (workflows by number, event logs, project fields,
    organisations, models...). See Oracle's guides at help.aconex.com/apis.
    Never writes."""
    target = api_url(args.path)
    params = {}
    for pair in args.param or []:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise AconexError(f"--param takes NAME=VALUE, not {pair!r}.")
        params[name] = value
    accept = ACCEPTS.get(args.accept, args.accept)
    if target.startswith("/"):
        response = client.open(target, params or None, accept)
    else:
        url = target + ("?" + urlencode(params) if params else "")
        response = client.open_url(url, accept)
    with response as resp:
        content = resp.read()
        content_type = resp.headers.get_content_type()
    if args.out:
        given = Path(args.out).expanduser()
        folder = checked_folder(given.parent)
        folder.mkdir(parents=True, exist_ok=True)
        out = unique_path(folder / safe_filename(given.name, "aconex-response"))
        out.write_bytes(content)
        print(f"Saved {out.resolve()} ({human_size(len(content))}, {content_type})")
        return
    text = pretty_xml(content) if "xml" in content_type else content.decode("utf-8", "replace")
    text = _CONTROL_CHARS.sub("", text)
    if len(text) > args.max_chars:
        print(text[: args.max_chars])
        print(f"[... {len(text) - args.max_chars} more characters: raise --max-chars or use --out]")
    else:
        print(text)


def _flatten(el, prefix: str = "") -> list:
    """(path, text) for every leaf of an element, for showing any record."""
    rows = []
    for child in el:
        path = f"{prefix}{child.tag}"
        if len(child):
            rows += _flatten(child, path + "/")
        elif (child.text or "").strip():
            rows.append((path, " ".join(child.text.split())))
    return rows


def cmd_doc_show(client: Aconex, args) -> None:
    root = client.get_xml(
        f"/projects/{_seg(args.project)}/register/{_seg(args.document_id)}/metadata")
    rows = _flatten(root)
    print(f"Document {args.document_id}: {len(rows)} field(s)")
    print_table(["FIELD", "VALUE"], rows, widths={"VALUE": 100})


WORKFLOW_STATUSES = ("current", "forecast", "complete", "skipped", "terminated")


def parse_workflows(root) -> list:
    return [
        {
            "id": w.get("WorkflowId", ""),
            "number": _text(w, "WorkflowNumber"),
            "name": _text(w, "WorkflowName"),
            "status": _text(w, "WorkflowStatus"),
            "step": _text(w, "StepName"),
            "step_status": _text(w, "StepStatus"),
            "outcome": _text(w, "StepOutcome"),
            "document": " ".join(filter(None, (_text(w, "DocumentNumber"),
                                               _text(w, "DocumentRevision")))),
            "title": _text(w, "DocumentTitle"),
            "due": _text(w, "DateDue"),
            "days_late": _text(w, "DaysLate"),
            "assignees": [_person(a) for a in w.findall("Assignees/Assignee")],
        }
        for w in root.iter("Workflow")
    ]


def cmd_workflows(client: Aconex, args) -> None:
    path = f"/projects/{_seg(args.project)}/workflows"
    params = {"page_size": str(api_page_size(args.limit)), "page_number": str(args.page)}
    if args.number:
        path += "/search"
        params["workflow_number"] = ",".join(args.number)
    else:
        if args.status:
            path += f"/{args.status}"
        if args.initiated_by:
            path += f"/initiated-by/{args.initiated_by}"
        elif args.assigned_to:
            path += f"/assigned-to/{args.assigned_to}"
        if args.since:
            params["updated_after"] = f"{args.since}T00:00:00Z"
    try:
        root = client.get_xml(path, params, ACCEPTS["workflow"])
    except AconexError as err:
        if "WORKFLOW_NOT_ENABLED_FOR_PROJECT" in str(err):
            raise AconexError("Workflows aren't turned on for this project in Aconex.") from None
        raise
    flows = parse_workflows(root)[: args.limit]
    print(f"Workflows: {paging(root) or str(len(flows)) + ' found'}.")
    print_table(
        ["NUMBER", "STATUS", "STEP", "STEP_STATUS", "OUTCOME", "DOCUMENT", "TITLE", "DUE",
         "LATE", "ASSIGNEES"],
        [[f["number"], f["status"], f["step"], f["step_status"], f["outcome"], f["document"],
          f["title"], day(f["due"]), f["days_late"], _people(f["assignees"], 3)] for f in flows],
        widths={"TITLE": 40, "ASSIGNEES": 50, "STEP": 30},
    )
    if more_pages(root, len(flows), api_page_size(args.limit)):
        print(f"More results: add --page {args.page + 1}.")


# Where `upload` may send a drawing: the one-time links RevRadar's connector
# returns from create_upload_links, checked against the comparison they're for.
# Nowhere else, so a stray or planted link doesn't get a project's drawings.
UPLOAD_HOST = "app.revradar.com.au"
UPLOAD_PATH = re.compile(r"/api/upload/([0-9a-fA-F-]{36})/(?:old|new)")


def upload_url(url: str, comparison: str) -> str:
    parts = urlsplit(url)
    target = UPLOAD_PATH.fullmatch(parts.path)
    if parts.scheme != "https" or (parts.hostname or "").lower() != UPLOAD_HOST or not target:
        raise AconexError(
            "`upload` only sends files to RevRadar's own upload links. Use the "
            "link the RevRadar connector's create_upload_links just gave."
        )
    if target.group(1).casefold() != comparison.strip().casefold():
        raise AconexError(
            f"That upload link isn't for comparison {comparison}. Use the links "
            "create_upload_links gave with that comparison_id."
        )
    return url


def cmd_upload(args) -> None:
    """PUT one downloaded PDF to a RevRadar upload link. Sends no Aconex
    keys; the link itself is the permission, valid once."""
    url = upload_url(args.url, args.comparison)
    path = Path(args.file).expanduser()
    if not path.is_file():
        raise AconexError(f"File not found: {path}")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise AconexError(f"{path.name} isn't a PDF; RevRadar compares PDFs.")
    request = urllib.request.Request(
        url, data=data, method="PUT", headers={"Content-Type": "application/pdf"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        raise AconexError(
            f"RevRadar refused the upload (HTTP {err.code}: {_error_text(err)}). "
            "Upload links work once and expire after two hours: ask the "
            "connector for new ones."
        ) from None
    except urllib.error.URLError as err:
        raise AconexError(_unreachable(url, err.reason)) from None
    print(f"Uploaded {path.name} ({human_size(len(data))}) to RevRadar.")


# The RevRadar connector's upload links are on app.revradar.com.au. A sandbox
# that only reaches allowed domains needs it on that list, so check it before
# creating a comparison: one that can't be uploaded to waits for files forever.
REVRADAR_CHECK_URL = "https://app.revradar.com.au/api/health"


def cmd_revradar_check(args) -> None:
    """Whether this sandbox can reach RevRadar's upload address. Sends nothing."""
    try:
        with urllib.request.urlopen(REVRADAR_CHECK_URL, timeout=TIMEOUT_S) as resp:
            resp.read()
    except urllib.error.HTTPError as err:
        raise AconexError(_unreachable(REVRADAR_CHECK_URL, f"HTTP {err.code}")) from None
    except urllib.error.URLError as err:
        raise AconexError(_unreachable(REVRADAR_CHECK_URL, err.reason)) from None
    except (TimeoutError, socket.timeout):
        raise AconexError(_unreachable(REVRADAR_CHECK_URL, "timed out")) from None
    print("RevRadar can be reached at app.revradar.com.au.")


def load_directory(client: Aconex, project: str) -> list:
    # Aconex returns the whole directory in one response; it takes no paging.
    return parse_directory(client.get_xml(f"/projects/{_seg(project)}/directory"))


def cmd_directory(client: Aconex, args) -> None:
    people = load_directory(client, args.project)
    words = " ".join(args.text).casefold().split()
    shown = [p for p in people
             if all(w in f"{p['name']} {p['org']} {p['job']}".casefold() for w in words)]
    print(f"{len(shown)} of {len(people)} people in this project's directory"
          + (f" (showing {args.limit})" if len(shown) > args.limit else "") + ":")
    print_table(["USER_ID", "NAME", "ORGANISATION", "JOB_TITLE", "EMAIL"],
                [[p["user_id"], p["name"], p["org"], p["job"], p["email"]]
                 for p in shown[: args.limit]],
                widths={"ORGANISATION": 40, "JOB_TITLE": 30})


def mail_schema(client: Aconex, project: str) -> dict:
    root = client.get_xml(f"/projects/{_seg(project)}/mail/schema", accept=ACCEPT_MAIL)
    return {
        "types": parse_schema_values(root, "MailTypeId"),
        "reasons": parse_schema_values(root, "ReasonForIssue"),
    }


def cmd_mail_types(client: Aconex, args) -> None:
    schema = mail_schema(client, args.project)
    for title, values in (("Mail types", schema["types"]),
                          ("Reasons for issue (transmittals)", schema["reasons"])):
        print(f"{title}:")
        print_table(["ID", "NAME"], [[v, k] for k, v in sorted(values.items())])
        print()


# A filled-in key, whatever the file holding it is called.
_KEY_IN_FILE = re.compile(rb"ACONEX_\w*(?:SECRET|CREDENTIALS\w*|B64)\s*=\s*[\"']?[A-Za-z0-9+/=_-]{8,}")


def read_for_mail(path: str) -> tuple:
    """(path, content) of a file to put in a mail, unless it's a key file or
    somewhere tools keep settings: those must never reach a draft, or the
    preview printed here. The name is the real one on disk, because Windows also
    opens "aconex.env." and "aconex.env::$DATA" as aconex.env."""
    p = Path(path).expanduser()
    name = p.resolve().name.split(":")[0].rstrip(" .").casefold()
    if name.endswith(".env") or name.startswith((".env", ".aconex")):
        raise AconexError(f"Won't put {p.name} in a mail: it looks like a key file.")
    checked_folder(p.parent)
    if not p.is_file():
        raise AconexError(f"File not found: {p}")
    data = p.read_bytes()
    if _KEY_IN_FILE.search(data):
        raise AconexError(f"Won't put {p.name} in a mail: it has Aconex keys in it.")
    return p, data


def _read_body(args) -> str:
    text = args.body or ""
    if args.body_file:
        path, data = read_for_mail(args.body_file)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise AconexError(f"{path.name} isn't plain text, so it can't be the message.") from None
    if not text.strip():
        raise AconexError("Give the message with --body or --body-file.")
    return text_to_html(text)


def _read_files(paths: list) -> list:
    files = []
    for path in paths:
        p, data = read_for_mail(path)
        files.append((p.name, data))
    return files


def _document_label(client: Aconex, project: str, document_id: str) -> str:
    root = client.get_xml(f"/projects/{_seg(project)}/register/{_seg(document_id)}/metadata")
    number, revision = _first(root, "DocumentNumber", "DocumentNo"), _text(root, "Revision")
    label = " ".join(filter(None, (number, f"rev {revision}" if revision else "", _text(root, "Title"))))
    return f"{label} (DOCUMENT_ID {document_id})" if label else f"DOCUMENT_ID {document_id}"


def approval_code(project: str, reply_to: str, draft: Draft, kept: list = ()) -> str:
    """A short code for exactly this draft, plus any attachments it keeps from
    the one it replaces. The preview prints it and --save needs it, so nothing
    changes between the two. It doesn't prove a person approved."""
    digest = hashlib.sha256(json.dumps([
        project, reply_to, draft.type_id, draft.reason_id, draft.replace_id, draft.subject,
        draft.body_html, draft.to, draft.cc, draft.bcc, draft.doc_ids, list(kept),
    ]).encode())
    for name, data in draft.files:
        digest.update(hashlib.sha256(name.encode() + b"\0" + data).digest())
    return digest.hexdigest()[:8]


def _compose(client: Aconex, args, *, subject: str, to: list, cc: list,
             reply_note: str = "") -> None:
    """Resolve names, show the draft, and save it only with --save."""
    schema = mail_schema(client, args.project)
    type_id, type_name = pick(schema["types"], args.type, "Mail type")
    reason_id = reason_name = ""
    if args.reason:
        reason_id, reason_name = pick(schema["reasons"], args.reason, "Reason for issue")
    transmittal = "transmittal" in type_name.casefold()
    if args.attach_doc and not transmittal:
        raise AconexError(
            "Aconex only attaches register documents to Transmittal or Tender "
            f"Transmittal mail, not {type_name!r}. Pick a transmittal --type, "
            "or download the files and use --attach."
        )
    if args.attach and transmittal:
        raise AconexError(
            f"Aconex doesn't allow local files on {type_name!r} mail: attach "
            "register documents with --attach-doc, or pick another --type."
        )
    directory = load_directory(client, args.project)
    people = {
        "To": pick_people(directory, to, "--to"),
        "Cc": pick_people(directory, cc, "--cc"),
        "Bcc": pick_people(directory, args.bcc or [], "--bcc"),
    }
    if not people["To"]:
        raise AconexError("A draft needs at least one --to recipient.")
    draft = Draft(
        type_id=type_id, subject=subject, body_html=_read_body(args),
        to=[p["user_id"] for p in people["To"]], cc=[p["user_id"] for p in people["Cc"]],
        bcc=[p["user_id"] for p in people["Bcc"]], doc_ids=list(args.attach_doc or []),
        files=_read_files(args.attach or []), reason_id=reason_id,
        replace_id=args.replace or "",
    )

    old = parse_mail(client.get_xml(
        f"/projects/{_seg(args.project)}/mail/{_seg(args.replace)}", accept=ACCEPT_MAIL),
        args.replace) if args.replace else None
    kept = [a["file"] or a["number"] or a["attachment_id"] for a in old["attachments"]] if old else []
    code = approval_code(args.project, getattr(args, "mail_id", ""), draft, kept)
    saving = args.save and args.confirm == code
    print("DRAFT " + ("(saving now)" if saving else "PREVIEW - nothing has been saved to Aconex"))
    if reply_note:
        print(reply_note)
    if old:
        recipients = _people([p["name"] for p in old["to"]]) or "no one yet"
        print(f"Replaces {old['number'] or 'draft'} (MAIL_ID {args.replace}), "
              f"now to {recipients}: {old['subject']}")
    print(f"From: the Aconex user these keys belong to"
          + (f" ({client.keys.username})" if client.keys.username else ""))
    for label, chosen in people.items():
        if chosen:
            print(f"{label}: " + "; ".join(_person_label(p) for p in chosen))
    print(f"Type: {type_name}" + (f"\nReason: {reason_name}" if reason_name else ""))
    print(f"Subject: {subject}\n\n--- Message ---\n{html_to_text(draft.body_html)}")
    if draft.doc_ids or draft.files or kept:
        print("\n--- Attachments ---")
        for name in kept:
            print(f"  already on the draft, and stays on it: {name}")
        for doc_id in draft.doc_ids:
            print(f"  register document {_document_label(client, args.project, doc_id)}")
        for name, data in draft.files:
            print(f"  file {draft_file_name(name)} ({human_size(len(data))})")
        if args.replace and (draft.doc_ids or draft.files):
            print("  Note: Aconex keeps the attachments a draft already has when it is "
                  "replaced, so attaching the same file again adds a second copy.")

    if args.save and not saving:
        raise AconexError(
            "Nothing saved: --confirm needs the code from a preview of exactly this draft. "
            "Run the command without --save, show the user the preview, and if they "
            "approve it, use the code it gives."
        )
    if not args.save:
        print("\nNothing saved. Show this preview to the user; once they approve it, "
              f"run the same command again with --save --confirm {code}")
        return
    boundary = "aconex-skill-" + base64.b16encode(os.urandom(8)).decode().lower()
    mail_id = client.save_draft(args.project, build_draft_body(draft, boundary), boundary,
                                reply_to=getattr(args, "mail_id", ""))
    saved = parse_mail(client.get_xml(
        f"/projects/{_seg(args.project)}/mail/{_seg(mail_id)}", accept=ACCEPT_MAIL), mail_id)
    print(f"\nSaved as {saved['number'] or 'a draft'} (MAIL_ID {mail_id}). It has NOT been sent.")
    print("To send it, open Aconex > Mail > Drafts, check it, and press Send there.")
    if draft.doc_ids and not any(a["kind"] == "register" for a in saved["attachments"]):
        print("Check the attached register documents when reviewing it in Aconex: "
              "Aconex doesn't list them on the saved draft.")


def cmd_draft(client: Aconex, args) -> None:
    _compose(client, args, subject=args.subject, to=args.to or [], cc=args.cc or [])


def cmd_draft_reply(client: Aconex, args) -> None:
    original = parse_mail(client.get_xml(
        f"/projects/{_seg(args.project)}/mail/{_seg(args.mail_id)}", accept=ACCEPT_MAIL),
        args.mail_id)
    to, cc = list(args.to or []), list(args.cc or [])
    if not to:  # reply to the sender, plus everyone else with --all
        to = [original["from_user_id"]] if original["from_user_id"] else []
        if args.all:
            to += [p["user_id"] for p in original["to"] if p["user_id"]]
            cc += [p["user_id"] for p in original["cc"] if p["user_id"]]
    subject = args.subject or (
        original["subject"] if re.match(r"(?i)re:", original["subject"])
        else f"RE: {original['subject']}")
    note = (f"Reply to {original['number'] or args.mail_id}: {original['subject']} "
            f"(from {original['from']})")
    _compose(client, args, subject=subject, to=to, cc=cc, reply_note=note)


def cmd_setup(args) -> None:
    path = Path(args.path).expanduser() if args.path else (
        profile_key_file(args.profile) if args.profile else DEFAULT_KEY_FILE)
    if path.exists():
        print(f"{path} already exists: open it and fill in the keys (nothing changed).")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(KEY_FILE_TEMPLATE)
    if args.path:
        check = f"check --key-file {path}"
    elif args.profile:
        check = f"check --profile {args.profile}"
    else:
        check = "check"
    print(f"Created {path}. Fill in the base64 credentials and Aconex email, save it, then run: {check}")


# ── Command line ────────────────────────────────────────────────────


def _limit(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 500:
        raise argparse.ArgumentTypeError("must be between 1 and 500")
    return n


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD")
    return value


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--key-file", metavar="PATH", help="read the keys from this file")
    common.add_argument("--profile", metavar="NAME",
                        help="use the keys in ~/.aconex-NAME.env (e.g. a test account)")
    common.add_argument("--raw", action="store_true",
                        help="also print the raw XML Aconex sends back")

    parser = argparse.ArgumentParser(
        prog="aconex.py",
        description="Aconex lookups (register, mail, directory, revision history, "
                    "downloads) and mail drafts that a person reviews and sends "
                    "in Aconex. It cannot send mail.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    def command(name, func, summary):
        p = sub.add_parser(name, parents=[common], help=summary, description=summary)
        p.set_defaults(func=func)
        return p

    command("check", cmd_check, "Test the keys and connection.")

    p = command("projects", cmd_projects, "List the projects this Aconex user can see.")
    p.add_argument("text", nargs="*", help="only projects whose name or code has these words")

    p = command("docs", cmd_docs,
                "Search a project's document register (current revisions by default).")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("text", nargs="*", help="words to find in the document number or title")
    p.add_argument("--number", help="document number, exactly")
    p.add_argument("--title", help="words that must all be in the title")
    p.add_argument("--content", metavar="TEXT",
                   help="exact text to find inside the files, such as a door number")
    p.add_argument("--discipline", help="discipline, as the project names it")
    p.add_argument("--type", help="document type, as the project names it")
    p.add_argument("--status", help="status name (e.g. 'For Construction') or status ID")
    p.add_argument("--query", help="extra raw Lucene, ANDed with the rest")
    p.add_argument("--all-revisions", action="store_true", help="include older revisions")
    p.add_argument("--sort", choices=sorted(DOC_SORTS), default="number",
                   help="date sorts newest first (default: number)")
    p.add_argument("--limit", type=_limit, default=25, help="most results to show (default 25)")
    p.add_argument("--page", type=int, default=1,
                   help="page number (default 1); a page holds --limit rounded up "
                        "to a multiple of 25")

    p = command("history", cmd_history, "Every revision of one document, oldest first.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("number", help="document number")

    p = command("mail", cmd_mail,
                "Search the whole mailbox through Aconex's search, newest first.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("text", nargs="*",
                   help="whole words to find anywhere in the mail, message text included")
    p.add_argument("--box", choices=sorted(MAIL_BOXES), default="inbox",
                   help="all is the inbox and sent mail together (default inbox)")
    p.add_argument("--from", dest="sender", help="sender's name or organisation")
    p.add_argument("--to", help="recipient's name or organisation, Cc included")
    p.add_argument("--subject", help="whole words that must all be in the subject")
    p.add_argument("--body", metavar="TEXT", help="exact text to find in the message")
    p.add_argument("--type", help="mail type, as the project names it (see mail-types)")
    p.add_argument("--since", type=_date, help="sent on or after this date (YYYY-MM-DD)")
    p.add_argument("--until", type=_date, help="sent on or before this date (YYYY-MM-DD)")
    p.add_argument("--query", help="extra Super Search (Lucene), ANDed with the rest")
    p.add_argument("--limit", type=_positive, default=25,
                   help="most mails to show, as many as you like (default 25)")
    p.add_argument("--page", type=int, default=1,
                   help="page number (default 1); a page holds --limit rounded up "
                        "to a multiple of 25, 500 at most")

    p = command("mail-show", cmd_mail_show,
                "One mail: sender, recipients, message text and attachments.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("mail_id", help="MAIL_ID")
    p.add_argument("--max-attachments", type=int, default=100,
                   help="most attachments to list (default 100)")

    p = command("download-doc", cmd_download_doc, "Save a register document's file.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("document_id", help="DOCUMENT_ID (each revision has its own)")
    p.add_argument("--out", default="aconex-downloads",
                   help="folder to save into (default ./aconex-downloads)")

    p = command("download-attachment", cmd_download_attachment, "Save a mail attachment.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("mail_id", help="MAIL_ID")
    p.add_argument("attachment_id", help="ATTACHMENT_ID")
    p.add_argument("--out", default="aconex-downloads",
                   help="folder to save into (default ./aconex-downloads)")

    p = command("doc-show", cmd_doc_show, "Every field of one register document revision.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("document_id", help="DOCUMENT_ID")

    p = command("workflows", cmd_workflows, "A project's document review workflows.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("--status", choices=WORKFLOW_STATUSES, help="only steps in this state")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--initiated-by", choices=("us", "others"),
                     help="started by this user's organisation, or by others")
    who.add_argument("--assigned-to", choices=("us", "others"),
                     help="assigned to this user's organisation, or to others")
    p.add_argument("--number", action="append", metavar="WF_NUMBER",
                   help="look up by workflow number, e.g. WF-000031; repeat (up to 10)")
    p.add_argument("--since", type=_date, help="updated on or after this date (YYYY-MM-DD)")
    p.add_argument("--limit", type=_limit, default=25, help="most to show (default 25)")
    p.add_argument("--page", type=int, default=1, help="page number (default 1)")

    p = command("api", cmd_api,
                "GET any Aconex endpoint (read-only), for anything without its own command.")
    p.add_argument("path", help="API path such as /projects/ID/register/DOC/eventlog, "
                                "or a full https URL on an Aconex host")
    p.add_argument("--param", action="append", metavar="NAME=VALUE", help="query parameter; repeat")
    p.add_argument("--accept", default="xml",
                   help="xml, mail, workflow, json, or a full media type (default xml)")
    p.add_argument("--out", metavar="FILE",
                   help="save the response to this new file instead (never overwrites)")
    p.add_argument("--max-chars", type=int, default=20_000,
                   help="most characters to print (default 20000)")

    p = command("directory", cmd_directory,
                "People on a project, with the USER_IDs drafts are addressed to.")
    p.add_argument("project", help="PROJECT_ID")
    p.add_argument("text", nargs="*", help="only people whose name, organisation or job has these words")
    p.add_argument("--limit", type=int, default=100, help="most people to list (default 100)")

    p = command("mail-types", cmd_mail_types,
                "A project's mail types and reasons for issue, for drafts.")
    p.add_argument("project", help="PROJECT_ID")

    def draft_options(p, reply: bool) -> None:
        p.add_argument("project", help="PROJECT_ID")
        if reply:
            p.add_argument("mail_id", help="MAIL_ID of the mail to reply to")
        p.add_argument("--to", action="append", metavar="PERSON",
                       help="name or USER_ID; repeat for more"
                            + (" (default: the sender)" if reply else ""))
        p.add_argument("--cc", action="append", metavar="PERSON", help="name or USER_ID; repeat")
        p.add_argument("--bcc", action="append", metavar="PERSON", help="name or USER_ID; repeat")
        if reply:
            p.add_argument("--all", action="store_true",
                           help="reply to the sender and everyone the mail went to")
        p.add_argument("--subject", required=not reply,
                       help="subject" + (" (default: RE: the original's)" if reply else ""))
        p.add_argument("--type", required=True,
                       help="mail type name or ID (see mail-types), e.g. 'General Correspondence'")
        p.add_argument("--reason", help="reason for issue (transmittals), name or ID")
        p.add_argument("--body", help="the message, as plain text")
        p.add_argument("--body-file", metavar="PATH", help="read the message from this file")
        p.add_argument("--attach", action="append", metavar="PATH",
                       help="attach a local file; repeat for more")
        p.add_argument("--attach-doc", action="append", metavar="DOCUMENT_ID",
                       help="attach a register document revision (transmittals only); repeat")
        p.add_argument("--replace", metavar="MAIL_ID", help="overwrite this existing draft")
        p.add_argument("--save", action="store_true",
                       help="save the draft to Aconex; without it, only show a preview")
        p.add_argument("--confirm", metavar="CODE",
                       help="with --save: the code from the preview of exactly this draft")

    draft_options(command("draft", cmd_draft,
                          "Prepare a new mail as an unsent Aconex draft (preview unless --save)."),
                  reply=False)
    draft_options(command("draft-reply", cmd_draft_reply,
                          "Prepare a reply as an unsent Aconex draft (preview unless --save)."),
                  reply=True)

    p = sub.add_parser("upload", help="Send a downloaded PDF to a RevRadar upload link.",
                       description="Send a downloaded PDF to a one-time RevRadar upload "
                                   "link (from the RevRadar connector's create_upload_links).")
    p.add_argument("file", help="the PDF to send")
    p.add_argument("url", help="the upload link")
    p.add_argument("--comparison", required=True, metavar="COMPARISON_ID",
                   help="the comparison_id create_upload_links returned with the link")
    p.set_defaults(func=None)

    p = sub.add_parser("revradar-check", help="Check this sandbox can reach RevRadar for uploads.",
                       description="Check that RevRadar's upload address (app.revradar.com.au) "
                                   "can be reached, before creating a comparison. Sends nothing.")
    p.set_defaults(func=None)

    p = sub.add_parser("setup", help="Create a blank key file for the user to fill in.",
                       description="Create a blank key file for the user to fill in. "
                                   "Never overwrites an existing file.")
    p.add_argument("--path", help=f"where to create it (default {DEFAULT_KEY_FILE})")
    p.add_argument("--profile", metavar="NAME",
                   help="create ~/.aconex-NAME.env, for a second Aconex account")
    p.set_defaults(func=None)
    return parser


def main(argv: list | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            cmd_setup(args)
        elif args.command == "upload":
            cmd_upload(args)  # no Aconex login needed
        elif args.command == "revradar-check":
            cmd_revradar_check(args)  # likewise
        else:
            args.func(Aconex(load_keys(args.key_file, args.profile), raw=args.raw), args)
        return 0
    except AconexError as err:
        print(f"Error: {err}", file=sys.stderr)
    except (TimeoutError, socket.timeout):
        print("Error: Aconex took too long to answer; try again or narrow the search.",
              file=sys.stderr)
    except OSError as err:
        print(f"Error: {err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
