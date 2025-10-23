// pump-sdk-integration.js - Integración con SDK oficial
const { Connection, PublicKey, Keypair } = require('@solana/web3.js');
const { PumpSdk, getBuyTokenAmountFromSolAmount, getSellSolAmountFromTokenAmount } = require('@pump-fun/pump-sdk');
const { loadWalletKeypair } = require('./wallet-loader');

class PumpSDKIntegration {
  constructor(config) {
    this.config = config;
    this.connection = new Connection(
      config.RPC_URL || config.HELIUS_RPC_URL,
      'confirmed'
    );
    
    // Inicializar SDK oficial
    this.sdk = new PumpSdk(this.connection);
    
    // Cargar wallet
    try {
      this.wallet = loadWalletKeypair();
      console.log(`✅ Wallet cargada para SDK: ${this.wallet.publicKey.toString()}`);
    } catch (error) {
      console.warn('⚠️ No se pudo cargar wallet:', error.message);
      this.wallet = null;
    }
    
    // Cache para global account
    this.globalAccount = null;
    this.lastGlobalUpdate = 0;
  }

  /**
   * Obtener cuenta global (cachear por 60 segundos)
   */
  async fetchGlobal() {
    const now = Date.now();
    if (this.globalAccount && (now - this.lastGlobalUpdate) < 60000) {
      return this.globalAccount;
    }
    
    this.globalAccount = await this.sdk.fetchGlobal();
    this.lastGlobalUpdate = now;
    return this.globalAccount;
  }

  /**
   * Obtener datos completos de un token usando SDK
   */
  async getTokenData(mintAddress) {
    try {
      const mint = new PublicKey(mintAddress);
      const global = await this.fetchGlobal();
      
      // Obtener estado de bonding curve
      const { bondingCurve, bondingCurveAccountInfo } = await this.sdk.fetchBuyState(
        mint,
        this.wallet ? this.wallet.publicKey : PublicKey.default
      );
      
      if (!bondingCurve || !bondingCurveAccountInfo) {
        return null;
      }

      // Calcular datos del token
      const virtualSolReserves = bondingCurve.virtualSolReserves.toNumber();
      const virtualTokenReserves = bondingCurve.virtualTokenReserves.toNumber();
      const realSolReserves = bondingCurve.realSolReserves.toNumber();
      const realTokenReserves = bondingCurve.realTokenReserves.toNumber();
      const tokenTotalSupply = bondingCurve.tokenTotalSupply.toNumber();
      
      // Precio en SOL
      const priceInSol = virtualSolReserves / virtualTokenReserves;
      const priceInUsd = priceInSol * (this.config.SOL_PRICE_USD || 150);
      
      // Market Cap
      const supply = tokenTotalSupply / 1e6; // Ajustar decimales
      const marketCapUsd = priceInUsd * supply;
      
      // Liquidez
      const liquiditySol = realSolReserves / 1e9;
      const liquidityUsd = liquiditySol * (this.config.SOL_PRICE_USD || 150);
      
      // Bonding Curve Progress
      const bondingCurveProgress = Math.min(100, (liquiditySol / 85) * 100);
      
      return {
        mint: mintAddress,
        price: priceInUsd,
        priceInSol,
        marketCap: marketCapUsd,
        liquidity: liquidityUsd,
        liquiditySol,
        bondingCurve: Math.round(bondingCurveProgress),
        supply,
        virtualSolReserves: virtualSolReserves / 1e9,
        virtualTokenReserves: virtualTokenReserves / 1e6,
        realSolReserves: liquiditySol,
        realTokenReserves: realTokenReserves / 1e6,
        complete: bondingCurve.complete,
        creator: bondingCurve.creator?.toString(),
        source: 'pump_sdk_official'
      };
      
    } catch (error) {
      console.error(`Error obteniendo datos SDK para ${mintAddress.slice(0, 8)}:`, error.message);
      return null;
    }
  }

  /**
   * Comprar tokens usando SDK oficial
   */
  async buy(mintAddress, solAmount) {
    if (!this.wallet) {
      throw new Error('Wallet no cargada');
    }

    try {
      const mint = new PublicKey(mintAddress);
      const user = this.wallet.publicKey;
      const global = await this.fetchGlobal();
      
      // Obtener estado actual
      const { bondingCurveAccountInfo, bondingCurve, associatedUserAccountInfo } = 
        await this.sdk.fetchBuyState(mint, user);
      
      // Calcular cantidad de tokens
      const solAmountLamports = Math.floor(solAmount * 1e9);
      const tokenAmount = getBuyTokenAmountFromSolAmount(
        global,
        bondingCurve,
        solAmountLamports
      );
      
      // Crear instrucciones
      const slippageBps = this.config.SLIPPAGE_BPS || 100;
      const instructions = await this.sdk.buyInstructions({
        global,
        bondingCurveAccountInfo,
        bondingCurve,
        associatedUserAccountInfo,
        mint,
        user,
        solAmount: solAmountLamports,
        amount: tokenAmount,
        slippage: slippageBps / 100 // Convertir bps a porcentaje
      });
      
      // Agregar priority fee
      const priorityFee = Math.floor((this.config.PRIORITY_FEE_BASE || 0.0003) * 1e9);
      if (priorityFee > 0) {
        instructions.unshift(
          ComputeBudgetProgram.setComputeUnitPrice({
            microLamports: priorityFee
          })
        );
      }
      
      // Enviar transacción
      const { blockhash } = await this.connection.getLatestBlockhash();
      const transaction = new Transaction({
        recentBlockhash: blockhash,
        feePayer: user
      }).add(...instructions);
      
      transaction.sign(this.wallet);
      
      const signature = await this.connection.sendRawTransaction(
        transaction.serialize(),
        { skipPreflight: false }
      );
      
      console.log(`🚀 Compra enviada: ${signature}`);
      
      // Esperar confirmación
      await this.connection.confirmTransaction(signature, 'confirmed');
      
      console.log(`✅ Compra confirmada: ${signature}`);
      
      return signature;
      
    } catch (error) {
      console.error(`❌ Error en compra SDK:`, error.message);
      return null;
    }
  }

