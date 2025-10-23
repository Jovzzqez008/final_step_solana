// bot.js - Bot élite de Pump.fun (Integración completa)
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');

// Configuración y módulos
const CONFIG = require('./config');
const { 
  seenMint, 
  lockMonitor, 
  releaseMonitor, 
  incrStat, 
  getStat,
  setParam,
  getParam 
} = require('./redis');
const { connectWebSocket } = require('./ws');
const { initDB, saveDryRunTrade } = require('./db');
const { simulateBuy, simulateSell, recordDryRunTrade } = require('./simulator');
const { checkEliteRules } = require('./rules');
const { setupTelegramBot } = require('./telegram');

// Estado global
const monitoredTokens = new Map();
const alertedTokens = new Set();
let stats = { 
  detected: 0, 
  monitored: 0, 
  alerts: 0, 
  filtered: 0,
  dryrun_trades: 0,
  dryrun_wins: 0,
  dryrun_losses: 0
};

// Clase TokenData (mejorada para DRY_RUN)
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

    // Campos para DRY_RUN
    this.entryPrice = 0;
    this.tokensHeld = 0;
    this.entryTime = 0;
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

// Logger
const log = {
  info: (msg) => console.log(`[INFO] ${new Date().toISOString()} - ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${new Date().toISOString()} - ${msg}`),
  error: (msg) => console.error(`[ERROR] ${new Date().toISOString()} - ${msg}`),
  debug: (msg) => {
    if (CONFIG.LOG_LEVEL === 'DEBUG') {
      console.log(`[DEBUG] ${new Date().toISOString()} - ${msg}`);
    }
  }
};

// Precio desde DexScreener (mantenemos por ahora)
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

