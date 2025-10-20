/**
 * trading-bot-hybrid.js
 * Pump.fun + PumpSwap trading bot
 * - Filtros matemáticos basados en bonding curve (K)
 * - Intenta usar Pump SDK, fallback a PumpPortal
 * - Vende antes de que la curva complete
 * - PumpSwap SDK integrado (no migramos automáticamente por defecto)
 *
 * Requiere en package.json:
 *  @pump-fun/pump-sdk, @pump-fun/pump-swap-sdk, @solana/web3.js, axios, ws, node-telegram-bot-api, bn.js, bs58, dotenv
 */

require('dotenv').config();
const WebSocket = require('ws');
const axios = require('axios');
const TelegramBot = require('node-telegram-bot-api');
const { Connection, PublicKey, Keypair, Transaction, sendAndConfirmTransaction, LAMPORTS_PER_SOL, VersionedTransaction } = require('@solana/web3.js');
let PumpSdk = null;
let PumpAmmSdk = null;
try { PumpSdk = require('@pump-fun/pump-sdk').PumpSdk; } catch(e){ PumpSdk = require('@pump-fun/pump-sdk').default || require('@pump-fun/pump-sdk'); }
try { PumpAmmSdk = require('@pump-fun/pump-swap-sdk').PumpAmmSdk || require('@pump-fun/pump-swap-sdk'); } catch(e){ PumpAmmSdk = null; }
const BN = require('bn.js');
const bs58 = require('bs58');

// ------------------ CONFIG ------------------
const CONFIG = {
  WALLET_PRIVATE_KEY: process.env.WALLET_PRIVATE_KEY,
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN,
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID,
  RPC_ENDPOINT: process.env.RPC_ENDPOINT || 'https://api.mainnet-beta.solana.com',
  PUMPPORTAL_URL: process.env.PUMPPORTAL_URL || 'https://pumpportal.fun',
  PUMPPORTAL_API_KEY: process.env.PUMPPORTAL_API_KEY || '',
  DRY_RUN: (process.env.DRY_RUN === 'true'),
  TRADE_AMOUNT_SOL: parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007'),
  SLIPPAGE: parseFloat(process.env.SLIPPAGE || '3'),        // default slippage %
  PRIORITY_FEE: parseFloat(process.env.PRIORITY_FEE || '0.0005'),
  // Filters for entering trades (priority: strict for profitability)
  MIN_REAL_SOL: parseFloat(process.env.MIN_REAL_SOL || '0.08'),   // min real SOL liquidity
  MIN_BUY_COUNT: parseInt(process.env.MIN_BUY_COUNT || '2'),      // buys count from dexscreener
  MIN_MARKETCAP_USD: parseFloat(process.env.MIN_MARKETCAP_USD || '3000'), // minimal MC (est)
  MIN_K_GROWTH_PERCENT: parseFloat(process.env.MIN_K_GROWTH_PERCENT || '6'), // K growth in window
  EARLY_TIME_WINDOW: parseInt(process.env.EARLY_TIME_WINDOW || '30'), // seconds to measure velocity
  CONFIRMATION_TIME: parseInt(process.env.CONFIRMATION_TIME || '60'), // seconds for confirmation
  PRICE_CHECK_INTERVAL_SEC: parseInt(process.env.PRICE_CHECK_INTERVAL_SEC || '3'),
  MAX_CONCURRENT_POSITIONS: parseInt(process.env.MAX_CONCURRENT_POSITIONS || '3'),
  WARN_RATIO_BEFORE_COMPLETE: parseFloat(process.env.WARN_RATIO_BEFORE_COMPLETE || '0.05'), // when real_token_reserves <= WARN_RATIO * initialRealTokenReserves => sell
  AUTO_MIGRATE_IF_ADVANTAGE: (process.env.AUTO_MIGRATE_IF_ADVANTAGE === 'true'), // false by default
  TELEGRAM_PRO: true
};

// ------------------ STATE ------------------
const STATE = {
  connection: null,
  wallet: null,
  pumpSdk: null,
  pumpAmmSdk: null,
  ws: null,
  bot: null,
  watchlist: new Map(),
  positions: new Map(),
  stats: { detected:0, analyzing:0, filtered:0, trades:0, wins:0, losses:0, totalPnl:0 }
};

