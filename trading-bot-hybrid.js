// trading-bot-hybrid.js - Pump.fun Trading Bot Automatizado
// 🚀 Bot completo de trading con stop-loss híbrido y take profit
// 💰 Optimizado para operar automáticamente en pump.fun

const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, Keypair, VersionedTransaction } = require('@solana/web3.js');
const bs58 = require('bs58');

// ============================================================================
// CONFIGURACIÓN
// ============================================================================

const CONFIG = {
  // 🔐 Telegram (REQUERIDO para recibir alertas)
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  
  // 💼 Wallet de Solana (REQUERIDO para trading)
  // ⚠️ USA UNA WALLET NUEVA SOLO PARA EL BOT
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY || '', // Base58 format
  
  // 🌐 RPC de Solana
  // Para mejor rendimiento usa un RPC de pago (Helius, QuickNode, etc)
  SOLANA_RPC: process.env.SOLANA_RPC || 'https://api.mainnet-beta.solana.com',
  
  // 📡 PumpPortal API
  PUMPPORTAL_WSS: 'wss://pumpportal.fun/api/data',
  PUMPPORTAL_API: 'https://pumpportal.fun/api/trade-local',
  
  // ════════════════════════════════════════════════════════════════════════
  // 💰 ESTRATEGIA DE TRADING
  // ════════════════════════════════════════════════════════════════════════
  
  // Monto por operación
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.03'), // ~$5 USD
  
  // 🛡️ STOP LOSS (Protección de capital)
  HARD_STOP_LOSS_PERCENT: -40,        // Si cae -40% → VENDE TODO
  TRAILING_STOP_ACTIVATION: 30,       // Activa trailing al +30%
  TRAILING_STOP_PERCENT: -25,         // Vende si cae -25% desde máximo
  
  // 💚 TAKE PROFIT (Objetivos de ganancia)
  TAKE_PROFIT_TARGETS: [
    { percent: 100, sellPercent: 50 },  // +100%: vende 50%
    { percent: 200, sellPercent: 30 },  // +200%: vende 30% más
    { percent: 500, sellPercent: 100 }  // +500%: vende todo restante
  ],
  
  // ⏱️ Límite de tiempo
  MAX_HOLD_TIME_MIN: 15,               // Vende después de 15 minutos
  
  // ════════════════════════════════════════════════════════════════════════
  // 🛡️ FILTROS DE SEGURIDAD
  // ════════════════════════════════════════════════════════════════════════
  
  MIN_INITIAL_LIQUIDITY_USD: 500,      // Mínimo $500 de liquidez
  MAX_CONCURRENT_POSITIONS: 3,         // Máximo 3 tokens simultáneos
  MIN_PRICE_USD: 0.00000001,           // Evita tokens con precio 0
  
  // Filtro de velocidad (opcional)
  MIN_BUY_DELAY_MS: 5000,              // Espera 5s después de detección
  
  // ════════════════════════════════════════════════════════════════════════
  // ⚙️ CONFIGURACIÓN DE EJECUCIÓN
  // ════════════════════════════════════════════════════════════════════════
  
  TRADING_ENABLED: process.env.TRADING_ENABLED === 'true', // Debe ser 'true'
  DRY_RUN: process.env.DRY_RUN !== 'false',                // Default: simular
  
  SLIPPAGE: 30,                        // 30% slippage (pump.fun es volátil)
  PRIORITY_FEE: 0.0005,                // Fee para ejecución rápida
  
  PRICE_CHECK_INTERVAL_SEC: 3,         // Actualizar precio cada 3s
  
  // Health check server
  HEALTH_PORT: process.env.PORT || 8080
};

// ============================================================================
// ESTADO GLOBAL
// ============================================================================

let telegramBot = null;
let ws = null;
let wallet = null;
let connection = null;

const positions = new Map();        // mint → PositionData
const pendingTokens = new Map();    // mint → timestamp (cooldown)

const stats = {
  detected: 0,
  filtered: 0,
  bought: 0,
  sold: 0,
  wins: 0,
  losses: 0,
  totalProfitSOL: 0,
  totalProfitUSD: 0,
  bestTrade: 0,
  worstTrade: 0,
  errors: 0
};

// ============================================================================
// LOGGER
// ============================================================================

const log = {
  info: (msg) => console.log(`[INFO] ${new Date().toISOString()} ${msg}`),
  warn: (msg) => console.warn(`[WARN] ${new Date().toISOString()} ${msg}`),
  error: (msg) => console.error(`[ERROR] ${new Date().toISOString()} ${msg}`),
  trade: (msg) => console.log(`[TRADE] ${new Date().toISOString()} ${msg}`),
  debug: (msg) => {
    if (process.env.LOG_LEVEL === 'DEBUG') {
      console.log(`[DEBUG] ${new Date().toISOString()} ${msg}`);
    }
  }
};

// ============================================================================
// POSITION DATA
// ============================================================================

class PositionData {
  constructor({ mint, symbol, name, buyPrice, amountSOL }) {
    this.mint = mint;
    this.symbol = symbol || 'UNKNOWN';
    this.name = name || 'UNKNOWN';
    this.buyPrice = buyPrice;
    this.buyTime = Date.now();
    this.amountSOL = amountSOL;
    this.soldAmountSOL = 0;
    this.currentPrice = buyPrice;
    this.maxPrice = buyPrice;
    this.minPrice = buyPrice;
    this.status = 'holding';
    this.trailingStopActive = false;
    this.partialSells = [];
    this.remainingPercent = 100;
    this.txBuy = null;
    this.txSells = [];
    this.checksCount = 0;
  }
  
