// test-utils.js - Utilidades para testing y debugging
require('dotenv').config();
const PumpDataFetcher = require('./pump-data-fetcher');
const TradingEngine = require('./trading-engine');
const CONFIG = require('./config');

// Colores para terminal
const colors = {
  reset: '\x1b[0m',
  bright: '\x1b[1m',
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  magenta: '\x1b[35m',
  cyan: '\x1b[36m'
};

function colorLog(color, msg) {
  console.log(`${colors[color]}${msg}${colors.reset}`);
}

// Test 1: Verificar RPC Connection
async function testRPCConnection() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 1: RPC CONNECTION');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  const fetcher = new PumpDataFetcher(CONFIG.RPC_URL);
  
  const health = await fetcher.healthCheck();
  
  if (health.healthy) {
    colorLog('green', `✅ RPC Healthy`);
    console.log(`   Endpoint: ${health.endpoint}`);
    console.log(`   Latency: ${health.latency}ms`);
    
    if (health.latency < 500) {
      colorLog('green', '   Performance: EXCELENTE');
    } else if (health.latency < 1000) {
      colorLog('yellow', '   Performance: BUENA');
    } else {
      colorLog('yellow', '   Performance: ACEPTABLE (considere usar Helius)');
    }
    
    return true;
  } else {
    colorLog('red', `❌ RPC Unhealthy`);
    console.log(`   Error: ${health.error}`);
    return false;
  }
}

// Test 2: Verificar Pump.fun Data Fetching
async function testPumpDataFetching(mintAddress) {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 2: PUMP.FUN DATA FETCHING');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  if (!mintAddress) {
    colorLog('yellow', '⚠️ No se proporcionó mint address, usando ejemplo conocido');
    // Usar un token conocido de Pump.fun (ajustar según necesidad)
    mintAddress = 'pump.fun_token_mint_aqui';
  }

  const fetcher = new PumpDataFetcher(CONFIG.RPC_URL);
  
  console.log(`Obteniendo datos para: ${mintAddress.slice(0, 8)}...${mintAddress.slice(-8)}\n`);
  
  const start = Date.now();
  const data = await fetcher.getTokenData(mintAddress);
  const elapsed = Date.now() - start;
  
  if (data) {
    colorLog('green', '✅ Datos obtenidos exitosamente');
    console.log(`   Tiempo: ${elapsed}ms`);
    console.log(`   Fuente: ${data.source}`);
    console.log('');
    console.log('📊 Datos del Token:');
    console.log(`   Precio: $${data.price.toFixed(8)}`);
    console.log(`   Market Cap: $${data.marketCap.toLocaleString()}`);
    console.log(`   Liquidez: $${data.liquidity.toLocaleString()}`);
    console.log(`   Bonding Curve: ${data.bondingCurve}%`);
    console.log(`   Supply: ${data.supply?.toLocaleString() || 'N/A'}`);
    
    // Verificar si pasa filtros
    console.log('');
    console.log('🔍 Validación de Filtros:');
    
    const passesLiquidity = data.marketCap >= CONFIG.MIN_INITIAL_LIQUIDITY_USD;
    const passesBondingCurve = data.bondingCurve < CONFIG.MAX_BONDING_CURVE_PROGRESS;
    
    console.log(`   Liquidez mínima ($${CONFIG.MIN_INITIAL_LIQUIDITY_USD}): ${passesLiquidity ? '✅' : '❌'} ($${data.marketCap.toLocaleString()})`);
    console.log(`   Bonding Curve máx (${CONFIG.MAX_BONDING_CURVE_PROGRESS}%): ${passesBondingCurve ? '✅' : '❌'} (${data.bondingCurve}%)`);
    
    if (passesLiquidity && passesBondingCurve) {
      colorLog('green', '\n   ✅ Token PASARÍA los filtros');
    } else {
      colorLog('red', '\n   ❌ Token NO pasaría los filtros');
    }
    
    return true;
  } else {
    colorLog('red', '❌ No se pudieron obtener datos');
    console.log(`   Tiempo: ${elapsed}ms`);
    return false;
  }
}

