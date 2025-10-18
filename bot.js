// bot.js - Pump.fun Monitoring Bot (Node.js + PumpPortal)
// 🚀 Detecta tokens nuevos y alerta cuando suben +100-150%

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');

// ============================================================================
// CONFIG
// ============================================================================

const CONFIG = {
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  
  // PumpPortal
  PUMPPORTAL_WSS: process.env.PUMPPORTAL_WSS || 'wss://pumpportal.fun/api/data',
  
  // Alert Rules
  ALERT_RULES: [
    { name: 'fast_2x', percent: 100, timeWindowMin: 20, description: '2x en 20 minutos' },
    { name: 'momentum_150', percent: 150, timeWindowMin: 15, description: '2.5x en 15 minutos' }
  ],
  
  // Monitoring
  MAX_MONITOR_TIME_MIN: 30,
  DUMP_THRESHOLD_PERCENT: -60,
  PRICE_CHECK_INTERVAL_SEC: 3,
  MIN_INITIAL_LIQUIDITY_USD: 100,
  
  // Health
  HEALTH_PORT: process.env.PORT || 8080
};

// ============================================================================
// GLOBALS
// ============================================================================

const monitoredTokens = new Map(); // mint -> TokenData
const alertedTokens = new Set();   // mints que ya alertaron
let telegramBot = null;
let ws = null;
let stats = { detected: 0, monitored: 0, alerts: 0, filtered: 0 };

// ============================================================================
// LOGGER
// ============================================================================

const log = {
  info: (msg) => console.log(`[INFO] ${new Date().toISOString()} - ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${new Date().toISOString()} - ${msg}`),
  error: (msg) => console.error(`[ERROR] ${new Date().toISOString()} - ${msg}`),
  debug: (msg) => {
    if (process.env.LOG_LEVEL === 'DEBUG') {
      console.log(`[DEBUG] ${new Date().toISOString()} - ${msg}`);
    }
  }
};

// ============================================================================
// TOKEN DATA CLASS
// ============================================================================

class TokenData {
  constructor({ mint, symbol, name, initialPrice, initialMarketCap, bondingCurve }) {
    this.mint = mint;
    this.symbol = symbol || 'UNKNOWN';
    this.name = name || 'UNKNOWN';
    this.initialPrice = initialPrice || 0;
    this.initialMarketCap = initialMarketCap || 0;
    this.maxPrice = initialPrice || 0;
    this.currentPrice = initialPrice || 0;
    this.bondingCurve = bondingCurve;
    this.startTime = Date.now();
    this.lastChecked = Date.now();
    this.checksCount = 0;
  }
  
  get elapsedMinutes() {
    return (Date.now() - this.startTime) / 60000;
  }
  
  get gainPercent() {
    if (this.initialPrice === 0) return 0;
    return ((this.currentPrice - this.initialPrice) / this.initialPrice) * 100;
  }
  
  get lossFromMaxPercent() {
    if (this.maxPrice === 0) return 0;
    return ((this.currentPrice - this.maxPrice) / this.maxPrice) * 100;
  }
}

// ============================================================================
// PRICE FETCHER
// ============================================================================

async function getCurrentPrice(mint) {
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
      { timeout: 5000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      const pair = response.data.pairs[0];
      return {
        price: parseFloat(pair.priceUsd || 0),
        marketCap: parseFloat(pair.fdv || pair.marketCap || 0),
        liquidity: parseFloat(pair.liquidity?.usd || 0)
      };
    }
  } catch (error) {
    log.debug(`DexScreener failed for ${mint.slice(0, 8)}: ${error.message}`);
  }
  
  return null;
}

// ============================================================================
// ALERT ENGINE
// ============================================================================

function checkAlertRules(token) {
  const alerts = [];
  
  for (const rule of CONFIG.ALERT_RULES) {
    if (token.gainPercent >= rule.percent && token.elapsedMinutes <= rule.timeWindowMin) {
      alerts.push({
        ruleName: rule.name,
        description: rule.description,
        gainPercent: token.gainPercent,
        timeElapsed: token.elapsedMinutes,
        priceAtAlert: token.currentPrice,
        marketCapAtAlert: token.currentPrice * (token.initialMarketCap / Math.max(token.initialPrice, 0.0000001))
      });
    }
  }
  
  return alerts;
}

