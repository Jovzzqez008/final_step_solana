// pump-sdk.js - Implementación directa sin SDK externo
const { Connection, PublicKey } = require('@solana/web3.js');
const axios = require('axios');

// Configuración
const connection = new Connection(
  process.env.RPC_URL || 'https://api.mainnet-beta.solana.com',
  'confirmed'
);

// Función para obtener datos de tokens usando DexScreener (más confiable)
async function getTokenDataFromDexScreener(mintAddress) {
  try {
    const response = await axios.get(
      `https://api.dexscreener.com/latest/dex/tokens/${mintAddress}`,
      { timeout: 5000 }
    );
    
    if (response.data.pairs && response.data.pairs.length > 0) {
      const pair = response.data.pairs[0];
      
      // Buscar el par que sea de Pump.fun o el más líquido
      const pumpPair = response.data.pairs.find(p => 
        p.dexId === 'pump.fun' || p.url?.includes('pump.fun')
      ) || response.data.pairs[0];

      return {
        price: parseFloat(pumpPair.priceUsd || 0),
        marketCap: parseFloat(pumpPair.fdv || pumpPair.marketCap || 0),
        liquidity: parseFloat(pumpPair.liquidity?.usd || 0),
        volume: parseFloat(pumpPair.volume?.h24 || 0),
        priceChange: parseFloat(pumpPair.priceChange?.h24 || 0),
        dexId: pumpPair.dexId,
        url: pumpPair.url,
        source: 'dexscreener'
      };
    }
    
    return null;
  } catch (error) {
    console.error('❌ DexScreener error:', error.message);
    return null;
  }
}

// Función alternativa para obtener datos de bonding curve via RPC directo
async function getBondingCurveData(mintAddress) {
  try {
    const mint = new PublicKey(mintAddress);
    
    // Aquí iría la lógica para leer directamente la bonding curve account
    // Por ahora usamos DexScreener como fallback
    
    return await getTokenDataFromDexScreener(mintAddress);
  } catch (error) {
    console.error('❌ Bonding curve RPC error:', error.message);
    return await getTokenDataFromDexScreener(mintAddress);
  }
}

// Función para obtener precio de SOL
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

// Función principal mejorada
async function getTokenDataFromBondingCurve(mintAddress) {
  try {
    // PRIMERO: Intentar con DexScreener (más confiable)
    const dexscreenerData = await getTokenDataFromDexScreener(mintAddress);
    
    if (dexscreenerData && dexscreenerData.price > 0) {
      // Calcular bonding curve progress aproximado basado en liquidez
      let bondingCurveProgress = 0;
      if (dexscreenerData.liquidity > 0) {
        // Pump.fun migra a ~$69k ≈ 460 SOL @ $150
        bondingCurveProgress = Math.min(100, (dexscreenerData.liquidity / 69000) * 100);
      }
      
      return {
        price: dexscreenerData.price,
        liquidity: dexscreenerData.liquidity,
        marketCap: dexscreenerData.marketCap,
        volume: dexscreenerData.volume,
        priceChange: dexscreenerData.priceChange,
        bondingCurve: Math.round(bondingCurveProgress),
        isComplete: bondingCurveProgress >= 95, // Asumir completo cerca de 100%
        source: 'dexscreener'
      };
    }
    
    return null;
  } catch (error) {
    console.error('❌ Error obteniendo datos del token:', error.message);
    return null;
  }
}

// Función para múltiples tokens
async function getMultipleTokenData(mints) {
  const results = {};
  
  for (const mint of mints) {
    results[mint] = await getTokenDataFromBondingCurve(mint);
    // Pequeño delay para no saturar la API
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  
  return results;
}

module.exports = {
  getTokenDataFromBondingCurve,
  getMultipleTokenData,
  getCurrentSolPrice,
  getTokenDataFromDexScreener
};