  get elapsedMinutes() {
    return (Date.now() - this.buyTime) / 60000;
  }
  
  get profitPercent() {
    if (this.buyPrice === 0) return 0;
    return ((this.currentPrice - this.buyPrice) / this.buyPrice) * 100;
  }
  
  get profitFromMax() {
    if (this.maxPrice === 0) return 0;
    return ((this.currentPrice - this.maxPrice) / this.maxPrice) * 100;
  }
  
  get realizedProfitSOL() {
    return this.soldAmountSOL - (this.amountSOL * (100 - this.remainingPercent) / 100);
  }
  
  get estimatedTotalProfitSOL() {
    const soldProfit = this.realizedProfitSOL;
    const remainingValue = (this.amountSOL * this.remainingPercent / 100) * (1 + this.profitPercent / 100);
    const remainingProfit = remainingValue - (this.amountSOL * this.remainingPercent / 100);
    return soldProfit + remainingProfit;
  }
}

// ============================================================================
// WALLET SETUP
// ============================================================================

async function setupWallet() {
  if (!CONFIG.WALLET_PRIVATE_KEY) {
    log.error('❌ WALLET_PRIVATE_KEY not configured');
    return false;
  }
  
  try {
    const secretKey = bs58.decode(CONFIG.WALLET_PRIVATE_KEY);
    wallet = Keypair.fromSecretKey(secretKey);
    connection = new Connection(CONFIG.SOLANA_RPC, 'confirmed');
    
    const balance = await connection.getBalance(wallet.publicKey);
    const balanceSOL = balance / 1e9;
    
    log.info(`✅ Wallet: ${wallet.publicKey.toBase58()}`);
    log.info(`💰 Balance: ${balanceSOL.toFixed(4)} SOL`);
    
    if (balanceSOL < CONFIG.TRADE_AMOUNT_SOL * 2) {
      log.warn(`⚠️ Balance bajo. Necesitas al menos ${(CONFIG.TRADE_AMOUNT_SOL * 2).toFixed(3)} SOL`);
    }
    
    return true;
  } catch (error) {
    log.error(`❌ Wallet setup failed: ${error.message}`);
    return false;
  }
}

// ============================================================================
// PUMPPORTAL TRADING FUNCTIONS
// ============================================================================