// ============================================================================
// TELEGRAM NOTIFICATION
// ============================================================================

async function sendTelegramAlert(token, alert) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) {
    log.info(`🚀 ALERT (no telegram): ${token.symbol} +${alert.gainPercent.toFixed(1)}%`);
    return;
  }
  
  const message = `
🚀 *ALERTA DE MOMENTUM* 🚀

*Token:* ${token.name} (${token.symbol})
*Mint:* \`${token.mint}\`
*Ganancia:* +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)} min
*Precio inicial:* $${token.initialPrice.toFixed(8)}
*Precio actual:* $${alert.priceAtAlert.toFixed(8)}
*Market Cap:* $${alert.marketCapAtAlert.toLocaleString('en-US', { maximumFractionDigits: 0 })}

📈 *Enlaces rápidos*
• [Pump.fun](https://pump.fun/${token.mint})
• [DexScreener](https://dexscreener.com/solana/${token.mint})
• [RugCheck](https://rugcheck.xyz/tokens/${token.mint})
• [Birdeye](https://birdeye.so/token/${token.mint}?chain=solana)

🕐 Tiempo: ${alert.timeElapsed.toFixed(1)} min desde creación
  `.trim();
  
  const keyboard = {
    inline_keyboard: [
      [
        { text: 'Pump.fun', url: `https://pump.fun/${token.mint}` },
        { text: 'DexScreener', url: `https://dexscreener.com/solana/${token.mint}` }
      ],
      [
        { text: 'RugCheck', url: `https://rugcheck.xyz/tokens/${token.mint}` },
        { text: 'Birdeye', url: `https://birdeye.so/token/${token.mint}?chain=solana` }
      ]
    ]
  };
  
  try {
    await telegramBot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, {
      parse_mode: 'Markdown',
      reply_markup: keyboard,
      disable_web_page_preview: true
    });
    log.info(`✅ Alert sent for ${token.symbol}`);
  } catch (error) {
    log.error(`Telegram send failed: ${error.message}`);
  }
}

// ============================================================================
// TOKEN MONITOR
// ============================================================================

async function monitorToken(mint) {
  const token = monitoredTokens.get(mint);
  if (!token) return;
  
  try {
    while (monitoredTokens.has(mint)) {
      // Check timeout
      if (token.elapsedMinutes >= CONFIG.MAX_MONITOR_TIME_MIN) {
        log.info(`⏰ Timeout: ${token.symbol} after ${token.elapsedMinutes.toFixed(1)}min`);
        removeToken(mint, 'timeout');
        return;
      }
      
      // Get current price
      const priceData = await getCurrentPrice(mint);
      
      if (!priceData || priceData.price === 0) {
        await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
        continue;
      }
      
      // Update token
      token.currentPrice = priceData.price;
      token.lastChecked = Date.now();
      token.checksCount++;
      
      if (token.currentPrice > token.maxPrice) {
        token.maxPrice = token.currentPrice;
      }
      
      // Set initial price if it was 0
      if (token.initialPrice === 0) {
        token.initialPrice = token.currentPrice;
        token.maxPrice = token.currentPrice;
        token.initialMarketCap = priceData.marketCap;
        log.info(`✅ Set initial price for ${token.symbol}: $${token.initialPrice.toFixed(8)}`);
      }
      
      // Check dump
      if (token.lossFromMaxPercent <= CONFIG.DUMP_THRESHOLD_PERCENT) {
        log.info(`📉 Dump detected: ${token.symbol} ${token.lossFromMaxPercent.toFixed(1)}%`);
        removeToken(mint, 'dumped');
        return;
      }
      
      // Check alerts
      if (!alertedTokens.has(mint) && token.initialPrice > 0) {
        const alerts = checkAlertRules(token);
        
        for (const alert of alerts) {
          alertedTokens.add(mint);
          stats.alerts++;
          
          await sendTelegramAlert(token, alert);
          
          log.info(`🚀 ALERT: ${token.symbol} +${alert.gainPercent.toFixed(1)}% in ${alert.timeElapsed.toFixed(1)}min`);
          
          removeToken(mint, 'alert_sent');
          return;
        }
      }
      
      // Log progress periodically
      if (token.checksCount % 10 === 0) {
        log.debug(`📊 ${token.symbol}: $${token.currentPrice.toFixed(8)} (${token.gainPercent.toFixed(1)}%) - ${token.elapsedMinutes.toFixed(1)}min`);
      }
      
      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
    }
  } catch (error) {
    log.error(`Monitor error for ${mint.slice(0, 8)}: ${error.message}`);
    removeToken(mint, 'error');
  }
}

