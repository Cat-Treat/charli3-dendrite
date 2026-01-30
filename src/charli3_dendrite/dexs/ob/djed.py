"""Djed/Shen Stablecoin Order Book Module.

This module provides order book functionality for Djed (collateralized stablecoin)
and Shen (liquidity token) operations, following the exact patterns established
by the GeniusYield implementation.
"""

import time
from dataclasses import dataclass
from typing import Any
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

from charli3_dendrite.backend import get_backend
from charli3_dendrite.dataclasses.datums import OrderDatum
from charli3_dendrite.dataclasses.datums import PlutusFullAddress
from charli3_dendrite.dataclasses.models import Assets
from charli3_dendrite.dataclasses.models import OrderType
from charli3_dendrite.dataclasses.models import PoolSelector
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderBookState
from charli3_dendrite.dexs.ob.ob_base import AbstractOrderState
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

# Shelley mainnet genesis parameters - Necessary for order datum creation
SHELLEY_START_POSIX = 1596491091  # Unix timestamp when Shelley started
SHELLEY_START_SLOT = 4924800  # Slot number when Shelley started


def _slot_to_posix_ms(slot: int) -> int:
    """Convert a Cardano slot number to POSIX milliseconds (mainnet only)."""
    return (slot - SHELLEY_START_SLOT + SHELLEY_START_POSIX) * 1000


def _finalize_order_tx(
    tx_builder: TransactionBuilder,
    user_address: Address,
    pool_datum: "DjedPoolDatum",
    minting_policy_ref: UTxO,
    order_nft_unit: str,
) -> None:
    """Add common order transaction components (pool datum output, signer, mint NFT)."""
    # Add pool datum hash output to user's address (required by minting script)
    pool_datum_hash_output = TransactionOutput(
        address=user_address,
        amount=asset_to_value(Assets(lovelace=2_000_000)),
        datum_hash=pool_datum.hash(),
    )
    tx_builder.add_output(pool_datum_hash_output)

    # Add user as required signer
    if tx_builder.required_signers is None:
        tx_builder.required_signers = []
    tx_builder.required_signers.append(user_address.payment_part)

    # Mint the order NFT (+1)
    tx_builder.add_minting_script(
        script=minting_policy_ref,
        redeemer=Redeemer(DjedOrderMintRedeemer()),
    )
    mint_assets = Assets(**{order_nft_unit: 1})
    if tx_builder.mint is None:
        tx_builder.mint = asset_to_value(mint_assets).multi_asset
    else:
        tx_builder.mint += asset_to_value(mint_assets).multi_asset


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


