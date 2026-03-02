"""
EMPIRE DEX MODULE — IMPROVED VERSION
- Fixed jupiter_quote method/attribute name collision
- Removed duplicate EVM DEX classes (was EvmDEXModule AND EVMDEXModule)
- Removed orphaned standalone build_solana_swap function
- Removed redundant re-attachment code at bottom
- Dynamic SOL pubkey from wallets instead of hardcoded
- Added error resilience
"""

import time
import requests
import base58
import struct
import base64
import json
import uuid
from datetime import datetime
from decimal import Decimal
from web3 import Web3

# Will be set by main.py via set_wallets()
WALLETS = None


def set_wallets(w):
    global WALLETS
    WALLETS = w


# =========================================================================
#  CORE DEX ROUTER
# =========================================================================
class EmpireDEXCore:
    def __init__(self):
        print("[DEX] Initializing Empire DEX Core...")
        self.solana = None
        self.evm = None
        self.sui = None
        self.aptos = None
        self.price_cache = {}
        self.volume_cache = {}

        # Cached SOL price to avoid hammering CoinGecko
        self._sol_price = 230.0
        self._sol_price_ts = 0

    def get_sol_price(self):
        now = time.time()
        # Cache for 60 seconds
        if now - self._sol_price_ts < 60:
            return self._sol_price
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                timeout=5,
            )
            price = float(r.json()["solana"]["usd"])
            self._sol_price = price
            self._sol_price_ts = now
            return price
        except Exception:
            try:
                r = requests.get("https://price.jup.ag/v6/price?ids=SOL", timeout=5)
                price = float(r.json()["data"]["SOL"]["price"])
                self._sol_price = price
                self._sol_price_ts = now
                return price
            except Exception:
                return self._sol_price  # return last known

    # -----------------------------------------------------------------
    #  SCANNER
    # -----------------------------------------------------------------
    def scan_tokens(self):
        result = {
            "SOL": [], "BASE": [], "ARB": [], "OP": [],
            "AVAX": [], "CRO": [], "SUI": [], "APTOS": [],
        }
        try:
            if self.solana and hasattr(self.solana, "scan_solana_tokens"):
                result["SOL"] = self.solana.scan_solana_tokens()
        except Exception:
            pass
        try:
            if self.evm and hasattr(self.evm, "scan_evm_tokens"):
                evm_data = self.evm.scan_evm_tokens()
                for chain, items in evm_data.items():
                    result[chain] = items
        except Exception:
            pass
        try:
            if self.sui and hasattr(self.sui, "scan_sui_tokens"):
                result["SUI"] = self.sui.scan_sui_tokens()
        except Exception:
            pass
        try:
            if self.aptos and hasattr(self.aptos, "scan_aptos_tokens"):
                result["APTOS"] = self.aptos.scan_aptos_tokens()
        except Exception:
            pass
        return result

    # -----------------------------------------------------------------
    #  VOLUME TRACKING
    # -----------------------------------------------------------------
    def update_volume(self, chain, token, volume):
        key = f"{chain}:{token}"
        now = time.time()
        if key not in self.volume_cache:
            self.volume_cache[key] = []
        self.volume_cache[key].append((now, volume))
        self.volume_cache[key] = [
            (ts, vol) for ts, vol in self.volume_cache[key] if now - ts <= 300
        ]

    def compute_volume_score(self, chain, token):
        key = f"{chain}:{token}"
        if key not in self.volume_cache:
            return 0
        vols = [v for _, v in self.volume_cache[key]]
        if not vols:
            return 0
        return sum(vols) / max(len(vols), 1)

    def sentiment_score(self, chain, token):
        return 1.0

    def get_chain_scores(self):
        scanned = self.scan_tokens()
        chain_scores = {}
        for chain, tokens in scanned.items():
            if not tokens:
                chain_scores[chain] = 0
                continue
            rising = 0
            vol_sum = 0
            for t in tokens:
                symbol = t.get("symbol")
                vol = t.get("volume_5m", 0)
                price_change = t.get("change_5m", 0)
                self.update_volume(chain, symbol, vol)
                vol_score = self.compute_volume_score(chain, symbol)
                sent = self.sentiment_score(chain, symbol)
                if price_change > 0:
                    rising += 1
                vol_sum += vol_score * sent
            chain_scores[chain] = rising * 0.5 + vol_sum * 0.5
        return chain_scores

    # -----------------------------------------------------------------
    #  UNIVERSAL SWAP BUILDER
    # -----------------------------------------------------------------
    def build_swap_tx(self, chain, token_symbol, usd_amount):
        chain = chain.upper()
        try:
            if chain == "SOL" and self.solana:
                return self.solana.build_solana_swap(token_symbol, usd_amount)
            if chain in ["BASE", "ARB", "OP", "AVAX", "CRO"] and self.evm:
                return self.evm.build_evm_swap(chain, token_symbol, usd_amount)
            if chain == "SUI" and self.sui:
                return self.sui.build_sui_swap(token_symbol, usd_amount)
            if chain == "APTOS" and self.aptos:
                return self.aptos.build_aptos_swap(token_symbol, usd_amount)
        except Exception as e:
            print(f"[DEX] build_swap_tx error for {chain}: {e}")
        return None


