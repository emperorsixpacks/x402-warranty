"""USDC refund logic."""

import time

from web3 import Web3

from config import settings


# ERC-20 transfer ABI
USDC_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def send_refund(to: str, amount_atomic: str) -> str:
    """Send a USDC refund. Returns tx hash."""
    if not settings.treasury_private_key:
        raise ValueError("TREASURY_PRIVATE_KEY not set")
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    treasury_account = w3.eth.account.from_key(settings.treasury_private_key)
    usdc = w3.eth.contract(address=Web3.to_checksum_address(settings.usdc_contract), abi=USDC_ABI)
    nonce = w3.eth.get_transaction_count(treasury_account.address)
    tx = usdc.functions.transfer(Web3.to_checksum_address(to), int(amount_atomic)).build_transaction({
        "from": treasury_account.address,
        "nonce": nonce,
        "gas": 100000,
        "maxFeePerGas": w3.to_wei("0.1", "gwei"),
        "maxPriorityFeePerGas": w3.to_wei("0.01", "gwei"),
        "chainId": settings.chain_id,
    })
    signed = treasury_account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    return tx_hash.hex()


_balance_cache: tuple[float, int] | None = None
_BALANCE_TTL_SECONDS = 30.0


def get_treasury_balance() -> int:
    """USDC balance of the treasury, cached for 30s. Raises on RPC failure."""
    global _balance_cache
    now = time.monotonic()
    if _balance_cache and now - _balance_cache[0] < _BALANCE_TTL_SECONDS:
        return _balance_cache[1]
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
    usdc = w3.eth.contract(address=Web3.to_checksum_address(settings.usdc_contract), abi=USDC_ABI)
    balance = usdc.functions.balanceOf(Web3.to_checksum_address(settings.treasury_address)).call()
    _balance_cache = (now, balance)
    return balance
