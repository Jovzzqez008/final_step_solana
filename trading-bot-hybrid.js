// trading-bot-fixed.js - PUMP.FUN TRADING BOT
// 🚀 Detección + Análisis + Trading automatizado
// Combinación de bot.js (detección) + trading bot (análisis)

require('dotenv').config();
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, PublicKey, Keypair, VersionedTransaction, LAMPORTS_PER_SOL } = require('@solana/web3.js');

// ═══════════════════════════════════════════════════════════════
// CONFIGURACIÓN
// ═══════════════════════════════════════════════════════════════

const CONFIG = {
  // Wallet
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY,
  
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
  
  // Trading
  DRY_RUN: process.env.DRY_RUN === 'true',
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007'),
  SLIPPAGE: parseInt(process.env.SLIPPAGE || '25'),
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  // Stop Loss / Take Profit
  HARD_STOP_LOSS: parseFloat(process.env.HARD_STOP_LOSS || '-45'),
  QUICK_STOP: parseFloat(process.env.QUICK_STOP || '-25'),
  TRAILING_ACTIVATION: parseFloat(process.env.TRAILING_ACTIVATION || '40'),
  TRAILING_PERCENT: parseFloat(process.env.TRAILING_PERCENT || '-20'),
  TAKE_PROFIT_1: parseFloat(process.env.TAKE_PROFIT_1 || '80'),
  TAKE_PROFIT_2: parseFloat(process.env.TAKE_PROFIT_2 || '150'),
  TAKE_PROFIT_3: parseFloat(process.env.TAKE_PROFIT_3 || '300'),
  
  // Smart Trader - VALORES AJUSTADOS PARA MEJOR DETECCIÓN
  EARLY_VELOCITY_MIN: parseFloat(process.env.EARLY_VELOCITY_MIN || '10'), // Bajado de 15
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '45'), // Aumentado de 30
  CONFIRMATION_VELOCITY: parseFloat(process.env.CONFIRMATION_VELOCITY || '25'), // Bajado de 35
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '90'), // Aumentado de 60
  MIN_LIQUIDITY_SOL: parseFloat(process.env.MIN_LIQUIDITY_SOL || '0.3'), // Bajado de 0.5
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '3'), // Bajado de 5
  MIN_UNIQUE_BUYERS: parseInt(process.env.MIN_UNIQUE_BUYERS || '3'), // Bajado de 4
  
  // Timing
  MAX_HOLD_TIME_MIN: parseInt(process.env.MAX_HOLD_TIME_MIN || '12'),
  STAGNANT_TIME_MIN: parseInt(process.env.STAGNANT_TIME_MIN || '4'),
  MAX_WATCH_TIME_SEC: parseInt(process.env.MAX_WATCH_TIME_SEC || '180'), // Aumentado de 60
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  
  // Análisis
  PRICE_CHECK_INTERVAL_SEC: parseInt(process.env.PRICE_CHECK_INTERVAL_SEC || '5'),
  MIN_INITIAL_MARKET_CAP: parseFloat(process.env.MIN_INITIAL_MARKET_CAP || '100'),
  
  // RPCs
  RPC_ENDPOINTS: [
    'https://api.mainnet-beta.solana.com',
    'https://solana-api.projectserum.com',
    'https://rpc.ankr.com/solana'
  ]
};

// ═══════════════════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

const STATE = {
  wallet: null,
  connection: null,
  currentRpcIndex: 0,
  bot: null,
  ws: null,
  positions: new Map(),
  watchlist: new Map(),
  stats: {
    detected: 0,
    analyzing: 0,
    filtered: 0,
    totalTrades: 0,
    wins: 0,
    losses: 0,
    totalPnl: 0
  }
};

// ═══════════════════════════════════════════════════════════════
// LOGGER
// ═══════════════════════════════════════════════════════════════

function log(level, message) {
  const timestamp = new Date().toISOString();
  const colors = {
    'INFO': '\x1b[36m',    // Cyan
    'SUCCESS': '\x1b[32m', // Green
    'WARN': '\x1b[33m',    // Yellow
    'ERROR': '\x1b[31m',   // Red
    'DEBUG': '\x1b[90m'    // Gray
  };
  const emoji = {
    'INFO': 'ℹ️',
    'SUCCESS': '✅',
    'WARN': '⚠️',
    'ERROR': '❌',
    'DEBUG': '🔍'
  }[level] || 'ℹ️';
  
  const color = colors[level] || '';
  const reset = '\x1b[0m';
  
  console.log(`${color}[${level}] ${timestamp} ${emoji} ${message}${reset}`);
}

