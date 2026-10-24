"""Create the first administrator (run once, by the person operating the installation).

    python scripts/bootstrap_admin.py --email you@example.org

Roles are never self-assigned in NyaySutra; this script is the one exception, because it runs on the server with the
Supabase secret key (an operator action, not a request from the browser). It:

1. creates the Supabase Auth account with a password you type (hidden; checked against the app's password rules),
   marked as confirmed because you are the operator — or, with --invite-only, only records the invitation so you can
   register through the app's sign-up page (that path needs the redirect URL set in the Supabase dashboard);
2. records an administrator invitation for that e-mail that also takes ownership of the matters, chats, drafts and
   imported records created before accounts existed;
3. tells you to sign in at the app and set up two-step verification (required for administrators).

Nothing is printed except the e-mail address and the next steps.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nyaya import accounts, config, db  # noqa: E402
from nyaya import supabase_auth as sb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--invite-only", action="store_true", help="only record the invitation; register through the app")
    args = ap.parse_args()
    if config.auth_mode() != "supabase" or not config.SUPABASE_SECRET_KEY:
        print("Supabase Auth is not configured: set SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY and SUPABASE_SECRET_KEY in .env.")
        return 2
    try:
        email = accounts.norm_email(args.email)
    except accounts.AccountError as exc:
        print(exc)
        return 2
    with db.system():
        db.init()
        if not args.invite_only:
            pw = getpass.getpass("Password for the administrator (hidden): ")
            problems = accounts.password_problems(pw, email)
            if problems:
                print("Password not accepted: " + " ".join(problems))
                return 2
            if getpass.getpass("Repeat the password: ") != pw:
                print("The passwords do not match.")
                return 2
            try:
                sb.admin_create_user(email, pw, args.name or email.split("@")[0], email_confirm=True)
            except sb.AuthError as exc:
                print(f"Supabase refused to create the account: {exc} ({exc.code})")
                return 1
        accounts.create_invitation(db.DEFAULT_ORG, email, "admin", invited_by="bootstrap", claim=True)
        db.audit("bootstrap.admin", "user", None, email=accounts.mask_email(email), invite_only=args.invite_only)
    base = config.PUBLIC_URL or f"http://127.0.0.1:{config.APP_PORT}"
    print(f"\nAdministrator prepared for {email}.")
    print(f"Next: start the app (start.bat), open {base}, " +
          ("register with this address and open the confirmation e-mail in the same browser, then " if args.invite_only
           else "") + "sign in and set up two-step verification with an authenticator app. Keep the recovery codes safe.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        db.close_pool()
    sys.exit(code)
