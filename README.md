# Composer Ticker Canary

A daily early-warning system for Composer traders. It watches every ticker in your strategies and emails you when something is about to go wrong — **before** it breaks your symphonies.

---

## What problem does this solve?

If you trade automated strategies on Composer, your symphonies depend on every ticker they reference being available, tradable, and correctly named on every trading day. When that breaks — even for one ticker — the symphony can fail to rebalance, leaving you stuck in yesterday's positions during a volatile market.

Common ways this breaks:

- **An ETF gets liquidated.** Issuers close ETFs all the time — leveraged and inverse ETFs especially. By the time Composer notices, you're already in trouble.
- **A ticker gets delisted.** The exchange pulls a security; trading stops; your symphony's logic fails silently.
- **A symbol or name change.** The ticker you knew as `XYZ` is now `ABC`; your symphony still says `XYZ`; trades fail.
- **A broker-side restriction.** Alpaca (or Apex) flags a symbol as untradable; orders get rejected.

In all these cases, you usually have **30 to 60 days of advance notice** if you know where to look. The notices appear in:

- SEC EDGAR filings (issuer announcements of liquidations, name changes, reorganizations)
- Alpaca's broker asset list (when broker-side changes happen)
- Other public broker data feeds

This canary checks all of these every weekday morning and emails you a briefing. If anything's wrong, you find out with weeks of runway — not after the fact.

---

## What you get

Every weekday at ~7:17 AM ET (configurable), you receive an email with:

