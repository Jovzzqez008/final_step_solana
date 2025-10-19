#!/usr/bin/env node
// verify-setup.js
// 🔍 Verifica que todo esté configurado correctamente antes de iniciar el bot

require('dotenv').config();
const fs = require('fs');
const axios = require('axios');

console.log('\n');
console.log('═══════════════════════════════════════════════════════════════');
console.log('🔍 VERIFICACIÓN DE CONFIGURACIÓN');
console.log('═══════════════════════════════════════════════════════════════');
console.log('');

let errors = 0;
let warnings = 0;

// ============================================================================
// Helper functions
// ============================================================================

function checkOk(message) {
  console.log(`✅ ${message}`);
}

function checkError(message) {
  console.log(`❌ ${message}`);
  errors++;
}

function checkWarning(message) {
  console.log(`⚠️  ${message}`);
  warnings++;
}

// ============================================================================
// 1. Verificar archivos necesarios
// ============================================================================

console.log('📁 Verificando archivos...\n');

const requiredFiles = [
  'trading-bot-hybrid.js',
  '.env',
  'package.json'
];

for (const file of requiredFiles) {
  if (fs.existsSync(file)) {
    checkOk(`Archivo encontrado: ${file}`);
  } else {
    checkError(`Archivo faltante: ${file}`);
  }
}

console.log('');

// ============================================================================
// 2. Verificar variables de entorno
// ============================================================================

console.log('🔐 Verificando variables de entorno...\n');

// HELIUS_API_KEY
if (process.env.HELIUS_API_KEY) {
  if (process.env.HELIUS_API_KEY.length > 20) {
    checkOk('HELIUS_API_KEY configurado');
  } else {
    checkError('HELIUS_API_KEY parece inválido (muy corto)');
  }
} else {
  checkError('HELIUS_API_KEY no configurado');
}

// TELEGRAM
if (process.env.TELEGRAM_BOT_TOKEN) {
  checkOk('TELEGRAM_BOT_TOKEN configurado');
} else {
  checkError('TELEGRAM_BOT_TOKEN no configurado');
}

if (process.env.TELEGRAM_CHAT_ID) {
  checkOk('TELEGRAM_CHAT_ID configurado');
} else {
  checkError('TELEGRAM_CHAT_ID no configurado');
}

// WALLET
if (process.env.WALLET_PRIVATE_KEY) {
  if (process.env.WALLET_PRIVATE_KEY.length > 40) {
    checkOk('WALLET_PRIVATE_KEY configurado');
  } else {
    checkError('WALLET_PRIVATE_KEY parece inválido');
  }
} else {
  checkError('WALLET_PRIVATE_KEY no configurado');
}

// DRY_RUN
if (process.env.DRY_RUN !== undefined) {
  if (process.env.DRY_RUN === 'true') {
    checkOk('DRY_RUN configurado: true (Simulación)');
  } else if (process.env.DRY_RUN === 'false') {
    checkWarning('DRY_RUN configurado: false (DINERO REAL)');
  } else {
    checkWarning(`DRY_RUN tiene valor extraño: "${process.env.DRY_RUN}"`);
  }
} else {
  checkWarning('DRY_RUN no configurado (usará default: true)');
}

// TRADE_AMOUNT_SOL
const tradeAmount = parseFloat(process.env.TRADE_AMOUNT_SOL || '0.007');
if (tradeAmount > 0 && tradeAmount < 0.1) {
  checkOk(`TRADE_AMOUNT_SOL: ${tradeAmount} SOL`);
  if (tradeAmount > 0.01 && process.env.DRY_RUN === 'false') {
    checkWarning('TRADE_AMOUNT_SOL es alto para primeras pruebas en LIVE');
  }
} else if (tradeAmount >= 0.1) {
  checkWarning(`TRADE_AMOUNT_SOL muy alto: ${tradeAmount} SOL`);
} else {
  checkError('TRADE_AMOUNT_SOL inválido o no configurado');
}

console.log('');

// ============================================================================
// 3. Verificar conexión a Helius
// ============================================================================

console.log('🌐 Verificando conexión a Helius...\n');

async function verifyHelius() {
  if (!process.env.HELIUS_API_KEY) {
    checkError('No se puede verificar Helius sin API key');
    return;
  }

  try {
    // Test RPC
    const rpcResponse = await axios.post(
      `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`,
      {
        jsonrpc: '2.0',
        id: 1,
        method: 'getBlockHeight'
      },
      { timeout: 5000 }
    );
    
    if (rpcResponse.data.result) {
      checkOk(`Helius RPC funcional (block: ${rpcResponse.data.result})`);
    }
    
    // Test Enhanced API
    const testMint = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'; // USDC
    const apiResponse = await axios.get(
      `https://api.helius.xyz/v0/addresses/${testMint}/transactions?api-key=${process.env.HELIUS_API_KEY}&limit=1`,
      { timeout: 5000 }
    );
    
    if (apiResponse.data && Array.isArray(apiResponse.data)) {
      checkOk('Helius Enhanced API funcional');
    }
  } catch (error) {
    if (error.response && error.response.status === 401) {
      checkError('Helius API key inválida');
    } else if (error.response && error.response.status === 429) {
      checkWarning('Helius rate limit alcanzado (normal si ya usaste mucho hoy)');
    } else {
      checkError(`Error conectando a Helius: ${error.message}`);
    }
  }
}