async function executeBuy(mint, amountSOL) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Comprando ${amountSOL} SOL de ${mint.slice(0, 8)}`);
    await sleep(1000); // Simular latencia
    return { success: true, signature: `dry-run-buy-${Date.now()}`, dryRun: true };
  }
  
  try {
    log.debug(`Enviando orden de compra para ${mint.slice(0, 8)}`);
    
    const response = await axios.post(CONFIG.PUMPPORTAL_API, {
      publicKey: wallet.publicKey.toBase58(),
      action: 'buy',
      mint: mint,
      denominatedInSol: 'true',
      amount: amountSOL,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, {
      timeout: 15000,
      responseType: 'arraybuffer'
    });
    
    if (response.status !== 200) {
      throw new Error(`API returned ${response.status}`);
    }
    
    const txData = new Uint8Array(response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([wallet]);
    
    const signature = await connection.sendTransaction(tx, {
      skipPreflight: false,
      maxRetries: 3
    });
    
    log.debug(`Esperando confirmación de compra: ${signature.slice(0, 16)}...`);
    await connection.confirmTransaction(signature, 'confirmed');
    
    log.trade(`✅ COMPRA: ${mint.slice(0, 8)} | ${amountSOL} SOL | Tx: ${signature.slice(0, 16)}...`);
    return { success: true, signature };
    
  } catch (error) {
    log.error(`❌ Compra falló: ${error.message}`);
    return { success: false, error: error.message };
  }
}

async function executeSell(mint, percentage) {
  if (CONFIG.DRY_RUN) {
    log.trade(`[DRY RUN] Vendiendo ${percentage}% de ${mint.slice(0, 8)}`);
    await sleep(1000);
    return { success: true, signature: `dry-run-sell-${Date.now()}`, dryRun: true };
  }
  
  try {
    const amount = percentage === 100 ? '100%' : `${percentage}%`;
    
    log.debug(`Enviando orden de venta para ${mint.slice(0, 8)} (${percentage}%)`);
    
    const response = await axios.post(CONFIG.PUMPPORTAL_API, {
      publicKey: wallet.publicKey.toBase58(),
      action: 'sell',
      mint: mint,
      denominatedInSol: 'false',
      amount: amount,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, {
      timeout: 15000,
      responseType: 'arraybuffer'
    });
    
    if (response.status !== 200) {
      throw new Error(`API returned ${response.status}`);
    }
    
    const txData = new Uint8Array(response.data);
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([wallet]);
    
    const signature = await connection.sendTransaction(tx, {
      skipPreflight: false,
      maxRetries: 3
    });
    
    await connection.confirmTransaction(signature, 'confirmed');
    
    log.trade(`✅ VENTA: ${mint.slice(0, 8)} | ${percentage}% | Tx: ${signature.slice(0, 16)}...`);
    return { success: true, signature };
    
  } catch (error) {
    log.error(`❌ Venta falló: ${error.message}`);
    return { success: false, error: error.message };
  }
}

// ============================================================================
// PRICE FETCHER (HYBRID: Bonding Curve + DexScreener)
// ============================================================================

// Importar el calculador de precios
const { HybridPriceFetcher } = require('./price-calculator');
let priceFetcher = null;

/**
 * Inicializar el fetcher de precios híbrido
 */
function initPriceFetcher() {
  if (!connection) {
    log.warn('⚠️ Connection no inicializada, usando solo DexScreener');
    return;
  }
  
  priceFetcher = new HybridPriceFetcher(connection, 150); // SOL @ $150 USD
  log.info('✅ Price fetcher híbrido inicializado');
}

/**
 * Obtiene el precio actual usando estrategia híbrida
 * 1. Intenta desde bonding curve (más rápido y preciso)
 * 2. Fallback a DexScreener si falla
 */
async function getCurrentPrice(mint) {
  // Si tenemos el fetcher híbrido, usarlo
  if (priceFetcher) {
    try {
      const priceData = await priceFetcher.getPrice(mint);
      
      if (priceData && priceData.priceUSD > 0) {
        log.debug(`Precio obtenido desde ${priceData.source}: ${priceData.priceUSD.toFixed(8)}`);
        return priceData.priceUSD;
      }
    } catch (error) {
      log.debug(`Hybrid fetcher failed: ${error.message}`);
    }
  }
  
  // Fallback a método simple con DexScreener
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
      { timeout: 5000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      return parseFloat(response.data.pairs[0].priceUsd || 0);
    }
  } catch (error) {
    log.debug(`DexScreener failed: ${error.message}`);
  }
  
  return null;
}

/**
 * Obtiene datos completos del precio (incluye liquidez, market cap, etc)
 */
async function getFullPriceData(mint) {
  if (!priceFetcher) return null;
  
  try {
    return await priceFetcher.getPrice(mint);
  } catch (error) {
    log.debug(`Error getting full price data: ${error.message}`);
    return null;
  }
}

// ============================================================================
// TELEGRAM NOTIFICATIONS
// ============================================================================

async function sendTelegram(message, options = {}) {
  if (!telegramBot || !CONFIG.TELEGRAM_CHAT_ID) return;
  
  try {
    await telegramBot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, {
      parse_mode: 'Markdown',
      disable_web_page_preview: true,
      ...options
    });
  } catch (error) {
    log.error(`Telegram failed: ${error.message}`);
  }
}

// ============================================================================
// TRADING LOGIC - NEW TOKEN HANDLER
// ============================================================================

async function handleNewToken(data) {
  try {
    stats.detected++;
    
    const payload = data.data || data;
    const mint = payload.mint || payload.token;
    
    if (!mint) {
      log.debug('Token sin mint, ignorando');
      return;
    }
    
    // Verificar si ya está en posiciones o cooldown
    if (positions.has(mint) || pendingTokens.has(mint)) {
      return;
    }
    
    // Verificar si el trading está habilitado
    if (!CONFIG.TRADING_ENABLED || !wallet) {
      log.debug(`Token detectado: ${mint.slice(0, 8)} (trading deshabilitado)`);
      return;
    }
    
    // Verificar límite de posiciones
    if (positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
      log.debug(`Posiciones llenas (${CONFIG.MAX_CONCURRENT_POSITIONS}), ignorando token`);
      stats.filtered++;
      return;
    }
    
    const symbol = payload.symbol || payload.tokenSymbol || 'UNKNOWN';
    const name = payload.name || payload.tokenName || symbol;
    
    let initialPrice = 0;
    let marketCap = 0;
    
    // Extraer precio inicial
    if (payload.pairs && Array.isArray(payload.pairs) && payload.pairs.length > 0) {
      const pair = payload.pairs[0];
      initialPrice = parseFloat(pair.priceUsd || pair.price || 0);
      marketCap = parseFloat(pair.marketCap || pair.fdv || 0);
    }
    
    log.info(`🆕 Nuevo token: ${symbol} (${mint.slice(0, 8)})`);
    
    // FILTROS DE SEGURIDAD
    
    // Filtro: Liquidez mínima
    if (marketCap > 0 && marketCap < CONFIG.MIN_INITIAL_LIQUIDITY_USD) {
      log.info(`🚫 ${symbol} - Liquidez baja ($${marketCap.toFixed(0)})`);
      stats.filtered++;
      return;
    }
    
    // Filtro: Precio mínimo
    if (initialPrice < CONFIG.MIN_PRICE_USD) {
      log.warn(`⚠️ ${symbol} - Precio muy bajo`);
      stats.filtered++;
      return;
    }
    
    // Cooldown (esperar antes de comprar)
    if (CONFIG.MIN_BUY_DELAY_MS > 0) {
      log.info(`⏳ ${symbol} - Esperando ${CONFIG.MIN_BUY_DELAY_MS / 1000}s antes de comprar`);
      pendingTokens.set(mint, Date.now());
      
      await sleep(CONFIG.MIN_BUY_DELAY_MS);
      
      // Re-verificar precio después del cooldown
      const currentPrice = await getCurrentPrice(mint);
      if (!currentPrice || currentPrice === 0) {
        log.warn(`❌ ${symbol} - No se pudo obtener precio actual`);
        pendingTokens.delete(mint);
        stats.filtered++;
        return;
      }
      
      initialPrice = currentPrice;
    }
    
    log.info(`🎯 COMPRANDO: ${symbol} @ $${initialPrice.toFixed(8)} | MCap: $${marketCap.toFixed(0)}`);
    
    // EJECUTAR COMPRA
    const buyResult = await executeBuy(mint, CONFIG.TRADE_AMOUNT_SOL);
    
    pendingTokens.delete(mint);
    
    if (!buyResult.success) {
      log.error(`❌ Compra falló para ${symbol}: ${buyResult.error}`);
      stats.errors++;
      return;
    }
    
    // CREAR POSICIÓN
    const position = new PositionData({
      mint,
      symbol,
      name,
      buyPrice: initialPrice,
      amountSOL: CONFIG.TRADE_AMOUNT_SOL
    });
    
    position.txBuy = buyResult.signature;
    positions.set(mint, position);
    stats.bought++;
    
    const dryTag = buyResult.dryRun ? '[DRY RUN] ' : '';
    log.trade(`${dryTag}💰 POSICIÓN ABIERTA: ${symbol} @ $${initialPrice.toFixed(8)} | ${CONFIG.TRADE_AMOUNT_SOL} SOL`);
    
    // Notificar a Telegram
    await sendTelegram(`