- **Technical regime status** of the SPY index (bull/bear/sideways/volatile + ADX, ER, +DI/-DI)
- **Market internals** (VIX, yield curve spread, SPY daily move)
- **Exception triggers** if any market metric breached a threshold
- **AI risk assessment** (only when exceptions fire — uses Google's Gemini, free tier)
- **Broker status** (any tickers untradable or missing from your broker's database)
- **Ticker advance warning** (any SEC filings or Alpaca asset-list changes affecting your tickers)

The "Ticker advance warning" section is the main reason this exists. The rest is bonus context that's useful daily info if you trade.

---

## How it works (briefly)

The system runs as a GitHub Actions workflow — a free automation that runs daily inside GitHub's servers. You don't run anything on your own computer. You don't need to keep a laptop on. It just runs and sends emails.

It uses three independent sources to catch problems:

1. **SEC EDGAR full-text search** — queries the SEC's free filings database for each of your tickers, looking for liquidation, termination, and name-change announcements in recent filings. Gives 30-60 days advance notice.
2. **Alpaca asset-list diff** — (backup) checks Alpaca's list of tradable securities every day and flags any ticker on your list that disappeared, became untradable, or changed name overnight. Gives same-day notice if something slips past EDGAR.
3. **Alpaca corporate actions feed** — checks for upcoming mergers and spinoffs in the next 14 days affecting your tickers.

If any source flags something, it appears in your email with details and a link to the relevant filing.

---

## Setup time and difficulty

**Setup time:** about 30-45 minutes if you've never done this before. Most of that is creating accounts and copying API keys.

**Difficulty:** non-coder friendly. You'll be pasting things into web forms and clicking buttons. No programming required. No software to install on your computer.

**What you need:**
- A computer with a web browser
- A GitHub account (free — sign up at github.com if you don't have one)
- A Gmail account (for sending and receiving the briefings)
- About 30-45 minutes

---

## Setup walkthrough

### Step 1 — Create your own copy of this repo

This template is set up so you can create your own copy with one click. **Do not edit this repo directly.**

1. Make sure you're logged into GitHub.
2. At the top of this repo's page, find the green **"Use this template"** button. (It's near the top right.)
3. Click it, then choose **"Create a new repository"**.
4. On the next page:
   - **Owner:** your GitHub username
   - **Repository name:** anything you want — `my-canary` is fine
   - **Description:** optional, leave blank or write what you want
   - **Visibility:** choose **Private**. This is important — you'll be putting your trading ticker list in here, and that's information you don't want public.
   - Leave everything else as default.
5. Click **"Create repository"**.

You now have your own copy. Everything from here on happens in your copy, not this template. Keep your copy's tab open — you'll need it.

### Step 2 — Get an Alpaca paper-trading account

Alpaca is the broker whose asset database we'll be reading. They allow free paper-trading accounts, separate from your Composer account. We only need to *read* their public asset metadata, so paper credentials work fine — we don't actually trade through Alpaca with this account.

1. Go to **https://alpaca.markets** and click **Sign Up**.
2. Create an account with your email. (Free, no credit card needed for paper trading.)
3. After you confirm your email and log in, look at the dashboard. By default, you'll be in the Paper Trading view.
4. Find the **API Keys** section (left sidebar or under your profile menu, depending on the version of their UI you see).
5. Click to **generate** new API keys. Alpaca will show you two strings:
   - **API Key ID** (starts with letters/numbers, ~20 chars)
   - **Secret Key** (longer, ~40 chars)
6. **Copy both somewhere safe immediately** — Alpaca will only show the secret key once. If you lose it, you'll need to regenerate.

Keep these for Step 5.

### Step 3 — Get a Gmail app password

Your canary will send the daily briefing email *from your Gmail account to your Gmail account* (you to yourself). For this to work, Gmail needs an "app password" — a special 16-character password just for this script.

You cannot use your regular Gmail password. Google blocks that for security.

1. Go to **https://myaccount.google.com**.
2. Click **Security** in the left sidebar.
3. Look for **2-Step Verification**. If it's not on, you need to turn it on first. Follow Google's prompts to enable it (usually involves your phone). This is required before app passwords are available.
4. Once 2-Step Verification is on, go to **https://myaccount.google.com/apppasswords**.
5. You may be asked to confirm your password.
6. On the App Passwords page:
   - Type a name like `canary` in the "App name" box.
   - Click **Create**.
7. Google shows you a **16-character password** with spaces (like `abcd efgh ijkl mnop`). **Copy this password somewhere safe immediately** — Google only shows it once.
8. **Important:** when you use this password later, you can include or omit the spaces — both work. But copy it exactly as shown to be safe.

Keep this 16-character password for Step 5.

### Step 4 — Get a Google Gemini API key

The canary uses Gemini (Google's AI) to interpret market data when any of the exception triggers fire. The free tier is more than enough — Gemini only gets called on days when something noteworthy happens, which is usually a few times a month at most.

1. Go to **https://aistudio.google.com/app/apikey** (you may need to log in with your Google account).
2. Click **Create API key**.
3. If asked, select or create a Google Cloud project. (Don't worry about this — the free tier doesn't require billing.)
4. Google will show you a key starting with `AIzaSy...` (about 40 characters total).
5. **Copy this key somewhere safe.**

Keep this for Step 5.

### Step 5 — Set up your GitHub Secrets

You now have:
- Alpaca API Key ID
- Alpaca Secret Key
- Your Gmail address
- Your 16-character Gmail app password
- Your Gemini API key
- A real email contact for SEC EDGAR (just use your Gmail address)

These six pieces of information get stored as **encrypted secrets** inside your repo. GitHub Secrets are not visible in your repo's files — they're stored in a vault that only the workflow can read.

1. Go to **your copy of the repo** (not this template).
2. Click the **"Settings"** tab near the top of the repo page (gear icon area, far right of the tab row).
3. In the left sidebar, scroll down and click **"Secrets and variables"**, then click **"Actions"** under it.
4. You'll see a page titled "Actions secrets and variables." Click the green **"New repository secret"** button.
5. You're going to add **six secrets, one at a time**. For each:
   - Type the **Name** exactly as shown below (capital letters, underscores).
   - Paste the **Value** in the box below the name.
   - Click **"Add secret"**.
   - You'll be returned to the secrets list. Click **"New repository secret"** again to add the next one.

| Secret Name | Value |
|---|---|
| `GMAIL_ADDRESS` | Your full Gmail address (e.g., `you@gmail.com`) |
| `GMAIL_APP_PASSWORD` | The 16-character app password from Step 3 |
| `ALPACA_API_KEY` | The API Key ID from Step 2 |
| `ALPACA_SECRET_KEY` | The Secret Key from Step 2 |
| `GEMINI_API_KEY` | The Gemini API key from Step 4 (starts with `AIzaSy...`) |
| `EDGAR_USER_AGENT` | Your name and email together, in the format `Your Name your.email@gmail.com` (the SEC requires this — see note below) |

**Notes on `EDGAR_USER_AGENT`:**

The SEC requires every program that queries their EDGAR database to identify itself with a real, contactable email. They don't validate the email, but they do block requests with no User-Agent or fake-looking ones. Format must be a name and email separated by a space — for example, `Jane Smith jane.smith@gmail.com`. **Do not use a fake or shared email.** If many people use the same fake email and SEC notices, they may block that User-Agent string for everyone.

After all six secrets are added, the secrets list should show six entries. The values are hidden — you'll only see the names. That's correct. They're stored encrypted and the workflow accesses them automatically when it runs.

### Step 6 — Build your `active_tickers.txt`

This file is the list of every ticker your Composer symphonies trade. The canary checks each one daily. Tickers you don't include are not watched.

The file currently has an example list. You need to replace it with **your own** tickers.

#### How to figure out what tickers to include

The fastest way:

1. Open Composer.trade.
2. For each symphony you trade (live or in your draft roster), open the symphony in the Symphony Editor.
3. Find Composer's **"Copy Symphony JSON"** button. Copy the entire JSON code.
4. Open Claude (claude.ai) or another AI in a new tab.
5. Paste the JSON and use a prompt like this:

   > Extract all unique stock ticker symbols from this Composer.trade symphony JSON. Return them comma-separated, sorted alphabetically, with no duplicates. Skip any cash placeholders that start with `$` (like `$USD`).

6. The AI will give you a comma-separated list. Save it.
7. Repeat for every symphony. (Tip: If you don't trade a single "master" symphony, copy all your symphonies into a singe "master" draft symphony, then get that symphony's json code. This allows you to extract all your traded tickers in one shot.) 
8. Combine all the lists into one big list, deduplicated.

#### Updating the file in your repo

1. In your copy of the repo, click on **`active_tickers.txt`** to open it.
2. Click the **pencil icon** (top right of the file view) to edit.
3. Delete everything in the file.
4. Paste in your full ticker list. Format:
   - Comma-separated.
   - Whitespace and newlines around commas are OK.
   - You can put `#` at the start of a line to add a comment.
   - Tickers like `BRK/B` are fine — the script auto-converts the slash to a dot.
5. Scroll down and click **"Commit changes"**.

#### A note on completeness

Be thorough. Every ticker that *could* appear in any symphony's logic — not just the ones currently held — should be in the list. If a symphony has a "rotate to TLT in a downturn" branch, you need TLT in your list even if you've never actually held TLT.

If you miss a ticker, the canary won't watch it, and you won't get advance warning if something goes wrong with it.

### Step 7 — Run the workflow manually for the first time

This first run does two important things: it confirms everything is configured correctly, and it establishes the baseline that future runs compare against.

1. In your repo, click the **"Actions"** tab (top of the repo page).
2. The first time you visit the Actions tab, GitHub may ask you to confirm you want to enable workflows. If you see a green button saying **"I understand my workflows, go ahead and enable them"**, click it.
3. In the left sidebar, you'll see your workflow listed: **"Composer Ticker Canary"**. Click on it.
4. On the right side of the page, you'll see a small notice: **"This workflow has a workflow_dispatch event trigger."** Below it, there's a **"Run workflow"** button. Click it.
5. A small dropdown appears. Leave the default options. Click the green **"Run workflow"** button at the bottom.
6. The page will refresh. You'll see a new entry in the workflow runs list with a yellow circle (in progress).
7. Wait a few minutes - shouldn't take more than 10. The yellow circle will become a green checkmark (success) or red X (failed).

#### If the run succeeded (green checkmark)

- Check your Gmail. You should have a briefing email titled something like "Canary Briefing — ✅ Systems Normal" (or other status).
- The "Ticker Advance Warning" section in the email will say "No EDGAR matches and no Alpaca asset-list changes since last run" — that's expected. The first run establishes the baseline; subsequent runs do real diff comparisons.

You're done. The workflow is now scheduled to run every weekday morning automatically.

#### If the run failed (red X)

Click on the failed run, then click on the **"run-radar"** job, then expand the **"Run Canary Radar"** step. Look at the error message at the bottom. The most common causes:

- **`EDGAR_USER_AGENT environment variable is not set`** — you forgot to set the `EDGAR_USER_AGENT` secret, or you set it as an empty value. Go back to Step 5 and add it.
- **`smtplib.SMTPAuthenticationError`** — your Gmail app password is wrong, or you used your regular Gmail password instead of an app password. Regenerate the app password (Step 3) and update the `GMAIL_APP_PASSWORD` secret.
- **`HTTP 403`** on Alpaca calls — your Alpaca API keys are wrong. Regenerate them in Alpaca's dashboard and update the secrets.

Fix the issue, then click **"Run workflow"** again to retry.

### Step 8 (optional) — Adjust the schedule

By default, the workflow runs Monday-Friday at **11:17 UTC**, which is **7:17 AM US Eastern Time during EDT** (April-October) and **6:17 AM Eastern during EST** (November-March).

If you want a different time, edit the file `.github/workflows/canary.yml`:

1. Click on the file in your repo to open it.
2. Click the pencil icon to edit.
3. Find the line near the top that says `- cron: '17 11 * * 1-5'`.
4. Change the numbers. The format is `MINUTE HOUR * * DAY-OF-WEEK`, all in UTC.
   - Example: `'30 13 * * 1-5'` = 1:30 PM UTC (9:30 AM EDT, before market open)
   - Example: `'0 12 * * 1-5'` = 12:00 PM UTC (8:00 AM EDT)
5. Commit the change.

Note: GitHub Actions may delay scheduled jobs by 5-30 minutes during peak times. If you want delivery before market open, schedule the run at least 30 minutes before you actually need the email.

---

## What this won't catch

Be aware of the limits:

- **Filings that don't mention your ticker exactly.** EDGAR's full-text search relies on exact-phrase matching. If an issuer announces a closure using a strange ticker format or doesn't mention the ticker at all, we miss it. Rare but possible.
- **Pure broker-side surprises with no SEC filing.** If Alpaca decides on their own to restrict a symbol with no underlying corporate event, the asset-list diff catches it but only on the day Alpaca's database updates — usually within a day of the actual change, but not always.
- **Symbol changes where the issuer skipped the standard announcement process.** Most issuers follow SEC rules and pre-announce, but a tiny ETF that quietly rebrands might not produce an EDGAR-searchable result. The Alpaca asset-list diff backstops this — it catches the name change in the broker's metadata.
- **Anything that breaks during US market hours after 7:17 AM.** The canary runs once per day, in the morning. If something breaks at 11 AM, you find out the next morning. For most delistings/liquidations this is fine because they're announced weeks in advance; for rare same-day events, you'll have to find out from Composer directly.

The canary covers maybe 95% of real-world failure modes. The remaining 5% are rare and hard to defend against without paid data sources.

---

## Customization

A few things you might want to change later. All in `canary_monitor.py`:

- **EDGAR lookback window.** Default is 90 days. Change `EDGAR_LOOKBACK_DAYS` if you want to look further back or focus on more recent filings.
- **EDGAR keywords.** Default catches liquidation, termination, name change, reorganization. Add or remove keywords in `EDGAR_KEYWORDS` if you want to broaden or narrow the scan.
- **EDGAR degraded coverage threshold.** Default is 15% — if more than 15% of EDGAR queries fail, you get a warning. Change `EDGAR_DEGRADED_COVERAGE_THRESHOLD`.
- **Market exception thresholds** (VIX level, SPY drop, yield curve, etc.). Change `THRESHOLDS` near the top of the file.

Edit any of these, commit the change, and the next run will use the new values.

---

## Privacy and security

- **All your secrets stay in your private repo.** They're encrypted by GitHub and only the workflow can read them. They are not visible in source code, never appear in workflow logs, and never get committed to git.
- **Your ticker list stays in your private repo.** That's why you should keep your repo set to Private.
- **What data leaves your repo:**
  - Tickers and EDGAR queries → SEC EDGAR (public; identifies you only by your User-Agent)
  - Tickers → Alpaca's API (public asset metadata; uses your API keys)
  - Market data queries → Yahoo Finance (anonymous, no auth)
  - Market data → Google Gemini (only when exception triggers fire; no tickers sent)
  - Daily briefing emails → from your Gmail to your Gmail
- **Nothing gets sent to anyone else.** This canary doesn't phone home. Your data goes only to the services listed above, and only what they need to do their job.

---

## Communities

This tool is intended for traders who use Composer.trade and may benefit from sharing experience or improvements with other Composer users. There are several active Composer trading communities online (Discord servers, Reddit communities, etc.) where users discuss strategies, share canary improvements, and troubleshoot issues. This README does not link to specific communities — they exist, you can find them.

---

## Author and license

Created by Arthur Gueli.

Released under the MIT License. See the LICENSE file for full text.

The MIT License means: do whatever you want with this code, including modify, redistribute, and use commercially — just keep the copyright notice. The software is provided as-is, with no warranty of any kind.

---

## No support

This software is provided as-is. There is no support channel — no email to write to, no issues queue being monitored. The README is the documentation. If something doesn't work and the troubleshooting section doesn't help, you're on your own. You can always ask an AI agent to help troubleshoot.

If you're not comfortable with that, this might not be the right tool for you. If you find bugs and want to fix them, fork it and fix it yourself — that's what the MIT License is for.

---

## Frequently asked questions

**Q: Does this trade for me or change my Composer settings?**
A: No. It only reads data and sends emails. It cannot place trades, modify your Composer symphonies, or do anything else to your account.

**Q: Will this affect my Composer performance?**
A: No. It runs entirely outside Composer. Composer doesn't know it exists.

**Q: How much does it cost to run?**
A: Free. GitHub Actions has a generous free tier for public and private repos. Alpaca paper trading is free. Gmail is free. Gemini's free tier covers the small amount of usage this generates.

**Q: Can I run this without a Gmail account?**
A: Not without modifying the code. The script uses Gmail's SMTP server. If you want to use Outlook or another provider, you'd need to change the SMTP settings in `canary_monitor.py`. That's a small code change but it's a code change.

**Q: How do I add a new ticker later?**
A: Edit `active_tickers.txt` in your repo, add the ticker (anywhere — order doesn't matter), commit. The next run will start watching it.

**Q: How do I remove a ticker?**
A: Edit `active_tickers.txt`, delete it, commit. Done.

**Q: What if SEC EDGAR is down on a particular day?**
A: The canary retries failed requests once, then continues. If a lot of requests fail (more than 15% by default), you get a warning in the email letting you know the day's coverage was degraded. You can re-run the workflow manually later.

**Q: Why does the canary commit files back to my repo every day?**
A: It saves daily state — the previous day's market regime, Alpaca asset snapshot, and a daily intel record — so it can compare against tomorrow's data. This is how the same-day diff detection works.

**Q: Can I use this for trading platforms other than Composer?**
A: Yes. The canary doesn't actually know what Composer is — it just watches a list of tickers. If you trade on any platform and want advance warning of ticker problems, this works. The setup is identical.

**Q: I want to share this with friends. What's the best way?**
A: Send them the link to this template repo. They click "Use this template" to make their own copy, follow this README, and they're set up in 30 minutes with their own private canary. Their setup doesn't depend on yours.