  /**
   * Vender tokens usando SDK oficial
   */
  async sell(mintAddress, tokenAmount) {
    if (!this.wallet) {
      throw new Error('Wallet no cargada');
    }

    try {
      const mint = new PublicKey(mintAddress);
      const user = this.wallet.publicKey;
      const global = await this.fetchGlobal();
      
      // Obtener estado actual
      const { bondingCurveAccountInfo, bondingCurve } = 
        await this.sdk.fetchSellState(mint, user);
      
      // Calcular SOL a recibir
      const tokenAmountRaw = Math.floor(tokenAmount * 1e6);
      const solAmount = getSellSolAmountFromTokenAmount(
        global,
        bondingCurve,
        tokenAmountRaw
      );
      
      // Crear instrucciones
      const slippageBps = this.config.SLIPPAGE_BPS || 50;
      const instructions = await this.sdk.sellInstructions({
        global,
        bondingCurveAccountInfo,
        bondingCurve,
        mint,
        user,
        amount: tokenAmountRaw,
        solAmount,
        slippage: slippageBps / 100
      });
      
      // Agregar priority fee
      const priorityFee = Math.floor((this.config.PRIORITY_FEE_BASE || 0.0003) * 1e9);
      if (priorityFee > 0) {
        instructions.unshift(
          ComputeBudgetProgram.setComputeUnitPrice({
            microLamports: priorityFee
          })
        );
      }
      
      // Enviar transacción
      const { blockhash } = await this.connection.getLatestBlockhash();
      const transaction = new Transaction({
        recentBlockhash: blockhash,
        feePayer: user
      }).add(...instructions);
      
      transaction.sign(this.wallet);
      
      const signature = await this.connection.sendRawTransaction(
        transaction.serialize(),
        { skipPreflight: false }
      );
      
      console.log(`💰 Venta enviada: ${signature}`);
      
      // Esperar confirmación
      await this.connection.confirmTransaction(signature, 'confirmed');
      
      console.log(`✅ Venta confirmada: ${signature}`);
      
      return signature;
      
    } catch (error) {
      console.error(`❌ Error en venta SDK:`, error.message);
      return null;
    }
  }

  /**
   * Crear y comprar en una sola transacción
   */
  async createAndBuy(params) {
    if (!this.wallet) {
      throw new Error('Wallet no cargada');
    }

    try {
      const { mint, name, symbol, uri, solAmount } = params;
      const user = this.wallet.publicKey;
      const global = await this.fetchGlobal();
      
      const solAmountLamports = Math.floor(solAmount * 1e9);
      const tokenAmount = getBuyTokenAmountFromSolAmount(
        global,
        null,
        solAmountLamports
      );
      
      const instructions = await this.sdk.createAndBuyInstructions({
        global,
        mint,
        name,
        symbol,
        uri,
        creator: user,
        user,
        solAmount: solAmountLamports,
        amount: tokenAmount
      });
      
      // Enviar transacción
      const { blockhash } = await this.connection.getLatestBlockhash();
      const transaction = new Transaction({
        recentBlockhash: blockhash,
        feePayer: user
      }).add(...instructions);
      
      transaction.sign(this.wallet);
      
      const signature = await this.connection.sendRawTransaction(
        transaction.serialize()
      );
      
      await this.connection.confirmTransaction(signature, 'confirmed');
      
      return signature;
      
    } catch (error) {
      console.error(`❌ Error en createAndBuy:`, error.message);
      return null;
    }
  }

  /**
   * Verificar balance
   */
  async checkBalance() {
    if (!this.wallet) return 0;
    
    try {
      const balance = await this.connection.getBalance(this.wallet.publicKey);
      return balance / 1e9;
    } catch (error) {
      console.error('Error obteniendo balance:', error.message);
      return 0;
    }
  }

  /**
   * Obtener fees acumulados como creador
   */
  async getCreatorFees() {
    if (!this.wallet) return 0;
    
    try {
      const fees = await this.sdk.getCreatorVaultBalanceBothPrograms(
        this.wallet.publicKey
      );
      return fees.toNumber() / 1e9;
    } catch (error) {
      console.error('Error obteniendo creator fees:', error.message);
      return 0;
    }
  }

  /**
   * Recolectar fees como creador
   */
  async collectCreatorFees() {
    if (!this.wallet) {
      throw new Error('Wallet no cargada');
    }

    try {
      const instructions = await this.sdk.collectCoinCreatorFeeInstructions(
        this.wallet.publicKey
      );
      
      const { blockhash } = await this.connection.getLatestBlockhash();
      const transaction = new Transaction({
        recentBlockhash: blockhash,
        feePayer: this.wallet.publicKey
      }).add(...instructions);
      
      transaction.sign(this.wallet);
      
      const signature = await this.connection.sendRawTransaction(
        transaction.serialize()
      );
      
      await this.connection.confirmTransaction(signature, 'confirmed');
      
      return signature;
      
    } catch (error) {
      console.error('Error recolectando fees:', error.message);
      return null;
    }
  }
}

// Necesitamos importar para priority fees
const { Transaction, ComputeBudgetProgram } = require('@solana/web3.js');

module.exports = PumpSDKIntegration;
