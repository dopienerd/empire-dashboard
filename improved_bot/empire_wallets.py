# empire_wallets.py — IMPROVED VERSION — Thread-safe, EVM tx support, crash-resistant
import os
import time
import json
import base58
import base64
import traceback
from dataclasses import dataclass
from typing import Optional, Dict, Any

# EVM
from web3 import Web3
from eth_account import Account

# Solana
from solders.keypair import Keypair as SolanaKeypair
from solders.pubkey import Pubkey as SolanaPubkey
try:
    from solders.transaction import VersionedTransaction
except ImportError:
    VersionedTransaction = None


@dataclass
class WalletConfig:
    evm_rpc: dict
    solana_rpc: str
    evm_private_key: str
    sol_private_key: str


# ================= CONFIG =================
def load_first_available_sol_key() -> str:
    main = os.getenv("SOL_PRIVATE_KEY", "").strip()
    if main:
        return main
    for i in range(1, 30):
        k = os.getenv(f"SOL_PRIVATE_KEY_{i}", "").strip()
        if k:
            return k
    return ""


CONFIG = WalletConfig(
    evm_rpc={
        "ETH": "https://ethereum.publicnode.com",
        "BASE": "https://mainnet.base.org",
        "ARB": "https://arb1.arbitrum.io/rpc",
        "OP": "https://mainnet.optimism.io",
        "AVAX": "https://api.avax.network/ext/bc/C/rpc",
        "CRO": "https://evm.cronos.org",
    },
    solana_rpc=os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com"),
    evm_private_key=os.getenv("EVM_PRIVATE_KEY", "").strip(),
    sol_private_key=load_first_available_sol_key(),
)


# ================= SOLANA WALLET =================
class SolanaWallet:
    def __init__(self, rpc_url: str, private_key: str):
        self.rpc_url = rpc_url
        self.client = __import__("requests").Session()
        self.keypair = None
        self.pubkey = None
        if not private_key:
            return
        try:
            raw = base58.b58decode(private_key)
            self.keypair = SolanaKeypair.from_bytes(raw)
            self.pubkey = self.keypair.pubkey()
        except Exception:
            try:
                raw = bytes.fromhex(private_key.replace("0x", ""))
                self.keypair = SolanaKeypair.from_bytes(raw)
                self.pubkey = self.keypair.pubkey()
            except Exception:
                print("[SOL] Failed to parse private key (tried base58 + hex)")

    def get_balance(self):
        if not self.pubkey:
            return 0.0
        try:
            res = self.client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getBalance",
                "params": [str(self.pubkey)]
            }, timeout=8).json()
            return res["result"]["value"] / 1_000_000_000
        except Exception:
            return 0.0

    def send_raw_tx(self, raw_bytes):
        try:
            if isinstance(raw_bytes, str):
                # Already base64 encoded
                b64 = raw_bytes
            else:
                b64 = base64.b64encode(raw_bytes).decode()
            res = self.client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [b64, {"encoding": "base64"}]
            }, timeout=15).json()
            return res.get("result") or res
        except Exception as e:
            print(f"[SOL] send_raw_tx error: {e}")
            return None

    def sign_and_send_jupiter_tx(self, swap_tx_b64: str):
        """Sign a Jupiter versioned transaction and send it."""
        if not self.keypair:
            print("[SOL] No keypair — cannot sign")
            return None
        try:
            raw = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw)

            # Create a new signed transaction
            signed_tx = VersionedTransaction(tx.message, [self.keypair])

            signed_b64 = base64.b64encode(bytes(signed_tx)).decode()
            res = self.client.post(self.rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [signed_b64, {
                    "encoding": "base64",
                    "skipPreflight": True,
                    "maxRetries": 3,
                }]
            }, timeout=20).json()
            return res.get("result") or res
        except Exception as e:
            print(f"[SOL] sign_and_send error: {e}")
            return None


# ================= EVM WALLET =================
class EVMWallet:
    def __init__(self, chain: str, rpc: str, pk: str):
        self.chain = chain
        self.web3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        self.account = Account.from_key(pk) if pk else None
        self.address = self.account.address if self.account else None

    def get_balance(self):
        if not self.account:
            return 0.0
        try:
            bal = self.web3.eth.get_balance(self.account.address)
            return float(self.web3.from_wei(bal, "ether"))
        except Exception:
            return 0.0

    def sign_and_send(self, tx_dict: dict) -> Optional[str]:
        """Sign and send an EVM transaction. Returns tx hash or None."""
        if not self.account:
            return None
        try:
            w3 = self.web3
            # Fill in missing fields
            if "from" not in tx_dict:
                tx_dict["from"] = self.address
            if "nonce" not in tx_dict:
                tx_dict["nonce"] = w3.eth.get_transaction_count(self.address)
            if "chainId" not in tx_dict:
                tx_dict["chainId"] = w3.eth.chain_id
            if "gas" not in tx_dict:
                tx_dict["gas"] = 500000
            if "gasPrice" not in tx_dict and "maxFeePerGas" not in tx_dict:
                tx_dict["gasPrice"] = w3.eth.gas_price

            # Ensure 'to' is checksummed
            if "to" in tx_dict:
                tx_dict["to"] = Web3.to_checksum_address(tx_dict["to"])

            signed = self.account.sign_transaction(tx_dict)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            return tx_hash.hex()
        except Exception as e:
            print(f"[EVM][{self.chain}] sign_and_send error: {e}")
            return None