${dryTag}💰 *COMPRA EJECUTADA*

*Token:* ${name} (${symbol})
*Precio:* $${initialPrice.toFixed(8)}
*Invertido:* ${CONFIG.TRADE_AMOUNT_SOL} SOL

*Estrategia:*
• Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%
• Trailing Stop: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)
• Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min

${buyResult.dryRun ? '' : `[Pump.fun](https://pump.fun/${mint}) | [Tx](https://solscan.io/tx/${buyResult.signature})`}
    `.trim());
    
    // Iniciar monitoreo
    monitorPosition(mint).catch(err => {
      log.error(`Error en monitor de ${mint.slice(0, 8)}: ${err.message}`);
    });
    
  } catch (error) {
    log.error(`Error manejando nuevo token: ${error.message}`);
    stats.errors++;
  }
}

// ============================================================================
// POSITION MONITORING
// ============================================================================

async function monitorPosition(mint) {
  const position = positions.get(mint);
  if (!position) return;
  
  try {
    log.info(`👀 Monitoreando ${position.symbol}...`);
    
    while (positions.has(mint) && position.status === 'holding' && position.remainingPercent > 0) {
      
      // ⏱️ Verificar timeout
      if (position.elapsedMinutes >= CONFIG.MAX_HOLD_TIME_MIN) {
        log.trade(`⏰ MAX HOLD TIME: ${position.symbol} (${position.elapsedMinutes.toFixed(1)} min)`);
        await closePosition(mint, position.remainingPercent, 'timeout');
        return;
      }
      
      // 💹 Obtener precio actual
      const price = await getCurrentPrice(mint);
      
      if (!price || price === 0) {
        await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
        continue;
      }
      
      // Actualizar posición
      position.currentPrice = price;
      position.maxPrice = Math.max(position.maxPrice, price);
      position.minPrice = Math.min(position.minPrice, price);
      position.checksCount++;
      
      const profit = position.profitPercent;
      const profitFromMax = position.profitFromMax;
      
      // ════════════════════════════════════════════════════════════════════
      // 🛡️ NIVEL 1: HARD STOP LOSS (Protección absoluta)
      // ════════════════════════════════════════════════════════════════════
      if (profit <= CONFIG.HARD_STOP_LOSS_PERCENT) {
        log.trade(`🛑 HARD STOP LOSS: ${position.symbol} ${profit.toFixed(1)}%`);
        await closePosition(mint, position.remainingPercent, 'hard_stop_loss');
        return;
      }
      
      // ════════════════════════════════════════════════════════════════════
      // 📈 NIVEL 2: TRAILING STOP (Después de ganancia)
      // ════════════════════════════════════════════════════════════════════
      if (profit >= CONFIG.TRAILING_STOP_ACTIVATION && !position.trailingStopActive) {
        position.trailingStopActive = true;
        log.trade(`✅ TRAILING STOP ACTIVADO: ${position.symbol} @ +${profit.toFixed(1)}%`);
        
        await sendTelegram(`
✅ *TRAILING STOP ACTIVADO*

*${position.symbol}*
Ganancia: +${profit.toFixed(1)}%
Stop dinámico: ${CONFIG.TRAILING_STOP_PERCENT}% desde máximo

