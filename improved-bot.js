// improved-bot.js - VERSIÓN MEJORADA con todas las optimizaciones
require('dotenv').config();
const WebSocket = require('ws');

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
const PumpDataFetcher = require('./pump-data-fetcher');
const PumpSDKIntegration = require('./pump-sdk-integration');
const OnChainTokenDetector = require('./onchain-token-detector');

// Estado global
const monitoredTokens = new Map();
const alertedTokens = new Set();
let telegramBot = null;
let tradingEngine = null;
let dataFetcher = null;
let sdkIntegration = null;
let onchainDetector = null;
let stats = { 
  detected: 0, 
  monitored: 0, 
  alerts: 0, 
  filtered: 0,
  trades_executed: 0,
  trades_success: 0,
  trades_failed: 0
};

// Reportes automáticos cada 10 minutos
let reportInterval = null;

// Clase TokenData mejorada
class TokenData {
  constructor({ mint, symbol, name, initialPrice, initialMarketCap, bondingCurve, liquidity }) {
    this.mint = mint;
    this.symbol = symbol || 'UNKNOWN';
    this.name = name || 'UNKNOWN';
    this.initialPrice = initialPrice || 0;
    this.initialMarketCap = initialMarketCap || 0;
    this.initialLiquidity = liquidity || 0;
    this.maxPrice = initialPrice || 0;
    this.currentPrice = initialPrice || 0;
    this.currentMarketCap = initialMarketCap || 0;
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

  get marketCapGainPercent() {
    if (this.initialMarketCap === 0) return 0;
    return ((this.currentMarketCap - this.initialMarketCap) / this.initialMarketCap) * 100;
  }

  get lossFromMaxPercent() {
    if (this.maxPrice === 0) return 0;
    return ((this.currentPrice - this.maxPrice) / this.maxPrice) * 100;
  }

  updatePrice(priceData) {
    this.currentPrice = priceData.price;
    this.currentMarketCap = priceData.marketCap;
    this.priceSource = priceData.source;
    
    if (priceData.liquidity > 0) {
      this.initialLiquidity = priceData.liquidity;
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
    console.log(`[INFO] ${timestamp} - ${msg}`, data ? JSON.stringify(data) : '');
  },
  warn: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.warn(`[WARN] ${timestamp} - ${msg}`, data ? JSON.stringify(data) : '');
  },
  error: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.error(`[ERROR] ${timestamp} - ${msg}`, data ? JSON.stringify(data) : '');
  },
  debug: (msg, data) => {
    if (CONFIG.LOG_LEVEL === 'DEBUG') {
      const timestamp = new Date().toISOString();
      console.log(`[DEBUG] ${timestamp} - ${msg}`, data ? JSON.stringify(data) : '');
    }
  },
  trade: (msg, data) => {
    const timestamp = new Date().toISOString();
    console.log(`🎯 [TRADE] ${timestamp} - ${msg}`, data ? JSON.stringify(data) : '');
  }
};

// Obtener precio usando PumpDataFetcher
async function getCurrentPrice(mint) {
  try {
    const tokenData = await dataFetcher.getTokenData(mint);
    
    if (tokenData && tokenData.price > 0) {
      return {
        price: tokenData.price,
        marketCap: tokenData.marketCap,
        liquidity: tokenData.liquidity,
        bondingCurve: tokenData.bondingCurve,
        source: tokenData.source
      };
    }
    
    return null;
  } catch (error) {
    log.debug(`Error obteniendo precio para ${mint.slice(0, 8)}: ${error.message}`);
    return null;
  }
}

// Alertas Telegram mejoradas
async function sendTelegramAlert(token, alert, tradeResult = null) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) {
    log.info(`🚀 ALERT (sin Telegram): ${token.symbol} +${alert.gainPercent.toFixed(1)}%`);
    return;
  }

  let message = '';

  if (tradeResult) {
    message = `
🎯 *TRADE EJECUTADO* 🎯

*Token:* ${token.name} (${token.symbol})
*Mint:* \`${token.mint}\`
*Acción:* ${tradeResult.action || 'COMPRA'}
*Resultado:* ${tradeResult.success ? '✅ EXITOSO' : '❌ FALLIDO'}
${tradeResult.txHash ? `*TX:* \`${tradeResult.txHash}\`` : ''}
${tradeResult.reason ? `*Razón:* ${tradeResult.reason}` : ''}
${tradeResult.simulated ? `*MODO:* 🧪 DRY_RUN` : ''}
${tradeResult.executionTime ? `*Tiempo:* ${tradeResult.executionTime}ms` : ''}
${tradeResult.retries ? `*Retries:* ${tradeResult.retries}` : ''}