// ------------------ LOG ------------------
function log(level, msg) {
  const t = new Date().toISOString();
  const icons = { INFO:'ℹ️', SUCCESS:'✅', WARN:'⚠️', ERROR:'❌', DEBUG:'🔍' };
  console.log(`[${level}] ${t} ${icons[level]||''} ${msg}`);
}

// ------------------ UTIL ------------------
function decodeBase58(s){ return bs58.decode(s); }
function sleep(ms){ return new Promise(r=>setTimeout(r, ms)); }
async function sendTelegram(text) {
  if (!STATE.bot || !CONFIG.TELEGRAM_CHAT_ID) return;
  try { await STATE.bot.sendMessage(CONFIG.TELEGRAM_CHAT_ID, text, { parse_mode: 'HTML' }); } catch(e){ log('ERROR', `Telegram send error: ${e.message}`); }
}

// ------------------ SDK / CONNECTION SETUP ------------------
async function setupConnectionAndWallet(){
  try {
    if (!CONFIG.WALLET_PRIVATE_KEY) throw new Error('WALLET_PRIVATE_KEY missing');
    const secret = decodeBase58(CONFIG.WALLET_PRIVATE_KEY);
    STATE.wallet = Keypair.fromSecretKey(new Uint8Array(secret));
    STATE.connection = new Connection(CONFIG.RPC_ENDPOINT, 'confirmed');

    // Init SDKs if available
    try {
      if (PumpSdk) STATE.pumpSdk = new PumpSdk(STATE.connection);
      if (PumpAmmSdk) STATE.pumpAmmSdk = new PumpAmmSdk(STATE.connection);
    } catch(e){
      log('WARN', `SDK init warning: ${e.message}`);
    }

    log('SUCCESS', `Wallet: ${STATE.wallet.publicKey.toString().slice(0,8)}...`);
    try {
      const bal = await STATE.connection.getBalance(STATE.wallet.publicKey);
      log('SUCCESS', `Balance: ${(bal / LAMPORTS_PER_SOL).toFixed(4)} SOL`);
    } catch(e){
      log('WARN', `Balance check failed: ${e.message}`);
    }
    return true;
  } catch(err){
    log('ERROR', `setupConnectionAndWallet: ${err.message}`);
    return false;
  }
}

// ------------------ PUMPPORTAL TRANSACTION (fallback) ------------------
async function sendPortalTransaction({ action='buy', mint, amount, denominatedInSol=true, pool='pump', slippage=CONFIG.SLIPPAGE, priorityFee=CONFIG.PRIORITY_FEE }){
  const url = `${CONFIG.PUMPPORTAL_URL}/api/trade-local`;
  const headers = {'Content-Type':'application/json'};
  if (CONFIG.PUMPPORTAL_API_KEY) headers['x-api-key'] = CONFIG.PUMPPORTAL_API_KEY;
  const body = {
    publicKey: STATE.wallet.publicKey.toString(),
    action,
    mint,
    denominatedInSol: denominatedInSol ? "true" : "false",
    amount,
    slippage,
    priorityFee,
    pool
  };
  const r = await axios.post(url, body, { headers, responseType: 'arraybuffer', timeout: 15000 }).catch(e=>{ throw new Error(`Portal err: ${e?.response?.status || e.message}`); });
  if (r.status !== 200) throw new Error('Portal failed to generate tx');
  const txBuf = Buffer.from(r.data);
  const tx = VersionedTransaction.deserialize(txBuf);
  const signer = Keypair.fromSecretKey(new Uint8Array(decodeBase58(CONFIG.WALLET_PRIVATE_KEY)));
  tx.sign([signer]);
  const raw = tx.serialize();
  const sig = await STATE.connection.sendRawTransaction(raw, { skipPreflight:false });
  await STATE.connection.confirmTransaction(sig, 'confirmed');
  return sig;
}

// ------------------ PRICE / BONDING HELPERS ------------------