Protección de ganancias activada 🛡️
        `.trim());
      }
      
      if (position.trailingStopActive && profitFromMax <= CONFIG.TRAILING_STOP_PERCENT) {
        log.trade(`📉 TRAILING STOP: ${position.symbol} cayó ${profitFromMax.toFixed(1)}% desde máximo`);
        await closePosition(mint, position.remainingPercent, 'trailing_stop');
        return;
      }
      
      // ════════════════════════════════════════════════════════════════════
      // 💚 TAKE PROFIT PARCIAL (Asegurar ganancias)
      // ════════════════════════════════════════════════════════════════════
      for (const target of CONFIG.TAKE_PROFIT_TARGETS) {
        const alreadyTaken = position.partialSells.find(s => s.targetPercent === target.percent);
        
        if (!alreadyTaken && profit >= target.percent && position.remainingPercent > 0) {
          const sellPercent = Math.min(target.sellPercent, position.remainingPercent);
          
          log.trade(`💚 TAKE PROFIT: ${position.symbol} @ +${profit.toFixed(1)}% | Vendiendo ${sellPercent}%`);
          
          const sellResult = await executeSell(mint, sellPercent);
          
          if (sellResult.success) {
            position.partialSells.push({
              targetPercent: target.percent,
              sellPercent: sellPercent,
              price: price,
              time: Date.now(),
              tx: sellResult.signature
            });
            
            position.remainingPercent -= sellPercent;
            
            // Estimar SOL vendido
            const soldValue = (position.amountSOL * sellPercent / 100) * (1 + profit / 100);
            position.soldAmountSOL += soldValue;
            
            await sendTelegram(`
💚 *TAKE PROFIT PARCIAL*

*${position.symbol}*
*Ganancia:* +${profit.toFixed(1)}%
*Vendido:* ${sellPercent}%
*Quedan:* ${position.remainingPercent}%

${sellResult.dryRun ? '' : `[Tx](https://solscan.io/tx/${sellResult.signature})`}
            `.trim());
            
            // Si vendimos todo, finalizar
            if (position.remainingPercent === 0) {
              await finalizePosition(mint, 'take_profit_complete');
              return;
            }
          }
        }
      }
      
      // 📊 Log periódico
      if (position.checksCount % 10 === 0) {
        log.info(`📊 ${position.symbol}: $${price.toFixed(8)} (${profit >= 0 ? '+' : ''}${profit.toFixed(1)}%) | ${position.remainingPercent}% | ${position.elapsedMinutes.toFixed(1)}min`);
      }
      
      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
    }
    
  } catch (error) {
    log.error(`Error monitoreando ${position.symbol}: ${error.message}`);
    await closePosition(mint, position.remainingPercent, 'error');
  }
}

// ============================================================================
// CLOSE & FINALIZE POSITION
// ============================================================================

async function closePosition(mint, percentage, reason) {
  const position = positions.get(mint);
  if (!position || percentage === 0) return;
  
  log.trade(`Cerrando posición: ${position.symbol} (${percentage}%) - ${reason}`);
  
  const sellResult = await executeSell(mint, percentage);
  
  if (!sellResult.success) {
    log.error(`❌ No se pudo vender ${position.symbol}`);
    stats.errors++;
    return;
  }
  
  position.txSells.push(sellResult.signature);
  
  // Estimar SOL vendido
  const profit = position.profitPercent;
  const soldValue = (position.amountSOL * percentage / 100) * (1 + profit / 100);
  position.soldAmountSOL += soldValue;
  position.remainingPercent -= percentage;
  
  if (position.remainingPercent === 0) {
    await finalizePosition(mint, reason);
  }
}

async function finalizePosition(mint, reason) {
  const position = positions.get(mint);
  if (!position) return;
  
  position.status = 'sold';
  
  const totalProfit = position.estimatedTotalProfitSOL;
  const profitPercent = position.profitPercent;
  const profitUSD = totalProfit * 150; // Aproximado
  
  stats.sold++;
  stats.totalProfitSOL += totalProfit;
  stats.totalProfitUSD += profitUSD;
  
  if (totalProfit > 0) {
    stats.wins++;
    if (profitPercent > stats.bestTrade) stats.bestTrade = profitPercent;
  } else {
    stats.losses++;
    if (profitPercent < stats.worstTrade) stats.worstTrade = profitPercent;
  }
  
  const emoji = totalProfit > 0 ? '💚' : '❌';
  const dryTag = position.txSells.some(tx => tx && tx.includes('dry-run')) ? '[DRY RUN] ' : '';
  
  log.trade(`${dryTag}${emoji} CERRADO: ${position.symbol} | ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}% (${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL) | ${reason}`);
  
  await sendTelegram(`
${dryTag}${emoji} *POSICIÓN CERRADA*

*Token:* ${position.name} (${position.symbol})
*Compra:* $${position.buyPrice.toFixed(8)}
*Venta:* $${position.currentPrice.toFixed(8)}
*Ganancia:* ${profitPercent >= 0 ? '+' : ''}${profitPercent.toFixed(1)}%

*Profit:*
• ${totalProfit >= 0 ? '+' : ''}${totalProfit.toFixed(4)} SOL
• $${profitUSD.toFixed(2)} USD

*Tiempo:* ${position.elapsedMinutes.toFixed(1)} min
*Razón:* ${reason}

*Balance Total Hoy:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• ${stats.totalProfitUSD.toFixed(2)} USD
• W/L: ${stats.wins}/${stats.losses}
  `.trim());
  
  // Remover posición después de 1 minuto
  setTimeout(() => positions.delete(mint), 60000);
}

// ============================================================================
// WEBSOCKET CONNECTION
// ============================================================================

function connectWebSocket() {
  log.info('🔌 Conectando a PumpPortal WebSocket...');
  
  ws = new WebSocket(CONFIG.PUMPPORTAL_WSS);
  
  ws.on('open', () => {
    log.info('✅ WebSocket conectado');
    
    // Suscribirse a nuevos tokens
    ws.send(JSON.stringify({
      method: 'subscribeNewToken'
    }));
    
    log.info('✅ Suscrito a nuevos tokens');
    
    if (CONFIG.TRADING_ENABLED && wallet) {
      const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN (Simulación)' : '🔴 LIVE (Dinero Real)';
      log.warn(`🤖 MODO TRADING: ${mode}`);
      log.info(`💰 Monto por trade: ${CONFIG.TRADE_AMOUNT_SOL} SOL`);
    } else {
      log.warn('⚠️ Trading deshabilitado - Solo monitoreo');
    }
  });
  
  ws.on('message', (data) => {
    try {
      const parsed = JSON.parse(data);
      handleNewToken(parsed);
    } catch (error) {
      log.error(`Error parseando mensaje: ${error.message}`);
    }
  });
  
  ws.on('error', (error) => {
    log.error(`❌ WebSocket error: ${error.message}`);
  });
  
  ws.on('close', () => {
    log.warn('⚠️ WebSocket desconectado, reconectando en 5s...');
    setTimeout(connectWebSocket, 5000);
  });
}

// ============================================================================
// TELEGRAM BOT COMMANDS
// ============================================================================

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log.warn('⚠️ TELEGRAM_BOT_TOKEN no configurado - Bot de Telegram deshabilitado');
    return;
  }
  
  telegramBot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  // /start
  telegramBot.onText(/\/start/, (msg) => {
    const chatId = msg.chat.id;
    const mode = CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE';
    
    telegramBot.sendMessage(chatId, `
🤖 *Pump.fun Trading Bot*

*Estado:* ${CONFIG.TRADING_ENABLED ? '✅ ACTIVO' : '❌ INACTIVO'}
*Modo:* ${mode}
*Por trade:* ${CONFIG.TRADE_AMOUNT_SOL} SOL

*Estrategia:*
• Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%
• Trailing Stop: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)
• Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min

