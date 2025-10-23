// bot.js - BOT ÉLITE FUSIONADO - Main integrado CON HELIUS
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');

// Módulos
const CONFIG = require('./config');
const { 
  seenMint, 
  lockMonitor, 
  releaseMonitor, 
  incrStat, 
  getStat 
} = require('./redis');
const { connectWebSocket } = require('./ws');
const { initDB } = require('./db');
const { checkEliteRules } = require('./rules');
const { setupTelegramBot } = require('./telegram');
const TradingEngine = require('./trading-engine');

// Estado global
const monitoredTokens = new Map();
const alertedTokens = new Set();
let telegramBot = null;
let tradingEngine = null;
let stats = { 
  detected: 0, 
  monitored: 0, 
  alerts: 0, 
  filtered: 0,
  trades_executed: 0,
  trades_success: 0,
  trades_failed: 0
};

// Clase TokenData mejorada
class TokenData {
  constructor({ mint, symbol, name, initialPrice, initialMarketCap, bondingCurve }) {
    this.mint = mint;
    this.symbol = symbol || 'UNKNOWN';
    this.name = name || 'UNKNOWN';
    this.initialPrice = initialPrice || 0;
    this.initialMarketCap = initialMarketCap || 0;
    this.maxPrice = initialPrice || 0;
    this.currentPrice = initialPrice || 0;
    this.bondingCurve = bondingCurve || 0;
    this.startTime = Date.now();
    this.lastChecked = Date.now();
    this.checksCount = 0;
    this.priceSource = 'unknown';
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

  updatePrice(priceData) {
    this.currentPrice = priceData.price;
    this.priceSource = priceData.source;
    
    if (priceData.marketCap > 0) {
      this.initialMarketCap = priceData.marketCap;
    }
    
    if (priceData.bondingCurve !== undefined) {
      this.bondingCurve = priceData.bondingCurve;
    }
    
    if (this.currentPrice > this.maxPrice) {
      this.maxPrice = this.currentPrice;
    }
    
    this.lastChecked = Date.now();
    this.checksCount++;
  }
}

// Logger mejorado
const log = {
  info: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.log(`[INFO] ${timestamp} - ${msg}`, data || '');
  },
  warn: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.warn(`[WARN] ${timestamp} - ${msg}`, data || '');
  },
  error: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.error(`[ERROR] ${timestamp} - ${msg}`, data || '');
  },
  debug: (msg, data) => {
    if (CONFIG.LOG_LEVEL === 'DEBUG') {
      const timestamp = new Date().toISOString();
      console.log(`[DEBUG] ${timestamp} - ${msg}`, data || '');
    }
  },
  trade: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.log(`🎯 [TRADE] ${timestamp} - ${msg}`, data || '');
  }
};

// Obtener precio MEJORADO con Helius como prioridad
async function getCurrentPrice(mint) {
  // PRIORIDAD 1: Helius + Bonding Curve (datos REALES)
  const { getTokenDataFromBondingCurve } = require('./pump-sdk');
  const pumpData = await getTokenDataFromBondingCurve(mint);
  
  if (pumpData && pumpData.price > 0) {
    return {
      price: pumpData.price,
      marketCap: pumpData.marketCap,
      liquidity: pumpData.liquidity,
      bondingCurve: pumpData.bondingCurve,
      source: pumpData.source || 'helius-bonding-curve'
    };
  }

  // PRIORIDAD 2: API directa de Pump.fun
  try {
    const response = await axios.get(
      `https://frontend-api.pump.fun/coins/${mint}`,
      { timeout: 3000 }
    );
    
    if (response.data && response.data.price_usd) {
      return {
        price: parseFloat(response.data.price_usd || 0),
        marketCap: parseFloat(response.data.market_cap || 0),
        liquidity: parseFloat(response.data.liquidity_usd || 0),
        bondingCurve: parseFloat(response.data.bonding_curve_progress || 0),
        source: 'pump.fun-api'
      };
    }
  } catch (error) {
    log.debug(`Pump.fun API falló para ${mint.slice(0, 8)}: ${error.message}`);
  }

  // PRIORIDAD 3: DexScreener (fallback)
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
      { timeout: 3000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      const pair = response.data.pairs[0];
      
      // Calcular market cap aproximado si no está disponible
      let marketCap = parseFloat(pair.fdv || pair.marketCap || 0);
      if (marketCap === 0 && pair.priceUsd) {
        marketCap = parseFloat(pair.priceUsd) * 1000000; // Asumir 1M supply
      }

      return {
        price: parseFloat(pair.priceUsd || 0),
        marketCap: marketCap,
        liquidity: parseFloat(pair.liquidity?.usd || 0),
        bondingCurve: 0, // No disponible en DexScreener
        source: 'dexscreener'
      };
    }
  } catch (error) {
    log.debug(`DexScreener falló para ${mint.slice(0, 8)}: ${error.message}`);
  }
  
  return null;
}

