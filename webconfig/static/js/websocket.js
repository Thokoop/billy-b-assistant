// WebSocket connection for real-time updates
let ws = null;
let reconnectTimeout = null;
let bluetoothStatus = null;
let deviceInfo = null;

// Recovery is confirmed by both workers, not systemd's "active" flag.
// Missing/failed snapshots keep the last notice rather than hiding a problem.
window.updateRecoveryNotice = (notice) => {
    if (!notice || typeof notice.active !== 'boolean') return;
    const configuration = notice.kind === 'configuration';
    const action = configuration
        ? {label: 'Open .env editor', run: () => window.LogPanel?.openEnvEditorModal()}
        : notice.kind === 'startup' ? null
        : {label: 'Open Debug', run: () => window.LogPanel?.openLogPanel()};
    window.NotificationBar?.setPersistent(notice.active ? {
        message: notice.message || 'Settings are awaiting a successful startup.',
        type: 'warning',
        action,
    } : null);
    const editorNotice = document.getElementById('env-recovery-notice');
    if (editorNotice) {
        editorNotice.hidden = !notice.active || !configuration;
        editorNotice.textContent = configuration ? (notice.message || 'Configuration needs attention.') : '';
    }
};

// Seed the same bar from the server before the first live status arrives.
const initialRecoveryNotice = document.getElementById('notification')?.dataset.recoveryNotice;
if (initialRecoveryNotice) {
    try { window.updateRecoveryNotice(JSON.parse(initialRecoveryNotice)); }
    catch (error) { console.warn('Could not read initial recovery status', error); }
}

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        console.log('WebSocket connected');
        window.dispatchEvent(new CustomEvent('billy:websocket:connected'));
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
    };
    
    ws.onmessage = (event) => {
        try {
            const message = JSON.parse(event.data);
            
            if (message.type === 'status') {
                const statusData = message.data;
                if (statusData && typeof statusData === 'object') {
                    window.updateRecoveryNotice(statusData.recovery);
                    if (statusData.bluetooth && typeof statusData.bluetooth === 'object') {
                        bluetoothStatus = statusData.bluetooth;
                        window.dispatchEvent(new CustomEvent('billy:bluetooth:status', {detail: bluetoothStatus}));
                    }
                    if (statusData.device_info && typeof statusData.device_info === 'object') {
                        deviceInfo = statusData.device_info;
                        window.dispatchEvent(new CustomEvent('billy:device-info:status', {detail: deviceInfo}));
                    }
                }

                // Update service status display
                if (window.updateServiceStatus) {
                    window.updateServiceStatus(statusData.status || statusData);
                }
                
                // Handle full status updates (persona/personality)
                if (window.handleStatusUpdate && typeof statusData === 'object') {
                    window.handleStatusUpdate(statusData);
                }
            } else if (message.type === 'logs') {
                // Update logs display
                if (window.updateLogs) {
                    window.updateLogs(message.data);
                }
            }
        } catch (e) {
            console.error('Error parsing WebSocket message:', e);
        }
    };
    
    ws.onerror = (error) => {
        if (window.LoadingOverlay?.shouldReloadOnReconnect?.()) {
            return;
        }
        console.error('WebSocket error:', error);
    };
    
    ws.onclose = () => {
        bluetoothStatus = null;
        deviceInfo = null;
        ws = null;
        window.dispatchEvent(new CustomEvent('billy:websocket:disconnected'));
        if (window.LoadingOverlay?.shouldReloadOnReconnect?.()) {
            window.dispatchEvent(new CustomEvent('billy:restart-unavailable'));
            console.log('WebSocket disconnected during restart; waiting for interface reload.');
            return;
        }
        console.log('WebSocket disconnected, reconnecting in 3s...');
        reconnectTimeout = setTimeout(connectWebSocket, 3000);
    };
}

// Auto-connect on page load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', connectWebSocket);
} else {
    connectWebSocket();
}

// Export for use in other scripts
window.billyWebSocket = {
    connect: connectWebSocket,
    // Only a received Bluetooth snapshot proves the backend supports this.
    // Older backends and installations without WebSockets retain HTTP fallback.
    getBluetoothStatus: () => ws?.readyState === WebSocket.OPEN ? bluetoothStatus : null,
    getDeviceInfo: () => ws?.readyState === WebSocket.OPEN ? deviceInfo : null,
    disconnect: () => {
        bluetoothStatus = null;
        deviceInfo = null;
        if (ws) {
            ws.close();
            ws = null;
        }
        if (reconnectTimeout) {
            clearTimeout(reconnectTimeout);
            reconnectTimeout = null;
        }
    }
};