*Comandos disponibles:*
/status - Estado del bot
/stats - Estadísticas de trading
/positions - Posiciones abiertas
/balance - Balance de wallet
/help - Ayuda
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  // /status
  telegramBot.onText(/\/status/, async (msg) => {
    const chatId = msg.chat.id;
    const wsStatus = ws && ws.readyState === WebSocket.OPEN ? '✅' : '❌';
    
    let positionsText = '';
    if (positions.size > 0) {
      positionsText = '\n\n*Posiciones Abiertas:*\n';
      for (const [mint, pos] of positions) {
        const profit = pos.profitPercent;
        const emoji = profit > 0 ? '💚' : profit < -20 ? '❌' : '🟡';
        const trailing = pos.trailingStopActive ? '🛡️' : '';
        positionsText += `${emoji} ${pos.symbol}: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% (${pos.remainingPercent}%) ${trailing}\n`;
      }
    } else {
      positionsText = '\n\n🔭 Sin posiciones abiertas';
    }
    
    telegramBot.sendMessage(chatId, `
📊 *ESTADO DEL BOT*

*Conexión:*
• WebSocket: ${wsStatus}
• Trading: ${CONFIG.TRADING_ENABLED ? '✅' : '❌'}
• Modo: ${CONFIG.DRY_RUN ? 'DRY RUN' : 'LIVE'}

*Actividad:*
• Detectados: ${stats.detected}
• Filtrados: ${stats.filtered}
• Comprados: ${stats.bought}
• Vendidos: ${stats.sold}
• Posiciones: ${positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}
${positionsText}
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  // /stats
  telegramBot.onText(/\/stats/, (msg) => {
    const chatId = msg.chat.id;
    const winRate = (stats.wins + stats.losses) > 0 
      ? ((stats.wins / (stats.wins + stats.losses)) * 100).toFixed(1) 
      : 0;
    const avgProfit = stats.sold > 0 ? (stats.totalProfitSOL / stats.sold).toFixed(4) : 0;
    
    telegramBot.sendMessage(chatId, `
📈 *ESTADÍSTICAS DE TRADING*

*Profit Total:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• ${stats.totalProfitUSD.toFixed(2)} USD

*Trades:*
• Total: ${stats.sold}
• Wins: ${stats.wins} (${winRate}%)
• Losses: ${stats.losses}

*Performance:*
• Mejor: +${stats.bestTrade.toFixed(1)}%
• Peor: ${stats.worstTrade.toFixed(1)}%
• Promedio: ${avgProfit} SOL/trade

*Sistema:*
• Tokens detectados: ${stats.detected}
• Filtrados: ${stats.filtered}
• Errores: ${stats.errors}
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  // /positions
  telegramBot.onText(/\/positions/, (msg) => {
    const chatId = msg.chat.id;
    
    if (positions.size === 0) {
      telegramBot.sendMessage(chatId, '🔭 No hay posiciones abiertas actualmente');
      return;
    }
    
    let message = '📊 *POSICIONES ABIERTAS*\n\n';
    
    for (const [mint, pos] of positions) {
      const profit = pos.profitPercent;
      const profitSOL = pos.estimatedTotalProfitSOL;
      const emoji = profit > 0 ? '💚' : '❌';
      
      message += `${emoji} *${pos.symbol}*\n`;
      message += `Ganancia: ${profit >= 0 ? '+' : ''}${profit.toFixed(1)}% (${profitSOL >= 0 ? '+' : ''}${profitSOL.toFixed(4)} SOL)\n`;
      message += `Precio: ${pos.buyPrice.toFixed(8)} → ${pos.currentPrice.toFixed(8)}\n`;
      message += `Máximo: ${pos.maxPrice.toFixed(8)}\n`;
      message += `Tiempo: ${pos.elapsedMinutes.toFixed(1)} min | Quedan: ${pos.remainingPercent}%\n`;
      message += `Trailing: ${pos.trailingStopActive ? '✅ Activo' : '❌ Inactivo'}\n`;
      message += `[Pump.fun](https://pump.fun/${mint}) | [DexScreener](https://dexscreener.com/solana/${mint})\n\n`;
    }
    
    telegramBot.sendMessage(chatId, message, { 
      parse_mode: 'Markdown',
      disable_web_page_preview: true 
    });
  });
  
  // /balance
  telegramBot.onText(/\/balance/, async (msg) => {
    const chatId = msg.chat.id;
    
    if (!wallet || !connection) {
      telegramBot.sendMessage(chatId, '❌ Wallet no configurado');
      return;
    }
    
    try {
      const balance = await connection.getBalance(wallet.publicKey);
      const balanceSOL = balance / 1e9;
      const balanceUSD = balanceSOL * 150; // Aproximado
      const tradesAvailable = Math.floor(balanceSOL / CONFIG.TRADE_AMOUNT_SOL);
      
      telegramBot.sendMessage(chatId, `
💰 *BALANCE DE WALLET*

*Dirección:*
\`${wallet.publicKey.toBase58()}\`

*Balance:*
• ${balanceSOL.toFixed(4)} SOL
• ~${balanceUSD.toFixed(2)} USD

*Trading:*
• Trades disponibles: ${tradesAvailable}
• Monto por trade: ${CONFIG.TRADE_AMOUNT_SOL} SOL
• Posiciones abiertas: ${positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}

*Profit Sesión:*
• ${stats.totalProfitSOL >= 0 ? '+' : ''}${stats.totalProfitSOL.toFixed(4)} SOL
• ${stats.totalProfitUSD.toFixed(2)} USD
      `.trim(), { parse_mode: 'Markdown' });
    } catch (error) {
      telegramBot.sendMessage(chatId, `❌ Error obteniendo balance: ${error.message}`);
    }
  });
  
  // /help
  telegramBot.onText(/\/help/, (msg) => {
    const chatId = msg.chat.id;
    
    telegramBot.sendMessage(chatId, `
❓ *AYUDA - Pump.fun Trading Bot*

*Comandos:*
/start - Iniciar bot y ver info
/status - Estado actual del bot
/stats - Estadísticas de trading
/positions - Ver posiciones abiertas
/balance - Balance de wallet
/help - Esta ayuda

*Sobre el Bot:*
Este bot detecta nuevos tokens en pump.fun y ejecuta trades automáticos usando una estrategia de stop-loss híbrido y take profit escalonado.

*Estrategia:*
1. Hard Stop Loss (${CONFIG.HARD_STOP_LOSS_PERCENT}%): Protección absoluta
2. Trailing Stop (${CONFIG.TRAILING_STOP_PERCENT}%): Se activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%
3. Take Profit: Venta parcial en +100%, +200%, +500%
4. Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} minutos

*Modo:* ${CONFIG.DRY_RUN ? 'DRY RUN (simulación)' : 'LIVE (dinero real)'}

⚠️ *Riesgo:* Trading de criptomonedas es altamente riesgoso. Solo invierte lo que puedes perder.
    `.trim(), { parse_mode: 'Markdown' });
  });
  
  log.info('✅ Telegram bot inicializado');
}

// ============================================================================
// HEALTH CHECK SERVER
// ============================================================================

function startHealthServer() {
  const http = require('http');
  
  const server = http.createServer((req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        status: 'healthy',
        websocket_connected: ws && ws.readyState === WebSocket.OPEN,
        trading_enabled: CONFIG.TRADING_ENABLED,
        dry_run: CONFIG.DRY_RUN,
        positions: positions.size,
        stats: stats,
        uptime: process.uptime()
      }));
    } else if (req.url === '/metrics') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({
        positions: positions.size,
        ...stats
      }));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('Pump.fun Trading Bot - Running ✅');
    }
  });
  
  server.listen(CONFIG.HEALTH_PORT, () => {
    log.info(`✅ Health server: http://0.0.0.0:${CONFIG.HEALTH_PORT}`);
  });
}

