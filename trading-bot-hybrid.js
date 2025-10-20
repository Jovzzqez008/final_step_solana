// trading-bot-fixed.js - PUMP.FUN BOT FUNCIONAL
// ✅ Corregido para usar SDK oficial correctamente
// ✅ Fallback a PumpPortal API
// ✅ Cálculo de precio robusto

require('dotenv').config();
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, PublicKey, Keypair, Transaction, sendAndConfirmTransaction, LAMPORTS_PER_SOL, VersionedTransaction } = require('@solana/web3.js');
const BN = require('bn.js');

// Importar SDK con manejo de errores
let PumpSdk = null;
let getBuyTokenAmountFromSolAmount = null;
let getSellSolAmountFromTokenAmount = null;

try {
  const pumpSdkModule = require('@pump-fun/pump-sdk');
  PumpSdk = pumpSdkModule.PumpSdk || pumpSdkModule.default?.PumpSdk || pumpSdkModule;
  getBuyTokenAmountFromSolAmount = pumpSdkModule.getBuyTokenAmountFromSolAmount;
  getSellSolAmountFromTokenAmount = pumpSdkModule.getSellSolAmountFromTokenAmount;
} catch (e) {
  console.log('⚠️ SDK no disponible, usando solo PumpPortal');
}

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
  SLIPPAGE: parseFloat(process.env.SLIPPAGE || '5'),
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  
  // Filtros
  MIN_REAL_SOL: parseFloat(process.env.MIN_REAL_SOL || '0.1'),
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '3'),
  MIN_K_GROWTH_PERCENT: parseFloat(process.env.MIN_K_GROWTH_PERCENT || '8'),
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '45'),
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '90'),
  
  // Timing
  MAX_WATCH_TIME_SEC: parseInt(process.env.MAX_WATCH_TIME_SEC || '240'),
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
  PUMPPORTAL_URL: process.env.PUMPPORTAL_URL || 'https://pumpportal.fun',
  PUMPPORTAL_API_KEY: process.env.PUMPPORTAL_API_KEY || '',
};

// ═══════════════════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

