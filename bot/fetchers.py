"""Read-only balance lookups against free public APIs.

Each asset has a primary and a fallback endpoint. All amounts are returned
as Decimal in the asset's main unit (BTC, ETH, SOL, XRP, LTC, USDC, USDT),
never floats. No API keys are used anywhere.
"""

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


def _eth_cloudflare(address):
    return _eth_rpc("https://cloudflare-eth.com", address)


def _eth_publicnode(address):
    return _eth_rpc("https://ethereum-rpc.publicnode.com", address)


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
        return _erc20_rpc("https://cloudflare-eth.com", contract, address, decimals)

    def _fallback(address):
        return _erc20_rpc("https://ethereum-rpc.publicnode.com", contract, address, decimals)

    _primary.__name__ = f"_{symbol.lower()}_cloudflare"
    _fallback.__name__ = f"_{symbol.lower()}_publicnode"
    return [_primary, _fallback]


FETCHERS = {
    "BTC": [_btc_mempool_space, _btc_blockstream],
    "ETH": [_eth_cloudflare, _eth_publicnode],
    "SOL": [_sol_mainnet, _sol_publicnode],
    "XRP": [_xrp_cluster, _xrp_ripple_s1],
    "LTC": [_ltc_litecoinspace, _ltc_blockcypher],
    "USDC": _make_erc20_fetchers("USDC"),
    "USDT": _make_erc20_fetchers("USDT"),
}

SUPPORTED_ASSETS = sorted(FETCHERS)


def get_balance(asset, address):
    """Return the balance as Decimal, trying primary then fallback endpoint.

    Raises FetchError if every endpoint fails.
    """
    errors = []
    for fetcher in FETCHERS[asset]:
        try:
            return fetcher(address)
        except Exception as exc:  # noqa: BLE001 - record and try the fallback
            errors.append(f"{fetcher.__name__}: {exc}")
    raise FetchError(f"{asset} {address}: all endpoints failed: {'; '.join(errors)}")
