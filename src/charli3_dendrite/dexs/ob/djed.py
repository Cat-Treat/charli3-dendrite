"""Djed/Shen Stablecoin Order Book Module.

This module provides order book functionality for Djed (collateralized stablecoin)
and Shen (liquidity token) operations, following the exact patterns established
by the GeniusYield implementation.
"""

import math
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Union

from pycardano import Address
from pycardano import PlutusData
from pycardano import PlutusV1Script
from pycardano import PlutusV2Script
from pycardano import Redeemer
from pycardano import TransactionBuilder
from pycardano import TransactionId
from pycardano import TransactionInput
from pycardano import TransactionOutput
from pycardano import UTxO
from pycardano import Value
from pycardano import min_lovelace

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import BuyOrderBook
from charli3_dendrite.dexs.ob.ob_base import OrderBookOrder
from charli3_dendrite.dexs.ob.ob_base import SellOrderBook
from charli3_dendrite.utility import asset_to_value

# Djed/Shen mainnet asset IDs (policy_id + asset_name hex)
DJED_TOKEN = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "446a65644d6963726f555344"
)
SHEN_TOKEN = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "5368656e4d6963726f555344"
)
POOL_NFT = (
    "8db269c3ec630e06ae29f74bc39edd1f87c819f1056206e879a1cd61"
    "446a6564537461626c65436f696e4e4654"
)
ORDER_NFT_POLICY = "04ea363a127872366ef2d3186325a25a5cee8826ff8a79dc7c8fa671"
ORDER_NFT_NAME_HEX = "446a65644f726465725469636b6574"

# Shelley mainnet genesis parameters - Necessary for order datum creation
SHELLEY_START_POSIX = 1596491091  # Unix timestamp when Shelley started
SHELLEY_START_SLOT = 4924800  # Slot number when Shelley started
MIN_RESERVE_RATIO_PERCENT = 400
MAX_RESERVE_RATIO_PERCENT = 800
MIN_ORDER_AMOUNT = 50_000_000
FEE_NUMERATOR = 15
FEE_DENOMINATOR = 1000


def _slot_to_posix_ms(slot: int) -> int:
    """Convert a Cardano slot number to POSIX milliseconds (mainnet only)."""
    return (slot - SHELLEY_START_SLOT + SHELLEY_START_POSIX) * 1000


@dataclass
class DjedRational(PlutusData):
    """Plutus-compatible rational number for on-chain data.

    IMPORTANT: Field order matches open-djed TypeScript - denominator first.
    """

    CONSTR_ID = 0
    denominator: int
    numerator: int


@dataclass
class DjedProcessOrderRedeemer(PlutusData):
    """Redeemer for processing orders (spending order UTxO)."""

    CONSTR_ID = 0


@dataclass
class DjedCancelOrderRedeemer(PlutusData):
    """Redeemer for canceling orders / burning order NFT."""

    CONSTR_ID = 1


@dataclass
class DjedOrderMintRedeemer(PlutusData):
    """Redeemer for minting order NFT."""

    CONSTR_ID = 0


@dataclass
class DjedProcessPoolRedeemer(PlutusData):
    """Redeemer for processing pool UTxO."""

    CONSTR_ID = 1


@dataclass
class DjedTxHash(PlutusData):
    """Transaction hash wrapper."""

    CONSTR_ID = 0
    tx_hash: bytes


@dataclass
class DjedOutputReference(PlutusData):
    """Output reference structure for pool datum."""

    CONSTR_ID = 0
    tx_hash: DjedTxHash
    output_index: int


@dataclass
class DjedLastOrderEntry(PlutusData):
    """Last order entry with order reference and timestamp."""

    CONSTR_ID = 0
    order: DjedOutputReference
    time: int


@dataclass
class DjedLastOrder(PlutusData):
    """Wrapper for last order tuple."""

    CONSTR_ID = 0
    entry: DjedLastOrderEntry


@dataclass
class DjedPoolDatumNone(PlutusData):
    """Null/None value for optional fields in pool datum."""

    CONSTR_ID = 1