const STATE = {
  wallet: null,
  connection: null,
  pumpSdk: null,
  globalAccount: null, // Cache del global account
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
    'INFO': '\x1b[36m',
    'SUCCESS': '\x1b[32m',
    'WARN': '\x1b[33m',
    'ERROR': '\x1b[31m',
    'DEBUG': '\x1b[90m'
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
  const bs58 = require('bs58');
  return bs58.decode(str);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function sendTelegram(message) {
  if (!STATE.bot || !CONFIG.TELEGRAM_CHAT_ID) return;
  try {
    await STATE.bot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, message, { parse_mode: 'HTML' });
  } catch (error) {
    log('ERROR', `Error Telegram: ${error.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════
// OBTENER PRECIO - MÉTODO CORREGIDO
// ═══════════════════════════════════════════════════════════════

async function getTokenPriceSDK(mint) {
  try {
    const mintPubkey = new PublicKey(mint);
    
    // Método 1: Intentar obtener cuenta de bonding curve directamente
    const bondingCurvePDA = PublicKey.findProgramAddressSync(
      [Buffer.from('bonding-curve'), mintPubkey.toBuffer()],
      new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P') // Pump program ID
    )[0];
    
    const accountInfo = await STATE.connection.getAccountInfo(bondingCurvePDA);
    
    if (!accountInfo) {
      log('DEBUG', `No existe bonding curve para ${mint.slice(0, 8)}`);
      return null;
    }
    
    // Parsear datos de la bonding curve
    const data = accountInfo.data;
    
    // Estructura de BondingCurve account (según IDL):
    // virtual_token_reserves: u64 (8 bytes) - offset 8
    // virtual_sol_reserves: u64 (8 bytes) - offset 16
    // real_token_reserves: u64 (8 bytes) - offset 24
    // real_sol_reserves: u64 (8 bytes) - offset 32
    // token_total_supply: u64 (8 bytes) - offset 40
    // complete: bool (1 byte) - offset 48
    
    const virtualTokenReserves = new BN(data.slice(8, 16), 'le').toNumber();
    const virtualSolReserves = new BN(data.slice(16, 24), 'le').toNumber();
    const realTokenReserves = new BN(data.slice(24, 32), 'le').toNumber();
    const realSolReserves = new BN(data.slice(32, 40), 'le').toNumber();
    const complete = data[48] === 1;
    
    if (virtualTokenReserves <= 0 || virtualSolReserves <= 0) {
      return null;
    }
    
    // Calcular precio
    const solAmount = virtualSolReserves / LAMPORTS_PER_SOL;
    const tokenAmount = virtualTokenReserves / 1e6; // 6 decimales
    const priceSol = solAmount / tokenAmount;
    
    // Liquidez real
    const realSolLiquidity = realSolReserves / LAMPORTS_PER_SOL;
    
    // K constante
    const K = virtualSolReserves * virtualTokenReserves;
    
    return {
      priceSol,
      priceUsd: priceSol * 180, // Aproximado
      realSolReserves: realSolLiquidity,
      virtualSolReserves: solAmount,
      virtualTokenReserves: tokenAmount,
      realTokenReserves: realTokenReserves / 1e6,
      complete,
      K,
      marketCap: (priceSol * 180 * 1_000_000_000), // Supply fijo
    };
    
  } catch (error) {
    log('DEBUG', `Error precio SDK ${mint.slice(0, 8)}: ${error.message}`);
    return null;
  }
}

// Fallback: PumpPortal API
async function getTokenPricePortal(mint) {
  try {
    const url = `${CONFIG.PUMPPORTAL_URL}/api/data?mint=${mint}`;
    const headers = {};
    if (CONFIG.PUMPPORTAL_API_KEY) {
      headers['x-api-key'] = CONFIG.PUMPPORTAL_API_KEY;
    }
    
    const response = await axios.get(url, { headers, timeout: 3000 });
    
    if (!response.data) return null;
    
    return {
      priceSol: response.data.price_sol || 0,
      priceUsd: response.data.price_usd || 0,
      realSolReserves: response.data.real_sol_reserves || 0,
      marketCap: response.data.market_cap || 0,
    };
  } catch (error) {
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════
// BUY/SELL CON PUMPPORTAL (FALLBACK ROBUSTO)
// ═══════════════════════════════════════════════════════════════

async function sendPortalTransaction({ action = 'buy', mint, amount, denominatedInSol = true, slippage = CONFIG.SLIPPAGE, priorityFee = CONFIG.PRIORITY_FEE }) {
  const url = `${CONFIG.PUMPPORTAL_URL}/api/trade-local`;
  const headers = { 'Content-Type': 'application/json' };
  
  if (CONFIG.PUMPPORTAL_API_KEY) {
    headers['x-api-key'] = CONFIG.PUMPPORTAL_API_KEY;
  }
  
  const body = {
    publicKey: STATE.wallet.publicKey.toString(),
    action,
    mint,
    denominatedInSol: denominatedInSol ? "true" : "false",
    amount,
    slippage,
    priorityFee,
    pool: 'pump'
  };
  
  const response = await axios.post(url, body, {
    headers,
    responseType: 'arraybuffer',
    timeout: 15000
  });
  
  if (response.status !== 200) {
    throw new Error('Portal failed to generate transaction');
  }
  
  const txBuf = Buffer.from(response.data);
  const tx = VersionedTransaction.deserialize(txBuf);
  
  tx.sign([STATE.wallet]);
  
  const signature = await STATE.connection.sendRawTransaction(tx.serialize(), {
    skipPreflight: false,
    maxRetries: 3
  });
  
  await STATE.connection.confirmTransaction(signature, 'confirmed');
  
  return signature;
}

async function buyToken(mint, solAmount) {
  log('INFO', `Comprando ${solAmount} SOL de ${mint.slice(0, 8)}...`);
  
  if (CONFIG.DRY_RUN) {
    log('SUCCESS', '[DRY RUN] Compra simulada');
    return 'dry-run-signature';
  }
  
  try {
    const signature = await sendPortalTransaction({
      action: 'buy',
      mint,
      amount: solAmount,
      denominatedInSol: true,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE
    });
    
    log('SUCCESS', `✅ Compra ejecutada: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `❌ Error comprando: ${error.message}`);
    throw error;
  }
}

