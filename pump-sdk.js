// pump-sdk.js - Implementación REAL con Helius + Bonding Curve
const { Connection, PublicKey } = require('@solana/web3.js');
const { struct, u64, bool, publicKey } = require('@coral-xyz/borsh');
const axios = require('axios');

// Configuración Helius (MUCHO mejor para Pump.fun)
const connection = new Connection(
  process.env.HELIUS_RPC_URL || process.env.RPC_URL || 'https://api.mainnet-beta.solana.com',
  'confirmed'
);

// Estructura de datos de la Bonding Curve Account (Pump.fun)
const BondingCurveLayout = struct([
  publicKey('global'),           // 0: global account
  publicKey('mint'),             // 32: token mint
  u64('virtualSolReserves'),     // 64: virtual SOL reserves
  u64('virtualTokenReserves'),   // 72: virtual token reserves  
  u64('realSolReserves'),        // 80: real SOL reserves
  bool('complete'),              // 88: is complete (migrated to Raydium)
  u64('tokenTotalSupply'),       // 89: total token supply
]);

// Pump.fun program IDs
const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const BONDING_CURVE_PROGRAM_ID = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');

async function getTokenDataFromBondingCurve(mintAddress) {
  try {
    const mint = new PublicKey(mintAddress);
    
    console.log(`🔍 Buscando bonding curve para: ${mintAddress.slice(0, 8)}`);
    
    // 1. Encontrar la bonding curve account address
    const [bondingCurveAddress] = PublicKey.findProgramAddressSync(
      [Buffer.from('bonding-curve'), mint.toBuffer()],
      BONDING_CURVE_PROGRAM_ID
    );

    console.log(`📍 Bonding curve address: ${bondingCurveAddress.toString()}`);
    
    // 2. Obtener los datos de la cuenta
    const bondingCurveAccount = await connection.getAccountInfo(bondingCurveAddress);
    
    if (!bondingCurveAccount) {
      console.log(`❌ No se encontró bonding curve account`);
      return await getTokenDataFromPumpFunAPI(mintAddress); // Fallback
    }

    // 3. Parsear los datos usando Borsh
    let bondingCurveData;
    try {
      bondingCurveData = BondingCurveLayout.decode(bondingCurveAccount.data);
    } catch (error) {
      console.log(`❌ Error parseando bonding curve: ${error.message}`);
      return await getTokenDataFromPumpFunAPI(mintAddress); // Fallback
    }

    console.log(`📊 Bonding curve data:`, {
      virtualSolReserves: bondingCurveData.virtualSolReserves.toString(),
      virtualTokenReserves: bondingCurveData.virtualTokenReserves.toString(),
      realSolReserves: bondingCurveData.realSolReserves.toString(),
      complete: bondingCurveData.complete,
      tokenTotalSupply: bondingCurveData.tokenTotalSupply.toString()
    });

    // 4. CALCULAR PRECIO REAL desde la bonding curve
    const virtualSolReserves = Number(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = Number(bondingCurveData.virtualTokenReserves);
    
    if (virtualTokenReserves === 0) {
      console.log(`❌ Virtual token reserves es cero`);
      return null;
    }

    // Fórmula: price = virtualSolReserves / virtualTokenReserves
    const priceInLamports = virtualSolReserves / virtualTokenReserves;
    const priceInSol = priceInLamports; // Ya está en lamports por token
    const priceInUsd = await convertSolToUsd(priceInSol);

    // 5. CALCULAR MARKET CAP REAL
    const tokenTotalSupply = Number(bondingCurveData.tokenTotalSupply);
    const typicalSupply = tokenTotalSupply > 0 ? tokenTotalSupply : 1000000; // 1M si no disponible
    const marketCapUsd = priceInUsd * typicalSupply;

    // 6. CALCULAR LIQUIDEZ REAL
    const realSolReserves = Number(bondingCurveData.realSolReserves);
    const liquiditySol = realSolReserves / 1e9; // Convertir lamports a SOL
    const liquidityUsd = await convertSolToUsd(liquiditySol);

    // 7. CALCULAR PROGRESO BONDING CURVE
    const bondingCurveProgress = calculateBondingCurveProgress(realSolReserves);

    console.log(`🎯 Cálculos finales:`, {
      priceInUsd: priceInUsd,
      marketCapUsd: marketCapUsd,
      liquidityUsd: liquidityUsd,
      bondingCurveProgress: bondingCurveProgress
    });

    return {
      price: priceInUsd,
      liquidity: liquidityUsd,
      marketCap: marketCapUsd,
      virtualSolReserves: virtualSolReserves,
      virtualTokenReserves: virtualTokenReserves,
      realSolReserves: liquiditySol,
      bondingCurve: bondingCurveProgress,
      isComplete: bondingCurveData.complete,
      tokenTotalSupply: typicalSupply,
      source: 'helius-bonding-curve'
    };

  } catch (error) {
    console.error(`❌ Error Helius bonding curve para ${mintAddress.slice(0, 8)}:`, error.message);
    return await getTokenDataFromPumpFunAPI(mintAddress); // Fallback
  }
}

// Función para calcular progreso de bonding curve
function calculateBondingCurveProgress(realSolReserves) {
  const solReserves = realSolReserves / 1e9; // Convertir a SOL
  
  // Pump.fun migra a Raydium cuando tiene ~460 SOL ($69k @ $150/SOL)
  const migrationThreshold = 460; // SOL
  const progress = Math.min(100, (solReserves / migrationThreshold) * 100);
  
  return Math.round(progress);
}

// Convertir SOL a USD
async function convertSolToUsd(solAmount) {
  try {
    const solPrice = await getCurrentSolPrice();
    return solAmount * solPrice;
  } catch (error) {
    console.log('⚠️ Usando precio SOL por defecto: $150');
    return solAmount * 150;
  }
}

// Fallback: API directa de Pump.fun
async function getTokenDataFromPumpFunAPI(mintAddress) {
  try {
    const response = await axios.get(
      `https://frontend-api.pump.fun/coins/${mintAddress}`,
      { timeout: 5000 }
    );
    
    if (response.data) {
      const data = response.data;
      return {
        price: parseFloat(data.price_usd || 0),
        marketCap: parseFloat(data.market_cap || 0),
        liquidity: parseFloat(data.liquidity_usd || 0),
        bondingCurve: parseFloat(data.bonding_curve_progress || 0),
        isComplete: data.is_complete || false,
        source: 'pump.fun-api'
      };
    }
    
    return null;
  } catch (error) {
    console.log('❌ Pump.fun API falló:', error.message);
    return await getTokenDataFromDexScreener(mintAddress); // Último fallback
  }
}

// Último fallback: DexScreener
async function getTokenDataFromDexScreener(mintAddress) {
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mintAddress}`,
      { timeout: 5000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      const pair = response.data.pairs[0];
      
      // Calcular market cap aproximado si no está disponible
      let marketCap = parseFloat(pair.fdv || pair.marketCap || 0);
      if (marketCap === 0 && pair.priceUsd) {
        marketCap = parseFloat(pair.priceUsd) * 1000000; // Asumir 1M supply
      }

      return {
        price: parseFloat(pair.priceUsd || 0),
        marketCap: marketCap,
        liquidity: parseFloat(pair.liquidity?.usd || 0),
        bondingCurve: 0, // No disponible en DexScreener
        isComplete: false,
        source: 'dexscreener'
      };
    }
    
    return null;
  } catch (error) {
    console.error('❌ DexScreener error:', error.message);
    return null;
  }
}

// Obtener precio SOL
async function getCurrentSolPrice() {
  try {
    const response = await axios.get(
      'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd',
      { timeout: 5000 }
    );
    return response.data.solana.usd || 150;
  } catch (error) {
    console.log('⚠️ No se pudo obtener precio SOL, usando $150');
    return 150;
  }
}

module.exports = {
  getTokenDataFromBondingCurve,
  getTokenDataFromDexScreener,
  getCurrentSolPrice
};
