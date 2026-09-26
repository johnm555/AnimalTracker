"""One-time interactive FindMy.py account login. Saves session to secrets/findmy_account.json."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

from findmy.reports import AppleAccount, LoginState, RemoteAnisetteProvider

ANISETTE_SERVER = "https://ani.sidestore.io"
SECRETS_DIR = Path(__file__).resolve().parent.parent / "secrets"
ACCOUNT_JSON = SECRETS_DIR / "findmy_account.json"


def main() -> None:
    SECRETS_DIR.mkdir(exist_ok=True)

    # If a saved session exists, try to reuse it
    if ACCOUNT_JSON.exists():
        print(f"Loading existing session from {ACCOUNT_JSON}")
        acc = AppleAccount.from_json(ACCOUNT_JSON)
        print("Session loaded. Checking if still valid...")
        acc.to_json(ACCOUNT_JSON)
        print("Session is valid.")
        return

    print("=== FindMy.py iCloud Login ===")
    anisette = RemoteAnisetteProvider(ANISETTE_SERVER)

    email = input("Apple ID email: ").strip()
    password = getpass.getpass("Password: ")

    acc = AppleAccount(anisette)
    state = acc.login(email, password)

    if state == LoginState.REQUIRE_2FA:
        methods = acc.get_2fa_methods()
        print(f"\n2FA required. Available methods:")
        for i, m in enumerate(methods):
            print(f"  [{i}] {m}")

        # Try trusted device first
        print("\nSending trusted device 2FA request...")
        acc.td_2fa_request()
        code = input("Enter 2FA code from your trusted device: ").strip()
        state = acc.td_2fa_submit(code)

    if state in (LoginState.AUTHENTICATED, LoginState.LOGGED_IN):
        acc.to_json(ACCOUNT_JSON)
        print(f"\nLogin successful! Session saved to {ACCOUNT_JSON}")
        print("This session can be reused for polling without re-authenticating.")
    elif state == LoginState.REQUIRE_2FA:
        print("2FA failed. Try again.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Unexpected login state: {state}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