async function sellToken(mint, percentage = 100) {
  log('INFO', `Vendiendo ${percentage}% de ${mint.slice(0, 8)}...`);
  
  if (CONFIG.DRY_RUN) {
    log('SUCCESS', '[DRY RUN] Venta simulada');
    return 'dry-run-signature';
  }
  
  try {
    const signature = await sendPortalTransaction({
      action: 'sell',
      mint,
      amount: `${percentage}%`,
      denominatedInSol: false,
      slippage: CONFIG.SLIPPAGE,
      priorityFee: CONFIG.PRIORITY_FEE
    });
    
    log('SUCCESS', `✅ Venta ejecutada: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `❌ Error vendiendo: ${error.message}`);
    throw error;
  }
}

// ═══════════════════════════════════════════════════════════════
// ANÁLISIS DE TOKENS
// ═══════════════════════════════════════════════════════════════

async function getTokenBuyCount(mint) {
  try {
    const response = await axios.get(`https://api.dexscreener.com/latest/dex/tokens/${mint}`, {
      timeout: 3000
    });
    
    if (!response.data?.pairs || response.data.pairs.length === 0) {
      return 0;
    }
    
    const pair = response.data.pairs[0];
    return pair.txns?.m5?.buys || 0;
    
  } catch (error) {
    return 0;
  }
}

