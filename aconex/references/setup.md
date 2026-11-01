# Setting up Aconex keys

The skill logs in as one Aconex user, through an Aconex API client: a client ID and secret for a **USERBOUND** client, which is bound to that user. Claude sees exactly the projects, mail and documents that user can see, and drafts it saves land in that user's Drafts folder.

Getting a client is a one-off job for a Lobby Admin at the company, following [Oracle's instructions](https://help.aconex.com/apis/create-an-oauth-client/):

1. Sign in to the Lobby at https://constructionandengineering.oraclecloud.com/ui/v1/login/.
2. From the main menu, open **OAuth Clients** and select **Add**.
3. Enter a name and description. Set **Resource Application** to Aconex APIs and **Type** to User-Bound Integration.
4. Under **User Account**, choose the person who will use Claude. Choose the **Organizations**, accept the terms and select **Add**.
5. Copy the client ID from the list. The eye icon shows the client secret.
6. If the organisation uses them, add the client to its IdP and sign-on policies under **Security**.

The user needs that client ID and secret, and the email they sign in to Aconex with (`ACONEX_USERNAME` in a key file). It only labels which account is in use; the login itself uses the client ID and secret. The setup page converts the ID and secret to the single base64 value (base64 of `client_id:secret`) it asks for. For drafts, the user must also be allowed to create mail on the project.

The script looks for keys in this order and uses the first place that has them:

1. A key file passed with `--key-file PATH`, or named by `ACONEX_KEY_FILE=PATH`.
2. Environment variables: `ACONEX_CLIENT_ID` + `ACONEX_CLIENT_SECRET`, or `ACONEX_CREDENTIALS_B64` (base64 of `client_id:secret`).
3. The key file `~/.aconex.env`.
4. `aconex.env` in the skill's own folder, which is where the setup page puts them.
5. Nothing local. It logs in without sending keys, which works when the environment's network proxy adds them (a network secret; see below).

The older names `ACONEX_USERBOUND_CLIENT_CREDENTIALS_B64`, `ACONEX_USERBOUND_LOBBY_USERNAME`, `ACONEX_USERBOUND_BASE_URL` and `ACONEX_USERBOUND_TOKEN_ENDPOINT` work too, so an existing Aconex integration's `.env` can be used as is.

Never ask the user to paste keys into the chat. Walk them through one of the options below, then run `check`.

## Claude Code on the web (cloud sessions)

**Preferred: a network secret.** The proxy adds the key to requests on the way out, so the session never sees it. The user opens the session's environment menu, chooses **Edit cloud environment**, finds **Network secrets**, and selects **Add secret**:

- **Allowed websites:** `constructionandengineering.oraclecloud.com` (the Aconex login server, not the API)
- **Secret type:**
  - With a combined base64 value: keep the default type and set the header row to Name `Authorization`, Prefix `Basic`, Value = that combined value.
  - With the client ID and secret: choose **Basic** (or **Username and password** on the shorter Team form). Username = client ID, password = client secret.

The environment's network access must also allow `api.aconex.com`, and `app.revradar.com.au` to send drawings to RevRadar for comparison. The login server is reachable automatically once it holds a secret.

**Simpler: environment variables.** Add `ACONEX_CLIENT_ID` and `ACONEX_CLIENT_SECRET` under the environment's **Environment variables**. Anyone who uses the environment can read these. A running session only picks up a changed variable when its VM restarts, so suggest a new session.

## Claude Code on a computer

Set the environment variables in the shell, or run `setup` to create `~/.aconex.env` and have the user fill it in. On Windows the file isn't made private automatically; the user's home folder is the safest place for it.

## Claude apps and Cowork

1. **Keys: the setup page (preferred).** Send the user to the setup page, https://asmblr-com-au.github.io/claude-aconex-skill/. They enter their Aconex email and base64 credentials, and get a personal copy of this skill with the keys inside. They upload that under Settings → Capabilities → Skills, and no attached folder is needed. The page needs no install and can't send the keys anywhere; they stay on the user's computer until the upload, which stores them in their Claude account with the skill. To use it offline, they can download `docs/index.html` from https://github.com/ASMBLR-COM-AU/claude-aconex-skill and open that file instead. That copy holds their keys, so they shouldn't share it with their team. If the keys ever get out, their Lobby Admin should delete the OAuth client.
2. **Network.** Cowork blocks most websites by default. Add `api.aconex.com` and `constructionandengineering.oraclecloud.com` to the allowed domains for code execution, and `app.revradar.com.au` to send drawings to RevRadar for comparison:
   - Pro/Max: the user does this in Settings → Capabilities.
   - Team/Enterprise: an Owner does it in Organization settings → Capabilities.

   Only new sessions pick up the change.
3. **Check.** In a new session, run `check`.

**If they can't upload a personal skill: a key file in a folder.**
- Run `setup --path <their working folder>/aconex.env` and ask them to open the file, fill in the base64 credentials and Aconex email, and save it.
- Then run `check --key-file <that path>`, and pass the same `--key-file` on every command.
- Tell them once: files in that folder are copied to Anthropic's servers while Cowork works with them, and Anthropic's guidance is to keep keys out of shared files.

## Other Aconex setups

API calls go to `https://api.aconex.com/api` and logins to Oracle's `constructionandengineering.oraclecloud.com`. Oracle uses these for all its commercial instances (ASIA1, ASIA2, AU1, CA1, EU1, KSA1, MEA, UK1 and US1), even when the company's Aconex website is a regional one such as `au1.aconex.com`: a USERBOUND token is issued for `api.aconex.com`, so regional website addresses don't work for API calls.

If the company's Aconex administrator says to use different servers, set `ACONEX_API_URL` and `ACONEX_TOKEN_URL` in the environment or the key file. The script only sends keys over https to Aconex's own hosts and Oracle's Aconex login servers, and refuses anything else.

- **A Lobby account linked to more than one Aconex account:** Aconex needs to know which one to use. Set `ACONEX_USER_ID` and `ACONEX_USER_SITE` (the Aconex website with no trailing slash, such as `https://au1.aconex.com`) in the key file, or use that section of the setup page. The company's Aconex administrator can give the user both.
- **AU2 (Australian government):** this has its own Lobby, at `https://constructionandengineering-au2.oraclecloud.com`. Create the client there and set `ACONEX_TOKEN_URL` to `https://constructionandengineering-au2.oraclecloud.com/auth/token`. Oracle's guide doesn't give AU2's API address, so ask Oracle and set `ACONEX_API_URL` to it. This hasn't been tested on AU2.
- **High Compliance instances (USDoD, USGOV):** these don't work. Oracle doesn't offer User-Bound clients on them.

## Troubleshooting

- **"Aconex refused the login (HTTP 400/401)":** the client ID or secret is wrong, or the API client was revoked or rotated.
- **"Aconex can't tell which account to log in to":** the Lobby account is linked to more than one Aconex account. See "Other Aconex setups" above.
- **"... isn't an Aconex server":** a server setting points somewhere the keys mustn't go. Remove it, or correct it if the Aconex administrator gave a different address.
- **"No Aconex keys found":** none of the places above had keys. With a network secret set, check that its host is exactly `constructionandengineering.oraclecloud.com`.
- **"Couldn't reach <host>":** the host isn't on the sandbox's allowed network domains, or the API address is wrong.
- **HTTP 403 on a project or document:** the Aconex user isn't a member of that project, or can't see that document.
- **HTTP 403 or 405 when saving a draft:** the user or API client isn't allowed to create mail on that project. The project's Aconex administrator can allow it.
