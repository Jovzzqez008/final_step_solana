// price-calculator.js - Calcula precios directamente desde pump.fun bonding curve

const { Connection, PublicKey } = require('@solana/web3.js');
const path = require('path');

let struct, u64, bool, layoutU64;

// ========================== SAFE IMPORT SETUP ==========================
try {
  // Cargar buffer-layout base
  const bufferLayout = require('@solana/buffer-layout');
  struct = bufferLayout.struct || bufferLayout;
  u64 = bufferLayout.u64 || bufferLayout.UInt64BE || null;
  bool = bufferLayout.bool || null;
} catch (e) {
  console.error('❌ Error cargando @solana/buffer-layout:', e.message);
}

try {
  // Cargar buffer-layout-utils para layoutU64
  const bufferUtils = require('@solana/buffer-layout-utils');
  layoutU64 = bufferUtils.u64 || bufferUtils.layoutU64 || bufferUtils.uint64 || null;
  if (!struct && bufferUtils.struct) struct = bufferUtils.struct;
  if (!u64 && bufferUtils.u64) u64 = bufferUtils.u64;
  if (!bool && bufferUtils.bool) bool = bufferUtils.bool;
} catch (e) {
  console.warn('⚠️ No se encontró @solana/buffer-layout-utils (fallback activo)');
}

// Validación dura para evitar crashes silenciosos
if (!struct) {
  throw new Error('❌ Falta `struct` de @solana/buffer-layout. Instala: npm install @solana/buffer-layout');
}
if (!layoutU64 && !u64) {
  throw new Error('❌ Falta función u64/layoutU64. Instala: npm install @solana/buffer-layout-utils');
}
if (!layoutU64 && u64) {
  layoutU64 = u64; // fallback
}
// ========================== END SAFE IMPORT ==========================


// ============================================================================
// PUMP.FUN CONSTANTS
// ============================================================================
const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const PUMP_GLOBAL_ACCOUNT = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');

// Bonding Curve Layout (según IDL)
const BONDING_CURVE_LAYOUT = struct([
  layoutU64('virtualTokenReserves'),
  layoutU64('virtualSolReserves'),
  layoutU64('realTokenReserves'),
  layoutU64('realSolReserves'),
  layoutU64('tokenTotalSupply'),
  bool('complete')
]);

// ============================================================================
// PRICE CALCULATOR CLASS
// ============================================================================
class PumpPriceCalculator {
  constructor(connection) {
    this.connection = connection;
  }

  async getBondingCurvePDA(mint) {
    const [bondingCurve] = await PublicKey.findProgramAddress(
      [Buffer.from('bonding-curve'), new PublicKey(mint).toBuffer()],
      PUMP_PROGRAM_ID
    );
    return bondingCurve;
  }

  async getBondingCurveData(mint) {
    try {
      const bondingCurvePDA = await this.getBondingCurvePDA(mint);
      const accountInfo = await this.connection.getAccountInfo(bondingCurvePDA);

      if (!accountInfo) {
        throw new Error('Bonding curve account not found');
      }

      const data = BONDING_CURVE_LAYOUT.decode(accountInfo.data);
      return {
        virtualTokenReserves: data.virtualTokenReserves.toString(),
        virtualSolReserves: data.virtualSolReserves.toString(),
        realTokenReserves: data.realTokenReserves.toString(),
        realSolReserves: data.realSolReserves.toString(),
        tokenTotalSupply: data.tokenTotalSupply.toString(),
        complete: data.complete
      };
    } catch (error) {
      console.error(`Error getting bonding curve data: ${error.message}`);
      return null;
    }
  }

