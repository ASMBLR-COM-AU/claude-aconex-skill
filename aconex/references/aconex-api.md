# Aconex API notes

What `aconex.py` does under the hood, for when a search needs tuning or Aconex answers oddly. Everything here was checked against the live Aconex API (2026-10).

## Login

- `POST https://constructionandengineering.oraclecloud.com/auth/token` with body `grant_type=client_credentials` and header `Authorization: Basic base64(client_id:secret)`. It returns an `access_token`, valid for 60 minutes. Every call after that sends `Authorization: Bearer <token>`. Neither header is repeated if a server redirects the request, so a redirect can't carry them to another host.
- A Lobby account linked to more than one Aconex account must also send `user_id` and `user_site` (the Aconex website, with no trailing slash) in that body, or Aconex answers `invalid_aconex_account`. The script sends them when `ACONEX_USER_ID` and `ACONEX_USER_SITE` are set.
- The API client is USERBOUND: it acts as one Aconex user and sees exactly that user's projects and permissions.
- API base: `https://api.aconex.com/api`, Aconex's shared API host. A USERBOUND token is issued for it, so a regional website address such as `au1.aconex.com` doesn't work for API calls.

## Document register search

`GET /projects/{project}/register` takes these parameters:

- `search_query` (Lucene)
- `return_fields`
- `search_type=PAGED`, with `page_size` and `page_number`
- `sort_field` and `sort_direction`
- `show_document_history`
- `content_search=true`, to look inside the files as well

Things to know:

- **Searching inside files needs a query with no field name.** With `content_search=true`, `"L2.04-17"` searches the files, but `title:"L2.04-17"` still looks only at the title. A quoted value ignores case and matches whole: `"L2.04-1"` doesn't find `L2.04-17`, and `"PLANT ROOM 2"` matches those words in that order. Unquoted single terms get a wildcard, so `SETDOW` finds `SETDOWN`. It combines with field filters through `AND`, and with `show_document_history=true` it reaches older revisions, but put the file text last: `"L2.04-17" AND docno:"A-1100"` wrongly finds nothing, while `docno:"A-1100" AND "L2.04-17"` works. Results name documents only, with no page or snippet. In one live test a PDF matched while its `-DWG` copy in the register didn't.