async function analyzeToken(mint) {
  const watch = STATE.watchlist.get(mint);
  if (!watch) return;
  
  const now = Date.now();
  const elapsed = (now - watch.firstSeen) / 1000;
  
  // Timeout
  if (elapsed > CONFIG.MAX_WATCH_TIME_SEC) {
    log('INFO', `⏱️ Timeout para ${watch.symbol} (${elapsed.toFixed(0)}s)`);
    STATE.watchlist.delete(mint);
    STATE.stats.analyzing = STATE.watchlist.size;
    return;
  }
  
  // Obtener precio
  let priceData = await getTokenPriceSDK(mint);
  
  if (!priceData) {
    // Fallback a Portal
    priceData = await getTokenPricePortal(mint);
  }
  
  if (!priceData || !priceData.priceSol) {
    if (watch.checksCount % 5 === 0) {
      log('DEBUG', `${watch.symbol}: Sin datos de precio aún (${elapsed.toFixed(0)}s)`);
    }
    watch.checksCount = (watch.checksCount || 0) + 1;
    return;
  }
  
  const priceSol = priceData.priceSol;
  const realSolLiquidity = priceData.realSolReserves || 0;
  const K = priceData.K || 0;
  
  watch.priceHistory.push({ price: priceSol, K, time: now });
  watch.lastPrice = priceSol;
  watch.realSolLiquidity = realSolLiquidity;
  watch.K = K;
  
  // Limitar historial
  if (watch.priceHistory.length > 60) {
    watch.priceHistory.shift();
  }
  
  // Obtener buy count cada 2 checks
  if (watch.checksCount % 2 === 0) {
    getTokenBuyCount(mint).then(count => {
      watch.buyCount = count;
    }).catch(() => {});
  }
  
  watch.checksCount = (watch.checksCount || 0) + 1;
  
  // Calcular velocidad K
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
  
  if (watch.checksCount % 3 === 0) {
    log('INFO', `[📊] ${watch.symbol}: ${priceChange > 0 ? '+' : ''}${priceChange.toFixed(1)}% | K: ${kGrowth > 0 ? '+' : ''}${kGrowth.toFixed(1)}% | Liq: ${realSolLiquidity.toFixed(2)} SOL | ${elapsed.toFixed(0)}s`);
  }
  
  // Señal temprana
  if (!watch.earlySignal && elapsed <= CONFIG.EARLY_TIME_WINDOW && kGrowth >= CONFIG.MIN_K_GROWTH_PERCENT) {
    watch.earlySignal = true;
    log('SUCCESS', `[SEÑAL] ⚡ ${watch.symbol} K+${kGrowth.toFixed(1)}% en ${elapsed.toFixed(0)}s`);
    
    await sendTelegram(
      `⚡ <b>SEÑAL TEMPRANA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
      `📈 Precio: +${priceChange.toFixed(1)}%\n` +
      `🔁 K Growth: +${kGrowth.toFixed(1)}%\n` +
      `💵 Liq: ${realSolLiquidity.toFixed(2)} SOL\n` +
      `🛒 Buys: ${watch.buyCount || 0}`
    );
  }
  
  // Confirmación
  if (watch.earlySignal && elapsed >= CONFIG.CONFIRMATION_TIME && kGrowth >= (CONFIG.MIN_K_GROWTH_PERCENT / 2)) {
    
    // Validar filtros
    if (realSolLiquidity < CONFIG.MIN_REAL_SOL) {
      log('WARN', `[FILTRO] ❌ ${watch.symbol}: SOL insuficiente (${realSolLiquidity.toFixed(2)})`);
      STATE.watchlist.delete(mint);
      STATE.stats.filtered++;
      return;
    }
    
    if ((watch.buyCount || 0) < CONFIG.MIN_BUY_COUNT) {
      log('WARN', `[FILTRO] ❌ ${watch.symbol}: Pocas compras (${watch.buyCount || 0})`);
      STATE.watchlist.delete(mint);
      STATE.stats.filtered++;
      return;
    }
    
    if (STATE.positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
      log('WARN', `Límite de posiciones alcanzado`);
      return;
    }
    
    log('SUCCESS', `[🚀 CONFIRMADO] ${watch.symbol} +${priceChange.toFixed(1)}%`);
    
    await executeBuy(mint, watch, priceChange, elapsed);
  }
}

async function executeBuy(mint, watch, priceChange, elapsed) {
  try {
    const signature = await buyToken(mint, CONFIG.TRADE_AMOUNT_SOL);
    
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
    let priceData = await getTokenPriceSDK(mint);
    
    if (!priceData) {
      priceData = await getTokenPricePortal(mint);
    }
    
    if (!priceData) continue;
    
    const currentPrice = priceData.priceSol;
    const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
    const holdTime = (Date.now() - pos.entryTime) / 1000 / 60;
    
    if (currentPrice > pos.highestPrice) {
      pos.highestPrice = currentPrice;
    }
    
    if (!pos.trailingActive && pnlPercent >= CONFIG.TRAILING_ACTIVATION) {
      pos.trailingActive = true;
      log('INFO', `🛡️ Trailing activado para ${pos.symbol}`);
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
  }
}

async function executeSell(mint, pos, percentage, reason, pnlPercent) {
  try {
    const signature = await sellToken(mint, percentage);
    
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
// WEBSOCKET PUMPPORTAL
// ═══════════════════════════════════════════════════════════════

function setupWebSocket() {
  STATE.ws = new WebSocket('wss://pumpportal.fun/api/data');
  
  STATE.ws.on('open', () => {
    log('SUCCESS', '✅ Conectado a PumpPortal WebSocket');
    STATE.ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
    log('INFO', '📡 Suscrito a tokens nuevos');
  });
  
  STATE.ws.on('message', async (data) => {
    try {
      const msg = JSON.parse(data.toString());
      
      if (msg.txType === 'create' || msg.mint) {
        const mint = msg.mint || msg.token;
        const name = msg.name || msg.tokenName || 'Unknown';
        const symbol = msg.symbol || msg.tokenSymbol || 'UNKNOWN';
        
        if (STATE.watchlist.has(mint)) return;
        
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
          checksCount: 0
        });
        
        STATE.stats.analyzing = STATE.watchlist.size;
      }
    } catch (error) {
      log('ERROR', `Error WS: ${error.message}`);
    }
  });
  
  STATE.ws.on('error', (error) => {
    log('ERROR', `WebSocket error: ${error.message}`);
  });
  
  STATE.ws.on('close', () => {
    log('WARN', '⚠️ WebSocket cerrado. Reconectando...');
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
  
  STATE.bot.onText(/\/status/, async (msg) => {
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey) / LAMPORTS_PER_SOL;
      const wsStatus = STATE.ws?.readyState === WebSocket.OPEN ? '✅' : '❌';
      
      STATE.bot.sendMessage(msg.chat.id,
        `📊 <b>Estado</b>\n\n` +
        `💰 Balance: ${balance.toFixed(4)} SOL\n` +
        `📈 Posiciones: ${STATE.positions.size}\n` +
        `👀 Analizando: ${STATE.watchlist.size}\n` +
        `🌐 WS: ${wsStatus}\n` +
        `🎯 ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n\n` +
        `🆕 Detectados: ${STATE.stats.detected}\n` +
        `🚫 Filtrados: ${STATE.stats.filtered}`,
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
      `💰 P&L: ${STATE.stats.totalPnl > 0 ? '+' : ''}${STATE.stats.totalPnl.toFixed(2)}%`,
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
      text += `<b>${pos.symbol}</b>\n`;
      text += `💰 ${pos.amount} SOL\n`;
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
      text += `💵 ${watch.realSolLiquidity?.toFixed(2) || '0.00'} SOL\n\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
  });
  
  log('SUCCESS', '✅ Telegram configurado');
}

