"""Re-verify the Sepolia WETH/USDC Uniswap V3 pool addresses and liquidity
that adapter/sepolia_contracts.py hardcodes.

Queries UniswapV3Factory.getPool(WETH, USDC, fee) for every standard fee
tier directly against a public Sepolia RPC and prints each pool's live
liquidity(). Run this any time to confirm the addresses baked into the
adapter still carry real liquidity before a demo.

Usage:
    python scripts/verify_sepolia_pool.py
"""

from __future__ import annotations

from web3 import Web3

RPCS = [
    "https://ethereum-sepolia-rpc.publicnode.com",
    "https://rpc.sepolia.org",
    "https://sepolia.gateway.tenderly.co",
]

FACTORY = Web3.to_checksum_address("0x0227628f3F023bb0B980b67D528571c95c6DaC1c")
WETH = Web3.to_checksum_address("0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14")
USDC = Web3.to_checksum_address("0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238")

FACTORY_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]
POOL_ABI = [
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"internalType": "int24", "name": "tick", "type": "int24"},
            {"internalType": "uint16", "name": "observationIndex", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinality", "type": "uint16"},
            {"internalType": "uint16", "name": "observationCardinalityNext", "type": "uint16"},
            {"internalType": "uint8", "name": "feeProtocol", "type": "uint8"},
            {"internalType": "bool", "name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def main() -> None:
    for rpc in RPCS:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            print(f"{rpc}: not connected")
            continue
        print(f"{rpc}: chain_id={w3.eth.chain_id} block={w3.eth.block_number}")
        factory = w3.eth.contract(address=FACTORY, abi=FACTORY_ABI)
        for fee in (100, 500, 3000, 10000):
            pool = factory.functions.getPool(WETH, USDC, fee).call()
            if int(pool, 16) == 0:
                print(f"  fee={fee} pool=NONE")
                continue
            pool_c = w3.eth.contract(address=Web3.to_checksum_address(pool), abi=POOL_ABI)
            liq = pool_c.functions.liquidity().call()
            slot0 = pool_c.functions.slot0().call()
            print(f"  fee={fee} pool={pool} liquidity={liq} sqrtPriceX96={slot0[0]}")
        return


if __name__ == "__main__":
    main()
