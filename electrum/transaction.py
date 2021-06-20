#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.



# Note: The deserialization code originally comes from ABE.

import struct
import traceback
import sys
import io
import base64
from typing import (Sequence, Union, NamedTuple, Tuple, Optional, Iterable,
                    Callable, List, Dict, Set, TYPE_CHECKING)
from collections import defaultdict
from enum import IntEnum
import itertools
import binascii
import copy

from . import ecc, bitcoin, constants, bip32
from .bip32 import BIP32Node
from .util import profiler, to_bytes, bh2u, bfh, chunks, is_hex_str
from .bitcoin import (TYPE_ADDRESS, TYPE_SCRIPT, hash_160,
                      hash160_to_p2sh, hash160_to_p2pkh,
                      var_int, TOTAL_COIN_SUPPLY_LIMIT_IN_BTC, COIN,
                      int_to_hex, push_script, b58_address_to_hash160,
                      opcodes, add_number_to_script, base_decode,
                      base_encode, construct_witness, construct_script)
from .crypto import sha256d
from .logging import get_logger
from .keystore import xpubkey_to_address, xpubkey_to_pubkey
from pyblake2 import blake2b

if TYPE_CHECKING:
    from .wallet import Abstract_Wallet


_logger = get_logger(__name__)
DEBUG_PSBT_PARSING = False

NO_SIGNATURE = 'ff'
OVERWINTERED_VERSION_GROUP_ID = 0x03C48270
OVERWINTER_BRANCH_ID = 0x5BA81B19
SAPLING_VERSION_GROUP_ID = 0x892F2085
SAPLING_BRANCH_ID = 0x76B809BB


class TransactionVersionError(Exception):
    """ Thrown when there's a problem with transaction versioning """


class SerializationError(Exception):
    """ Thrown when there's a problem deserializing or serializing """


class NotRecognizedRedeemScript(Exception):
    pass


class UnknownTxinType(Exception):
    pass


class BadHeaderMagic(SerializationError):
    pass


class UnexpectedEndOfStream(SerializationError):
    pass


class PSBTInputConsistencyFailure(SerializationError):
    pass


class MalformedBitcoinScript(Exception):
    pass


class MissingTxInputAmount(Exception):
    pass


SIGHASH_ALL = 1


