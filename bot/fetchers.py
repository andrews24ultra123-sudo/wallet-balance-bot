"""Read-only balance lookups against free public APIs.

Each (asset, chain) pair has a primary and a fallback endpoint. All amounts are
returned as Decimal in the asset's main unit (BTC, ETH, SOL, XRP, LTC, TRX,
USDC, USDT), never floats. No API keys are used anywhere.
"""

import hashlib
from decimal import Decimal

import requests

TIMEOUT = 15
HEADERS = {"User-Agent": "wallet-balance-bot/1.0"}

# XRPL reserves (XRP): the base reserve plus per-object owner reserve cannot
# be spent, so the spendable balance excludes them. Values current as of the
# Dec 2024 reserve reduction; adjust here if the XRPL reserve changes again.
XRP_BASE_RESERVE = Decimal("1")
XRP_OWNER_RESERVE = Decimal("0.2")


class FetchError(Exception):
    """All endpoints for an asset failed."""


def _get_json(url):
    resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def _post_json(url, payload):
    resp = requests.post(url, json=payload, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def _utxo_chain_balance(base_url, address, divisor):
    """mempool.space-style address API (used by BTC and LTC explorers)."""
    data = _get_json(f"{base_url}/address/{address}")
    stats = data["chain_stats"]
    base_units = stats["funded_txo_sum"] - stats["spent_txo_sum"]
    return Decimal(base_units) / divisor


def _btc_mempool_space(address):
    return _utxo_chain_balance("https://mempool.space/api", address, Decimal(10**8))


def _btc_blockstream(address):
    return _utxo_chain_balance("https://blockstream.info/api", address, Decimal(10**8))


def _ltc_litecoinspace(address):
    return _utxo_chain_balance("https://litecoinspace.org/api", address, Decimal(10**8))


def _ltc_blockcypher(address):
    data = _get_json(f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance")
    return Decimal(data["balance"]) / Decimal(10**8)


def _eth_rpc(url, address):
    data = _post_json(url, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [address, "latest"],
    })
    if "result" not in data:
        raise FetchError(f"RPC error from {url}: {data.get('error')}")
    return Decimal(int(data["result"], 16)) / Decimal(10**18)


def _eth_publicnode(address):
    return _eth_rpc("https://ethereum-rpc.publicnode.com", address)


def _eth_drpc(address):
    return _eth_rpc("https://eth.drpc.org", address)


def _sol_rpc(url, address):
    data = _post_json(url, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getBalance",
        "params": [address],
    })
    if "result" not in data:
        raise FetchError(f"RPC error from {url}: {data.get('error')}")
    return Decimal(data["result"]["value"]) / Decimal(10**9)


def _sol_mainnet(address):
    return _sol_rpc("https://api.mainnet-beta.solana.com", address)


def _sol_publicnode(address):
    return _sol_rpc("https://solana-rpc.publicnode.com", address)


def _xrp_rpc(url, address):
    data = _post_json(url, {
        "method": "account_info",
        "params": [{"account": address, "ledger_index": "validated"}],
    })
    result = data.get("result", {})
    if result.get("status") != "success":
        raise FetchError(f"XRPL error from {url}: {result.get('error_message') or result}")
    account = result["account_data"]
    total = Decimal(account["Balance"]) / Decimal(10**6)
    reserve = XRP_BASE_RESERVE + XRP_OWNER_RESERVE * Decimal(account.get("OwnerCount", 0))
    spendable = total - reserve
    return spendable if spendable > 0 else Decimal(0)


def _xrp_cluster(address):
    return _xrp_rpc("https://xrplcluster.com", address)


def _xrp_ripple_s1(address):
    return _xrp_rpc("https://s1.ripple.com:51234", address)


# ERC-20 token contracts on Ethereum mainnet
_ERC20_CONTRACTS = {
    "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    "USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
}

# balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"


def _erc20_rpc(rpc_url, token_contract, wallet_address, decimals):
    padded = wallet_address[2:].lower().zfill(64)
    data = _BALANCE_OF_SELECTOR + padded
    result = _post_json(rpc_url, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": token_contract, "data": data}, "latest"],
    })
    raw = result.get("result", "0x")
    if not raw or raw == "0x":
        raise FetchError(f"ERC-20 balanceOf returned empty from {rpc_url}")
    return Decimal(int(raw, 16)) / Decimal(10 ** decimals)


def _make_erc20_fetchers(symbol):
    contract, decimals = _ERC20_CONTRACTS[symbol]

    def _primary(address):
        return _erc20_rpc("https://ethereum-rpc.publicnode.com", contract, address, decimals)

    def _fallback(address):
        return _erc20_rpc("https://eth.drpc.org", contract, address, decimals)

    _primary.__name__ = f"_{symbol.lower()}_publicnode"
    _fallback.__name__ = f"_{symbol.lower()}_drpc"
    return [_primary, _fallback]