async function getGlobalFlexible() {
  try {
    if (!STATE.pumpSdk) return null;
    if (typeof STATE.pumpSdk.fetchGlobal === 'function') return await STATE.pumpSdk.fetchGlobal();
    if (typeof STATE.pumpSdk.getGlobalState === 'function') return await STATE.pumpSdk.getGlobalState();
    if (typeof STATE.pumpSdk.getGlobal === 'function') return await STATE.pumpSdk.getGlobal();
    return null;
  } catch(e) { log('DEBUG', `getGlobalFlexible err: ${e.message}`); return null; }
}

async function getBondingFromSdk(mint) {
  try {
    if (!STATE.pumpSdk) return null;
    const mintPubkey = new PublicKey(mint);
    let buyState = null;
    try { if (typeof STATE.pumpSdk.fetchBuyState==='function') buyState = await STATE.pumpSdk.fetchBuyState(mintPubkey, STATE.wallet.publicKey); } catch(e){ buyState=null; }
    if (buyState && buyState.bondingCurve) return buyState.bondingCurve;
    try { const sellState = await STATE.pumpSdk.fetchSellState(mintPubkey, STATE.wallet.publicKey); if (sellState && sellState.bondingCurve) return sellState.bondingCurve; } catch(e){ }
    return null;
  } catch(e){ log('DEBUG', `getBondingFromSdk err: ${e.message}`); return null; }
}

function bondingToNumeric(bonding) {
  try {
    const vToken = bonding.virtualTokenReserves.toNumber ? bonding.virtualTokenReserves.toNumber() : Number(bonding.virtualTokenReserves);
    const vSol = bonding.virtualSolReserves.toNumber ? bonding.virtualSolReserves.toNumber() : Number(bonding.virtualSolReserves);
    const rToken = bonding.realTokenReserves.toNumber ? bonding.realTokenReserves.toNumber() : Number(bonding.realTokenReserves);
    const rSol = bonding.realSolReserves.toNumber ? bonding.realSolReserves.toNumber() : Number(bonding.realSolReserves);
    return {
      virtualTokenReserves: vToken,
      virtualSolReserves: vSol,
      realTokenReserves: rToken,
      realSolReserves: rSol
    };
  } catch(e){ return null; }
}

function computeK(vSol, vToken){ return vSol * vToken; }
function computePrice(vSol, vToken){ if (!vToken) return null; return (vSol / vToken); }

// ------------------ DexScreener buys count (backup) ------------------
async function getTokenBuyCount(mint) {
  try {
    const url = `https://api.dexscreener.com/latest/dex/tokens/${mint}`;
    const r = await axios.get(url, { timeout:3000 }).catch(()=>null);
    if (!r || !r.data) return 0;
    const pair = (r.data.pairs && r.data.pairs[0]) ? r.data.pairs[0] : null;
    if (!pair) return 0;
    // Some endpoints provide recent tx counts; fallback to 0
    return (pair.txns && pair.txns.m5 && pair.txns.m5.buys) ? pair.txns.m5.buys : 0;
  } catch(e){ return 0; }
}