class DjedShenOrderStateBase(AbstractOrderState):
    """Base class for Djed/Shen order states sharing common functionality.

    Reduces code duplication between Djed and Shen implementations by providing
    shared methods that follow the exact GeniusYield pattern.
    """

    tx_hash: str
    tx_index: int
    datum_cbor: str
    datum_hash: str
    inactive: bool = False

    _batcher_fee: Assets = Assets(lovelace=2_000_000)  # 2 ADA operator fee
    _datum_parsed: PlutusData | None = None

    @classmethod
    def dex_policy(cls) -> list[str] | None:
        """Djed/Shen order NFT policy IDs (following GeniusYield pattern)."""
        return [ORDER_NFT_POLICY]

    @classmethod
    def djed_asset(cls) -> str:
        """Djed token asset ID (mainnet)."""
        return DJED_TOKEN

    @classmethod
    def shen_asset(cls) -> str:
        """Shen token asset ID (mainnet)."""
        return SHEN_TOKEN

    @property
    def volume_fee(self) -> float:
        """Fee percentage for operations (following GeniusYield pattern)."""
        return 150  # 1.5% in basis points

    @property
    def reference_utxo(self) -> UTxO | None:
        """Get reference UTxO for script validation (following GeniusYield pattern)."""
        order_info = get_backend().get_pool_in_tx(
            self.tx_hash,
            assets=[self.dex_nft.unit()],
            addresses=self.pool_selector().addresses,
        )

        script = get_backend().get_script_from_address(
            Address.decode(order_info[0].address),
        )

        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=script.tx_index,
            ),
            output=TransactionOutput(
                address=script.address,
                amount=asset_to_value(script.assets),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )

    def _get_pool_utxo(self) -> UTxO:
        """Get pool UTxO using backend (shared by both Djed and Shen)."""
        selector = self.pool_selector()
        pool_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not pool_utxos:
            raise RuntimeError("Pool UTxO not found")

        pool_info = pool_utxos[0]
        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(pool_info.tx_hash)),
                index=pool_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(pool_info.address),
                amount=asset_to_value(pool_info.assets),
            ),
        )

    def _get_oracle_utxo(self) -> UTxO:
        """Get oracle UTxO using backend (shared by both Djed and Shen)."""
        selector = self.oracle_selector()
        oracle_utxos = get_backend().get_pool_utxos(
            limit=1,
            historical=False,
            **selector.model_dump(),
        )
        if not oracle_utxos:
            raise RuntimeError("Oracle UTxO not found")

        oracle_info = oracle_utxos[0]
        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(oracle_info.tx_hash)),
                index=oracle_info.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(oracle_info.address),
                amount=asset_to_value(oracle_info.assets),
            ),
        )

    @classmethod
    def post_init(cls, values: dict[str, Any]) -> None:
        """Post initialization validation (shared logic)."""
        super().post_init(values)

        # Parse and validate order datum
        try:
            datum = cls.order_datum_class().from_cbor(values["datum_cbor"])

            # Check if order is expired (3-minute TTL)
            current_time = int(time.time())
            if current_time > datum.creation_time + 180:  # 3 minutes
                values["inactive"] = True

        except (ValueError, KeyError):
            values["inactive"] = True

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order contract addresses (shared)."""
        return [
            "addr1wypp5vhw2csaf62d78vmaa4652z20nr4hfgmkhacqnrvgug2vdyq4",  # mainnet
        ]

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection for Djed/Shen orders (shared)."""
        # mainnet pool address
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
        # mainnet oracle NFT
        oracle_nft = (
            "815aca02042ba9188a2ca4f8ce7b276046e2376b4bce56391342299e"
            "446a65644f7261636c654e4654"
        )
        return PoolSelector(
            addresses=["addr1wxyc99q448xlkv4q2y3truxq7j2msr6hkqqg0wmzz9n9r6q8j7kpa"],
            assets=[oracle_nft],
        )

    @property
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        return False

    @property
    def stake_address(self) -> Address | None:
        """Return the staking address."""
        return None

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return DjedOrderDatum

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Get default script class."""
        return PlutusV2Script

    @property
    def pool_id(self) -> str:
        """A unique identifier for the pool or ob."""
        return "Djed"


class DjedOrderState(DjedShenOrderStateBase):
    """Djed order state handling Djed mint/burn operations.

    Inherits common functionality from DjedShenOrderStateBase and implements
    Djed-specific pricing and transaction logic.
    """

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Djed"

    @property
    def price(self) -> tuple[int, int]:
        """Price for Djed operations (ADA per Djed).

        Oracle rate is USD/ADA (= DJED/ADA since DJED is pegged to $1).
        We invert to get ADA/DJED, then apply fee multiplier.
        """
        oracle = self.order_datum.oracle_rate
        if isinstance(self.order_datum.action, DjedMintAction):
            # Inverted rate with 1.5% fee: (denom * 1015) / (num * 1000)
            return (oracle.denominator * 1015, oracle.numerator * 1000)
        # DjedBurnAction - Inverted rate with 1.5% fee deduction
        return (oracle.denominator * 985, oracle.numerator * 1000)

    @property
    def available(self) -> Assets:
        """Available amount for Djed orders."""
        if isinstance(self.order_datum.action, DjedMintAction):
            return Assets(**{DJED_TOKEN: self.order_datum.action.djed_amount})
        # DjedBurnAction - Calculate ADA to return based on current oracle rate
        ada_amount = self._calculate_ada_return(self.order_datum.action.djed_amount)
        return Assets(lovelace=ada_amount)

    def _calculate_ada_return(self, djed_amount: int) -> int:
        """Calculate ADA to return for Djed burning.

        Formula: djed_amount * (inverted_rate) * (1 - 1.5% fee)
               = djed_amount * (denom/num) * (985/1000)
               = (djed_amount * denom * 985) // (num * 1000)
        """
        oracle = self.order_datum.oracle_rate
        return (djed_amount * oracle.denominator * 985) // (oracle.numerator * 1000)

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
        """Build transaction for Djed order processing."""
        # Get reference UTxOs (using shared methods)
        pool_utxo = self._get_pool_utxo()
        oracle_utxo = self._get_oracle_utxo()

        # Add order UTxO as script input (following GeniusYield pattern)
        assets = self.assets + Assets(**{self.dex_nft.unit(): 1})
        order_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(assets),
                datum_hash=self.order_datum.hash(),
            ),
        )

        # Add script input with redeemer
        if out_assets.quantity() < self.available.quantity():
            redeemer = Redeemer(self._get_partial_redeemer(out_assets))
        else:
            redeemer = Redeemer(self._get_complete_redeemer())

        tx_builder.add_script_input(
            utxo=order_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )

        # Add reference inputs
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(oracle_utxo)

        # Process based on Djed operation type
        if isinstance(self.order_datum.action, DjedMintAction):
            return self._process_djed_mint(tx_builder, in_assets, out_assets, pool_utxo)
        # DjedBurnAction
        return self._process_djed_burn(tx_builder, in_assets, out_assets, pool_utxo)

    def _process_djed_mint(
        self,
        tx_builder: TransactionBuilder,
        in_assets: Assets,
        out_assets: Assets,
        pool_utxo: UTxO,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Process Djed minting order."""
        # Update order datum if partial fill
        order_datum = self.order_datum_class().from_cbor(self.order_datum.to_cbor())
        order_datum.action.djed_amount -= out_assets.quantity()

        # Update pool state with new Djed tokens
        updated_assets = self.assets.copy()
        updated_assets.root[in_assets.unit()] += in_assets.quantity()
        updated_assets.root[out_assets.unit()] -= out_assets.quantity()
        updated_assets += self._batcher_fee

        if out_assets.quantity() < self.available.quantity():
            # Partial fill - return updated order
            txo = TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(updated_assets),
                datum_hash=order_datum.hash(),
            )
        else:
            # Complete fill - pay user and close order
            # Burn the beacon token using OrderBurnRedeemer (CONSTR_ID = 1)
            tx_builder.add_minting_script(
                script=self.reference_utxo,
                redeemer=Redeemer(DjedCancelOrderRedeemer()),
            )
            if tx_builder.mint is None:
                tx_builder.mint = asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset
            else:
                tx_builder.mint += asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset

            # Pay Djed tokens to user
            payment_assets = Assets(**{out_assets.unit(): out_assets.quantity()})
            payment_assets += Assets(lovelace=2_000_000)  # Min ADA

            txo = TransactionOutput(
                address=order_datum.owner_address.to_address(),
                amount=asset_to_value(payment_assets),
            )

        tx_builder.datums.update({order_datum.hash(): order_datum})
        return txo, order_datum

    def _process_djed_burn(
        self,
        tx_builder: TransactionBuilder,
        in_assets: Assets,
        out_assets: Assets,
        pool_utxo: UTxO,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Process Djed burning order."""
        # Similar to mint but burning Djed for ADA
        order_datum = self.order_datum_class().from_cbor(self.order_datum.to_cbor())
        order_datum.action.djed_amount -= in_assets.quantity()

        # Update pool state
        updated_assets = self.assets.copy()
        updated_assets.root[in_assets.unit()] -= in_assets.quantity()
        updated_assets.root[out_assets.unit()] += out_assets.quantity()
        updated_assets += self._batcher_fee

        if in_assets.quantity() < self.available.quantity():
            # Partial fill
            txo = TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(updated_assets),
                datum_hash=order_datum.hash(),
            )
        else:
            # Complete fill - close order and pay ADA
            # Burn the beacon token using OrderBurnRedeemer (CONSTR_ID = 1)
            tx_builder.add_minting_script(
                script=self.reference_utxo,
                redeemer=Redeemer(DjedCancelOrderRedeemer()),
            )
            if tx_builder.mint is None:
                tx_builder.mint = asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset
            else:
                tx_builder.mint += asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset

            # Pay ADA to user
            payment_assets = Assets(lovelace=out_assets.quantity())

            txo = TransactionOutput(
                address=order_datum.owner_address.to_address(),
                amount=asset_to_value(payment_assets),
            )

        tx_builder.datums.update({order_datum.hash(): order_datum})
        return txo, order_datum

    def _get_partial_redeemer(self, out_assets: Assets) -> PlutusData:
        """Get redeemer for partial order processing."""
        return DjedProcessOrderRedeemer()

    def _get_complete_redeemer(self) -> PlutusData:
        """Get redeemer for complete order processing."""
        return DjedProcessOrderRedeemer()


class ShenOrderState(DjedShenOrderStateBase):
    """Shen order state handling Shen mint/burn operations.

    Inherits common functionality from DjedShenOrderStateBase and implements
    Shen-specific pricing and transaction logic. Shen pricing is more complex
    as it depends on pool reserves and collateral ratios.
    """

    @classmethod
    def dex(cls) -> str:
        """Official dex name."""
        return "Shen"

    @property
    def price(self) -> tuple[int, int]:
        """Price for Shen operations (more complex - requires pool state)."""
        return self._calculate_shen_price()

    @property
    def available(self) -> Assets:
        """Available amount for Shen orders."""
        if isinstance(self.order_datum.action, ShenMintAction):
            return Assets(**{SHEN_TOKEN: self.order_datum.action.shen_amount})
        # ShenBurnAction
        ada_amount = self._calculate_shen_ada_return(
            self.order_datum.action.shen_amount,
        )
        return Assets(lovelace=ada_amount)

    def _get_pool_datum(self) -> DjedPoolDatum | None:
        """Parse pool datum from pool UTxO."""
        try:
            pool_utxo = self._get_pool_utxo()
            if pool_utxo.output.datum:
                return DjedPoolDatum.from_cbor(pool_utxo.output.datum.to_cbor())
        except (RuntimeError, ValueError):
            pass
        return None

    def _get_oracle_datum(self) -> DjedOracleDatum | None:
        """Parse oracle datum from oracle UTxO."""
        try:
            oracle_utxo = self._get_oracle_utxo()
            if oracle_utxo.output.datum:
                return DjedOracleDatum.from_cbor(oracle_utxo.output.datum.to_cbor())
        except (RuntimeError, ValueError):
            pass
        return None

    def _calculate_shen_price(self) -> tuple[int, int]:
        """Calculate Shen price based on current pool state.

        Shen price is determined by the excess ADA reserves beyond what's needed
        to back the Djed tokens. This is more complex than Djed pricing.
        Formula: (adaInReserve - djedInCirculation * djedADARate) / shenInCirculation
        """
        try:
            pool_datum = self._get_pool_datum()
            oracle_datum = self._get_oracle_datum()

            if not pool_datum or not oracle_datum:
                return (1, 1)  # Fallback if datums unavailable

            # Get oracle rate (stored as denominator/numerator)
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

            djed_backing_ada = (
                pool_datum.djed_in_circulation * oracle_rate.denominator
            ) // oracle_rate.numerator

            # Calculate excess ADA
            excess_ada = pool_datum.ada_in_reserve - djed_backing_ada

            if pool_datum.shen_in_circulation == 0:
                return (1, 1)  # No Shen in circulation

            # Return as rational (excess_ada, shen_in_circulation)
            # Apply 1.5% fee for minting
            if isinstance(self.order_datum.action, ShenMintAction):
                return (excess_ada * 1015, pool_datum.shen_in_circulation * 1000)
            # ShenBurnAction - deduct fee
            return (excess_ada * 985, pool_datum.shen_in_circulation * 1000)

        except (RuntimeError, ValueError):
            return (1, 1)  # Fallback price

    def _calculate_shen_ada_return(self, shen_amount: int) -> int:
        """Calculate ADA to return for Shen burning.

        Based on Shen's share of excess reserves beyond Djed backing,
        minus the 1.5% burn fee.
        """
        try:
            pool_datum = self._get_pool_datum()
            oracle_datum = self._get_oracle_datum()

            if not pool_datum or not oracle_datum:
                return shen_amount  # Fallback

            # Get oracle rate
            oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

            # Calculate excess ADA
            djed_backing_ada = (
                pool_datum.djed_in_circulation * oracle_rate.denominator
            ) // oracle_rate.numerator
            excess_ada = pool_datum.ada_in_reserve - djed_backing_ada

            if pool_datum.shen_in_circulation == 0:
                return shen_amount  # Fallback

            # Calculate ADA return = (shen_amount / total_shen) * excess_ada * (1 - fee)
            # = shen_amount * excess_ada * 985 / (shen_in_circulation * 1000)
            return (shen_amount * excess_ada * 985) // (
                pool_datum.shen_in_circulation * 1000
            )

        except (RuntimeError, ValueError):
            return shen_amount  # Fallback

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
        """Build transaction for Shen order processing."""
        # Get reference UTxOs (using shared methods)
        pool_utxo = self._get_pool_utxo()
        oracle_utxo = self._get_oracle_utxo()

        assets = self.assets + Assets(**{self.dex_nft.unit(): 1})
        order_utxo = UTxO(
            TransactionInput(
                transaction_id=TransactionId(bytes.fromhex(self.tx_hash)),
                index=self.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(assets),
                datum_hash=self.order_datum.hash(),
            ),
        )

        redeemer = Redeemer(DjedProcessOrderRedeemer())

        tx_builder.add_script_input(
            utxo=order_utxo,
            script=self.reference_utxo,
            redeemer=redeemer,
        )

        # Add reference inputs
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(oracle_utxo)

        # Process based on Shen operation type
        if isinstance(self.order_datum.action, ShenMintAction):
            return self._process_shen_mint(tx_builder, in_assets, out_assets, pool_utxo)
        # ShenBurnAction
        return self._process_shen_burn(tx_builder, in_assets, out_assets, pool_utxo)

    def _process_shen_mint(
        self,
        tx_builder: TransactionBuilder,
        in_assets: Assets,
        out_assets: Assets,
        pool_utxo: UTxO,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Process Shen minting order."""
        order_datum = self.order_datum_class().from_cbor(self.order_datum.to_cbor())
        order_datum.action.shen_amount -= out_assets.quantity()

        updated_assets = self.assets.copy()
        updated_assets.root[in_assets.unit()] += in_assets.quantity()
        updated_assets.root[out_assets.unit()] -= out_assets.quantity()
        updated_assets += self._batcher_fee

        if out_assets.quantity() < self.available.quantity():
            txo = TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(updated_assets),
                datum_hash=order_datum.hash(),
            )
        else:
            # Complete fill - burn beacon token
            tx_builder.add_minting_script(
                script=self.reference_utxo,
                redeemer=Redeemer(DjedCancelOrderRedeemer()),
            )
            if tx_builder.mint is None:
                tx_builder.mint = asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset
            else:
                tx_builder.mint += asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset

            payment_assets = Assets(**{out_assets.unit(): out_assets.quantity()})
            payment_assets += Assets(lovelace=2_000_000)

            txo = TransactionOutput(
                address=order_datum.owner_address.to_address(),
                amount=asset_to_value(payment_assets),
            )

        tx_builder.datums.update({order_datum.hash(): order_datum})
        return txo, order_datum

    def _process_shen_burn(
        self,
        tx_builder: TransactionBuilder,
        in_assets: Assets,
        out_assets: Assets,
        pool_utxo: UTxO,
    ) -> tuple[TransactionOutput | None, PlutusData]:
        """Process Shen burning order."""
        order_datum = self.order_datum_class().from_cbor(self.order_datum.to_cbor())
        order_datum.action.shen_amount -= in_assets.quantity()

        updated_assets = self.assets.copy()
        updated_assets.root[in_assets.unit()] -= in_assets.quantity()
        updated_assets.root[out_assets.unit()] += out_assets.quantity()
        updated_assets += self._batcher_fee

        if in_assets.quantity() < self.available.quantity():
            txo = TransactionOutput(
                address=Address.decode(self.address),
                amount=asset_to_value(updated_assets),
                datum_hash=order_datum.hash(),
            )
        else:
            # Complete fill - burn beacon token
            tx_builder.add_minting_script(
                script=self.reference_utxo,
                redeemer=Redeemer(DjedCancelOrderRedeemer()),
            )
            if tx_builder.mint is None:
                tx_builder.mint = asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset
            else:
                tx_builder.mint += asset_to_value(
                    Assets(**{self.dex_nft.unit(): -1}),
                ).multi_asset

            payment_assets = Assets(lovelace=out_assets.quantity())

            txo = TransactionOutput(
                address=order_datum.owner_address.to_address(),
                amount=asset_to_value(payment_assets),
            )

        tx_builder.datums.update({order_datum.hash(): order_datum})
        return txo, order_datum