// Alertas Telegram mejoradas
async function sendTelegramAlert(token, alert, tradeResult = null) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) {
    log.info(`🚀 ALERT (sin Telegram): ${token.symbol} +${alert.gainPercent.toFixed(1)}%`);
    return;
  }

  let message = '';
  let keyboard = {};

  if (tradeResult) {
    // Mensaje de trade ejecutado
    message = `
🎯 *TRADE EJECUTADO* 🎯

*Token:* ${token.name} (${token.symbol})
*Acción:* ${tradeResult.action || 'COMPRA'}
*Resultado:* ${tradeResult.success ? '✅ EXITOSO' : '❌ FALLIDO'}
${tradeResult.txHash ? `*TX:* \`${tradeResult.txHash}\`` : ''}
${tradeResult.reason ? `*Razón:* ${tradeResult.reason}` : ''}
${tradeResult.simulated ? `*MODO:* 🧪 DRY_RUN` : ''}

*Métricas:*
• Ganancia: +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min
• Precio: $${alert.priceAtAlert.toFixed(8)}
• Market Cap: $${alert.marketCapAtAlert.toLocaleString()}
• Bonding Curve: ${token.bondingCurve}%

*Enlaces:*
• [Pump.fun](https://pump.fun/${token.mint})
• [DexScreener](https://dexscreener.com/solana/${token.mint})
    `.trim();

  } else {
    // Mensaje de alerta normal
    message = `
🚀 *ALERTA DE MOMENTUM* 🚀

*Token:* ${token.name} (${token.symbol})
*Mint:* \`${token.mint}\`
*Ganancia:* +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min
*Precio:* $${alert.priceAtAlert.toFixed(8)}
*Market Cap:* $${alert.marketCapAtAlert.toLocaleString()}
*Bonding Curve:* ${token.bondingCurve}%
*Slope:* ${alert.slope?.toFixed(6)} USD/min

*Enlaces rápidos:*
• [Pump.fun](https://pump.fun/${token.mint})
• [DexScreener](https://dexscreener.com/solana/${token.mint})
• [RugCheck](https://rugcheck.xyz/tokens/${token.mint})
    `.trim();
  }

  keyboard = {
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
    log.info(`✅ Alerta enviada: ${token.symbol}`);
  } catch (error) {
    log.error(`Error enviando alerta Telegram: ${error.message}`);
  }
}