// ------------------ ANALYSIS & ENTRY LOGIC (STRICT) ------------------
async function analyzeToken(mint) {
  const watch = STATE.watchlist.get(mint);
  if (!watch) return;
  const now = Date.now();
  const elapsed = (now - watch.firstSeen) / 1000;
  // timeout
  if (elapsed > 300) { STATE.watchlist.delete(mint); return; }

  // Get bonding curve
  const bonding = await getBondingFromSdk(mint);
  if (!bonding) { log('DEBUG', `${watch.symbol}: bonding not available`); return; }
  const nums = bondingToNumeric(bonding);
  if (!nums) return;
  const priceSol = computePrice(nums.virtualSolReserves, nums.virtualTokenReserves);
  const K = computeK(nums.virtualSolReserves, nums.virtualTokenReserves);

  // add history
  watch.priceHistory.push({ price: priceSol, k: K, time: now, realSol: nums.realSolReserves, realToken: nums.realTokenReserves });
  if (watch.priceHistory.length > 60) watch.priceHistory.shift();

  // compute K growth over EARLY_TIME_WINDOW
  const windowStart = now - CONFIG.EARLY_TIME_WINDOW*1000;
  const old = watch.priceHistory.find(p=>p.time >= windowStart) || watch.priceHistory[0];
  const kgrowth = old && old.k ? ((K - old.k) / old.k) * 100 : 0;
  const realSol = nums.realSolReserves / LAMPORTS_PER_SOL;
  const realToken = nums.realTokenReserves;
  const virtualSol = nums.virtualSolReserves / LAMPORTS_PER_SOL;
  const virtualToken = nums.virtualTokenReserves / 1e6;

  // update watch
  watch.last = { priceSol, K, kgrowth, realSol, realToken, virtualSol, virtualToken, timestamp: now };
  watch.checks = (watch.checks||0) + 1;

  // quick logs
  if (watch.checks % 3 === 0) {
    log('INFO', `[ANALYSIS] ${watch.symbol}: priceSol=${priceSol?.toFixed(6)||'NA'} realSol=${realSol.toFixed(3)} Kgrowth=${kgrowth.toFixed(2)}%`);
  }

  // gather buy count
  if (!watch.buyCount || watch.buyCount === 0) {
    getTokenBuyCount(mint).then(c=>watch.buyCount=c).catch(()=>{});
  }

  // Strict filter to decide entry
  const passesLiquidity = realSol >= CONFIG.MIN_REAL_SOL;
  const passesBuys = (watch.buyCount || 0) >= CONFIG.MIN_BUY_COUNT;
  const passesK = kgrowth >= CONFIG.MIN_K_GROWTH_PERCENT;
  const passesMarketCap = (watch.estimatedMarketCapUsd || 1e9) >= CONFIG.MIN_MARKETCAP_USD;

  // Save reason
  watch.filter = { passesLiquidity, passesBuys, passesK, passesMarketCap, kgrowth };

  // If all filters pass and not in positions & below concurrent limit, place buy
  if (passesLiquidity && passesBuys && passesK && STATE.positions.size < CONFIG.MAX_CONCURRENT_POSITIONS && !watch.entered) {
    watch.entered = true;
    log('SUCCESS', `SIGNAL EARLY: ${watch.symbol} kgrowth=${kgrowth.toFixed(2)}% realSol=${realSol.toFixed(3)}`);
    await sendTelegram(
      `⚡ <b>SEÑAL TEMPRANA</b>\n\n` +
      `${watch.name} (${watch.symbol})\n` +
      `<code>${mint}</code>\n` +
      `📈 Price: ${priceSol?.toFixed(8)||'NA'} SOL\n` +
      `🔁 Kgrowth: ${kgrowth.toFixed(2)}%\n` +
      `💵 RealSOL: ${realSol.toFixed(3)} SOL\n` +
      `🛒 Buys (m5): ${watch.buyCount||0}\n` +
      `🔬 Filters: liquidity=${passesLiquidity} buys=${passesBuys} kgrowth=${passesK}`
    );

    // Confirm after CONFIRMATION_TIME: ensure momentum remains
    setTimeout(async ()=>{
      // re-evaluate
      const latest = watch.last;
      if (!latest) return;
      if (latest.kgrowth >= CONFIG.CONFIRMATION_VELOCITY || latest.kgrowth >= (CONFIG.MIN_K_GROWTH_PERCENT/2)) {
        log('SUCCESS', `CONFIRMED SIGNAL: ${watch.symbol}`);
        // execute buy
        try {
          const sig = await buyToken(mint, CONFIG.TRADE_AMOUNT_SOL);
          STATE.positions.set(mint, {
            mint, symbol: watch.symbol, name: watch.name, entryTime: Date.now(),
            entryPrice: latest.priceSol, amountSol: CONFIG.TRADE_AMOUNT_SOL, highestPrice: latest.priceSol,
            entrySig: sig, lastPnl: 0, realTokenAtEntry: latest.realToken
          });
          STATE.stats.trades++;
          await sendTelegram(`🟢 <b>POSICIÓN ABIERTA</b>\n${watch.symbol}\n💰 ${CONFIG.TRADE_AMOUNT_SOL} SOL @ ${latest.priceSol?.toFixed(8)||'NA'}\nTx: https://solscan.io/tx/${sig}`);
        } catch(e){
          log('ERROR', `Buy failed: ${e.message}`);
        }
      } else {
        log('WARN', `CONFIRMATION FAILED for ${watch.symbol} (kgrowth=${latest.kgrowth.toFixed(2)}%)`);
      }
    }, CONFIG.CONFIRMATION_TIME * 1000);
  }

  // Update stats
  STATE.stats.analyzing = STATE.watchlist.size;
}