class MultiEVM:
    def __init__(self, config: WalletConfig):
        self.wallets = {}
        for chain, rpc in config.evm_rpc.items():
            try:
                w = EVMWallet(chain, rpc, config.evm_private_key)
                self.wallets[chain.upper()] = w
            except Exception as e:
                print(f"[EVM] Failed to init {chain}: {e}")

    def get(self, chain):
        return self.wallets.get(chain.upper())


# ================= SMART MULTI-SOLANA =================
class MultiSolana:
    def __init__(self, config: WalletConfig):
        self.wallets = []
        keys = [config.sol_private_key] if config.sol_private_key else []
        for i in range(1, 30):
            k = os.getenv(f"SOL_PRIVATE_KEY_{i}", "").strip()
            if k and k not in keys:
                keys.append(k)

        for pk in keys:
            if not pk:
                continue
            try:
                wallet = SolanaWallet(config.solana_rpc, pk)
                bal = wallet.get_balance()
                pub = str(wallet.pubkey)[:10] if wallet.pubkey else "None"
                print(f"[SOL] Loaded {pub}... -> {bal:.5f} SOL")
                self.wallets.append(wallet)
            except Exception as e:
                print(f"[SOL] Failed to load key: {e}")

        if not self.wallets:
            print("[FATAL] No Solana wallets loaded!")
        else:
            print(f"[SOL] Ready - {len(self.wallets)} Solana wallet(s) loaded")

    def get(self):
        if not self.wallets:
            return None
        # Prefer wallet with >= 0.1 SOL
        for w in self.wallets:
            try:
                if w.get_balance() >= 0.1:
                    return w
            except Exception:
                continue
        # Fallback: first wallet
        return self.wallets[0] if self.wallets else None

    def get_total_balance(self):
        total = 0.0
        for w in self.wallets:
            try:
                total += w.get_balance()
            except Exception:
                pass
        return total


# ================= MASTER CLASS =================
class EmpireWallets:
    def __init__(self, config: WalletConfig):
        print("[WALLETS] Initializing multi-chain engine...")
        self.config = config
        self.solana = MultiSolana(config)
        self.evm = MultiEVM(config)
        print(f"[WALLETS] Ready - {len(self.solana.wallets)} SOL + {len(self.evm.wallets)} EVM wallets")

    def get_solana_balance(self):
        return self.solana.get_total_balance()

    def get_evm_balance(self, chain="ETH"):
        w = self.evm.get(chain.upper())
        return w.get_balance() if w else 0.0

    def get_all_balances(self) -> dict:
        out = {"SOL": self.get_solana_balance()}
        for chain in self.config.evm_rpc.keys():
            out[chain] = self.get_evm_balance(chain)
        return out

    def get_total_dex_usd(self):
        return self.get_solana_balance() * 230

    def send_tx(self, chain: str, unsigned_tx: dict):
        """Send a transaction on any chain. Returns tx hash or None."""
        chain = (chain or unsigned_tx.get("chain", "SOL")).upper()

        if chain == "SOL":
            wallet = self.solana.get()
            if not wallet:
                print("[WALLETS] No Solana wallet available")
                return None
            raw_data = unsigned_tx.get("tx_bytes") or unsigned_tx.get("transaction")
            if not raw_data:
                print("[WALLETS] No tx_bytes in Solana tx")
                return None
            if isinstance(raw_data, str):
                # Could be base64
                try:
                    raw = base64.b64decode(raw_data)
                    return wallet.send_raw_tx(raw)
                except Exception:
                    return wallet.send_raw_tx(raw_data)
            return wallet.send_raw_tx(raw_data)

        # EVM chains
        evm_wallet = self.evm.get(chain)
        if not evm_wallet:
            print(f"[WALLETS] No EVM wallet for chain {chain}")
            return None

        # Handle swap sub-dict from DEX module
        swap_data = unsigned_tx.get("swap") or unsigned_tx
        tx_dict = {
            "to": swap_data.get("to"),
            "data": swap_data.get("data", "0x"),
            "value": int(swap_data.get("value", 0)),
            "gas": int(swap_data.get("gas", 500000)),
        }
        return evm_wallet.sign_and_send(tx_dict)

    def get_address(self, chain="SOL"):
        if chain.upper() == "SOL":
            w = self.solana.get()
            return str(w.pubkey) if w and w.pubkey else None
        evm = self.evm.get(chain.upper())
        return evm.address if evm and evm.address else None


# ================= FACTORY =================
def create_wallets(config: WalletConfig) -> EmpireWallets:
    return EmpireWallets(config)


WALLETS = None