// Monitoreo de token FUSIONADO
async function monitorToken(mint) {
  const token = monitoredTokens.get(mint);
  if (!token) return;

  try {
    while (monitoredTokens.has(mint)) {
      // Timeout de monitoreo
      if (token.elapsedMinutes >= CONFIG.MAX_MONITOR_TIME_MIN) {
        log.info(`⏰ Timeout: ${token.symbol} después de ${token.elapsedMinutes.toFixed(1)}min`);
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
      token.updatePrice(priceData);

      // Establecer precio inicial si era 0
      if (token.initialPrice === 0 && priceData.price > 0) {
        token.initialPrice = priceData.price;
        token.maxPrice = priceData.price;
        if (priceData.marketCap > 0) {
          token.initialMarketCap = priceData.marketCap;
        }
        log.info(`✅ Precio inicial establecido: ${token.symbol} @ $${token.initialPrice.toFixed(8)} (${token.priceSource})`);
      }

      // Detectar dump
      if (token.lossFromMaxPercent <= CONFIG.DUMP_THRESHOLD_PERCENT) {
        log.info(`📉 Dump detectado: ${token.symbol} ${token.lossFromMaxPercent.toFixed(1)}%`);
        removeToken(mint, 'dumped');
        await releaseMonitor(mint);
        return;
      }

      // Verificar reglas de alerta + EJECUTAR TRADING
      if (!alertedTokens.has(mint) && token.initialPrice > 0) {
        const alerts = await checkEliteRules(token, CONFIG);
        
        for (const alert of alerts) {
          alertedTokens.add(mint);
          await incrStat('alerts');
          stats.alerts++;

          log.trade(`🚀 ALERTA DISPARADA: ${token.symbol} +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min`, {
            price: `$${token.currentPrice.toFixed(8)}`,
            marketCap: `$${Math.round(token.initialMarketCap)}`,
            bondingCurve: `${token.bondingCurve}%`,
            source: token.priceSource
          });

          // EJECUTAR TRADING
          const tradeResult = await tradingEngine.executeBuy(token, alert);
          stats.trades_executed++;
          
          if (tradeResult.success) {
            stats.trades_success++;
            log.trade(`✅ TRADE EXITOSO: ${token.symbol}`, { 
              txHash: tradeResult.txHash,
              simulated: tradeResult.simulated,
              amount: CONFIG.MINIMUM_BUY_AMOUNT
            });

            // INICIAR MONITOREO PARA VENTA
            tradingEngine.monitorAndSell(mint).catch(err => {
              log.error(`Monitoreo de venta falló: ${err.message}`);
            });

          } else {
            stats.trades_failed++;
            log.trade(`❌ TRADE FALLIDO: ${token.symbol}`, { 
              reason: tradeResult.reason 
            });
          }

          // ENVIAR ALERTA TELEGRAM
          await sendTelegramAlert(token, alert, {
            action: 'BUY',
            success: tradeResult.success,
            txHash: tradeResult.txHash,
            reason: tradeResult.reason,
            simulated: tradeResult.simulated
          });
        }
      }

      // Log de progreso
      if (token.checksCount % 10 === 0) {
        log.debug(`📊 ${token.symbol}: $${token.currentPrice.toFixed(8)} (${token.gainPercent.toFixed(1)}%) - ${token.elapsedMinutes.toFixed(1)}min - BC: ${token.bondingCurve}%`);
      }

      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
    }
  } catch (error) {
    log.error(`Error en monitoreo para ${mint.slice(0, 8)}: ${error.message}`);
    removeToken(mint, 'error');
    await releaseMonitor(mint);
  }
}

function removeToken(mint, reason) {
  if (monitoredTokens.has(mint)) {
    const token = monitoredTokens.get(mint);
    monitoredTokens.delete(mint);
    log.debug(`🗑️ Token removido: ${token.symbol} (${reason})`);
  }
}

// Manejo de nuevos tokens MEJORADO con Helius
async function handleNewToken(data) {
  let mint;
  try {
    await incrStat('detected');
    stats.detected++;

    const payload = data.data || data;
    mint = payload.mint || payload.token;

    if (!mint) {
      log.warn('❌ Token sin mint, saltando');
      return;
    }

    // Verificar si ya fue visto
    if (!(await seenMint(mint))) {
      log.debug(`⏭️ Token ya visto: ${mint.slice(0, 8)}`);
      return;
    }

    // Obtener lock de monitoreo
    if (!(await lockMonitor(mint))) {
      log.debug(`🔒 Token ya siendo monitoreado: ${mint.slice(0, 8)}`);
      return;
    }

    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;
    const bondingCurve = payload.bondingCurve || payload.bonding_curve;

    // OBTENER DATOS REALES desde bonding curve con Helius
    const tokenData = await getCurrentPrice(mint);
    
    let initialPrice = 0;
    let initialMarketCap = 0;
    let initialLiquidity = 0;
    let bondingCurveProgress = 0;
    let dataSource = 'none';

    if (tokenData) {
      initialPrice = tokenData.price;
      initialMarketCap = tokenData.marketCap;
      initialLiquidity = tokenData.liquidity;
      bondingCurveProgress = tokenData.bondingCurve || 0;
      dataSource = tokenData.source;
      
      log.info(`🎯 DATOS REALES: ${symbol} (${mint.slice(0, 8)})`, {
        price: `$${initialPrice.toFixed(8)}`,
        marketCap: `$${Math.round(initialMarketCap)}`,
        liquidity: `$${Math.round(initialLiquidity)}`,
        bondingCurve: `${bondingCurveProgress}%`,
        source: dataSource
      });
    } else {
      log.warn(`⚠️ No se pudieron obtener datos para: ${symbol} (${mint.slice(0, 8)})`);
    }

    // FILTRO MEJORADO - considerar múltiples factores
    const hasValidData = initialPrice > 0 && initialMarketCap > 0;
    const hasSufficientLiquidity = initialMarketCap >= CONFIG.MIN_INITIAL_LIQUIDITY_USD;
    const bondingCurveOK = bondingCurveProgress < CONFIG.MAX_BONDING_CURVE_PROGRESS;

    if (!hasValidData || !hasSufficientLiquidity || !bondingCurveOK) {
      const reason = !hasValidData ? 'datos inválidos' : 
                    !hasSufficientLiquidity ? 'liquidez insuficiente' : 
                    'bonding curve muy alta';
                    
      log.info(`🚫 Filtrado: ${symbol}`, {
        reason: reason,
        marketCap: `$${Math.round(initialMarketCap)}`,
        bondingCurve: `${bondingCurveProgress}%`,
        minLiquidity: `$${CONFIG.MIN_INITIAL_LIQUIDITY_USD}`,
        maxBondingCurve: `${CONFIG.MAX_BONDING_CURVE_PROGRESS}%`
      });
      
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
      bondingCurve: bondingCurveProgress
    });

    monitoredTokens.set(mint, token);
    await incrStat('monitored');
    stats.monitored++;

    log.info(`✅ Monitoreando: ${symbol}`, {
      price: `$${initialPrice.toFixed(8)}`,
      marketCap: `$${Math.round(initialMarketCap)}`,
      bondingCurve: `${bondingCurveProgress}%`,
      source: dataSource
    });

    // Iniciar monitoreo
    monitorToken(mint).catch(err => {
      log.error(`Tarea de monitoreo falló para ${mint.slice(0, 8)}: ${err.message}`);
      removeToken(mint, 'monitor_error');
      releaseMonitor(mint).catch(() => {}); // Ignorar errores de release
    });

  } catch (error) {
    log.error(`Error manejando nuevo token ${mint ? mint.slice(0, 8) : 'unknown'}: ${error.message}`);
    
    // Liberar lock en caso de error
    if (mint) {
      try {
        await releaseMonitor(mint);
      } catch (releaseError) {
        // Ignorar error al liberar
      }
    }
  }
}