# ---------------------------------------------------------------------------
# Tron (native TRX and TRC-20 tokens), via the Tron full-node HTTP API.
# TronGrid is primary, PublicNode the fallback; both answer without an API key.

TRON_ENDPOINTS = ("https://api.trongrid.io", "https://tron-rpc.publicnode.com")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def tron_address_bytes(address):
    """Decode a Tron base58check address to its 21 raw bytes (0x41 + account id).

    Raises ValueError if it is not a valid Tron mainnet address (bad character,
    wrong length, failed checksum or wrong prefix), so a typo is caught.
    """
    num = 0
    for char in address:
        index = _B58_ALPHABET.find(char)
        if index < 0:
            raise ValueError(f"'{char}' is not a base58 character")
        num = num * 58 + index
    try:
        raw = num.to_bytes(25, "big")
    except OverflowError:
        raise ValueError("too long for a Tron address")
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("checksum does not match (likely a typo)")
    if payload[0] != 0x41:
        raise ValueError("not a Tron mainnet address (should start with T)")
    return payload


def _tron_account(base_url, address):
    data = _post_json(f"{base_url}/wallet/getaccount", {"address": address, "visible": True})
    if not data:
        # An unknown or never-activated account comes back as {}. Treat it as a
        # failed read, not a zero balance, so a wrong address is visible.
        raise FetchError(f"Tron account not found from {base_url} (not activated, or wrong address)")
    return data


def _trx_rpc(base_url, address):
    """Liquid (spendable) TRX. Staked TRX sits in frozenV2 and is excluded."""
    data = _tron_account(base_url, address)
    # Tron omits zero-valued fields, so an activated account with no TRX has no 'balance'.
    sun = int(data.get("balance", 0))
    if sun < 0:
        raise FetchError(f"Negative TRX balance from {base_url}: {sun}")
    return Decimal(sun) / Decimal(10**6)


def _trx_trongrid(address):
    return _trx_rpc(TRON_ENDPOINTS[0], address)


def _trx_publicnode(address):
    return _trx_rpc(TRON_ENDPOINTS[1], address)


# TRC-20 token contracts on Tron mainnet. These are public token contracts, not
# wallets. sensitive-data-guard:allow
_TRC20_CONTRACTS = {
    "USDT": ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6),
}


def _trc20_rpc(base_url, token_contract, wallet_address, decimals):
    # balanceOf takes the 20-byte account id (without the 0x41 prefix), ABI-padded.
    param = tron_address_bytes(wallet_address)[1:].hex().zfill(64)
    data = _post_json(f"{base_url}/wallet/triggerconstantcontract", {
        "owner_address": wallet_address,
        "contract_address": token_contract,
        "function_selector": "balanceOf(address)",
        "parameter": param,
        "visible": True,
    })
    if not (data.get("result") or {}).get("result"):
        raise FetchError(f"TRC-20 balanceOf failed from {base_url}: {data.get('result')}")
    raw = (data.get("constant_result") or [""])[0]
    if not raw:
        raise FetchError(f"TRC-20 balanceOf returned empty from {base_url}")
    return Decimal(int(raw, 16)) / Decimal(10 ** decimals)


def _make_trc20_fetchers(symbol):
    contract, decimals = _TRC20_CONTRACTS[symbol]

    def _primary(address):
        return _trc20_rpc(TRON_ENDPOINTS[0], contract, address, decimals)

    def _fallback(address):
        return _trc20_rpc(TRON_ENDPOINTS[1], contract, address, decimals)

    _primary.__name__ = f"_{symbol.lower()}_trc20_trongrid"
    _fallback.__name__ = f"_{symbol.lower()}_trc20_publicnode"
    return [_primary, _fallback]


# ---------------------------------------------------------------------------
# Chains. Each wallet is an (asset, chain) pair: the same asset can live on more
# than one chain (USDT on Ethereum and on Tron). Chains are shown and configured
# by their short network code; the full names are accepted as aliases.

CHAIN_ALIASES = {
    "BTC": "BTC", "BITCOIN": "BTC",
    "ETH": "ETH", "ETHEREUM": "ETH",
    "SOL": "SOL", "SOLANA": "SOL",
    "XRP": "XRP", "XRPL": "XRP",
    "LTC": "LTC", "LITECOIN": "LTC",
    "TRX": "TRX", "TRON": "TRX",
}