// ═══════════════════════════════════════════════════════════════
// UTILIDADES
// ═══════════════════════════════════════════════════════════════

function decodeBase58(str) {
  try {
    const bs58 = require('bs58');
    if (typeof bs58.decode === 'function') {
      return bs58.decode(str);
    } else if (typeof bs58 === 'function') {
      return bs58(str);
    } else if (bs58.default && typeof bs58.default.decode === 'function') {
      return bs58.default.decode(str);
    }
  } catch (e) {
    log('WARN', 'bs58 no disponible, usando decodificador manual');
  }
  
  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let num = BigInt(0);
  for (let i = 0; i < str.length; i++) {
    const value = ALPHABET.indexOf(str[i]);
    if (value === -1) throw new Error(`Invalid base58 character: ${str[i]}`);
    num = num * 58n + BigInt(value);
  }
  let hex = num.toString(16);
  if (hex.length % 2) hex = '0' + hex;
  for (let i = 0; i < str.length && str[i] === '1'; i++) {
    hex = '00' + hex;
  }
  return Buffer.from(hex, 'hex');
}

async function sendTelegram(message) {
  if (!STATE.bot || !CONFIG.TELEGRAM_CHAT_ID) return;
  try {
    await STATE.bot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, { parse_mode: 'HTML' });
  } catch (error) {
    log('ERROR', `Error enviando Telegram: ${error.message}`);
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ═══════════════════════════════════════════════════════════════
// DEXSCREENER API
// ═══════════════════════════════════════════════════════════════

async function getTokenData(mint) {
  try {
    const response = await axios.get(`https://api.dexscreener.com/latest/dex/tokens/${mint}`, {
      timeout: 8000
    });
    
    if (!response.data?.pairs || response.data.pairs.length === 0) {
      return null;
    }
    
    const pair = response.data.pairs.find(p => 
      p.dexId === 'raydium' || p.chainId === 'solana'
    ) || response.data.pairs[0];
    
    return {
      mint,
      price: parseFloat(pair.priceUsd || 0),
      priceChange5m: parseFloat(pair.priceChange?.m5 || 0),
      priceChange1h: parseFloat(pair.priceChange?.h1 || 0),
      volume5m: parseFloat(pair.volume?.m5 || 0),
      volume1h: parseFloat(pair.volume?.h1 || 0),
      liquidity: parseFloat(pair.liquidity?.usd || 0),
      marketCap: parseFloat(pair.fdv || pair.marketCap || 0),
      txns5m: (pair.txns?.m5?.buys || 0) + (pair.txns?.m5?.sells || 0),
      buys5m: pair.txns?.m5?.buys || 0,
      sells5m: pair.txns?.m5?.sells || 0
    };
  } catch (error) {
    if (error.code === 'ECONNABORTED') {
      log('DEBUG', `Timeout obteniendo datos de ${mint.slice(0, 8)}`);
    } else if (error.response?.status !== 404) {
      log('DEBUG', `Error DexScreener para ${mint.slice(0, 8)}: ${error.message}`);
    }
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════
// PUMPPORTAL API
// ═══════════════════════════════════════════════════════════════

async function buyToken(mint, solAmount) {
  try {
    const response = await axios.post('https://pumpportal.fun/api/trade-local', {
      publicKey: STATE.wallet.publicKey.toString(),
      action: 'buy',
      mint: mint,
      denominatedInSol: 'true',
      amount: solAmount,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, { timeout: 10000 });
    
    if (!response.data) {
      throw new Error('No se recibió transacción de PumpPortal');
    }
    
    const txData = Buffer.from(response.data, 'base64');
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([STATE.wallet]);
    
    const signature = await STATE.connection.sendTransaction(tx, {
      skipPreflight: true,
      maxRetries: 3
    });
    
    await STATE.connection.confirmTransaction(signature, 'confirmed');
    return signature;
  } catch (error) {
    log('ERROR', `Error comprando ${mint.slice(0, 8)}: ${error.message}`);
    throw error;
  }
}

async function sellToken(mint, percentage = 100) {
  try {
    const response = await axios.post('https://pumpportal.fun/api/trade-local', {
      publicKey: STATE.wallet.publicKey.toString(),
      action: 'sell',
      mint: mint,
      denominatedInSol: 'false',
      amount: percentage,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE,
      pool: 'pump'
    }, { timeout: 10000 });
    
    if (!response.data) {
      throw new Error('No se recibió transacción de PumpPortal');
    }
    
    const txData = Buffer.from(response.data, 'base64');
    const tx = VersionedTransaction.deserialize(txData);
    tx.sign([STATE.wallet]);
    
    const signature = await STATE.connection.sendTransaction(tx, {
      skipPreflight: true,
      maxRetries: 3
    });
    
    await STATE.connection.confirmTransaction(signature, 'confirmed');
    return signature;
  } catch (error) {
    log('ERROR', `Error vendiendo ${mint.slice(0, 8)}: ${error.message}`);
    throw error;
  }
}

// ═══════════════════════════════════════════════════════════════
// ANÁLISIS DE TOKENS
// ═══════════════════════════════════════════════════════════════

async function analyzeToken(mint) {
  const watch = STATE.watchlist.get(mint);
  if (!watch) return;
  
  const now = Date.now();
  const elapsed = (now - watch.firstSeen) / 1000;
  
  // Timeout de observación
  if (elapsed > CONFIG.MAX_WATCH_TIME_SEC) {
    log('INFO', `⏱️ Timeout de observación para ${watch.symbol} después de ${elapsed.toFixed(0)}s`);
    STATE.watchlist.delete(mint);
    STATE.stats.analyzing = STATE.watchlist.size;
    return;
  }
  
  // Obtener datos actualizados
  const data = await getTokenData(mint);
  if (!data || !data.price) {
    log('DEBUG', `No hay datos para ${watch.symbol}, reintentando...`);
    return;
  }
  
  // Guardar precio
  watch.priceHistory.push({ price: data.price, time: now });
  watch.lastPrice = data.price;
  watch.buyCount = data.buys5m || 0;
  watch.liquidity = data.liquidity;
  
  // Calcular velocidad de precio
  const recentPrices = watch.priceHistory.filter(p => (now - p.time) <= CONFIG.EARLY_TIME_WINDOW * 1000);
  
  if (recentPrices.length < 2) {
    log('DEBUG', `${watch.symbol}: Esperando más datos (${recentPrices.length} puntos)`);
    return;
  }
  
  const firstPrice = recentPrices[0].price;
  const currentPrice = recentPrices[recentPrices.length - 1].price;
  
  if (firstPrice === 0) {
    log('DEBUG', `${watch.symbol}: Precio inicial es 0, saltando...`);
    return;
  }
  
  const priceChange = ((currentPrice - firstPrice) / firstPrice) * 100;
  const liquiditySOL = data.liquidity / LAMPORTS_PER_SOL;
  
  // Log detallado cada 3 checks
  if (watch.checksCount % 3 === 0) {
    log('INFO', `[ANÁLISIS] 📊 ${watch.symbol}: ${priceChange > 0 ? '+' : ''}${priceChange.toFixed(1)}% | Precio: $${currentPrice.toFixed(8)} | Liq: ${liquiditySOL.toFixed(2)} SOL | Buys: ${watch.buyCount} | ${elapsed.toFixed(0)}s`);
  }
  
  watch.checksCount++;
  
  // Señal temprana
  if (!watch.earlySignal && elapsed <= CONFIG.EARLY_TIME_WINDOW && priceChange >= CONFIG.EARLY_VELOCITY_MIN) {
    watch.earlySignal = true;
    log('SUCCESS', `[SEÑAL] ⚡ Señal temprana en ${watch.symbol}: +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s`);
    
    await sendTelegram(
      `⚡ <b>SEÑAL TEMPRANA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
      `📈 +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s\n` +
      `💰 Liq: ${liquiditySOL.toFixed(2)} SOL\n` +
      `🛒 Buys: ${watch.buyCount}`
    );
  }
  
  // Confirmación para comprar
  if (watch.earlySignal && elapsed >= CONFIG.CONFIRMATION_TIME && priceChange >= CONFIG.CONFIRMATION_VELOCITY) {
    
    // Validaciones adicionales
    if (liquiditySOL < CONFIG.MIN_LIQUIDITY_SOL) {
      log('WARN', `[FILTRO] ❌ ${watch.symbol}: Liquidez insuficiente (${liquiditySOL.toFixed(2)} SOL)`);
      STATE.watchlist.delete(mint);
      STATE.stats.filtered++;
      return;
    }
    
    if (watch.buyCount < CONFIG.MIN_BUY_COUNT) {
      log('WARN', `[FILTRO] ❌ ${watch.symbol}: Pocas compras (${watch.buyCount})`);
      STATE.watchlist.delete(mint);
      STATE.stats.filtered++;
      return;
    }
    
    log('SUCCESS', `[SEÑAL CONFIRMADA] 🚀 ${watch.symbol} | +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s | ${watch.buyCount} buys | ${liquiditySOL.toFixed(2)} SOL`);
    
    await executeBuy(mint, watch, priceChange, elapsed);
  }
}

// ═══════════════════════════════════════════════════════════════
// TRADING
// ═══════════════════════════════════════════════════════════════

async function executeBuy(mint, watch, priceChange, elapsed) {
  // Límite de posiciones concurrentes
  if (STATE.positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
    log('WARN', `[TRADE] ⚠️ Límite de posiciones alcanzado (${STATE.positions.size})`);
    return;
  }
  
  try {
    log('INFO', `[TRADE] 🛒 Ejecutando compra de ${CONFIG.TRADE_AMOUNT_SOL} SOL de ${watch.symbol}...`);
    
    if (CONFIG.DRY_RUN) {
      log('SUCCESS', `[DRY RUN] ✅ Compra simulada: ${watch.symbol} @ $${watch.lastPrice.toFixed(8)}`);
    } else {
      const signature = await buyToken(mint, CONFIG.TRADE_AMOUNT_SOL);
      log('SUCCESS', `[TRADE] ✅ Compra ejecutada: https://solscan.io/tx/${signature}`);
    }
    
    // Crear posición
    STATE.positions.set(mint, {
      mint,
      symbol: watch.symbol,
      name: watch.name,
      entryPrice: watch.lastPrice,
      entryTime: Date.now(),
      amount: CONFIG.TRADE_AMOUNT_SOL,
      highestPrice: watch.lastPrice,
      trailingActive: false,
      tp1Taken: false,
      tp2Taken: false,
      tp3Taken: false,
      lastPnl: 0
    });
    
    STATE.watchlist.delete(mint);
    STATE.stats.analyzing = STATE.watchlist.size;
    STATE.stats.totalTrades++;
    
    await sendTelegram(
      `🟢 <b>POSICIÓN ABIERTA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
      `💰 Inversión: ${CONFIG.TRADE_AMOUNT_SOL} SOL\n` +
      `💵 Precio entrada: $${watch.lastPrice.toFixed(8)}\n` +
      `📈 Ganancia al entrar: +${priceChange.toFixed(1)}%\n` +
      `⏱️ Tiempo análisis: ${elapsed.toFixed(0)}s`
    );
    
    log('SUCCESS', `[POSICIÓN] ✅ Abierta: ${watch.symbol} @ $${watch.lastPrice.toFixed(8)}`);
    
  } catch (error) {
    log('ERROR', `[TRADE] ❌ Error ejecutando compra: ${error.message}`);
  }
}

async function monitorPositions() {
  for (const [mint, pos] of STATE.positions.entries()) {
    const data = await getTokenData(mint);
    if (!data || !data.price) continue;
    
    const currentPrice = data.price;
    const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
    const holdTime = (Date.now() - pos.entryTime) / 1000 / 60;
    
    // Actualizar precio más alto
    if (currentPrice > pos.highestPrice) {
      pos.highestPrice = currentPrice;
    }
    
    // Activar trailing stop
    if (!pos.trailingActive && pnlPercent >= CONFIG.TRAILING_ACTIVATION) {
      pos.trailingActive = true;
      log('INFO', `[POSITION] 🛡️ Trailing activado para ${pos.symbol} @ +${pnlPercent.toFixed(1)}%`);
    }
    
    // Calcular trailing stop
    let trailingStopPrice = null;
    if (pos.trailingActive) {
      trailingStopPrice = pos.highestPrice * (1 + CONFIG.TRAILING_PERCENT / 100);
    }
    
    const trailingEmoji = pos.trailingActive ? '🛡️' : '';
    log('INFO', `[POSITION] 📊 ${pos.symbol}: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(1)}% | $${currentPrice.toFixed(8)} | ${holdTime.toFixed(1)}min ${trailingEmoji}`);
    
    let shouldSell = false;
    let sellPercentage = 100;
    let sellReason = '';
    
    // Hard stop loss
    if (pnlPercent <= CONFIG.HARD_STOP_LOSS) {
      shouldSell = true;
      sellReason = `Hard Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Quick stop
    else if (holdTime <= 2 && pnlPercent <= CONFIG.QUICK_STOP) {
      shouldSell = true;
      sellReason = `Quick Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Trailing stop
    else if (pos.trailingActive && currentPrice <= trailingStopPrice) {
      shouldSell = true;
      sellReason = `Trailing Stop (${pnlPercent.toFixed(1)}%)`;
    }
    // Take profits
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_3 && !pos.tp3Taken) {
      shouldSell = true;
      sellPercentage = 50;
      sellReason = `TP3 (${pnlPercent.toFixed(1)}%)`;
      pos.tp3Taken = true;
    }
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_2 && !pos.tp2Taken) {
      shouldSell = true;
      sellPercentage = 30;
      sellReason = `TP2 (${pnlPercent.toFixed(1)}%)`;
      pos.tp2Taken = true;
    }
    else if (pnlPercent >= CONFIG.TAKE_PROFIT_1 && !pos.tp1Taken) {
      shouldSell = true;
      sellPercentage = 25;
      sellReason = `TP1 (${pnlPercent.toFixed(1)}%)`;
      pos.tp1Taken = true;
    }
    // Max hold time
    else if (holdTime >= CONFIG.MAX_HOLD_TIME_MIN) {
      shouldSell = true;
      sellReason = `Max Hold (${holdTime.toFixed(0)}min)`;
    }
    // Estancamiento
    else if (holdTime >= CONFIG.STAGNANT_TIME_MIN && Math.abs(pnlPercent - pos.lastPnl) < 2) {
      shouldSell = true;
      sellReason = `Estancado (${pnlPercent.toFixed(1)}%)`;
    }
    
    if (shouldSell) {
      await executeSell(mint, pos, sellPercentage, sellReason, pnlPercent);
    }
    
    pos.lastPnl = pnlPercent;
  }
}

async function executeSell(mint, pos, percentage, reason, pnlPercent) {
  try {
    log('INFO', `[TRADE] 🔔 Vendiendo ${percentage}% de ${pos.symbol}: ${reason}`);
    
    if (CONFIG.DRY_RUN) {
      log('SUCCESS', `[DRY RUN] ✅ Venta simulada: ${percentage}% @ ${pnlPercent.toFixed(2)}%`);
    } else {
      const signature = await sellToken(mint, percentage);
      log('SUCCESS', `[TRADE] ✅ Venta ejecutada: https://solscan.io/tx/${signature}`);
    }
    
    const isFullExit = percentage === 100;
    
    if (isFullExit) {
      STATE.positions.delete(mint);
      
      if (pnlPercent > 0) STATE.stats.wins++;
      else STATE.stats.losses++;
      STATE.stats.totalPnl += pnlPercent;
      
      const emoji = pnlPercent > 0 ? '🟢' : '🔴';
      await sendTelegram(
        `${emoji} <b>POSICIÓN CERRADA</b>\n\n` +
        `${pos.name} (${pos.symbol})\n` +
        `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
        `📊 P&L: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(2)}%\n` +
        `💰 Inversión: ${pos.amount} SOL\n` +
        `🎯 Razón: ${reason}`
      );
      
      log('SUCCESS', `[POSITION] ✅ Cerrada: ${pos.symbol} @ ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(2)}%`);
    } else {
      await sendTelegram(
        `💰 <b>VENTA PARCIAL</b>\n\n` +
        `${pos.name} (${pos.symbol})\n` +
        `Vendido: ${percentage}%\n` +
        `P&L: +${pnlPercent.toFixed(2)}%\n` +
        `Razón: ${reason}`
      );
      
      log('SUCCESS', `[POSITION] ✅ Vendido ${percentage}% de ${pos.symbol}`);
    }
    
  } catch (error) {
    log('ERROR', `[TRADE] ❌ Error vendiendo: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// WEBSOCKET PUMPPORTAL
// ═══════════════════════════════════════════════════════════════

function setupWebSocket() {
  STATE.ws = new WebSocket('wss://pumpportal.fun/api/data');
  
  STATE.ws.on('open', () => {
    log('SUCCESS', '✅ Conectado a PumpPortal WebSocket');
    STATE.ws.send(JSON.stringify({
      method: 'subscribeNewToken'
    }));
    log('INFO', '📡 Suscrito a tokens nuevos');
  });
  
  STATE.ws.on('message', async (data) => {
    try {
      const msg = JSON.parse(data.toString());
      
      // Filtrar solo eventos de creación
      if (msg.txType === 'create' || msg.mint) {
        const mint = msg.mint || msg.token;
        const name = msg.name || msg.tokenName || 'Unknown';
        const symbol = msg.symbol || msg.tokenSymbol || 'UNKNOWN';
        
        // Verificar si ya está en watchlist
        if (STATE.watchlist.has(mint)) {
          log('DEBUG', `Token ${mint.slice(0, 8)} ya está en watchlist`);
          return;
        }
        
        STATE.stats.detected++;
        
        log('SUCCESS', `🆕 NUEVO TOKEN: ${name} (${symbol}) - ${mint.slice(0, 8)}...`);
        
        // Agregar a watchlist
        STATE.watchlist.set(mint, {
          mint,
          name,
          symbol,
          firstSeen: Date.now(),
          priceHistory: [],
          buyCount: 0,
          liquidity: 0,
          lastPrice: 0,
          earlySignal: false,
          checksCount: 0
        });
        
        STATE.stats.analyzing = STATE.watchlist.size;
        
        await sendTelegram(
          `🆕 <b>Token Detectado</b>\n\n` +
          `${name} (${symbol})\n` +
          `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
          `👀 Iniciando análisis...`
        );
        
        log('INFO', `📊 Total en watchlist: ${STATE.watchlist.size}`);
      }
    } catch (error) {
      log('ERROR', `Error procesando mensaje WS: ${error.message}`);
    }
  });
  
  STATE.ws.on('error', (error) => {
    log('ERROR', `WebSocket error: ${error.message}`);
  });
  
  STATE.ws.on('close', () => {
    log('WARN', '⚠️ WebSocket cerrado. Reconectando en 5s...');
    setTimeout(setupWebSocket, 5000);
  });
}

// ═══════════════════════════════════════════════════════════════
// TELEGRAM BOT
// ═══════════════════════════════════════════════════════════════

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log('WARN', '⚠️ Telegram no configurado');
    return;
  }
  
  STATE.bot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  
  STATE.bot.onText(/\/start/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id,
      `🤖 <b>Pump.fun Trading Bot</b>\n\n` +
      `Bot de trading automatizado para tokens de Pump.fun\n\n` +
      `<b>Comandos:</b>\n` +
      `/status - Estado del bot\n` +
      `/stats - Estadísticas de trading\n` +
      `/positions - Posiciones abiertas\n` +
      `/watchlist - Tokens en análisis\n` +
      `/balance - Balance de wallet\n` +
      `/config - Ver configuración`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.onText(/\/status/, async (msg) => {
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey) / LAMPORTS_PER_SOL;
      const wsStatus = STATE.ws && STATE.ws.readyState === WebSocket.OPEN ? '✅ Conectado' : '❌ Desconectado';
      
      STATE.bot.sendMessage(msg.chat.id,
        `📊 <b>Estado del Bot</b>\n\n` +
        `💰 Balance: ${balance.toFixed(4)} SOL\n` +
        `📈 Posiciones abiertas: ${STATE.positions.size}\n` +
        `👀 Tokens analizando: ${STATE.watchlist.size}\n` +
        `🌐 WebSocket: ${wsStatus}\n` +
        `🎯 Modo: ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n\n` +
        `<b>Detección:</b>\n` +
        `🆕 Detectados: ${STATE.stats.detected}\n` +
        `📊 Analizando: ${STATE.stats.analyzing}\n` +
        `🚫 Filtrados: ${STATE.stats.filtered}`,
        { parse_mode: 'HTML' }
      );
    } catch (error) {
      log('ERROR', `Error en /status: ${error.message}`);
    }
  });
  
  STATE.bot.onText(/\/stats/, (msg) => {
    const winRate = STATE.stats.totalTrades > 0 
      ? (STATE.stats.wins / STATE.stats.totalTrades * 100).toFixed(1) 
      : '0.0';
    
    STATE.bot.sendMessage(msg.chat.id,
      `📊 <b>Estadísticas de Trading</b>\n\n` +
      `📈 Trades totales: ${STATE.stats.totalTrades}\n` +
      `✅ Wins: ${STATE.stats.wins}\n` +
      `❌ Losses: ${STATE.stats.losses}\n` +
      `💹 Win Rate: ${winRate}%\n` +
      `💰 P&L Total: ${STATE.stats.totalPnl > 0 ? '+' : ''}${STATE.stats.totalPnl.toFixed(2)}%\n\n` +
      `<b>Rendimiento promedio:</b>\n` +
      `📊 Por trade: ${STATE.stats.totalTrades > 0 ? (STATE.stats.totalPnl / STATE.stats.totalTrades).toFixed(2) : '0.00'}%`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.onText(/\/positions/, (msg) => {
    if (STATE.positions.size === 0) {
      STATE.bot.sendMessage(msg.chat.id, '📊 No hay posiciones abiertas');
      return;
    }
    
    let text = '📊 <b>Posiciones Abiertas</b>\n\n';
    
    for (const [mint, pos] of STATE.positions.entries()) {
      const holdTime = ((Date.now() - pos.entryTime) / 1000 / 60).toFixed(1);
      text += `<b>${pos.symbol}</b>\n`;
      text += `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n`;
      text += `💰 ${pos.amount} SOL @ ${pos.entryPrice.toFixed(8)}\n`;
      text += `⏱️ ${holdTime} min\n`;
      text += `${pos.trailingActive ? '🛡️ Trailing activo\n' : ''}`;
      text += `\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/watchlist/, (msg) => {
    if (STATE.watchlist.size === 0) {
      STATE.bot.sendMessage(msg.chat.id, '👀 No hay tokens en análisis');
      return;
    }
    
    let text = '👀 <b>Tokens en Análisis</b>\n\n';
    
    const tokens = Array.from(STATE.watchlist.entries()).slice(0, 10);
    for (const [mint, watch] of tokens) {
      const elapsed = ((Date.now() - watch.firstSeen) / 1000).toFixed(0);
      text += `<b>${watch.symbol}</b> ${watch.earlySignal ? '⚡' : ''}\n`;
      text += `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n`;
      text += `⏱️ ${elapsed}s | 🛒 ${watch.buyCount} buys\n\n`;
    }
    
    if (STATE.watchlist.size > 10) {
      text += `\n... y ${STATE.watchlist.size - 10} más`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/balance/, async (msg) => {
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey) / LAMPORTS_PER_SOL;
      STATE.bot.sendMessage(msg.chat.id,
        `💰 <b>Balance de Wallet</b>\n\n` +
        `${balance.toFixed(4)} SOL\n\n` +
        `<code>${STATE.wallet.publicKey.toString()}</code>`,
        { parse_mode: 'HTML' }
      );
    } catch (error) {
      log('ERROR', `Error en /balance: ${error.message}`);
    }
  });
  
  STATE.bot.onText(/\/config/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id,
      `⚙️ <b>Configuración</b>\n\n` +
      `<b>Trading:</b>\n` +
      `💰 Monto: ${CONFIG.TRADE_AMOUNT_SOL} SOL\n` +
      `📊 Slippage: ${CONFIG.SLIPPAGE}%\n` +
      `🎯 Modo: ${CONFIG.DRY_RUN ? 'DRY RUN' : 'REAL'}\n\n` +
      `<b>Señales:</b>\n` +
      `⚡ Velocidad temprana: +${CONFIG.EARLY_VELOCITY_MIN}%\n` +
      `✅ Confirmación: +${CONFIG.CONFIRMATION_VELOCITY}%\n` +
      `⏱️ Ventana: ${CONFIG.EARLY_TIME_WINDOW}s\n\n` +
      `<b>Take Profit:</b>\n` +
      `🎯 TP1: +${CONFIG.TAKE_PROFIT_1}%\n` +
      `🎯 TP2: +${CONFIG.TAKE_PROFIT_2}%\n` +
      `🎯 TP3: +${CONFIG.TAKE_PROFIT_3}%\n\n` +
      `<b>Stop Loss:</b>\n` +
      `🛑 Hard Stop: ${CONFIG.HARD_STOP_LOSS}%\n` +
      `⚡ Quick Stop: ${CONFIG.QUICK_STOP}%\n` +
      `🛡️ Trailing: ${CONFIG.TRAILING_PERCENT}%`,
      { parse_mode: 'HTML' }
    );
  });
  
  log('SUCCESS', '✅ Telegram bot configurado');
}

// ═══════════════════════════════════════════════════════════════
// SETUP WALLET
// ═══════════════════════════════════════════════════════════════

async function setupWallet() {
  try {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      throw new Error('WALLET_PRIVATE_KEY no configurada en .env');
    }
    
    const privateKeyBytes = decodeBase58(CONFIG.WALLET_PRIVATE_KEY);
    STATE.wallet = Keypair.fromSecretKey(new Uint8Array(privateKeyBytes));
    log('SUCCESS', `✅ Wallet: ${STATE.wallet.publicKey.toString().slice(0, 8)}...`);
    
    STATE.connection = new Connection(CONFIG.RPC_ENDPOINTS[0], 'confirmed');
    log('INFO', `🌐 Conectado a RPC: ${CONFIG.RPC_ENDPOINTS[0]}`);
    
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey);
      log('SUCCESS', `💰 Balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
    } catch (error) {
      log('WARN', `⚠️ No se pudo verificar balance: ${error.message}`);
    }
    
    return true;
  } catch (error) {
    log('ERROR', `❌ Error configurando wallet: ${error.message}`);
    return false;
  }
}

// ═══════════════════════════════════════════════════════════════
// LOOPS
// ═══════════════════════════════════════════════════════════════

async function watchlistLoop() {
  log('INFO', '🔄 Iniciando loop de análisis...');
  
  while (true) {
    try {
      if (STATE.watchlist.size > 0) {
        for (const mint of STATE.watchlist.keys()) {
          await analyzeToken(mint);
          await sleep(1000); // 1 segundo entre análisis
        }
      }
    } catch (error) {
      log('ERROR', `Error en watchlist loop: ${error.message}`);
    }
    
    await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
  }
}

async function positionsLoop() {
  log('INFO', '🔄 Iniciando loop de posiciones...');
  
  while (true) {
    try {
      if (STATE.positions.size > 0) {
        await monitorPositions();
      }
    } catch (error) {
      log('ERROR', `Error en positions loop: ${error.message}`);
    }
    
    await sleep(5000); // 5 segundos
  }
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
  console.log('\n');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  log('INFO', '🚀 PUMP.FUN TRADING BOT v2.0');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  console.log('\n');
  
  // Setup wallet
  log('INFO', '💼 Configurando wallet...');
  const walletOk = await setupWallet();
  if (!walletOk) {
    log('ERROR', '❌ No se pudo configurar wallet. Abortando.');
    process.exit(1);
  }
  
  console.log('\n');
  
  // Setup Telegram
  log('INFO', '💬 Configurando Telegram...');
  setupTelegramBot();
  
  console.log('\n');
  
  // Setup WebSocket
  log('INFO', '🌐 Conectando a PumpPortal...');
  setupWebSocket();
  
  // Esperar conexión
  await sleep(3000);
  
  console.log('\n');
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  log('SUCCESS', `✅ BOT INICIADO - MODO ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}`);
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  console.log('\n');
  
  log('INFO', `📊 Configuración de señales:`);
  log('INFO', `   ⚡ Velocidad temprana: +${CONFIG.EARLY_VELOCITY_MIN}% en ${CONFIG.EARLY_TIME_WINDOW}s`);
  log('INFO', `   ✅ Confirmación: +${CONFIG.CONFIRMATION_VELOCITY}% en ${CONFIG.CONFIRMATION_TIME}s`);
  log('INFO', `   💰 Liquidez mínima: ${CONFIG.MIN_LIQUIDITY_SOL} SOL`);
  log('INFO', `   🛒 Compras mínimas: ${CONFIG.MIN_BUY_COUNT}`);
  console.log('\n');
  
  await sendTelegram(
    `🚀 <b>Bot Iniciado</b>\n\n` +
    `Modo: ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n` +
    `💰 Inversión: ${CONFIG.TRADE_AMOUNT_SOL} SOL\n` +
    `📊 Max Posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}\n\n` +
    `<b>Señales:</b>\n` +
    `⚡ Temprana: +${CONFIG.EARLY_VELOCITY_MIN}%\n` +
    `✅ Confirmación: +${CONFIG.CONFIRMATION_VELOCITY}%\n\n` +
    `<b>Stop Loss:</b>\n` +
    `🛑 Hard: ${CONFIG.HARD_STOP_LOSS}%\n` +
    `🛡️ Trailing: ${CONFIG.TRAILING_PERCENT}%`
  );
  
  // Iniciar loops
  watchlistLoop();
  positionsLoop();
}

// ═══════════════════════════════════════════════════════════════
// GRACEFUL SHUTDOWN
// ═══════════════════════════════════════════════════════════════

process.on('SIGTERM', async () => {
  log('WARN', '🛑 SIGTERM recibido, cerrando...');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>\n\nSIGTERM recibido');
  process.exit(0);
});

process.on('SIGINT', async () => {
  log('WARN', '🛑 SIGINT recibido, cerrando...');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>\n\nSIGINT recibido');
  process.exit(0);
});

// ═══════════════════════════════════════════════════════════════
// INICIO
// ═══════════════════════════════════════════════════════════════

main().catch(error => {
  log('ERROR', `❌ Error fatal: ${error.message}`);
  sendTelegram(`❌ <b>Error Fatal</b>\n\n${error.message}`);
  process.exit(1);
});
