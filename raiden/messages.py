from dataclasses import dataclass, field
from operator import attrgetter

from cachetools import LRUCache, cached
from eth_utils import big_endian_to_int

from raiden.constants import EMPTY_SIGNATURE, UINT64_MAX, UINT256_MAX
from raiden.encoding import messages
from raiden.encoding.format import buffer_for
from raiden.exceptions import InvalidProtocolMessage, InvalidSignature
from raiden.storage.serialization import DictSerializer
from raiden.transfer import channel
from raiden.transfer.architecture import SendMessageEvent
from raiden.transfer.balance_proof import (
    pack_balance_proof,
    pack_balance_proof_update,
    pack_reward_proof,
)
from raiden.transfer.events import SendProcessed
from raiden.transfer.identifiers import CanonicalIdentifier
from raiden.transfer.mediated_transfer.events import (
    SendBalanceProof,
    SendLockedTransfer,
    SendLockExpired,
    SendRefundTransfer,
    SendSecretRequest,
    SendSecretReveal,
)
from raiden.transfer.mediated_transfer.state import LockedTransferSignedState
from raiden.transfer.state import (
    BalanceProofSignedState,
    HashTimeLockState,
    NettingChannelState,
    balanceproof_from_envelope,
)
from raiden.transfer.utils import hash_balance_data
from raiden.utils import ishash, pex, sha3
from raiden.utils.signer import Signer, recover
from raiden.utils.typing import (
    MYPY_ANNOTATION,
    AdditionalHash,
    Address,
    BalanceHash,
    BlockExpiration,
    ChainID,
    ChannelID,
    ClassVar,
    Dict,
    FeeAmount,
    InitiatorAddress,
    Locksroot,
    MessageID,
    Nonce,
    Optional,
    PaymentAmount,
    PaymentID,
    PaymentWithFeeAmount,
    RaidenProtocolVersion,
    Secret,
    SecretHash,
    Signature,
    TargetAddress,
    TokenAddress,
    TokenAmount,
    TokenNetworkAddress,
    Type,
)

__all__ = (
    "Delivered",
    "EnvelopeMessage",
    "Lock",
    "LockedTransfer",
    "LockedTransferBase",
    "LockExpired",
    "Message",
    "Ping",
    "Pong",
    "Processed",
    "RefundTransfer",
    "RequestMonitoring",
    "RevealSecret",
    "SecretRequest",
    "SignedBlindedBalanceProof",
    "SignedMessage",
    "ToDevice",
    "Unlock",
    "UpdatePFS",
    "decode",
    "from_dict",
    "message_from_sendevent",
)


_senders_cache = LRUCache(maxsize=128)
_hashes_cache = LRUCache(maxsize=128)
_lock_bytes_cache = LRUCache(maxsize=128)


def assert_envelope_values(
    nonce: int,
    channel_identifier: ChannelID,
    transferred_amount: TokenAmount,
    locked_amount: TokenAmount,
    locksroot: Locksroot,
):
    if nonce <= 0:
        raise ValueError("nonce cannot be zero or negative")

    if nonce > UINT64_MAX:
        raise ValueError("nonce is too large")

    if channel_identifier < 0:
        raise ValueError("channel id cannot be negative")

    if channel_identifier > UINT256_MAX:
        raise ValueError("channel id is too large")

    if transferred_amount < 0:
        raise ValueError("transferred_amount cannot be negative")

    if transferred_amount > UINT256_MAX:
        raise ValueError("transferred_amount is too large")

    if locked_amount < 0:
        raise ValueError("locked_amount cannot be negative")

    if locked_amount > UINT256_MAX:
        raise ValueError("locked_amount is too large")

    if len(locksroot) != 32:
        raise ValueError("locksroot must have length 32")


def assert_transfer_values(payment_identifier, token, recipient):
    if payment_identifier < 0:
        raise ValueError("payment_identifier cannot be negative")

    if payment_identifier > UINT64_MAX:
        raise ValueError("payment_identifier is too large")

    if len(token) != 20:
        raise ValueError("token is an invalid address")

    if len(recipient) != 20:
        raise ValueError("recipient is an invalid address")


def decode(data: bytes) -> "Message":
    try:
        klass = CMDID_TO_CLASS[data[0]]
    except KeyError:
        raise InvalidProtocolMessage("Invalid message type (CMDID = {})".format(hex(data[0])))
    return klass.decode(data)


def from_dict(data: dict) -> "Message":
    try:
        CLASSNAME_TO_CLASS[data["type"]]
    except KeyError:
        if "type" in data:
            raise InvalidProtocolMessage(
                'Invalid message type (data["type"] = {})'.format(data["type"])
            ) from None
        else:
            raise InvalidProtocolMessage(
                "Invalid message data. Can not find the data type"
            ) from None
    return DictSerializer.serialize(data)