class TxOutput:
    scriptpubkey: bytes
    value: Union[int, str]

    def __init__(self, *, scriptpubkey: bytes, value: Union[int, str]):
        self.scriptpubkey = scriptpubkey
        self.value = value  # str when the output is set to max: '!'  # in satoshis

    @classmethod
    def from_address_and_value(cls, address: str, value: Union[int, str]) -> Union['TxOutput', 'PartialTxOutput']:
        return cls(scriptpubkey=bfh(bitcoin.address_to_script(address)),
                   value=value)

    def serialize_to_network(self) -> bytes:
        buf = int.to_bytes(self.value, 8, byteorder="little", signed=False)
        script = self.scriptpubkey
        buf += bfh(var_int(len(script.hex()) // 2))
        buf += script
        return buf

    @classmethod
    def from_network_bytes(cls, raw: bytes) -> 'TxOutput':
        vds = BCDataStream()
        vds.write(raw)
        txout = parse_output(vds)
        if vds.can_read_more():
            raise SerializationError('extra junk at the end of TxOutput bytes')
        return txout

    def to_legacy_tuple(self) -> Tuple[int, str, Union[int, str]]:
        if self.address:
            return TYPE_ADDRESS, self.address, self.value
        return TYPE_SCRIPT, self.scriptpubkey.hex(), self.value

    @classmethod
    def from_legacy_tuple(cls, _type: int, addr: str, val: Union[int, str]) -> Union['TxOutput', 'PartialTxOutput']:
        if _type == TYPE_ADDRESS:
            return cls.from_address_and_value(addr, val)
        if _type == TYPE_SCRIPT:
            return cls(scriptpubkey=bfh(addr), value=val)
        raise Exception(f"unexptected legacy address type: {_type}")

    @property
    def address(self) -> Optional[str]:
        return get_address_from_output_script(self.scriptpubkey)  # TODO cache this?

    def get_ui_address_str(self) -> str:
        addr = self.address
        if addr is not None:
            return addr
        return f"SCRIPT {self.scriptpubkey.hex()}"

    def __repr__(self):
        return f"<TxOutput script={self.scriptpubkey.hex()} address={self.address} value={self.value}>"

    def __eq__(self, other):
        if not isinstance(other, TxOutput):
            return False
        return self.scriptpubkey == other.scriptpubkey and self.value == other.value

    def __ne__(self, other):
        return not (self == other)

    def to_json(self):
        d = {
            'scriptpubkey': self.scriptpubkey.hex(),
            'address': self.address,
            'value_sats': self.value,
        }
        return d


class BIP143SharedTxDigestFields(NamedTuple):
    hashPrevouts: str
    hashSequence: str
    hashOutputs: str


class TxOutpoint(NamedTuple):
    txid: bytes  # endianness same as hex string displayed; reverse of tx serialization order
    out_idx: int

    @classmethod
    def from_str(cls, s: str) -> 'TxOutpoint':
        hash_str, idx_str = s.split(':')
        assert len(hash_str) == 64, f"{hash_str} should be a sha256 hash"
        return TxOutpoint(txid=bfh(hash_str),
                          out_idx=int(idx_str))

    def to_str(self) -> str:
        return f"{self.txid.hex()}:{self.out_idx}"

    def to_json(self):
        return [self.txid.hex(), self.out_idx]

    def serialize_to_network(self) -> bytes:
        return self.txid[::-1] + bfh(int_to_hex(self.out_idx, 4))

    def is_coinbase(self) -> bool:
        return self.txid == bytes(32)


class TxXsg(NamedTuple):
    x_pubkeys: None  # type []
    pubkeys: None
    signatures: None  # type {}
    address: None
    num_sig: int


class TxInput:
    prevout: TxOutpoint
    script_sig: Optional[bytes]
    nsequence: int
    witness: Optional[bytes]
    _is_coinbase_output: bool
    txxsg: TxXsg

    def __init__(self, *,
                 prevout: TxOutpoint,
                 txxsg: TxXsg,
                 script_sig: bytes = None,
                 nsequence: int = 0xffffffff - 1,
                 witness: bytes = None,
                 is_coinbase_output: bool = False):
        self.prevout = prevout
        self.script_sig = script_sig
        self.nsequence = nsequence
        self.witness = witness
        self._is_coinbase_output = is_coinbase_output

    def is_coinbase_input(self) -> bool:
        """Whether this is the input of a coinbase tx."""
        return self.prevout.is_coinbase()

    def is_coinbase_output(self) -> bool:
        """Whether the coin being spent is an output of a coinbase tx.
        This matters for coin maturity.
        """
        return self._is_coinbase_output

    def value_sats(self) -> Optional[int]:
        return None

    def to_json(self):
        d = {
            'prevout_hash': self.prevout.txid.hex(),
            'prevout_n': self.prevout.out_idx,
            'coinbase': self.is_coinbase_output(),
            'nsequence': self.nsequence,
        }
        if self.script_sig is not None:
            d['scriptSig'] = self.script_sig.hex()
        if self.witness is not None:
            d['witness'] = self.witness.hex()
        return d

    def witness_elements(self)-> Sequence[bytes]:
        vds = BCDataStream()
        vds.write(self.witness)
        n = vds.read_compact_size()
        return list(vds.read_bytes(vds.read_compact_size()) for i in range(n))



class BCDataStream(object):
    """Workalike python implementation of Bitcoin's CDataStream class."""

    def __init__(self):
        self.input = None  # type: Optional[bytearray]
        self.read_cursor = 0

    def clear(self):
        self.input = None
        self.read_cursor = 0

    def write(self, _bytes: Union[bytes, bytearray]):  # Initialize with string of _bytes
        assert isinstance(_bytes, (bytes, bytearray))
        if self.input is None:
            self.input = bytearray(_bytes)
        else:
            self.input += bytearray(_bytes)

    def read_string(self, encoding='ascii'):
        # Strings are encoded depending on length:
        # 0 to 252 :  1-byte-length followed by bytes (if any)
        # 253 to 65,535 : byte'253' 2-byte-length followed by bytes
        # 65,536 to 4,294,967,295 : byte '254' 4-byte-length followed by bytes
        # ... and the Bitcoin client is coded to understand:
        # greater than 4,294,967,295 : byte '255' 8-byte-length followed by bytes of string
        # ... but I don't think it actually handles any strings that big.
        if self.input is None:
            raise SerializationError("call write(bytes) before trying to deserialize")

        length = self.read_compact_size()

        return self.read_bytes(length).decode(encoding)

    def write_string(self, string, encoding='ascii'):
        string = to_bytes(string, encoding)
        # Length-encoded as with read-string
        self.write_compact_size(len(string))
        self.write(string)

    def read_bytes(self, length: int) -> bytes:
        if self.input is None:
            raise SerializationError("call write(bytes) before trying to deserialize")
        assert length >= 0
        input_len = len(self.input)
        read_begin = self.read_cursor
        read_end = read_begin + length
        if 0 <= read_begin <= read_end <= input_len:
            result = self.input[read_begin:read_end]  # type: bytearray
            self.read_cursor += length
            return bytes(result)
        else:
            raise SerializationError('attempt to read past end of buffer')

    def write_bytes(self, _bytes: Union[bytes, bytearray], length: int):
        assert len(_bytes) == length, len(_bytes)
        self.write(_bytes)

    def can_read_more(self) -> bool:
        if not self.input:
            return False
        return self.read_cursor < len(self.input)

    def read_boolean(self) -> bool: return self.read_bytes(1) != b'\x00'
    def read_int16(self): return self._read_num('<h')
    def read_uint16(self): return self._read_num('<H')
    def read_int32(self): return self._read_num('<i')
    def read_uint32(self): return self._read_num('<I')
    def read_int64(self): return self._read_num('<q')
    def read_uint64(self): return self._read_num('<Q')

    def write_boolean(self, val): return self.write(b'\x01' if val else b'\x00')
    def write_int16(self, val): return self._write_num('<h', val)
    def write_uint16(self, val): return self._write_num('<H', val)
    def write_int32(self, val): return self._write_num('<i', val)
    def write_uint32(self, val): return self._write_num('<I', val)
    def write_int64(self, val): return self._write_num('<q', val)
    def write_uint64(self, val): return self._write_num('<Q', val)

    def read_compact_size(self):
        try:
            size = self.input[self.read_cursor]
            self.read_cursor += 1
            if size == 253:
                size = self._read_num('<H')
            elif size == 254:
                size = self._read_num('<I')
            elif size == 255:
                size = self._read_num('<Q')
            return size
        except IndexError as e:
            raise SerializationError("attempt to read past end of buffer") from e

    def write_compact_size(self, size):
        if size < 0:
            raise SerializationError("attempt to write size < 0")
        elif size < 253:
            self.write(bytes([size]))
        elif size < 2**16:
            self.write(b'\xfd')
            self._write_num('<H', size)
        elif size < 2**32:
            self.write(b'\xfe')
            self._write_num('<I', size)
        elif size < 2**64:
            self.write(b'\xff')
            self._write_num('<Q', size)
        else:
            raise Exception(f"size {size} too large for compact_size")

    def _read_num(self, format):
        try:
            (i,) = struct.unpack_from(format, self.input, self.read_cursor)
            self.read_cursor += struct.calcsize(format)
        except Exception as e:
            raise SerializationError(e) from e
        return i

    def _write_num(self, format, num):
        s = struct.pack(format, num)
        self.write(s)


def script_GetOp(_bytes : bytes):
    i = 0
    while i < len(_bytes):
        vch = None
        opcode = _bytes[i]
        i += 1

        if opcode <= opcodes.OP_PUSHDATA4:
            nSize = opcode
            if opcode == opcodes.OP_PUSHDATA1:
                try: nSize = _bytes[i]
                except IndexError: raise MalformedBitcoinScript()
                i += 1
            elif opcode == opcodes.OP_PUSHDATA2:
                try: (nSize,) = struct.unpack_from('<H', _bytes, i)
                except struct.error: raise MalformedBitcoinScript()
                i += 2
            elif opcode == opcodes.OP_PUSHDATA4:
                try: (nSize,) = struct.unpack_from('<I', _bytes, i)
                except struct.error: raise MalformedBitcoinScript()
                i += 4
            vch = _bytes[i:i + nSize]
            i += nSize

        yield opcode, vch, i


class OPPushDataGeneric:
    def __init__(self, pushlen: Callable=None):
        if pushlen is not None:
            self.check_data_len = pushlen

    @classmethod
    def check_data_len(cls, datalen: int) -> bool:
        # Opcodes below OP_PUSHDATA4 all just push data onto stack, and are equivalent.
        return opcodes.OP_PUSHDATA4 >= datalen >= 0

    @classmethod
    def is_instance(cls, item):
        # accept objects that are instances of this class
        # or other classes that are subclasses
        return isinstance(item, cls) \
               or (isinstance(item, type) and issubclass(item, cls))


OPPushDataPubkey = OPPushDataGeneric(lambda x: x in (33, 65))

SCRIPTPUBKEY_TEMPLATE_P2PKH = [opcodes.OP_DUP, opcodes.OP_HASH160,
                               OPPushDataGeneric(lambda x: x == 20),
                               opcodes.OP_EQUALVERIFY, opcodes.OP_CHECKSIG]
SCRIPTPUBKEY_TEMPLATE_P2SH = [opcodes.OP_HASH160, OPPushDataGeneric(lambda x: x == 20), opcodes.OP_EQUAL]
SCRIPTPUBKEY_TEMPLATE_WITNESS_V0 = [opcodes.OP_0, OPPushDataGeneric(lambda x: x in (20, 32))]


def match_script_against_template(script, template) -> bool:
    """Returns whether 'script' matches 'template'."""
    if script is None:
        return False
    # optionally decode script now:
    if isinstance(script, (bytes, bytearray)):
        try:
            script = [x for x in script_GetOp(script)]
        except MalformedBitcoinScript:
            return False
    if len(script) != len(template):
        return False
    for i in range(len(script)):
        template_item = template[i]
        script_item = script[i]
        if OPPushDataGeneric.is_instance(template_item) and template_item.check_data_len(script_item[0]):
            continue
        if template_item != script_item[0]:
            return False
    return True

def match_decoded(decoded, to_match):
    if len(decoded) != len(to_match):
        return False;
    for i in range(len(decoded)):
        if to_match[i] == opcodes.OP_PUSHDATA4 and decoded[i][0] <= opcodes.OP_PUSHDATA4 and decoded[i][0]>0:
            continue  # Opcodes below OP_PUSHDATA4 all just push data onto stack, and are equivalent.
        if to_match[i] != decoded[i][0]:
            return False
    return True

def parse_sig(x_sig):
    return [None if x == NO_SIGNATURE else x for x in x_sig]

def safe_parse_pubkey(x):
    try:
        return xpubkey_to_pubkey(x)
    except:
        return x

def parse_scriptSig(_bytes):
    d = {}
    try:
        decoded = [x for x in script_GetOp(_bytes)]
    except Exception as e:
        # coinbase transactions raise an exception
        raise Exception("parse_scriptSig: cannot find address in input script (coinbase?)",
                        bh2u(_bytes))

    match = [opcodes.OP_PUSHDATA4]
    if match_decoded(decoded, match):
        item = decoded[0][1]
        # assert item[0] == 0x30
        # pay-to-pubkey
        d['type'] = 'p2pk'
        d['address'] = "(pubkey)"
        d['signatures'] = [bh2u(item)]
        d['num_sig'] = 1
        d['x_pubkeys'] = ["(pubkey)"]
        d['pubkeys'] = ["(pubkey)"]
        return d

    # p2pkh TxIn transactions push a signature
    # (71-73 bytes) and then their public key
    # (33 or 65 bytes) onto the stack:
    match = [opcodes.OP_PUSHDATA4, opcodes.OP_PUSHDATA4]
    if match_decoded(decoded, match):
        sig = bh2u(decoded[0][1])
        x_pubkey = bh2u(decoded[1][1])
        try:
            signatures = parse_sig([sig])
            pubkey, address = xpubkey_to_address(x_pubkey)
        except:
            raise Exception("parse_scriptSig: cannot find address in input script (p2pkh?)",
                            bh2u(_bytes))
        d['type'] = 'p2pkh'
        d['signatures'] = signatures
        d['x_pubkeys'] = [x_pubkey]
        d['num_sig'] = 1
        d['pubkeys'] = [pubkey]
        d['address'] = address
        return d

    # p2sh transaction, m of n
    match = [opcodes.OP_0] + [opcodes.OP_PUSHDATA4] * (len(decoded) - 1)
    if match_decoded(decoded, match):
        x_sig = [bh2u(x[1]) for x in decoded[1:-1]]
        try:
            m, n, x_pubkeys, pubkeys, redeemScript = parse_redeemScript(
                decoded[-1][1])
        except NotRecognizedRedeemScript:
            raise Exception("parse_scriptSig: cannot find address in input script (p2sh?)",
                            bh2u(_bytes))
            # we could still guess:
            # d['address'] = hash160_to_p2sh(hash_160(decoded[-1][1]))
            return None
        # write result in d
        d['type'] = 'p2sh'
        d['num_sig'] = m
        d['signatures'] = parse_sig(x_sig)
        d['x_pubkeys'] = x_pubkeys
        d['pubkeys'] = pubkeys
        d['redeemScript'] = redeemScript
        d['address'] = hash160_to_p2sh(hash_160(bfh(redeemScript)))
        return d

    raise Exception("parse_scriptSig: cannot find address in input script (unknown)",
                    bh2u(_bytes))

def parse_redeemScript(s):
    dec2 = [x for x in script_GetOp(s)]
    try:
        m = dec2[0][0] - opcodes.OP_1 + 1
        n = dec2[-2][0] - opcodes.OP_1 + 1
    except IndexError:
        raise NotRecognizedRedeemScript()
    op_m = opcodes.OP_1 + m - 1
    op_n = opcodes.OP_1 + n - 1
    match_multisig = [op_m] + [opcodes.OP_PUSHDATA4] * \
        n + [op_n, opcodes.OP_CHECKMULTISIG]
    if not match_decoded(dec2, match_multisig):
        raise NotRecognizedRedeemScript()
    x_pubkeys = [bh2u(x[1]) for x in dec2[1:-2]]
    pubkeys = [safe_parse_pubkey(x) for x in x_pubkeys]
    redeemScript = multisig_script(pubkeys, m)
    return m, n, x_pubkeys, pubkeys, redeemScript

def get_script_type_from_output_script(_bytes: bytes) -> Optional[str]:
    if _bytes is None:
        return None
    try:
        decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return None
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PKH):
        return 'p2pkh'
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2SH):
        return 'p2sh'
    return None

def get_address_from_output_script(_bytes: bytes, *, net=None) -> Optional[str]:
    try:
        decoded = [x for x in script_GetOp(_bytes)]
    except MalformedBitcoinScript:
        return None

    # p2pkh
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2PKH):
        return hash160_to_p2pkh(decoded[2][1], net=net)

    # p2sh
    if match_script_against_template(decoded, SCRIPTPUBKEY_TEMPLATE_P2SH):
        return hash160_to_p2sh(decoded[1][1], net=net)