// ------------------ BUY / SELL HELPERS (SDK primary, Portal fallback) ------------------
async function buyToken(mint, solAmount) {
  log('INFO', `Try buy ${solAmount} SOL ${mint}`);
  if (CONFIG.DRY_RUN) { log('SUCCESS', '[DRY RUN] buy'); return null; }
  // Try SDK buy
  try {
    if (STATE.pumpSdk && typeof STATE.pumpSdk.buyInstructions === 'function') {
      const global = await getGlobalFlexible();
      if (!global) throw new Error('no global');
      const mintPub = new PublicKey(mint);
      const { bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo } = await STATE.pumpSdk.fetchBuyState(mintPub, STATE.wallet.publicKey);
      const lam = new BN(Math.floor(solAmount * LAMPORTS_PER_SOL));
      const tokenAmount = require('@pump-fun/pump-sdk').getBuyTokenAmountFromSolAmount(global, bondingCurve, lam);
      const buyInstructions = await STATE.pumpSdk.buyInstructions({
        global, bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo,
        mint: mintPub, user: STATE.wallet.publicKey, solAmount: lam, amount: tokenAmount, slippage: CONFIG.SLIPPAGE
      });
      const tx = new Transaction();
      buyInstructions.forEach(ix=>tx.add(ix));
      const sig = await sendAndConfirmTransaction(STATE.connection, tx, [STATE.wallet], { commitment:'confirmed' });
      log('SUCCESS', `Buy SDK tx: ${sig}`);
      return sig;
    }
  } catch(e){ log('WARN', `SDK buy err: ${e.message}`); }
  // fallback to portal
  try {
    const sig = await sendPortalTransaction({ action:'buy', mint, amount: solAmount, denominatedInSol:true, slippage: CONFIG.SLIPPAGE, priorityFee: CONFIG.PRIORITY_FEE });
    log('SUCCESS', `Buy Portal tx: ${sig}`);
    return sig;
  } catch(e){ throw e; }
}

async function sellToken(mint, percent = 100) {
  log('INFO', `Try sell ${percent}% ${mint}`);
  if (CONFIG.DRY_RUN) { log('SUCCESS', '[DRY RUN] sell'); return null; }
  // Try SDK sell
  try {
    if (STATE.pumpSdk && typeof STATE.pumpSdk.sellInstructions === 'function') {
      const global = await getGlobalFlexible();
      if (!global) throw new Error('no global');
      const mintPub = new PublicKey(mint);
      const { bondingCurveAccountInfo, bondingCurve } = await STATE.pumpSdk.fetchSellState(mintPub, STATE.wallet.publicKey);

      // get token account balance
      const userTokenAccounts = await STATE.connection.getTokenAccountsByOwner(STATE.wallet.publicKey, { mint: mintPub });
      if (userTokenAccounts.value.length === 0) throw new Error('No token account');
      // Parse amount - NOTE: relies on account layout, may need adjustments if different
      const tokenAccountData = userTokenAccounts.value[0].account.data;
      const tokenBalance = new BN(tokenAccountData.slice(64,72), 'le');
      const amountToSell = tokenBalance.muln(percent).divn(100);
      const solAmount = require('@pump-fun/pump-sdk').getSellSolAmountFromTokenAmount(global, bondingCurve, amountToSell);

      const sellInstructions = await STATE.pumpSdk.sellInstructions({
        global, bondingCurveAccountInfo, bondingCurve, mint: mintPub, user: STATE.wallet.publicKey, amount: amountToSell, solAmount, slippage: CONFIG.SLIPPAGE
      });
      const tx = new Transaction();
      sellInstructions.forEach(ix=>tx.add(ix));
      const sig = await sendAndConfirmTransaction(STATE.connection, tx, [STATE.wallet], { commitment:'confirmed' });
      log('SUCCESS', `Sell SDK tx: ${sig}`);
      return sig;
    }
  } catch(e){ log('WARN', `SDK sell err: ${e.message}`); }

  // fallback Portal: use percent expressed as "100%" etc.
  try {
    const amt = `${percent}%`;
    const sig = await sendPortalTransaction({ action:'sell', mint, amount: amt, denominatedInSol:false, slippage: CONFIG.SLIPPAGE, priorityFee: CONFIG.PRIORITY_FEE });
    log('SUCCESS', `Sell Portal tx: ${sig}`);
    return sig;
  } catch(e){ throw e; }
}