# The chain an asset is on when the config does not say. USDT and USDC default to
# Ethereum, so configs written before chains existed keep working unchanged.
DEFAULT_CHAIN = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "LTC": "LTC",
    "TRX": "TRX",
    "USDC": "ETH",
    "USDT": "ETH",
}

FETCHERS = {
    ("BTC", "BTC"): [_btc_mempool_space, _btc_blockstream],
    ("ETH", "ETH"): [_eth_publicnode, _eth_drpc],
    ("SOL", "SOL"): [_sol_mainnet, _sol_publicnode],
    ("XRP", "XRP"): [_xrp_cluster, _xrp_ripple_s1],
    ("LTC", "LTC"): [_ltc_litecoinspace, _ltc_blockcypher],
    ("USDC", "ETH"): _make_erc20_fetchers("USDC"),
    ("USDT", "ETH"): _make_erc20_fetchers("USDT"),
    ("TRX", "TRX"): [_trx_trongrid, _trx_publicnode],
    ("USDT", "TRX"): _make_trc20_fetchers("USDT"),
}

SUPPORTED_ASSETS = sorted({asset for asset, _ in FETCHERS})
SUPPORTED_PAIRS = sorted(FETCHERS)


def normalise_chain(chain):
    """Return the chain code for a code or alias ('tron' -> 'TRX'), or None if unknown."""
    return CHAIN_ALIASES.get(str(chain).strip().upper())


def get_balance(asset, address, chain=None):
    """Return the balance as Decimal, trying primary then fallback endpoint.

    chain defaults to the asset's usual chain (USDT -> ETH). Raises FetchError
    if every endpoint fails.
    """
    chain = chain or DEFAULT_CHAIN[asset]
    errors = []
    for fetcher in FETCHERS[(asset, chain)]:
        try:
            return fetcher(address)
        except Exception as exc:  # noqa: BLE001 - record and try the fallback
            errors.append(f"{fetcher.__name__}: {exc}")
    raise FetchError(f"{asset} on {chain} {address}: all endpoints failed: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# Pending / stuck transaction detection (Ethereum and ERC-20)
#
# Every transaction from an Ethereum address consumes the same sequential nonce,
# whether it moves native ETH or an ERC-20 token, so one stuck (unmined)
# transaction blocks every later withdrawal from that address. We detect that by
# comparing the mined nonce ("latest") with the mempool-inclusive nonce
# ("pending"): a persistent, non-advancing gap means the queue is not draining.
# Read-only: this never signs or sends anything.

# The ETH JSON-RPC endpoints the balance fetchers already use, reused here.
ETH_RPC_ENDPOINTS = ("https://ethereum-rpc.publicnode.com", "https://eth.drpc.org")

# Chains whose wallets share an address's nonce queue (Ethereum only). Keyed by
# chain, not asset, so USDT on Tron is never sent to the Ethereum nonce check.
EVM_CHAINS = frozenset({"ETH"})


def _eth_nonce(url, address, block_tag):
    """Transaction count for an address at a block tag ('latest' or 'pending')."""
    data = _post_json(url, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getTransactionCount",
        "params": [address, block_tag],
    })
    if "result" not in data:
        raise FetchError(f"RPC error from {url}: {data.get('error')}")
    return int(data["result"], 16)


def get_eth_nonce_pair(address, url):
    """(latest, pending) nonce from one endpoint, so the gap is internally consistent."""
    latest = _eth_nonce(url, address, "latest")
    pending = _eth_nonce(url, address, "pending")
    return latest, pending


def get_eth_pending_gap(address, endpoints=ETH_RPC_ENDPOINTS):
    """Probe every endpoint and return the most pessimistic view of the queue.

    Returns a dict: latest (max mined nonce seen), gap (max pending backlog seen,
    floored at 0), pending (latest + gap), endpoints_ok, endpoints_total. Taking
    the max gap guards against a single node with an incomplete mempool hiding a
    stuck transaction. Raises FetchError only if every endpoint fails.
    """
    best_latest = None
    best_gap = 0
    ok = 0
    errors = []
    for url in endpoints:
        try:
            latest, pending = get_eth_nonce_pair(address, url)
        except Exception as exc:  # noqa: BLE001 - record and try the next endpoint
            errors.append(f"{url}: {exc}")
            continue
        ok += 1
        gap = pending - latest
        if gap < 0:
            gap = 0
        if best_latest is None or latest > best_latest:
            best_latest = latest
        if gap > best_gap:
            best_gap = gap
    if ok == 0:
        raise FetchError(f"nonce {address}: all endpoints failed: {'; '.join(errors)}")
    return {
        "latest": best_latest,
        "pending": best_latest + best_gap,
        "gap": best_gap,
        "endpoints_ok": ok,
        "endpoints_total": len(endpoints),
    }
