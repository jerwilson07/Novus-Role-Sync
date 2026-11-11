# WOM Role Sync Discord Bot

The WOM Role Sync Discord Bot is a Python-based application that automatically synchronizes roles between a [Wise Old Man (WOM)](https://wiseoldman.net/) group and a Discord server. This allows Old School RuneScape clans to manage their Discord roles based on their members' in-game ranks in the WOM clan.


## Features

*   **Automatic Role Synchronization:** Syncs current Novus members daily using the WOM API. Historical clan activity is refreshed separately every two hours.
*   **Focused Discord Commands:** Operational commands plus the owner-only `/exportchat` command (hidden by default).
*   **Nickname Enforcement:** Option to enforce member nicknames to match their RuneScape Name (RSN).
*   **DM Notifications:** Users can opt-in to receive direct messages when their roles are changed.
*   **Inactivity Reminders:** Admins can be prompted to re-sync their WOM group if it becomes inactive.

## Getting Started

### Prerequisites

*   Python 3.8+
*   [Docker](https://www.docker.com/) & [Docker Compose](https://docs.docker.com/compose/) (for Docker setup)

### Installation & Configuration (Standard Setup)

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Configure the bot:**
    -   Open the `config.env` file and fill in the required values:
        -   `DISCORD_BOT_TOKEN`: Your Discord bot token.
        -   `WOM_API_KEY`: Your Wise Old Man API key.
        -   `BOT_OWNER_ID`: Your Discord user ID for owner-level commands.

## Running the Bot

### Standard Setup

Once you have configured your `.env` file, you can run the bot with:

```bash
python3 main.py
```

### Docker Setup

1.  **Configure the bot:**
    -   Open the `config.env` file and fill in the required values as described in the standard setup.

2.  **Build and start the services:**
    ```bash
    sudo docker-compose up --build -d
    ```

3.  **Stopping the services:**
    ```bash
    sudo docker-compose down
    ```

## Available Discord Commands

The bot intentionally exposes just four commands for Novus staff:

* `/audit`: Review current Novus WOM accounts and eligible linked Discord members.
* `/syncall`: Sync active Novus members immediately.
* `/autolink`: Match eligible Discord Member-role accounts to the Novus WOM group.
* `/trials`: Review current in-game Trial Members and trial deadlines.

Historical tenure runs automatically, without a staff command. The bot
periodically imports WOM group join and leave activity in small, API-friendly
batches and accumulates separate periods of confirmed Novus membership. Time
spent outside Novus is not credited. The daily rank sync uses the recovered
tenure to grant due promotions while never automatically demoting existing
tenure ranks.

Historical coverage depends on the events WOM actually recorded. The bot
preserves observed periods going forward even if older activity is incomplete.
Group membership timestamps can differ slightly from in-game clan dates.

The original configuration and diagnostic command implementations remain in
the source repository but are not registered as Discord slash commands.

## License

This project is open-source and available under the MIT License.

## Private staff channel export

The `/exportchat` command is restricted in code to Discord user ID `534469721802670093` and to `#council-chat` or `#mod-chat`. Responses and attached JSON files are ephemeral. It exports up to 1000 messages per invocation; use the returned `before` message ID to page through older history. These exports contain private staff conversations, so keep the downloaded files confidential.

**Activation required before using:**

1. In the Discord Developer Portal for this bot, enable **Bot → Privileged Gateway Intents → Message Content Intent**.
2. In Railway, set `EXPORT_MESSAGE_CONTENT_ENABLED=true`, then redeploy. Without both settings, the command will refuse to export, to avoid silently returning empty message bodies. Do not enable the environment flag until the portal intent is enabled or Discord may reject the bot's connection.
3. In **Server Settings → Integrations → the bot → Commands → /exportchat**, grant permission to the owner's user account and deny `@everyone` and unwanted roles. The command has zero default member permissions. Discord administrators may still be able to discover/manage it. A hard-coded user ID check is the definitive execution restriction.
4. Make sure the bot has View Channel, Read Message History, and Attach Files permissions for both staff channels. The bot must have Message Content intent to retrieve message text.

Note: Slash command registration is global and may take time to propagate. No channel message is posted when exporting. Discord audit systems and administrators are not guaranteed to be unaware that the command exists.