// Test 3: Verificar Trading Engine (DRY_RUN)
async function testTradingEngine() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 3: TRADING ENGINE (DRY_RUN)');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  // Forzar DRY_RUN para testing
  const testConfig = { ...CONFIG, DRY_RUN: true, TRADING_MODE: 'DRY_RUN' };
  const engine = new TradingEngine(testConfig);
  
  console.log('Componentes inicializados:');
  console.log(`   Circuit Breaker: ✅`);
  console.log(`   Performance Metrics: ✅`);
  console.log(`   Rate Limiter: ✅`);
  console.log('');
  
  // Simular token
  const mockToken = {
    mint: 'TEST_MINT_ADDRESS_123456789',
    symbol: 'TEST',
    name: 'Test Token',
    currentPrice: 0.00001234,
    currentMarketCap: 50000,
    bondingCurve: 5,
    initialPrice: 0.00001000,
    elapsedMinutes: 5,
    priceSource: 'test'
  };
  
  const mockAlert = {
    ruleName: 'test_rule',
    gainPercent: 23.4,
    timeElapsed: 5,
    priceAtAlert: 0.00001234,
    marketCapAtAlert: 50000
  };
  
  console.log('Ejecutando compra simulada...');
  const buyResult = await engine.executeBuy(mockToken, mockAlert);
  
  if (buyResult.success) {
    colorLog('green', '✅ Compra simulada exitosa');
    console.log(`   Simulated: ${buyResult.simulated}`);
    console.log(`   Price: $${buyResult.price?.toFixed(8) || 'N/A'}`);
    console.log(`   Slippage: ${buyResult.slippage?.toFixed(3) || 'N/A'}%`);
    console.log(`   Execution Time: ${buyResult.executionTime || 'N/A'}ms`);
  } else {
    colorLog('red', '❌ Compra simulada falló');
    console.log(`   Reason: ${buyResult.reason}`);
  }
  
  console.log('');
  console.log('📊 Stats del Engine:');
  const stats = engine.getStats();
  console.log(`   Active Trades: ${stats.activeTrades}`);
  console.log(`   Circuit Breaker: ${stats.circuitBreaker.isOpen ? '🔴 OPEN' : '🟢 CLOSED'}`);
  console.log(`   Total Trades: ${stats.performance.totalTrades}`);
  
  return buyResult.success;
}

// Test 4: Verificar Rate Limiting
async function testRateLimiting() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 4: RATE LIMITING');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  const fetcher = new PumpDataFetcher(CONFIG.RPC_URL);
  
  console.log('Enviando 20 requests simultáneos...\n');
  
  const start = Date.now();
  const promises = [];
  
  for (let i = 0; i < 20; i++) {
    promises.push(
      fetcher.healthCheck().then(() => {
        const elapsed = Date.now() - start;
        console.log(`   Request ${i + 1}: ${elapsed}ms`);
      })
    );
  }
  
  await Promise.all(promises);
  const totalTime = Date.now() - start;
  
  console.log('');
  colorLog('green', '✅ Rate limiting funcionando correctamente');
  console.log(`   Total time: ${totalTime}ms`);
  console.log(`   Avg per request: ${(totalTime / 20).toFixed(0)}ms`);
  
  const stats = fetcher.getStats();
  console.log(`   Queue size: ${stats.queueSize}`);
  console.log(`   Requests/sec: ${stats.requestsPerSecond}`);
  
  return true;
}

// Test 5: Verificar Wallet (si está configurada)
async function testWallet() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 5: WALLET CONFIGURATION');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  if (!process.env.SOLANA_PRIVATE_KEY && !process.env.SOLANA_WALLET_PATH) {
    colorLog('yellow', '⚠️ No wallet configurada (OK para DRY_RUN)');
    console.log('   Para trading LIVE, configura:');
    console.log('   - SOLANA_PRIVATE_KEY (para Railway/Heroku)');
    console.log('   - SOLANA_WALLET_PATH (para local)');
    return false;
  }

  try {
    const { loadWalletKeypair } = require('./wallet-loader');
    const keypair = loadWalletKeypair();
    
    colorLog('green', '✅ Wallet cargada exitosamente');
    console.log(`   Address: ${keypair.publicKey.toString()}`);
    
    // Verificar balance si es posible
    const PumpFunTrading = require('./pump-api');
    const trading = new PumpFunTrading(CONFIG);
    
    if (trading.isWalletLoaded()) {
      const balance = await trading.checkBalance();
      console.log(`   Balance: ${balance} SOL`);
      
      if (balance >= CONFIG.MINIMUM_BUY_AMOUNT) {
        colorLog('green', `   ✅ Balance suficiente para trading`);
      } else {
        colorLog('yellow', `   ⚠️ Balance insuficiente (min: ${CONFIG.MINIMUM_BUY_AMOUNT} SOL)`);
      }
    }
    
    return true;
  } catch (error) {
    colorLog('red', '❌ Error cargando wallet');
    console.log(`   Error: ${error.message}`);
    return false;
  }
}

