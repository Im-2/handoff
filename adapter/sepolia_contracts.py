"""Sepolia contract addresses and calldata builders for the KeeperHub adapter.

Almanak's own DEX connectors (framework/agent_tools + connectors/uniswap_v3)
hardcode mainnet contract addresses only -- there is no Sepolia wiring inside
the installed SDK (verified by reading .venv/Lib/site-packages/almanak/core
and connectors/uniswap_v3 source; see README "Why the adapter builds its own
calldata"). So this module -- not Almanak -- is responsible for turning an
Almanak-approved swap decision into real Sepolia calldata.

All addresses below were verified live against Sepolia on 2026-09-15:
  - Contract identity: fetched each address on sepolia.etherscan.io and
    confirmed the verified contract name (UniswapV3Factory / SwapRouter02 /
    WETH9 / USDC token).
  - Liquidity: called Factory.getPool(WETH, USDC, fee) over
    https://ethereum-sepolia-rpc.publicnode.com for every standard fee tier
    and read back non-zero `liquidity()` on each returned pool (see
    scripts/verify_sepolia_pool.py). The 0.05% (500) tier is used here as it
    had the tightest price and ample depth for small test-swap sizes.
"""

from __future__ import annotations

from web3 import Web3

CHAIN_ID = 11155111  # Sepolia
CHAIN_ID_STR = "11155111"

WETH9 = Web3.to_checksum_address("0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14")
USDC = Web3.to_checksum_address("0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238")
UNISWAP_V3_FACTORY = Web3.to_checksum_address("0x0227628f3F023bb0B980b67D528571c95c6DaC1c")
SWAP_ROUTER_02 = Web3.to_checksum_address("0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E")

# fee tier (hundredths of a bip) confirmed to carry real liquidity as of the
# verification run referenced in the module docstring.
WETH_USDC_POOL_FEE = 500
WETH_USDC_POOL = Web3.to_checksum_address("0x3289680dD4d6C10bb19b899729cda5eEF58AEfF1")

TOKENS = {
    "WETH": WETH9,
    "USDC": USDC,
}
TOKEN_DECIMALS = {
    "WETH": 18,
    "USDC": 6,
}


def erc20_approve_args(spender: str, amount_wei: int) -> tuple[str, list]:
    """Return (method, args) for kh execute contract-call targeting an ERC20 `approve`."""
    return "approve", [Web3.to_checksum_address(spender), str(amount_wei)]


def exact_input_single_args(
    *,
    token_in: str,
    token_out: str,
    fee: int,
    recipient: str,
    amount_in_wei: int,
    amount_out_minimum_wei: int,
) -> tuple[str, list]:
    """Return (method, args) for kh execute contract-call targeting SwapRouter02.exactInputSingle.

    SwapRouter02's exactInputSingle takes a single tuple param (struct). `kh
    execute contract-call --args` binds a tuple/struct argument from a JSON
    *object* keyed by the ABI's component names (confirmed empirically: a
    positional JSON array here fails with "params.tokenOut: address is
    missing" -- kh reads the array's first element as the whole `params`
    value and then looks for a `tokenOut` key on it).
    Note: SwapRouter02 dropped the `deadline` field the original SwapRouter had.
    """
    struct = {
        "tokenIn": Web3.to_checksum_address(token_in),
        "tokenOut": Web3.to_checksum_address(token_out),
        "fee": str(fee),
        "recipient": Web3.to_checksum_address(recipient),
        "amountIn": str(amount_in_wei),
        "amountOutMinimum": str(amount_out_minimum_wei),
        "sqrtPriceLimitX96": "0",
    }
    return "exactInputSingle", [struct]


EXACT_INPUT_SINGLE_ABI_FRAGMENT = {
    "inputs": [
        {
            "components": [
                {"internalType": "address", "name": "tokenIn", "type": "address"},
                {"internalType": "address", "name": "tokenOut", "type": "address"},
                {"internalType": "uint24", "name": "fee", "type": "uint24"},
                {"internalType": "address", "name": "recipient", "type": "address"},
                {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
            "internalType": "struct ISwapRouter.ExactInputSingleParams",
            "name": "params",
            "type": "tuple",
        }
    ],
    "name": "exactInputSingle",
    "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
    "type": "function",
}

ERC20_APPROVE_ABI_FRAGMENT = {
    "inputs": [
        {"internalType": "address", "name": "spender", "type": "address"},
        {"internalType": "uint256", "name": "amount", "type": "uint256"},
    ],
    "name": "approve",
    "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
    "stateMutability": "nonpayable",
    "type": "function",
}


def to_token_units(amount_decimal: str, symbol: str) -> int:
    from decimal import Decimal

    decimals = TOKEN_DECIMALS[symbol]
    return int((Decimal(amount_decimal) * (Decimal(10) ** decimals)).to_integral_value())
