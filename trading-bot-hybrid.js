// trading-bot-v7-SDK.js - PUMP.FUN BOT CON SDK OFICIAL
// ✅ Usa @pump-fun/pump-sdk oficial
// ✅ Cálculos de precio nativos y precisos
// ✅ Trading directo sin intermediarios
// ✅ WebSocket robusto con reconexión inteligente
// ✅ Memory leak corregido

require('dotenv').config();
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, PublicKey, Keypair, LAMPORTS_PER_SOL, Transaction, sendAndConfirmTransaction } = require('@solana/web3.js');
const { PumpSdk, getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount } = require('@pump-fun/pump-sdk');
const BN = require('bn.js');

// Importar bs58
let bs58;
try {
  bs58 = require('bs58');
  if (typeof bs58 !== 'function' && bs58.default) {
    bs58 = bs58.default;
  }
} catch (e) {
  console.error('Error importando bs58:', e.message);
  process.exit(1);
}

// ═══════════════════════════════════════════════════════════════
// CONFIGURACIÓN
// ═══════════════════════════════════════════════════════════════

const CONFIG = {
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY,
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
  DRY_RUN: process.env.DRY_RUN === 'true',
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007'),
  SLIPPAGE: parseFloat(process.env.SLIPPAGE || '5'), // % slippage
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  // Filtros
  MIN_REAL_SOL: parseFloat(process.env.MIN_REAL_SOL || '0.05'),
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '0'),
  MIN_K_GROWTH_PERCENT: parseFloat(process.env.MIN_K_GROWTH_PERCENT || '3'),
  MIN_PRICE_CHANGE_PERCENT: parseFloat(process.env.MIN_PRICE_CHANGE_PERCENT || '8'),
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '30'),
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '45'),
  
  // Timing
  MAX_WATCH_TIME_SEC: parseInt(process.env.MAX_WATCH_TIME_SEC || '120'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  PRICE_CHECK_INTERVAL_SEC: parseInt(process.env.PRICE_CHECK_INTERVAL_SEC || '3'),
  
  // Stop Loss / Take Profit
  HARD_STOP_LOSS: parseFloat(process.env.HARD_STOP_LOSS || '-45'),
  QUICK_STOP: parseFloat(process.env.QUICK_STOP || '-25'),
  TRAILING_ACTIVATION: parseFloat(process.env.TRAILING_ACTIVATION || '40'),
  TRAILING_PERCENT: parseFloat(process.env.TRAILING_PERCENT || '-20'),
  TAKE_PROFIT_1: parseFloat(process.env.TAKE_PROFIT_1 || '80'),
  
  // RPC
  RPC_ENDPOINT: process.env.RPC_ENDPOINT || 'https://api.mainnet-beta.solana.com',
  
  // Memory management
  MAX_WATCHLIST_SIZE: 30,
  MAX_PRICE_HISTORY: 25,
  MIN_CHECKS_BEFORE_FILTER: 3,
  RPC_REQUEST_DELAY: 150,
};

// ═══════════════════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

const STATE = {
  wallet: null,
  connection: null,
  pumpSdk: null,
  globalData: null, // Cache de global data
  bot: null,
  ws: null,
  wsReconnecting: false,
  positions: new Map(),
  watchlist: new Map(),
  lastRpcCall: 0,
  stats: {
    detected: 0,
    analyzing: 0,
    filtered: 0,
    totalTrades: 0,
    wins: 0,
    losses: 0,
    totalPnl: 0,
    crashes: 0
  }
};

// ═══════════════════════════════════════════════════════════════
// LOGGER
// ═══════════════════════════════════════════════════════════════

function log(level, message) {
  const timestamp = new Date().toISOString();
  const emoji = {
    'INFO': 'ℹ️',
    'SUCCESS': '✅',
    'WARN': '⚠️',
    'ERROR': '❌',
    'DEBUG': '🔍'
  }[level] || 'ℹ️';
  
  console.log(`[${level}] ${timestamp} ${emoji} ${message}`);
}

// ═══════════════════════════════════════════════════════════════
// UTILIDADES
// ═══════════════════════════════════════════════════════════════