class DjedShenOrderBookBase(AbstractOrderBookState):
    """Base class for Djed/Shen order books sharing common functionality.

    Djed/Shen uses an order-based system modeled as an order book:
    - buy() creates a mint order (user sends ADA, requests tokens)
    - sell() creates a burn order (user sends tokens, requests ADA)
    """

    fee: int = 150  # 1.5% fee in basis points
    _deposit: Assets = Assets(lovelace=2_000_000)

    @classmethod
    def order_selector(cls) -> list[str]:
        """Order selection information."""
        return DjedShenOrderStateBase.order_selector()

    @classmethod
    def pool_selector(cls) -> PoolSelector:
        """Pool selection information."""
        return DjedShenOrderStateBase.pool_selector()

    @classmethod
    def oracle_selector(cls) -> PoolSelector:
        """Oracle selection information."""
        return DjedShenOrderStateBase.oracle_selector()

    @property
    def swap_forward(self) -> bool:
        """Returns if swap forwarding is enabled."""
        return True

    @classmethod
    def default_script_class(cls) -> type[PlutusV1Script] | type[PlutusV2Script]:
        """Get default script class."""
        return DjedShenOrderStateBase.default_script_class()

    @classmethod
    def order_datum_class(cls) -> type[PlutusData]:
        """Returns data class used for handling order datums."""
        return DjedShenOrderStateBase.order_datum_class()

    @property
    def stake_address(self) -> Address | None:
        """Return the staking address."""
        return None

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
        # Include datum in UTxO output - required for Ogmios evaluation
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
        # Include datum in UTxO output - required for Ogmios evaluation
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

        # Get script from address derived from policy ID
        script = get_backend().get_script_from_address(
            Address(
                payment_part=ScriptHash(
                    payload=bytes.fromhex(ORDER_NFT_POLICY),
                ),
            ),
        )
        return UTxO(
            input=TransactionInput(
                TransactionId(bytes.fromhex(script.tx_hash)),
                index=script.tx_index,
            ),
            output=TransactionOutput(
                address=Address.decode(script.address),
                amount=asset_to_value(script.assets),
                script=PlutusV2Script(bytes.fromhex(script.script)),
            ),
        )


