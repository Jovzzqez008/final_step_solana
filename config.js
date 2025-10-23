// config.js - Configuración centralizada con trading real (VERSIÓN FINAL)
require('dotenv').config();

const CONFIG = {
  // Telegram
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || '',
  
  // PumpPortal
  PUMPPORTAL_WSS: process.env.PUMPPORTAL_WSS || 'wss://pumpportal.fun/api/data',
  
  // Database
  REDIS_URL: process.env.REDIS_URL || '',
  DATABASE_URL: process.env.DATABASE_URL || '',
  
  // Solana
  RPC_URL: process.env.RPC_URL || process.env.HELIUS_RPC_URL || 'https://api.mainnet-beta.solana.com',
  SOL_PRICE_USD: Number(process.env.SOL_PRICE_USD || 150),
  
  // Trading Mode
  TRADING_MODE: process.env.TRADING_MODE || 'DRY_RUN', // DRY_RUN | LIVE
  DRY_RUN: process.env.DRY_RUN === 'true' || process.env.TRADING_MODE === 'DRY_RUN',
  TRADE_AMOUNT_SOL: Number(process.env.TRADE_AMOUNT_SOL || 0.01),
  SLIPPAGE_BPS: Number(process.env.SLIPPAGE_BPS || 100),
  
  // Wallet Real
  SOLANA_WALLET_PATH: process.env.SOLANA_WALLET_PATH,
  DEVELOPER_ADDRESS: process.env.DEVELOPER_ADDRESS || '8bXf8Rg3u4Prz71LgKR5mpa7aMe2F4cSKYYRctmqro6x',
  PRIORITY_FEE_BASE: Number(process.env.PRIORITY_FEE_BASE || 0.0003),
  
  // Estrategia TreeCityWes
  MINIMUM_BUY_AMOUNT: Number(process.env.MINIMUM_BUY_AMOUNT || 0.015),
  MAX_BONDING_CURVE_PROGRESS: Number(process.env.MAX_BONDING_CURVE_PROGRESS || 10),
  SELL_BONDING_CURVE_PROGRESS: Number(process.env.SELL_BONDING_CURVE_PROGRESS || 15),
  PROFIT_TARGET_1: 1.25,    // 25%
  PROFIT_TARGET_2: 1.50,    // 50% total  
  STOP_LOSS: 0.90,          // -10%
  MOON_BAG: 0.25,           // 25% moon bag
  
  // Alert Rules
  ALERT_RULES: [
    { name: 'fast_2x', percent: 100, timeWindowMin: 20, description: '2x en 20 minutos' },
    { name: 'momentum_150', percent: 150, timeWindowMin: 15, description: '2.5x en 15 minutos' }
  ],
  
  // Monitoring
  MAX_MONITOR_TIME_MIN: Number(process.env.MAX_MONITOR_TIME_MIN || 30),
  DUMP_THRESHOLD_PERCENT: Number(process.env.DUMP_THRESHOLD_PERCENT || -60),
  PRICE_CHECK_INTERVAL_SEC: Number(process.env.PRICE_CHECK_INTERVAL_SEC || 5), // ✅ AUMENTADO de 3 a 5
  MIN_INITIAL_LIQUIDITY_USD: Number(process.env.MIN_INITIAL_LIQUIDITY_USD || 50), // ✅ REDUCIDO de 300 a 50
  
  // ✅ NUEVO: Configuración para retry de tokens nuevos
  INITIAL_DATA_RETRY_DELAY: Number(process.env.INITIAL_DATA_RETRY_DELAY || 2000), // 2 segundos
  MAX_DATA_FETCH_RETRIES: Number(process.env.MAX_DATA_FETCH_RETRIES || 3),        // 3 reintentos
  DATA_FETCH_TIMEOUT: Number(process.env.DATA_FETCH_TIMEOUT || 10000),            // 10 segundos
  
  // ✅ NUEVO: Retry para tokens con liquidez = 0
  ZERO_LIQUIDITY_RETRY_DELAY: Number(process.env.ZERO_LIQUIDITY_RETRY_DELAY || 5000), // 5 segundos
  ALLOW_ZERO_LIQUIDITY_MONITORING: process.env.ALLOW_ZERO_LIQUIDITY_MONITORING === 'true', // Monitorear sin liquidez
  
  // ✅ NUEVO: Rate limiting mejorado
  MAX_REQUESTS_PER_SECOND: Number(process.env.MAX_REQUESTS_PER_SECOND || 10),
  REQUEST_DELAY_MS: Number(process.env.REQUEST_DELAY_MS || 100),
  
  // ✅ NUEVO: Manejo de API errors
  API_530_MAX_RETRIES: Number(process.env.API_530_MAX_RETRIES || 3),
  API_530_RETRY_DELAY_MS: Number(process.env.API_530_RETRY_DELAY_MS || 2000),
  API_429_RETRY_DELAY_MS: Number(process.env.API_429_RETRY_DELAY_MS || 5000),
  
  // Performance
  MAX_CONCURRENCY: Number(process.env.MAX_CONCURRENCY || 8),
  
  // Health
  HEALTH_PORT: process.env.PORT || 8080,
  
  // Logging
  LOG_LEVEL: process.env.LOG_LEVEL || 'INFO'
};

// Validaciones
if (CONFIG.TRADING_MODE === 'LIVE' && !CONFIG.SOLANA_WALLET_PATH && !process.env.SOLANA_PRIVATE_KEY) {
  console.warn('⚠️ TRADING_MODE=LIVE pero SOLANA_WALLET_PATH/SOLANA_PRIVATE_KEY no está configurado');
}

// ✅ Validación de RPC
if (!process.env.HELIUS_RPC_URL && !process.env.RPC_URL) {
  console.warn('⚠️ Usando RPC público de Solana - considera usar Helius para mejor performance');
  console.warn('📝 Regístrate gratis en: https://helius.dev');
} else if (process.env.HELIUS_RPC_URL) {
  console.log('✅ Helius RPC configurado');
}

// ✅ Validación de timeouts
if (CONFIG.PRICE_CHECK_INTERVAL_SEC < 3) {
  console.warn('⚠️ PRICE_CHECK_INTERVAL_SEC muy bajo, puede causar rate limiting');
  CONFIG.PRICE_CHECK_INTERVAL_SEC = 3;
}

if (CONFIG.INITIAL_DATA_RETRY_DELAY < 1000) {
  console.warn('⚠️ INITIAL_DATA_RETRY_DELAY muy bajo, tokens nuevos pueden no estar listos');
  CONFIG.INITIAL_DATA_RETRY_DELAY = 1000;
}

// ✅ Mostrar configuración crítica al inicio
if (CONFIG.LOG_LEVEL === 'DEBUG' || CONFIG.LOG_LEVEL === 'INFO') {
  console.log('\n📋 Configuración cargada:');
  console.log(`   RPC: ${CONFIG.RPC_URL.split('?')[0]}...`);
  console.log(`   Trading Mode: ${CONFIG.TRADING_MODE}`);
  console.log(`   Price Check Interval: ${CONFIG.PRICE_CHECK_INTERVAL_SEC}s`);
  console.log(`   Data Retry Delay: ${CONFIG.INITIAL_DATA_RETRY_DELAY}ms`);
  console.log(`   Max Retries: ${CONFIG.MAX_DATA_FETCH_RETRIES}`);
  console.log(`   Min Liquidity: $${CONFIG.MIN_INITIAL_LIQUIDITY_USD}`);
  console.log(`   Max Bonding Curve: ${CONFIG.MAX_BONDING_CURVE_PROGRESS}%`);
  console.log('');
}

module.exports = CONFIG;