# Singleton
DEX = EmpireDEXCore()


# =========================================================================
#  SOLANA DEX MODULE (Jupiter + Raydium + pump.fun)
# =========================================================================
class SolanaDEXModule:
    def __init__(self, owner_pubkey):
        print("[DEX][SOL] Initializing Solana DEX module...")
        self.owner = owner_pubkey
        self.rpc = "https://api.mainnet-beta.solana.com"
        self.session = requests.Session()
        # Jupiter endpoints
        self._jupiter_quote_url = "https://quote-api.jup.ag/v6/quote"
        self._jupiter_swap_url = "https://quote-api.jup.ag/v6/swap"

    def rpc_call(self, method, params):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = self.session.post(self.rpc, json=body, timeout=10)
        return r.json()

    # -----------------------------------------------------------------
    #  Jupiter quote + swap
    # -----------------------------------------------------------------
    def get_jupiter_quote(self, input_mint, output_mint, amount):
        """Get a Jupiter quote. Returns the quote JSON."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": 300,
        }
        r = self.session.get(self._jupiter_quote_url, params=params, timeout=8)
        return r.json()

    def jupiter_swap_tx(self, input_mint, output_mint, amount):
        """Get Jupiter swap transaction bytes."""
        quote = self.get_jupiter_quote(input_mint, output_mint, amount)
        if "error" in quote or "inputMint" not in quote:
            print(f"[SOL][JUP] Quote error: {quote}")
            return None

        # Use the owner address, falling back to WALLETS
        owner = self.owner
        if not owner and WALLETS:
            owner = WALLETS.get_address("SOL")
        if not owner:
            print("[SOL][JUP] No owner pubkey for swap")
            return None

        data = {
            "quoteResponse": quote,
            "userPublicKey": owner,
            "wrapAndUnwrapSol": True,
        }
        r = self.session.post(self._jupiter_swap_url, json=data, timeout=10)
        js = r.json()

        if "swapTransaction" not in js:
            print(f"[SOL][JUP] No swapTransaction returned: {js}")
            return None

        return js["swapTransaction"]  # base64 encoded

    # -----------------------------------------------------------------
    #  Raydium AMM Swap (v4)
    # -----------------------------------------------------------------
    def raydium_swap(self, pool_keys, input_mint, output_mint, amount_in):
        ix_data = bytes([9]) + struct.pack("<Q", amount_in) + struct.pack("<Q", 0)
        return {
            "chain": "SOL",
            "raydium_ix": {
                "program_id": pool_keys["program_id"],
                "accounts": [
                    pool_keys["amm_id"],
                    pool_keys["amm_authority"],
                    pool_keys["amm_open_orders"],
                    pool_keys["amm_target_orders"],
                    pool_keys["pool_coin_token_account"],
                    pool_keys["pool_pc_token_account"],
                    pool_keys["serum_market"],
                    pool_keys["serum_bids"],
                    pool_keys["serum_asks"],
                    pool_keys["serum_event_queue"],
                    pool_keys["serum_coin_vault"],
                    pool_keys["serum_pc_vault"],
                    pool_keys["serum_vault_signer"],
                    input_mint,
                    output_mint,
                    self.owner,
                    "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                ],
                "data": ix_data,
            },
        }

    # -----------------------------------------------------------------
    #  Pump.fun Sniper
    # -----------------------------------------------------------------
    def pumpfun_get_bonding(self, mint):
        try:
            r = self.session.get(f"https://pump.fun/api/curve/{mint}", timeout=6)
            return r.json()
        except Exception:
            return None

    def pumpfun_build_buy(self, mint, amount_sol):
        bonding = self.pumpfun_get_bonding(mint)
        if not bonding or "error" in bonding:
            return None

        PUMP_PROGRAM = "pumpE2RkAr1dKfCqJr4CemGs6RB1V98Fo6iLQwSEf7b"
        pda = bonding.get("pda")

        ix_data = struct.pack("<B", 0) + struct.pack("<Q", int(amount_sol * 1e9))

        return {
            "chain": "SOL",
            "pump_fun_ix": {
                "program_id": PUMP_PROGRAM,
                "accounts": [self.owner, pda, self.owner, "11111111111111111111111111111111"],
                "data": ix_data,
            },
        }

    # -----------------------------------------------------------------
    #  Build SWAP TX (called by EmpireDEXCore)
    # -----------------------------------------------------------------
    def build_solana_swap(self, mint: str, usd_amount: float):
        """Jupiter swap via quote API."""
        try:
            sol_price = DEX.get_sol_price()
            amount_in = int((usd_amount / sol_price) * 1e9)  # lamports

            SOL_MINT = "So11111111111111111111111111111111111111112"
            tx_b64 = self.jupiter_swap_tx(SOL_MINT, mint, amount_in)
            if not tx_b64:
                return None

            return {"chain": "SOL", "tx_bytes": tx_b64}
        except Exception as e:
            print(f"[SOL] build_solana_swap error: {e}")
            return None

    # -----------------------------------------------------------------
    #  Token Scanner
    # -----------------------------------------------------------------
    def scan_solana_tokens(self):
        try:
            r = self.session.get("https://tokens.jup.ag/tokens", timeout=8)
            items = r.json()
        except Exception:
            return []

        out = []
        for t in items[:200]:
            out.append({
                "symbol": t.get("symbol"),
                "mint": t.get("address"),
                "volume_5m": 1,
                "change_5m": 0,
            })
        return out


# =========================================================================
#  EVM DEX MODULE (Unified — Base, Arb, OP, AVAX, CRO)
# =========================================================================
class EVMDEXModule:
    def __init__(self):
        print("[DEX][EVM] Initializing Universal EVM Sniper")
        self.rpcs = {
            "BASE": "https://mainnet.base.org",
            "ARB": "https://arb1.arbitrum.io/rpc",
            "OP": "https://mainnet.optimism.io",
            "AVAX": "https://api.avax.network/ext/bc/C/rpc",
            "CRO": "https://evm.cronos.org",
        }
        self.providers = {
            chain: Web3(Web3.HTTPProvider(url)) for chain, url in self.rpcs.items()
        }
        self.token_lists = {chain: [] for chain in self.rpcs}

    def build_evm_swap(self, chain: str, token_in: str, usd_amount: float):
        try:
            chain = chain.upper()
            native_prices = {
                "BASE": 3400, "ETH": 3400, "ARB": 3400,
                "OP": 3400, "AVAX": 52, "CRO": 0.15,
            }
            price = native_prices.get(chain, 3400)
            value_wei = int((usd_amount / price) * 1e18)

            # Get recipient address
            recipient = "0x0000000000000000000000000000000000000000"
            if WALLETS:
                addr = WALLETS.get_address(chain)
                if addr:
                    recipient = addr

            calldata = (
                "0x414bf389"
                + "0000000000000000000000000000000000000000".zfill(64)
                + token_in[2:].lower().zfill(64)
                + "00000000000000000000000000000000000000000000000000000000000001f4"
                + recipient[2:].lower().zfill(64)
                + format(value_wei, "x").zfill(64)
                + "0000000000000000000000000000000000000000000000000000000000000001"
                + "0000000000000000000000000000000000000000000000000000000000000000"
            )

            return {
                "chain": chain,
                "swap": {
                    "to": "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
                    "data": "0x" + calldata,
                    "value": value_wei,
                    "gas": 500000,
                },
            }
        except Exception as e:
            print(f"[DEX][EVM] build_evm_swap failed: {e}")
            return None

    def scan_evm_tokens(self):
        out = {}
        for chain in self.rpcs:
            arr = []
            for t in self.token_lists.get(chain, []):
                arr.append({
                    "symbol": t.get("symbol", ""),
                    "address": t.get("address", ""),
                    "volume_5m": 1,
                    "change_5m": 0,
                })
            out[chain] = arr
        return out


# =========================================================================
#  SUI DEX MODULE
# =========================================================================
class SuiDEXModule:
    def __init__(self):
        print("[DEX][SUI] Initializing Sui DEX Module...")
        self.rpc = "https://fullnode.mainnet.sui.io"
        self.session = requests.Session()
        self.CETUS_PACKAGE = "0x34fc..."
        self.tokens = []

    def rpc_call(self, method, params):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        r = self.session.post(self.rpc, json=body, timeout=10)
        return r.json()

    def cetus_swap(self, coin_in, coin_out, amount):
        return {
            "chain": "SUI",
            "module": f"{self.CETUS_PACKAGE}::pool::swap",
            "args": [coin_in, coin_out, str(amount), "0"],
        }

    def build_sui_swap(self, symbol, usd):
        sui_amount = int(usd * 1e9)
        return self.cetus_swap("0x2::sui::SUI", symbol, sui_amount)

    def scan_sui_tokens(self):
        return [
            {"symbol": t.get("symbol", ""), "address": t.get("address", ""),
             "volume_5m": 1, "change_5m": 0}
            for t in self.tokens
        ]


# =========================================================================
#  APTOS DEX MODULE
# =========================================================================
class AptosDEXModule:
    def __init__(self):
        print("[DEX][APTOS] Initializing Aptos DEX Module...")
        self.rpc = "https://fullnode.mainnet.aptoslabs.com/v1"
        self.session = requests.Session()
        self.PANCAKE_ROUTER = "0x1::swap::router"
        self.tokens = []

    def pancake_swap(self, coin_in, coin_out, amount):
        return {
            "chain": "APTOS",
            "entry_func": f"{self.PANCAKE_ROUTER}::swap_exact_input",
            "args": [coin_in, coin_out, str(amount), "0"],
        }

    def build_aptos_swap(self, symbol, usd):
        apt_atoms = int(usd * 1e8 / 7)
        return self.pancake_swap("0x1::aptos_coin::AptosCoin", symbol, apt_atoms)

    def scan_aptos_tokens(self):
        return [
            {"symbol": t.get("symbol", ""), "address": t.get("address", ""),
             "volume_5m": 1, "change_5m": 0}
            for t in self.tokens
        ]


# =========================================================================
#  ATTACH MODULES — Dynamic SOL pubkey, no hardcoding
# =========================================================================
def init_dex_modules():
    """Called by main.py after wallets are created."""
    global WALLETS

    # Solana module
    owner = None
    if WALLETS:
        owner = WALLETS.get_address("SOL")
    if owner:
        DEX.solana = SolanaDEXModule(owner)
        print("[DEX] Solana module loaded")
    else:
        print("[DEX] WARNING: No SOL address — Solana module skipped")

    # EVM module
    DEX.evm = EVMDEXModule()
    print("[DEX] EVM 5-chain module loaded")

    # SUI + Aptos
    DEX.sui = SuiDEXModule()
    DEX.aptos = AptosDEXModule()
    print("[DEX] SUI + APTOS modules loaded")