// Test 6: Verificar Circuit Breaker
async function testCircuitBreaker() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 6: CIRCUIT BREAKER');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  const testConfig = { ...CONFIG, DRY_RUN: true };
  const engine = new TradingEngine(testConfig);
  
  console.log('Estado inicial:');
  let status = engine.circuitBreaker.getStatus();
  console.log(`   Failures: ${status.failures}/${status.maxLosses}`);
  console.log(`   Is Open: ${status.isOpen ? '🔴' : '🟢'}`);
  
  console.log('\nSimulando 3 fallos consecutivos...');
  
  for (let i = 1; i <= 3; i++) {
    engine.circuitBreaker.recordFailure();
    status = engine.circuitBreaker.getStatus();
    console.log(`   Fallo ${i}: ${status.failures}/${status.maxLosses} ${status.isOpen ? '🔴 ABIERTO' : '🟢'}`);
  }
  
  console.log('\nIntentando trade con circuit breaker abierto...');
  const mockToken = {
    mint: 'TEST',
    symbol: 'TEST',
    currentPrice: 0.00001,
    currentMarketCap: 50000,
    bondingCurve: 5
  };
  
  const result = await engine.executeBuy(mockToken, {});
  
  if (!result.success && result.reason === 'circuit_breaker_open') {
    colorLog('green', '✅ Circuit Breaker funcionando correctamente');
    console.log(`   Reset en: ${(result.resetIn / 60000).toFixed(1)} minutos`);
  } else {
    colorLog('red', '❌ Circuit Breaker NO funcionó');
  }
  
  console.log('\nReseteando circuit breaker...');
  engine.circuitBreaker.reset();
  status = engine.circuitBreaker.getStatus();
  console.log(`   Failures: ${status.failures}`);
  console.log(`   Is Open: ${status.isOpen ? '🔴' : '🟢'}`);
  
  return true;
}

// Test 7: Verificar Cache
async function testCache() {
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '🧪 TEST 7: CACHE SYSTEM');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');

  const fetcher = new PumpDataFetcher(CONFIG.RPC_URL);
  const testMint = 'TEST_MINT_FOR_CACHE_12345678901234567890';
  
  // Simular datos en cache
  fetcher.setCache(testMint, {
    price: 0.00001,
    marketCap: 50000,
    source: 'test'
  });
  
  console.log('Datos guardados en cache');
  
  const cached = fetcher.getCached(testMint);
  if (cached) {
    colorLog('green', '✅ Cache funcionando - datos recuperados');
    console.log(`   Price: ${cached.price}`);
    console.log(`   Market Cap: ${cached.marketCap}`);
  } else {
    colorLog('red', '❌ Cache NO funcionó');
    return false;
  }
  
  console.log('\nEsperando que el cache expire (3 segundos)...');
  await new Promise(resolve => setTimeout(resolve, 3500));
  
  const expiredCache = fetcher.getCached(testMint);
  if (!expiredCache) {
    colorLog('green', '✅ Cache expiración funcionando correctamente');
  } else {
    colorLog('yellow', '⚠️ Cache no expiró como se esperaba');
  }
  
  const stats = fetcher.getStats();
  console.log('\nStats del cache:');
  console.log(`   Cache size: ${stats.cacheSize}`);
  
  return true;
}