// Alertas por Telegram
async function sendTelegramAlert(token, alert) {
  // Verificar si estamos en silencio
  const silenceUntil = await getParam('silence_until', 0);
  if (Date.now() < silenceUntil) {
    log.info(`🔇 Alert silenced: ${token.symbol}`);
    return;
  }

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
*Slope:* ${alert.slope?.toFixed(6)} USD/min

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

// Monitoreo de token (mejorado con DRY_RUN)
async function monitorToken(mint) {
  const token = monitoredTokens.get(mint);
  if (!token) return;

  try {
    while (monitoredTokens.has(mint)) {
      // Check timeout
      if (token.elapsedMinutes >= CONFIG.MAX_MONITOR_TIME_MIN) {
        log.info(`⏰ Timeout: ${token.symbol} after ${token.elapsedMinutes.toFixed(1)}min`);
        removeToken(mint, 'timeout');
        await releaseMonitor(mint);
        return;
      }

      // Obtener precio actual
      const priceData = await getCurrentPrice(mint);
      if (!priceData || priceData.price === 0) {
        await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
        continue;
      }

      // Actualizar token
      token.currentPrice = priceData.price;
      token.lastChecked = Date.now();
      token.checksCount++;

      if (token.currentPrice > token.maxPrice) {
        token.maxPrice = token.currentPrice;
      }

      // Establecer precio inicial si era 0
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
        await releaseMonitor(mint);
        return;
      }

      // Verificar reglas de alerta
      if (!alertedTokens.has(mint) && token.initialPrice > 0) {
        const alerts = await checkEliteRules(token, CONFIG);
        for (const alert of alerts) {
          alertedTokens.add(mint);
          await incrStat('alerts');
          stats.alerts++;

          // Enviar alerta
          await sendTelegramAlert(token, alert);

          // Si estamos en DRY_RUN, simular compra
          if (CONFIG.DRY_RUN) {
            const sim = simulateBuy(token.currentPrice, CONFIG.TRADE_AMOUNT_SOL, CONFIG.SLIPPAGE_BPS);
            if (sim.failed) {
              log.warn(`🧪 DRY_RUN BUY failed for ${token.symbol}`);
            } else {
              token.entryPrice = sim.execPrice;
              token.tokensHeld = sim.tokensBought;
              token.entryTime = Date.now();
              log.info(`🧪 DRY_RUN BUY: ${token.symbol} @ $${sim.execPrice.toFixed(8)} (fill ${Math.round(sim.partialFill*100)}%)`);

              // Guardar trade de compra
              await recordDryRunTrade({
                type: 'buy',
                mint: token.mint,
                symbol: token.symbol,
                entryPrice: sim.execPrice,
                amountSol: CONFIG.TRADE_AMOUNT_SOL,
                tokens: sim.tokensBought,
                slippageBps: CONFIG.SLIPPAGE_BPS,
                slipFactor: sim.slipFactor,
                partialFill: sim.partialFill,
                liqUsdAtEntry: token.initialMarketCap,
                slopeAtEntry: (token.currentPrice - token.initialPrice) / Math.max(token.elapsedMinutes, 0.1),
                drawdownAtEntry: token.lossFromMaxPercent,
                gainPercentAtEntry: token.gainPercent,
                elapsedMinAtEntry: token.elapsedMinutes,
                rulesMatched: [alert.ruleName]
              });
            }
          }

          log.info(`🚀 ALERT: ${token.symbol} +${alert.gainPercent.toFixed(1)}% in ${alert.timeElapsed.toFixed(1)}min`);
          // No removemos el token porque ahora lo monitoreamos para vender
          // removeToken(mint, 'alert_sent');
          // await releaseMonitor(mint);
          // return;
        }
      }

      // Si tenemos tokens en DRY_RUN, verificar venta
      if (CONFIG.DRY_RUN && token.tokensHeld > 0) {
        // Estrategia de venta simple: take profit + stop loss
        const currentGain = ((token.currentPrice - token.entryPrice) / token.entryPrice) * 100;
        const holdTimeMin = (Date.now() - token.entryTime) / 60000;

        // Take profit: +50% o +100%
        // Stop loss: -20% o timeout (10 min)
        if (currentGain >= 100 || currentGain <= -20 || holdTimeMin >= 10) {
          const sim = simulateSell(token.entryPrice, token.currentPrice, token.tokensHeld, CONFIG.SLIPPAGE_BPS);
          if (sim.failed) {
            log.warn(`🧪 DRY_RUN SELL failed for ${token.symbol}`);
          } else {
            log.info(`🧪 DRY_RUN SELL: ${token.symbol} P&L ${sim.pnlPercent.toFixed(2)}% (fill ${Math.round(sim.partialFill*100)}%)`);

            // Guardar trade de venta
            await recordDryRunTrade({
              type: 'sell',
              mint: token.mint,
              symbol: token.symbol,
              entryPrice: token.entryPrice,
              exitPrice: sim.execPrice,
              amountSol: CONFIG.TRADE_AMOUNT_SOL,
              tokens: sim.tokensSold,
              slippageBps: CONFIG.SLIPPAGE_BPS,
              slipFactor: sim.slipFactor,
              partialFill: sim.partialFill,
              pnlPercent: sim.pnlPercent,
              holdTimeMin: holdTimeMin,
              liqUsdAtEntry: token.initialMarketCap,
              slopeAtEntry: (token.currentPrice - token.initialPrice) / Math.max(token.elapsedMinutes, 0.1),
              drawdownAtEntry: token.lossFromMaxPercent,
              gainPercentAtEntry: token.gainPercent,
              elapsedMinAtEntry: token.elapsedMinutes,
              rulesMatched: [] // Podríamos guardar las reglas que dispararon la compra
            });

            // Resetear posición
            token.tokensHeld = 0;
            token.entryPrice = 0;
            token.entryTime = 0;

            // Remover token después de vender
            removeToken(mint, 'sold');
            await releaseMonitor(mint);
            return;
          }
        }
      }

      // Log progreso
      if (token.checksCount % 10 === 0) {
        log.debug(`📊 ${token.symbol}: $${token.currentPrice.toFixed(8)} (${token.gainPercent.toFixed(1)}%) - ${token.elapsedMinutes.toFixed(1)}min`);
      }

      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
    }
  } catch (error) {
    log.error(`Monitor error for ${mint.slice(0, 8)}: ${error.message}`);
    removeToken(mint, 'error');
    await releaseMonitor(mint);
  }
}