// ============================================================================
// UTILITIES
// ============================================================================

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ============================================================================
// MAIN FUNCTION
// ============================================================================

async function main() {
  console.log('\n'.repeat(2));
  log.info('═══════════════════════════════════════════════════════════════');
  log.info('🚀 PUMP.FUN TRADING BOT - INICIANDO');
  log.info('═══════════════════════════════════════════════════════════════');
  
  // Validar configuración
  if (!CONFIG.TELEGRAM_BOT_TOKEN || !CONFIG.TELEGRAM_CHAT_ID) {
    log.warn('⚠️ Telegram no configurado - Las notificaciones estarán deshabilitadas');
  }
  
  if (!CONFIG.TRADING_ENABLED) {
    log.warn('⚠️ TRADING_ENABLED=false - Bot solo monitoreará (sin trading)');
  } else {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      log.error('❌ WALLET_PRIVATE_KEY requerido para trading!');
      log.error('   Configura la variable de entorno WALLET_PRIVATE_KEY');
      process.exit(1);
    }
    
    log.info('💼 Configurando wallet...');
    const walletReady = await setupWallet();
    
    if (!walletReady) {
      log.error('❌ Error configurando wallet!');
      process.exit(1);
    }
  }
  
  // Mostrar configuración
  log.info('');
  log.info('⚙️  CONFIGURACIÓN:');
  log.info(`   • Monto: ${CONFIG.TRADE_AMOUNT_SOL} SOL por trade`);
  log.info(`   • Hard Stop: ${CONFIG.HARD_STOP_LOSS_PERCENT}%`);
  log.info(`   • Trailing: ${CONFIG.TRAILING_STOP_PERCENT}% (activa al +${CONFIG.TRAILING_STOP_ACTIVATION}%)`);
  log.info(`   • Max Hold: ${CONFIG.MAX_HOLD_TIME_MIN} min`);
  log.info(`   • Max Posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}`);
  log.info(`   • Min Liquidez: ${CONFIG.MIN_INITIAL_LIQUIDITY_USD}`);
  log.info(`   • Slippage: ${CONFIG.SLIPPAGE}%`);
  log.info(`   • Priority Fee: ${CONFIG.PRIORITY_FEE} SOL`);
  
  // Advertencias según modo
  log.info('');
  if (CONFIG.DRY_RUN) {
    log.warn('═══════════════════════════════════════════════════════════════');
    log.warn('🟡 MODO DRY RUN - TRANSACCIONES SIMULADAS');
    log.warn('   No se gastará dinero real');
    log.warn('   Para trading real: DRY_RUN=false');
    log.warn('═══════════════════════════════════════════════════════════════');
  } else if (CONFIG.TRADING_ENABLED) {
    log.warn('═══════════════════════════════════════════════════════════════');
    log.warn('🔴 MODO LIVE - DINERO REAL');
    log.warn('   ⚠️  Las transacciones gastarán SOL real');
    log.warn('   ⚠️  Asegúrate de entender los riesgos');
    log.warn('   ⚠️  Solo usa fondos que puedas perder');
    log.warn('═══════════════════════════════════════════════════════════════');
  }
  
  log.info('');
  log.info('🚀 Iniciando componentes...');
  
  // Iniciar componentes
  setupTelegramBot();
  startHealthServer();
  connectWebSocket();
  
  log.info('');
  log.info('✅ Bot iniciado correctamente');
  log.info('📊 Esperando nuevos tokens...');
  log.info('═══════════════════════════════════════════════════════════════');
  log.info('');
  
  // Mensaje de bienvenida a Telegram
  if (telegramBot && CONFIG.TELEGRAM_CHAT_ID) {
    await sendTelegram(`
🤖 *Bot Iniciado*

*Modo:* ${CONFIG.DRY_RUN ? '🟡 DRY RUN' : '🔴 LIVE'}
*Trading:* ${CONFIG.TRADING_ENABLED ? '✅ Activo' : '❌ Inactivo'}
*Monto:* ${CONFIG.TRADE_AMOUNT_SOL} SOL/trade

El bot está monitoreando nuevos tokens en pump.fun 👀
    `.trim());
  }
}

