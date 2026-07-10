// WebSocket connection for real-time updates
let ws = null;
let reconnectTimeout = null;

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
        window.dispatchEvent(new CustomEvent('billy:websocket:disconnected'));
        ws = null;
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
    disconnect: () => {
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