function removeToken(mint, reason) {
  if (monitoredTokens.has(mint)) {
    const token = monitoredTokens.get(mint);
    monitoredTokens.delete(mint);
    log.debug(`🗑️ Removed ${token.symbol} (${mint.slice(0, 8)}): ${reason}`);
  }
}

// Manejo de nuevos tokens
async function handleNewToken(data) {
  try {
    await incrStat('detected');
    stats.detected++;

    const payload = data.data || data;
    const mint = payload.mint || payload.token;

    if (!mint) {
      log.warn('❌ Token without mint, skipping');
      return;
    }

    // Verificar si ya fue visto
    if (!(await seenMint(mint))) {
      log.debug(`⏭️ Token ${mint.slice(0, 8)} already seen`);
      return;
    }

    // Intentar obtener lock de monitoreo
    if (!(await lockMonitor(mint))) {
      log.debug(`🔒 Token ${mint.slice(0, 8)} already being monitored`);
      return;
    }

    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;
    const bondingCurve = payload.bondingCurve || payload.bonding_curve;

    let initialPrice = 0;
    let initialMarketCap = 0;

    // Extraer precio inicial
    if (payload.pairs && Array.isArray(payload.pairs) && payload.pairs.length > 0) {
      const pair = payload.pairs[0];
      initialPrice = parseFloat(pair.priceUsd || pair.price || 0);
      initialMarketCap = parseFloat(pair.marketCap || pair.fdv || 0);
    }

    log.info(`🆕 New token: ${symbol} (${mint.slice(0, 8)})`);

    // Filtro: liquidez mínima
    if (initialMarketCap > 0 && initialMarketCap < CONFIG.MIN_INITIAL_LIQUIDITY_USD) {
      log.info(`🚫 Filtered ${symbol} - Low market cap ($${initialMarketCap.toFixed(0)})`);
      await incrStat('filtered');
      stats.filtered++;
      await releaseMonitor(mint);
      return;
    }

    // Crear token
    const token = new TokenData({
      mint,
      symbol,
      name,
      initialPrice,
      initialMarketCap,
      bondingCurve
    });

    monitoredTokens.set(mint, token);
    await incrStat('monitored');
    stats.monitored++;

    log.info(`✅ Monitoring ${symbol} - Initial price: $${initialPrice.toFixed(8)} | MCap: $${initialMarketCap.toFixed(0)}`);

    // Iniciar monitoreo en background
    monitorToken(mint).catch(err => {
      log.error(`Monitor task failed for ${mint.slice(0, 8)}: ${err.message}`);
    });

  } catch (error) {
    log.error(`Error handling new token: ${error.message}`);
  }
}

// Health server
function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        websocket_connected: true, // Mejorar con estado real del WS
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
      res.end('Pump.fun Elite Bot - OK');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server listening on port ${CONFIG.HEALTH_PORT}`);
  });
}

// Utilidades
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Main
async function main() {
  log.info('🚀 Starting Pump.fun Elite Bot...');
  
  // Validar configuración
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set - alerts will not be sent!');
  }

  // Inicializar base de datos
  await initDB();

  // Iniciar componentes
  setupTelegramBot(monitoredTokens, stats, sendTelegramAlert);
  startHealthServer();
  
  // Conectar WebSocket
  const stopWS = connectWebSocket(CONFIG.PUMPPORTAL_WSS, handleNewToken, log);
  
  log.info('✅ Bot started successfully!');
  log.info(`📊 Monitoring for tokens with +${CONFIG.ALERT_RULES[0].percent}% gains`);
  log.info(`🧪 DRY_RUN: ${CONFIG.DRY_RUN ? 'ENABLED' : 'DISABLED'}`);
}

// Manejo de señales
process.on('SIGTERM', () => {
  log.info('🛑 SIGTERM received, shutting down gracefully...');
  process.exit(0);
});

process.on('SIGINT', () => {
  log.info('🛑 SIGINT received, shutting down gracefully...');
  process.exit(0);
});

// Iniciar bot
main().catch(error => {
  log.error(`Fatal error: ${error.message}`);
  process.exit(1);
});
