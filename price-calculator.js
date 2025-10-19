// price-calculator.js - Calcula precios directamente desde pump.fun bonding curve
const { Connection, PublicKey } = require('@solana/web3.js');
const { struct, u64, bool } = require('@solana/buffer-layout');
const { publicKey, u64 as layoutU64 } = require('@solana/buffer-layout-utils');

// ============================================================================
// PUMP.FUN CONSTANTS
// ============================================================================

const PUMP_PROGRAM_ID = new PublicKey('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P');
const PUMP_GLOBAL_ACCOUNT = new PublicKey('4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf');

// Bonding Curve Layout (según el IDL)
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

  /**
   * Deriva la dirección de la bonding curve para un mint
   */
  async getBondingCurvePDA(mint) {
    const [bondingCurve] = await PublicKey.findProgramAddress(
      [Buffer.from('bonding-curve'), new PublicKey(mint).toBuffer()],
      PUMP_PROGRAM_ID
    );
    return bondingCurve;
  }

  /**
   * Obtiene los datos de la bonding curve directamente de Solana
   */
  async getBondingCurveData(mint) {
    try {
      const bondingCurvePDA = await this.getBondingCurvePDA(mint);
      const accountInfo = await this.connection.getAccountInfo(bondingCurvePDA);
      
      if (!accountInfo) {
        throw new Error('Bonding curve account not found');
      }

      // Decodificar los datos de la cuenta
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

  /**
   * Calcula el precio actual del token en SOL usando la fórmula del bonding curve
   * Precio = virtualSolReserves / virtualTokenReserves
   */
  calculatePriceInSOL(bondingCurveData) {
    if (!bondingCurveData) return 0;

    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);

    if (virtualTokenReserves === 0n) return 0;

    // Precio en SOL por token
    const priceInSOL = Number(virtualSolReserves) / Number(virtualTokenReserves);
    
    return priceInSOL;
  }

  /**
   * Calcula cuántos tokens recibirías por X cantidad de SOL
   * Usa la fórmula del Automated Market Maker (AMM)
   */
  calculateTokensOut(bondingCurveData, solAmountIn) {
    if (!bondingCurveData) return 0;

    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);
    const solIn = BigInt(Math.floor(solAmountIn * 1e9)); // Convert to lamports

    // Fórmula AMM: tokensOut = (solIn * virtualTokenReserves) / (virtualSolReserves + solIn)
    const numerator = solIn * virtualTokenReserves;
    const denominator = virtualSolReserves + solIn;
    
    if (denominator === 0n) return 0;

    const tokensOut = Number(numerator / denominator) / 1e9; // Convert back to tokens
    
    return tokensOut;
  }

  /**
   * Calcula cuántos SOL recibirías por X cantidad de tokens
   */
  calculateSOLOut(bondingCurveData, tokenAmountIn) {
    if (!bondingCurveData) return 0;

    const virtualSolReserves = BigInt(bondingCurveData.virtualSolReserves);
    const virtualTokenReserves = BigInt(bondingCurveData.virtualTokenReserves);
    const tokensIn = BigInt(Math.floor(tokenAmountIn * 1e9));

    // Fórmula AMM: solOut = (tokensIn * virtualSolReserves) / (virtualTokenReserves + tokensIn)
    const numerator = tokensIn * virtualSolReserves;
    const denominator = virtualTokenReserves + tokensIn;
    
    if (denominator === 0n) return 0;

    const solOut = Number(numerator / denominator) / 1e9;
    
    return solOut;
  }

  /**
   * Calcula el market cap del token en SOL
   */
  calculateMarketCapSOL(bondingCurveData) {
    if (!bondingCurveData) return 0;

    const priceInSOL = this.calculatePriceInSOL(bondingCurveData);
    const totalSupply = Number(bondingCurveData.tokenTotalSupply) / 1e9;
    
    return priceInSOL * totalSupply;
  }

  /**
   * Obtiene precio completo con toda la información
   */
  async getFullPriceData(mint, solPriceUSD = 150) {
    const bondingCurveData = await this.getBondingCurveData(mint);
    
    if (!bondingCurveData) {
      return null;
    }

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
// HYBRID PRICE FETCHER (Fallback strategy)
// ============================================================================

class HybridPriceFetcher {
  constructor(connection, solPriceUSD = 150) {
    this.calculator = new PumpPriceCalculator(connection);
    this.solPriceUSD = solPriceUSD;
    this.axios = require('axios');
  }

  /**
   * Intenta obtener precio desde bonding curve, si falla usa DexScreener
   */
  async getPrice(mint) {
    // Método 1: Directo desde bonding curve (MÁS RÁPIDO Y PRECISO)
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

    // Método 2: Fallback a DexScreener (si el token ya migró a Raydium)
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
          complete: true // Asumimos que ya migró
        };
      }
    } catch (error) {
      console.debug(`DexScreener fetch failed: ${error.message}`);
    }

    return null;
  }

  /**
   * Simula una compra para ver el precio de impacto
   */
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

  /**
   * Simula una venta
   */
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
// EXPORT
// ============================================================================

module.exports = {
  PumpPriceCalculator,
  HybridPriceFetcher,
  PUMP_PROGRAM_ID,
  PUMP_GLOBAL_ACCOUNT
};

// ============================================================================
// EJEMPLO DE USO
// ============================================================================

if (require.main === module) {
  (async () => {
    const { Connection } = require('@solana/web3.js');
    
    const connection = new Connection('https://api.mainnet-beta.solana.com', 'confirmed');
    const fetcher = new HybridPriceFetcher(connection, 150); // SOL @ $150
    
    // Ejemplo con un mint
    const testMint = 'So11111111111111111111111111111111111111112'; // Reemplaza con un mint real
    
    console.log('📊 Obteniendo precio...');
    const priceData = await fetcher.getPrice(testMint);
    
    if (priceData) {
      console.log('\n✅ Datos obtenidos:');
      console.log(`   Fuente: ${priceData.source}`);
      console.log(`   Precio: $${priceData.priceUSD.toFixed(8)}`);
      console.log(`   Market Cap: $${priceData.marketCapUSD.toLocaleString()}`);
      console.log(`   Liquidez: $${priceData.liquidityUSD.toLocaleString()}`);
      
      // Simular una compra
      console.log('\n💰 Simulando compra de 0.1 SOL...');
      const buySimulation = await fetcher.simulateBuy(testMint, 0.1);
      if (buySimulation) {
        console.log(`   Tokens: ${buySimulation.tokensOut.toFixed(2)}`);
        console.log(`   Precio impacto: ${buySimulation.priceImpact}`);
      }
    } else {
      console.log('❌ No se pudo obtener precio');
    }
  })();
}
