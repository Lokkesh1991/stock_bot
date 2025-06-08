import os
import json
import datetime
from kiteconnect import KiteConnect
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv()
api_key = os.getenv("KITE_API_KEY")
api_secret = os.getenv("KITE_API_SECRET")

TOKEN_FILE = "token.json"

def get_stored_token():
    """
    Check if the token file exists and return the stored token if it hasn't expired.
    """
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
            token = data.get("access_token")
            expiry_str = data.get("expiry")
            if token and expiry_str:
                expiry = datetime.datetime.fromisoformat(expiry_str)
                if expiry > datetime.datetime.now():
                    print("Using stored access token.")
                    return token
    return None

def store_token(token, expiry):
    """
    Store the access token and its expiry in a JSON file.
    """
    data = {
        "access_token": token,
        "expiry": expiry.isoformat()
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)

def generate_new_token():
    """
    Generate a new access token using the request token.
    """
    kite = KiteConnect(api_key=api_key)
    print("Login URL:", kite.login_url())
    request_token = input("Enter the request token from the URL: ")
    session_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session_data["access_token"]

    # Set expiry to today at 11:59 PM
    today = datetime.date.today()
    expiry = datetime.datetime.combine(today, datetime.time(23, 59))
    store_token(access_token, expiry)
    print("New access token generated and stored.")
    return access_token

def get_access_token():
    """
    Get a valid access token either from storage or by generating a new one.
    """
    token = get_stored_token()
    if token:
        return token
    else:
        print("No valid token found. Generating new access token...")
        return generate_new_token()