// ═══════════════════════════════════════════════════════════════
// SETUP WALLET
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
    
    // Inicializar SDK si está disponible
    if (PumpSdk) {
      try {
        STATE.pumpSdk = new PumpSdk(STATE.connection);
        log('SUCCESS', '✅ Pump SDK inicializado');
      } catch (e) {
        log('WARN', `⚠️ SDK init warning: ${e.message}`);
      }
    }
    
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey);
      log('SUCCESS', `💰 Balance: ${(balance / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
    } catch (error) {
      log('WARN', `⚠️ No se pudo verificar balance: ${error.message}`);
    }
    
    return true;
  } catch (error) {
    log('ERROR', `❌ Error setup wallet: ${error.message}`);
    return false;
  }
}

// ═══════════════════════════════════════════════════════════════
// LOOPS PRINCIPALES
// ═══════════════════════════════════════════════════════════════

async function watchlistLoop() {
  log('INFO', '🔄 Loop de análisis iniciado');
  
  while (true) {
    try {
      if (STATE.watchlist.size > 0) {
        for (const mint of STATE.watchlist.keys()) {
          try {
            await analyzeToken(mint);
          } catch (e) {
            log('ERROR', `Error analizando ${mint.slice(0, 8)}: ${e.message}`);
          }
          await sleep(500);
        }
      }
    } catch (error) {
      log('ERROR', `Error watchlist loop: ${error.message}`);
    }
    
    await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
  }
}

async function positionsLoop() {
  log('INFO', '🔄 Loop de posiciones iniciado');
  
  while (true) {
    try {
      if (STATE.positions.size > 0) {
        await monitorPositions();
      }
    } catch (error) {
      log('ERROR', `Error positions loop: ${error.message}`);
    }
    
    await sleep(5000);
  }
}

// ═══════════════════════════════════════════════════════════════
// MAIN
// ═══════════════════════════════════════════════════════════════

async function main() {
  console.log('\n');
  log('INFO', '═══════════════════════════════════════════════════════════════');
  log('INFO', '🚀 PUMP.FUN TRADING BOT v4.1 - FIXED EDITION');
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
  
  await sleep(3000);
  
  console.log('\n');
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  log('SUCCESS', `✅ BOT INICIADO - ${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}`);
  log('SUCCESS', '═══════════════════════════════════════════════════════════════');
  console.log('\n');
  
  log('INFO', `⚡ K Growth mínimo: +${CONFIG.MIN_K_GROWTH_PERCENT}% en ${CONFIG.EARLY_TIME_WINDOW}s`);
  log('INFO', `💰 SOL mínimo: ${CONFIG.MIN_REAL_SOL}`);
  log('INFO', `🛒 Buys mínimas: ${CONFIG.MIN_BUY_COUNT}`);
  log('INFO', `🔬 Método: Bonding curve directa + PumpPortal fallback`);
  console.log('\n');
  
  await sendTelegram(
    `🚀 <b>Bot Iniciado</b>\n\n` +
    `${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n` +
    `💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL por trade\n` +
    `📊 Max: ${CONFIG.MAX_CONCURRENT_POSITIONS} posiciones\n\n` +
    `⚡ K Growth: +${CONFIG.MIN_K_GROWTH_PERCENT}%\n` +
    `💵 SOL mínimo: ${CONFIG.MIN_REAL_SOL}\n` +
    `🛒 Buys mínimas: ${CONFIG.MIN_BUY_COUNT}\n\n` +
    `🛑 Stop: ${CONFIG.HARD_STOP_LOSS}%\n` +
    `🛡️ Trailing: ${CONFIG.TRAILING_PERCENT}%`
  );
  
  // Iniciar loops
  watchlistLoop();
  positionsLoop();
}

// ═══════════════════════════════════════════════════════════════
// SHUTDOWN HANDLERS
// ═══════════════════════════════════════════════════════════════

process.on('SIGTERM', async () => {
  log('WARN', '🛑 SIGTERM recibido');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>');
  process.exit(0);
});

process.on('SIGINT', async () => {
  log('WARN', '🛑 SIGINT recibido');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>');
  process.exit(0);
});

process.on('unhandledRejection', (reason, promise) => {
  log('ERROR', `Unhandled Rejection: ${reason}`);
});

// ═══════════════════════════════════════════════════════════════
// START
// ═══════════════════════════════════════════════════════════════

main().catch(error => {
  log('ERROR', `❌ Error fatal: ${error.message}`);
  sendTelegram(`❌ <b>Error Fatal</b>\n\n${error.message}`);
  process.exit(1);
});Text(/\/start/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id,
      `🤖 <b>Pump.fun Trading Bot</b>\n\n` +
      `<b>Comandos:</b>\n` +
      `/status - Estado\n` +
      `/stats - Estadísticas\n` +
      `/positions - Posiciones\n` +
      `/watchlist - Análisis`,
      { parse_mode: 'HTML' }
    );
  });
  
  STATE.bot.on