def message_from_sendevent(send_event: SendMessageEvent) -> "Message":
    if type(send_event) == SendLockedTransfer:
        assert isinstance(send_event, SendLockedTransfer), MYPY_ANNOTATION
        message = LockedTransfer.from_event(send_event)
    elif type(send_event) == SendSecretReveal:
        assert isinstance(send_event, SendSecretReveal), MYPY_ANNOTATION
        message = RevealSecret.from_event(send_event)
    elif type(send_event) == SendBalanceProof:
        assert isinstance(send_event, SendBalanceProof), MYPY_ANNOTATION
        message = Unlock.from_event(send_event)
    elif type(send_event) == SendSecretRequest:
        assert isinstance(send_event, SendSecretRequest), MYPY_ANNOTATION
        message = SecretRequest.from_event(send_event)
    elif type(send_event) == SendRefundTransfer:
        assert isinstance(send_event, SendRefundTransfer), MYPY_ANNOTATION
        message = RefundTransfer.from_event(send_event)
    elif type(send_event) == SendLockExpired:
        assert isinstance(send_event, SendLockExpired), MYPY_ANNOTATION
        message = LockExpired.from_event(send_event)
    elif type(send_event) == SendProcessed:
        assert isinstance(send_event, SendProcessed), MYPY_ANNOTATION
        message = Processed.from_event(send_event)
    else:
        raise ValueError(f"Unknown event type {send_event}")

    return message


@dataclass(repr=False, eq=False)
class Message:
    # Needs to be set by a subclass
    cmdid: ClassVar[int]

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.hash == other.hash

    def __hash__(self):
        return big_endian_to_int(self.hash)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "<{klass} [msghash={msghash}]>".format(
            klass=self.__class__.__name__, msghash=pex(self.hash)
        )

    @property
    def hash(self):
        packed = self.packed()
        return sha3(packed.data)

    @classmethod
    def decode(cls, data):
        packed = messages.wrap(data)
        return cls.unpack(packed)

    def encode(self) -> bytes:
        packed = self.packed()
        return bytes(packed.data)

    def packed(self):
        klass = messages.CMDID_MESSAGE[self.cmdid]
        data = buffer_for(klass)
        data[0] = self.cmdid
        packed = klass(data)
        self.pack(packed)

        return packed

    @classmethod
    def unpack(cls, packed):
        raise NotImplementedError("Method needs to be implemented in a subclass.")

    def pack(self, packed) -> None:
        raise NotImplementedError("Method needs to be implemented in a subclass.")


@dataclass(repr=False, eq=False)
class AuthenticatedMessage(Message):
    """ Message, that has a sender. """

    def sender(self) -> Address:
        raise NotImplementedError("Property needs to be implemented in subclass.")


@dataclass(repr=False, eq=False)
class SignedMessage(AuthenticatedMessage):
    # signing is a bit problematic, we need to pack the data to sign, but the
    # current API assumes that signing is called before, this can be improved
    # by changing the order to packing then signing
    signature: Signature

    def _data_to_sign(self) -> bytes:
        """ Return the binary data to be/which was signed """
        packed = self.packed()

        field = type(packed).fields_spec[-1]
        assert field.name == "signature", "signature is not the last field"

        # this slice must be from the end of the buffer
        return packed.data[: -field.size_bytes]

    def sign(self, signer: Signer):
        """ Sign message using signer. """
        message_data = self._data_to_sign()
        self.signature = signer.sign(data=message_data)

    @property  # type: ignore
    @cached(_senders_cache, key=attrgetter("signature"))
    def sender(self) -> Optional[Address]:
        if not self.signature:
            return None
        data_that_was_signed = self._data_to_sign()
        message_signature = self.signature

        try:
            address: Optional[Address] = recover(
                data=data_that_was_signed, signature=message_signature
            )
        except InvalidSignature:
            address = None
        return address

    @classmethod
    def decode(cls, data):
        packed = messages.wrap(data)

        if packed is None:
            return None

        return cls.unpack(packed)


@dataclass(repr=False, eq=False)
class RetrieableMessage:
    """ Message, that supports a retry-queue. """

    message_identifier: MessageID


@dataclass(repr=False, eq=False)
class SignedRetrieableMessage(SignedMessage, RetrieableMessage):
    """ Mixin of SignedMessage and RetrieableMessage. """

    pass


@dataclass(repr=False, eq=False)
class EnvelopeMessage(SignedRetrieableMessage):
    chain_id: ChainID
    nonce: Nonce
    transferred_amount: TokenAmount
    locked_amount: TokenAmount
    locksroot: Locksroot
    channel_identifier: ChannelID
    token_network_address: TokenNetworkAddress

    def __post_init__(self):
        assert_envelope_values(
            self.nonce,
            self.channel_identifier,
            self.transferred_amount,
            self.locked_amount,
            self.locksroot,
        )

    @property
    def message_hash(self):
        packed = self.packed()
        klass = type(packed)

        field = klass.fields_spec[-1]
        assert field.name == "signature", "signature is not the last field"

        data = packed.data
        message_data = data[: -field.size_bytes]
        message_hash = sha3(message_data)

        return message_hash

    def _data_to_sign(self) -> bytes:
        balance_hash = hash_balance_data(
            self.transferred_amount, self.locked_amount, self.locksroot
        )
        balance_proof_packed = pack_balance_proof(
            nonce=self.nonce,
            balance_hash=balance_hash,
            additional_hash=self.message_hash,
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.chain_id,
                token_network_address=self.token_network_address,
                channel_identifier=self.channel_identifier,
            ),
        )
        return balance_proof_packed


