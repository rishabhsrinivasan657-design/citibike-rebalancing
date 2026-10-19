# Turning on the 24/7 recorder

This sets up free, always-on recording. GitHub runs the recorder on its computers,
and the data is saved to Hugging Face. It takes about 15 minutes, and you don't
need a credit card.

**Never paste your token into a chat, an email, or a code file.** Only enter it
where these steps say to.

## 1. Make a Hugging Face account and token

1. Sign up for free at https://huggingface.co/join.
2. Click your profile picture (top right) → **Settings** → **Access Tokens**.
3. Click **Create new token**.
   - **Token type:** choose **Write**.
   - **Name:** `citibike-recorder`.
4. Click **Create token** and copy it. You'll paste it in step 3.

## 2. Put the project on GitHub

The project must be a **public** GitHub repository, because GitHub only gives
unlimited free running time to public projects. The code has no passwords in it.

(Claude can do this step for you.)

## 3. Give GitHub the token

In a terminal, inside the project folder, run:

```bash
gh secret set HF_TOKEN
```

It asks you to paste the token. Paste it and press Enter. The text stays hidden.

Then run this to name the dataset. The recorder works out your username from the token:

```bash
gh variable set HF_REPO --body "citibike-station-status"
```

## 4. Start it

```bash
gh workflow run record.yml
```

From now on, a new run also starts by itself every 2 hours.

## 5. Check it's working

- **On GitHub:** go to your repo → the **Actions** tab → **record**. The run should show
  a yellow dot (running) and later a green check.
- **On Hugging Face:** after about 15 minutes, a dataset called
  `citibike-station-status` appears on your profile, with files in a `lake/` folder.
  It's **private** at first. You can make it public later from its Settings page
  once you've read Citi Bike's data license.

## 6. Stop the laptop version

When the first upload appears on Hugging Face, you can turn off the copy running
on your laptop:

```bash
make down
```

## If something goes wrong

- **The run shows a red ✗:** click it to see the error.
  - "HF_REPO and HF_TOKEN must be set" means step 3 was missed.
  - "read-only" means the token isn't a **Write** token. Make a new one.
  - "token belongs to ..." means HF_REPO names a different account. Set it to just
    `citibike-station-status`.
  - A 401 error means the token was copied wrongly. Run `gh secret set HF_TOKEN` again.
- **Files failed to upload:** they're saved on the run's page under **Artifacts**
  for 7 days.
- **Runs stopped starting by themselves:** go to **Actions** → **record** → **Enable workflow**.