function decodeBase58(str) {
  try {
    if (typeof bs58 === 'function') {
      return bs58(str);
    } else if (bs58.decode) {
      return bs58.decode(str);
    }
    throw new Error('bs58 no disponible');
  } catch (e) {
    log('ERROR', `Error decodificando base58: ${e.message}`);
    throw e;
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function rateLimitedRpcCall(fn) {
  const now = Date.now();
  const timeSinceLastCall = now - STATE.lastRpcCall;
  
  if (timeSinceLastCall < CONFIG.RPC_REQUEST_DELAY) {
    await sleep(CONFIG.RPC_REQUEST_DELAY - timeSinceLastCall);
  }
  
  STATE.lastRpcCall = Date.now();
  return await fn();
}

async function sendTelegram(message) {
  if (!STATE.bot || !CONFIG.TELEGRAM_CHAT_ID) return;
  
  try {
    await STATE.bot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, { parse_mode: 'HTML' });
  } catch (error) {
    log('DEBUG', `Error Telegram: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// PUMP SDK - OBTENER PRECIO Y DATOS DE BONDING CURVE
// ═══════════════════════════════════════════════════════════════

async function fetchBondingCurveData(mint) {
  try {
    return await rateLimitedRpcCall(async () => {
      const mintPubkey = new PublicKey(mint);
      
      // Fetch bonding curve state
      const { bondingCurve, bondingCurveAccountInfo } = await STATE.pumpSdk.fetchBondingCurve(mintPubkey);
      
      if (!bondingCurve || !bondingCurveAccountInfo) {
        return null;
      }
      
      // Extraer datos de la bonding curve
      const virtualTokenReserves = bondingCurve.virtualTokenReserves.toNumber();
      const virtualSolReserves = bondingCurve.virtualSolReserves.toNumber();
      const realTokenReserves = bondingCurve.realTokenReserves.toNumber();
      const realSolReserves = bondingCurve.realSolReserves.toNumber();
      const complete = bondingCurve.complete;
      
      if (virtualTokenReserves <= 0 || virtualSolReserves <= 0) {
        return null;
      }
      
      // Calcular precio
      const solAmount = virtualSolReserves / LAMPORTS_PER_SOL;
      const tokenAmount = virtualTokenReserves / 1e6;
      const priceSol = solAmount / tokenAmount;
      const realSolLiquidity = realSolReserves / LAMPORTS_PER_SOL;
      const K = virtualSolReserves * virtualTokenReserves;
      
      return {
        priceSol,
        priceUsd: priceSol * 180,
        realSolReserves: realSolLiquidity,
        virtualSolReserves: solAmount,
        virtualTokenReserves: tokenAmount,
        realTokenReserves: realTokenReserves / 1e6,
        complete,
        K,
        marketCap: (priceSol * 180 * 1_000_000_000),
        bondingCurve, // Guardar para trading
        bondingCurveAccountInfo
      };
    });
    
  } catch (error) {
    if (error.message.includes('429')) {
      log('WARN', 'Rate limit RPC');
      await sleep(2000);
    }
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════
// PUMP SDK - TRADING DIRECTO
// ═══════════════════════════════════════════════════════════════

async function buyTokenWithSDK(mint, solAmount) {
  log('INFO', `💰 Comprando ${solAmount} SOL de ${mint.slice(0, 8)}...`);
  
  if (CONFIG.DRY_RUN) {
    log('SUCCESS', '[DRY RUN] ✅ Compra simulada');
    return 'dry-run-signature';
  }
  
  try {
    const mintPubkey = new PublicKey(mint);
    const solAmountLamports = new BN(solAmount * LAMPORTS_PER_SOL);
    
    // Fetch global data (cache)
    if (!STATE.globalData) {
      STATE.globalData = await STATE.pumpSdk.fetchGlobal();
    }
    
    // Fetch buy state
    const { bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo } = 
      await STATE.pumpSdk.fetchBuyState(mintPubkey, STATE.wallet.publicKey);
    
    // Calcular cantidad de tokens
    const tokenAmount = getBuyTokenAmountFromSolAmount(
      STATE.globalData,
      bondingCurve,
      solAmountLamports
    );
    
    // Crear instrucciones de compra
    const instructions = await STATE.pumpSdk.buyInstructions({
      global: STATE.globalData,
      bondingCurveAccountInfo,
      bondingCurve,
      associatedUserAccountInfo,
      mint: mintPubkey,
      user: STATE.wallet.publicKey,
      solAmount: solAmountLamports,
      amount: tokenAmount,
      slippage: CONFIG.SLIPPAGE
    });
    
    // Crear y enviar transacción
    const transaction = new Transaction().add(...instructions);
    const signature = await sendAndConfirmTransaction(
      STATE.connection,
      transaction,
      [STATE.wallet],
      { commitment: 'confirmed' }
    );
    
    log('SUCCESS', `✅ Compra: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `Error comprando: ${error.message}`);
    throw error;
  }
}

async function sellTokenWithSDK(mint, percentage = 100) {
  log('INFO', `💸 Vendiendo ${percentage}% de ${mint.slice(0, 8)}...`);
  
  if (CONFIG.DRY_RUN) {
    log('SUCCESS', '[DRY RUN] ✅ Venta simulada');
    return 'dry-run-signature';
  }
  
  try {
    const mintPubkey = new PublicKey(mint);
    
    // Fetch global data
    if (!STATE.globalData) {
      STATE.globalData = await STATE.pumpSdk.fetchGlobal();
    }
    
    // Fetch sell state
    const { bondingCurveAccountInfo, bondingCurve } = 
      await STATE.pumpSdk.fetchSellState(mintPubkey, STATE.wallet.publicKey);
    
    // Obtener balance de tokens del usuario
    const userTokenAccount = await STATE.connection.getTokenAccountsByOwner(
      STATE.wallet.publicKey,
      { mint: mintPubkey }
    );
    
    if (userTokenAccount.value.length === 0) {
      throw new Error('No token account found');
    }
    
    const accountInfo = await STATE.connection.getTokenAccountBalance(
      userTokenAccount.value[0].pubkey
    );
    
    const userBalance = new BN(accountInfo.value.amount);
    const amountToSell = userBalance.mul(new BN(percentage)).div(new BN(100));
    
    // Calcular SOL output
    const solOutput = getSellSolAmountFromTokenAmount(
      STATE.globalData,
      bondingCurve,
      amountToSell
    );
    
    // Crear instrucciones de venta
    const instructions = await STATE.pumpSdk.sellInstructions({
      global: STATE.globalData,
      bondingCurveAccountInfo,
      bondingCurve,
      mint: mintPubkey,
      user: STATE.wallet.publicKey,
      amount: amountToSell,
      solAmount: solOutput,
      slippage: CONFIG.SLIPPAGE
    });
    
    // Crear y enviar transacción
    const transaction = new Transaction().add(...instructions);
    const signature = await sendAndConfirmTransaction(
      STATE.connection,
      transaction,
      [STATE.wallet],
      { commitment: 'confirmed' }
    );
    
    log('SUCCESS', `✅ Venta: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `Error vendiendo: ${error.message}`);
    throw error;
  }
}

// ═══════════════════════════════════════════════════════════════
// BUY COUNT (OPCIONAL - DEXSCREENER)
// ═══════════════════════════════════════════════════════════════

async function getTokenBuyCount(mint) {
  try {
    const response = await axios.get(`https://api.dexscreener.com/latest/dex/tokens/${mint}`, {
      timeout: 5000
    });
    
    if (!response.data?.pairs || response.data.pairs.length === 0) return 0;
    return response.data.pairs[0].txns?.m5?.buys || 0;
    
  } catch (error) {
    return 0;
  }
}