def parse_input(vds: BCDataStream) -> TxInput:
    prevout_hash = vds.read_bytes(32)[::-1]
    prevout_n = vds.read_uint32()
    prevout = TxOutpoint(txid=prevout_hash, out_idx=prevout_n)
    script_sig = vds.read_bytes(vds.read_compact_size())
    nsequence = vds.read_uint32()

    x_pubkeys = []
    pubkeys = []
    signatures = {}
    address = None
    num_sig = 0

    data = None
    if script_sig:
        try:
            data = parse_scriptSig(script_sig)
            if data != None:
                x_pubkeys = data['x_pubkeys']
                pubkeys = data['pubkeys']
                signatures = data['signatures']
                num_sig = data['num_sig']
                address = data['address']
            else:
                raise Exception("parse_scriptSig: Error")
        except BaseException:
            raise Exception("parse_scriptSig: Error")
    else:
        script_sig = None
    # custom field

    txxsg = TxXsg(x_pubkeys=x_pubkeys, pubkeys=pubkeys,
                  signatures=signatures, address=address, num_sig=num_sig)

    return TxInput(prevout=prevout, txxsg=txxsg, script_sig=script_sig, nsequence=nsequence)


def parse_witness(vds: BCDataStream, txin: TxInput) -> None:
    n = vds.read_compact_size()
    witness_elements = list(vds.read_bytes(vds.read_compact_size()) for i in range(n))
    txin.witness = bfh(construct_witness(witness_elements))


def parse_output(vds: BCDataStream, i: int) -> TxOutput:
    value = vds.read_int64()
    if value > TOTAL_COIN_SUPPLY_LIMIT_IN_BTC * COIN:
        raise SerializationError('invalid output amount (too large)')
    if value < 0:
        raise SerializationError('invalid output amount (negative)')
    scriptpubkey = vds.read_bytes(vds.read_compact_size())
    return TxOutput(value=value, scriptpubkey=scriptpubkey)

def parse_join_split(vds):
    d = {}
    d['vpub_old'] = vds.read_uint64()
    d['vpub_new'] = vds.read_uint64()
    d['anchor'] = vds.read_bytes(32)
    d['nullifiers'] = vds.read_bytes(64)
    d['commitments'] = vds.read_bytes(64)
    d['ephemeralKey'] = vds.read_bytes(32)
    d['randomSeed'] = vds.read_bytes(32)
    d['vmacs'] = vds.read_bytes(64)
    d['zkproof'] = vds.read_bytes(296)
    d['encCiphertexts'] = vds.read_bytes(1202)
    return d

def deserialize(raw):
    vds = BCDataStream()
    vds.write(bfh(raw))
    d = {}
    start = vds.read_cursor

    header = vds.read_uint32()
    overwintered = True if header & 0x80000000 else False
    version = header & 0x7FFFFFFF

    if overwintered:
        if version not in [3, 4]:
            raise TransactionVersionError('Overwintered transaction'
                                          ' with invalid version=%d' % version)
        ver_group_id = vds.read_uint32()
        if (version == 3 and ver_group_id != OVERWINTERED_VERSION_GROUP_ID or
                version == 4 and ver_group_id != SAPLING_VERSION_GROUP_ID):
            raise TransactionVersionError('Overwintered transaction with wrong'
                                          ' versionGroupId=%X' % ver_group_id)
        d['versionGroupId'] = ver_group_id

    d['overwintered'] = overwintered
    d['version'] = version

    n_vin = vds.read_compact_size()
    d['inputs'] = [parse_input(vds) for i in range(n_vin)]
    n_vout = vds.read_compact_size()
    d['outputs'] = [parse_output(vds, i) for i in range(n_vout)]
    d['lockTime'] = vds.read_uint32()

    if overwintered:
        d['expiryHeight'] = vds.read_uint32()

        if version == 4:
            d['valueBalance'] = vds.read_int64()
            n_sh_sp = vds.read_compact_size()
            if n_sh_sp > 0:
                d['shieldedSpends'] = vds.read_bytes(n_sh_sp*384)
            n_sh_out = vds.read_compact_size()
            if n_sh_out > 0:
                d['shieldedOutputs'] = vds.read_bytes(n_sh_out*948)

        n_js = vds.read_compact_size()
        if n_js > 0:
            if version == 3:
                d['joinSplits'] = [parse_join_split(vds) for i in range(n_js)]
            else:
                d['joinSplits'] = vds.read_bytes(n_js*1698)
            d['joinSplitPubKey'] = vds.read_bytes(32)
            d['joinSplitSig'] = vds.read_bytes(64)
            if version == 4:
                d['bindingSig'] = vds.read_bytes(64)

    return d

# pay & redeem scripts

def multisig_script(public_keys: Sequence[str], m: int) -> str:
    n = len(public_keys)
    assert 1 <= m <= n <= 15, f'm {m}, n {n}'
    return construct_script([m, *public_keys, n, opcodes.OP_CHECKMULTISIG])


