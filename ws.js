// ws.js
const WebSocket = require('ws');
const { enqueueEvent } = require('./queue');

function connectWebSocket(url, onEvent, log = console) {
  let attempt = 0;
  const schedule = [5000, 15000, 45000, 120000];
  let ws;
  let pingInterval;

  function start() {
    log.info(`🔌 Connecting WS: ${url}`);
    ws = new WebSocket(url);

    ws.on('open', () => {
      log.info('✅ WS connected');
      attempt = 0;
      ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
      log.info('✅ Subscribed to new tokens');
      clearInterval(pingInterval);
      pingInterval = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          try { ws.ping(); } catch {}
        }
      }, 25000);
    });

    ws.on('pong', () => log.debug?.('🏓 pong'));
    ws.on('message', (data) => {
      try {
        const parsed = JSON.parse(data);
        enqueueEvent(parsed, onEvent);
      } catch (e) {
        log.error(`Parse error: ${e.message}`);
      }
    });

    ws.on('close', (code) => {
      clearInterval(pingInterval);
      const delay = schedule[Math.min(attempt++, schedule.length - 1)];
      log.warn(`⚠️ WS closed (code=${code}), reconnecting in ${Math.round(delay/1000)}s...`);
      setTimeout(start, delay);
    });

    ws.on('error', (err) => log.error(`WS error: ${err.message}`));
  }

  start();
  return () => {
    try {
      clearInterval(pingInterval);
      ws?.close();
    } catch {}
  };
}

module.exports = { connectWebSocket };
