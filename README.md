# KFC Entra User Manager

A focused tool for KFC admins to add new colleagues to Microsoft Entra (Azure
AD). Each invitation flows through the same two-step pattern your team
already uses:

1. Send a Microsoft Graph invitation (creates a **Guest** user).
2. Immediately PATCH `userType` to **Member**.

The display name is set on the invitation, or inferred from the email's local
part if you leave it blank.

The same Python codebase runs in two modes:

| Mode    | Command            | What it does                                    |
| ------- | ------------------ | ----------------------------------------------- |
| Web     | `python app.py`    | Flask server at `http://localhost:5000`         |
| Desktop | `python desktop.py`| Same Flask server wrapped in a native window    |
| Exe     | `pyinstaller kfc_entra_manager.spec` | Bundles `desktop.py` into a single `.exe` |

---

## 1. Create the Entra app registration

In the [Entra admin center](https://entra.microsoft.com):

1. **App registrations -> New registration**
   - Name: `KFC Entra User Manager`
   - Supported account types: *Accounts in this organizational directory only*
   - Redirect URI: select **Public client / native (mobile & desktop)** and
     enter `http://localhost:5000/auth/callback`
2. Open the new registration. Copy the **Application (client) ID** and
   **Directory (tenant) ID** - you'll paste these into `.env`.
3. **Authentication -> Advanced settings**: confirm
   *"Allow public client flows"* is **Yes**.
4. **API permissions -> Add a permission -> Microsoft Graph -> Delegated
   permissions** and add:
   - `User.Invite.All`
   - `User.ReadWrite.All`
   - `Directory.ReadWrite.All`
5. Click **Grant admin consent for KFC**.
6. The signing-in admin needs the **Guest Inviter** + **User Administrator**
   roles (or Global Administrator).

> If you change the port via `PORT` in `.env`, update the redirect URI in
> Entra to match.

---

## 2. Run locally (dev)

```bash
python -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in CLIENT_ID, TENANT_ID, FLASK_SECRET_KEY.

python app.py                      # web mode  -> http://localhost:5000
# or
python desktop.py                  # desktop mode (native window)
```

Sign in with your Entra admin account. You'll land on the dashboard.

---

## 3. Use it

### Add a user
- Click **Add user**.
- Type the email (e.g. `jane.doe@partner.com`).
- Display name is optional - leave blank and the app will turn
  `jane.doe@partner.com` into `Jane Doe`.
- Submit. The app sends the invitation, then promotes the new Guest to
  Member in a single workflow.

### Browse / fix existing users
- **Users** page lists everyone with their type badge (Member / Guest).
- Edit a display name inline and click **Save**.
- Any user still marked **Guest** has a **Make Member** button.

---

## 4. Build the Windows .exe

On a Windows machine with Python 3.11+:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

pyinstaller kfc_entra_manager.spec
```

Result: `dist\KFC Entra User Manager.exe` - a single-file launcher that
opens the app in a native window.

> The exe still needs a `.env` file next to it (or the same env vars set in
> the user's environment) so it knows your client/tenant IDs. Ship `.env`
> via group policy / Intune; do **not** bundle it into the exe.

---

## 5. Architecture

```
app.py            -> Flask web mode entry point
desktop.py        -> pywebview wrapper around Flask
config.py         -> Env-based config loader

kfc_entra/
  auth.py         -> MSAL PublicClientApplication, PKCE auth code flow,
                     session-backed token cache, @login_required decorator
  graph_client.py -> Thin requests wrapper around /users and /invitations
  users.py        -> invite_and_promote() - the two-step "guest -> member"
  web.py          -> Flask blueprints + create_app()
  templates/      -> Jinja2 HTML
  static/css/     -> KFC-red styling
```

Tokens never touch disk - they live in the Flask session cookie (encrypted
with `FLASK_SECRET_KEY`) and are refreshed silently via MSAL's
`acquire_token_silent`.

---

## 6. Security notes

- **Bind to localhost only.** `app.py` and `desktop.py` bind to `127.0.0.1`.
  Don't expose this on a LAN - it would hand your admin token to anyone
  who can hit the port.
- **Public client, no secret.** The app holds no client secret. Auth is
  delegated - every action runs as the signed-in admin.
- **Audit.** Every invite / update is logged in Entra's audit log against
  the signing-in admin.
- **Secret key.** `FLASK_SECRET_KEY` only protects the local session cookie.
  Generate a random one per install:
  `python -c "import secrets; print(secrets.token_hex(32))"`