class Transaction:
    _cached_network_ser: Optional[str]

    def __str__(self):
        return self.serialize()

    def __init__(self, raw):
        if raw is None:
            self._cached_network_ser = None
        elif isinstance(raw, str):
            self._cached_network_ser = raw.strip() if raw else None
            assert is_hex_str(self._cached_network_ser)
        elif isinstance(raw, (bytes, bytearray)):
            self._cached_network_ser = bh2u(raw)
        else:
            raise Exception(f"cannot initialize transaction from {raw}")
        self._inputs = None  # type: List[TxInput]
        self._outputs = None  # type: List[TxOutput]
        self._locktime = 0
        self._version = 4
        self.overwintered = True
        self.versionGroupId = SAPLING_VERSION_GROUP_ID
        self.expiryHeight = 0
        self.valueBalance = 0
        self.shieldedSpends = None
        self.shieldedOutputs = None
        self.joinSplits = None
        self.joinSplitPubKey = None
        self.joinSplitSig = None
        self.bindingSig = None

        self._cached_txid = None  # type: Optional[str]

    @property
    def locktime(self):
        self.deserialize()
        return self._locktime

    @locktime.setter
    def locktime(self, value: int):
        assert isinstance(value, int), f"locktime must be int, not {value!r}"
        self._locktime = value
        self.invalidate_ser_cache()

    @property
    def version(self):
        self.deserialize()
        return self._version

    @version.setter
    def version(self, value):
        self._version = value
        self.invalidate_ser_cache()

    def to_json(self) -> dict:
        d = {
            'version': self.version,
            'locktime': self.locktime,
            'inputs': [txin.to_json() for txin in self.inputs()],
            'outputs': [txout.to_json() for txout in self.outputs()],
        }
        return d

    def inputs(self) -> Sequence[TxInput]:
        if self._inputs is None:
            self.deserialize()
        return self._inputs

    def outputs(self) -> Sequence[TxOutput]:
        if self._outputs is None:
            self.deserialize()
        return self._outputs

    def deserialize(self) -> None:
        if self._cached_network_ser is None:
            return
        if self._inputs is not None:
            return

        d = deserialize(self._cached_network_ser)
        self._inputs = d['inputs']
        self._outputs = d['outputs']
        self.locktime = d['lockTime']
        self.version = d['version']
        self.overwintered = d['overwintered']
        self.versionGroupId = d.get('versionGroupId')
        self.expiryHeight = d.get('expiryHeight', 0)
        self.valueBalance = d.get('valueBalance', 0)
        self.shieldedSpends = d.get('shieldedSpends')
        self.shieldedOutputs = d.get('shieldedOutputs')
        self.joinSplits = d.get('joinSplits')
        self.joinSplitPubKey = d.get('joinSplitPubKey')
        self.joinSplitSig = d.get('joinSplitSig')
        self.bindingSig = d.get('bindingSig')
        return d

    @classmethod
    def get_siglist(self, txin: 'PartialTxInput', *, estimate_size=False):
        if txin.is_coinbase_input():
            return [], []

        if estimate_size:
            try:
                pubkey_size = len(txin.pubkeys[0])
            except IndexError:
                pubkey_size = 33  # guess it is compressed
            num_pubkeys = max(1, len(txin.pubkeys))
            pk_list = ["00" * pubkey_size] * num_pubkeys
            num_sig = max(1, txin.num_sig)
            # we guess that signatures will be 72 bytes long
            # note: DER-encoded ECDSA signatures are 71 or 72 bytes in practice
            #       See https://bitcoin.stackexchange.com/questions/77191/what-is-the-maximum-size-of-a-der-encoded-ecdsa-signature
            #       We assume low S (as that is a bitcoin standardness rule).
            #       We do not assume low R (even though the sigs we create conform), as external sigs,
            #       e.g. from a hw signer cannot be expected to have a low R.
            sig_list = ["00" * 72] * num_sig
        else:
            pk_list = [pubkey.hex() for pubkey in txin.pubkeys]
            sig_list = [txin.part_sigs.get(pubkey, b'').hex() for pubkey in txin.pubkeys]
            if txin.is_complete():
                sig_list = [sig for sig in sig_list if sig]
        return pk_list, sig_list

    @classmethod
    def serialize_witness(cls, txin: TxInput, *, estimate_size=False) -> str:
        if txin.witness is not None:
            return txin.witness.hex()
        if txin.is_coinbase_input():
            return ''
        assert isinstance(txin, PartialTxInput)

        _type = txin.script_type

        if _type in ('address', 'unknown') and estimate_size:
            _type = cls.guess_txintype_from_address(txin.address)
        pubkeys, sig_list = cls.get_siglist(txin, estimate_size=estimate_size)
        if _type in ['p2pk', 'p2pkh', 'p2sh']:
            return construct_witness([])
        raise UnknownTxinType(f'cannot construct witness for txin_type: {_type}')

    @classmethod
    def guess_txintype_from_address(cls, addr: Optional[str]) -> str:
        # It's not possible to tell the script type in general
        # just from an address.
        # - "1" addresses are of course p2pkh
        # - "3" addresses are p2sh but we don't know the redeem script..
        # If we don't know the script, we _guess_ it is pubkeyhash.
        # As this method is used e.g. for tx size estimation,
        # the estimation will not be precise.
        addrtype, hash_160_ = b58_address_to_hash160(addr)
        if addrtype == constants.net.ADDRTYPE_P2PKH:
            return 'p2pkh'
        raise Exception(f'unrecognized address: {repr(addr)}')

    @classmethod
    def input_script(self, txin: TxInput, *, estimate_size=False) -> str:
        if txin.script_sig is not None:
            return txin.script_sig.hex()
        if txin.is_coinbase_input():
            return ''
        assert isinstance(txin, PartialTxInput)

        _type = txin.script_type
        pubkeys, sig_list = self.get_siglist(txin, estimate_size=estimate_size)
        if _type in ('address', 'unknown') and estimate_size:
            _type = self.guess_txintype_from_address(txin.address)
        if _type == 'p2pk':
            return construct_script([sig_list[0]])
        elif _type == 'p2sh':
            # put op_0 before script
            redeem_script = multisig_script(pubkeys, txin.num_sig)
            return construct_script([0, *sig_list, redeem_script])
        elif _type == 'p2pkh':
            return construct_script([sig_list[0], pubkeys[0]])
        raise UnknownTxinType(f'cannot construct scriptSig for txin_type: {_type}')

    @classmethod
    def get_preimage_script(cls, txin: 'PartialTxInput') -> str:

        pubkeys = [pk.hex() for pk in txin.pubkeys]
        if txin.script_type == 'p2sh':
            return multisig_script(pubkeys, txin.num_sig)
        elif txin.script_type == 'p2pkh':
            pubkey = pubkeys[0]
            pkh = bh2u(hash_160(bfh(pubkey)))
            return bitcoin.pubkeyhash_to_p2pkh_script(pkh)
        elif txin.script_type == 'p2pk':
            pubkey = pubkeys[0]
            return bitcoin.public_key_to_p2pk_script(pubkey)
        else:
            raise UnknownTxinType(f'cannot construct preimage_script for txin_type: {txin.script_type}')

    @classmethod
    def serialize_input(self, txin: TxInput, script: str) -> str:
        # Prev hash and index
        s = txin.prevout.serialize_to_network().hex()
        # Script length, script, sequence
        s += var_int(len(script)//2)
        s += script
        s += int_to_hex(txin.nsequence, 4)
        return s

    def _calc_bip143_shared_txdigest_fields(self) -> BIP143SharedTxDigestFields:
        inputs = self.inputs()
        outputs = self.outputs()
        hashPrevouts = bh2u(sha256d(b''.join(txin.prevout.serialize_to_network() for txin in inputs)))
        hashSequence = bh2u(sha256d(bfh(''.join(int_to_hex(txin.nsequence, 4) for txin in inputs))))
        hashOutputs = bh2u(sha256d(bfh(''.join(o.serialize_to_network().hex() for o in outputs))))
        return BIP143SharedTxDigestFields(hashPrevouts=hashPrevouts,
                                          hashSequence=hashSequence,
                                          hashOutputs=hashOutputs)


    def invalidate_ser_cache(self):
        self._cached_network_ser = None
        self._cached_txid = None

    def serialize(self) -> str:
        if not self._cached_network_ser:
            self._cached_network_ser = self.serialize_to_network(estimate_size=False, include_sigs=True)
        return self._cached_network_ser

    def serialize_as_bytes(self) -> bytes:
        return bfh(self.serialize())

    def serialize_to_network(self, *, estimate_size=False, include_sigs=True) -> str:
        """Serialize the transaction as used on the Bitcoin network, into hex.
        `include_sigs` signals whether to include scriptSigs and witnesses.
        """
        self.deserialize()
        nVersion = int_to_hex(self.version, 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()

        def create_script_sig(txin: TxInput) -> str:
            if include_sigs:
                return self.input_script(txin, estimate_size=estimate_size)
            return ''
        txins = var_int(len(inputs)) + ''.join(self.serialize_input(txin, create_script_sig(txin))
                                               for txin in inputs)
        txouts = var_int(len(outputs)) + ''.join(o.serialize_to_network().hex() for o in outputs)

        return nVersion + txins + txouts + nLocktime

    def to_qr_data(self) -> str:
        """Returns tx as data to be put into a QR code. No side-effects."""
        tx = copy.deepcopy(self)  # make copy as we mutate tx
        if isinstance(tx, PartialTransaction):
            # this makes QR codes a lot smaller (or just possible in the first place!)
            tx.convert_all_utxos_to_witness_utxos()
        tx_bytes = tx.serialize_as_bytes()
        return base_encode(tx_bytes, base=43)

    def txid(self) -> Optional[str]:
        if self._cached_txid is None:
            self.deserialize()
            if not self.is_complete():
                return None
            try:
                ser = self.serialize_to_network()
            except UnknownTxinType:
                # we might not know how to construct scriptSig for some scripts
                return None
            self._cached_txid = bh2u(sha256d(bfh(ser))[::-1])
        return self._cached_txid


    def add_info_from_wallet(self, wallet: 'Abstract_Wallet', **kwargs) -> None:
        return  # no-op

    def is_final(self) -> bool:
        """Whether RBF is disabled."""
        return not any([txin.nsequence < 0xffffffff - 1 for txin in self.inputs()])

    def estimated_size(self):
        """Return an estimated virtual tx size in vbytes.
        BIP-0141 defines 'Virtual transaction size' to be weight/4 rounded up.
        This definition is only for humans, and has little meaning otherwise.
        If we wanted sub-byte precision, fee calculation should use transaction
        weights, but for simplicity we approximate that with (virtual_size)x4
        """
        weight = self.estimated_weight()
        return self.virtual_size_from_weight(weight)

    @classmethod
    def estimated_input_weight(cls, txin):
        '''Return an estimate of serialized input weight in weight units.'''
        script = cls.input_script(txin, estimate_size=True)
        input_size = len(cls.serialize_input(txin, script)) // 2
        return 4 * input_size

    @classmethod
    def estimated_output_size_for_address(cls, address: str) -> int:
        """Return an estimate of serialized output size in bytes."""
        script = bitcoin.address_to_script(address)
        return cls.estimated_output_size_for_script(script)

    @classmethod
    def estimated_output_size_for_script(cls, script: str) -> int:
        """Return an estimate of serialized output size in bytes."""
        # 8 byte value + varint script len + script
        script_len = len(script) // 2
        var_int_len = len(var_int(script_len)) // 2
        return 8 + var_int_len + script_len

    @classmethod
    def virtual_size_from_weight(cls, weight):
        return weight // 4 + (weight % 4 > 0)

    @classmethod
    def satperbyte_from_satperkw(cls, feerate_kw):
        """Converts feerate from sat/kw to sat/vbyte."""
        return feerate_kw * 4 / 1000

    def estimated_total_size(self):
        """Return an estimated total transaction size in bytes."""
        if not self.is_complete() or self._cached_network_ser is None:
            return len(self.serialize_to_network(estimate_size=True)) // 2
        else:
            return len(self._cached_network_ser) // 2  # ASCII hex string


    def estimated_base_size(self):
        """Return an estimated base transaction size in bytes."""
        return self.estimated_total_size() - self.estimated_witness_size()

    def estimated_weight(self):
        """Return an estimate of transaction weight."""
        total_tx_size = self.estimated_total_size()
        base_tx_size = self.estimated_base_size()
        return 3 * base_tx_size + total_tx_size

    def is_complete(self) -> bool:
        return True

    def get_output_idxs_from_scriptpubkey(self, script: str) -> Set[int]:
        """Returns the set indices of outputs with given script."""
        assert isinstance(script, str)  # hex
        # build cache if there isn't one yet
        # note: can become stale and return incorrect data
        #       if the tx is modified later; that's out of scope.
        if not hasattr(self, '_script_to_output_idx'):
            d = defaultdict(set)
            for output_idx, o in enumerate(self.outputs()):
                o_script = o.scriptpubkey.hex()
                assert isinstance(o_script, str)
                d[o_script].add(output_idx)
            self._script_to_output_idx = d
        return set(self._script_to_output_idx[script])  # copy

    def get_output_idxs_from_address(self, addr: str) -> Set[int]:
        script = bitcoin.address_to_script(addr)
        return self.get_output_idxs_from_scriptpubkey(script)

    def output_value_for_address(self, addr):
        # assumes exactly one output has that address
        for o in self.outputs():
            if o.address == addr:
                return o.value
        else:
            raise Exception('output not found', addr)

    def get_input_idx_that_spent_prevout(self, prevout: TxOutpoint) -> Optional[int]:
        # build cache if there isn't one yet
        # note: can become stale and return incorrect data
        #       if the tx is modified later; that's out of scope.
        if not hasattr(self, '_prevout_to_input_idx'):
            d = {}  # type: Dict[TxOutpoint, int]
            for i, txin in enumerate(self.inputs()):
                d[txin.prevout] = i
            self._prevout_to_input_idx = d
        idx = self._prevout_to_input_idx.get(prevout)
        if idx is not None:
            assert self.inputs()[idx].prevout == prevout
        return idx


def convert_raw_tx_to_hex(raw: Union[str, bytes]) -> str:
    """Sanitizes tx-describing input (hex/base43/base64) into
    raw tx hex string."""
    if not raw:
        raise ValueError("empty string")
    raw_unstripped = raw
    raw = raw.strip()
    # try hex
    try:
        return binascii.unhexlify(raw).hex()
    except:
        pass
    # try base43
    try:
        return base_decode(raw, base=43).hex()
    except:
        pass
    # try base64
    if raw[0:6] in ('cHNidP', b'cHNidP'):  # base64 psbt
        try:
            return base64.b64decode(raw).hex()
        except:
            pass
    # raw bytes (do not strip whitespaces in this case)
    if isinstance(raw_unstripped, bytes):
        return raw_unstripped.hex()
    raise ValueError(f"failed to recognize transaction encoding for txt: {raw[:30]}...")


def tx_from_any(raw: Union[str, bytes], *,
                deserialize: bool = True) -> Union['PartialTransaction', 'Transaction']:
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    raw = convert_raw_tx_to_hex(raw)
    try:
        return PartialTransaction.from_raw_psbt(raw)
    except BadHeaderMagic:
        if raw[:10] == b'EPTF\xff'.hex():
            raise SerializationError("Partial transactions generated with old Electrum versions "
                                     "(< 4.0) are no longer supported. Please upgrade Electrum on "
                                     "the other machine where this transaction was created.")
    try:
        tx = Transaction(raw)
        if deserialize:
            tx.deserialize()
        return tx
    except Exception as e:
        raise SerializationError(f"Failed to recognise tx encoding, or to parse transaction. "
                                 f"raw: {raw[:30]}...") from e


class PSBTGlobalType(IntEnum):
    UNSIGNED_TX = 0
    XPUB = 1
    VERSION = 0xFB


class PSBTInputType(IntEnum):
    NON_WITNESS_UTXO = 0
    WITNESS_UTXO = 1
    PARTIAL_SIG = 2
    SIGHASH_TYPE = 3
    REDEEM_SCRIPT = 4
    WITNESS_SCRIPT = 5
    BIP32_DERIVATION = 6
    FINAL_SCRIPTSIG = 7
    FINAL_SCRIPTWITNESS = 8


class PSBTOutputType(IntEnum):
    REDEEM_SCRIPT = 0
    WITNESS_SCRIPT = 1
    BIP32_DERIVATION = 2


# Serialization/deserialization tools
def deser_compact_size(f) -> Optional[int]:
    try:
        nit = f.read(1)[0]
    except IndexError:
        return None     # end of file

    if nit == 253:
        nit = struct.unpack("<H", f.read(2))[0]
    elif nit == 254:
        nit = struct.unpack("<I", f.read(4))[0]
    elif nit == 255:
        nit = struct.unpack("<Q", f.read(8))[0]
    return nit


class PSBTSection:

    def _populate_psbt_fields_from_fd(self, fd=None):
        if not fd: return

        while True:
            try:
                key_type, key, val = self.get_next_kv_from_fd(fd)
            except StopIteration:
                break
            self.parse_psbt_section_kv(key_type, key, val)

    @classmethod
    def get_next_kv_from_fd(cls, fd) -> Tuple[int, bytes, bytes]:
        key_size = deser_compact_size(fd)
        if key_size == 0:
            raise StopIteration()
        if key_size is None:
            raise UnexpectedEndOfStream()

        full_key = fd.read(key_size)
        key_type, key = cls.get_keytype_and_key_from_fullkey(full_key)

        val_size = deser_compact_size(fd)
        if val_size is None: raise UnexpectedEndOfStream()
        val = fd.read(val_size)

        return key_type, key, val

    @classmethod
    def create_psbt_writer(cls, fd):
        def wr(key_type: int, val: bytes, key: bytes = b''):
            full_key = cls.get_fullkey_from_keytype_and_key(key_type, key)
            fd.write(bytes.fromhex(var_int(len(full_key))))  # key_size
            fd.write(full_key)  # key
            fd.write(bytes.fromhex(var_int(len(val))))  # val_size
            fd.write(val)  # val
        return wr

    @classmethod
    def get_keytype_and_key_from_fullkey(cls, full_key: bytes) -> Tuple[int, bytes]:
        with io.BytesIO(full_key) as key_stream:
            key_type = deser_compact_size(key_stream)
            if key_type is None: raise UnexpectedEndOfStream()
            key = key_stream.read()
        return key_type, key

    @classmethod
    def get_fullkey_from_keytype_and_key(cls, key_type: int, key: bytes) -> bytes:
        key_type_bytes = bytes.fromhex(var_int(key_type))
        return key_type_bytes + key

    def _serialize_psbt_section(self, fd):
        wr = self.create_psbt_writer(fd)
        self.serialize_psbt_section_kvs(wr)
        fd.write(b'\x00')  # section-separator

    def parse_psbt_section_kv(self, kt: int, key: bytes, val: bytes) -> None:
        raise NotImplementedError()  # implemented by subclasses

    def serialize_psbt_section_kvs(self, wr) -> None:
        raise NotImplementedError()  # implemented by subclasses


class PartialTxInput(TxInput, PSBTSection):
    def __init__(self, *args, **kwargs):
        TxInput.__init__(self, *args, **kwargs)
        self._utxo = None  # type: Optional[Transaction]
        self._witness_utxo = None  # type: Optional[TxOutput]
        self.part_sigs = {}  # type: Dict[bytes, bytes]  # pubkey -> sig
        self.sighash = None  # type: Optional[int]
        self.bip32_paths = {}  # type: Dict[bytes, Tuple[bytes, Sequence[int]]]  # pubkey -> (xpub_fingerprint, path)
        self.redeem_script = None  # type: Optional[bytes]
        self.witness_script = None  # type: Optional[bytes]
        self._unknown = {}  # type: Dict[bytes, bytes]

        self.script_type = 'unknown'
        self.num_sig = 0  # type: int  # num req sigs for multisig
        self.pubkeys = []  # type: List[bytes]  # note: order matters
        self._trusted_value_sats = None  # type: Optional[int]
        self._trusted_address = None  # type: Optional[str]
        self.block_height = None  # type: Optional[int]  # height at which the TXO is mined; None means unknown
        self.spent_height = None  # type: Optional[int]  # height at which the TXO got spent

    @property
    def utxo(self):
        return self._utxo

    @utxo.setter
    def utxo(self, tx: Optional[Transaction]):
        if tx is None:
            return
        # note that tx might be a PartialTransaction
        # serialize and de-serialize tx now. this might e.g. convert a complete PartialTx to a Tx
        tx = tx_from_any(str(tx))
        # 'utxo' field in PSBT cannot be another PSBT:
        if not tx.is_complete():
            return
        self._utxo = tx
        self.validate_data()
        self.ensure_there_is_only_one_utxo()

    @property
    def witness_utxo(self):
        return self._witness_utxo

    @witness_utxo.setter
    def witness_utxo(self, value: Optional[TxOutput]):
        self._witness_utxo = value
        self.validate_data()
        self.ensure_there_is_only_one_utxo()

    def to_json(self):
        d = super().to_json()
        d.update({
            'height': self.block_height,
            'value_sats': self.value_sats(),
            'address': self.address,
            'utxo': str(self.utxo) if self.utxo else None,
            'witness_utxo': self.witness_utxo.serialize_to_network().hex() if self.witness_utxo else None,
            'sighash': self.sighash,
            'redeem_script': self.redeem_script.hex() if self.redeem_script else None,
            'witness_script': self.witness_script.hex() if self.witness_script else None,
            'part_sigs': {pubkey.hex(): sig.hex() for pubkey, sig in self.part_sigs.items()},
            'bip32_paths': {pubkey.hex(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                            for pubkey, (xfp, path) in self.bip32_paths.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_txin(cls, txin: TxInput, *, strip_witness: bool = True) -> 'PartialTxInput':
        # FIXME: if strip_witness is True,
        # res.estimated_size() will return an incorrect value. These methods
        # will return the correct values after we call add_input_info().
        # This is very fragile: the value returned by estimate_size() depends on the calling order.
        res = PartialTxInput(prevout=txin.prevout,
                             script_sig=None if strip_witness else txin.script_sig,
                             nsequence=txin.nsequence,
                             witness=None if strip_witness else txin.witness,
                             is_coinbase_output=txin.is_coinbase_output())
        return res

    def validate_data(self, *, for_signing=False) -> None:
        if self.utxo:
            if self.prevout.txid.hex() != self.utxo.txid():
                raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                  f"If a non-witness UTXO is provided, its hash must match the hash specified in the prevout")
            if self.witness_utxo:
                if self.utxo.outputs()[self.prevout.out_idx] != self.witness_utxo:
                    raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                      f"If both non-witness UTXO and witness UTXO are provided, they must be consistent")
        # The following test is disabled, so we are willing to sign legacy inputs
        # without verifying the input amount. This means, given a maliciously modified PSBT,
        # for legacy inputs, we might end up burning coins as miner fees.
        if self.redeem_script and self.address:
            addr = hash160_to_p2sh(hash_160(self.redeem_script))
            if self.address != addr:
                raise PSBTInputConsistencyFailure(f"PSBT input validation: "
                                                  f"If a redeemScript is provided, the scriptPubKey must be for that redeemScript")

    def parse_psbt_section_kv(self, kt, key, val):
        try:
            kt = PSBTInputType(kt)
        except ValueError:
            pass  # unknown type
        if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
        if kt == PSBTInputType.NON_WITNESS_UTXO:
            if self.utxo is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.utxo = Transaction(val)
            self.utxo.deserialize()
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.WITNESS_UTXO:
            if self.witness_utxo is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_utxo = TxOutput.from_network_bytes(val)
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.PARTIAL_SIG:
            if key in self.part_sigs:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.part_sigs[key] = val
        elif kt == PSBTInputType.SIGHASH_TYPE:
            if self.sighash is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(val) != 4:
                raise SerializationError(f"value for {repr(kt)} has unexpected length: {len(val)}")
            self.sighash = struct.unpack("<I", val)[0]
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.BIP32_DERIVATION:
            if key in self.bip32_paths:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.bip32_paths[key] = unpack_bip32_root_fingerprint_and_int_path(val)
        elif kt == PSBTInputType.REDEEM_SCRIPT:
            if self.redeem_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.redeem_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.WITNESS_SCRIPT:
            if self.witness_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.FINAL_SCRIPTSIG:
            if self.script_sig is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.script_sig = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTInputType.FINAL_SCRIPTWITNESS:
            if self.witness is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        else:
            full_key = self.get_fullkey_from_keytype_and_key(kt, key)
            if full_key in self._unknown:
                raise SerializationError(f'duplicate key. PSBT input key for unknown type: {full_key}')
            self._unknown[full_key] = val

    def serialize_psbt_section_kvs(self, wr):
        self.ensure_there_is_only_one_utxo()
        if self.witness_utxo:
            wr(PSBTInputType.WITNESS_UTXO, self.witness_utxo.serialize_to_network())
        if self.utxo:
            wr(PSBTInputType.NON_WITNESS_UTXO, bfh(self.utxo.serialize_to_network(include_sigs=True)))
        for pk, val in sorted(self.part_sigs.items()):
            wr(PSBTInputType.PARTIAL_SIG, val, pk)
        if self.sighash is not None:
            wr(PSBTInputType.SIGHASH_TYPE, struct.pack('<I', self.sighash))
        if self.redeem_script is not None:
            wr(PSBTInputType.REDEEM_SCRIPT, self.redeem_script)
        if self.witness_script is not None:
            wr(PSBTInputType.WITNESS_SCRIPT, self.witness_script)
        for k in sorted(self.bip32_paths):
            packed_path = pack_bip32_root_fingerprint_and_int_path(*self.bip32_paths[k])
            wr(PSBTInputType.BIP32_DERIVATION, packed_path, k)
        if self.script_sig is not None:
            wr(PSBTInputType.FINAL_SCRIPTSIG, self.script_sig)
        if self.witness is not None:
            wr(PSBTInputType.FINAL_SCRIPTWITNESS, self.witness)
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = self.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)

    def value_sats(self) -> Optional[int]:
        if self._trusted_value_sats is not None:
            return self._trusted_value_sats
        if self.utxo:
            out_idx = self.prevout.out_idx
            return self.utxo.outputs()[out_idx].value
        if self.witness_utxo:
            return self.witness_utxo.value
        return None

    @property
    def address(self) -> Optional[str]:
        if self._trusted_address is not None:
            return self._trusted_address
        scriptpubkey = self.scriptpubkey
        if scriptpubkey:
            return get_address_from_output_script(scriptpubkey)
        return None

    @property
    def scriptpubkey(self) -> Optional[bytes]:
        if self._trusted_address is not None:
            return bfh(bitcoin.address_to_script(self._trusted_address))
        if self.utxo:
            out_idx = self.prevout.out_idx
            return self.utxo.outputs()[out_idx].scriptpubkey
        if self.witness_utxo:
            return self.witness_utxo.scriptpubkey
        return None

    def set_script_type(self) -> None:
        if self.scriptpubkey is None:
            return
        type = get_script_type_from_output_script(self.scriptpubkey)
        inner_type = None
        if type is not None:
            if type == 'p2sh':
                inner_type = get_script_type_from_output_script(self.redeem_script)
            if inner_type is not None:
                type = inner_type + '-' + type
            if type == 'p2pkh':
                self.script_type = type
        return

    def is_complete(self) -> bool:
        if self.script_sig is not None and self.witness is not None:
            return True
        if self.is_coinbase_input():
            return True
        signatures = list(self.part_sigs.values())
        s = len(signatures)
        # note: The 'script_type' field is currently only set by the wallet,
        #       for its own addresses. This means we can only finalize inputs
        #       that are related to the wallet.
        #       The 'fix' would be adding extra logic that matches on templates,
        #       and figures out the script_type from available fields.
        if self.script_type in ('p2pk', 'p2pkh'):
            return s >= 1
        if self.script_type == 'p2sh':
            return s >= self.num_sig
        return False

    def finalize(self) -> None:
        def clear_fields_when_finalized():
            # BIP-174: "All other data except the UTXO and unknown fields in the
            #           input key-value map should be cleared from the PSBT"
            self.part_sigs = {}
            self.sighash = None
            self.bip32_paths = {}
            self.redeem_script = None
            self.witness_script = None

        if self.script_sig is not None and self.witness is not None:
            clear_fields_when_finalized()
            return  # already finalized
        if self.is_complete():
            self.script_sig = bfh(Transaction.input_script(self))
            self.witness = bfh(Transaction.serialize_witness(self))
            clear_fields_when_finalized()

    def combine_with_other_txin(self, other_txin: 'TxInput') -> None:
        assert self.prevout == other_txin.prevout
        if other_txin.script_sig is not None:
            self.script_sig = other_txin.script_sig
        if other_txin.witness is not None:
            self.witness = other_txin.witness
        if isinstance(other_txin, PartialTxInput):
            if other_txin.witness_utxo:
                self.witness_utxo = other_txin.witness_utxo
            if other_txin.utxo:
                self.utxo = other_txin.utxo
            self.part_sigs.update(other_txin.part_sigs)
            if other_txin.sighash is not None:
                self.sighash = other_txin.sighash
            self.bip32_paths.update(other_txin.bip32_paths)
            if other_txin.redeem_script is not None:
                self.redeem_script = other_txin.redeem_script
            if other_txin.witness_script is not None:
                self.witness_script = other_txin.witness_script
            self._unknown.update(other_txin._unknown)
        self.ensure_there_is_only_one_utxo()
        # try to finalize now
        self.finalize()

    def ensure_there_is_only_one_utxo(self):
        # we prefer having the full previous tx, even for non legacy inputs. see #6198
        # for witness v1, witness_utxo will be enough though
        if self.utxo is not None and self.witness_utxo is not None:
            self.witness_utxo = None

    def convert_utxo_to_witness_utxo(self) -> None:
        if self.utxo:
            self._witness_utxo = self.utxo.outputs()[self.prevout.out_idx]
            self._utxo = None  # type: Optional[Transaction]


    def already_has_some_signatures(self) -> bool:
        """Returns whether progress has been made towards completing this input."""
        return (self.part_sigs
                or self.script_sig is not None
                or self.witness is not None)


class PartialTxOutput(TxOutput, PSBTSection):
    def __init__(self, *args, **kwargs):
        TxOutput.__init__(self, *args, **kwargs)
        self.redeem_script = None  # type: Optional[bytes]
        self.witness_script = None  # type: Optional[bytes]
        self.bip32_paths = {}  # type: Dict[bytes, Tuple[bytes, Sequence[int]]]  # pubkey -> (xpub_fingerprint, path)
        self._unknown = {}  # type: Dict[bytes, bytes]

        self.script_type = 'unknown'
        self.num_sig = 0  # num req sigs for multisig
        self.pubkeys = []  # type: List[bytes]  # note: order matters
        self.is_mine = False  # type: bool  # whether the wallet considers the output to be ismine
        self.is_change = False  # type: bool  # whether the wallet considers the output to be change

    def to_json(self):
        d = super().to_json()
        d.update({
            'redeem_script': self.redeem_script.hex() if self.redeem_script else None,
            'witness_script': self.witness_script.hex() if self.witness_script else None,
            'bip32_paths': {pubkey.hex(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                            for pubkey, (xfp, path) in self.bip32_paths.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_txout(cls, txout: TxOutput) -> 'PartialTxOutput':
        res = PartialTxOutput(scriptpubkey=txout.scriptpubkey,
                              value=txout.value)
        return res

    def parse_psbt_section_kv(self, kt, key, val):
        try:
            kt = PSBTOutputType(kt)
        except ValueError:
            pass  # unknown type
        if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
        if kt == PSBTOutputType.REDEEM_SCRIPT:
            if self.redeem_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.redeem_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTOutputType.WITNESS_SCRIPT:
            if self.witness_script is not None:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            self.witness_script = val
            if key: raise SerializationError(f"key for {repr(kt)} must be empty")
        elif kt == PSBTOutputType.BIP32_DERIVATION:
            if key in self.bip32_paths:
                raise SerializationError(f"duplicate key: {repr(kt)}")
            if len(key) not in (33, 65):  # TODO also allow 32? one of the tests in the BIP is "supposed to" fail with len==32...
                raise SerializationError(f"key for {repr(kt)} has unexpected length: {len(key)}")
            self.bip32_paths[key] = unpack_bip32_root_fingerprint_and_int_path(val)
        else:
            full_key = self.get_fullkey_from_keytype_and_key(kt, key)
            if full_key in self._unknown:
                raise SerializationError(f'duplicate key. PSBT output key for unknown type: {full_key}')
            self._unknown[full_key] = val

    def serialize_psbt_section_kvs(self, wr):
        if self.redeem_script is not None:
            wr(PSBTOutputType.REDEEM_SCRIPT, self.redeem_script)
        if self.witness_script is not None:
            wr(PSBTOutputType.WITNESS_SCRIPT, self.witness_script)
        for k in sorted(self.bip32_paths):
            packed_path = pack_bip32_root_fingerprint_and_int_path(*self.bip32_paths[k])
            wr(PSBTOutputType.BIP32_DERIVATION, packed_path, k)
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = self.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)

    def combine_with_other_txout(self, other_txout: 'TxOutput') -> None:
        assert self.scriptpubkey == other_txout.scriptpubkey
        if not isinstance(other_txout, PartialTxOutput):
            return
        if other_txout.redeem_script is not None:
            self.redeem_script = other_txout.redeem_script
        if other_txout.witness_script is not None:
            self.witness_script = other_txout.witness_script
        self.bip32_paths.update(other_txout.bip32_paths)
        self._unknown.update(other_txout._unknown)


class PartialTransaction(Transaction):

    def __init__(self):
        Transaction.__init__(self, None)
        self.xpubs = {}  # type: Dict[BIP32Node, Tuple[bytes, Sequence[int]]]  # intermediate bip32node -> (xfp, der_prefix)
        self._inputs = []  # type: List[PartialTxInput]
        self._outputs = []  # type: List[PartialTxOutput]
        self._unknown = {}  # type: Dict[bytes, bytes]

    def to_json(self) -> dict:
        d = super().to_json()
        d.update({
            'xpubs': {bip32node.to_xpub(): (xfp.hex(), bip32.convert_bip32_intpath_to_strpath(path))
                      for bip32node, (xfp, path) in self.xpubs.items()},
            'unknown_psbt_fields': {key.hex(): val.hex() for key, val in self._unknown.items()},
        })
        return d

    @classmethod
    def from_tx(cls, tx: Transaction) -> 'PartialTransaction':
        res = cls()
        res._inputs = [PartialTxInput.from_txin(txin, strip_witness=True)
                       for txin in tx.inputs()]
        res._outputs = [PartialTxOutput.from_txout(txout) for txout in tx.outputs()]
        res.version = tx.version
        res.locktime = tx.locktime
        return res

    @classmethod
    def from_raw_psbt(cls, raw) -> 'PartialTransaction':
        # auto-detect and decode Base64 and Hex.
        if raw[0:10].lower() in (b'70736274ff', '70736274ff'):  # hex
            raw = bytes.fromhex(raw)
        elif raw[0:6] in (b'cHNidP', 'cHNidP'):  # base64
            raw = base64.b64decode(raw)
        if not isinstance(raw, (bytes, bytearray)) or raw[0:5] != b'psbt\xff':
            raise BadHeaderMagic("bad magic")

        tx = None  # type: Optional[PartialTransaction]

        # We parse the raw stream twice. The first pass is used to find the
        # PSBT_GLOBAL_UNSIGNED_TX key in the global section and set 'tx'.
        # The second pass does everything else.
        with io.BytesIO(raw[5:]) as fd:  # parsing "first pass"
            while True:
                try:
                    kt, key, val = PSBTSection.get_next_kv_from_fd(fd)
                except StopIteration:
                    break
                try:
                    kt = PSBTGlobalType(kt)
                except ValueError:
                    pass  # unknown type
                if kt == PSBTGlobalType.UNSIGNED_TX:
                    if tx is not None:
                        raise SerializationError(f"duplicate key: {repr(kt)}")
                    if key: raise SerializationError(f"key for {repr(kt)} must be empty")
                    unsigned_tx = Transaction(val.hex())
                    for txin in unsigned_tx.inputs():
                        if txin.script_sig or txin.witness:
                            raise SerializationError(f"PSBT {repr(kt)} must have empty scriptSigs and witnesses")
                    tx = PartialTransaction.from_tx(unsigned_tx)

        if tx is None:
            raise SerializationError(f"PSBT missing required global section PSBT_GLOBAL_UNSIGNED_TX")

        with io.BytesIO(raw[5:]) as fd:  # parsing "second pass"
            # global section
            while True:
                try:
                    kt, key, val = PSBTSection.get_next_kv_from_fd(fd)
                except StopIteration:
                    break
                try:
                    kt = PSBTGlobalType(kt)
                except ValueError:
                    pass  # unknown type
                if DEBUG_PSBT_PARSING: print(f"{repr(kt)} {key.hex()} {val.hex()}")
                if kt == PSBTGlobalType.UNSIGNED_TX:
                    pass  # already handled during "first" parsing pass
                elif kt == PSBTGlobalType.XPUB:
                    bip32node = BIP32Node.from_bytes(key)
                    if bip32node in tx.xpubs:
                        raise SerializationError(f"duplicate key: {repr(kt)}")
                    xfp, path = unpack_bip32_root_fingerprint_and_int_path(val)
                    if bip32node.depth != len(path):
                        raise SerializationError(f"PSBT global xpub has mismatching depth ({bip32node.depth}) "
                                                 f"and derivation prefix len ({len(path)})")
                    child_number_of_xpub = int.from_bytes(bip32node.child_number, 'big')
                    if not ((bip32node.depth == 0 and child_number_of_xpub == 0)
                            or (bip32node.depth != 0 and child_number_of_xpub == path[-1])):
                        raise SerializationError(f"PSBT global xpub has inconsistent child_number and derivation prefix")
                    tx.xpubs[bip32node] = xfp, path
                elif kt == PSBTGlobalType.VERSION:
                    if len(val) > 4:
                        raise SerializationError(f"value for {repr(kt)} has unexpected length: {len(val)} > 4")
                    psbt_version = int.from_bytes(val, byteorder='little', signed=False)
                    if psbt_version > 0:
                        raise SerializationError(f"Only PSBTs with version 0 are supported. Found version: {psbt_version}")
                    if key: raise SerializationError(f"key for {repr(kt)} must be empty")
                else:
                    full_key = PSBTSection.get_fullkey_from_keytype_and_key(kt, key)
                    if full_key in tx._unknown:
                        raise SerializationError(f'duplicate key. PSBT global key for unknown type: {full_key}')
                    tx._unknown[full_key] = val
            try:
                # inputs sections
                for txin in tx.inputs():
                    if DEBUG_PSBT_PARSING: print("-> new input starts")
                    txin._populate_psbt_fields_from_fd(fd)
                # outputs sections
                for txout in tx.outputs():
                    if DEBUG_PSBT_PARSING: print("-> new output starts")
                    txout._populate_psbt_fields_from_fd(fd)
            except UnexpectedEndOfStream:
                raise UnexpectedEndOfStream('Unexpected end of stream. Num input and output maps provided does not match unsigned tx.') from None

            if fd.read(1) != b'':
                raise SerializationError("extra junk at the end of PSBT")

        for txin in tx.inputs():
            txin.validate_data()

        return tx

    @classmethod
    def from_io(cls, inputs: Sequence[PartialTxInput], outputs: Sequence[PartialTxOutput], *,
                locktime: int = None, version: int = None):
        self = cls()
        self._inputs = list(inputs)
        self._outputs = list(outputs)
        if locktime is not None:
            self.locktime = locktime
        if version is not None:
            self.version = version
        self.BIP69_sort()
        return self

    def _serialize_psbt(self, fd) -> None:
        wr = PSBTSection.create_psbt_writer(fd)
        fd.write(b'psbt\xff')
        # global section
        wr(PSBTGlobalType.UNSIGNED_TX, bfh(self.serialize_to_network(include_sigs=False)))
        for bip32node, (xfp, path) in sorted(self.xpubs.items()):
            val = pack_bip32_root_fingerprint_and_int_path(xfp, path)
            wr(PSBTGlobalType.XPUB, val, key=bip32node.to_bytes())
        for full_key, val in sorted(self._unknown.items()):
            key_type, key = PSBTSection.get_keytype_and_key_from_fullkey(full_key)
            wr(key_type, val, key=key)
        fd.write(b'\x00')  # section-separator
        # input sections
        for inp in self._inputs:
            inp._serialize_psbt_section(fd)
        # output sections
        for outp in self._outputs:
            outp._serialize_psbt_section(fd)

    def finalize_psbt(self) -> None:
        for txin in self.inputs():
            txin.finalize()

    def combine_with_other_psbt(self, other_tx: 'Transaction') -> None:
        """Pulls in all data from other_tx we don't yet have (e.g. signatures).
        other_tx must be concerning the same unsigned tx.
        """
        if self.serialize_to_network(include_sigs=False) != other_tx.serialize_to_network(include_sigs=False):
            raise Exception('A Combiner must not combine two different PSBTs.')
        # BIP-174: "The resulting PSBT must contain all of the key-value pairs from each of the PSBTs.
        #           The Combiner must remove any duplicate key-value pairs, in accordance with the specification."
        # global section
        if isinstance(other_tx, PartialTransaction):
            self.xpubs.update(other_tx.xpubs)
            self._unknown.update(other_tx._unknown)
        # input sections
        for txin, other_txin in zip(self.inputs(), other_tx.inputs()):
            txin.combine_with_other_txin(other_txin)
        # output sections
        for txout, other_txout in zip(self.outputs(), other_tx.outputs()):
            txout.combine_with_other_txout(other_txout)
        self.invalidate_ser_cache()

    def join_with_other_psbt(self, other_tx: 'PartialTransaction') -> None:
        """Adds inputs and outputs from other_tx into this one."""
        if not isinstance(other_tx, PartialTransaction):
            raise Exception('Can only join partial transactions.')
        # make sure there are no duplicate prevouts
        prevouts = set()
        for txin in itertools.chain(self.inputs(), other_tx.inputs()):
            prevout_str = txin.prevout.to_str()
            if prevout_str in prevouts:
                raise Exception(f"Duplicate inputs! "
                                f"Transactions that spend the same prevout cannot be joined.")
            prevouts.add(prevout_str)
        # copy global PSBT section
        self.xpubs.update(other_tx.xpubs)
        self._unknown.update(other_tx._unknown)
        # copy and add inputs and outputs
        self.add_inputs(list(other_tx.inputs()))
        self.add_outputs(list(other_tx.outputs()))
        self.remove_signatures()
        self.invalidate_ser_cache()

    def inputs(self) -> Sequence[PartialTxInput]:
        return self._inputs

    def outputs(self) -> Sequence[PartialTxOutput]:
        return self._outputs

    def add_inputs(self, inputs: List[PartialTxInput]) -> None:
        self._inputs.extend(inputs)
        self.BIP69_sort(outputs=False)
        self.invalidate_ser_cache()

    def add_outputs(self, outputs: List[PartialTxOutput]) -> None:
        self._outputs.extend(outputs)
        self.BIP69_sort(inputs=False)
        self.invalidate_ser_cache()

    def BIP69_sort(self, inputs=True, outputs=True):
        # NOTE: other parts of the code rely on these sorts being *stable* sorts
        if inputs:
            self._inputs.sort(key = lambda i: (i.prevout.txid, i.prevout.out_idx))
        if outputs:
            self._outputs.sort(key = lambda o: (o.value, o.scriptpubkey))
        self.invalidate_ser_cache()

    def input_value(self) -> int:
        input_values = [txin.value_sats() for txin in self.inputs()]
        if any([val is None for val in input_values]):
            raise MissingTxInputAmount()
        return sum(input_values)

    def output_value(self) -> int:
        return sum(o.value for o in self.outputs())

    def get_fee(self) -> Optional[int]:
        try:
            return self.input_value() - self.output_value()
        except MissingTxInputAmount:
            return None

    def serialize_preimage(self, txin_index: int, *,
                           bip143_shared_txdigest_fields: BIP143SharedTxDigestFields = None) -> str:
        overwintered = self.overwintered
        version = self.version
        nHashType = int_to_hex(1, 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()
        txin = inputs[txin_index]
        if overwintered:
            nHeader = int_to_hex(0x80000000 | version, 4)
            nVersionGroupId = int_to_hex(self.versionGroupId, 4)
            s_prevouts = bfh(''.join(self.serialize_outpoint(txin)
                             for txin in inputs))
            hashPrevouts = blake2b(
                s_prevouts, digest_size=32, person=b'ZcashPrevoutHash').hexdigest()
            s_sequences = bfh(''.join(int_to_hex(txin.nsequence, 4)
                              for txin in inputs))
            hashSequence = blake2b(
                s_sequences, digest_size=32, person=b'ZcashSequencHash').hexdigest()
            s_outputs = bfh(''.join(self.serialize_output(o) for o in outputs))
            hashOutputs = blake2b(s_outputs, digest_size=32,
                                  person=b'ZcashOutputsHash').hexdigest()
            joinSplits = self.joinSplits
            #if joinSplits is None:
            #    hashJoinSplits = '00'*32
            #else:
            #    s_joinSplits = bfh(''.join(self.serialize_join_split(j) for j in joinSplits))
            #    s_joinSplits += self.joinSplitPubKey
            #    hashJoinSplits = blake2b(s_joinSplits, digest_size=32, person=b'SnowGemJSplitsHash').hexdigest()
            hashJoinSplits = '00'*32
            hashShieldedSpends = '00'*32
            hashShieldedOutputs = '00'*32
            nExpiryHeight = int_to_hex(self.expiryHeight, 4)
            nValueBalance = int_to_hex(self.valueBalance, 8)

            txin = inputs[txin_index]

            preimage_script = self.get_preimage_script(txin)
            scriptCode = var_int(len(preimage_script) // 2) + preimage_script
            preimage = (
                nHeader + nVersionGroupId + hashPrevouts + hashSequence + hashOutputs
                + hashJoinSplits + hashShieldedSpends + hashShieldedOutputs + nLocktime
                + nExpiryHeight + nValueBalance + nHashType
                + self.serialize_outpoint(txin)
                + scriptCode
                + int_to_hex(txin._trusted_value_sats, 8)
                + int_to_hex(txin.nsequence, 4)
            )
        else:
            nVersion = int_to_hex(version, 4)
            txins = var_int(len(inputs)) + ''.join(self.serialize_input(
                txin, self.get_preimage_script(txin) if txin_index == k else '') for k, txin in enumerate(inputs))
            txouts = var_int(len(outputs)) + \
                ''.join(self.serialize_output(o) for o in outputs)
            preimage = nVersion + txins + txouts + nLocktime + nHashType
        return preimage

    def sign(self, keypairs) -> None:
        # keypairs:  pubkey_hex -> (secret_bytes, is_compressed)
        bip143_shared_txdigest_fields = self._calc_bip143_shared_txdigest_fields()
        for i, txin in enumerate(self.inputs()):
            pubkeys = [pk.hex() for pk in txin.pubkeys]
            for pubkey in pubkeys:
                if txin.is_complete():
                    break
                if pubkey not in keypairs:
                    continue
                _logger.info(f"adding signature for {pubkey}")
                sec, compressed = keypairs[pubkey]
                sig = self.sign_txin(i, sec, bip143_shared_txdigest_fields=bip143_shared_txdigest_fields)
                self.add_signature_to_txin(txin_idx=i, signing_pubkey=pubkey, sig=sig)

        _logger.debug(f"is_complete {self.is_complete()}")
        self.invalidate_ser_cache()

    def sign_txin(self, txin_index, privkey_bytes, *, bip143_shared_txdigest_fields=None) -> str:
        txin = self.inputs()[txin_index]
        txin.validate_data(for_signing=True)
        pre_hash = sha256d(bfh(self.serialize_preimage(txin_index,
                                                       bip143_shared_txdigest_fields=bip143_shared_txdigest_fields)))
        privkey = ecc.ECPrivkey(privkey_bytes)
        sig = privkey.sign_transaction(pre_hash)
        sig = bh2u(sig) + '01'  # SIGHASH_ALL
        return sig

    def is_complete(self) -> bool:
        return all([txin.is_complete() for txin in self.inputs()])

    def signature_count(self) -> Tuple[int, int]:
        s = 0  # "num Sigs we have"
        r = 0  # "Required"
        for txin in self.inputs():
            if txin.is_coinbase_input():
                continue
            signatures = list(txin.part_sigs.values())
            s += len(signatures)
            r += txin.num_sig
        return s, r

    def serialize(self) -> str:
        """Returns PSBT as base64 text, or raw hex of network tx (if complete)."""
        self.finalize_psbt()
        if self.is_complete():
            return Transaction.serialize(self)
        return self._serialize_as_base64()

    def serialize_as_bytes(self, *, force_psbt: bool = False) -> bytes:
        """Returns PSBT as raw bytes, or raw bytes of network tx (if complete)."""
        self.finalize_psbt()
        if force_psbt or not self.is_complete():
            with io.BytesIO() as fd:
                self._serialize_psbt(fd)
                return fd.getvalue()
        else:
            return Transaction.serialize_as_bytes(self)

    def _serialize_as_base64(self) -> str:
        raw_bytes = self.serialize_as_bytes()
        return base64.b64encode(raw_bytes).decode('ascii')

    def update_signatures(self, signatures: Sequence[str]):
        """Add new signatures to a transaction

        `signatures` is expected to be a list of sigs with signatures[i]
        intended for self._inputs[i].
        This is used by the Trezor, KeepKey and Safe-T plugins.
        """
        if self.is_complete():
            return
        if len(self.inputs()) != len(signatures):
            raise Exception('expected {} signatures; got {}'.format(len(self.inputs()), len(signatures)))
        for i, txin in enumerate(self.inputs()):
            pubkeys = [pk.hex() for pk in txin.pubkeys]
            sig = signatures[i]
            if bfh(sig) in list(txin.part_sigs.values()):
                continue
            pre_hash = sha256d(bfh(self.serialize_preimage(i)))
            sig_string = ecc.sig_string_from_der_sig(bfh(sig[:-2]))
            for recid in range(4):
                try:
                    public_key = ecc.ECPubkey.from_sig_string(sig_string, recid, pre_hash)
                except ecc.InvalidECPointException:
                    # the point might not be on the curve for some recid values
                    continue
                pubkey_hex = public_key.get_public_key_hex(compressed=True)
                if pubkey_hex in pubkeys:
                    try:
                        public_key.verify_message_hash(sig_string, pre_hash)
                    except Exception:
                        _logger.exception('')
                        continue
                    _logger.info(f"adding sig: txin_idx={i}, signing_pubkey={pubkey_hex}, sig={sig}")
                    self.add_signature_to_txin(txin_idx=i, signing_pubkey=pubkey_hex, sig=sig)
                    break
        # redo raw
        self.invalidate_ser_cache()

    def add_signature_to_txin(self, *, txin_idx: int, signing_pubkey: str, sig: str):
        txin = self._inputs[txin_idx]
        txin.part_sigs[bfh(signing_pubkey)] = bfh(sig)
        # force re-serialization
        txin.script_sig = None
        txin.witness = None
        self.invalidate_ser_cache()

    def add_info_from_wallet(
            self,
            wallet: 'Abstract_Wallet',
            *,
            include_xpubs: bool = False,
            ignore_network_issues: bool = True,
    ) -> None:
        if self.is_complete():
            return
        # only include xpubs for multisig wallets; currently only they need it in practice
        # note: coldcard fw have a limitation that if they are included then all
        #       inputs are assumed to be multisig... https://github.com/spesmilo/electrum/pull/5440#issuecomment-549504761
        # note: trezor plugin needs xpubs included, if there are multisig inputs/change_outputs
        from .wallet import Multisig_Wallet
        if include_xpubs and isinstance(wallet, Multisig_Wallet):
            from .keystore import Xpub
            for ks in wallet.get_keystores():
                if isinstance(ks, Xpub):
                    fp_bytes, der_full = ks.get_fp_and_derivation_to_be_used_in_partial_tx(
                        der_suffix=[], only_der_suffix=False)
                    xpub = ks.get_xpub_to_be_used_in_partial_tx(only_der_suffix=False)
                    bip32node = BIP32Node.from_xkey(xpub)
                    self.xpubs[bip32node] = (fp_bytes, der_full)
        for txin in self.inputs():
            wallet.add_input_info(
                txin,
                only_der_suffix=False,
                ignore_network_issues=ignore_network_issues,
            )
        for txout in self.outputs():
            wallet.add_output_info(
                txout,
                only_der_suffix=False,
            )

    def remove_xpubs_and_bip32_paths(self) -> None:
        self.xpubs.clear()
        for txin in self.inputs():
            txin.bip32_paths.clear()
        for txout in self.outputs():
            txout.bip32_paths.clear()

    def prepare_for_export_for_coinjoin(self) -> None:
        """Removes all sensitive details."""
        # globals
        self.xpubs.clear()
        self._unknown.clear()
        # inputs
        for txin in self.inputs():
            txin.bip32_paths.clear()
        # outputs
        for txout in self.outputs():
            txout.redeem_script = None
            txout.witness_script = None
            txout.bip32_paths.clear()
            txout._unknown.clear()

    def convert_all_utxos_to_witness_utxos(self) -> None:
        """Replaces all NON-WITNESS-UTXOs with WITNESS-UTXOs.
        This will likely make an exported PSBT invalid spec-wise,
        but it makes e.g. QR codes significantly smaller.
        """
        for txin in self.inputs():
            txin.convert_utxo_to_witness_utxo()

    def remove_signatures(self):
        for txin in self.inputs():
            txin.part_sigs = {}
            txin.script_sig = None
            txin.witness = None
        assert not self.is_complete()
        self.invalidate_ser_cache()

    def update_txin_script_type(self):
        """Determine the script_type of each input by analyzing the scripts.
        It updates all tx-Inputs, NOT only the wallet owned, if the
        scriptpubkey is present.
        """
        for txin in self.inputs():
            if txin.script_type in ('unknown', 'address'):
                txin.set_script_type()

def pack_bip32_root_fingerprint_and_int_path(xfp: bytes, path: Sequence[int]) -> bytes:
    if len(xfp) != 4:
        raise Exception(f'unexpected xfp length. xfp={xfp}')
    return xfp + b''.join(i.to_bytes(4, byteorder='little', signed=False) for i in path)


def unpack_bip32_root_fingerprint_and_int_path(path: bytes) -> Tuple[bytes, Sequence[int]]:
    if len(path) % 4 != 0:
        raise Exception(f'unexpected packed path length. path={path.hex()}')
    xfp = path[0:4]
    int_path = [int.from_bytes(b, byteorder='little', signed=False) for b in chunks(path[4:], 4)]
    return xfp, int_path