*Métricas:*
• Ganancia: +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min
• Precio: $${alert.priceAtAlert.toFixed(8)}
• Market Cap: $${alert.marketCapAtAlert.toLocaleString()}
• Bonding Curve: ${token.bondingCurve}%
• Fuente Datos: ${token.priceSource}

*Enlaces:*
• [Pump.fun](https://pump.fun/${token.mint})
• [DexScreener](https://dexscreener.com/solana/${token.mint})
• [RugCheck](https://rugcheck.xyz/tokens/${token.mint})
    `.trim();
  } else {
    message = `
🚀 *ALERTA DE MOMENTUM* 🚀

*Token:* ${token.name} (${token.symbol})
*Mint:* \`${token.mint}\`
*Ganancia:* +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min
*Precio:* $${alert.priceAtAlert.toFixed(8)}
*Market Cap:* $${alert.marketCapAtAlert.toLocaleString()}
*Bonding Curve:* ${token.bondingCurve}%
*Fuente:* ${token.priceSource}

*Enlaces:*
• [Pump.fun](https://pump.fun/${token.mint})
• [DexScreener](https://dexscreener.com/solana/${token.mint})
• [RugCheck](https://rugcheck.xyz/tokens/${token.mint})
    `.trim();
  }

  const keyboard = {
    inline_keyboard: [
      [
        { text: '🔥 Pump.fun', url: `https://pump.fun/${token.mint}` },
        { text: '📊 DexScreener', url: `https://dexscreener.com/solana/${token.mint}` }
      ],
      [
        { text: '🛡️ RugCheck', url: `https://rugcheck.xyz/tokens/${token.mint}` },
        { text: '🐦 Birdeye', url: `https://birdeye.so/token/${token.mint}?chain=solana` }
      ]
    ]
  };

  try {
    await telegramBot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, {
      parse_mode: 'Markdown',
      reply_markup: keyboard,
      disable_web_page_preview: true
    });
    log.info(`✅ Alerta Telegram enviada: ${token.symbol}`);
  } catch (error) {
    log.error(`Error enviando alerta Telegram: ${error.message}`);
  }
}