function removeToken(mint, reason) {
  if (monitoredTokens.has(mint)) {
    const token = monitoredTokens.get(mint);
    monitoredTokens.delete(mint);
    log.debug(`🗑️ Removed ${token.symbol} (${mint.slice(0, 8)}): ${reason}`);
  }
}

// ============================================================================
// TOKEN HANDLER (from WebSocket)
// ============================================================================

async function handleNewToken(data) {
  try {
    stats.detected++;
    
    // Extract token info
    const payload = data.data || data;
    const mint = payload.mint || payload.token;
    
    if (!mint) {
      log.warn('❌ Token without mint, skipping');
      return;
    }
    
    // Check if already monitored
    if (monitoredTokens.has(mint)) {
      log.debug(`⏭️ Token ${mint.slice(0, 8)} already monitored`);
      return;
    }
    
    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;
    const bondingCurve = payload.bondingCurve || payload.bonding_curve;
    
    let initialPrice = 0;
    let initialMarketCap = 0;
    
    // Extract initial price from pairs
    if (payload.pairs && Array.isArray(payload.pairs) && payload.pairs.length > 0) {
      const pair = payload.pairs[0];
      initialPrice = parseFloat(pair.priceUsd || pair.price || 0);
      initialMarketCap = parseFloat(pair.marketCap || pair.fdv || 0);
    }
    
    log.info(`🆕 New token: ${symbol} (${mint.slice(0, 8)})`);
    
    // Filter: minimum liquidity
    if (initialMarketCap > 0 && initialMarketCap < CONFIG.MIN_INITIAL_LIQUIDITY_USD) {
      log.info(`🚫 Filtered ${symbol} - Low market cap ($${initialMarketCap.toFixed(0)})`);
      stats.filtered++;
      return;
    }
    
    // Create token
    const token = new TokenData({
      mint,
      symbol,
      name,
      initialPrice,
      initialMarketCap,
      bondingCurve
    });
    
    monitoredTokens.set(mint, token);
    stats.monitored++;
    
    log.info(`✅ Monitoring ${symbol} - Initial price: $${initialPrice.toFixed(8)} | MCap: $${initialMarketCap.toFixed(0)}`);
    
    // Start monitoring in background
    monitorToken(mint).catch(err => {
      log.error(`Monitor task failed for ${mint.slice(0, 8)}: ${err.message}`);
    });
    
  } catch (error) {
    log.error(`Error handling new token: ${error.message}`);
  }
}

// ============================================================================
// WEBSOCKET CLIENT
// ============================================================================

function connectWebSocket() {
  log.info(`🔌 Connecting to PumpPortal WSS...`);
  
  ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
  
  ws.on('open', () => {
    log.info('✅ WebSocket connected');
    
    // Subscribe to new tokens
    ws.send(JSON.stringify({
      method: 'subscribeNewToken'
    }));
    
    log.info('✅ Subscribed to new tokens');
  });
  
  ws.on('message', (data) => {
    try {
      const parsed = JSON.parse(data);
      handleNewToken(parsed);
    } catch (error) {
      log.error(`Failed to parse WebSocket message: ${error.message}`);
    }
  });
  
  ws.on('error', (error) => {
    log.error(`❌ WebSocket error: ${error.message}`);
  });
  
  ws.on('close', () => {
    log.warn('⚠️ WebSocket closed, reconnecting in 5s...');
    setTimeout(connectWebSocket, 5000);
  });
}

// ============================================================================
// TELEGRAM BOT COMMANDS
// ============================================================================

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log.warn('⚠️ TELEGRAM_BOT_TOKEN not set, Telegram bot disabled');
    return;
  }
  
  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const keyboard = {
      inline_keyboard: [
        [
          { text: '📊 Status', callback_data: 'status' },
          { text: '🔍 Tokens', callback_data: 'tokens' }
        ]
      ]
    };
    
    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Monitor Bot*

