// pump-api.js - Trading real con la API de Pump.fun (ACTUALIZADO)
const axios = require('axios');
const bs58 = require('bs58');
const { Connection, clusterApiUrl, SystemProgram, Transaction, PublicKey, sendAndConfirmTransaction } = require('@solana/web3.js');
const { loadWalletKeypair } = require('./wallet-loader');

class PumpFunTrading {
  constructor(config) {
    this.config = config;
    this.connection = new Connection(
      config.RPC_URL || config.HELIUS_RPC_URL || clusterApiUrl('mainnet-beta'),
      'confirmed'
    );
    
    // Cargar wallet usando el nuevo loader
    try {
      this.payer = loadWalletKeypair();
      console.log(`✅ Trading wallet cargada: ${this.payer.publicKey.toString()}`);
    } catch (error) {
      console.error('❌ No se pudo cargar wallet para trading:', error.message);
      this.payer = null;
    }
  }

  async buy(mint, amountSol) {
    if (!this.payer) {
      throw new Error('Wallet no cargada - no se puede ejecutar trading real');
    }

    const url = "https://pumpapi.fun/api/trade";
    const data = {
      trade_type: "buy",
      mint,
      amount: amountSol,
      slippage: this.config.SLIPPAGE_PERCENT || 5,
      priorityFee: this.config.PRIORITY_FEE_BASE || 0.0003,
      userPrivateKey: bs58.encode(this.payer.secretKey)
    };

    try {
      console.log(`🛒 Ejecutando BUY: ${amountSol} SOL en ${mint.slice(0, 8)}`);
      const response = await axios.post(url, data, { timeout: 30000 });
      
      if (response.data.tx_hash) {
        console.log(`✅ BUY exitoso: ${response.data.tx_hash}`);
        return response.data.tx_hash;
      } else {
        console.error('❌ BUY falló:', response.data);
        return null;
      }
    } catch (error) {
      console.error('❌ Error en BUY:', error.response?.data || error.message);
      return null;
    }
  }

  async sell(mint, amountTokens) {
    if (!this.payer) {
      throw new Error('Wallet no cargada - no se puede ejecutar trading real');
    }

    const url = "https://pumpapi.fun/api/trade";
    const data = {
      trade_type: "sell",
      mint,
      amount: amountTokens.toString(),
      slippage: this.config.SLIPPAGE_PERCENT || 5,
      priorityFee: this.config.PRIORITY_FEE_BASE || 0.0003,
      userPrivateKey: bs58.encode(this.payer.secretKey)
    };

    try {
      console.log(`💰 Ejecutando SELL: ${amountTokens} tokens de ${mint.slice(0, 8)}`);
      const response = await axios.post(url, data, { timeout: 30000 });
      
      if (response.data.tx_hash) {
        console.log(`✅ SELL exitoso: ${response.data.tx_hash}`);
        return response.data.tx_hash;
      } else {
        console.error('❌ SELL falló:', response.data);
        return null;
      }
    } catch (error) {
      console.error('❌ Error en SELL:', error.response?.data || error.message);
      return null;
    }
  }

  async checkBalance() {
    if (!this.payer) {
      console.warn('⚠️ Wallet no cargada, no se puede consultar balance');
      return 0;
    }
    
    try {
      const balance = await this.connection.getBalance(this.payer.publicKey);
      const balanceSOL = balance / 1e9;
      console.log(`💰 Balance: ${balanceSOL} SOL`);
      return balanceSOL;
    } catch (error) {
      console.error('❌ Error consultando balance:', error.message);
      return 0;
    }
  }

  async sendDeveloperFee(amountSOL = 0.05) {
    if (!this.payer || !this.config.DEVELOPER_ADDRESS) {
      console.warn('⚠️ No se puede enviar fee: wallet o dirección no configurada');
      return null;
    }

    try {
      const transaction = new Transaction().add(
        SystemProgram.transfer({
          fromPubkey: this.payer.publicKey,
          toPubkey: new PublicKey(this.config.DEVELOPER_ADDRESS),
          lamports: amountSOL * 1e9
        })
      );

      const signature = await sendAndConfirmTransaction(
        this.connection, 
        transaction, 
        [this.payer]
      );
      
      console.log(`💸 Fee desarrollador enviado: ${signature}`);
      return signature;
    } catch (error) {
      console.error('❌ Error enviando fee desarrollador:', error.message);
      return null;
    }
  }

  getPublicKey() {
    return this.payer ? this.payer.publicKey.toString() : null;
  }

  isWalletLoaded() {
    return this.payer !== null;
  }
}

module.exports = PumpFunTrading;
