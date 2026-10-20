# Putting the app online (free, ~5 minutes)

## 1. Make a read-only Hugging Face token
The app only reads data, so give it a token that can't change anything.
1. https://huggingface.co → profile picture → **Settings** → **Access Tokens**.
2. **Create new token** → type **Read** → name it `streamlit-app` → **Create** → copy it.

## 2. Deploy on Streamlit
1. Go to https://share.streamlit.io and **sign in with GitHub**.
2. Click **Create app** → **Deploy a public app from GitHub**.
3. Fill in:
   - **Repository:** `rishabhsrinivasan657-design/citibike-rebalancing`
   - **Branch:** `main`
   - **Main file path:** `app/streamlit_app.py`
   - **App URL:** pick a name, e.g. `citibike-van-planner`
4. Open **Advanced settings**:
   - **Python version:** 3.12
   - **Secrets:** paste this, with your token from step 1:
     ```
     HF_TOKEN = "hf_..."
     ```
5. Click **Deploy**. The first start takes a few minutes while it installs packages.

## If something goes wrong
- "needs an HF_TOKEN secret": the secret is missing. App menu (⋮) → **Settings** → **Secrets**.
- An install error: check that the Python version is 3.12.
- The app sleeps after a few days without visitors; opening it wakes it up (about 30 s).
