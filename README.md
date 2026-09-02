# WOM Role Sync Discord Bot

The WOM Role Sync Discord Bot is a Python-based application that automatically synchronizes roles between a [Wise Old Man (WOM)](https://wiseoldman.net/) group and a Discord server. This allows Old School RuneScape clans to manage their Discord roles based on their members' in-game ranks in the WOM clan.

Bot Website: https://womrolesync.app/

## Features

*   **Automatic Role Synchronization:** Syncs roles every hour, fetching the latest data from the WOM API.
*   **Slash Command Configuration:** Simple server setup with intuitive commands.
*   **Nickname Enforcement:** Option to enforce member nicknames to match their RuneScape Name (RSN).
*   **DM Notifications:** Users can opt-in to receive direct messages when their roles are changed.
*   **Inactivity Reminders:** Admins can be prompted to re-sync their WOM group if it becomes inactive.
*   **Web-based Landing Page:** A simple Flask-based website serves as a landing page for the bot, displaying statistics and setup instructions.

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
    The website will be available at `https://localhost`. You may need to bypass a browser warning due to the self-signed SSL certificate.

3.  **Stopping the services:**
    ```bash
    sudo docker-compose down
    ```

## Available Commands

Server administrators can configure the bot using the following slash commands:

*   `/groupid`: Sets the WOM Group ID for the server.
*   `/linkrole`: Maps a WOM group role to a Discord role.
*   `/unlinkrole`: Removes a role mapping.
*   `/linkuser`: Links a Discord user to their RuneScape Name (RSN).
*   `/nickname`: Toggles enforcement of member nicknames to match their RSN.
*   `/notifyplayers`: Toggles DM notifications for role changes for the whole server.
*   `/reminder`: Configures inactivity reminders.

General commands:

*   `/help`: Displays a setup guide.
*   `/info`: Shows the server's configuration.
*   `/playerlist`: Displays all linked users.
*   `/notifyme`: Toggles personal DM notifications.

## License

This project is open-source and available under the MIT License.
