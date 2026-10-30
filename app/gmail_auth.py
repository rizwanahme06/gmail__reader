"""One-time Gmail sign-in:  python -m app.gmail_auth
Opens a browser, you approve READ-ONLY access, and token.json is saved."""
from . import gmail_client

if __name__ == "__main__":
    svc = gmail_client._service()
    profile = svc.users().getProfile(userId="me").execute()
    print(f"Connected to {profile['emailAddress']} (read-only). token.json saved.")