@dataclass
class DjedPoolDatum(PlutusData):
    """Pool datum containing reserve state and protocol configuration.

    Fields:
    - adaInReserve: How much ADA is in the pool
    - djedInCirculation: How much DJED is in circulation
    - shenInCirculation: How much SHEN is in circulation
    - lastOrder: Last action (mint/burn djed/shen) reference
    - minADA: Minimum ADA for UTxOs
    - reserved1: Reserved field (unknown purpose)
    - reserved2: Nullable reserved field
    - mintingPolicyId: Minting policy of DJED, SHEN and DjedStableCoinNFT
    - mintingPolicyUniqRef: Unique reference for one-shot minting policy
    - reserved3: Reserved output reference
    """

    CONSTR_ID = 0
    ada_in_reserve: int
    djed_in_circulation: int
    shen_in_circulation: int
    last_order: DjedLastOrder
    min_ada: int
    reserved1: int
    reserved2: Union[DjedPoolDatumNone, PlutusData]  # Nullable
    minting_policy_id: bytes
    minting_policy_uniq_ref: DjedOutputReference
    reserved3: DjedOutputReference


@dataclass
class DjedExtendedFinite(PlutusData):
    """Finite timestamp in Extended type (Constructor 1 = Finite)."""

    CONSTR_ID = 1
    time: int


@dataclass
class DjedExtendedPosInf(PlutusData):
    """Positive infinity in Extended type (Constructor 2 = PosInf)."""

    CONSTR_ID = 2


@dataclass
class DjedBoundClosed(PlutusData):
    """Closed bound indicator (Constructor 1 = True)."""

    CONSTR_ID = 1


@dataclass
class DjedBoundOpen(PlutusData):
    """Open bound indicator (Constructor 0 = False)."""

    CONSTR_ID = 0


@dataclass
class DjedLowerBound(PlutusData):
    """Lower bound of validity interval."""

    CONSTR_ID = 0
    bound: Union[DjedExtendedFinite, DjedExtendedPosInf]
    closed: Union[DjedBoundClosed, DjedBoundOpen]


@dataclass
class DjedUpperBound(PlutusData):
    """Upper bound of validity interval."""

    CONSTR_ID = 0
    bound: Union[DjedExtendedFinite, DjedExtendedPosInf]
    closed: Union[DjedBoundClosed, DjedBoundOpen]


@dataclass
class DjedValidityRange(PlutusData):
    """Validity range for oracle data."""

    CONSTR_ID = 0
    lower_bound: DjedLowerBound
    upper_bound: DjedUpperBound


@dataclass
class DjedOracleFields(PlutusData):
    """Oracle fields containing exchange rate and validity information."""

    CONSTR_ID = 0
    ada_usd_exchange_rate: DjedRational  # USD/ADA rate (uses denominator, numerator)
    validity_range: DjedValidityRange
    expressed_in: bytes  # Currency denomination (e.g., b"USD")


@dataclass
class DjedOracleDatum(PlutusData):
    """Oracle datum containing price feed data.

    The oracle provides the ADA/USD exchange rate used to:
    - Calculate Djed mint/burn prices
    - Determine Shen pricing based on excess reserves
    - Validate reserve ratio constraints
    """

    CONSTR_ID = 0
    oracle_signature: bytes  # 64-byte key
    oracle_fields: DjedOracleFields
    oracle_token_policy_id: bytes


@dataclass
class DjedMintAction(PlutusData):
    """Djed minting action in order datum."""

    CONSTR_ID = 0
    djed_amount: int
    ada_amount: int


@dataclass
class DjedBurnAction(PlutusData):
    """Djed burning action in order datum."""

    CONSTR_ID = 1
    djed_amount: int


@dataclass
class ShenMintAction(PlutusData):
    """Shen minting action in order datum."""

    CONSTR_ID = 2
    shen_amount: int
    ada_amount: int


@dataclass
class ShenBurnAction(PlutusData):
    """Shen burning action in order datum."""

    CONSTR_ID = 3
    shen_amount: int