@dataclass(repr=False, eq=False)
class Processed(SignedRetrieableMessage):
    """ All accepted messages should be confirmed by a `Processed` message which echoes the
    orginals Message hash.
    """

    # FIXME: Processed should _not_ be SignedRetrieableMessage, but only SignedMessage
    cmdid: ClassVar[int] = messages.PROCESSED

    message_identifier: MessageID

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        processed = cls(message_identifier=packed.message_identifier, signature=packed.signature)
        return processed

    def pack(self, packed) -> None:
        packed.message_identifier = self.message_identifier
        packed.signature = self.signature

    @classmethod
    def from_event(cls, event):
        return cls(message_identifier=event.message_identifier, signature=EMPTY_SIGNATURE)


@dataclass(repr=False, eq=False)
class ToDevice(SignedMessage):
    """
    Message, which can be directly sent to all devices of a node known by matrix,
    no room required. Messages which are supposed to be sent via transport.sent_to_device must
    subclass.
    """

    cmdid: ClassVar[int] = messages.TODEVICE

    message_identifier: MessageID

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        to_device = cls(message_identifier=packed.message_identifier, signature=packed.signature)
        return to_device

    def pack(self, packed) -> None:
        packed.message_identifier = self.message_identifier
        packed.signature = self.signature


@dataclass(repr=False, eq=False)
class Delivered(SignedMessage):
    """ Message used to inform the partner node that a message was received *and*
    persisted.
    """

    cmdid: ClassVar[int] = messages.DELIVERED

    delivered_message_identifier: MessageID

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        delivered = cls(
            delivered_message_identifier=packed.delivered_message_identifier,
            signature=packed.signature,
        )
        return delivered

    def pack(self, packed) -> None:
        packed.delivered_message_identifier = self.delivered_message_identifier
        packed.signature = self.signature


@dataclass(repr=False, eq=False)
class Pong(SignedMessage):
    """ Response to a Ping message. """

    cmdid: ClassVar[int] = messages.PONG

    nonce: Nonce

    @staticmethod
    def unpack(packed):
        pong = Pong(nonce=packed.nonce, signature=packed.signature)
        return pong

    def pack(self, packed) -> None:
        packed.nonce = self.nonce
        packed.signature = self.signature


@dataclass(repr=False, eq=False)
class Ping(SignedMessage):
    """ Healthcheck message. """

    cmdid: ClassVar[int] = messages.PING

    nonce: Nonce
    current_protocol_version: RaidenProtocolVersion

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        ping = cls(
            nonce=packed.nonce,
            current_protocol_version=packed.current_protocol_version,
            signature=packed.signature,
        )
        return ping

    def pack(self, packed) -> None:
        packed.nonce = self.nonce
        packed.current_protocol_version = self.current_protocol_version
        packed.signature = self.signature