  calculatePriceInSOL(bondingCurveData) {
    if (!bondingCurveData) return 0;
    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);
    if (virtualTokenReserves === 0n) return 0;
    const priceInSOL = Number(virtualSolReserves) / Number(virtualTokenReserves);
    return priceInSOL;
  }

  calculateTokensOut(bondingCurveData, solAmountIn) {
    if (!bondingCurveData) return 0;
    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);
    const solIn = BigInt(Math.floor(solAmountIn * 1e9));
    const numerator = solIn * virtualTokenReserves;
    const denominator = virtualSolReserves + solIn;
    if (denominator === 0n) return 0;
    const tokensOut = Number(numerator / denominator) / 1e9;
    return tokensOut;
  }

  calculateSOLOut(bondingCurveData, tokenAmountIn) {
    if (!bondingCurveData) return 0;
    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);
    const tokensIn = BigInt(Math.floor(tokenAmountIn * 1e9));
    const numerator = tokensIn * virtualSolReserves;
    const denominator = virtualSolReserves + tokensIn;
    if (denominator === 0n) return 0;
    const solOut = Number(numerator / denominator) / 1e9;
    return solOut;
  }

  calculateMarketCapSOL(bondingCurveData) {
    if (!bondingCurveData) return 0;
    const priceInSOL = this.calculatePriceInSOL(bondingCurveData);
    const totalSupply = Number(bondingCurveData.tokenTotalSupply) / 1e9;
    return priceInSOL * totalSupply;
  }

  async getFullPriceData(mint, solPriceUSD = 150) {
    const bondingCurveData = await this.getBondingCurveData(mint);
    if (!bondingCurveData) return null;

    const priceInSOL = this.calculatePriceInSOL(bondingCurveData);
    const priceInUSD = priceInSOL * solPriceUSD;
    const marketCapSOL = this.calculateMarketCapSOL(bondingCurveData);
    const marketCapUSD = marketCapSOL * solPriceUSD;

    return {
      mint,
      priceInSOL,
      priceInUSD,
      marketCapSOL,
      marketCapUSD,
      virtualSolReserves: bondingCurveData.virtualSolReserves,
      virtualTokenReserves: bondingCurveData.virtualTokenReserves,
      realSolReserves: bondingCurveData.realSolReserves,
      realTokenReserves: bondingCurveData.realTokenReserves,
      complete: bondingCurveData.complete,
      liquiditySOL: Number(bondingCurveData.realSolReserves) / 1e9
    };
  }
}

// ============================================================================
// HYBRID PRICE FETCHER
// ============================================================================
class HybridPriceFetcher {
  constructor(connection, solPriceUSD = 150) {
    this.calculator = new PumpPriceCalculator(connection);
    this.solPriceUSD = solPriceUSD;
    this.axios = require('axios');
  }

  async getPrice(mint) {
    try {
      const priceData = await this.calculator.getFullPriceData(mint, this.solPriceUSD);
      if (priceData && priceData.priceInUSD > 0) {
        return {
          source: 'bonding_curve',
          priceUSD: priceData.priceInUSD,
          priceSOL: priceData.priceInSOL,
          marketCapUSD: priceData.marketCapUSD,
          liquidityUSD: priceData.liquiditySOL * this.solPriceUSD,
          complete: priceData.complete
        };
      }
    } catch (error) {
      console.debug(`Bonding curve fetch failed: ${error.message}`);
    }

    try {
      const response = await this.axios.get(
        `https://api.dexscreener.com/latest/dex/tokens/${mint}`,
        { timeout: 5000 }
      );
      if (response.data.pairs && response.data.pairs.length > 0) {
        const pair = response.data.pairs[0];
        return {
          source: 'dexscreener',
          priceUSD: parseFloat(pair.priceUsd || 0),
          priceSOL: parseFloat(pair.priceUsd || 0) / this.solPriceUSD,
          marketCapUSD: parseFloat(pair.marketCap || pair.fdv || 0),
          liquidityUSD: parseFloat(pair.liquidity?.usd || 0),
          complete: true
        };
      }
    } catch (error) {
      console.debug(`DexScreener fetch failed: ${error.message}`);
    }

    return null;
  }

  async simulateBuy(mint, solAmount) {
    const bondingCurveData = await this.calculator.getBondingCurveData(mint);
    if (!bondingCurveData) return null;
    const tokensOut = this.calculator.calculateTokensOut(bondingCurveData, solAmount);
    const pricePerToken = solAmount / tokensOut;
    const currentPrice = this.calculator.calculatePriceInSOL(bondingCurveData);
    const priceImpact = ((pricePerToken - currentPrice) / currentPrice) * 100;

    return {
      solIn: solAmount,
      tokensOut,
      pricePerToken,
      currentPrice,
      priceImpact: priceImpact.toFixed(2) + '%'
    };
  }

  async simulateSell(mint, tokenAmount) {
    const bondingCurveData = await this.calculator.getBondingCurveData(mint);
    if (!bondingCurveData) return null;
    const solOut = this.calculator.calculateSOLOut(bondingCurveData, tokenAmount);
    const pricePerToken = solOut / tokenAmount;
    const currentPrice = this.calculator.calculatePriceInSOL(bondingCurveData);
    const priceImpact = ((currentPrice - pricePerToken) / currentPrice) * 100;

    return {
      tokensIn: tokenAmount,
      solOut,
      pricePerToken,
      currentPrice,
      priceImpact: priceImpact.toFixed(2) + '%'
    };
  }
}

// ============================================================================
// EXPORTS
// ============================================================================
module.exports = {
  PumpPriceCalculator,
  HybridPriceFetcher,
  PUMP_PROGRAM_ID,
  PUMP_GLOBAL_ACCOUNT
};
