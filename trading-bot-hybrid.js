// trading-bot-sdk.js - PUMP.FUN BOT CON SDK OFICIAL
// 🚀 Usa @pump-fun/pump-sdk para precios y trading directos
// ✅ No depende de Helius, Quicknode o APIs externas
// ✅ Máxima precisión y velocidad

require('dotenv').config();
const WebSocket = require('ws');
const TelegramBot = require('node-telegram-bot-api');
const axios = require('axios');
const { Connection, PublicKey, Keypair, Transaction, sendAndConfirmTransaction, LAMPORTS_PER_SOL } = require('@solana/web3.js');
const { PumpSdk, getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount } = require('@pump-fun/pump-sdk');
const BN = require('bn.js');

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
  
  // Stop Loss / Take Profit
  HARD_STOP_LOSS: parseFloat(process.env.HARD_STOP_LOSS || '-45'),
  QUICK_STOP: parseFloat(process.env.QUICK_STOP || '-25'),
  TRAILING_ACTIVATION: parseFloat(process.env.TRAILING_ACTIVATION || '40'),
  TRAILING_PERCENT: parseFloat(process.env.TRAILING_PERCENT || '-20'),
  TAKE_PROFIT_1: parseFloat(process.env.TAKE_PROFIT_1 || '80'),
  TAKE_PROFIT_2: parseFloat(process.env.TAKE_PROFIT_2 || '150'),
  TAKE_PROFIT_3: parseFloat(process.env.TAKE_PROFIT_3 || '300'),
  
  // Smart Trader
  EARLY_VELOCITY_MIN: parseFloat(process.env.EARLY_VELOCITY_MIN || '8'),
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '45'),
  CONFIRMATION_VELOCITY: parseFloat(process.env.CONFIRMATION_VELOCITY || '20'),
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '90'),
  MIN_LIQUIDITY_SOL: parseFloat(process.env.MIN_LIQUIDITY_SOL || '0.15'),
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '2'),
  MIN_REAL_SOL: parseFloat(process.env.MIN_REAL_SOL || '0.1'),
  
  // Timing
  MAX_HOLD_TIME_MIN: parseInt(process.env.MAX_HOLD_TIME_MIN || '12'),
  STAGNANT_TIME_MIN: parseInt(process.env.STAGNANT_TIME_MIN || '4'),
  MAX_WATCH_TIME_SEC: parseInt(process.env.MAX_WATCH_TIME_SEC || '240'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  
  // Análisis
  PRICE_CHECK_INTERVAL_SEC: parseInt(process.env.PRICE_CHECK_INTERVAL_SEC || '3'),
  
  // RPC
  RPC_ENDPOINT: process.env.RPC_ENDPOINT || 'https://api.mainnet-beta.solana.com',
  
  // Solscan API (opcional)
  SOLSCAN_API_KEY: process.env.SOLSCAN_API_KEY || '',
};

// ═══════════════════════════════════════════════════════════════
// ESTADO GLOBAL
// ═══════════════════════════════════════════════════════════════