@dataclass(repr=False, eq=False)
class SecretRequest(SignedRetrieableMessage):
    """ Requests the secret which unlocks a secrethash. """

    cmdid: ClassVar[int] = messages.SECRETREQUEST

    payment_identifier: PaymentID
    secrethash: SecretHash
    amount: PaymentAmount
    expiration: BlockExpiration

    @classmethod
    def unpack(cls, packed):
        secret_request = cls(
            message_identifier=packed.message_identifier,
            payment_identifier=packed.payment_identifier,
            secrethash=packed.secrethash,
            amount=packed.amount,
            expiration=packed.expiration,
            signature=packed.signature,
        )
        return secret_request

    def pack(self, packed) -> None:
        packed.message_identifier = self.message_identifier
        packed.payment_identifier = self.payment_identifier
        packed.secrethash = self.secrethash
        packed.amount = self.amount
        packed.expiration = self.expiration
        packed.signature = self.signature

    @classmethod
    def from_event(cls, event):
        # pylint: disable=unexpected-keyword-arg
        return cls(
            message_identifier=event.message_identifier,
            payment_identifier=event.payment_identifier,
            secrethash=event.secrethash,
            amount=event.amount,
            expiration=event.expiration,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class Unlock(EnvelopeMessage):
    """ Message used to do state changes on a partner Raiden Channel.

    Locksroot changes need to be synchronized among both participants, the
    protocol is for only the side unlocking to send the Unlock message allowing
    the other party to claim the unlocked lock.
    """

    cmdid: ClassVar[int] = messages.UNLOCK

    payment_identifier: PaymentID
    secret: Secret = field(repr=False)

    def __post_init__(self):
        super().__post_init__()
        if self.payment_identifier < 0:
            raise ValueError("payment_identifier cannot be negative")

        if self.payment_identifier > UINT64_MAX:
            raise ValueError("payment_identifier is too large")

        if len(self.secret) != 32:
            raise ValueError("secret must have 32 bytes")

    @property  # type: ignore
    @cached(_hashes_cache, key=attrgetter("secret"))
    def secrethash(self):
        return sha3(self.secret)

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        secret = cls(
            chain_id=packed.chain_id,
            message_identifier=packed.message_identifier,
            payment_identifier=packed.payment_identifier,
            nonce=packed.nonce,
            token_network_address=packed.token_network_address,
            channel_identifier=packed.channel_identifier,
            transferred_amount=packed.transferred_amount,
            locked_amount=packed.locked_amount,
            locksroot=packed.locksroot,
            secret=packed.secret,
            signature=packed.signature,
        )
        return secret

    def pack(self, packed) -> None:
        packed.chain_id = self.chain_id
        packed.message_identifier = self.message_identifier
        packed.payment_identifier = self.payment_identifier
        packed.nonce = self.nonce
        packed.token_network_address = self.token_network_address
        packed.channel_identifier = self.channel_identifier
        packed.transferred_amount = self.transferred_amount
        packed.locked_amount = self.locked_amount
        packed.locksroot = self.locksroot
        packed.secret = self.secret
        packed.signature = self.signature

    @classmethod
    def from_event(cls, event):
        balance_proof = event.balance_proof
        # pylint: disable=unexpected-keyword-arg
        return cls(
            chain_id=balance_proof.chain_id,
            message_identifier=event.message_identifier,
            payment_identifier=event.payment_identifier,
            nonce=balance_proof.nonce,
            token_network_address=balance_proof.token_network_address,
            channel_identifier=balance_proof.channel_identifier,
            transferred_amount=balance_proof.transferred_amount,
            locked_amount=balance_proof.locked_amount,
            locksroot=balance_proof.locksroot,
            secret=event.secret,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class RevealSecret(SignedRetrieableMessage):
    """Message used to reveal a secret to party known to have interest in it.

    This message is not sufficient for state changes in the raiden Channel, the
    reason is that a node participating in split transfer or in both mediated
    transfer for an exchange might can reveal the secret to it's partners, but
    that must not update the internal channel state.
    """

    cmdid: ClassVar[int] = messages.REVEALSECRET

    secret: Secret = field(repr=False)

    @property  # type: ignore
    @cached(_hashes_cache, key=attrgetter("secret"))
    def secrethash(self):
        return sha3(self.secret)

    @classmethod
    def unpack(cls, packed):
        reveal_secret = RevealSecret(
            message_identifier=packed.message_identifier,
            secret=packed.secret,
            signature=packed.signature,
        )
        return reveal_secret

    def pack(self, packed) -> None:
        packed.message_identifier = self.message_identifier
        packed.secret = self.secret
        packed.signature = self.signature

    @classmethod
    def from_event(cls, event):
        # pylint: disable=unexpected-keyword-arg
        return cls(
            message_identifier=event.message_identifier,
            secret=event.secret,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class Lock:
    """ Describes a locked `amount`.

    Args:
        amount: Amount of the token being transferred.
        expiration: Highest block_number until which the transfer can be settled
        secrethash: Hashed secret `sha3(secret)` used to register the transfer,
        the real `secret` is necessary to release the locked amount.
    """

    # Lock is not a message, it is a serializable structure that is reused in
    # some messages
    amount: PaymentWithFeeAmount
    expiration: BlockExpiration
    secrethash: SecretHash

    def __post_init__(self):
        # guarantee that `amount` can be serialized using the available bytes
        # in the fixed length format
        if self.amount < 0:
            raise ValueError(f"amount {self.amount} needs to be positive")

        if self.amount > UINT256_MAX:
            raise ValueError(f"amount {self.amount} is too large")

        if self.expiration < 0:
            raise ValueError(f"expiration {self.expiration} needs to be positive")

        if self.expiration > UINT256_MAX:
            raise ValueError(f"expiration {self.expiration} is too large")

        if not ishash(self.secrethash):
            raise ValueError("secrethash {self.secrethash} is not a valid hash")

    @property  # type: ignore
    @cached(_lock_bytes_cache, key=attrgetter("amount", "expiration", "secrethash"))
    def as_bytes(self):
        packed = messages.Lock(buffer_for(messages.Lock))
        packed.amount = self.amount
        packed.expiration = self.expiration
        packed.secrethash = self.secrethash

        # convert bytearray to bytes
        return bytes(packed.data)

    @property  # type: ignore
    @cached(_hashes_cache, key=attrgetter("as_bytes"))
    def lockhash(self):
        return sha3(self.as_bytes)

    @classmethod
    def from_bytes(cls, serialized):
        packed = messages.Lock(serialized)

        # pylint: disable=unexpected-keyword-arg
        return cls(
            amount=packed.amount, expiration=packed.expiration, secrethash=packed.secrethash
        )


@dataclass(repr=False, eq=False)
class LockedTransferBase(EnvelopeMessage):
    """ A transfer which signs that the partner can claim `locked_amount` if
    she knows the secret to `secrethash`.

    The token amount is implicitly represented in the `locksroot` and won't be
    reflected in the `transferred_amount` until the secret is revealed.

    This signs Carol, that she can claim locked_amount from Bob if she knows
    the secret to secrethash.

    If the secret to secrethash becomes public, but Bob fails to sign Carol a
    netted balance, with an updated rootlock which reflects the deletion of the
    lock, then Carol can request settlement on chain by providing: any signed
    [nonce, token, balance, recipient, locksroot, ...] along a merkle proof
    from locksroot to the not yet netted formerly locked amount.
    """

    payment_identifier: PaymentID
    token: TokenAddress
    recipient: Address
    lock: Lock

    def __post_init__(self):
        super().__post_init__()
        assert_transfer_values(self.payment_identifier, self.token, self.recipient)

    @classmethod
    def unpack(cls, packed):
        lock = Lock(
            amount=packed.amount, expiration=packed.expiration, secrethash=packed.secrethash
        )

        # pylint: disable=unexpected-keyword-arg
        locked_transfer = cls(
            chain_id=packed.chain_id,
            message_identifier=packed.message_identifier,
            payment_identifier=packed.payment_identifier,
            nonce=packed.nonce,
            token_network_address=packed.token_network_address,
            token=packed.token,
            channel_identifier=packed.channel_identifier,
            transferred_amount=packed.transferred_amount,
            recipient=packed.recipient,
            locked_amount=packed.locked_amount,
            locksroot=packed.locksroot,
            lock=lock,
            signature=packed.signature,
        )
        return locked_transfer

    def pack(self, packed) -> None:
        packed.chain_id = self.chain_id
        packed.message_identifier = self.message_identifier
        packed.payment_identifier = self.payment_identifier
        packed.nonce = self.nonce
        packed.token_network_address = self.token_network_address
        packed.token = self.token
        packed.channel_identifier = self.channel_identifier
        packed.transferred_amount = self.transferred_amount
        packed.locked_amount = self.locked_amount
        packed.recipient = self.recipient
        packed.locksroot = self.locksroot

        lock = self.lock
        packed.amount = lock.amount
        packed.expiration = lock.expiration
        packed.secrethash = lock.secrethash

        packed.signature = self.signature


@dataclass(repr=False, eq=False)
class LockedTransfer(LockedTransferBase):
    """
    A LockedTransfer has a `target` address to which a chain of transfers shall
    be established. Here the `secrethash` is mandatory.

    `fee` is the remaining fee a recipient shall use to complete the mediated transfer.
    The recipient can deduct his own fee from the amount and lower `fee` to the remaining fee.
    Just as the recipient can fail to forward at all, or the assumed amount,
    it can deduct a too high fee, but this would render completion of the transfer unlikely.

    The initiator of a mediated transfer will calculate fees based on the likely fees along the
    path. Note, it can not determine the path, as it does not know which nodes are available.

    Initial `amount` should be expected received amount + fees.

    Fees are always payable by the initiator.

    `initiator` is the party that knows the secret to the `secrethash`
    """

    cmdid: ClassVar[int] = messages.LOCKEDTRANSFER

    target: TargetAddress
    initiator: InitiatorAddress
    fee: int

    def __post_init__(self):
        super().__post_init__()

        if len(self.target) != 20:
            raise ValueError("target is an invalid address")

        if len(self.initiator) != 20:
            raise ValueError("initiator is an invalid address")

        if self.fee > UINT256_MAX:
            raise ValueError("fee is too large")

    @classmethod
    def unpack(cls, packed):
        lock = Lock(
            amount=packed.amount, expiration=packed.expiration, secrethash=packed.secrethash
        )

        # pylint: disable=unexpected-keyword-arg
        mediated_transfer = cls(
            chain_id=packed.chain_id,
            message_identifier=packed.message_identifier,
            payment_identifier=packed.payment_identifier,
            nonce=packed.nonce,
            token_network_address=packed.token_network_address,
            token=packed.token,
            channel_identifier=packed.channel_identifier,
            transferred_amount=packed.transferred_amount,
            locked_amount=packed.locked_amount,
            recipient=packed.recipient,
            locksroot=packed.locksroot,
            lock=lock,
            target=packed.target,
            initiator=packed.initiator,
            fee=packed.fee,
            signature=packed.signature,
        )
        return mediated_transfer

    def pack(self, packed) -> None:
        packed.chain_id = self.chain_id
        packed.message_identifier = self.message_identifier
        packed.payment_identifier = self.payment_identifier
        packed.nonce = self.nonce
        packed.token_network_address = self.token_network_address
        packed.token = self.token
        packed.channel_identifier = self.channel_identifier
        packed.transferred_amount = self.transferred_amount
        packed.locked_amount = self.locked_amount
        packed.recipient = self.recipient
        packed.locksroot = self.locksroot
        packed.target = self.target
        packed.initiator = self.initiator
        packed.fee = self.fee

        lock = self.lock
        packed.amount = lock.amount
        packed.expiration = lock.expiration
        packed.secrethash = lock.secrethash

        packed.signature = self.signature

    @classmethod
    def from_event(cls, event: SendLockedTransfer) -> "LockedTransfer":
        transfer = event.transfer
        balance_proof = transfer.balance_proof
        lock = Lock(
            amount=transfer.lock.amount,
            expiration=transfer.lock.expiration,
            secrethash=transfer.lock.secrethash,
        )
        fee = 0

        # pylint: disable=unexpected-keyword-arg
        return cls(
            chain_id=balance_proof.chain_id,
            message_identifier=event.message_identifier,
            payment_identifier=transfer.payment_identifier,
            nonce=balance_proof.nonce,
            token_network_address=balance_proof.token_network_address,
            token=transfer.token,
            channel_identifier=balance_proof.channel_identifier,
            transferred_amount=balance_proof.transferred_amount,
            locked_amount=balance_proof.locked_amount,
            recipient=event.recipient,
            locksroot=balance_proof.locksroot,
            lock=lock,
            target=transfer.target,
            initiator=transfer.initiator,
            fee=fee,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class RefundTransfer(LockedTransfer):
    """ A special LockedTransfer sent from a payee to a payer indicating that
    no route is available, this transfer will effectively refund the payer the
    transfer amount allowing him to try a new path to complete the transfer.
    """

    cmdid: ClassVar[int] = messages.REFUNDTRANSFER

    @classmethod
    def unpack(cls, packed):
        lock = Lock(
            amount=packed.amount, expiration=packed.expiration, secrethash=packed.secrethash
        )

        # pylint: disable=unexpected-keyword-arg
        locked_transfer = cls(
            chain_id=packed.chain_id,
            message_identifier=packed.message_identifier,
            payment_identifier=packed.payment_identifier,
            nonce=packed.nonce,
            token_network_address=packed.token_network_address,
            token=packed.token,
            channel_identifier=packed.channel_identifier,
            transferred_amount=packed.transferred_amount,
            locked_amount=packed.locked_amount,
            recipient=packed.recipient,
            locksroot=packed.locksroot,
            lock=lock,
            target=packed.target,
            initiator=packed.initiator,
            fee=packed.fee,
            signature=packed.signature,
        )
        return locked_transfer

    @classmethod
    def from_event(cls, event):
        transfer = event.transfer
        balance_proof = transfer.balance_proof
        lock = Lock(
            amount=transfer.lock.amount,
            expiration=transfer.lock.expiration,
            secrethash=transfer.lock.secrethash,
        )
        fee = 0

        # pylint: disable=unexpected-keyword-arg
        return cls(
            chain_id=balance_proof.chain_id,
            message_identifier=event.message_identifier,
            payment_identifier=transfer.payment_identifier,
            nonce=balance_proof.nonce,
            token_network_address=balance_proof.token_network_address,
            token=transfer.token,
            channel_identifier=balance_proof.channel_identifier,
            transferred_amount=balance_proof.transferred_amount,
            locked_amount=balance_proof.locked_amount,
            recipient=event.recipient,
            locksroot=balance_proof.locksroot,
            lock=lock,
            target=transfer.target,
            initiator=transfer.initiator,
            fee=fee,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class LockExpired(EnvelopeMessage):
    """Message used to notify opposite channel participant that a lock has
    expired.
    """

    cmdid: ClassVar[int] = messages.LOCKEXPIRED

    recipient: Address
    secrethash: SecretHash

    @classmethod
    def unpack(cls, packed):
        # pylint: disable=unexpected-keyword-arg
        transfer = cls(
            chain_id=packed.chain_id,
            nonce=packed.nonce,
            message_identifier=packed.message_identifier,
            token_network_address=packed.token_network_address,
            channel_identifier=packed.channel_identifier,
            transferred_amount=packed.transferred_amount,
            recipient=packed.recipient,
            locked_amount=packed.locked_amount,
            locksroot=packed.locksroot,
            secrethash=packed.secrethash,
            signature=packed.signature,
        )

        return transfer

    def pack(self, packed) -> None:
        packed.chain_id = self.chain_id
        packed.nonce = self.nonce
        packed.message_identifier = self.message_identifier
        packed.token_network_address = self.token_network_address
        packed.channel_identifier = self.channel_identifier
        packed.transferred_amount = self.transferred_amount
        packed.locked_amount = self.locked_amount
        packed.recipient = self.recipient
        packed.locksroot = self.locksroot
        packed.secrethash = self.secrethash
        packed.signature = self.signature

    @classmethod
    def from_event(cls, event):
        balance_proof = event.balance_proof

        # pylint: disable=unexpected-keyword-arg
        return cls(
            chain_id=balance_proof.chain_id,
            nonce=balance_proof.nonce,
            token_network_address=balance_proof.token_network_address,
            channel_identifier=balance_proof.channel_identifier,
            transferred_amount=balance_proof.transferred_amount,
            locked_amount=balance_proof.locked_amount,
            locksroot=balance_proof.locksroot,
            message_identifier=event.message_identifier,
            recipient=event.recipient,
            secrethash=event.secrethash,
            signature=EMPTY_SIGNATURE,
        )


@dataclass(repr=False, eq=False)
class SignedBlindedBalanceProof:
    """Message sub-field `onchain_balance_proof` for `RequestMonitoring`.
    """

    channel_identifier: ChannelID
    token_network_address: TokenNetworkAddress
    nonce: Nonce
    additional_hash: AdditionalHash
    chain_id: ChainID
    balance_hash: BalanceHash
    signature: Signature
    non_closing_signature: Optional[Signature] = field(default=EMPTY_SIGNATURE)

    def __post_init__(self):
        if self.signature == EMPTY_SIGNATURE:
            raise ValueError("balance proof is not signed")

    @classmethod
    def from_balance_proof_signed_state(
        cls, balance_proof: BalanceProofSignedState
    ) -> "SignedBlindedBalanceProof":
        if not isinstance(balance_proof, BalanceProofSignedState):
            raise ValueError(
                "balance_proof is not an instance of the type BalanceProofSignedState"
            )

        # pylint: disable=unexpected-keyword-arg
        return cls(
            channel_identifier=balance_proof.channel_identifier,
            token_network_address=balance_proof.token_network_address,
            nonce=balance_proof.nonce,
            additional_hash=balance_proof.message_hash,
            chain_id=balance_proof.chain_id,
            signature=balance_proof.signature,
            balance_hash=hash_balance_data(
                balance_proof.transferred_amount,
                balance_proof.locked_amount,
                balance_proof.locksroot,
            ),
        )

    def _data_to_sign(self) -> bytes:
        packed = pack_balance_proof_update(
            nonce=self.nonce,
            balance_hash=self.balance_hash,
            additional_hash=self.additional_hash,
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.chain_id,
                token_network_address=self.token_network_address,
                channel_identifier=self.channel_identifier,
            ),
            partner_signature=self.signature,
        )
        return packed

    def _sign(self, signer: Signer) -> Signature:
        """Internal function for the overall `sign` function of `RequestMonitoring`.
        """
        # Important: we don't write the signature to `.signature`
        data = self._data_to_sign()
        return signer.sign(data)


@dataclass(repr=False, eq=False)
class RequestMonitoring(SignedMessage):
    """Message to request channel watching from a monitoring service.
    Spec:
        https://raiden-network-specification.readthedocs.io/en/latest/monitoring_service.html\
#monitor-request
    """

    balance_proof: SignedBlindedBalanceProof
    reward_amount: TokenAmount
    non_closing_signature: Optional[Signature] = None

    def __post_init__(self):
        if self.balance_proof is None:
            raise ValueError("no balance proof given")

        if not isinstance(self.balance_proof, SignedBlindedBalanceProof):
            raise ValueError("onchain_balance_proof is not a SignedBlindedBalanceProof")

    @classmethod
    def from_balance_proof_signed_state(
        cls, balance_proof: BalanceProofSignedState, reward_amount: TokenAmount
    ) -> "RequestMonitoring":
        if not isinstance(balance_proof, BalanceProofSignedState):
            raise ValueError(
                "balance_proof is not an instance of the type BalanceProofSignedState"
            )

        onchain_balance_proof = SignedBlindedBalanceProof.from_balance_proof_signed_state(
            balance_proof=balance_proof
        )
        # pylint: disable=unexpected-keyword-arg
        return cls(
            balance_proof=onchain_balance_proof,
            reward_amount=reward_amount,
            signature=EMPTY_SIGNATURE,
        )
        return cls(onchain_balance_proof=onchain_balance_proof, reward_amount=reward_amount)

    @property
    def reward_proof_signature(self) -> Optional[Signature]:
        return self.signature

    def _data_to_sign(self) -> bytes:
        """ Return the binary data to be/which was signed """
        packed = pack_reward_proof(
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.balance_proof.chain_id,
                token_network_address=self.balance_proof.token_network_address,
                channel_identifier=self.balance_proof.channel_identifier,
            ),
            reward_amount=self.reward_amount,
            nonce=self.balance_proof.nonce,
        )
        return packed

    def sign(self, signer: Signer):
        """This method signs twice:
            - the `non_closing_signature` for the balance proof update
            - the `reward_proof_signature` for the monitoring request
        """
        self.non_closing_signature = self.balance_proof._sign(signer)
        message_data = self._data_to_sign()
        self.signature = signer.sign(data=message_data)

    def packed(self) -> bytes:
        klass = messages.RequestMonitoring
        data = buffer_for(klass)
        packed = klass(data)
        self.pack(packed)
        return packed

    def pack(self, packed) -> None:
        if self.non_closing_signature is None:
            raise ValueError("non_closing_signature missing, did you forget to sign()?")
        if self.reward_proof_signature is None:
            raise ValueError("reward_proof_signature missing, did you forget to sign()?")
        packed.nonce = self.balance_proof.nonce
        packed.chain_id = self.balance_proof.chain_id
        packed.token_network_address = self.balance_proof.token_network_address
        packed.channel_identifier = self.balance_proof.channel_identifier
        packed.balance_hash = self.balance_proof.balance_hash
        packed.additional_hash = self.balance_proof.additional_hash
        packed.signature = self.balance_proof.signature
        packed.non_closing_signature = self.non_closing_signature
        packed.reward_amount = self.reward_amount
        packed.reward_proof_signature = self.reward_proof_signature

    @classmethod
    def unpack(cls, packed) -> "RequestMonitoring":
        onchain_balance_proof = SignedBlindedBalanceProof(
            nonce=packed.nonce,
            chain_id=packed.chain_id,
            token_network_address=packed.token_network_address,
            channel_identifier=packed.channel_identifier,
            balance_hash=packed.balance_hash,
            additional_hash=packed.additional_hash,
            signature=packed.signature,
        )
        # pylint: disable=unexpected-keyword-arg
        monitoring_request = cls(
            balance_proof=onchain_balance_proof,
            non_closing_signature=packed.non_closing_signature,
            reward_amount=packed.reward_amount,
            signature=packed.reward_proof_signature,
        )
        return monitoring_request

    def verify_request_monitoring(
        self, partner_address: Address, requesting_address: Address
    ) -> bool:
        """ One should only use this method to verify integrity and signatures of a
        RequestMonitoring message. """
        if not self.non_closing_signature:
            return False

        balance_proof_data = pack_balance_proof(
            nonce=self.balance_proof.nonce,
            balance_hash=self.balance_proof.balance_hash,
            additional_hash=self.balance_proof.additional_hash,
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.balance_proof.chain_id,
                token_network_address=self.balance_proof.token_network_address,
                channel_identifier=self.balance_proof.channel_identifier,
            ),
        )
        blinded_data = pack_balance_proof_update(
            nonce=self.balance_proof.nonce,
            balance_hash=self.balance_proof.balance_hash,
            additional_hash=self.balance_proof.additional_hash,
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.balance_proof.chain_id,
                token_network_address=self.balance_proof.token_network_address,
                channel_identifier=self.balance_proof.channel_identifier,
            ),
            partner_signature=self.balance_proof.signature,
        )
        reward_proof_data = pack_reward_proof(
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=self.balance_proof.chain_id,
                token_network_address=self.balance_proof.token_network_address,
                channel_identifier=self.balance_proof.channel_identifier,
            ),
            reward_amount=self.reward_amount,
            nonce=self.balance_proof.nonce,
        )
        reward_proof_signature = self.reward_proof_signature or EMPTY_SIGNATURE
        return (
            recover(balance_proof_data, self.balance_proof.signature) == partner_address
            and recover(blinded_data, self.non_closing_signature) == requesting_address
            and recover(reward_proof_data, reward_proof_signature) == requesting_address
        )


