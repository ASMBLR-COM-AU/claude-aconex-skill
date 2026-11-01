# Aconex skill for Claude

A Claude skill that lets Claude work inside Oracle Aconex for you: find drawings and documents in a project's register, trace a document's revisions, read project mail and transmittals in full, look people up in the project directory, download files, and write mail and replies as **drafts that a person reviews and sends**.

It works with commercial Aconex anywhere, including AU1. Each company plugs in its own Aconex API keys, and Claude then sees exactly what that Aconex user can see. AU2 (Australian government) takes extra setup, and Oracle's High Compliance instances aren't supported: see the [setup guide](aconex/references/setup.md).

**[Open the setup page](https://asmblr-com-au.github.io/claude-aconex-skill/)** to connect it to Claude. Built by [ASMBLR](https://asmblr.com.au).

## What it can do

| Ask Claude | What happens |
|---|---|
| "What's the latest revision of S-1100 on the Harbour St job?" | Searches the register and shows the current revision, and every one before it if you ask |
| "Which plan is door L2.04-17 on?" | Searches inside the project's files and lists the drawings that show it |
| "Who in our company last downloaded S-1100?" | Reads the drawing's event log and tells you who and when |
| "What changed between the last two revisions of S-2040?" | With the RevRadar connector added: downloads both revisions, runs a RevRadar comparison, and reports what changed with a link to the full report. Without it, downloads both for you to compare |
| "Anything from the engineer since Monday?" | Finds the matching mail, newest first |
| "Every mail Sam sent us about the slab pour this year" | Searches the whole mailbox by sender, recipient, subject, message text, type and date |
| "Open that transmittal" | Shows the message, To/Cc/Bcc and every attachment |
| "Send me the IFC set from that transmittal" | Downloads the attachments to a folder you can open. Claude can read them too |
| "Who's on this job from the engineer's office?" | Looks people up in the project directory by name, company or job title |
| "Reply to Sam and say we'll pour Thursday" | Drafts the reply, shows it to you, and saves it to your Aconex Drafts once you approve |

## Human in the loop

Claude can't send mail through this skill. There is no send function in the code, and every write asks Aconex for a draft (`is_draft=true`). Drafts go through two people-steps:

1. **Approve.** Draft commands only preview until they're run with `--save`. Claude shows you the preview (recipients, type, subject, message and attachments) and saves it only after you say yes. Saving also needs a code from the preview of exactly that draft, so nothing can change between the preview and the save. The code can't prove a person said yes: that relies on Claude following the skill, and on you checking the draft before you press Send.
2. **Send.** The saved draft sits in Aconex → Mail → Drafts. A person opens it, checks it and presses Send in Aconex.

Nothing else in Aconex is changed: no register uploads, workflows, deletions or edits to sent mail. These limits are in the skill, not in your Aconex account: the keys themselves carry that user's full API rights, which is why they need protecting.

## Before you use it

- **What other people write:** anyone on a project can write the mail and documents Claude reads, and could try to give it instructions there. The skill tells Claude to ignore them, and its commands refuse what looks like a key file and any unknown server, but no skill can guarantee Claude won't be fooled. Read every preview and question from Claude before you say yes.
- **Your project data:** whatever Claude reads through this skill, such as mail and drawings, is handled under your Claude plan's terms. Check your company allows that for its project data.
- **Your keys:** uploading the personal skill stores your keys in your Claude account with it. They sit in a file Claude can open. The skill tells it never to, but anyone or anything with the keys can use Aconex as that user, so bind them to an account that can see only what you want Claude to see. If they get out, ask your Lobby Admin to disable or delete the OAuth client.
- **RevRadar:** drawings go to RevRadar only if you add its connector and say yes when Claude asks. The skill's `upload` command refuses any other address, and any link that doesn't match the comparison ID it's given.
- **Check what Claude tells you:** it can misread a drawing or a mail, so check anything that matters against Aconex before you act on it.

This is an independent project, not made by or affiliated with Oracle or Anthropic. It's provided as is, under the MIT licence, and your company's agreement with Oracle still applies to how you use Aconex. To report a security problem, see [SECURITY.md](SECURITY.md).

## Install and connect

### Claude apps and Cowork

1. Open the [setup page](https://asmblr-com-au.github.io/claude-aconex-skill/) in your browser.
2. Follow the page. It helps you ask your Aconex administrator for keys, then gives you a personal skill zip to upload under Settings → Capabilities → Skills.
3. Ask Claude to "check the Aconex connection". It logs in and lists the projects it can see.

The page needs no install, and it can't send your keys anywhere: your browser blocks it from using the internet once it's open, so they stay on your computer until you upload the skill. To use it offline instead, open [`docs/index.html`](docs/index.html), select **Download raw file**, and open the downloaded file in your browser. It's the same page.

The zip it makes holds your keys, so keep it to yourself and don't share the uploaded skill with your team. Each person makes their own.

### Claude Code

1. Copy `aconex/` to `~/.claude/skills/aconex` (for you) or to `.claude/skills/aconex` in a project (for everyone working on it):

   ```bash
   git clone https://github.com/ASMBLR-COM-AU/claude-aconex-skill.git
   mkdir -p ~/.claude/skills
   cp -r claude-aconex-skill/aconex ~/.claude/skills/aconex
   ```

2. Get base64 credentials (of `client_id:secret`) from your Aconex administrator, and have the email you sign in to Aconex with ready: a USERBOUND API client for the Aconex user Claude should act as. For drafts, the client must be allowed to create mail.
3. Ask Claude to "set up Aconex". It creates `~/.aconex.env` for you to fill in, so you don't paste keys into a chat. It can also use environment variables (`ACONEX_CLIENT_ID`, `ACONEX_CLIENT_SECRET`) or, in Claude Code on the web, a network secret.
4. Ask Claude to "check the Aconex connection".

The skill needs Python 3.8 or later, with nothing to install, and network access to `api.aconex.com` and `constructionandengineering.oraclecloud.com`, plus `app.revradar.com.au` to send drawings to RevRadar.

[`aconex/references/setup.md`](aconex/references/setup.md) covers every environment in detail.

## Develop

```bash
python -m pip install pytest
python -m pytest
```

After changing anything in `aconex/` or `setup/`, run `python setup/build.py` to rebuild the setup page, `docs/index.html`, which GitHub Pages serves. A test fails if you forget.

The tests are offline. [`aconex/references/aconex-api.md`](aconex/references/aconex-api.md) records how the live Aconex API behaves, including the quirks the script works around, and what still hasn't been confirmed live.

## Questions

[Open an issue](https://github.com/ASMBLR-COM-AU/claude-aconex-skill/issues). Issues are public, so leave out keys, names and project details.

## Licence

MIT, copyright ASMBLR. See [LICENSE](LICENSE).
