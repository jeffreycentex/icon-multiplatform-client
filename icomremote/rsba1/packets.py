"""RS-BA1 packet layouts.

Every packet starts with a 16-byte header:
    0..3   total length, uint32 little-endian
    4..5   type, uint16 little-endian
           0 = data / idle, 1 = retransmit request, 3 = are-you-there,
           4 = I-am-here, 5 = disconnect, 6 = are-you-ready, 7 = ping
    6..7   sequence number, uint16 little-endian
    8..11  sender session id, uint32 big-endian
    12..15 receiver session id, uint32 big-endian

Byte layouts of the login/auth/stream-request packets are copied byte for
byte from kappanhang's captures (controlstream.go); field names follow what
each byte is known to do, unknown bytes are kept as constants.
"""

import struct

HEADER = struct.Struct("<IHH")
SIDS = struct.Struct(">II")

T_DATA = 0x00
T_RETRANSMIT = 0x01
T_ARE_YOU_THERE = 0x03
T_I_AM_HERE = 0x04
T_DISCONNECT = 0x05
T_ARE_YOU_READY = 0x06
T_PING = 0x07

# Sizes of control-stream packets (also their identifying length field).
LEN_LOGIN = 0x80
LEN_LOGIN_REPLY = 0x60
LEN_AUTH = 0x40
LEN_STATUS = 0x50
LEN_CONNINFO = 0x90
CAP_HEADER_LEN = 0x42
CAP_RADIO_LEN = 0x66

# Audio codec ids used in the stream request (wfview naming).
CODEC_PCM16_MONO = 0x04


def header(length, ptype, seq, local_sid, remote_sid):
    return HEADER.pack(length, ptype, seq & 0xFFFF) + SIDS.pack(local_sid, remote_sid)


def parse_header(d):
    """-> (length, type, seq, sender_sid, receiver_sid) or None if too short."""
    if len(d) < 16:
        return None
    length, ptype, seq = HEADER.unpack_from(d, 0)
    a, b = SIDS.unpack_from(d, 8)
    return length, ptype, seq, a, b


def control(ptype, seq, local_sid, remote_sid):
    """16-byte control packet: are-you-there, are-you-ready, disconnect, idle."""
    return header(0x10, ptype, seq, local_sid, remote_sid)


def retransmit_request(seq, local_sid, remote_sid):
    return header(0x10, T_RETRANSMIT, seq, local_sid, remote_sid)


def retransmit_range_request(ranges, local_sid, remote_sid):
    """ranges: [(first, last), ...] inclusive sequence ranges."""
    body = b"".join(struct.pack("<HH", a & 0xFFFF, b & 0xFFFF) for a, b in ranges)
    return header(0x10 + len(body), T_RETRANSMIT, 0, local_sid, remote_sid) + body


def is_idle(d):
    return len(d) == 16 and d[0] == 0x10 and d[4] == T_DATA and d[5] == 0


def is_retransmit_request(d):
    return len(d) >= 16 and d[4] == T_RETRANSMIT and d[5] == 0 and d[1:4] == b"\x00\x00\x00"


def parse_retransmit_request(d):
    """-> list of inclusive (first, last) ranges the peer wants resent."""
    if len(d) == 16:
        seq = struct.unpack_from("<H", d, 6)[0]
        return [(seq, seq)]
    out = []
    body = d[16:]
    for i in range(0, len(body) - 3, 4):
        out.append(struct.unpack_from("<HH", body, i))
    return out


# ---------------------------------------------------------------- ping (type 7)

PING_LEN = 21


def is_ping(d):
    # First byte is 0x15 from the PC and 0x00 or 0x15 from the radio.
    return len(d) == PING_LEN and d[1:6] == b"\x00\x00\x00\x07\x00"


def ping(seq, local_sid, remote_sid, reply, ping_id):
    """reply=False: our ping (ping_id is 4 bytes we choose).
    reply=True: answer to the radio's ping, echoing its seq and ping_id."""
    return (header(PING_LEN, T_PING, seq, local_sid, remote_sid)
            + bytes([1 if reply else 0]) + bytes(ping_id))


# ---------------------------------------------------------------- control stream

def _inner(length_code, request_type, magic, inner_seq):
    """Bytes 16..25 shared by login/auth/stream-request packets."""
    return bytes([0x00, 0x00, 0x00, length_code, request_type, magic, 0x00,
                  inner_seq & 0xFF, (inner_seq >> 8) & 0xFF, 0x00])


def login(local_sid, remote_sid, inner_seq, auth_start_id, user_enc, pass_enc):
    p = bytearray(LEN_LOGIN)
    p[0:16] = header(LEN_LOGIN, T_DATA, 0, local_sid, remote_sid)
    p[16:26] = _inner(0x70, 0x01, 0x00, inner_seq)
    p[26:28] = auth_start_id
    p[64:80] = user_enc
    p[80:96] = pass_enc
    p[96:104] = b"icom-pc\x00"
    return bytes(p)


def auth(local_sid, remote_sid, inner_seq, auth_id, magic):
    """magic 0x02 = token confirm after login, 0x05 = (re)auth, 0x01 = deauth."""
    p = bytearray(LEN_AUTH)
    p[0:16] = header(LEN_AUTH, T_DATA, 0, local_sid, remote_sid)
    p[16:26] = _inner(0x30, 0x01, magic, inner_seq)
    p[26:32] = auth_id
    return bytes(p)