// ------------------ POSITIONS MONITOR ------------------
async function monitorPositions() {
  for (const [mint, pos] of Array.from(STATE.positions.entries())) {
    try {
      // get latest bonding
      const bonding = await getBondingFromSdk(mint);
      if (!bonding) continue;
      const nums = bondingToNumeric(bonding);
      if (!nums) continue;
      const currentPrice = computePrice(nums.virtualSolReserves, nums.virtualTokenReserves);
      const pnlPercent = ((currentPrice - pos.entryPrice) / pos.entryPrice) * 100;
      if (currentPrice > pos.highestPrice) pos.highestPrice = currentPrice;

      // compute remaining ratio
      const remainingRatio = nums.realTokenReserves / (pos.realTokenAtEntry || nums.realTokenReserves || 1);

      // If remaining real_token_reserves <= WARN_RATIO_BEFORE_COMPLETE -> SELL (pre-migration)
      if (nums.realTokenReserves <= (pos.realTokenAtEntry * CONFIG.WARN_RATIO_BEFORE_COMPLETE) || nums.realTokenReserves <= 1) {
        log('WARN', `Pre-complete condition met for ${pos.symbol}, selling before migration (remaining ratio ${remainingRatio.toFixed(3)})`);
        const sig = await sellToken(mint, 100);
        STATE.positions.delete(mint);
        const emoji = (pnlPercent>0)?'🟢':'🔴';
        await sendTelegram(`${emoji} <b>POSICIÓN CERRADA</b>\n${pos.symbol}\nP&L: ${pnlPercent>0?'+':''}${pnlPercent.toFixed(2)}%\nTx: https://solscan.io/tx/${sig}`);
        continue;
      }

      // Trailing & TP logic (simplified):
      if (!pos.trailingActive && pnlPercent >= 40) pos.trailingActive = true;
      if (pos.trailingActive) {
        const trailingStopPrice = pos.highestPrice * (1 + (-20)/100); // trailing -20%
        if (currentPrice <= trailingStopPrice) {
          log('INFO', `Trailing stop hit for ${pos.symbol}`);
          const sig = await sellToken(mint, 100);
          STATE.positions.delete(mint);
          await sendTelegram(`🔔 <b>Trailing stop</b> ${pos.symbol}\nP&L: ${pnlPercent.toFixed(2)}%\nTx: https://solscan.io/tx/${sig}`);
          continue;
        }
      }

      // Quick stop
      const holdTimeMin = (Date.now() - pos.entryTime)/60000;
      if (holdTimeMin <= 2 && pnlPercent <= -25) {
        log('WARN', `Quick stop for ${pos.symbol}`);
        const sig = await sellToken(mint, 100);
        STATE.positions.delete(mint);
        await sendTelegram(`🔴 <b>Quick stop</b> ${pos.symbol}\nP&L: ${pnlPercent.toFixed(2)}%\nTx: https://solscan.io/tx/${sig}`);
        continue;
      }

      // Update pos lastpnl
      pos.lastPnl = pnlPercent;
    } catch(err){
      log('ERROR', `monitorPositions error: ${err.message}`);
    }
  }
}

