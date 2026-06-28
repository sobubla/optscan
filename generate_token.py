"""
Fyers Access Token Generator

Run this ONCE per trading day to get a fresh access token.
Fyers tokens expire daily at end-of-day, so you need to regenerate each morning.

Usage:
    python generate_token.py

It will:
1. Print a login URL — open it in your browser
2. Log in with your Fyers credentials + PIN + TOTP
3. After login, you'll be redirected to a page showing 'auth_code' in the URL
4. Copy the auth_code value (the long string after '?auth_code=' and before '&state=')
5. Paste it here when prompted
6. The script will save the access_token automatically to config/settings.py
"""

import sys
import re
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent))

from fyers_apiv3 import fyersModel
from config import settings


def update_settings_file(access_token: str):
    """Write the new access token into config/settings.py automatically."""
    settings_path = Path(__file__).parent / "config" / "settings.py"
    content = settings_path.read_text()

    # Find the line: FYERS_ACCESS_TOKEN = "..."
    new_content = re.sub(
        r'FYERS_ACCESS_TOKEN\s*=\s*"[^"]*"',
        f'FYERS_ACCESS_TOKEN = "{access_token}"',
        content
    )
    settings_path.write_text(new_content)
    print(f"\n✓ Access token saved to {settings_path}")


def main():
    if not settings.FYERS_CLIENT_ID or settings.FYERS_CLIENT_ID == "YOUR_CLIENT_ID-100":
        print("ERROR: Edit config/settings.py first and paste your FYERS_CLIENT_ID and FYERS_SECRET_KEY")
        sys.exit(1)

    print("=" * 70)
    print("FYERS ACCESS TOKEN GENERATOR")
    print("=" * 70)

    # Step 1: Build the login URL
    session = fyersModel.SessionModel(
        client_id=settings.FYERS_CLIENT_ID,
        secret_key=settings.FYERS_SECRET_KEY,
        redirect_uri=settings.FYERS_REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
        state="sample_state",
    )

    login_url = session.generate_authcode()

    print("\nSTEP 1: Open this URL in your browser and log in:\n")
    print(f"  {login_url}\n")
    print("STEP 2: After login, Fyers will redirect you to a page.")
    print("        Look at the browser URL bar. You'll see something like:")
    print("        https://trade.fyers.in/...?auth_code=eyJhbGc...&state=sample_state\n")
    print("STEP 3: Copy ONLY the auth_code value (the long string between")
    print("        'auth_code=' and '&state=')\n")

    # Step 2: Get auth code from user
    auth_code = input("Paste auth_code here: ").strip()
    if not auth_code:
        print("ERROR: No auth_code provided. Aborting.")
        sys.exit(1)

    # Step 3: Exchange auth_code for access_token
    session.set_token(auth_code)
    try:
        response = session.generate_token()
    except Exception as e:
        print(f"ERROR: Token generation failed: {e}")
        sys.exit(1)

    if response.get("s") == "error":
        print(f"ERROR from Fyers: {response.get('message', response)}")
        sys.exit(1)

    access_token = response.get("access_token")
    if not access_token:
        print(f"ERROR: No access_token in response: {response}")
        sys.exit(1)

    print(f"\n✓ Access token generated successfully")
    print(f"  Token: {access_token[:30]}...{access_token[-10:]}")

    # Step 4: Save to settings.py
    update_settings_file(access_token)

    # Step 5: Verify it works
    print("\nVerifying token with a test API call...")
    fyers = fyersModel.FyersModel(
        client_id=settings.FYERS_CLIENT_ID,
        token=access_token,
        log_path="logs/"
    )
    profile = fyers.get_profile()
    if profile.get("s") == "ok":
        print(f"✓ Authenticated as: {profile['data']['name']} ({profile['data']['fy_id']})")
        print("\nYou're all set! Now run:  uvicorn backend.server:app --host 0.0.0.0 --port 8000")
    else:
        print(f"⚠ Profile fetch failed: {profile}")


if __name__ == "__main__":
    main()