def stream_request(local_sid, remote_sid, inner_seq, auth_id, radio_guid, radio_name,
                   user_enc, sample_rate, serial_port, audio_port, tx_buffer_ms):
    """Ask the radio to open the serial (CI-V) and audio streams."""
    p = bytearray(LEN_CONNINFO)
    p[0:16] = header(LEN_CONNINFO, T_DATA, 0, local_sid, remote_sid)
    p[16:26] = _inner(0x80, 0x01, 0x03, inner_seq)
    p[26:32] = auth_id
    p[32:48] = radio_guid
    name = radio_name.encode("ascii", "replace")[:31]
    p[64:64 + len(name)] = name
    p[96:112] = user_enc
    p[112] = 0x01                   # RX enable
    p[113] = 0x01                   # TX enable
    p[114] = CODEC_PCM16_MONO       # RX codec
    p[115] = CODEC_PCM16_MONO       # TX codec
    struct.pack_into(">I", p, 116, sample_rate)   # RX rate
    struct.pack_into(">I", p, 120, sample_rate)   # TX rate
    struct.pack_into(">I", p, 124, serial_port)
    struct.pack_into(">I", p, 128, audio_port)
    struct.pack_into(">I", p, 132, tx_buffer_ms)
    p[136] = 0x01
    return bytes(p)


def cstring(b):
    return bytes(b).split(b"\x00", 1)[0].decode("ascii", "replace")


class RadioCapabilities:
    """Parsed 0xA8.. capabilities packet (one record per radio behind the server)."""

    def __init__(self, guid, name, audio_name, civ_address, raw):
        self.guid = guid
        self.name = name
        self.audio_name = audio_name
        self.civ_address = civ_address
        self.raw = raw

    def __repr__(self):
        return "RadioCapabilities(name=%r, civ=0x%02X, audio=%r)" % (
            self.name, self.civ_address, self.audio_name)


def parse_capabilities(d):
    """-> list[RadioCapabilities]. Layout: 0x42-byte header, big-endian radio
    count at 64, then 0x66 bytes per radio: guid[16] name[32] audio[32]
    conntype[2] civ[1] ..."""
    if len(d) < CAP_HEADER_LEN + CAP_RADIO_LEN:
        return []
    count = struct.unpack_from(">H", d, 64)[0] or 1
    out = []
    for i in range(count):
        o = CAP_HEADER_LEN + i * CAP_RADIO_LEN
        if o + CAP_RADIO_LEN > len(d):
            break
        out.append(RadioCapabilities(
            guid=bytes(d[o:o + 16]),
            name=cstring(d[o + 16:o + 48]),
            audio_name=cstring(d[o + 48:o + 80]),
            civ_address=d[o + 82],
            raw=bytes(d[o:o + CAP_RADIO_LEN])))
    return out


def is_capabilities(d):
    return (len(d) >= CAP_HEADER_LEN + CAP_RADIO_LEN and d[4:6] == b"\x00\x00"
            and (len(d) - CAP_HEADER_LEN) % CAP_RADIO_LEN == 0
            and struct.unpack_from("<I", d, 0)[0] == len(d))


# ---------------------------------------------------------------- serial stream

SERIAL_DATA = 0xC1
SERIAL_OPEN_CLOSE = 0xC0


def serial_data(local_sid, remote_sid, inner_seq, civ):
    n = len(civ)
    return (header(0x15 + n, T_DATA, 0, local_sid, remote_sid)
            + bytes([SERIAL_DATA, n, 0x00, (inner_seq >> 8) & 0xFF, inner_seq & 0xFF]) + bytes(civ))


def serial_open_close(local_sid, remote_sid, inner_seq, close):
    return (header(0x16, T_DATA, 0, local_sid, remote_sid)
            + bytes([SERIAL_OPEN_CLOSE, 0x01, 0x00, (inner_seq >> 8) & 0xFF, inner_seq & 0xFF,
                     0x00 if close else 0x05]))


def is_serial_data(d):
    return len(d) >= 22 and d[16] == SERIAL_DATA and d[0] - 0x15 == d[17]


# ---------------------------------------------------------------- audio stream

AUDIO_HEADER_LEN = 24
TX_PART1_BYTES = 1364      # one 20 ms frame at 48 kHz 16-bit mono = 1920 bytes,
TX_PART2_BYTES = 556       # sent as two packets like the Icom software does


def audio_data(local_sid, remote_sid, inner_seq, pcm):
    n = len(pcm)
    return (header(AUDIO_HEADER_LEN + n, T_DATA, 0, local_sid, remote_sid)
            + bytes([0x80, 0x00, (inner_seq >> 8) & 0xFF, inner_seq & 0xFF, 0x00, 0x00,
                     (n >> 8) & 0xFF, n & 0xFF]) + bytes(pcm))


def is_audio_data(d):
    if len(d) <= AUDIO_HEADER_LEN or d[4:6] != b"\x00\x00":
        return False
    return struct.unpack_from("<I", d, 0)[0] == len(d)


def set_seq(pkt, seq):
    b = bytearray(pkt)
    struct.pack_into("<H", b, 6, seq & 0xFFFF)
    return bytes(b)


def redact(d):
    """Blank the (reversibly) encoded password in a login packet before logging."""
    if len(d) == LEN_LOGIN and d[16:20] == b"\x00\x00\x00\x70":
        return bytes(d[:80]) + b"\x00" * 16 + bytes(d[96:])
    return d


def hexdump(d, limit=96):
    d = redact(d)
    s = d[:limit].hex(" ")
    return s + (" …(+%d)" % (len(d) - limit) if len(d) > limit else "")
