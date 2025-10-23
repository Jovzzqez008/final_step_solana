// pump-sdk.js - SDK oficial mejorado para datos reales
const { Connection, PublicKey } = require('@solana/web3.js');
const { PumpSdk } = require('@pumpdotfun/pump-sdk');

// Configuración
const connection = new Connection(
  process.env.RPC_URL || 'https://api.mainnet-beta.solana.com',
  'confirmed'
);
const sdk = new PumpSdk(connection);

async function getTokenDataFromBondingCurve(mintAddress) {
  try {
    const mint = new PublicKey(mintAddress);
    
    // Obtener estado completo de la bonding curve
    const bondingCurve = await sdk.fetchBondingCurve(mint);
    if (!bondingCurve) {
      console.log(`❌ No se encontró bonding curve para ${mintAddress.slice(0, 8)}`);
      return null;
    }

    // Calcular métricas esenciales
    const virtualSolReserves = Number(bondingCurve.virtualSolReserves);
    const virtualTokenReserves = Number(bondingCurve.virtualTokenReserves);
    const realSolReserves = Number(bondingCurve.realSolReserves);
    
    if (virtualTokenReserves === 0) {
      console.log(`❌ Reservas de token cero para ${mintAddress.slice(0, 8)}`);
      return null;
    }

    // Precio en SOL por token
    const priceInSol = virtualSolReserves / virtualTokenReserves;
    
    // Convertir a USD (usar precio actual de SOL)
    const solPriceUsd = Number(process.env.SOL_PRICE_USD || await getCurrentSolPrice());
    const priceUsd = priceInSol * solPriceUsd;

    // Calcular liquidez real (SOL en curva * precio SOL)
    const liquidityUsd = (realSolReserves / 1e9) * solPriceUsd;

    // Calcular progreso de bonding curve (% hacia Raydium)
    // Pump.fun migra a ~69k MCap ≈ 460 SOL
    const bondingCurveProgress = Math.min(100, (realSolReserves / 1e9) / 460 * 100);

    // Market cap aproximado (para tokens nuevos)
    const marketCap = priceUsd * (virtualTokenReserves / 1e6); // Asumiendo 6 decimales

    return {
      price: priceUsd,
      liquidity: liquidityUsd,
      marketCap: marketCap,
      virtualSolReserves,
      virtualTokenReserves,
      realSolReserves: realSolReserves / 1e9, // En SOL
      bondingCurve: Math.round(bondingCurveProgress),
      isComplete: bondingCurve.complete,
      source: 'pump-sdk'
    };

  } catch (error) {
    console.error(`❌ Error Pump SDK para ${mintAddress.slice(0, 8)}:`, error.message);
    return null;
  }
}

// Función auxiliar para obtener precio de SOL
async function getCurrentSolPrice() {
  try {
    const response = await fetch('https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd');
    const data = await response.json();
    return data.solana.usd || 150;
  } catch (error) {
    console.log('⚠️ No se pudo obtener precio SOL, usando $150');
    return 150;
  }
}

// Función para obtener múltiples tokens
async function getMultipleTokenData(mints) {
  const results = {};
  
  for (const mint of mints) {
    results[mint] = await getTokenDataFromBondingCurve(mint);
    // Pequeño delay para no saturar
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  
  return results;
}

module.exports = {
  getTokenDataFromBondingCurve,
  getMultipleTokenData,
  getCurrentSolPrice
};
