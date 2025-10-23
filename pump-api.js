// pump-api.js - Trading real con la API de Pump.fun (de tu código)
const axios = require('axios');
const bs58 = require('bs58');
const fs = require('fs');
const { Keypair, Connection, clusterApiUrl } = require('@solana/web3.js');

class PumpFunTrading {
  constructor(config) {
    this.config = config;
    this.connection = new Connection(
      config.RPC_URL || clusterApiUrl('mainnet-beta')
    );
    this.payer = this.loadWallet();
  }

  loadWallet() {
    if (!this.config.SOLANA_WALLET_PATH) {
      console.warn('❌ SOLANA_WALLET_PATH no configurado - Trading real deshabilitado');
      return null;
    }

    try {
      const keypairData = fs.readFileSync(this.config.SOLANA_WALLET_PATH, 'utf8');
      let keypair;
      
      // Manejar diferentes formatos de wallet
      if (keypairData.startsWith('[')) {
        // Array JSON
        keypair = JSON.parse(keypairData);
      } else {
        // Secret key base58
        keypair = bs58.decode(keypairData);
      }
      
      return Keypair.fromSecretKey(Uint8Array.from(keypair));
    } catch (error) {
      console.error('❌ Error cargando wallet:', error.message);
      return null;
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
      slippage: 5,
      priorityFee: this.config.PRIORITY_FEE_BASE,
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
      slippage: 5,
      priorityFee: this.config.PRIORITY_FEE_BASE,
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
    if (!this.payer) return 0;
    
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

  async sendDeveloperFee() {
    if (!this.payer) return null;

    try {
      const transaction = new Transaction().add(
        SystemProgram.transfer({
          fromPubkey: this.payer.publicKey,
          toPubkey: new PublicKey(this.config.DEVELOPER_ADDRESS),
          lamports: 0.05 * 1e9 // 0.05 SOL
        })
      );

      const signature = await sendAndConfirmTransaction(this.connection, transaction, [this.payer]);
      console.log(`💸 Fee desarrollador enviado: ${signature}`);
      return signature;
    } catch (error) {
      console.error('❌ Error enviando fee desarrollador:', error.message);
      return null;
    }
  }
}

module.exports = PumpFunTrading;