@dataclass
class DjedOrderDatum(OrderDatum):
    """Djed/Shen order datum structure (following existing OrderDatum pattern)."""

    CONSTR_ID = 0
    action: Union[DjedMintAction, DjedBurnAction, ShenMintAction, ShenBurnAction]
    owner_address: PlutusFullAddress
    oracle_rate: DjedRational
    creation_time: int
    order_nft: bytes

    def pool_pair(self) -> Assets | None:
        """Return the asset pair for this order (required by OrderDatum interface)."""
        if isinstance(self.action, (DjedMintAction, DjedBurnAction)):
            # Djed <-> ADA pair
            return Assets(lovelace=0) + Assets(**{DJED_TOKEN: 0})
        # Shen operations - Shen <-> ADA pair
        return Assets(lovelace=0) + Assets(**{SHEN_TOKEN: 0})

    def address_source(self) -> str | None:
        """Source address (required by OrderDatum interface)."""
        return self.owner_address.to_address().encode("bech32")

    def requested_amount(self) -> Assets:
        """Return the requested amount for this order."""
        if isinstance(self.action, DjedMintAction):
            return Assets(**{DJED_TOKEN: self.action.djed_amount})
        if isinstance(self.action, DjedBurnAction):
            # For burn, calculate ADA amount based on oracle rate
            # Invert oracle rate (Djed/ADA -> ADA/Djed) and multiply
            ada_amount = (
                self.action.djed_amount
                * self.oracle_rate.denominator
                // self.oracle_rate.numerator
            )
            return Assets(lovelace=ada_amount)
        if isinstance(self.action, ShenMintAction):
            return Assets(**{SHEN_TOKEN: self.action.shen_amount})
        # ShenBurnAction: exact ADA requires pool state which isn't in datum
        return Assets(lovelace=self.action.shen_amount)

    def order_type(self) -> OrderType | None:
        """Order type classification (required by OrderDatum interface)."""
        if isinstance(self.action, (DjedMintAction, ShenMintAction)):
            return OrderType.deposit  # Minting = deposit operation
        return OrderType.swap  # Burning = swap operation