// Monitoreo de token OPTIMIZADO
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
        log.debug(`⚠️ No se pudieron obtener datos para ${token.symbol}, reintentando...`);
        await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
        continue;
      }

      // Actualizar token
      token.updatePrice(priceData);

      // Establecer precio inicial si era 0
      if (token.initialPrice === 0 && priceData.price > 0) {
        token.initialPrice = priceData.price;
        token.maxPrice = priceData.price;
        token.initialMarketCap = priceData.marketCap;
        log.info(`✅ Precio inicial: ${token.symbol} @ $${token.initialPrice.toFixed(8)} | MC: $${token.initialMarketCap.toLocaleString()} (${token.priceSource})`);
      }

      // Detectar dump severo
      if (token.lossFromMaxPercent <= CONFIG.DUMP_THRESHOLD_PERCENT) {
        log.info(`📉 Dump detectado: ${token.symbol} ${token.lossFromMaxPercent.toFixed(1)}% desde máximo`);
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

          log.trade(`🚀 ALERTA: ${token.symbol} +${alert.gainPercent.toFixed(1)}% en ${alert.timeElapsed.toFixed(1)}min`, {
            price: token.currentPrice.toFixed(8),
            marketCap: Math.round(token.currentMarketCap),
            bondingCurve: token.bondingCurve,
            source: token.priceSource
          });

          // EJECUTAR TRADING
          const tradeResult = await tradingEngine.executeBuy(token, alert);
          stats.trades_executed++;
          
          if (tradeResult.success) {
            stats.trades_success++;
            log.trade(`✅ COMPRA EXITOSA: ${token.symbol}`, { 
              txHash: tradeResult.txHash,
              simulated: tradeResult.simulated,
              executionTime: tradeResult.executionTime,
              retries: tradeResult.retries
            });

            // INICIAR MONITOREO PARA VENTA
            tradingEngine.monitorAndSell(mint).catch(err => {
              log.error(`Error en monitoreo de venta: ${err.message}`);
            });

          } else {
            stats.trades_failed++;
            log.trade(`❌ COMPRA FALLIDA: ${token.symbol}`, { 
              reason: tradeResult.reason,
              resetIn: tradeResult.resetIn
            });
          }

          // ENVIAR ALERTA TELEGRAM
          await sendTelegramAlert(token, alert, {
            action: 'BUY',
            success: tradeResult.success,
            txHash: tradeResult.txHash,
            reason: tradeResult.reason,
            simulated: tradeResult.simulated,
            executionTime: tradeResult.executionTime,
            retries: tradeResult.retries
          });
        }
      }

      // Log de progreso cada 10 checks
      if (token.checksCount % 10 === 0) {
        log.debug(`📊 ${token.symbol}: ${token.currentPrice.toFixed(8)} (+${token.gainPercent.toFixed(1)}%) | MC: ${Math.round(token.currentMarketCap)} | ${token.elapsedMinutes.toFixed(1)}min | BC: ${token.bondingCurve}%`);
      }

      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
    }
  } catch (error) {
    log.error(`Error en monitoreo ${mint.slice(0, 8)}: ${error.message}`);
    removeToken(mint, 'error');
    await releaseMonitor(mint);
  }
}

function removeToken(mint, reason) {
  if (monitoredTokens.has(mint)) {
    const token = monitoredTokens.get(mint);
    monitoredTokens.delete(mint);
    log.debug(`🗑️ Removido: ${token.symbol} (${reason})`);
  }
}

// Manejo de nuevos tokens con validación ROBUSTA
async function handleNewToken(data) {
  let mint;
  try {
    await incrStat('detected');
    stats.detected++;

    const payload = data.data || data;
    mint = payload.mint || payload.token;

    if (!mint) {
      log.warn('❌ Token sin mint');
      return;
    }

    // Verificar si ya fue visto (Redis)
    if (!(await seenMint(mint))) {
      log.debug(`⏭️ Ya visto: ${mint.slice(0, 8)}`);
      return;
    }

    // Lock para evitar procesamiento concurrente
    if (!(await lockMonitor(mint))) {
      log.debug(`🔒 Ya monitoreado: ${mint.slice(0, 8)}`);
      return;
    }

    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;

    // OBTENER DATOS REALES con PumpDataFetcher
    log.info(`🔍 Nuevo token detectado: ${symbol} (${mint.slice(0, 8)})`);
    
    const tokenData = await getCurrentPrice(mint);
    
    if (!tokenData) {
      log.warn(`⚠️ No se pudieron obtener datos: ${symbol}`);
      await releaseMonitor(mint);
      return;
    }

    log.info(`📊 Datos obtenidos:`, {
      symbol: symbol,
      price: `${tokenData.price.toFixed(8)}`,
      marketCap: `${Math.round(tokenData.marketCap)}`,
      liquidity: `${Math.round(tokenData.liquidity)}`,
      bondingCurve: `${tokenData.bondingCurve}%`,
      source: tokenData.source
    });

    // FILTROS DE CALIDAD
    const passesFilters = 
      tokenData.price > 0 &&
      tokenData.marketCap > 0 &&
      tokenData.marketCap >= CONFIG.MIN_INITIAL_LIQUIDITY_USD &&
      tokenData.bondingCurve < CONFIG.MAX_BONDING_CURVE_PROGRESS;

    if (!passesFilters) {
      const reason = tokenData.price === 0 ? 'precio inválido' :
                    tokenData.marketCap < CONFIG.MIN_INITIAL_LIQUIDITY_USD ? 'liquidez baja' :
                    tokenData.bondingCurve >= CONFIG.MAX_BONDING_CURVE_PROGRESS ? 'BC alta' :
                    'datos inválidos';
                    
      log.info(`🚫 Filtrado: ${symbol} - ${reason}`, {
        marketCap: `${Math.round(tokenData.marketCap)}`,
        bondingCurve: `${tokenData.bondingCurve}%`,
        minMCap: `${CONFIG.MIN_INITIAL_LIQUIDITY_USD}`,
        maxBC: `${CONFIG.MAX_BONDING_CURVE_PROGRESS}%`
      });
      
      await incrStat('filtered');
      stats.filtered++;
      await releaseMonitor(mint);
      return;
    }

    // Crear token para monitoreo
    const token = new TokenData({
      mint,
      symbol,
      name,
      initialPrice: tokenData.price,
      initialMarketCap: tokenData.marketCap,
      bondingCurve: tokenData.bondingCurve,
      liquidity: tokenData.liquidity
    });

    monitoredTokens.set(mint, token);
    await incrStat('monitored');
    stats.monitored++;

    log.info(`✅ MONITOREANDO: ${symbol}`, {
      price: `${tokenData.price.toFixed(8)}`,
      marketCap: `${Math.round(tokenData.marketCap)}`,
      bondingCurve: `${tokenData.bondingCurve}%`,
      source: tokenData.source
    });

    // Iniciar monitoreo asíncrono
    monitorToken(mint).catch(err => {
      log.error(`Error en tarea de monitoreo ${mint.slice(0, 8)}: ${err.message}`);
      removeToken(mint, 'monitor_error');
      releaseMonitor(mint).catch(() => {});
    });

  } catch (error) {
    log.error(`Error en handleNewToken ${mint ? mint.slice(0, 8) : 'unknown'}: ${error.message}`);
    
    if (mint) {
      try {
        await releaseMonitor(mint);
      } catch {}
    }
  }
}