- **Older revisions are hidden by default.** Without `show_document_history=true`, only current revisions come back, and nothing says older ones exist.
- **Page sizes must be a multiple of 25, from 25 to 500.** Anything else is a 400 (`INVALID_PARAMETER_VALUE`). The script asks for the next multiple of 25 and shows only `--limit` rows.
- **Without `search_type=PAGED`, `page_size` is ignored** and at most 250 results come back. `TotalResults` then says 250, not the true total.
- **Query and return field names:** `docno`, `title`, `revision`, `discipline`, `doctype`, `statusid`, `revisiondate`, `filename`.
- **Element names in the results differ from the field names:** `DocumentNumber`, `DocumentStatus`, `DocumentType`, `Filename`, `Title`, `Revision`, `RevisionDate`, `Discipline`. Mail attachments use different ones again: `DocumentNo`, `Status`, `FileName`.
- **Status searches need the status ID.** `statusid:"For Review"` matches nothing; `statusid:"358"` works. `docs --status` looks the name up in `GET /projects/{project}/register/schema`, which lists each status's ID. Discipline and type search by name: `doctype:"Shop Drawing"` works, but its ID does not.
- **Sorting:** `docno` and `revisiondate` sort; `title` is a 400 because the schema marks it not sortable. Date sorting is approximate: revisions a day apart sometimes come back out of order.
- **Quote values and escape only `"` and `\`.** Write `docno:"S-1100"`. Escaping anything else inside the quotes breaks the search: against the live register, `docno:"AB123\-ST\-S020"` matches nothing, while `docno:"AB123-ST-S020"` finds the document.
- **Combining clauses:** use `AND`, `OR` and parentheses. Pass extra Lucene with `docs --query '...'`; it is ANDed with the other filters.
- **Filter values are project-specific.** Each project configures its own discipline, type and status lists, so copy a value from a result row, or read them from the register schema.

## Super Search

`docs --query` and `mail --query` take the same syntax as Aconex's Super Search bar, and it's ANDed with the command's other options.

- **Operators:** `AND`, `OR`, `NOT`, `*`, `?`, `~` (similar spelling), ranges such as `revision:[A TO C]`, and parentheses. A wildcard can't come first: `filename:*.dwg` is an HTTP 500, so use `filetype:dwg`.
- **Values:** a quoted value ignores case and matches whole words, in order. `subject:"Transmit"` doesn't find "Transmittal", but `subject:Trans*` does. Oracle's guide says field names are case sensitive.
- **Plain terms** have no field name and search the whole mail, message text included, but not attachments: a drawing's number found nothing even though its event log showed it sent on a transmittal. Part of a hyphenated number can miss: `"TRANSMIT-000008"` finds nothing, while `"ACME-TRANSMIT-000008"` or `"000008"` works.
- **Keep a plain term away from the end of a `docno` clause:** `"text" AND docno:"X"` wrongly finds nothing, in both the register and mail. `docno:"X" AND "text"` and `"text" AND (docno:"X")` both work.

Mail fields:

| Field | Holds | Checked live |
|---|---|---|
| `fromuserfullname`, `fromtradingname` | Sender's name, sender's organisation | Yes |
| `touserfullname`, `totradingname` | Recipient's name, recipient's organisation | Yes, and a Cc-only recipient matches |
| `subject` | Subject | Yes |
| `corrdata` | Message text | Yes, on drafts |
| `corrtypename` | Mail type by name | Yes. `corrtypeid` doesn't take a name |
| `docno` | Mail number | Yes |
| `sentdate` | Sent date, as a range | Yes |
| `reasonforissueid` | Reason for issue, by ID from `GET /projects/{project}/mail/schema` | Yes. The name doesn't match here, but the name as a plain phrase does |
| `inreftomailno` | The mail this one responds to | Accepted, but the only test returned the named mail itself |
| `attribute`, `secondaryattribute`, `thirdattribute`, `fourthattribute`, `responsedate`, `closedoutdate`, `tostatusid` | As named | From Oracle's guide; the project tested had no values to find |

Document fields, beyond those under "Document register search": `docstatus` (by name), `filename`, `filetype`, `author`, `reviewstatus`, `printsize`, `confidential` and `hasmarkups` (1 or 0), `attribute`, `secondaryattribute`, `attribute3`, `attribute4`. `docstatus`, `filename` and `filetype` found documents live; the others come from Oracle's guide.

## Mail

- Mail calls need the header `Accept: application/vnd.aconex.mail.v2+xml`.
- `GET /projects/{project}/mail?mail_box=Inbox|Sentbox|Allbox|Draftbox` lists mail (`mail --box inbox|sent|all|drafts`). `Allbox` is the inbox and sent mail together, without drafts. It pages the same way as the register: multiples of 25 up to 500, and capped at 250 without `search_type=PAGED`. `mail` fetches as many pages as `--limit` needs.
- **Ask for fields, or you get only IDs.** Without `return_fields`, each `<Mail>` holds just its MailId and links. Valid fields include `docno` (the mail number, returned as `<MailNo>`), `subject`, `sentdate`, `corrtypeid` (returned as `<CorrespondenceType>`), `fromUserDetails`, `mailRecipients` and `attribute`. `mailno` and `toUsers` are rejected (`INVALID_RESULT_SET_FIELD`).
- **Search by date with a `sentdate` range:** `sentdate:[20260101 TO 20260331]` includes both ends. `everythingsince` is rejected, and `sentdate:[20260811 TO *]` wrongly finds nothing, so `mail --since` and `--until` fill an open end with a far-off date.
- **The date search doesn't follow UTC.** A mail stamped `2026-03-15T23:07Z` is found under 20260316 and not 20260315, so give a day's leeway around a boundary. The `SENT` column shows the UTC date.
- `GET /projects/{project}/mail/{mail_id}` returns the full mail. One with thousands of attachments can take about 50 seconds (2,856 attachments is the largest seen).
- **Every recipient is under `<ToUsers>`, including Cc and Bcc.** Each `<Recipient>` has `<DistributionType>` TO, CC or BCC, plus `Name`, `OrganizationName` and `UserId`. There is no separate `<CcUsers>` block, so reading only the parent element shows Cc'd people as "To". The script goes by `DistributionType`.
- **The body is `<MailData>`**, as escaped HTML (`<RichText>true</RichText>`); it is empty on mail with no message text. The script turns it into plain text.
- Attachment kinds: `RegisteredDocumentAttachment` (a register document revision, with `DocumentId`, `DocumentNo`, `Revision`, `Title`, `Status`, `FileName`), `LocalFileAttachment` (an uploaded file, with `FileName` and `FileSize`) and `MailAttachment`.

## Directory

- `GET /projects/{project}/directory` returns everyone on the project in one response, as `<Directory>` entries with `UserId`, `UserName`, `OrganizationName`, `JobTitle` and `Email`. It takes no paging or search parameters (`UNEXPECTED_PARAMETER`), so the script filters it locally.
- Names aren't unique: the same person can appear twice with different UserIds. Drafts address people by UserId, so a name that matches more than one entry is an error that lists the candidates.

## Drafts

Saving a draft is the only write the script makes. A person must open it in Aconex and press Send.

- **New mail:** `POST /projects/{project}/mail?is_draft=true`. **Reply:** `POST /projects/{project}/mail/{mail_id}/reply?is_draft=true`. The reply is linked to the original's thread (`InRefToMailId`, `ThreadId`).
- The body is `multipart/mixed`. The first part has `Content-Type: application/vnd.aconex.mail.v3+xml` and holds a `<Mail>` with, in this order: `AttachedDocId`*, `BccUserId`*, `CcUserId`*, `ExistingMailId`, `MailBody` (HTML in CDATA), `MailSubject`, `MailTypeId`, `ReasonForIssue`, `RichMailText` (`true`), `TotalAttachmentCount`, `ToUserId`* (`*` = repeatable). Each local file follows as its own part with an `X-Filename:` header and the file in base64.
- The answer is `<SendMailResult sendStatus="Drafted"><NewMailId>…</NewMailId>`. The script treats anything other than `Drafted` as an error.
- The older v2 XML create format (`<ToUsers><Recipient>`, no multipart) is rejected with 405 `OPERATION_NOT_SUPPORTED`.
- **Unknown fields are ignored silently**: a wrongly named field still returns 200, without that field. Check a saved draft with `mail-show`.
- **Mail types and reasons are per project:** `GET /projects/{project}/mail/schema` lists `MailTypeId` and `ReasonForIssue` values (`mail-types`). Some values appear without a name; they're skipped.
- **Register documents** (`AttachedDocId`) can only go on Transmittal or Tender Transmittal mail, according to Aconex. The API doesn't list them on a saved draft, so check them in Aconex before sending.
- **Replacing a draft** (`ExistingMailId`) updates its recipients, subject and body, but keeps every attachment it already has: attaching the same file again adds a second copy, and attachments can't be removed this way.

## Downloads

- **Register document:** `GET /projects/{project}/register/{document_id}`. Each revision has its own DOCUMENT_ID.
- **Mail attachment:** `GET /projects/{project}/mail/{mail_id}/attachments/{attachment_id}`.
- Both come back as `application/octet-stream`, whatever the file is. Aconex percent-encodes the file name in both `filename="..."` and `filename*=`, so the script decodes it (`%20` becomes a space, `%2B` becomes `+`).

## Errors

| Code | Meaning |
|---|---|
| 401 | The token expired or the keys are wrong. The script logs in again once. |
| 403 | This user can't see that project or document. When saving a draft: the user or API client isn't allowed to create mail there. |
| 405 on a draft | `OPERATION_NOT_SUPPORTED`: the wrong create format, or creating mail isn't allowed for this client. |
| 400 on a search | Usually query syntax or an unknown field name. |
| 503 | Aconex is throttling. For reads, the script waits 5 s, then 15 s, then gives up. It never repeats a draft save. Don't retry in a loop. |

## Not yet confirmed against the live API

Every command has been run against a live project (2026-10-22): login, projects, register search with paging, sorting and the discipline, type and status filters, `history`, the mail list in all three boxes, mail detail with HTML body, To and Cc, `directory`, `mail-types`, `doc-show` (register metadata), `api` (for example a document's event log), downloads of register documents and of both register and local-file mail attachments, and new and reply drafts with local files. Still unconfirmed:

- Searching inside files (`docs --content`) on scanned drawings with no text layer, and on file types other than PDF apart from the one `-DWG` copy that didn't match.
- AU2: the script accepts its Lobby, but Oracle's guide doesn't give its API address and it hasn't been tried there.
- `mail` search options on a big mailbox: they were checked live on a mailbox of 10 mails and 15 drafts, so reading past the first page is covered only by tests against a fake server.
- `workflows` rows: the endpoints answer (`Accept: application/vnd.aconex.workflow.v1+xml`), but no project tested had any workflows, so the row parsing follows Aconex's guide only. A project without the module answers 403 `WORKFLOW_NOT_ENABLED_FOR_PROJECT`.

- register documents on drafts (`AttachedDocId`): Aconex accepts the draft but doesn't list them on it
- Bcc on received mail (expected as `DistributionType` BCC, like Cc)
- how a draft looks once a person has sent it
- `MailAttachment` attachments

If one misbehaves, run the command with `--raw` to see what Aconex actually sent, then fix the script and this list.