@dataclass(repr=False, eq=False)
class UpdatePFS(SignedMessage):
    """ Message to inform a pathfinding service about a capacity change. """

    canonical_identifier: CanonicalIdentifier
    updating_participant: Address
    other_participant: Address
    updating_nonce: Nonce
    other_nonce: Nonce
    updating_capacity: TokenAmount
    other_capacity: TokenAmount
    reveal_timeout: int
    mediation_fee: FeeAmount

    def __post_init__(self):
        if self.signature is None:
            self.signature = EMPTY_SIGNATURE

    @classmethod
    def from_channel_state(cls, channel_state: NettingChannelState) -> "UpdatePFS":
        # pylint: disable=unexpected-keyword-arg
        return cls(
            canonical_identifier=channel_state.canonical_identifier,
            updating_participant=channel_state.our_state.address,
            other_participant=channel_state.partner_state.address,
            updating_nonce=channel.get_current_nonce(channel_state.our_state),
            other_nonce=channel.get_current_nonce(channel_state.partner_state),
            updating_capacity=channel.get_distributable(
                sender=channel_state.our_state, receiver=channel_state.partner_state
            ),
            other_capacity=channel.get_distributable(
                sender=channel_state.partner_state, receiver=channel_state.our_state
            ),
            reveal_timeout=channel_state.reveal_timeout,
            mediation_fee=channel_state.mediation_fee,
            signature=EMPTY_SIGNATURE,
        )

    def packed(self) -> bytes:
        klass = messages.UpdatePFS
        data = buffer_for(klass)
        packed = klass(data)
        self.pack(packed)
        return packed

    def pack(self, packed) -> None:
        packed.chain_id = self.canonical_identifier.chain_identifier
        packed.token_network_address = self.canonical_identifier.token_network_address
        packed.channel_identifier = self.canonical_identifier.channel_identifier
        packed.updating_participant = self.updating_participant
        packed.other_participant = self.other_participant
        packed.updating_nonce = self.updating_nonce
        packed.other_nonce = self.other_nonce
        packed.updating_capacity = self.updating_capacity
        packed.other_capacity = self.other_capacity
        packed.reveal_timeout = self.reveal_timeout
        packed.fee = self.mediation_fee
        packed.signature = self.signature

    @classmethod
    def unpack(cls, packed) -> "UpdatePFS":
        # pylint: disable=unexpected-keyword-arg
        return cls(
            canonical_identifier=CanonicalIdentifier(
                chain_identifier=packed.chain_id,
                token_network_address=packed.token_network_address,
                channel_identifier=packed.channel_identifier,
            ),
            updating_participant=packed.updating_participant,
            other_participant=packed.other_participant,
            updating_nonce=packed.updating_nonce,
            other_nonce=packed.other_nonce,
            updating_capacity=packed.other_capacity,
            other_capacity=packed.other_capacity,
            reveal_timeout=packed.reveal_timeout,
            mediation_fee=packed.fee,
            signature=packed.signature,
        )


