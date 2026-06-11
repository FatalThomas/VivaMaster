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

## 1. Authentication - user-based, no admin consent

There is **no app registration and no tenant-wide consent step**. The app
signs in via Microsoft's pre-consented first-party public client (the same
one the Azure CLI uses) and requests the delegated
`Directory.AccessAsUser.All` scope. That means:

- The signed-in user sees a normal Microsoft login - **no consent prompt**.
- The app can only do what **that user's own Entra roles** allow. Tokens are
  delegated; there is no app-level standing access.
- Conditional Access, MFA, and sign-in logs all apply as usual, and every
  change is attributed to the signed-in user in the Entra audit log.

Roles the signed-in user needs (assign only what they'll use):

| Action in this app                  | Entra role required                     |
| ----------------------------------- | --------------------------------------- |
| Invite a new user                   | **Guest Inviter** (or User Administrator) |
| Convert Guest -> Member, edit names | **User Administrator**                   |
| Add users to groups / create groups | **Groups Administrator** (or group owner) |

> Optional: if your org prefers its own app registration, set `CLIENT_ID`
> (and `TENANT_ID`) in `.env`. Register `http://localhost:5000` (root path)
> as a *Mobile and desktop applications* redirect URI and add the delegated
> `Directory.AccessAsUser.All` permission - note a custom registration *does*
> need admin consent for that scope; the default first-party client doesn't.

---

## 2. Run locally (dev)

```bash
python -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

python app.py                      # web mode  -> http://localhost:5000
# or
python desktop.py                  # desktop mode (native window)
```

No `.env` needed - sign in with your work account and you'll land on the
dashboard. Copy `.env.example` to `.env` only if you want to pin the tenant,
fix the session key, or use a custom app registration.

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

> The exe works with zero configuration - each user signs in with their own
> work account and gets exactly the access their Entra roles grant. Drop a
> `.env` next to the exe only to pin `TENANT_ID` or use a custom CLIENT_ID.

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
- **Public client, no secret.** The app holds no client secret and no
  app-level permissions. Auth is delegated via `Directory.AccessAsUser.All` -
  every action runs as, and is limited to the roles of, the signed-in user.
- **Least privilege by role.** A user with only Guest Inviter can invite but
  not convert userType; group operations need Groups Administrator or group
  ownership. Graph returns 403s for anything beyond the user's roles, which
  the UI surfaces per-row in bulk operations.
- **Audit.** Every invite / update is logged in Entra's audit log against
  the signing-in user.
- **Secret key.** `FLASK_SECRET_KEY` only protects the local session cookie;
  if unset, a random per-run key is generated (you re-sign-in after restart).