// Health Server
function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      const activeTrades = tradingEngine ? tradingEngine.getActiveTrades() : [];
      
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        timestamp: new Date().toISOString(),
        websocket_connected: true,
        monitored_tokens: monitoredTokens.size,
        active_trades: activeTrades.length,
        stats: stats
      }));
    } else if (req.url === '/metrics') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        monitored_tokens: monitoredTokens.size,
        ...stats
      }));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('🤖 Pump.fun Elite Bot - TreeCityWes Strategy 🚀');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server en puerto ${CONFIG.HEALTH_PORT}`);
  });
}

// Utilidades
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Main
async function main() {
  log.info('🚀 INICIANDO PUMP.FUN ELITE BOT...');
  log.info('💡 Fusionado con estrategia TreeCityWes');
  log.info('🔗 Usando Helius RPC para datos reales de bonding curve');
  
  // Validaciones
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ Telegram no configurado - alertas deshabilitadas');
  }

  if (CONFIG.TRADING_MODE === 'LIVE' && !CONFIG.SOLANA_WALLET_PATH) {
    log.warn('⚠️ MODO LIVE pero wallet no configurada - usando DRY_RUN');
    CONFIG.TRADING_MODE = 'DRY_RUN';
    CONFIG.DRY_RUN = true;
  }

  // Verificar configuración Helius
  const rpcUrl = CONFIG.RPC_URL || process.env.HELIUS_RPC_URL;
  if (!rpcUrl || !rpcUrl.includes('helius')) {
    log.warn('⚠️ RPC_URL no parece ser de Helius - la obtención de datos puede ser lenta');
  } else {
    log.info('✅ Helius RPC configurado');
  }

  // Inicializar componentes
  await initDB();
  tradingEngine = new TradingEngine(CONFIG);
  telegramBot = setupTelegramBot(monitoredTokens, stats, sendTelegramAlert);
  startHealthServer();
  
  // Conectar WebSocket
  const stopWS = connectWebSocket(CONFIG.PUMPPORTAL_WSS, handleNewToken, log);
  
  log.info('✅ BOT INICIADO EXITOSAMENTE!');
  log.info(`📊 Monitoreando ganancias de +${CONFIG.ALERT_RULES[0].percent}%`);
  log.info(`🧪 MODO: ${CONFIG.TRADING_MODE}`);
  log.info(`💰 TRADING: ${CONFIG.DRY_RUN ? 'DRY_RUN' : 'LIVE'}`);
  log.info(`🎯 ESTRATEGIA: TreeCityWes (TP 25%/50%, SL -10%, Moon Bag 25%)`);
  log.info(`📈 FILTROS: MCap > $${CONFIG.MIN_INITIAL_LIQUIDITY_USD}, BC < ${CONFIG.MAX_BONDING_CURVE_PROGRESS}%`);

  // Verificar balance si es LIVE
  if (CONFIG.TRADING_MODE === 'LIVE') {
    const balance = await tradingEngine.trading.checkBalance();
    if (balance < CONFIG.MINIMUM_BUY_AMOUNT) {
      log.error(`❌ Balance insuficiente: ${balance} SOL (mínimo: ${CONFIG.MINIMUM_BUY_AMOUNT} SOL)`);
    } else {
      log.info(`💰 Balance disponible: ${balance} SOL`);
    }
  }
}

// Manejo de señales
process.on('SIGTERM', () => {
  log.info('🛑 SIGTERM recibido, apagando...');
  process.exit(0);
});

process.on('SIGINT', () => {
  log.info('🛑 SIGINT recibido, apagando...');
  process.exit(0);
});

// Iniciar bot
main().catch(error => {
  log.error(`❌ Error fatal: ${error.message}`);
  process.exit(1);
});