// Health Server mejorado
function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      const activeTrades = tradingEngine ? tradingEngine.getActiveTrades() : [];
      const engineStats = tradingEngine ? tradingEngine.getStats() : {};
      const fetcherStats = dataFetcher ? dataFetcher.getStats() : {};
      
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        timestamp: new Date().toISOString(),
        websocket_connected: true,
        monitored_tokens: monitoredTokens.size,
        active_trades: activeTrades.length,
        stats: stats,
        engine: engineStats,
        fetcher: fetcherStats,
        sdk_integration: sdkIntegration ? 'active' : 'inactive',
        onchain_detector: onchainDetector ? 'active' : 'inactive'
      }));
    } else if (req.url === '/metrics') {
      const engineStats = tradingEngine ? tradingEngine.getStats() : {};
      
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        monitored_tokens: monitoredTokens.size,
        ...stats,
        performance: engineStats.performance || {},
        circuit_breaker: engineStats.circuitBreaker || {}
      }));
    } else if (req.url === '/report') {
      // Endpoint para reporte detallado
      if (tradingEngine) {
        const report = tradingEngine.getDetailedReport();
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(report, null, 2));
      } else {
        res.writeHead(503, { 'Content-Type': 'text/plain' });
        res.end('Trading engine not initialized');
      }
    } else {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('🤖 Pump.fun ULTIMATE Bot 🚀\n\nEndpoints:\n  /health\n  /metrics\n  /report');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server: http://localhost:${CONFIG.HEALTH_PORT}`);
  });
}