// ═══════════════════════════════════════════════════════════════
// ANÁLISIS DE TOKENS
// ═══════════════════════════════════════════════════════════════

async function analyzeToken(mint) {
  const watch = STATE.watchlist.get(mint);
  if (!watch) return;
  
  try {
    const now = Date.now();
    const elapsed = (now - watch.firstSeen) / 1000;
    
    // Timeout
    if (elapsed > CONFIG.MAX_WATCH_TIME_SEC) {
      STATE.watchlist.delete(mint);
      STATE.stats.analyzing = STATE.watchlist.size;
      return;
    }
    
    // Obtener datos de bonding curve
    const curveData = await fetchBondingCurveData(mint);
    
    if (!curveData || !curveData.priceSol) {
      if (watch.checksCount % 5 === 0) {
        log('DEBUG', `${watch.symbol}: Esperando datos (${elapsed.toFixed(0)}s)`);
      }
      watch.checksCount = (watch.checksCount || 0) + 1;
      return;
    }
    
    // Si la curva está completa, ignorar
    if (curveData.complete) {
      log('WARN', `${watch.symbol}: Bonding curve completa - migrado a PumpSwap`);
      STATE.watchlist.delete(mint);
      return;
    }
    
    const priceSol = curveData.priceSol;
    const realSolLiquidity = curveData.realSolReserves || 0;
    const K = curveData.K || 0;
    
    // Guardar historial
    watch.priceHistory.push({ price: priceSol, K, time: now });
    if (watch.priceHistory.length > CONFIG.MAX_PRICE_HISTORY) {
      watch.priceHistory.shift();
    }
    
    watch.lastPrice = priceSol;
    watch.realSolLiquidity = realSolLiquidity;
    watch.K = K;
    watch.bondingCurve = curveData.bondingCurve; // Guardar para trading
    watch.checksCount = (watch.checksCount || 0) + 1;
    
    // Buy count (opcional)
    if (watch.checksCount % 3 === 0 && !watch.buyCount) {
      getTokenBuyCount(mint).then(count => {
        watch.buyCount = count;
      }).catch(() => {});
    }
    
    // Calcular velocidad
    const windowStart = now - CONFIG.EARLY_TIME_WINDOW * 1000;
    const recentPrices = watch.priceHistory.filter(p => p.time >= windowStart);
    
    if (recentPrices.length < 2) {
      return;
    }
    
    const firstK = recentPrices[0].K || 1;
    const currentK = K;
    const kGrowth = firstK > 0 ? ((currentK - firstK) / firstK) * 100 : 0;
    
    const firstPrice = recentPrices[0].price;
    const priceChange = firstPrice > 0 ? ((priceSol - firstPrice) / firstPrice) * 100 : 0;
    
    if (watch.checksCount % 4 === 0) {
      log('INFO', `[📊] ${watch.symbol}: ${priceChange > 0 ? '+' : ''}${priceChange.toFixed(1)}% | K: ${kGrowth > 0 ? '+' : ''}${kGrowth.toFixed(1)}% | Liq: ${realSolLiquidity.toFixed(2)} SOL`);
    }
    
    // Señal temprana
    const hasKSignal = kGrowth >= CONFIG.MIN_K_GROWTH_PERCENT;
    const hasPriceSignal = priceChange >= CONFIG.MIN_PRICE_CHANGE_PERCENT;
    
    if (!watch.earlySignal && elapsed <= CONFIG.EARLY_TIME_WINDOW && (hasKSignal || hasPriceSignal)) {
      watch.earlySignal = true;
      watch.signalTime = now;
      const signalType = hasKSignal ? 'K' : 'Precio';
      
      log('SUCCESS', `[⚡ SEÑAL ${signalType}] ${watch.symbol} K+${kGrowth.toFixed(1)}% | P+${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s`);
      
      await sendTelegram(
        `⚡ <b>SEÑAL TEMPRANA (${signalType})</b>\n\n` +
        `${watch.name} (${watch.symbol})\n` +
        `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
        `📈 Precio: +${priceChange.toFixed(1)}%\n` +
        `🔁 K Growth: +${kGrowth.toFixed(1)}%\n` +
        `💵 Liq: ${realSolLiquidity.toFixed(2)} SOL\n` +
        `🛒 Buys: ${watch.buyCount || 0}`
      );
    }
    
    // Confirmación
    if (watch.earlySignal && elapsed >= CONFIG.CONFIRMATION_TIME) {
      
      if (watch.checksCount < CONFIG.MIN_CHECKS_BEFORE_FILTER) {
        log('DEBUG', `${watch.symbol}: Esperando más checks (${watch.checksCount}/${CONFIG.MIN_CHECKS_BEFORE_FILTER})`);
        return;
      }
      
      const confirmKGrowth = kGrowth >= (CONFIG.MIN_K_GROWTH_PERCENT * 0.5);
      const confirmPrice = priceChange >= (CONFIG.MIN_PRICE_CHANGE_PERCENT * 0.7);
      
      if (!confirmKGrowth && !confirmPrice) {
        log('WARN', `[FILTRO] ❌ ${watch.symbol}: Perdió momentum`);
        STATE.watchlist.delete(mint);
        STATE.stats.filtered++;
        return;
      }
      
      if (realSolLiquidity < CONFIG.MIN_REAL_SOL) {
        log('WARN', `[FILTRO] ❌ ${watch.symbol}: SOL bajo (${realSolLiquidity.toFixed(2)})`);
        STATE.watchlist.delete(mint);
        STATE.stats.filtered++;
        return;
      }
      
      if (CONFIG.MIN_BUY_COUNT > 0 && (watch.buyCount || 0) < CONFIG.MIN_BUY_COUNT) {
        log('WARN', `[FILTRO] ❌ ${watch.symbol}: Pocas compras (${watch.buyCount || 0})`);
        STATE.watchlist.delete(mint);
        STATE.stats.filtered++;
        return;
      }
      
      if (STATE.positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
        log('WARN', `⚠️ Límite de posiciones alcanzado`);
        return;
      }
      
      log('SUCCESS', `[🚀 CONFIRMADO] ${watch.symbol} +${priceChange.toFixed(1)}%`);
      
      await executeBuy(mint, watch, priceChange, elapsed);
    }
    
  } catch (error) {
    log('ERROR', `Error analizando ${mint.slice(0, 8)}: ${error.message}`);
    STATE.stats.crashes++;
  }
}

async function executeBuy(mint, watch, priceChange, elapsed) {
  try {
    const signature = await buyTokenWithSDK(mint, CONFIG.TRADE_AMOUNT_SOL);
    
    STATE.positions.set(mint, {
      mint,
      symbol: watch.symbol,
      name: watch.name,
      entryPrice: watch.lastPrice,
      entryTime: Date.now(),
      amount: CONFIG.TRADE_AMOUNT_SOL,
      highestPrice: watch.lastPrice,
      trailingActive: false,
      lastPnl: 0,
      entrySig: signature
    });
    
    STATE.watchlist.delete(mint);
    STATE.stats.totalTrades++;
    
    await sendTelegram(
      `🟢 <b>POSICIÓN ABIERTA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL @ ${watch.lastPrice.toFixed(10)}\n` +
      `📈 +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s\n` +
      `Tx: https://solscan.io/tx/${signature}`
    );
    
  } catch (error) {
    log('ERROR', `Error ejecutando compra: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// MONITOR DE POSICIONES
// ═══════════════════════════════════════════════════════════════

async function monitorPositions() {
  for (const [mint, pos] of STATE.positions.entries()) {
    try {
      const curveData = await fetchBondingCurveData(mint);
      if (!curveData) continue;
      
      const currentPrice = curveData.priceSol;
      const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
      const holdTime = (Date.now() - pos.entryTime) / 1000 / 60;
      
      if (currentPrice > pos.highestPrice) {
        pos.highestPrice = currentPrice;
      }
      
      if (!pos.trailingActive && pnlPercent >= CONFIG.TRAILING_ACTIVATION) {
        pos.trailingActive = true;
        log('INFO', `🛡️ Trailing activado ${pos.symbol} (+${pnlPercent.toFixed(1)}%)`);
      }
      
      let shouldSell = false;
      let sellPercentage = 100;
      let sellReason = '';
      
      if (pnlPercent <= CONFIG.HARD_STOP_LOSS) {
        shouldSell = true;
        sellReason = `Hard Stop (${pnlPercent.toFixed(1)}%)`;
      }
      else if (holdTime <= 2 && pnlPercent <= CONFIG.QUICK_STOP) {
        shouldSell = true;
        sellReason = `Quick Stop (${pnlPercent.toFixed(1)}%)`;
      }
      else if (pos.trailingActive) {
        const trailingStopPrice = pos.highestPrice * (1 + CONFIG.TRAILING_PERCENT / 100);
        if (currentPrice <= trailingStopPrice) {
          shouldSell = true;
          sellReason = `Trailing (${pnlPercent.toFixed(1)}%)`;
        }
      }
      else if (pnlPercent >= CONFIG.TAKE_PROFIT_1 && !pos.tp1Taken) {
        shouldSell = true;
        sellPercentage = 50;
        sellReason = `TP1 (${pnlPercent.toFixed(1)}%)`;
        pos.tp1Taken = true;
      }
      
      if (shouldSell) {
        await executeSell(mint, pos, sellPercentage, sellReason, pnlPercent);
      }
      
      pos.lastPnl = pnlPercent;
      
    } catch (error) {
      log('ERROR', `Error monitoreando ${mint.slice(0, 8)}: ${error.message}`);
    }
  }
}

async function executeSell(mint, pos, percentage, reason, pnlPercent) {
  try {
    const signature = await sellTokenWithSDK(mint, percentage);
    
    const isFullExit = percentage === 100;
    
    if (isFullExit) {
      STATE.positions.delete(mint);
      
      if (pnlPercent > 0) STATE.stats.wins++;
      else STATE.stats.losses++;
      STATE.stats.totalPnl += pnlPercent;
      
      const emoji = pnlPercent > 0 ? '🟢' : '🔴';
      await sendTelegram(
        `${emoji} <b>POSICIÓN CERRADA</b>\n\n` +
        `${pos.symbol}\n` +
        `📊 P&L: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(2)}%\n` +
        `🎯 ${reason}\n` +
        `Tx: https://solscan.io/tx/${signature}`
      );
    }
    
  } catch (error) {
    log('ERROR', `Error vendiendo: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// WEBSOCKET
// ═══════════════════════════════════════════════════════════════

let wsReconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 10;

function setupWebSocket() {
  if (STATE.ws) {
    try {
      STATE.ws.removeAllListeners();
      STATE.ws.close();
      STATE.ws = null;
    } catch (e) {}
  }
  
  if (STATE.wsReconnecting) {
    setTimeout(() => {
      STATE.wsReconnecting = false;
    }, 30000);
  }
  
  log('INFO', `📡 Conectando WebSocket... (intento ${wsReconnectAttempts + 1}/${MAX_RECONNECT_ATTEMPTS})`);
  
  try {
    STATE.ws = new WebSocket('wss://pumpportal.fun/api/data');
    
    const connectionTimeout = setTimeout(() => {
      if (STATE.ws.readyState !== WebSocket.OPEN) {
        log('ERROR', 'WebSocket timeout (10s)');
        STATE.ws.close();
      }
    }, 10000);
    
    STATE.ws.on('open', () => {
      clearTimeout(connectionTimeout);
      wsReconnectAttempts = 0;
      STATE.wsReconnecting = false;
      
      log('SUCCESS', '✅ WebSocket conectado');
      
      try {
        STATE.ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
        log('INFO', '📡 Suscrito a tokens nuevos');
      } catch (e) {
        log('ERROR', `Error suscribiendo: ${e.message}`);
      }
    });
    
    STATE.ws.on('message', async (data) => {
      try {
        const msg = JSON.parse(data.toString());
        
        if (msg.txType === 'create' || msg.mint) {
          const mint = msg.mint || msg.token;
          const name = msg.name || msg.tokenName || 'Unknown';
          const symbol = msg.symbol || msg.tokenSymbol || 'UNKNOWN';
          
          if (STATE.watchlist.has(mint)) return;
          
          // Limitar watchlist - eliminar tokens sin señal
          if (STATE.watchlist.size >= CONFIG.MAX_WATCHLIST_SIZE) {
            let oldestMint = null;
            let oldestTime = Date.now();
            
            for (const [m, w] of STATE.watchlist.entries()) {
              if (!w.earlySignal && w.firstSeen < oldestTime) {
                oldestTime = w.firstSeen;
                oldestMint = m;
              }
            }
            
            if (oldestMint) {
              STATE.watchlist.delete(oldestMint);
              log('DEBUG', `Watchlist llena - eliminando ${oldestMint.slice(0, 8)} (sin señal)`);
            }
          }
          
          STATE.stats.detected++;
          
          log('SUCCESS', `🆕 ${name} (${symbol}) - ${mint.slice(0, 8)}...`);
          
          STATE.watchlist.set(mint, {
            mint,
            name,
            symbol,
            firstSeen: Date.now(),
            priceHistory: [],
            buyCount: 0,
            realSolLiquidity: 0,
            K: 0,
            lastPrice: 0,
            earlySignal: false,
            checksCount: 0,
            bondingCurve: null
          });
          
          STATE.stats.analyzing = STATE.watchlist.size;
        }
      } catch (error) {
        log('ERROR', `Error WS message: ${error.message}`);
      }
    });
    
    STATE.ws.on('error', (error) => {
      log('ERROR', `WebSocket error: ${error.message}`);
      clearTimeout(connectionTimeout);
    });
    
    STATE.ws.on('close', (code, reason) => {
      clearTimeout(connectionTimeout);
      log('WARN', `⚠️ WebSocket cerrado (code: ${code})`);
      
      wsReconnectAttempts++;
      
      if (wsReconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        log('ERROR', '❌ Max intentos alcanzados - Esperando 5 min');
        wsReconnectAttempts = 0;
        setTimeout(() => {
          STATE.wsReconnecting = false;
          setupWebSocket();
        }, 300000);
        return;
      }
      
      if (!STATE.wsReconnecting) {
        STATE.wsReconnecting = true;
        const delay = Math.min(5000 * wsReconnectAttempts, 30000);
        log('INFO', `🔄 Reconectando en ${delay / 1000}s...`);
        
        setTimeout(() => {
          STATE.wsReconnecting = false;
          setupWebSocket();
        }, delay);
      }
    });
    
  } catch (error) {
    log('ERROR', `Error creando WebSocket: ${error.message}`);
    STATE.wsReconnecting = false;
    
    wsReconnectAttempts++;
    if (wsReconnectAttempts <= MAX_RECONNECT_ATTEMPTS) {
      setTimeout(setupWebSocket, 10000);
    }
  }
}

// ═══════════════════════════════════════════════════════════════
// TELEGRAM BOT
// ═══════════════════════════════════════════════════════════════

function setupTelegramBot() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) {
    log('WARN', '⚠️ Telegram no configurado');
    return;
  }
  
  try {
    STATE.bot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { 
      polling: true,
      polling_options: { timeout: 10 }
    });
    
    STATE.bot.on('polling_error', (error) => {
      log('WARN', `Telegram polling error: ${error.code}`);
    });
    
    STATE.bot.onText(/\/start/, (msg) => {
      STATE.bot.sendMessage(msg.chat.id,
        `🤖 <b>Pump.fun Bot v7.0 - SDK Oficial</b>\n\n` +
        `<b>Comandos:</b>\n` +
        `/status - Estado del bot\n` +
        `/stats - Estadísticas\n` +
        `/positions - Posiciones activas\n` +
        `/watchlist - Tokens en análisis\n` +
        `/reconnect - Reconectar WebSocket\n` +
        `/refresh - Actualizar global data`,
        { parse_mode: 'HTML' }
      );
    });
    
    STATE.bot.onText(/\/reconnect/, (msg) => {
      log('INFO', 'Reconexión manual solicitada');
      wsReconnectAttempts = 0;
      STATE.wsReconnecting = false;
      setupWebSocket();
      STATE.bot.sendMessage(msg.chat.id, '🔄 Reconectando WebSocket...');
    });
    
    STATE.bot.onText(/\/refresh/, async (msg) => {
      try {
        STATE.globalData = await STATE.pumpSdk.fetchGlobal();
        STATE.bot.sendMessage(msg.chat.id, '✅ Global data actualizado');
      } catch (e) {
        STATE.bot.sendMessage(msg.chat.id, `❌ Error: ${e.message}`);
      }
    });
    
    STATE.bot.onText(/\/status/, async (msg) => {
      try {
        const balance = await STATE.connection.getBalance(STATE.wallet.publicKey) / LAMPORTS_PER_SOL;
        const wsStatus = STATE.ws?.readyState === WebSocket.OPEN ? '✅ OK' : '❌ FAIL';
        const wsAttempts = wsReconnectAttempts > 0 ? ` (${wsReconnectAttempts} intentos)` : '';
        
        STATE.bot.sendMessage(msg.chat.id,
          `📊 <b>Estado v7.0</b>\n\n` +
          `💰 Balance: ${balance.toFixed(4)} SOL\n` +
          `📈 Posiciones: ${STATE.positions.size}/${CONFIG.MAX_CONCURRENT_POSITIONS}\n` +
          `👀 Analizando: ${STATE.watchlist.size}/${CONFIG.MAX_WATCHLIST_SIZE}\n` +
          `🌐 WS: ${wsStatus}${wsAttempts}\n` +
          `🎯 ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n\n` +
          `🆕 Detectados: ${STATE.stats.detected}\n` +
          `🚫 Filtrados: ${STATE.stats.filtered}\n` +
          `💥 Crashes evitados: ${STATE.stats.crashes}`,
          { parse_mode: 'HTML' }
        );
      } catch (error) {
        log('ERROR', `Error /status: ${error.message}`);
      }
    });
    
    STATE.bot.onText(/\/stats/, (msg) => {
      const winRate = STATE.stats.totalTrades > 0 
        ? (STATE.stats.wins / STATE.stats.totalTrades * 100).toFixed(1) 
        : '0.0';
      
      STATE.bot.sendMessage(msg.chat.id,
        `📊 <b>Estadísticas</b>\n\n` +
        `📈 Trades: ${STATE.stats.totalTrades}\n` +
        `✅ Wins: ${STATE.stats.wins}\n` +
        `❌ Losses: ${STATE.stats.losses}\n` +
        `💹 Win Rate: ${winRate}%\n` +
        `💰 P&L Total: ${STATE.stats.totalPnl > 0 ? '+' : ''}${STATE.stats.totalPnl.toFixed(2)}%`,
        { parse_mode: 'HTML' }
      );
    });
    
    STATE.bot.onText(/\/positions/, (msg) => {
      if (STATE.positions.size === 0) {
        STATE.bot.sendMessage(msg.chat.id, '📊 No hay posiciones activas');
        return;
      }
      
      let text = '📊 <b>Posiciones Activas</b>\n\n';
      
      for (const [mint, pos] of STATE.positions.entries()) {
        const holdTime = ((Date.now() - pos.entryTime) / 60000).toFixed(1);
        const pnl = ((pos.lastPnl || 0)).toFixed(1);
        text += `<b>${pos.symbol}</b>\n`;
        text += `💰 ${pos.amount} SOL\n`;
        text += `📊 P&L: ${pnl}%\n`;
        text += `⏱️ ${holdTime}min\n`;
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
        text += `${watch.symbol} ${watch.earlySignal ? '⚡' : ''}\n`;
        text += `⏱️ ${elapsed}s | 🛒 ${watch.buyCount || 0}\n`;
        text += `💵 ${(watch.realSolLiquidity || 0).toFixed(2)} SOL\n\n`;
      }
      
      if (STATE.watchlist.size > 10) {
        text += `\n... y ${STATE.watchlist.size - 10} más`;
      }
      
      STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
    });
    
    log('SUCCESS', '✅ Telegram configurado');
    
  } catch (error) {
    log('ERROR', `Error configurando Telegram: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// SETUP
// ═══════════════════════════════════════════════════════════════

async function setupWallet() {
  try {
    if (!CONFIG.WALLET_PRIVATE_KEY) {
      throw new Error('WALLET_PRIVATE_KEY no configurada');
    }
    
    const privateKeyBytes = decodeBase58(CONFIG.WALLET_PRIVATE_KEY);
    STATE.wallet = Keypair.fromSecretKey(new Uint8Array(privateKeyBytes));
    log('SUCCESS', `✅ Wallet: ${STATE.wallet.publicKey.toString().slice(0, 8)}...`);
    
    STATE.connection = new Connection(CONFIG.RPC_ENDPOINT, 'confirmed');
    log('INFO', `🌐 RPC: ${CONFIG.RPC_ENDPOINT}`);
    
    // Inicializar Pump SDK
    STATE.pumpSdk = new PumpSdk(STATE.connection);
    log('SUCCESS', '✅ Pump SDK inicializado');
    
    // Fetch global data inicial
    try {
      STATE.globalData = await STATE.pumpSdk.fetchGlobal();
      log('SUCCESS', '✅ Global data cargado');
    } catch (e) {
      log('WARN', `⚠️ No se pudo cargar global data: ${e.message}`);
    }
    
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey);
      log('SUCCESS', `💰 Balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
      
      if (balance === 0) {
        log('WARN', '⚠️ Balance en 0');
      }
    } catch (error) {
      log('WARN', `⚠️ No se pudo verificar balance: ${error.message}`);
    }
    
    return true;
  } catch (error) {
    log('ERROR', `Error setup wallet: ${error.message}`);
    return false;
  }
}

// ═══════════════════════════════════════════════════════════════
// LOOPS
// ═══════════════════════════════════════════════════════════════

async function watchlistLoop() {
  log('INFO', '🔄 Loop de análisis iniciado');
  
  while (true) {
    try {
      if (STATE.watchlist.size > 0) {
        const mints = Array.from(STATE.watchlist.keys());
        
        for (const mint of mints) {
          try {
            await analyzeToken(mint);
            await sleep(500);
          } catch (e) {
            log('ERROR', `Error analizando ${mint.slice(0, 8)}: ${e.message}`);
            STATE.stats.crashes++;
          }
        }
      }
      
      await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
      
    } catch (error) {
      log('ERROR', `Error watchlist loop: ${error.message}`);
      STATE.stats.crashes++;
      await sleep(5000);
    }
  }
}

async function positionsLoop() {
  log('INFO', '🔄 Loop de posiciones iniciado');
  
  while (true) {
    try {
      if (STATE.positions.size > 0) {
        await monitorPositions();
      }
      
      await sleep(5000);
      
    } catch (error) {
      log('ERROR', `Error positions loop: ${error.message}`);
      STATE.stats.crashes++;
      await sleep(5000);
    }
  }
}

async function healthCheckLoop() {
  log('INFO', '🔄 Health check iniciado');
  
  while (true) {
    await sleep(60000);
    
    try {
      const wsStatus = STATE.ws?.readyState === WebSocket.OPEN ? 'OK' : 'FAIL';
      log('INFO', `💓 Health: WS=${wsStatus} | Watch=${STATE.watchlist.size} | Pos=${STATE.positions.size}`);
      
      if (wsStatus === 'FAIL' && !STATE.wsReconnecting) {
        log('WARN', '⚠️ WebSocket caído - Reconectando...');
        wsReconnectAttempts = 0;
        setupWebSocket();
      }
      
      // Limpiar watchlist estancada
      const now = Date.now();
      for (const [mint, watch] of STATE.watchlist.entries()) {
        const elapsed = (now - watch.firstSeen) / 1000;
        if (elapsed > CONFIG.MAX_WATCH_TIME_SEC * 2) {
          STATE.watchlist.delete(mint);
          log('DEBUG', `Limpieza: ${mint.slice(0, 8)} estancado`);
        }
      }
      
      // Refrescar global data cada hora
      if (Date.now() % 3600000 < 60000) {
        try {
          STATE.globalData = await STATE.pumpSdk.fetchGlobal();
          log('DEBUG', 'Global data actualizado');
        } catch (e) {
          log('WARN', `No se pudo actualizar global data: ${e.message}`);
        }
      }
      
    } catch (error) {
      log('ERROR', `Error health check: ${error.message}`);
    }
  }
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
  console.log('\n');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  log('INFO', '🚀 PUMP.FUN BOT v7.0 - PUMP SDK OFICIAL');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  console.log('\n');
  
  const walletOk = await setupWallet();
  if (!walletOk) {
    log('ERROR', '❌ Error fatal. Abortando.');
    process.exit(1);
  }
  
  console.log('\n');
  
  setupTelegramBot();
  setupWebSocket();
  
  await sleep(5000);
  
  console.log('\n');
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  log('SUCCESS', `✅ BOT INICIADO - ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}`);
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  console.log('\n');
  
  log('INFO', `⚡ K Growth: +${CONFIG.MIN_K_GROWTH_PERCENT}% | Precio: +${CONFIG.MIN_PRICE_CHANGE_PERCENT}%`);
  log('INFO', `✅ Confirmación: ${CONFIG.CONFIRMATION_TIME}s`);
  log('INFO', `💰 SOL mínimo: ${CONFIG.MIN_REAL_SOL}`);
  log('INFO', `🛒 Buys mínimas: ${CONFIG.MIN_BUY_COUNT}`);
  log('INFO', `📊 Max posiciones: ${CONFIG.MAX_CONCURRENT_POSITIONS}`);
  log('INFO', `🔬 Método: Pump SDK Oficial`);
  console.log('\n');
  
  await sendTelegram(
    `🚀 <b>Bot v7.0 - Pump SDK Oficial</b>\n\n` +
    `${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n\n` +
    `💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL/trade\n` +
    `📊 Max: ${CONFIG.MAX_CONCURRENT_POSITIONS} posiciones\n\n` +
    `<b>Filtros:</b>\n` +
    `⚡ K: +${CONFIG.MIN_K_GROWTH_PERCENT}% | Precio: +${CONFIG.MIN_PRICE_CHANGE_PERCENT}%\n` +
    `💵 SOL: ${CONFIG.MIN_REAL_SOL} | Buys: ${CONFIG.MIN_BUY_COUNT}\n\n` +
    `<b>v7.0 Features:</b>\n` +
    `✅ Pump SDK oficial @pump-fun/pump-sdk\n` +
    `✅ Trading directo sin PumpPortal\n` +
    `✅ Cálculos nativos de bonding curve\n` +
    `✅ WebSocket robusto con auto-recovery\n` +
    `✅ Memory management optimizado`
  );
  
  watchlistLoop().catch(e => log('ERROR', `Watchlist crashed: ${e.message}`));
  positionsLoop().catch(e => log('ERROR', `Positions crashed: ${e.message}`));
  healthCheckLoop().catch(e => log('ERROR', `Health check crashed: ${e.message}`));
}

// ═══════════════════════════════════════════════════════════════
// SHUTDOWN
// ═══════════════════════════════════════════════════════════════

let isShuttingDown = false;

async function gracefulShutdown(signal) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  
  log('WARN', `🛑 ${signal} recibido`);
  
  try {
    if (STATE.ws) {
      STATE.ws.removeAllListeners();
      STATE.ws.close();
    }
    
    await sendTelegram(`🛑 <b>Bot Detenido (${signal})</b>`);
    await sleep(2000);
    
  } catch (error) {
    log('ERROR', `Error shutdown: ${error.message}`);
  } finally {
    process.exit(0);
  }
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

process.on('unhandledRejection', (reason) => {
  log('ERROR', `Unhandled Rejection: ${reason}`);
  STATE.stats.crashes++;
});

process.on('uncaughtException', (error) => {
  log('ERROR', `Uncaught Exception: ${error.message}`);
  STATE.stats.crashes++;
  sendTelegram(`⚠️ <b>Error Recuperado</b>\n\n${error.message}`);
});

// ═══════════════════════════════════════════════════════════════
// START
// ═══════════════════════════════════════════════════════════════

main().catch(error => {
  log('ERROR', `Error fatal: ${error.message}`);
  sendTelegram(`❌ <b>Error Fatal</b>\n\n${error.message}`).finally(() => {
    process.exit(1);
  });
});
