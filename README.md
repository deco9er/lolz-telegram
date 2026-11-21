# Lolz Telegram

[Русская версия](README.ru.md)

Forward new and bumped Lolzteam threads to separate topics in a private Telegram group. Manage subscriptions and check delivery from the bot’s admin chat.

- Search the forum catalog by name or subscribe to sections by ID. The bot creates a Telegram topic for each section and checks the thread’s current section before posting.
- Publications include the title, price when it can be identified, creation time, latest bump time, the source link, and the available text of the first post. Dates use Moscow time.
- SQLite keeps topic assignments, delivery progress, and the queue across restarts. The admin chat shows the remaining Lolzteam API quota and requests by endpoint.

## Run locally

Use Python 3.10 or newer. The bot uses the Python standard library.

~~~bash
cp .env.example .env
~~~

On Windows, use `Copy-Item .env.example .env` in PowerShell.

Fill in `TELEGRAM_BOT_TOKEN` with the token from BotFather and `TELEGRAM_ADMIN_ID` with your numeric Telegram user ID. The bot prompts for either value if it is missing on first launch. Add a Lolzteam forum token with the `read` scope in `LOLZTEAM_TOKEN` or through the Telegram admin menu. [Lolzteam authorization](https://lolzteam.readme.io/reference/oauth2)

Add the bot to a private Telegram supergroup with Topics enabled. Give it administrator access and permission to manage topics. Set `TELEGRAM_GROUP_ID` before first launch, or send `/bind` in the group from your administrator account after launch.

Start the bot with `python3 bot.py` or `python bot.py` on Windows. Open the bot in a private chat, send `/start`, and select sections. The Telegram admin interface is in Russian.

## Settings and delivery

The bot reads `.env` on first launch. It stores settings, credentials, and delivery progress in `data/bot.sqlite3`. Keep `.env` and `data/` private. Stop the bot before backing up the data directory, and use `python bot.py --setup` to recover from a revoked Telegram token.

The default polling interval is 60 seconds and can be changed in the admin chat. Lolzteam's documented base quota is 300 GET requests per 60 seconds. The bot uses the quota reported by the API and pauses until reset when the quota is exhausted. When a thread is bumped repeatedly between checks, the API exposes the latest bump. [Lolzteam API limits](https://lolzteam.readme.io/reference/information#api-rate-limits)