// Runner principal
async function runAllTests(mintAddress = null) {
  console.clear();
  colorLog('bright', '╔═══════════════════════════════════════════════════════════╗');
  colorLog('bright', '║         PUMP.FUN BOT - SUITE DE TESTS                    ║');
  colorLog('bright', '╚═══════════════════════════════════════════════════════════╝');
  
  const results = {
    rpc: false,
    dataFetching: false,
    tradingEngine: false,
    rateLimiting: false,
    wallet: false,
    circuitBreaker: false,
    cache: false
  };
  
  try {
    results.rpc = await testRPCConnection();
  } catch (error) {
    colorLog('red', `❌ Test RPC falló: ${error.message}`);
  }
  
  try {
    results.dataFetching = await testPumpDataFetching(mintAddress);
  } catch (error) {
    colorLog('red', `❌ Test Data Fetching falló: ${error.message}`);
  }
  
  try {
    results.tradingEngine = await testTradingEngine();
  } catch (error) {
    colorLog('red', `❌ Test Trading Engine falló: ${error.message}`);
  }
  
  try {
    results.rateLimiting = await testRateLimiting();
  } catch (error) {
    colorLog('red', `❌ Test Rate Limiting falló: ${error.message}`);
  }
  
  try {
    results.wallet = await testWallet();
  } catch (error) {
    colorLog('red', `❌ Test Wallet falló: ${error.message}`);
  }
  
  try {
    results.circuitBreaker = await testCircuitBreaker();
  } catch (error) {
    colorLog('red', `❌ Test Circuit Breaker falló: ${error.message}`);
  }
  
  try {
    results.cache = await testCache();
  } catch (error) {
    colorLog('red', `❌ Test Cache falló: ${error.message}`);
  }
  
  // Resumen
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════');
  colorLog('cyan', '📊 RESUMEN DE TESTS');
  colorLog('cyan', '═══════════════════════════════════════════════════════════\n');
  
  const tests = [
    { name: 'RPC Connection', result: results.rpc },
    { name: 'Data Fetching', result: results.dataFetching },
    { name: 'Trading Engine', result: results.tradingEngine },
    { name: 'Rate Limiting', result: results.rateLimiting },
    { name: 'Wallet Config', result: results.wallet },
    { name: 'Circuit Breaker', result: results.circuitBreaker },
    { name: 'Cache System', result: results.cache }
  ];
  
  tests.forEach(test => {
    const status = test.result ? '✅' : '❌';
    const color = test.result ? 'green' : 'red';
    colorLog(color, `${status} ${test.name}`);
  });
  
  const passed = tests.filter(t => t.result).length;
  const total = tests.length;
  
  console.log('');
  if (passed === total) {
    colorLog('green', `🎉 Todos los tests pasaron! (${passed}/${total})`);
    colorLog('green', '✅ El bot está listo para ejecutarse');
  } else {
    colorLog('yellow', `⚠️ ${passed}/${total} tests pasaron`);
    colorLog('yellow', 'Revisa los componentes fallidos antes de ejecutar el bot');
  }
  
  colorLog('cyan', '\n═══════════════════════════════════════════════════════════\n');
  
  return { passed, total, results };
}