// Reporte periódico automático
function startPeriodicReports() {
  reportInterval = setInterval(() => {
    log.info('📊 ═══════════════════════════════════════════════════════════');
    log.info('📊 REPORTE PERIÓDICO (cada 10 minutos)');
    log.info('📊 ═══════════════════════════════════════════════════════════');
    
    // Stats generales
    log.info(`📈 Tokens detectados: ${stats.detected}`);
    log.info(`✅ Monitoreando: ${monitoredTokens.size}`);
    log.info(`🚫 Filtrados: ${stats.filtered}`);
    log.info(`🚨 Alertas: ${stats.alerts}`);
    log.info(`💼 Trades ejecutados: ${stats.trades_executed} (✅ ${stats.trades_success} | ❌ ${stats.trades_failed})`);
    
    // Trading Engine stats
    if (tradingEngine) {
      const engineStats = tradingEngine.getStats();
      
      log.info('');
      log.info('🎯 TRADING ENGINE:');
      log.info(`  • Trades activos: ${engineStats.activeTrades}`);
      log.info(`  • Circuit Breaker: ${engineStats.circuitBreaker.isOpen ? '🔴 ABIERTO' : '🟢 CERRADO'}`);
      
      if (engineStats.performance.totalTrades > 0) {
        log.info(`  • Success Rate: ${engineStats.performance.successRate}%`);
        log.info(`  • Win Rate: ${engineStats.performance.winRate}%`);
        log.info(`  • Avg Execution Time: ${engineStats.performance.avgExecutionTime}ms`);
        log.info(`  • Avg PnL: ${engineStats.performance.avgPnL}%`);
      }
    }
    
    // Data Fetcher stats
    if (dataFetcher) {
      const fetcherStats = dataFetcher.getStats();
      log.info('');
      log.info('🔍 DATA FETCHER:');
      log.info(`  • Cache size: ${fetcherStats.cacheSize}`);
      log.info(`  • Queue size: ${fetcherStats.queueSize}`);
      log.info(`  • Requests/sec: ${fetcherStats.requestsPerSecond}`);
      log.info(`  • SOL Price: ${fetcherStats.solPrice}`);
    }
    
    // SDK Integration stats
    log.info('');
    log.info('🔗 SDK INTEGRATION:');
    log.info(`  • SDK Status: ${sdkIntegration ? '✅ ACTIVO' : '❌ INACTIVO'}`);
    log.info(`  • On-chain Detector: ${onchainDetector ? '✅ ACTIVO' : '❌ INACTIVO'}`);
    
    log.info('📊 ═══════════════════════════════════════════════════════════');
  }, 600000); // 10 minutos
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// Main
async function main() {
  log.info('🚀 INICIANDO PUMP.FUN ULTIMATE BOT - VERSIÓN MEJORADA');
  log.info('💡 Características: Circuit Breaker, Rate Limiting, Métricas Avanzadas');
  log.info('🔗 Datos on-chain directos de Bonding Curve');
  
  // Validaciones
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ Telegram no configurado - alertas deshabilitadas');
  }

  if (CONFIG.TRADING_MODE === 'LIVE' && !CONFIG.SOLANA_WALLET_PATH && !process.env.SOLANA_PRIVATE_KEY) {
    log.warn('⚠️ MODO LIVE sin wallet - cambiando a DRY_RUN');
    CONFIG.TRADING_MODE = 'DRY_RUN';
    CONFIG.DRY_RUN = true;
  }

  // Verificar RPC
  const rpcUrl = CONFIG.RPC_URL || process.env.HELIUS_RPC_URL;
  if (!rpcUrl || !rpcUrl.includes('helius')) {
    log.warn('⚠️ Sin Helius RPC - velocidad reducida');
    log.warn('📝 Obtén API key en: https://helius.dev');
  } else {
    log.info('✅ Helius RPC configurado');
  }

  // Inicializar componentes
  log.info('🔧 Inicializando componentes...');
  await initDB();
  
  dataFetcher = new PumpDataFetcher(rpcUrl);
  
  // Inicializar SDK (paralelo al WebSocket)
  try {
    sdkIntegration = new PumpSDKIntegration(CONFIG);
    
    // Iniciar detector on-chain
    onchainDetector = new OnChainTokenDetector(
      CONFIG.RPC_URL,
      handleNewToken // Usar el mismo handler
    );
    
    await onchainDetector.start();
    log.info('✅ SDK Pump.fun inicializado (modo híbrido)');
  } catch (error) {
    log.warn('⚠️ SDK no disponible, usando solo WebSocket:', error.message);
  }
  
  // Health check del RPC
  const rpcHealth = await dataFetcher.healthCheck();
  if (rpcHealth.healthy) {
    log.info(`✅ RPC Health Check: OK (latency: ${rpcHealth.latency}ms)`);
  } else {
    log.error(`❌ RPC Health Check: FAILED (${rpcHealth.error})`);
  }
  
  tradingEngine = new TradingEngine(CONFIG);
  telegramBot = setupTelegramBot(monitoredTokens, stats, sendTelegramAlert);
  startHealthServer();
  startPeriodicReports();
  
  // Conectar WebSocket
  connectWebSocket(CONFIG.PUMPPORTAL_WSS, handleNewToken, log);
  
  log.info('✅ BOT INICIADO EXITOSAMENTE!');
  log.info('');
  log.info('═══════════════════════════════════════════════════════════');
  log.info(`📊 Reglas: ${CONFIG.ALERT_RULES.map(r => r.description).join(', ')}`);
  log.info(`🧪 MODO: ${CONFIG.TRADING_MODE}`);
  log.info(`💰 TRADING: ${CONFIG.DRY_RUN ? '🧪 DRY_RUN' : '🔴 LIVE'}`);
  log.info(`🎯 ESTRATEGIA: TreeCityWes (TP 25%/50%, SL -10%, Trailing -15%)`);
  log.info(`📈 FILTROS: MCap>${CONFIG.MIN_INITIAL_LIQUIDITY_USD}, BC<${CONFIG.MAX_BONDING_CURVE_PROGRESS}%`);
  log.info(`🛡️ PROTECCIONES: Circuit Breaker (3 fallos), Rate Limiting (10 req/s)`);
  log.info(`🔗 MODO HÍBRIDO: ${sdkIntegration ? '✅ ACTIVO' : '❌ INACTIVO'}`);
  log.info('═══════════════════════════════════════════════════════════');

  // Verificar balance si es LIVE
  if (CONFIG.TRADING_MODE === 'LIVE') {
    try {
      const balance = await tradingEngine.trading.checkBalance();
      if (balance < CONFIG.MINIMUM_BUY_AMOUNT) {
        log.error(`❌ Balance insuficiente: ${balance} SOL (min: ${CONFIG.MINIMUM_BUY_AMOUNT})`);
      } else {
        log.info(`💰 Balance disponible: ${balance} SOL`);
      }
    } catch (error) {
      log.error(`⚠️ No se pudo verificar balance: ${error.message}`);
    }
  }
  
  // Comando manual para reporte
  log.info('');
  log.info('💡 TIP: Envía SIGUSR1 para reporte detallado: kill -SIGUSR1 ' + process.pid);
}

