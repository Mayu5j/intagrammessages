Instagram DM → Telegram Notifier


Monitor Instagram Direct messages from specific people and get a Telegram
notification — but only when you haven't already seen the message
yourself (e.g. you weren't just in that chat).


🇷🇺 Русская версия: README.ru.md




⚠️ Disclaimer: this project uses instagrapi,
an unofficial library that emulates Instagram's mobile app. There is
no official Instagram API for monitoring personal Direct messages.
Using this violates Instagram's Terms of Service and carries a real
risk of temporary blocks or challenge requests. Use at your own risk,
on an account you're okay putting at risk. See Risks below.





Features


Tracks one or several Instagram accounts (by username) at once.
Sends a Telegram message only to you (a single admin chat ID) when they
message you — with their nickname and the content type (text,
photo, video, reel, voice message, story share, etc).
Skips the notification if you already read the message yourself
(e.g. you were actively in that chat when it arrived) — no double
pings for things you've already seen.
Session/device fingerprint persistence, so it doesn't look like a new
"phone" logging in on every run (this matters a lot for avoiding bans).
Optional TOTP two-factor authentication support (no need to disable 2FA).
Optional proxy support.
Survives restarts — remembers the last processed message per contact.


How it works (architecture)


┌─────────────────┐        poll every ~40-80s        ┌──────────────────┐
│  Instagram        │ <──────────────────────────────  │  instagram_dm_   │
│  (via instagrapi)  │ ───────────────────────────────> │  monitor.py      │
└─────────────────┘     DM threads + read state         └─────────┬────────┘
                                                                    │
                                               new & unread message │
                                                                    ▼
                                                          ┌──────────────────┐
                                                          │  Telegram Bot API │
                                                          │  (your admin ID)  │
                                                          └──────────────────┘



The script is a single long-running Python process with one main loop
(main() in instagram_dm_monitor.py):


Login (ig_login()) — logs into Instagram once, saves the session
and device fingerprint to ig_session.json, and reuses it on every
subsequent run/retry so Instagram doesn't see a "new device" each time.
Resolve targets (build_targets()) — for each username in
TARGET_USERNAMES, finds their user_id and opens/creates a DM
thread with them, caching the thread_id.
Poll loop — every ~40-80 seconds (randomized, to avoid a robotic
fixed interval):

Fetches the thread for each tracked contact.
Compares the latest messages against the last processed message ID
(persisted in ig_monitor_state.json, per contact).
For each new message from that contact (not your own replies):

Checks thread.last_seen_at to see if you already saw a
message with an equal-or-later ID than this one (is_already_seen_by_me()).
If you did — i.e. you were already in that chat — the notification
is skipped.
Otherwise, sends a Telegram message: nickname + content type
(send_telegram_message(), content type labels in
CONTENT_TYPE_LABELS).




Sleeps, repeats. On session expiry, re-logs in automatically.


File layout




File
Purpose




instagram_dm_monitor.py
The whole thing — single-file script


ig_session.json
Generated at runtime: Instagram session + device fingerprint. Never commit this — it's equivalent to your password.


ig_monitor_state.json
Generated at runtime: last processed message ID per contact


.env.example
Template for configuration via environment variables


requirements.txt
Python dependencies




Installation


git clone <this-repo-url>
cd <repo>
pip install -r requirements.txt



Configuration


Copy .env.example to .env, fill it in, then load it before running:


cp .env.example .env
# edit .env with your values
export $(grep -v '^#' .env | xargs)
python3 instagram_dm_monitor.py



Or just export the variables directly in your shell — see the table below.




Variable
Required
Description




IG_LOGIN_METHOD
no (default password)
password or session — see Login methods


IG_USERNAME
yes (for password method)
Your Instagram login


IG_PASSWORD
yes (for password method)
Your Instagram password


IG_TOTP_SECRET
only if 2FA via authenticator app is on
The TOTP secret key (not the 6-digit code) — shown once when you set up 2FA


IG_PROXY
no
http://user:pass@host:port or socks5://...


IG_TARGET_USERNAMES
yes
Comma-separated usernames to watch, no @


TG_BOT_TOKEN
yes
Telegram bot token from @BotFather


TG_ADMIN_ID
yes
Your personal numeric Telegram ID (get it from @userinfobot) — the only recipient of notifications




Getting your Telegram bot token & ID


Message @BotFather, /newbot, follow the
prompts, copy the token it gives you into TG_BOT_TOKEN.
Message @userinfobot to get your numeric
Telegram ID, put it in TG_ADMIN_ID.
Send any message to your own bot once (so Telegram allows it to
message you first).


Login methods


The script supports pluggable login methods via IG_LOGIN_METHOD.
See ig_login() in the script — it's written so adding a new method is
just a new if IG_LOGIN_METHOD == "..." branch that returns a logged-in
instagrapi.Client.


password (default)


Standard username + password login. Session and device fingerprint are
cached in ig_session.json after the first successful login and reused
afterwards — this is important: Instagram treats a fresh device
fingerprint on every login attempt as suspicious.


Supports TOTP 2FA (IG_TOTP_SECRET) automatically — no manual code entry,
no need to disable 2FA.


session


Skips password login entirely and expects a ready-made ig_session.json
(an instagrapi settings/session file) to already exist — for example,
one you exported from another script, another machine, or generated
once interactively. If the file is missing, the script fails fast with a
clear error instead of silently falling back to password login.


export IG_LOGIN_METHOD=session
# make sure ig_session.json is present in the working directory
python3 instagram_dm_monitor.py



Adding your own login method


Open instagram_dm_monitor.py, find ig_login(), and add a new branch:


if IG_LOGIN_METHOD == "my_method":
    cl = Client()
    # ... your custom login logic ...
    return cl



Nothing else in the script needs to change — the polling loop, Telegram
notifications, and read-state logic are all login-method-agnostic.


Risks & limitations


Unofficial API. instagrapi reverse-engineers Instagram's private
mobile API. It can break whenever Instagram changes something
server-side, and using it goes against Instagram's Terms of Service.
Ban / challenge risk. Especially if you: poll too frequently, run
multiple copies at once, log in from a flagged datacenter IP, or
generate a new device fingerprint on every attempt (this script avoids
the last one by caching ig_session.json).
No official read-receipt API. The "already seen by me" check relies
on Instagram's internal last_seen_at field, which isn't a documented
public API and could change format without notice.
Credentials are sensitive. ig_session.json is equivalent to a
logged-in session — treat it like a password. Never commit it, never
share logs that contain it, your Instagram password, or your Telegram
bot token.
This project is provided for personal/educational use. You're
responsible for how you use it and for complying with Instagram's and
Telegram's terms of service.


License


MIT — see LICENSE. Use at your own risk.