def lockedtransfersigned_from_message(message: LockedTransfer) -> "LockedTransferSignedState":
    """ Create LockedTransferSignedState from a LockedTransfer message. """
    balance_proof = balanceproof_from_envelope(message)

    lock = HashTimeLockState(message.lock.amount, message.lock.expiration, message.lock.secrethash)

    transfer_state = LockedTransferSignedState(
        message.message_identifier,
        message.payment_identifier,
        message.token,
        balance_proof,
        lock,
        message.initiator,
        message.target,
    )

    return transfer_state


CMDID_TO_CLASS: Dict[int, Type[Message]] = {
    messages.DELIVERED: Delivered,
    messages.LOCKEDTRANSFER: LockedTransfer,
    messages.PING: Ping,
    messages.PONG: Pong,
    messages.PROCESSED: Processed,
    messages.REFUNDTRANSFER: RefundTransfer,
    messages.REVEALSECRET: RevealSecret,
    messages.UNLOCK: Unlock,
    messages.SECRETREQUEST: SecretRequest,
    messages.LOCKEXPIRED: LockExpired,
    messages.TODEVICE: ToDevice,
}

CLASSNAME_TO_CLASS = {klass.__name__: klass for klass in CMDID_TO_CLASS.values()}
CLASSNAME_TO_CLASS["Secret"] = Unlock