def _calculate_operator_fee(ada_amount: int) -> int:
    """Calculate Djed operator fee based on ADA amount.

    Fee is 0.25% (1/400) of the ADA amount, clamped between min and max.
    """
    return max(5_150_000, min(25_000_000, ada_amount // 400))


def _finalize_order_tx(
    tx_builder: TransactionBuilder,
    user_address: Address,
    pool_datum: "DjedPoolDatum",
    minting_policy_ref: UTxO,
) -> None:
    """Add common order transaction components (pool datum output, signer, mint NFT)."""
    # Add pool datum hash output to user's address (required by minting script)
    pool_datum_hash_output = TransactionOutput(
        address=user_address,
        amount=asset_to_value(Assets(lovelace=0)),
        datum_hash=pool_datum.hash(),
    )
    pool_datum_hash_output.amount.coin = min_lovelace(
        tx_builder.context,
        output=pool_datum_hash_output,
    )
    tx_builder.add_output(pool_datum_hash_output)

    # Add pool datum to witness set (required when output uses datum_hash)
    if tx_builder.datums is None:
        tx_builder.datums = {}
    tx_builder.datums[pool_datum.hash()] = pool_datum

    # Add user as required signer
    if tx_builder.required_signers is None:
        tx_builder.required_signers = []
    tx_builder.required_signers.append(user_address.payment_part)

    # Mint the order NFT (+1)
    tx_builder.add_minting_script(
        script=minting_policy_ref,
        redeemer=Redeemer(DjedOrderMintRedeemer()),
    )
    mint_assets = Assets(**{ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX: 1})
    if tx_builder.mint is None:
        tx_builder.mint = asset_to_value(mint_assets).multi_asset
    else:
        tx_builder.mint += asset_to_value(mint_assets).multi_asset


class DjedShenOrderBookBase(AbstractOrderBookState):
    """Base class for Djed/Shen order books sharing common functionality."""

    fee: int = 150  # 1.5% fee in basis points
    _deposit: Assets = Assets(lovelace=3_000_000)

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order contract address (shared)."""
        return [
            "addr1wypp5vhw2csaf62d78vmaa4652z20nr4hfgmkhacqnrvgug2vdyq4",
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection for Djed/Shen orders (shared)."""
        pool_addr = (
            "addr1z8mcpc26j64fmhhd6sv5qj5mk9xqnfxgm6k8zmk7h2rlu4"
            "qm5kjdmrpmng059yellupyvwgay2v0lz6663swmds7hp0qhxg9gt"
        )
        return PoolSelector(
            addresses=[pool_addr],
            assets=[POOL_NFT],
        )

    @classmethod
    def oracle_selector(cls) -> PoolSelector:
        """Oracle selection for Djed/Shen (shared)."""
        oracle_nft = (
            "815aca02042ba9188a2ca4f8ce7b276046e2376b4bce56391342299e"
            "446a65644f7261636c654e4654"
        )
        return PoolSelector(
            addresses=["addr1wxyc99q448xlkv4q2y3truxq7j2msr6hkqqg0wmzz9n9r6q8j7kpa"],
            assets=[oracle_nft],
        )

    @classmethod
    def djed_asset(cls) -> str:
        """Return the DJED asset ID."""
        return DJED_TOKEN

    @classmethod
    def shen_asset(cls) -> str:
        """Return the SHEN asset ID."""
        return SHEN_TOKEN

    @classmethod
    def _get_oracle_utxo_and_datum(cls) -> tuple[UTxO, DjedOracleDatum]:
        """Get oracle UTxO and datum in a single fetch (avoids race conditions)."""
        selector = cls.oracle_selector()
        oracle_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not oracle_utxos:
            raise RuntimeError("Oracle UTxO not found")
        oracle_info = oracle_utxos[0]
        datum = DjedOracleDatum.from_cbor(oracle_info.datum_cbor)
        utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(oracle_info.tx_hash)),
                index=oracle_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(oracle_info.address),
                amount=asset_to_value(oracle_info.assets),
                datum=datum,
            ),
        )
        return utxo, datum

    @classmethod
    def _get_pool_utxo_and_datum(cls) -> tuple[UTxO, DjedPoolDatum]:
        """Get pool UTxO and datum in a single fetch (avoids race conditions)."""
        selector = cls.pool_selector()
        pool_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not pool_utxos:
            raise RuntimeError("Pool UTxO not found")
        pool_info = pool_utxos[0]
        datum = DjedPoolDatum.from_cbor(pool_info.datum_cbor)
        utxo = UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(pool_info.tx_hash)),
                index=pool_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(pool_info.address),
                amount=asset_to_value(pool_info.assets),
                datum=datum,
            ),
        )
        return utxo, datum

    @classmethod
    def _get_minting_policy_ref_utxo(cls) -> UTxO:
        """Get the order minting policy script reference UTxO."""
        from pycardano import ScriptHash

        script = get_backend().get_script_from_address(
            Address(
                payment_part=ScriptHash(
                    payload=bytes.fromhex(ORDER_NFT_POLICY),
                ),
            ),
        )

        return UTxO(
            input=TransactionInput(
                TransactionId(
                    bytes.fromhex(
                        "1a757d9840dfd77f5aa0223245b553d412328dadb10abc5225f4f8e53ae90ee0",
                    ),
                ),
                index=1,
            ),
            output=TransactionOutput(
                address=Address.decode(script.address),
                amount=Value(coin=22_110_300),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )

    @classmethod
    def batcher_fee(
        cls,
        in_assets: Assets,
        out_assets: Assets,
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
        include_action_fee: bool = True,
    ) -> Assets:
        """Calculate total fee estimate for a mint or burn operation.

        Args:
            in_assets: Input assets (ADA for mints, tokens for burns)
            out_assets: Output assets (DJED for mints, ADA for burns)
            oracle_rate: Pre-fetched oracle rate (fetched if None)
            pool_datum: Pre-fetched pool datum (fetched if None, needed for SHEN)
            include_action_fee: If True, include action fee in the calculation

        Returns:
            Total fees in ADA (operator fee + action fee)
        """
        if oracle_rate is None:
            _oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        if in_assets.unit() == "lovelace":
            if out_assets.unit() == DJED_TOKEN:
                base_ada = math.ceil(
                    out_assets.quantity()
                    * oracle_rate.denominator
                    / oracle_rate.numerator,
                )
                ada_with_fee = math.ceil(
                    out_assets.quantity()
                    * oracle_rate.denominator
                    * 1015
                    / (oracle_rate.numerator * 1000),
                )
            elif out_assets.unit() == SHEN_TOKEN:
                if pool_datum is None:
                    _pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
                fee_num, fee_den = ShenOrderBook.price_ratio(
                    side="mint",
                    oracle_rate=oracle_rate,
                    pool_datum=pool_datum,
                )
                ada_with_fee = math.ceil(out_assets.quantity() * fee_num / fee_den)
                base_ada = math.ceil(
                    out_assets.quantity() * fee_num * 1000 / (fee_den * 1015),
                )
            else:
                raise ValueError(f"Unsupported mint output asset: {out_assets.unit()}")

            operator_fee = _calculate_operator_fee(ada_with_fee)
            action_fee = max(0, ada_with_fee - base_ada)
            if include_action_fee:
                return Assets(lovelace=operator_fee + action_fee)
            return Assets(lovelace=operator_fee)

        if in_assets.unit() == DJED_TOKEN:
            base_ada = (in_assets.quantity() * oracle_rate.denominator) // (
                oracle_rate.numerator
            )
            ada_with_fee = (in_assets.quantity() * oracle_rate.denominator * 985) // (
                oracle_rate.numerator * 1000
            )
        elif in_assets.unit() == SHEN_TOKEN:
            if pool_datum is None:
                _pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
            fee_num, fee_den = ShenOrderBook.price_ratio(
                side="burn",
                oracle_rate=oracle_rate,
                pool_datum=pool_datum,
            )
            ada_with_fee = (in_assets.quantity() * fee_num) // fee_den
            # Recover no-fee estimate from fee-applied ratio (x * 985 / 1000).
            base_ada = (in_assets.quantity() * fee_num * 1000) // (fee_den * 985)
        else:
            raise ValueError(f"Unsupported burn input asset: {in_assets.unit()}")

        operator_fee = _calculate_operator_fee(ada_with_fee)
        action_fee = max(0, base_ada - ada_with_fee)
        if include_action_fee:
            return Assets(lovelace=operator_fee + action_fee)
        return Assets(lovelace=operator_fee)

    @classmethod
    def get_reserve_ratio(
        cls,
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
    ) -> float:
        """Get current reserve ratio as a percentage."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if pool_datum is None:
            _, pool_datum = cls._get_pool_utxo_and_datum()

        liabilities = pool_datum.djed_in_circulation * oracle_rate.denominator
        if liabilities == 0:
            return float("inf")

        assets = pool_datum.ada_in_reserve * oracle_rate.numerator
        return (assets / liabilities) * 100

    @staticmethod
    def _fraction_floor_non_negative(value: Fraction) -> int:
        """Convert a rational to a non-negative floored integer."""
        return max(0, value.numerator // value.denominator)

    @classmethod
    def max_mintable_djed(
        cls,
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
    ) -> int:
        """Maximum DJED mintable under min reserve ratio constraint."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if pool_datum is None:
            _, pool_datum = cls._get_pool_utxo_and_datum()

        djed_ada_rate = Fraction(oracle_rate.denominator, oracle_rate.numerator)
        mint_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        min_reserve_ratio = Fraction(MIN_RESERVE_RATIO_PERCENT, 100)
        denominator_factor = min_reserve_ratio - 1 - mint_fee
        if denominator_factor <= 0:
            return 0

        value = (
            Fraction(pool_datum.ada_in_reserve)
            - min_reserve_ratio * pool_datum.djed_in_circulation * djed_ada_rate
        ) / (djed_ada_rate * denominator_factor)
        return cls._fraction_floor_non_negative(value)

    @classmethod
    def max_mintable_shen(
        cls,
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
    ) -> int:
        """Maximum SHEN mintable under max reserve ratio constraint."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if pool_datum is None:
            _, pool_datum = cls._get_pool_utxo_and_datum()
        if pool_datum.shen_in_circulation <= 0:
            return 0

        djed_ada_rate = Fraction(oracle_rate.denominator, oracle_rate.numerator)
        shen_ada_rate = (
            Fraction(pool_datum.ada_in_reserve)
            - pool_datum.djed_in_circulation * djed_ada_rate
        ) / pool_datum.shen_in_circulation
        if shen_ada_rate <= 0:
            return 0

        mint_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        max_reserve_ratio = Fraction(MAX_RESERVE_RATIO_PERCENT, 100)
        value = (
            (
                max_reserve_ratio * pool_datum.djed_in_circulation * djed_ada_rate
                - Fraction(pool_datum.ada_in_reserve)
            )
            / shen_ada_rate
            / (1 + mint_fee)
        )

        return max(0, cls._fraction_floor_non_negative(value) - 1)

    @classmethod
    def max_burnable_shen(
        cls,
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
    ) -> int:
        """Maximum SHEN burnable under min reserve ratio constraint."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if pool_datum is None:
            _, pool_datum = cls._get_pool_utxo_and_datum()
        if pool_datum.shen_in_circulation <= 0:
            return 0

        djed_ada_rate = Fraction(oracle_rate.denominator, oracle_rate.numerator)
        shen_ada_rate = (
            Fraction(pool_datum.ada_in_reserve)
            - pool_datum.djed_in_circulation * djed_ada_rate
        ) / pool_datum.shen_in_circulation
        if shen_ada_rate <= 0:
            return 0

        burn_fee = Fraction(FEE_NUMERATOR, FEE_DENOMINATOR)
        min_reserve_ratio = Fraction(MIN_RESERVE_RATIO_PERCENT, 100)
        fee_factor = 1 - burn_fee
        if fee_factor <= 0:
            return 0

        value = (
            (
                Fraction(pool_datum.ada_in_reserve)
                - min_reserve_ratio * pool_datum.djed_in_circulation * djed_ada_rate
            )
            / shen_ada_rate
            / fee_factor
        )
        return cls._fraction_floor_non_negative(value)

    @property
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        return True

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Get default script class."""
        return PlutusV2Script

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return DjedOrderDatum

    @property
    def stake_address(self) -> Address | None:
        """Return the staking address."""
        return None


class DjedOrderBook(DjedShenOrderBookBase):
    """Djed order book for Djed mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[OrderBookOrder] | None = None,
    ) -> "DjedOrderBook":
        """Create a placeholder Djed order book for transaction building."""
        if assets is None:
            assets = Assets({"lovelace": 0, DJED_TOKEN: 0})
        buy_orders = orders or []

        ob = DjedOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook([]),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        ob.buy_book_full = ob.buy_book_full[:3]

        return ob

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Djed"

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob."""
        return "Djed"

    @classmethod
    def price_ratio(
        cls,
        side: str = "mint",
        oracle_rate: "DjedRational | None" = None,
    ) -> tuple[int, int]:
        """Return ADA/DJED price as (numerator, denominator)."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        num = oracle_rate.denominator
        den = oracle_rate.numerator
        if side == "mint":  # Add 1.5% fee
            num *= 1015
            den *= 1000
        elif side == "burn":  # Deduct 1.5% fee
            num *= 985
            den *= 1000
        else:
            raise ValueError("side must be 'mint' or 'burn'")
        return num, den

    @property
    def price(self) -> tuple[int, int]:
        """Price for ADA/DJED (includes 1.5% action fee)."""
        return self.price_ratio(side="mint")

    @classmethod
    def get_amount_out(
        cls,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate output for a given input for Djed mint/burn operations.

        Args:
            asset: Input assets (ADA for mint, DJED for burn)

        Returns:
            Tuple of (output_assets, slippage). Includes 1.5% action fee.
        """
        _, oracle_datum = cls._get_oracle_utxo_and_datum()
        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if asset.unit() == "lovelace":
            num, den = cls.price_ratio(
                side="mint",
                oracle_rate=oracle_rate,
            )
            djed_out = (asset.quantity() * den) // num
            return Assets(**{DJED_TOKEN: djed_out}), 0
        num, den = cls.price_ratio(
            side="burn",
            oracle_rate=oracle_rate,
        )
        ada_out = (asset.quantity() * num) // den
        return Assets(lovelace=ada_out), 0

    @classmethod
    def get_amount_in(
        cls,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate required input for a desired output for Djed mint/burn operations.

        Args:
            asset: Desired output assets (DJED for mint, ADA for burn)

        Returns:
            Tuple of (input_assets, slippage). Includes 1.5% action fee.
        """
        _, oracle_datum = cls._get_oracle_utxo_and_datum()
        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if asset.unit() == "lovelace":
            num, den = cls.price_ratio(
                side="burn",
                oracle_rate=oracle_rate,
            )
            djed_in = math.ceil(asset.quantity() * den / num)
            return Assets(**{DJED_TOKEN: djed_in}), 0
        num, den = cls.price_ratio(
            side="mint",
            oracle_rate=oracle_rate,
        )
        ada_in = math.ceil(asset.quantity() * num / den)
        return Assets(lovelace=ada_in), 0

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Create a Djed mint/burn order.

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        target_address = address_target or address_source
        if in_assets.unit() == "lovelace":
            amount = out_assets.quantity()
            is_mint = True
            if out_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum mint amount for DJED is {MIN_ORDER_AMOUNT}",
                )
        elif in_assets.unit() == DJED_TOKEN:
            amount = in_assets.quantity()
            is_mint = False
            if in_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum burn amount for DJED is {MIN_ORDER_AMOUNT}",
                )
        else:
            raise ValueError(f"Unsupported input asset for Djed: {in_assets.unit()}")

        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot
        creation_time = _slot_to_posix_ms(ttl_slot)

        oracle_utxo, oracle_datum = self._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = self._get_pool_utxo_and_datum()
        minting_policy_ref = self._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        reserve_ratio = self.get_reserve_ratio(oracle_rate, pool_datum)

        if is_mint:
            if reserve_ratio <= MIN_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "DJED mint not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be > "
                    f"{MIN_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(
                side="mint",
                oracle_rate=oracle_rate,
            )
            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = ada_amount + pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=DjedMintAction(djed_amount=amount, ada_amount=ada_amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(lovelace=total_ada)
        else:
            num, den = self.price_ratio(
                side="burn",
                oracle_rate=oracle_rate,
            )
            ada_amount = (amount * num) // den
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=DjedBurnAction(djed_amount=amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(**{DJED_TOKEN: amount, "lovelace": total_ada})

        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        order_address = Address.decode(self.order_selector()[0])
        output_assets.root[ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            target_address,
            pool_datum,
            minting_policy_ref,
        )

        return order_output, order_datum


class ShenOrderBook(DjedShenOrderBookBase):
    """Shen order book for Shen mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets | None = None,
        orders: list[OrderBookOrder] | None = None,
    ) -> "ShenOrderBook":
        """Create a placeholder Shen order book for transaction building."""
        if assets is None:
            assets = Assets({"lovelace": 0, SHEN_TOKEN: 0})
        buy_orders = orders or []

        ob = ShenOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook([]),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        ob.buy_book_full = ob.buy_book_full[:3]

        return ob

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Shen"

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob."""
        return "Shen"

    @classmethod
    def price_ratio(
        cls,
        side: str = "mint",
        oracle_rate: "DjedRational | None" = None,
        pool_datum: "DjedPoolDatum | None" = None,
    ) -> tuple[int, int]:
        """Return ADA/SHEN price as (numerator, denominator)."""
        if oracle_rate is None:
            _, oracle_datum = cls._get_oracle_utxo_and_datum()
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        if pool_datum is None:
            _, pool_datum = cls._get_pool_utxo_and_datum()

        num = (
            pool_datum.ada_in_reserve * oracle_rate.numerator
            - pool_datum.djed_in_circulation * oracle_rate.denominator
        )
        den = pool_datum.shen_in_circulation * oracle_rate.numerator

        if side == "mint":  # Add 1.5% fee
            num *= 1015
            den *= 1000
        elif side == "burn":  # Deduct 1.5% fee
            num *= 985
            den *= 1000
        else:
            raise ValueError("side must be 'mint' or 'burn'")

        return num, den

    @property
    def price(self) -> tuple[int, int]:
        """Price for ADA/SHEN (includes 1.5% action fee)."""
        return self.price_ratio(side="mint")

    @classmethod
    def get_amount_out(
        cls,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate output for a given input for Shen mint/burn operations.

        Args:
            asset: Input assets (ADA for mint, SHEN for burn)

        Returns:
            Tuple of (output_assets, slippage). Includes 1.5% action fee.
        """
        _, oracle_datum = cls._get_oracle_utxo_and_datum()
        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        _, pool_datum = cls._get_pool_utxo_and_datum()
        num, den = cls.price_ratio(
            side="mint" if asset.unit() == "lovelace" else "burn",
            oracle_rate=oracle_rate,
            pool_datum=pool_datum,
        )
        if num <= 0 or den <= 0:
            return (
                Assets(**{SHEN_TOKEN: 0})
                if asset.unit() == "lovelace"
                else Assets(lovelace=0),
                0,
            )
        if asset.unit() == "lovelace":
            shen_out = (asset.quantity() * den) // num
            return Assets(**{SHEN_TOKEN: shen_out}), 0
        ada_out = (asset.quantity() * num) // den
        return Assets(lovelace=ada_out), 0.0

    @classmethod
    def get_amount_in(
        cls,
        asset: Assets,
    ) -> tuple[Assets, float]:
        """Calculate required input for a desired output for Shen mint/burn operations.

        Args:
            asset: Desired output assets (SHEN for mint, ADA for burn)

        Returns:
            Tuple of (input_assets, slippage). Includes 1.5% action fee.
        """
        _, oracle_datum = cls._get_oracle_utxo_and_datum()
        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        _, pool_datum = cls._get_pool_utxo_and_datum()
        if asset.unit() == "lovelace":
            num, den = cls.price_ratio(
                side="burn",
                oracle_rate=oracle_rate,
                pool_datum=pool_datum,
            )
            if num <= 0 or den <= 0:
                return Assets(**{SHEN_TOKEN: 0}), 0
            shen_in = math.ceil(asset.quantity() * den / num)
            return Assets(**{SHEN_TOKEN: shen_in}), 0
        num, den = cls.price_ratio(
            side="mint",
            oracle_rate=oracle_rate,
            pool_datum=pool_datum,
        )
        if num <= 0 or den <= 0:
            return Assets(lovelace=0), 0
        ada_in = math.ceil(asset.quantity() * num / den)
        return Assets(lovelace=ada_in), 0

    def swap_utxo(
        self,
        address_source: Address,
        in_assets: Assets,
        out_assets: Assets,
        tx_builder: TransactionBuilder,
        extra_assets: Assets | None = None,
        address_target: Address | None = None,
        datum_target: PlutusData | None = None,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Create a Shen mint/burn order.

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        target_address = address_target or address_source
        if in_assets.unit() == "lovelace":
            amount = out_assets.quantity()
            is_mint = True
            if out_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum mint amount for SHEN is {MIN_ORDER_AMOUNT}",
                )
        elif in_assets.unit() == SHEN_TOKEN:
            amount = in_assets.quantity()
            is_mint = False
            if in_assets.quantity() < MIN_ORDER_AMOUNT:
                raise ValueError(
                    f"Minimum burn amount for SHEN is {MIN_ORDER_AMOUNT}",
                )
        else:
            raise ValueError(f"Unsupported input asset for Shen: {in_assets.unit()}")

        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot
        creation_time = _slot_to_posix_ms(ttl_slot)

        oracle_utxo, oracle_datum = self._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = self._get_pool_utxo_and_datum()
        minting_policy_ref = self._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate
        reserve_ratio = self.get_reserve_ratio(oracle_rate, pool_datum)

        if is_mint:
            if reserve_ratio >= MAX_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "SHEN mint not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be < "
                    f"{MAX_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(
                side="mint",
                oracle_rate=oracle_rate,
                pool_datum=pool_datum,
            )
            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = ada_amount + pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=ShenMintAction(shen_amount=amount, ada_amount=ada_amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(lovelace=total_ada)
        else:
            if reserve_ratio <= MIN_RESERVE_RATIO_PERCENT:
                raise ValueError(
                    "SHEN burn not allowed: reserve ratio "
                    f"{reserve_ratio:.2f}% must be > "
                    f"{MIN_RESERVE_RATIO_PERCENT}%",
                )
            num, den = self.price_ratio(
                side="burn",
                oracle_rate=oracle_rate,
                pool_datum=pool_datum,
            )

            ada_amount = math.ceil(amount * num / den)
            operator_fee = _calculate_operator_fee(ada_amount)
            total_ada = pool_datum.min_ada + operator_fee
            order_datum = DjedOrderDatum(
                action=ShenBurnAction(shen_amount=amount),
                owner_address=PlutusFullAddress.from_address(target_address),
                oracle_rate=oracle_rate,
                creation_time=creation_time,
                order_nft=bytes.fromhex(ORDER_NFT_POLICY),
            )
            output_assets = Assets(**{SHEN_TOKEN: amount, "lovelace": total_ada})

        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        order_address = Address.decode(self.order_selector()[0])
        output_assets.root[ORDER_NFT_POLICY + ORDER_NFT_NAME_HEX] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            target_address,
            pool_datum,
            minting_policy_ref,
        )

        return order_output, order_datum