// ============================================================================
// 4. Verificar Telegram
// ============================================================================

console.log('📱 Verificando Telegram...\n');

async function verifyTelegram() {
  if (!process.env.TELEGRAM_BOT_TOKEN) {
    checkError('No se puede verificar Telegram sin bot token');
    return;
  }

  try {
    const response = await axios.get(
      `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/getMe`,
      { timeout: 5000 }
    );
    
    if (response.data.ok) {
      checkOk(`Telegram bot: @${response.data.result.username}`);
    } else {
      checkError('Telegram bot token inválido');
    }
    
    // Test chat ID
    if (process.env.TELEGRAM_CHAT_ID) {
      try {
        await axios.get(
          `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/getChat?chat_id=${process.env.TELEGRAM_CHAT_ID}`,
          { timeout: 5000 }
        );
        checkOk('Telegram chat ID válido');
      } catch (error) {
        checkError('Telegram chat ID inválido o bot no tiene acceso');
      }
    }
  } catch (error) {
    checkError(`Error conectando a Telegram: ${error.message}`);
  }
}

// ============================================================================
// 5. Verificar wallet
// ============================================================================

console.log('💼 Verificando wallet...\n');

async function verifyWallet() {
  if (!process.env.WALLET_PRIVATE_KEY) {
    checkError('No se puede verificar wallet sin private key');
    return;
  }

  try {
    const { Keypair, Connection } = require('@solana/web3.js');
    const bs58 = require('bs58');
    
    let keypair;
    try {
      // Try base58 format
      const decoded = bs58.decode(process.env.WALLET_PRIVATE_KEY);
      keypair = Keypair.fromSecretKey(decoded);
    } catch {
      // Try JSON array format
      const secretKey = JSON.parse(process.env.WALLET_PRIVATE_KEY);
      keypair = Keypair.fromSecretKey(Uint8Array.from(secretKey));
    }
    
    checkOk(`Wallet válido: ${keypair.publicKey.toString()}`);
    
    // Check balance
    if (process.env.HELIUS_API_KEY) {
      const rpcUrl = `https://mainnet.helius-rpc.com/?api-key=${process.env.HELIUS_API_KEY}`;
      const connection = new Connection(rpcUrl, 'confirmed');
      const balance = await connection.getBalance(keypair.publicKey);
      const solBalance = balance / 1e9;
      
      if (solBalance > 0.05) {
        checkOk(`Balance: ${solBalance.toFixed(4)} SOL`);
      } else if (solBalance > 0) {
        checkWarning(`Balance bajo: ${solBalance.toFixed(4)} SOL (recomendado: >0.05)`);
      } else {
        checkError('Wallet sin balance (necesitas SOL para operar)');
      }
    }
  } catch (error) {
    checkError(`Error verificando wallet: ${error.message}`);
  }
}

// ============================================================================
// 6. Verificar dependencias de Node
// ============================================================================

console.log('📦 Verificando dependencias...\n');

function verifyDependencies() {
  const requiredDeps = [
    'ws',
    'node-telegram-bot-api',
    'axios',
    '@solana/web3.js',
    'dotenv',
    'bs58'
  ];
  
  let packageJson;
  try {
    packageJson = JSON.parse(fs.readFileSync('package.json', 'utf8'));
  } catch {
    checkError('No se puede leer package.json');
    return;
  }
  
  const allDeps = {
    ...packageJson.dependencies,
    ...packageJson.devDependencies
  };
  
  for (const dep of requiredDeps) {
    if (allDeps[dep]) {
      checkOk(`${dep} instalado`);
    } else {
      checkError(`${dep} NO instalado (ejecuta: npm install ${dep})`);
    }
  }
}

verifyDependencies();

// ============================================================================
// 7. Ejecutar verificaciones asíncronas
// ============================================================================

(async () => {
  await verifyHelius();
  console.log('');
  await verifyTelegram();
  console.log('');
  await verifyWallet();
  console.log('');
  
  // ============================================================================
  // Resumen final
  // ============================================================================
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('📊 RESUMEN');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('');
  
  if (errors === 0 && warnings === 0) {
    console.log('✅ TODO PERFECTO - Listo para iniciar el bot');
    console.log('');
    console.log('Próximos pasos:');
    console.log('1. Asegúrate que DRY_RUN=true en .env');
    console.log('2. Ejecuta: node trading-bot-hybrid.js');
    console.log('3. Monitorea en Telegram con /status y /smartstats');
    console.log('');
  } else {
    console.log(`❌ ${errors} error(es) encontrado(s)`);
    console.log(`⚠️  ${warnings} advertencia(s)`);
    console.log('');
    console.log('Por favor corrige los errores antes de iniciar el bot.');
    console.log('');
    
    if (errors > 0) {
      console.log('💡 Errores comunes:');
      console.log('   • HELIUS_API_KEY: Obtén en https://helius.xyz');
      console.log('   • TELEGRAM_BOT_TOKEN: Crea bot con @BotFather');
      console.log('   • WALLET_PRIVATE_KEY: Exporta desde Phantom/Solflare');
      console.log('   • Dependencias: Ejecuta npm install');
      console.log('');
    }
  }
  
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('');
  
  process.exit(errors > 0 ? 1 : 0);
})();