Detecta tokens cuando suben +100-150% rápidamente.

Usa los comandos:
• /status - Ver estado del bot
• /tokens - Ver tokens monitoreados
    `.trim(), {
      parse_mode: 'Markdown',
      reply_markup: keyboard
    });
  });
  
  telegramBot.onText(/\/status/, (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
    
    const message = `
📊 *Estado del Bot*

• Tokens monitoreados: ${monitoredTokens.size}
• WebSocket: ${wsStatus}
• Detectados: ${stats.detected}
• Alertas enviadas: ${stats.alerts}
• Filtrados: ${stats.filtered}
    `.trim();
    
    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });
  
  telegramBot.onText(/\/tokens/, (msg) => {
    const chatId = msg.chat.id;
    
    if (monitoredTokens.size === 0) {
      telegramBot.sendMessage(chatId, '📭 No hay tokens monitoreados actualmente.');
      return;
    }
    
    let message = '🔍 *Tokens Monitoreados* (Top 15):\n\n';
    
    const tokens = Array.from(monitoredTokens.values()).slice(0, 15);
    for (const token of tokens) {
      const gain = token.gainPercent;
      const icon = gain > 50 ? '🔴' : gain > 0 ? '🟡' : '⚪';
      message += `${icon} ${token.symbol} | ${gain >= 0 ? '+' : ''}${gain.toFixed(1)}% | ${token.elapsedMinutes.toFixed(1)}min\n`;
    }
    
    telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
  });
  
  telegramBot.on('callback_query', (query) => {
    const chatId = query.message.chat.id;
    
    if (query.data === 'status') {
      telegramBot.answerCallbackQuery(query.id);
      const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
      
      const message = `
📊 *Estado del Bot*

• Tokens monitoreados: ${monitoredTokens.size}
• WebSocket: ${wsStatus}
• Detectados: ${stats.detected}
• Alertas enviadas: ${stats.alerts}
• Filtrados: ${stats.filtered}
      `.trim();
      
      telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
    } else if (query.data === 'tokens') {
      telegramBot.answerCallbackQuery(query.id);
      
      if (monitoredTokens.size === 0) {
        telegramBot.sendMessage(chatId, '📭 No hay tokens monitoreados actualmente.');
        return;
      }
      
      let message = '🔍 *Tokens Monitoreados*:\n\n';
      
      const tokens = Array.from(monitoredTokens.values()).slice(0, 10);
      for (const token of tokens) {
        message += `• ${token.symbol} | ${token.gainPercent >= 0 ? '+' : ''}${token.gainPercent.toFixed(1)}% | ${token.elapsedMinutes.toFixed(1)}min\n`;
      }
      
      telegramBot.sendMessage(chatId, message, { parse_mode: 'Markdown' });
    }
  });
  
  log.info('✅ Telegram bot initialized');
}

// ============================================================================
// HEALTH SERVER (for Railway)
// ============================================================================

function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        websocket_connected: ws && ws.readyState === WebSocket.OPEN,
        monitored_tokens: monitoredTokens.size,
        stats
      }));
    } else if (req.url === '/metrics') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        monitored_tokens: monitoredTokens.size,
        ...stats
      }));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('Pump.fun Monitor Bot - OK');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server listening on port ${CONFIG.HEALTH_PORT}`);
  });
}

// ============================================================================
// UTILITIES
// ============================================================================

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ============================================================================
// MAIN
// ============================================================================

async function main() {
  log.info('🚀 Starting Pump.fun Monitor Bot...');
  
  // Validate config
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - alerts will not be sent!');
  }
  
  // Start components
  setupTelegramBot();
  startHealthServer();
  connectWebSocket();
  
  log.info('✅ Bot started successfully!');
  log.info(`📊 Monitoring for tokens with +${CONFIG.ALERT_RULES[0].percent}% gains`);
}

// Start bot
main().catch(error => {
  log.error(`Fatal error: ${error.message}`);
  process.exit(1);
});

// Graceful shutdown
process.on('SIGTERM', () => {
  log.info('🛑 SIGTERM received, shutting down gracefully...');
  if (ws) ws.close();
  process.exit(0);
});

process.on('SIGINT', () => {
  log.info('🛑 SIGINT received, shutting down gracefully...');
  if (ws) ws.close();
  process.exit(0);
});