class DjedOrderBook(DjedShenOrderBookBase):
    """Djed order book for Djed mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[DjedOrderState] | None = None,
    ) -> "DjedOrderBook":
        """Create Djed order book."""
        if orders is None:
            selector = DjedOrderState.pool_selector()

            result = get_backend().get_pool_utxos(
                limit=1000,
                historical=False,
                **selector.model_dump(),
            )

            orders = [
                DjedOrderState.model_validate(r.model_dump())
                for r in result
                if cls._is_djed_order(r)
            ]

        buy_orders = []  # Djed mint orders
        sell_orders = []  # Djed burn orders

        for order in orders:
            if order.inactive:
                continue

            price = order.price[0] / order.price[1]
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )

            if isinstance(order.order_datum.action, DjedMintAction):
                buy_orders.append(o)  # Mint = Buy
            else:  # DjedBurnAction
                sell_orders.append(o)  # Burn = Sell

        ob = DjedOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        # Limit orders per transaction (following GeniusYield pattern)
        ob.sell_book_full = ob.sell_book_full[:3]
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
    def _is_djed_order(cls, order_data: AbstractOrderState) -> bool:
        """Check if order is a Djed order (not Shen)."""
        try:
            # Parse datum to check action type
            datum = DjedOrderDatum.from_cbor(order_data.datum_cbor)
            return isinstance(datum.action, (DjedMintAction, DjedBurnAction))
        except (ValueError, AttributeError):
            return False

    @classmethod
    def buy(
        cls,
        amount: int,
        user_address: Address,
        tx_builder: TransactionBuilder,
    ) -> tuple[TransactionOutput, DjedOrderDatum]:
        """Create a Djed mint order (buy DJED with ADA).

        Based on open-djed create-mint-djed-order.ts pattern.

        Args:
            amount: Amount of DJED to mint (in lovelace-equivalent units)
            user_address: User's address to receive DJED
            tx_builder: TransactionBuilder to add the order to

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        # Get current slot FIRST (for consistency with oracle validity range)
        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180  # 3 minutes

        # Set validity interval immediately
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot

        # Convert TTL slot to POSIX ms for datum
        creation_time = _slot_to_posix_ms(ttl_slot)

        # Fetch UTxOs (oracle validity range should contain our validity interval)
        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
        minting_policy_ref = cls._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        # Calculate ADA needed: amount * (denom/num) * (1 + 1.5% fee)
        ada_amount = (amount * oracle_rate.denominator * 1015) // (
            oracle_rate.numerator * 1000
        )

        operator_fee = max(5_150_000, min(25_000_000, ada_amount * 1 // 400))

        # Total ADA to send
        total_ada = ada_amount + pool_datum.min_ada + operator_fee

        # Create order datum
        order_datum = DjedOrderDatum(
            action=DjedMintAction(djed_amount=amount, ada_amount=ada_amount),
            owner_address=PlutusFullAddress.from_address(user_address),
            oracle_rate=oracle_rate,
            creation_time=creation_time,
            order_nft=bytes.fromhex(ORDER_NFT_POLICY),
        )

        # Add reference inputs (oracle, pool, minting policy)
        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        # Order NFT unit (policy + name)
        order_nft_unit = ORDER_NFT_POLICY + "446a65644f726465725469636b6574"

        # Create output to order contract (includes order NFT)
        order_address = Address.decode(cls.order_selector()[0])
        output_assets = Assets(lovelace=total_ada)
        output_assets.root[order_nft_unit] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            user_address,
            pool_datum,
            minting_policy_ref,
            order_nft_unit,
        )

        return order_output, order_datum

    @classmethod
    def sell(
        cls,
        amount: int,
        user_address: Address,
        tx_builder: TransactionBuilder,
    ) -> tuple[TransactionOutput, DjedOrderDatum]:
        """Create a Djed burn order (sell DJED for ADA).

        Based on open-djed create-burn-djed-order.ts pattern.

        Args:
            amount: Amount of DJED to burn (in lovelace-equivalent units)
            user_address: User's address to receive ADA
            tx_builder: TransactionBuilder to add the order to

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        # Get current slot FIRST (for consistency with oracle validity range)
        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180  # 3 minutes

        # Set validity interval immediately
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot

        # Convert TTL slot to POSIX ms for datum
        creation_time = _slot_to_posix_ms(ttl_slot)

        # Fetch UTxOs (oracle validity range should contain our validity interval)
        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
        minting_policy_ref = cls._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        # Create order datum
        order_datum = DjedOrderDatum(
            action=DjedBurnAction(djed_amount=amount),
            owner_address=PlutusFullAddress.from_address(user_address),
            oracle_rate=oracle_rate,
            creation_time=creation_time,
            order_nft=bytes.fromhex(ORDER_NFT_POLICY),
        )

        # Add reference inputs (oracle, pool, minting policy)
        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        # Order NFT unit (policy + name)
        order_nft_unit = ORDER_NFT_POLICY + "446a65644f726465725469636b6574"

        # For burn orders, user sends DJED tokens + order NFT
        output_assets = Assets(**{DJED_TOKEN: amount})
        output_assets += Assets(lovelace=pool_datum.min_ada)
        output_assets.root[order_nft_unit] = 1

        # Create output to order contract
        order_address = Address.decode(cls.order_selector()[0])
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            user_address,
            pool_datum,
            minting_policy_ref,
            order_nft_unit,
        )

        return order_output, order_datum


class ShenOrderBook(DjedShenOrderBookBase):
    """Shen order book for Shen mint/burn operations."""

    @classmethod
    def get_book(
        cls,
        assets: Assets,
        orders: list[ShenOrderState] | None = None,
    ) -> "ShenOrderBook":
        """Create Shen order book."""
        if orders is None:
            selector = ShenOrderState.pool_selector()

            result = get_backend().get_pool_utxos(
                limit=1000,
                historical=False,
                **selector.model_dump(),
            )

            orders = [
                ShenOrderState.model_validate(r.model_dump())
                for r in result
                if cls._is_shen_order(r)
            ]

        buy_orders = []  # Shen mint orders
        sell_orders = []  # Shen burn orders

        for order in orders:
            if order.inactive:
                continue

            price = order.price[0] / order.price[1]
            o = OrderBookOrder(
                price=price,
                quantity=int(order.available.quantity()),
                state=order,
            )

            if isinstance(order.order_datum.action, ShenMintAction):
                buy_orders.append(o)  # Mint = Buy
            else:  # ShenBurnAction
                sell_orders.append(o)  # Burn = Sell

        ob = ShenOrderBook(
            assets=assets,
            plutus_v2=True,
            block_time=int(time.time()),
            block_index=0,
            sell_book_full=SellOrderBook(sell_orders),
            buy_book_full=BuyOrderBook(buy_orders),
        )

        # Limit orders per transaction (following GeniusYield pattern)
        ob.sell_book_full = ob.sell_book_full[:3]
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
    def _is_shen_order(cls, order_data: AbstractOrderState) -> bool:
        """Check if order is a Shen order (not Djed)."""
        try:
            # Parse datum to check action type
            datum = DjedOrderDatum.from_cbor(order_data.datum_cbor)
            return isinstance(datum.action, (ShenMintAction, ShenBurnAction))
        except (ValueError, AttributeError):
            return False

    @classmethod
    def buy(
        cls,
        amount: int,
        user_address: Address,
        tx_builder: TransactionBuilder,
    ) -> tuple[TransactionOutput, DjedOrderDatum]:
        """Create a Shen mint order (buy SHEN with ADA).

        Based on open-djed create-mint-shen-order.ts pattern.
        Shen price is based on excess reserves beyond DJED backing.

        Args:
            amount: Amount of SHEN to mint (in lovelace-equivalent units)
            user_address: User's address to receive SHEN
            tx_builder: TransactionBuilder to add the order to

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180  # 3 minutes

        # Set validity interval immediately
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot

        # Convert TTL slot to POSIX ms for datum
        creation_time = _slot_to_posix_ms(ttl_slot)

        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
        minting_policy_ref = cls._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        # Calculate Shen price (excess ADA per Shen)
        djed_backing_ada = (
            pool_datum.djed_in_circulation * oracle_rate.denominator
        ) // oracle_rate.numerator
        excess_ada = pool_datum.ada_in_reserve - djed_backing_ada

        # Calculate ADA needed: (amount * excess_ada / shen_in_circulation) * 1.015 fee
        ada_amount = (amount * excess_ada * 1015) // (
            pool_datum.shen_in_circulation * 1000
        )

        # Calculate operator fee
        operator_fee = max(5_150_000, min(25_000_000, ada_amount * 1 // 400))

        # Total ADA to send
        total_ada = ada_amount + pool_datum.min_ada + operator_fee

        # Create order datum
        order_datum = DjedOrderDatum(
            action=ShenMintAction(shen_amount=amount, ada_amount=ada_amount),
            owner_address=PlutusFullAddress.from_address(user_address),
            oracle_rate=oracle_rate,
            creation_time=creation_time,
            order_nft=bytes.fromhex(ORDER_NFT_POLICY),
        )

        # Add reference inputs (oracle, pool, minting policy)
        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        # Order NFT unit (policy + name)
        order_nft_unit = ORDER_NFT_POLICY + "446a65644f726465725469636b6574"

        # Create output to order contract (includes order NFT)
        order_address = Address.decode(cls.order_selector()[0])
        output_assets = Assets(lovelace=total_ada)
        output_assets.root[order_nft_unit] = 1
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            user_address,
            pool_datum,
            minting_policy_ref,
            order_nft_unit,
        )

        return order_output, order_datum

    @classmethod
    def sell(
        cls,
        amount: int,
        user_address: Address,
        tx_builder: TransactionBuilder,
    ) -> tuple[TransactionOutput, DjedOrderDatum]:
        """Create a Shen burn order (sell SHEN for ADA).

        Based on open-djed create-burn-shen-order.ts pattern.

        Args:
            amount: Amount of SHEN to burn (in lovelace-equivalent units)
            user_address: User's address to receive ADA
            tx_builder: TransactionBuilder to add the order to

        Returns:
            Tuple of (TransactionOutput to order contract, OrderDatum)
        """
        # Get current slot FIRST (for consistency with oracle validity range)
        now_slot = tx_builder.context.last_block_slot
        ttl_slot = now_slot + 180  # 3 minutes

        # Set validity interval immediately
        tx_builder.validity_start = now_slot
        tx_builder.ttl = ttl_slot

        # Convert TTL slot to POSIX ms for datum
        creation_time = _slot_to_posix_ms(ttl_slot)

        # Fetch UTxOs (oracle validity range should contain our validity interval)
        oracle_utxo, oracle_datum = cls._get_oracle_utxo_and_datum()
        pool_utxo, pool_datum = cls._get_pool_utxo_and_datum()
        minting_policy_ref = cls._get_minting_policy_ref_utxo()

        oracle_rate = oracle_datum.oracle_fields.ada_usd_exchange_rate

        # Create order datum
        order_datum = DjedOrderDatum(
            action=ShenBurnAction(shen_amount=amount),
            owner_address=PlutusFullAddress.from_address(user_address),
            oracle_rate=oracle_rate,
            creation_time=creation_time,
            order_nft=bytes.fromhex(ORDER_NFT_POLICY),
        )

        # Add reference inputs (oracle, pool, minting policy)
        tx_builder.reference_inputs.add(oracle_utxo)
        tx_builder.reference_inputs.add(pool_utxo)
        tx_builder.reference_inputs.add(minting_policy_ref)

        # Order NFT unit (policy + name)
        order_nft_unit = ORDER_NFT_POLICY + "446a65644f726465725469636b6574"

        # For burn orders, user sends SHEN tokens + order NFT
        output_assets = Assets(**{SHEN_TOKEN: amount})
        output_assets += Assets(lovelace=pool_datum.min_ada)
        output_assets.root[order_nft_unit] = 1

        # Create output to order contract
        order_address = Address.decode(cls.order_selector()[0])
        order_output = TransactionOutput(
            address=order_address,
            amount=asset_to_value(output_assets),
            datum=order_datum,
        )

        tx_builder.add_output(order_output)
        _finalize_order_tx(
            tx_builder,
            user_address,
            pool_datum,
            minting_policy_ref,
            order_nft_unit,
        )

        return order_output, order_datum