// Manejo de señales
process.on('SIGTERM', () => {
  log.info('🛑 SIGTERM - apagando...');
  if (reportInterval) clearInterval(reportInterval);
  
  // Cerrar SDK y detector on-chain
  if (onchainDetector) {
    onchainDetector.stop().catch(err => {
      log.error('Error cerrando detector on-chain:', err.message);
    });
  }
  
  // Reporte final
  if (tradingEngine) {
    tradingEngine.getDetailedReport();
  }
  
  process.exit(0);
});

process.on('SIGINT', () => {
  log.info('🛑 SIGINT - apagando...');
  if (reportInterval) clearInterval(reportInterval);
  
  // Cerrar SDK y detector on-chain
  if (onchainDetector) {
    onchainDetector.stop().catch(err => {
      log.error('Error cerrando detector on-chain:', err.message);
    });
  }
  
  // Reporte final
  if (tradingEngine) {
    tradingEngine.getDetailedReport();
  }
  
  process.exit(0);
});

// Señal personalizada para reporte bajo demanda
process.on('SIGUSR1', () => {
  log.info('📊 Reporte solicitado manualmente (SIGUSR1)');
  if (tradingEngine) {
    tradingEngine.getDetailedReport();
  }
  
  if (dataFetcher) {
    const fetcherStats = dataFetcher.getStats();
    log.info('🔍 Fetcher Stats:', fetcherStats);
  }
});

// Manejo de errores no capturados
process.on('unhandledRejection', (reason, promise) => {
  log.error('❌ Unhandled Rejection:', { reason, promise });
});

process.on('uncaughtException', (error) => {
  log.error('❌ Uncaught Exception:', { error: error.message, stack: error.stack });
  
  // Reporte final antes de crash
  if (tradingEngine) {
    try {
      tradingEngine.getDetailedReport();
    } catch {}
  }
  
  process.exit(1);
});

// Iniciar
main().catch(error => {
  log.error(`❌ Error fatal: ${error.message}`);
  console.error(error.stack);
  process.exit(1);
});