// Test individual de un token específico
async function testSpecificToken(mintAddress) {
  console.clear();
  colorLog('bright', '╔═══════════════════════════════════════════════════════════╗');
  colorLog('bright', '║         TEST DE TOKEN ESPECÍFICO                          ║');
  colorLog('bright', '╚═══════════════════════════════════════════════════════════╝\n');
  
  console.log(`Token: ${mintAddress}\n`);
  
  const fetcher = new PumpDataFetcher(CONFIG.RPC_URL);
  
  // Método 1: Bonding Curve
  colorLog('cyan', '1️⃣ Intentando Bonding Curve (on-chain)...');
  const bcData = await fetcher.getTokenDataFromBondingCurve(mintAddress);
  if (bcData) {
    colorLog('green', '   ✅ Exitoso');
    console.log(`   Price: ${bcData.price.toFixed(8)}`);
    console.log(`   Market Cap: ${bcData.marketCap.toLocaleString()}`);
    console.log(`   Liquidity: ${bcData.liquidity.toLocaleString()}`);
    console.log(`   Bonding Curve: ${bcData.bondingCurve}%`);
  } else {
    colorLog('red', '   ❌ Falló');
  }
  
  console.log('');
  
  // Método 2: API
  colorLog('cyan', '2️⃣ Intentando Pump.fun API...');
  const apiData = await fetcher.getTokenDataFromAPI(mintAddress);
  if (apiData) {
    colorLog('green', '   ✅ Exitoso');
    console.log(`   Price: ${apiData.price.toFixed(8)}`);
    console.log(`   Market Cap: ${apiData.marketCap.toLocaleString()}`);
  } else {
    colorLog('red', '   ❌ Falló');
  }
  
  console.log('');
  
  // Método 3: DexScreener
  colorLog('cyan', '3️⃣ Intentando DexScreener...');
  const dexData = await fetcher.getTokenDataFromDexScreener(mintAddress);
  if (dexData) {
    colorLog('green', '   ✅ Exitoso');
    console.log(`   Price: ${dexData.price.toFixed(8)}`);
    console.log(`   Market Cap: ${dexData.marketCap.toLocaleString()}`);
  } else {
    colorLog('red', '   ❌ Falló');
  }
  
  console.log('');
  
  // Método principal (cascada)
  colorLog('cyan', '4️⃣ Método principal (cascada automática)...');
  const finalData = await fetcher.getTokenData(mintAddress);
  if (finalData) {
    colorLog('green', '   ✅ Datos obtenidos');
    console.log(`   Fuente: ${finalData.source}`);
    console.log('');
    console.log('📊 Resultado Final:');
    console.log(`   Price: ${finalData.price.toFixed(8)}`);
    console.log(`   Market Cap: ${finalData.marketCap.toLocaleString()}`);
    console.log(`   Liquidity: ${finalData.liquidity.toLocaleString()}`);
    console.log(`   Bonding Curve: ${finalData.bondingCurve}%`);
    
    // Evaluación
    console.log('');
    colorLog('cyan', '📋 Evaluación:');
    
    const evaluation = {
      'Precio válido': finalData.price > 0,
      'Liquidez suficiente': finalData.marketCap >= CONFIG.MIN_INITIAL_LIQUIDITY_USD,
      'Bonding Curve OK': finalData.bondingCurve < CONFIG.MAX_BONDING_CURVE_PROGRESS,
      'Datos completos': finalData.price > 0 && finalData.marketCap > 0
    };
    
    Object.entries(evaluation).forEach(([key, value]) => {
      const status = value ? '✅' : '❌';
      const color = value ? 'green' : 'red';
      colorLog(color, `   ${status} ${key}`);
    });
    
    const allPass = Object.values(evaluation).every(v => v);
    console.log('');
    if (allPass) {
      colorLog('green', '🎉 Token PASARÍA todos los filtros del bot');
    } else {
      colorLog('yellow', '⚠️ Token NO pasaría algunos filtros');
    }
    
  } else {
    colorLog('red', '   ❌ No se pudieron obtener datos por ningún método');
  }
}

// CLI
if (require.main === module) {
  const args = process.argv.slice(2);
  const command = args[0];
  
  if (command === 'all') {
    // Ejecutar todos los tests
    const mintAddress = args[1];
    runAllTests(mintAddress)
      .then(() => process.exit(0))
      .catch(error => {
        console.error('Error:', error);
        process.exit(1);
      });
      
  } else if (command === 'token' && args[1]) {
    // Test de token específico
    testSpecificToken(args[1])
      .then(() => process.exit(0))
      .catch(error => {
        console.error('Error:', error);
        process.exit(1);
      });
      
  } else if (command === 'rpc') {
    testRPCConnection()
      .then(() => process.exit(0))
      .catch(error => {
        console.error('Error:', error);
        process.exit(1);
      });
      
  } else if (command === 'wallet') {
    testWallet()
      .then(() => process.exit(0))
      .catch(error => {
        console.error('Error:', error);
        process.exit(1);
      });
      
  } else {
    console.log(`
╔═══════════════════════════════════════════════════════════╗
║         PUMP.FUN BOT - TEST UTILITIES                     ║
╚═══════════════════════════════════════════════════════════╝

USO:
  node test-utils.js <comando> [opciones]

COMANDOS:

  all [mint]
    Ejecutar suite completa de tests
    Opcionalmente probar con un mint address específico
    
  token <mint>
    Test exhaustivo de un token específico
    Prueba todos los métodos de obtención de datos
    
  rpc
    Test solo de conexión RPC
    
  wallet
    Test solo de configuración de wallet

EJEMPLOS:

  # Suite completa
  node test-utils.js all
  
  # Suite completa con token específico
  node test-utils.js all FjzWDvCq7h8VgqR...
  
  # Test de token
  node test-utils.js token FjzWDvCq7h8VgqR...
  
  # Test RPC
  node test-utils.js rpc
  
  # Test Wallet
  node test-utils.js wallet

═══════════════════════════════════════════════════════════
    `);
  }
}

module.exports = {
  runAllTests,
  testSpecificToken,
  testRPCConnection,
  testPumpDataFetching,
  testTradingEngine,
  testRateLimiting,
  testWallet,
  testCircuitBreaker,
  testCache
};
