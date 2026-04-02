import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def main():
    # Look for the secret file
    if not os.path.exists('client_secret.json'):
        print("❌ ERROR: client_secret.json not found in the main folder!")
        return

    # Force a localhost redirect
    flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES, redirect_uri='http://localhost:8080/')
    
    auth_url, _ = flow.authorization_url(prompt='consent')
    
    print("\n" + "="*50)
    print("🚀 GOOGLE DRIVE HEADLESS AUTHORIZATION")
    print("="*50)
    print("\n1. Copy this link and open it in your computer's web browser:\n")
    print(auth_url)
    print("\n2. Log into your Google Account and click 'Continue/Allow'.")
    print("3. Your browser will try to load a 'localhost' page and will say 'This site can't be reached'. THIS IS EXACTLY WHAT WE WANT!")
    print("4. Look at the address bar in your browser. Copy the ENTIRE URL (it should start with http://localhost:8080/?state=...)")
    
    auth_response = input("\n👉 PASTE THE FULL FAILED LOCALHOST URL HERE: ").strip()
    
    try:
        flow.fetch_token(authorization_response=auth_response)
        
        # Save token to the persistent storage volume
        with open('storage/token.json', 'w') as f:
            f.write(flow.credentials.to_json())
            
        print("\n✅ SUCCESS! token.json has been generated and saved.")
    except Exception as e:
        print(f"\n❌ ERROR generating token: {e}")

if __name__ == '__main__':
    main()
