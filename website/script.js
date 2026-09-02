document.addEventListener('DOMContentLoaded', () => {
    const API_ENDPOINT = '/api/stats'; 
    const MAX_RETRIES = 5;
    const RETRY_DELAY_MS = 2000;

    async function fetchStats(retries = MAX_RETRIES) {
        try {
            const response = await fetch(API_ENDPOINT);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            const data = await response.json();

            document.getElementById('servers').textContent = data.servers;
            document.getElementById('groups').textContent = data.groups;
            document.getElementById('users').textContent = data.users;
            // Format the last sync time to user's local time and desired format
            const lastSyncDate = new Date(data.last_global_sync);
            const formattedLastSync = new Intl.DateTimeFormat(undefined, {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
                hour: 'numeric',
                minute: 'numeric',
                hour12: true // For AM/PM
            }).format(lastSyncDate);

            document.getElementById('last-sync-time').textContent = formattedLastSync;

        } catch (error) {
            console.error('Error fetching statistics.', error);
            document.getElementById('servers').textContent = 'N/A';
            document.getElementById('groups').textContent = 'N/A';
            document.getElementById('users').textContent = 'N/A';
            document.getElementById('last-sync-time').textContent = 'N/A';
        }
    }

    fetchStats();
});