// ------------------ WEBSOCKET SETUP ------------------
function setupWebSocket() {
  STATE.ws = new WebSocket(`${CONFIG.PUMPPORTAL_URL.replace(/^http/,'ws')}/api/data`);
  STATE.ws.on('open', () => {
    log('SUCCESS', 'Connected to PumpPortal WS');
    STATE.ws.send(JSON.stringify({ method: 'subscribeNewToken' }));
    log('INFO', 'Subscribed to new tokens');
  });
  STATE.ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.txType === 'create' || msg.mint) {
        const mint = msg.mint || msg.token;
        const name = msg.name || msg.tokenName || 'Unknown';
        const symbol = msg.symbol || msg.tokenSymbol || 'UNKNOWN';
        if (STATE.watchlist.has(mint)) return;
        STATE.watchlist.set(mint, { mint, name, symbol, firstSeen: Date.now(), priceHistory: [], buyCount: 0, entered:false });
        STATE.stats.detected++;
        log('SUCCESS', `🆕 ${name} (${symbol}) - ${mint}`);
      }
    } catch(e){ log('ERROR', `WS message parse err: ${e.message}`); }
  });
  STATE.ws.on('close', () => { log('WARN', 'WS closed, reconnecting in 5s'); setTimeout(setupWebSocket,5000); });
  STATE.ws.on('error', (e)=>{ log('ERROR', `WS error: ${e.message}`); });
}

// ------------------ TELEGRAM ------------------
function setupTelegram() {
  if (!CONFIG.TELEGRAM_BOT_TOKEN) { log('WARN','Telegram token missing'); return; }
  STATE.bot = new TelegramBot(CONFIG.TELEGRAM_BOT_TOKEN, { polling: true });
  STATE.bot.onText(/\/start/, (msg) => {
    STATE.bot.sendMessage(msg.chat.id, 'Pump Bot online. Commands: /status /positions /watchlist /balance', { parse_mode: 'HTML' });
  });
  STATE.bot.onText(/\/status/, async (msg) => {
    const balance = (await STATE.connection.getBalance(STATE.wallet.publicKey))/LAMPORTS_PER_SOL;
    STATE.bot.sendMessage(msg.chat.id, `<b>Status</b>\nBalance: ${balance.toFixed(4)} SOL\nPositions: ${STATE.positions.size}\nWatching: ${STATE.watchlist.size}`, { parse_mode: 'HTML' });
  });
  STATE.bot.onText(/\/watchlist/, (msg) => {
    let text = `<b>Watchlist</b>\n`;
    for (const [mint, w] of Array.from(STATE.watchlist.entries()).slice(0,10)) {
      text += `\n${w.symbol} | ${w.name}\n<code>${mint}</code>\n`;
    }
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode:'HTML' });
  });
  STATE.bot.onText(/\/positions/, (msg) => {
    let text = `<b>Positions</b>\n`;
    for (const [m,p] of STATE.positions) {
      const hold = ((Date.now()-p.entryTime)/60000).toFixed(1);
      text += `\n${p.symbol} | Hold: ${hold}m | EntryPrice: ${p.entryPrice?.toFixed(6)||'NA'}\n`;
    }
    STATE.bot.sendMessage(msg.chat.id, text, { parse_mode:'HTML' });
  });
  log('SUCCESS','Telegram configured');
}

// ------------------ MAIN LOOPS ------------------
async function watchlistLoop() {
  log('INFO','Watchlist loop started');
  while(true) {
    try {
      if (STATE.watchlist.size > 0) {
        for (const mint of Array.from(STATE.watchlist.keys())) {
          try { await analyzeToken(mint); } catch(e){ log('ERROR', `analyzeToken ${mint}: ${e.message}`); }
          await sleep(500);
        }
      }
    } catch(e){ log('ERROR', `watchlistLoop: ${e.message}`); }
    await sleep(CONFIG.PRICE_CHECK_INTERVAL_SEC * 1000);
  }
}

async function positionsLoop() {
  log('INFO','Positions loop started');
  while(true) {
    try { if (STATE.positions.size > 0) await monitorPositions(); } catch(e){ log('ERROR', `positionsLoop: ${e.message}`); }
    await sleep(5000);
  }
}

// ------------------ START ------------------
async function main(){
  log('INFO','Starting bot');
  const ok = await setupConnectionAndWallet();
  if (!ok) return process.exit(1);
  setupTelegram();
  setupWebSocket();
  // small delay to populate watchlist
  setTimeout(()=>{ watchlistLoop(); positionsLoop(); }, 3000);
}

process.on('SIGINT', ()=>{ log('WARN','SIGINT'); process.exit(0); });
process.on('SIGTERM', ()=>{ log('WARN','SIGTERM'); process.exit(0); });

main().catch(e=>{ log('ERROR', `Fatal: ${e.message}`); process.exit(1); });
