# One-time FYERS API v3 access-token generator

This repository includes a standalone helper, `fyers_access_token_generator.py`, for creating a FYERS API v3 access token that you can save as the GitHub Secret `FYERS_ACCESS_TOKEN`.

The helper is intentionally separate from the CoinDCX scanner. It does **not** automate trading, place orders, modify scanner behavior, or change any existing workflow.

## Required environment variables

Set these variables locally before running the helper:

- `FYERS_APP_ID`: your FYERS API app/client ID, for example an ID ending in `-100`.
- `FYERS_SECRET_KEY`: your FYERS app secret key.
- `FYERS_REDIRECT_URI`: the exact redirect URI configured in your FYERS app.

## Usage

1. Install Python dependencies if needed:

   ```bash
   pip install -r requirement.txt
   ```

2. Export the required FYERS credentials in your local shell:

   ```bash
   export FYERS_APP_ID="your-app-id"
   export FYERS_SECRET_KEY="your-secret-key"
   export FYERS_REDIRECT_URI="your-redirect-uri"
   ```

3. Run the one-time token generator:

   ```bash
   python fyers_access_token_generator.py
   ```

4. Copy the login/authorization URL printed by the script and open it in your browser.

5. Complete the FYERS login and authorization in the browser.

6. After FYERS redirects you to your redirect URI, copy the full redirected URL from the browser address bar.

7. Re-run the helper with `FYERS_REDIRECTED_URL` set to that full redirected URL:

   ```bash
   FYERS_REDIRECTED_URL="https://your-redirect-uri?auth_code=..." python fyers_access_token_generator.py
   ```

8. The helper extracts the `auth_code` locally, exchanges it with FYERS through the official `fyers-apiv3` SDK flow, and writes the final access token to `new_token.txt`.

9. Copy the access token from `new_token.txt` immediately and save it as the GitHub Secret named `FYERS_ACCESS_TOKEN`.

## Security notes

- The helper never prints or logs `FYERS_SECRET_KEY`.
- The helper never prints or logs the extracted `auth_code`.
- The final FYERS access token is written to `new_token.txt`; delete this file after saving the token in GitHub Secrets.
- Run this only on a trusted local machine and avoid saving terminal logs that contain the final access token.
