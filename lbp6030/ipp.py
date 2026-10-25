"""IPP message encoding (RFC 8010): requests are parsed into attribute groups, responses are built from
(tag, name, values) tuples. Collections are dicts of member name -> (tag, values)."""
import struct

# delimiter tags
OPERATION, JOB, END, PRINTER, UNSUPPORTED = 0x01, 0x02, 0x03, 0x04, 0x05
# value tags
UNKNOWN, NO_VALUE = 0x12, 0x13
INTEGER, BOOLEAN, ENUM = 0x21, 0x22, 0x23
OCTETS, DATETIME, RESOLUTION, RANGE, BEGIN_COLLECTION, END_COLLECTION = 0x30, 0x31, 0x32, 0x33, 0x34, 0x37
TEXT, NAME, KEYWORD, URI, URI_SCHEME, CHARSET, LANGUAGE, MIME, MEMBER = (
    0x41, 0x42, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x4A)
TEXT_LANG, NAME_LANG = 0x35, 0x36

OK = 0x0000
OK_IGNORED = 0x0001
BAD_REQUEST = 0x0400
NOT_POSSIBLE = 0x0404
NOT_FOUND = 0x0406
FORMAT_NOT_SUPPORTED = 0x040A
ATTRIBUTES_NOT_SUPPORTED = 0x040B
COMPRESSION_NOT_SUPPORTED = 0x040F
COMPRESSION_ERROR = 0x0410
OPERATION_NOT_SUPPORTED = 0x0501
VERSION_NOT_SUPPORTED = 0x0503
INTERNAL_ERROR = 0x0500


class Request:
    def __init__(self, data):
        if len(data) < 9:
            raise ValueError("short IPP message")
        self.version = (data[0], data[1])
        self.operation, self.request_id = struct.unpack(">HI", data[2:8])
        self.groups = []        # [(group tag, {name: (value tag, [values])})]
        self.data = b""         # document data after the attributes
        try:
            self._parse(data, 8)
        except (struct.error, IndexError, KeyError, TypeError) as e:
            raise ValueError(f"malformed IPP message: {e!r}") from None

    def _parse(self, data, i):
        attrs, last, stack = None, None, []
        while i < len(data):
            tag = data[i]
            i += 1
            if tag == END:
                self.data = data[i:]
                return
            if tag < 0x10:
                if stack:
                    raise ValueError("group inside a collection")
                attrs, last = {}, None
                self.groups.append((tag, attrs))
                continue
            name, i = _field(data, i)
            raw, i = _field(data, i)
            name = name.decode("utf-8", "replace")
            if attrs is None:
                raise ValueError("attribute outside a group")
            if tag == BEGIN_COLLECTION:
                coll = {}
                if stack:
                    parent, member = stack[-1]
                    if member is None:
                        raise ValueError("collection value without a member name")
                    parent.setdefault(member, (tag, []))[1].append(coll)
                elif name:
                    attrs[name] = (tag, [coll])
                    last = name
                elif last is not None:
                    attrs[last][1].append(coll)
                else:
                    raise ValueError("additional value without an attribute")
                stack.append([coll, None])
            elif tag == END_COLLECTION:
                if not stack:
                    raise ValueError("end of collection without a collection")
                stack.pop()
            elif tag == MEMBER:
                if not stack:
                    raise ValueError("member name outside a collection")
                stack[-1][1] = raw.decode("utf-8", "replace")
            elif stack:
                coll, member = stack[-1]
                if member is None:
                    raise ValueError("collection value without a member name")
                coll.setdefault(member, (tag, []))[1].append(decode_value(tag, raw))
            else:
                if name:
                    attrs[name] = (tag, [decode_value(tag, raw)])
                    last = name
                elif last is not None:
                    attrs[last][1].append(decode_value(tag, raw))
                else:
                    raise ValueError("additional value without an attribute")
        raise ValueError("missing end-of-attributes tag")

    def group(self, tag):
        merged = {}
        for t, attrs in self.groups:
            if t == tag:
                merged.update(attrs)
        return merged

    def get(self, name, default=None, group=None):
        """first value of an attribute, from the given group or any group"""
        for t, attrs in self.groups:
            if (group is None or t == group) and name in attrs and attrs[name][1]:
                return attrs[name][1][0]
        return default

    def values(self, name):
        for _, attrs in self.groups:
            if name in attrs:
                return attrs[name][1]
        return []


def _field(data, i):
    """2-byte length + bytes -> (bytes, next index)"""
    if i + 2 > len(data):
        raise ValueError("truncated IPP message")
    (n,) = struct.unpack(">H", data[i:i + 2])
    if i + 2 + n > len(data):
        raise ValueError("truncated IPP message")
    return data[i + 2:i + 2 + n], i + 2 + n


def decode_value(tag, raw):
    if tag in (INTEGER, ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == BOOLEAN and len(raw) == 1:
        return bool(raw[0])
    if tag == RANGE and len(raw) == 8:
        return struct.unpack(">ii", raw)
    if tag == RESOLUTION and len(raw) == 9:
        return struct.unpack(">iib", raw)
    if tag in (OCTETS, DATETIME) or tag < 0x40 and tag not in (TEXT_LANG, NAME_LANG):
        return raw
    if tag in (TEXT_LANG, NAME_LANG) and len(raw) >= 4:   # language length, language, text length, text
        (n,) = struct.unpack(">H", raw[:2])
        raw = raw[4 + n:]
    return raw.decode("utf-8", "replace")


def encode_value(tag, value):
    if tag in (INTEGER, ENUM):
        return struct.pack(">i", value)
    if tag == BOOLEAN:
        return bytes([1 if value else 0])
    if tag == RANGE:
        return struct.pack(">ii", *value)
    if tag == RESOLUTION:
        return struct.pack(">iib", *value)
    if tag in (NO_VALUE, UNKNOWN):
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _item(tag, name, raw):
    n = name.encode("utf-8")
    return bytes([tag]) + struct.pack(">H", len(n)) + n + struct.pack(">H", len(raw)) + raw


def encode_attr(tag, name, values):
    out = bytearray()
    for k, value in enumerate(values):
        nm = name if k == 0 else ""
        if tag == BEGIN_COLLECTION:
            out += _item(BEGIN_COLLECTION, nm, b"")
            for member, (mtag, mvalues) in value.items():
                out += _item(MEMBER, "", member.encode("utf-8"))
                out += encode_attr(mtag, "", mvalues)
            out += _item(END_COLLECTION, "", b"")
        else:
            out += _item(tag, nm, encode_value(tag, value))
    return bytes(out)


def response(request_id, status, groups, version=(2, 0), message=None):
    """groups: [(group tag, [(value tag, name, [values])])]; the operation group is added in front"""
    op = [(CHARSET, "attributes-charset", ["utf-8"]), (LANGUAGE, "attributes-natural-language", ["en"])]
    if message:
        op.append((TEXT, "status-message", [message]))
    out = bytearray(bytes(version) + struct.pack(">HI", status, request_id))
    for gtag, attrs in [(OPERATION, op)] + list(groups):
        out.append(gtag)
        for tag, name, values in attrs:
            out += encode_attr(tag, name, values)
    out.append(END)
    return bytes(out)