// ============================================================================
// PROCESS HANDLERS
// ============================================================================

// Graceful shutdown
process.on('SIGTERM', async () => {
  log.info('');
  log.info('🛑 SIGTERM recibido - Cerrando posiciones...');
  
  // Cerrar todas las posiciones abiertas
  const closePromises = [];
  for (const [mint, pos] of positions) {
    if (pos.remainingPercent > 0) {
      log.info(`   Cerrando ${pos.symbol}...`);
      closePromises.push(closePosition(mint, pos.remainingPercent, 'shutdown'));
    }
  }
  
  await Promise.all(closePromises);
  
  if (ws) ws.close();
  log.info('✅ Shutdown completo');
  process.exit(0);
});

process.on('SIGINT', async () => {
  log.info('');
  log.info('🛑 SIGINT recibido - Cerrando posiciones...');
  
  const closePromises = [];
  for (const [mint, pos] of positions) {
    if (pos.remainingPercent > 0) {
      log.info(`   Cerrando ${pos.symbol}...`);
      closePromises.push(closePosition(mint, pos.remainingPercent, 'shutdown'));
    }
  }
  
  await Promise.all(closePromises);
  
  if (ws) ws.close();
  log.info('✅ Shutdown completo');
  process.exit(0);
});

// Manejo de errores no capturados
process.on('uncaughtException', (error) => {
  log.error(`❌ Uncaught Exception: ${error.message}`);
  log.error(error.stack);
});

process.on('unhandledRejection', (reason, promise) => {
  log.error(`❌ Unhandled Rejection: ${reason}`);
});

// ============================================================================
// START BOT
// ============================================================================

main().catch(error => {
  log.error(`❌ Error fatal: ${error.message}`);
  log.error(error.stack);
  process.exit(1);
});