const STATE = {
  wallet: null,
  connection: null,
  pumpSdk: null,
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
// 💰 FUNCIONES DE PRECIO CON SDK OFICIAL (CORREGIDO)
// ═══════════════════════════════════════════════════════════════

async function getTokenPriceSDK(mint) {
  try {
    const mintPubkey = new PublicKey(mint);
    
    // CORREGIDO: Usar fetchBuyState en lugar de getBondingCurveAccount
    const global = await STATE.pumpSdk.fetchGlobal();
    
    let bondingCurveData;
    try {
      const buyState = await STATE.pumpSdk.fetchBuyState(mintPubkey, STATE.wallet.publicKey);
      bondingCurveData = buyState.bondingCurve;
    } catch (error) {
      // Si no existe asociatedUserAccount, obtener solo la bondingCurve
      const sellState = await STATE.pumpSdk.fetchSellState(mintPubkey, STATE.wallet.publicKey);
      bondingCurveData = sellState.bondingCurve;
    }
    
    if (!bondingCurveData) {
      log('DEBUG', `No se encontró bonding curve para ${mint.slice(0, 8)}`);
      return null;
    }
    
    // Calcular precio usando las reservas
    const virtualTokenReserves = bondingCurveData.virtualTokenReserves.toNumber();
    const virtualSolReserves = bondingCurveData.virtualSolReserves.toNumber();
    const realTokenReserves = bondingCurveData.realTokenReserves.toNumber();
    const realSolReserves = bondingCurveData.realSolReserves.toNumber();
    
    if (virtualTokenReserves <= 0 || virtualSolReserves <= 0) {
      return null;
    }
    
    // Precio = (SOL reserves / LAMPORTS_PER_SOL) / (Token reserves / 10^6)
    const solAmount = virtualSolReserves / LAMPORTS_PER_SOL;
    const tokenAmount = virtualTokenReserves / 1e6;
    const priceSol = solAmount / tokenAmount;
    
    // Liquidez real en SOL
    const realSolLiquidity = realSolReserves / LAMPORTS_PER_SOL;
    
    return {
      priceSol,
      priceUsd: priceSol * 180, // Aprox (ajusta según precio SOL actual)
      realSolReserves: realSolLiquidity,
      virtualSolReserves: solAmount,
      virtualTokenReserves: tokenAmount,
      realTokenReserves: realTokenReserves / 1e6,
      complete: bondingCurveData.complete || false,
      bondingCurve: bondingCurveData,
      marketCap: (priceSol * 180 * 1_000_000_000), // Supply fijo de pump.fun
    };
    
  } catch (error) {
    log('DEBUG', `Error obteniendo precio SDK para ${mint.slice(0, 8)}: ${error.message}`);
    return null;
  }
}

// ═══════════════════════════════════════════════════════════════
// SOLSCAN API (OPCIONAL - SOLO METADATA)
// ═══════════════════════════════════════════════════════════════

async function getSolscanMetadata(mint) {
  if (!CONFIG.SOLSCAN_API_KEY) return null;
  
  try {
    const response = await axios.get(`https://pro-api.solscan.io/v1.0/token/meta?tokenAddress=${mint}`, {
      headers: { 'token': CONFIG.SOLSCAN_API_KEY },
      timeout: 3000
    });
    
    return {
      holder: response.data?.holder || 0,
      decimals: response.data?.decimals || 6,
      supply: response.data?.supply || 0,
      name: response.data?.name || null,
      symbol: response.data?.symbol || null,
    };
  } catch (error) {
    return null;
  }
}

// DexScreener para buy count (backup)
async function getTokenBuyCount(mint) {
  try {
    const response = await axios.get(`https://api.dexscreener.com/latest/dex/tokens/${mint}`, {
      timeout: 3000
    });
    
    if (!response.data?.pairs || response.data.pairs.length === 0) {
      return 0;
    }
    
    const pair = response.data.pairs.find(p => 
      p.dexId === 'raydium' || p.chainId === 'solana'
    ) || response.data.pairs[0];
    
    return pair.txns?.m5?.buys || 0;
    
  } catch (error) {
    return 0;
  }
}

// ═══════════════════════════════════════════════════════════════
// TRADING CON SDK OFICIAL
// ═══════════════════════════════════════════════════════════════

async function buyTokenSDK(mint, solAmount) {
  try {
    const mintPubkey = new PublicKey(mint);
    
    log('INFO', `[TRADE] Preparando compra de ${solAmount} SOL de ${mint.slice(0, 8)}...`);
    
    // Obtener estado global y de la bonding curve
    const global = await STATE.pumpSdk.fetchGlobal();
    const { bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo } = 
      await STATE.pumpSdk.fetchBuyState(mintPubkey, STATE.wallet.publicKey);
    
    // Convertir SOL a lamports
    const solAmountLamports = new BN(Math.floor(solAmount * LAMPORTS_PER_SOL));
    
    // Calcular cantidad de tokens a recibir
    const tokenAmount = getBuyTokenAmountFromSolAmount(global, bondingCurve, solAmountLamports);
    
    log('INFO', `[TRADE] Calculado: ${solAmount} SOL = ${(tokenAmount.toNumber() / 1e6).toFixed(2)} tokens`);
    
    // Crear instrucciones de compra
    const buyInstructions = await STATE.pumpSdk.buyInstructions({
      global,
      bondingCurveAccountInfo,
      bondingCurve,
      associatedUserAccountInfo,
      mint: mintPubkey,
      user: STATE.wallet.publicKey,
      solAmount: solAmountLamports,
      amount: tokenAmount,
      slippage: CONFIG.SLIPPAGE
    });
    
    // Crear transacción
    const transaction = new Transaction();
    
    // Añadir compute budget
    const computeBudgetIx = {
      programId: new PublicKey('ComputeBudget111111111111111111111111111111'),
      keys: [],
      data: Buffer.from([0, 0, 48, 117, 0, 0, 0, 0, 0]) // 300k compute units
    };
    transaction.add(computeBudgetIx);
    
    // Añadir priority fee
    if (CONFIG.PRIORITY_FEE > 0) {
      const priorityFeeIx = {
        programId: new PublicKey('ComputeBudget111111111111111111111111111111'),
        keys: [],
        data: Buffer.from([
          3,
          ...new BN(Math.floor(CONFIG.PRIORITY_FEE * LAMPORTS_PER_SOL / 300000)).toArray('le', 8)
        ])
      };
      transaction.add(priorityFeeIx);
    }
    
    buyInstructions.forEach(ix => transaction.add(ix));
    
    // Enviar transacción
    const signature = await sendAndConfirmTransaction(
      STATE.connection,
      transaction,
      [STATE.wallet],
      {
        skipPreflight: false,
        commitment: 'confirmed',
        maxRetries: 3
      }
    );
    
    log('SUCCESS', `[TRADE] ✅ Compra ejecutada: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `[TRADE] ❌ Error comprando: ${error.message}`);
    throw error;
  }
}

async function sellTokenSDK(mint, percentage = 100) {
  try {
    const mintPubkey = new PublicKey(mint);
    
    log('INFO', `[TRADE] Preparando venta de ${percentage}% de ${mint.slice(0, 8)}...`);
    
    // Obtener estado
    const global = await STATE.pumpSdk.fetchGlobal();
    const { bondingCurveAccountInfo, bondingCurve } = 
      await STATE.pumpSdk.fetchSellState(mintPubkey, STATE.wallet.publicKey);
    
    // Obtener balance de tokens
    const userTokenAccounts = await STATE.connection.getTokenAccountsByOwner(
      STATE.wallet.publicKey,
      { mint: mintPubkey }
    );
    
    if (userTokenAccounts.value.length === 0) {
      throw new Error('No se encontró cuenta de tokens');
    }
    
    const tokenAccountData = userTokenAccounts.value[0].account.data;
    const tokenBalance = new BN(tokenAccountData.slice(64, 72), 'le');
    
    // Calcular cantidad a vender
    const amountToSell = tokenBalance.muln(percentage).divn(100);
    
    log('INFO', `[TRADE] Vendiendo ${percentage}% = ${(amountToSell.toNumber() / 1e6).toFixed(2)} tokens`);
    
    // Calcular SOL a recibir
    const solAmount = getSellSolAmountFromTokenAmount(global, bondingCurve, amountToSell);
    
    log('INFO', `[TRADE] Recibirás ~${(solAmount.toNumber() / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
    
    // Crear instrucciones
    const sellInstructions = await STATE.pumpSdk.sellInstructions({
      global,
      bondingCurveAccountInfo,
      bondingCurve,
      mint: mintPubkey,
      user: STATE.wallet.publicKey,
      amount: amountToSell,
      solAmount: solAmount,
      slippage: CONFIG.SLIPPAGE
    });
    
    // Crear transacción
    const transaction = new Transaction();
    
    // Compute budget
    const computeBudgetIx = {
      programId: new PublicKey('ComputeBudget111111111111111111111111111111'),
      keys: [],
      data: Buffer.from([0, 0, 48, 117, 0, 0, 0, 0, 0])
    };
    transaction.add(computeBudgetIx);
    
    // Priority fee
    if (CONFIG.PRIORITY_FEE > 0) {
      const priorityFeeIx = {
        programId: new PublicKey('ComputeBudget111111111111111111111111111111'),
        keys: [],
        data: Buffer.from([
          3,
          ...new BN(Math.floor(CONFIG.PRIORITY_FEE * LAMPORTS_PER_SOL / 300000)).toArray('le', 8)
        ])
      };
      transaction.add(priorityFeeIx);
    }
    
    sellInstructions.forEach(ix => transaction.add(ix));
    
    // Enviar
    const signature = await sendAndConfirmTransaction(
      STATE.connection,
      transaction,
      [STATE.wallet],
      {
        skipPreflight: false,
        commitment: 'confirmed',
        maxRetries: 3
      }
    );
    
    log('SUCCESS', `[TRADE] ✅ Venta ejecutada: https://solscan.io/tx/${signature}`);
    return signature;
    
  } catch (error) {
    log('ERROR', `[TRADE] ❌ Error vendiendo: ${error.message}`);
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
  
  // Timeout
  if (elapsed > CONFIG.MAX_WATCH_TIME_SEC) {
    log('INFO', `⏱️ Timeout de observación para ${watch.symbol} después de ${elapsed.toFixed(0)}s`);
    STATE.watchlist.delete(mint);
    STATE.stats.analyzing = STATE.watchlist.size;
    return;
  }
  
  // Obtener precio desde SDK
  const priceData = await getTokenPriceSDK(mint);
  
  if (!priceData || !priceData.priceSol) {
    log('DEBUG', `${watch.symbol}: Sin datos de precio aún (${elapsed.toFixed(0)}s)`);
    return;
  }
  
  const priceSol = priceData.priceSol;
  const realSolLiquidity = priceData.realSolReserves;
  const marketCap = priceData.marketCap;
  
  watch.priceHistory.push({ price: priceSol, time: now });
  watch.lastPrice = priceSol;
  watch.realSolLiquidity = realSolLiquidity;
  watch.marketCap = marketCap;
  watch.bondingCurve = priceData.bondingCurve;
  
  // Buy count cada 2 checks
  if (watch.checksCount % 2 === 0) {
    getTokenBuyCount(mint).then(count => {
      watch.buyCount = count;
    }).catch(() => {});
  }
  
  // Calcular velocidad
  const recentPrices = watch.priceHistory.filter(p => (now - p.time) <= CONFIG.EARLY_TIME_WINDOW * 1000);
  
  if (recentPrices.length < 2) {
    log('DEBUG', `${watch.symbol}: Esperando más datos (${recentPrices.length} puntos)`);
    return;
  }
  
  const firstPrice = recentPrices[0].price;
  const currentPrice = recentPrices[recentPrices.length - 1].price;
  
  if (firstPrice === 0) return;
  
  const priceChange = ((currentPrice - firstPrice) / firstPrice) * 100;
  
  if (watch.checksCount % 3 === 0) {
    log('INFO', `[ANÁLISIS] 📊 ${watch.symbol}: ${priceChange > 0 ? '+' : ''}${priceChange.toFixed(1)}% | MC: $${(marketCap / 1000).toFixed(0)}k | Liq: ${realSolLiquidity.toFixed(2)} SOL | ${elapsed.toFixed(0)}s`);
  }
  
  watch.checksCount++;
  
  // Señal temprana
  if (!watch.earlySignal && elapsed <= CONFIG.EARLY_TIME_WINDOW && priceChange >= CONFIG.EARLY_VELOCITY_MIN) {
    watch.earlySignal = true;
    log('SUCCESS', `[SEÑAL] ⚡ Señal temprana: ${watch.symbol} +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s`);
    
    await sendTelegram(
      `⚡ <b>SEÑAL TEMPRANA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `<code>${mint.slice(0, 8)}...${mint.slice(-4)}</code>\n\n` +
      `📈 +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s\n` +
      `💰 MC: $${(marketCap / 1000).toFixed(0)}k\n` +
      `💵 Liq Real: ${realSolLiquidity.toFixed(2)} SOL\n` +
      `🛒 Buys: ${watch.buyCount || 0}`
    );
  }
  
  // Confirmación
  if (watch.earlySignal && elapsed >= CONFIG.CONFIRMATION_TIME && priceChange >= CONFIG.CONFIRMATION_VELOCITY) {
    
    if (realSolLiquidity < CONFIG.MIN_REAL_SOL) {
      log('WARN', `[FILTRO] ❌ ${watch.symbol}: SOL insuficiente (${realSolLiquidity.toFixed(2)})`);
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
    
    log('SUCCESS', `[SEÑAL CONFIRMADA] 🚀 ${watch.symbol} +${priceChange.toFixed(1)}% | ${watch.buyCount} buys`);
    
    await executeBuy(mint, watch, priceChange, elapsed);
  }
}

// Resto del código igual...
// (executeBuy, monitorPositions, executeSell, setupWebSocket, setupTelegramBot, etc.)

async function executeBuy(mint, watch, priceChange, elapsed) {
  if (STATE.positions.size >= CONFIG.MAX_CONCURRENT_POSITIONS) {
    log('WARN', `Límite de posiciones alcanzado (${STATE.positions.size})`);
    return;
  }
  
  try {
    log('INFO', `🛒 Ejecutando compra de ${CONFIG.TRADE_AMOUNT_SOL} SOL de ${watch.symbol}...`);
    
    if (CONFIG.DRY_RUN) {
      log('SUCCESS', `[DRY RUN] ✅ Compra simulada`);
    } else {
      await buyTokenSDK(mint, CONFIG.TRADE_AMOUNT_SOL);
    }
    
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
    STATE.stats.totalTrades++;
    
    await sendTelegram(
      `🟢 <b>POSICIÓN ABIERTA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL @ ${watch.lastPrice.toFixed(10)}\n` +
      `📈 +${priceChange.toFixed(1)}% en ${elapsed.toFixed(0)}s`
    );
    
  } catch (error) {
    log('ERROR', `Error ejecutando compra: ${error.message}`);
  }
}

async function monitorPositions() {
  for (const [mint, pos] of STATE.positions.entries()) {
    const priceData = await getTokenPriceSDK(mint);
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
    
    let trailingStopPrice = null;
    if (pos.trailingActive) {
      trailingStopPrice = pos.highestPrice * (1 + CONFIG.TRAILING_PERCENT / 100);
    }
    
    log('INFO', `[POS] ${pos.symbol}: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(1)}% | ${holdTime.toFixed(1)}min ${pos.trailingActive ? '🛡️' : ''}`);
    
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
    else if (pos.trailingActive && currentPrice <= trailingStopPrice) {
      shouldSell = true;
      sellReason = `Trailing (${pnlPercent.toFixed(1)}%)`;
    }
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
    else if (holdTime >= CONFIG.MAX_HOLD_TIME_MIN) {
      shouldSell = true;
      sellReason = `Max Hold (${holdTime.toFixed(0)}min)`;
    }
    else if (holdTime >= CONFIG.STAGNANT_TIME_MIN && Math.abs(pnlPercent - pos.lastPnl) < 2) {
      shouldSell = true;
      sellReason = `Estancado`;
    }
    
    if (shouldSell) {
      await executeSell(mint, pos, sellPercentage, sellReason, pnlPercent);
    }
    
    pos.lastPnl = pnlPercent;
  }
}

async function executeSell(mint, pos, percentage, reason, pnlPercent) {
  try {
    log('INFO', `🔔 Vendiendo ${percentage}% de ${pos.symbol}: ${reason}`);
    
    if (CONFIG.DRY_RUN) {
      log('SUCCESS', `[DRY RUN] ✅ Venta simulada: ${percentage}%`);
    } else {
      await sellTokenSDK(mint, percentage);
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
        `${pos.symbol}\n` +
        `📊 P&L: ${pnlPercent > 0 ? '+' : ''}${pnlPercent.toFixed(2)}%\n` +
        `🎯 ${reason}`
      );
    } else {
      await sendTelegram(
        `💰 <b>VENTA PARCIAL</b>\n\n` +
        `${pos.symbol}: ${percentage}%\n` +
        `P&L: +${pnlPercent.toFixed(2)}%`
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
          marketCap: 0,
          lastPrice: 0,
          earlySignal: false,
          checksCount: 0,
          bondingCurve: null
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
  
  STATE.bot.onText(/\/start/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id,
      `🤖 <b>Pump.fun Trading Bot - SDK</b>\n\n` +
      `<b>Comandos:</b>\n` +
      `/status - Estado\n` +
      `/stats - Estadísticas\n` +
      `/positions - Posiciones\n` +
      `/watchlist - Análisis\n` +
      `/balance - Balance`,
      { parse_mode: 'HTML' }
    );
  });
  
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
      STATE.bot.sendMessage(msg.chat.id, '📊 No hay posiciones');
      return;
    }
    
    let text = '📊 <b>Posiciones</b>\n\n';
    
    for (const [mint, pos] of STATE.positions.entries()) {
      const holdTime = ((Date.now() - pos.entryTime) / 60000).toFixed(1);
      text += `<b>${pos.symbol}</b>\n`;
      text += `💰 ${pos.amount} SOL\n`;
      text += `⏱️ ${holdTime}min\n\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/watchlist/, (msg) => {
    if (STATE.watchlist.size === 0) {
      STATE.bot.sendMessage(msg.chat.id, '👀 No hay tokens');
      return;
    }
    
    let text = '👀 <b>Análisis</b>\n\n';
    
    const tokens = Array.from(STATE.watchlist.entries()).slice(0, 10);
    for (const [mint, watch] of tokens) {
      const elapsed = ((Date.now() - watch.firstSeen) / 1000).toFixed(0);
      text += `${watch.symbol} ${watch.earlySignal ? '⚡' : ''}\n`;
      text += `⏱️ ${elapsed}s | 🛒 ${watch.buyCount}\n\n`;
    }
    
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode: 'HTML' });
  });
  
  STATE.bot.onText(/\/balance/, async (msg) => {
    try {
      const balance = await STATE.connection.getBalance(STATE.wallet.publicKey) / LAMPORTS_PER_SOL;
      STATE.bot.sendMessage(msg.chat.id,
        `💰 <b>Balance</b>\n\n${balance.toFixed(4)} SOL`,
        { parse_mode: 'HTML' }
      );
    } catch (error) {
      log('ERROR', `Error /balance: ${error.message}`);
    }
  });
  
  log('SUCCESS', '✅ Telegram configurado');
}

// ═══════════════════════════════════════════════════════════════
// SETUP WALLET Y SDK
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
    
    STATE.pumpSdk = new PumpSdk(STATE.connection);
    log('SUCCESS', '✅ Pump SDK inicializado');
    
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
// LOOPS
// ═══════════════════════════════════════════════════════════════

async function watchlistLoop() {
  log('INFO', '🔄 Loop de análisis iniciado');
  
  while (true) {
    try {
      if (STATE.watchlist.size > 0) {
        for (const mint of STATE.watchlist.keys()) {
          await analyzeToken(mint);
          await sleep(500);
        }
      }
    } catch (error) {
      log('ERROR', `Error watchlist: ${error.message}`);
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
      log('ERROR', `Error positions: ${error.message}`);
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
  log('INFO', '🚀 PUMP.FUN TRADING BOT v4.0 - SDK EDITION');
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
  
  log('INFO', `⚡ Velocidad: +${CONFIG.EARLY_VELOCITY_MIN}% en ${CONFIG.EARLY_TIME_WINDOW}s`);
  log('INFO', `✅ Confirmación: +${CONFIG.CONFIRMATION_VELOCITY}% en ${CONFIG.CONFIRMATION_TIME}s`);
  log('INFO', `💰 SOL mínimo: ${CONFIG.MIN_REAL_SOL}`);
  log('INFO', `🔬 Precios: SDK Pump.fun oficial`);
  console.log('\n');
  
  await sendTelegram(
    `🚀 <b>Bot Iniciado</b>\n\n` +
    `${CONFIG.DRY_RUN ? '🧪 DRY RUN' : '💰 REAL'}\n` +
    `💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL por trade\n` +
    `📊 Max: ${CONFIG.MAX_CONCURRENT_POSITIONS} posiciones\n\n` +
    `⚡ +${CONFIG.EARLY_VELOCITY_MIN}% / ${CONFIG.EARLY_TIME_WINDOW}s\n` +
    `✅ +${CONFIG.CONFIRMATION_VELOCITY}% / ${CONFIG.CONFIRMATION_TIME}s\n\n` +
    `🛑 Stop: ${CONFIG.HARD_STOP_LOSS}%\n` +
    `🛡️ Trailing: ${CONFIG.TRAILING_PERCENT}%\n\n` +
    `🔧 SDK oficial Pump.fun`
  );
  
  watchlistLoop();
  positionsLoop();
}

// ═══════════════════════════════════════════════════════════════
// SHUTDOWN
// ═══════════════════════════════════════════════════════════════

process.on('SIGTERM', async () => {
  log('WARN', '🛑 SIGTERM');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>');
  process.exit(0);
});

process.on('SIGINT', async () => {
  log('WARN', '🛑 SIGINT');
  if (STATE.ws) STATE.ws.close();
  await sendTelegram('🛑 <b>Bot Detenido</b>');
  process.exit(0);
});

// ═══════════════════════════════════════════════════════════════
// START
// ═══════════════════════════════════════════════════════════════

main().catch(error => {
  log('ERROR', `❌ Error fatal: ${error.message}`);
  sendTelegram(`❌ <b>Error Fatal</b>\n\n${error.message}`);
  process.exit(1);
});
