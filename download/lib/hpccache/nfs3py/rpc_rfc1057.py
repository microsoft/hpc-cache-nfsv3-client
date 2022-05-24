#
# lib/hpccache/nfs3py/rpc_rfc1057.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
# This was originally derived from the Demo/rpc code distributed
# with Python 2.7.11. It has since been updated for Python3
# and substantially restructured.
#
# Sun RPC version 2 -- RFC 1057
#
# Individual clients are not thread-safe. Different threads
# may use different clients concurrently without issue.
#
# Timeouts:
# timeoutRel is a timeout that is relative to the current time.
# timeoutAbs is an absolute time
# None = no timeout
#
# Layering:
#   make_call: External API to do an RPC. This performs retries
#              as necessary. The return value is an ONCRPCres
#              instance (usually some subclass). Exceptions at
#              lower layers are captured. When fatal, they map
#              to ONCRPCresfail with the status set to a string
#              describing the exception.
#   _make_call_internal: Internal API for a single RPC try. This
#                        does the marshalling and unmarshalling
#                        (pickling) of args and results as necessary.
#
# To ensure timeout consistency, make_call() takes the timeout
# argument relative to the current time (timeoutRel). When it
# invokes _make_call_internal(), it passes an absolute timeout
# (timeoutAbs). That way, no matter what iterations are done
# within _make_call_internal() and lower layers, the logical
# timeout for the single RPC attempt is constant. make_call() may
# be invoked with timeoutAbs
#
import errno
import functools
import inspect
import io
import logging
import os
import pprint
import socket
import struct
import sys
import threading
import time
import traceback

######################################################################

class LoggerState():
    '''
    Encapsulate logger state and operations
    '''
    LOGGER_NAME_DEFAULT = 'hpcache.rpc_rfc1057'
    LOG_LEVEL = logging.DEBUG
    _LOG_FORMAT_BASE = '%(asctime)s %(levelname).3s'
    #_LOG_FORMAT_BASE = '%(asctime)s %(levelname).3s %(name)s:%(module)s:%(funcName)s:%(lineno)s:'
    _log_format_extra = ''

    _lock = threading.RLock()
    _logger = None

    @classmethod
    def logger_get(cls, logger=None, log_level=None, **kwargs):
        '''
        Return logger to use. If the logger must be created,
        log_level specifies the level. Otherwise, log_level is
        ignored.
        '''
        if logger:
            return logger
        with cls._lock:
            return cls._logger_get_NL(log_level=log_level, **kwargs)

    @classmethod
    def _logger_get_NL(cls, log_level=None, **kwargs):
        if cls._logger is not None:
            return cls._logger
        name = kwargs.pop('name', cls.LOGGER_NAME_DEFAULT)
        log_level = log_level if log_level is not None else cls.LOG_LEVEL
        logging.basicConfig(format=cls._log_format_get_NL(), stream=sys.stdout)
        logger = logging.getLogger(name=name)
        logger.setLevel(log_level)
        return logger

    @classmethod
    def logger_set(cls, logger):
        '''
        Discard the current logger if it exists and use this logger instead.
        '''
        with cls._lock:
            cls._logger = logger

    @classmethod
    def log_format_get(cls):
        with cls._lock:
            return cls._log_format_get_NL()

    @classmethod
    def _log_format_get_NL(cls):
        ret = cls._LOG_FORMAT_BASE
        if cls._log_format_extra:
            ret += ' '
            ret += cls._log_format_extra
        ret += ' %(message)s'
        return ret

######################################################################

RPCVERSION = 2

AUTH_NULL = 0
AUTH_UNIX = 1
AUTH_SHORT = 2
AUTH_DES = 3

# Default timeout (in seconds) for a single RPC attempt.
# See make_call() for details.
timeoutRel_default = 30.0

# Default number of times to attempt an RPC in the face of
# failures at this level.
# See make_call() for details.
callTries_default = 2

# The RPCTimeout exception is used internally. External callers get a
# result ONCRPCresfail with the status set to the string "RPCTimeout".
class RPCTimeout(Exception):
    pass

class ProtocolError(Exception):
    pass

class ShortRead(ProtocolError):
    pass

class PortUnavailableException(Exception):
    pass

# annotated_enum is used in place of raw integers in cases where a protocol definition
# provides an enumeration of legal values. This is available to external entities
# such as nfs3.py, but users are expected to always subclass annotated_enum rather
# than using it directly.
#
# The idea is that by using an annotated_enum subclass in place of a raw integer,
# other operations may verify that an enum of the correct type is passed as an
# argument, preventing for example passing a mode3 where a createmode3 is expected,
# even though both are named "mode" in RFC 1813.
#
# The only comparators allowed on annotated_enum are equality checks. Ordering checks
# are forbidden. (Obviously, subclasses could define their own behaviors if desired.)
# The behavior of the equality checks is configurable with two parameters:
# mismatch_cmp_allow and mismatch_cmp_except. The default behavior is that any
# attempt to compare mismatched annotated_enums throws an exception. Setting
# mismatch_cmp_except=False suppresses that, in which case mismatch_cmp_allow
# specifies the behavior of the comparison; if mismatch_cmp_allow=True, then
# the integer values are compared for equality. If mismatch_cmp_allow=False, then
# two annotated_enums of different types are considered not equal regardless
# of the value specified. When mismatch_cmp_except=True, mismatch_cmp_allow
# is not considered. Note that when comparing with == and !=, the behavior is
# defined by the leftmost item (eg 'x' in 'x==y').
#
# Although operations such as >, >=, <, <= are forbidden on annotated_enum,
# a __cmp__() method is defined that provides the expected ordered-comparison
# behavior for the values after applying the same sanity checks for
# mismatched types that are used for equality checks.
#
# An annotated_enum may be evaluated as a bool. In that case, the
# semantic is to compare the integral value with a predefined successval
# set at __init__() time. If the successval is None, then the bool
# is False. Otherwise, the bool is true iff the value is not equal
# to the success value. Think of this as evaluating whether or not there
# is an error; a single value in the num can represent success, while
# any other value indicates error. __bool__() represents an is-error check.
#
# Subclasses of annotated_enum define the legal values in the enumeration
# by defining the dict _XLAT. Keys in the dict are legal integers
# in the enumeration. Values are human-readable strings. For example, a
# trivial enumeration might be:
#   class single_digit_number(annotated_enum):
#       _SUCCESSVAL = None
#       _XLAT = {0 : "ZERO",
#                1 : "ONE",
#                2 : "TWO",
#                3 : "THREE",
#                4 : "FOUR",
#                5 : "FIVE",
#                6 : "SIX",
#                7 : "SEVEN",
#                8 : "EIGHT",
#                9 : "NONE",
#               }
#  Because None is passed for successval, every instance of single_digit_number
#  evaluates False as a bool.
#  This:
#      print("%s" % single_digit_number(8))
#  prints:
#      8(EIGHT)
#
@functools.total_ordering
class annotated_enum():
    """
    annotated_enum is used in place of raw integers in cases where a protocol definition
    provides an enumeration of legal values. By default, comparisons of two different
    annotated_enum subclasses generates a TypeError exception to trap cases where
    a caller is checking against a value of the wrong type.
    """
    def __init__(self, val):
        if not isinstance(val, int):
            raise TypeError("expected int or long for val, got %s" % type(val).__name__)
        self._val = val

    _XLAT = dict()

    _SUCCESSVAL = 0
    _MISMATCH_CMP_ALLOW = False
    _MISMATCH_CMP_EXCEPT = False

    def __str__(self):
        return "%s(%s)" % (self._val, self._XLAT.get(self._val, "?"))

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, self._val)

    def __bool__(self):
        if self._SUCCESSVAL is None:
            return False
        return self._val != self._SUCCESSVAL

    def __index__(self):
        return self._val

    def __int__(self):
        return self._val

    def __hash__(self):
        return hash(self._val)

    def __eq__(self, other):
        if isinstance(other, annotated_enum):
            if type(self) is not type(other):
                if self._MISMATCH_CMP_ALLOW:
                    return False
                logger = LoggerState.logger_get()
                logger.warning("attempt to compare (eq) mismatched enums %s and %s", type(self).__name__, type(other).__name__)
                if self._MISMATCH_CMP_EXCEPT:
                    raise TypeError("attempt to compare (eq) mismatched enums %s and %s" % (type(self).__name__, type(other).__name__))
            return int(self) == int(other)
        if isinstance(other, str):
            if self._MISMATCH_CMP_ALLOW:
                return False
            logger = LoggerState.logger_get()
            logger.warning("attempt to compare (eq) %s with non-annotated_enum %s \"%s\"", type(self).__name__, type(other).__name__, other)
        else:
            logger = LoggerState.logger_get()
            logger.warning("attempt to compare (eq) %s with non-annotated_enum %s", type(self).__name__, type(other).__name__)
        if self._MISMATCH_CMP_EXCEPT:
            raise TypeError("attempt to compare (eq) %s with non-annotated_enum %s" % (type(self).__name__, type(other).__name__))
        return self._val == other

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        raise TypeError("annotated_enum: illegal operation")

    def __le__(self, other):
        raise TypeError("annotated_enum: illegal operation")

    def __gt__(self, other):
        raise TypeError("annotated_enum: illegal operation")

    def __ge__(self, other):
        raise TypeError("annotated_enum: illegal operation")

    def write(self, stream):
        '''
        Write this to a stream
        '''
        # No kidding. Signed. See RFC 1014.
        stream.write(struct.pack('!i', self._val))

    @property
    def value(self):
        return self._val

    @property
    def string(self):
        return self._XLAT.get(self._val, "?")

    @classmethod
    def string_from_value(cls, value):
        if isinstance(value, cls):
            value = int(value)
        return cls._XLAT.get(value, '?')

    def to_dict(self):
        return {'value' : self.value,
                'string' : self.string,
               }

    @classmethod
    def valid(cls, value):
        try:
            value = int(value)
        except Exception:
            return False
        return value in cls._XLAT

class msg_type(annotated_enum):
    _XLAT = {0 : 'CALL',
             1 : 'REPLY',
            }
CALL = msg_type(0)
REPLY = msg_type(1)

class reply_stat(annotated_enum):
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {0 : 'MSG_ACCEPTED',
             1 : 'MSG_DENIED',
            }
MSG_ACCEPTED = reply_stat(0)
MSG_DENIED = reply_stat(1)

class accept_stat(annotated_enum):
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {0 : 'SUCCESS',
             1 : 'PROG_UNAVAIL',
             2 : 'PROG_MISMATCH',
             3 : 'PROC_UNAVAIL',
             4 : 'GARBAGE_ARGS',
            }
SUCCESS = accept_stat(0)
PROG_UNAVAIL = accept_stat(1)
PROG_MISMATCH = accept_stat(2)
PROC_UNAVAIL = accept_stat(3)
GARBAGE_ARGS = accept_stat(4)

class reject_stat(annotated_enum):
    _SUCCESSVAL = None
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {0 : 'RPC_MISMATCH',
             1 : 'AUTH_ERROR',
            }
RPC_MISMATCH = reject_stat(0)
AUTH_ERROR = reject_stat(1)

class Auth_stat(annotated_enum):
    _SUCCESSVAL = None
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {1 : 'AUTH_BADCRED',
             2 : 'AUTH_REJECTEDCRED',
             3 : 'AUTH_BADVERF',
             4 : 'AUTH_REJECTEDVERF',
             5 : 'AUTH_TOOWEAK',
            }
AUTH_BADCRED = Auth_stat(1)
AUTH_REJECTEDCRED = Auth_stat(2)
AUTH_BADVERF = Auth_stat(3)
AUTH_REJECTEDVERF = Auth_stat(4)
AUTH_TOOWEAK = Auth_stat(5)

class ONCRPCwire():
    '''
    base class for things that go on/off the wire
    '''
    def __repr__(self):
        return type(self).__name__ + '(' + ','.join(self.desc()) + ')'

    # _DESC_ATTRS_1 goes in front ordered from super to sub
    # _DESC_ATTRS_2 goes in back ordered from sub to super
    #
    _DESC_ATTRS_1 = list()
    _DESC_ATTRS_2 = list()

    _DESC_ATTRS_FMT = dict()
    _DESC_ATTRS_FMT_WALKED = None

    @classmethod
    def _desc_attrs_fmt(cls):
        '''
        Return a disc of attribute format overrides
        '''
        if cls._DESC_ATTRS_FMT_WALKED is not None:
            return cls._DESC_ATTRS_FMT_WALKED
        ret = dict()
        mro = inspect.getmro(cls)
        for k in reversed(mro):
            try:
                attrs = getattr(k, '_DESC_ATTRS_FMT')
            except AttributeError:
                continue
            ret.update(attrs)
        cls._DESC_ATTRS_FMT_WALKED = ret
        return ret

    def _desc_attrs(self):
        '''
        Return a list of attribute names interesting for desc()
        '''
        ret = list()
        prev_attrs = None
        mro = inspect.getmro(type(self))
        for k in mro:
            try:
                attrs = getattr(k, '_DESC_ATTRS_2')
            except AttributeError:
                continue
            if attrs is prev_attrs:
                continue
            ret.extend(attrs)
            prev_attrs = attrs
        for k in reversed(mro):
            try:
                attrs = getattr(k, '_DESC_ATTRS_1')
            except AttributeError:
                continue
            if attrs is prev_attrs:
                continue
            ret = attrs + ret
            prev_attrs = attrs
        return ret

    def _desc_val(self, key):
        '''
        Given a key that is an attribute of self,
        return it stringified. The default return
        is repr(). If there is a format override
        in self._desc_attrs_fmt(), use that instead.
        Subclasses may also override this for special cases.
        '''
        val = getattr(self, key)
        try:
            fmt = self._desc_attrs_fmt()[key]
        except KeyError:
            return repr(val)
        return fmt % val

    def _desc_simple(self, attrs):
        '''
        Helper for _desc that includes key=val for every attribute in attrs.
        '''
        return [a + '=' + self._desc_val(a) for a in attrs if getattr(self, a) is not None]

    def desc_str(self):
        '''
        Return self.desc() as a comma-separated string
        '''
        return ','.join(self.desc())

    def desc(self):
        '''
        Return a list describing this object.
        Items in the list are 'key=value'. This list
        is used to generate repr.
        '''
        return self._desc_simple(self._desc_attrs())

    @staticmethod
    def write_enum(stream, val):
        '''
        Write val to stream as an enum (signed int)
        '''
        stream.write(struct.pack('!i', val))

    @staticmethod
    def write_uint32(stream, val):
        '''
        Write val to stream as uint32
        '''
        stream.write(struct.pack('!I', val))

    @classmethod
    def write_string(cls, stream, data):
        '''
        Write the given string to stream
        '''
        if not isinstance(data, (bytes, bytearray)):
            if isinstance(data, str):
                data = os.fsencode(data)
            else:
                raise TypeError("unexpected data %s" % type(data).__name__)
        cls.write_bytes(stream, data)

    @classmethod
    def write_bytes(cls, stream, data):
        '''
        Write the given bytes to stream
        '''
        stream.write(struct.pack('!I', len(data)))
        cls.write_then_align(stream, data, 4)

    @staticmethod
    def write_auth_null(stream):
        pass

    @classmethod
    def write_auth_unix(cls, stream, seed, host, uid, gid, gids):
        stream.write(struct.pack('!I', seed))
        cls.write_string(stream, host)
        stream.write(struct.pack('!III', uid, gid, len(gids)))
        for g in gids:
            stream.write(struct.pack('!I', g))

    @classmethod
    def read_exact(cls, stream, count, logger=None):
        '''
        Read count bytes from stream. Raise an exception
        if we cannot do so.
        '''
        ret = stream.read(count)
        if len(ret) != count:
            logger = LoggerState.logger_get(logger=logger)
            logger.debug("%s short read from:\n%s", cls.__name__, stack_pformat())
            raise ShortRead("expected=%d got=%d" % (count, len(ret)))
        return ret

    @classmethod
    def read_then_align(cls, stream, count, align):
        '''
        Read count bytes from stream, then seek
        forward so skip bytes rounding count up to align.
        '''
        ret = cls.read_exact(stream, count)
        extra = count % align
        if extra:
            cls.read_exact(stream, align-extra)
        return ret

    @classmethod
    def write_then_align(cls, stream, data, align):
        '''
        Write count bytes to stream followed
        by enough zeros to get to the desired alignment.
        '''
        stream.write(data)
        extra = len(data) % align
        if extra:
            stream.write(bytes(align-extra))

    @classmethod
    def read_uint64(cls, stream):
        return struct.unpack('!Q', cls.read_exact(stream, 8))[0]

    @classmethod
    def read_uint32(cls, stream):
        return struct.unpack('!I', cls.read_exact(stream, 4))[0]

    @classmethod
    def read_int32(cls, stream):
        return struct.unpack('!i', cls.read_exact(stream, 4))[0]

    @classmethod
    def read_bool(cls, stream):
        return bool(struct.unpack('!i', cls.read_exact(stream, 4))[0])

    @classmethod
    def read_enum(cls, stream):
        # Really. Signed. See RFC 1014.
        return struct.unpack('!i', cls.read_exact(stream, 4))[0]

    @classmethod
    def read_bytes(cls, stream):
        '''
        Reverse of write_bytes().
        '''
        count = cls.read_uint32(stream)
        return cls.read_exact(stream, count)

    @classmethod
    def discard_auth(cls, stream):
        '''
        Read and discard auth from stream
        '''
        _, stuff_len = struct.unpack('!iI', cls.read_exact(stream, 8))
        stream.seek(stuff_len, io.SEEK_CUR)

    @classmethod
    def read(cls, stream): # pylint: disable=unused-argument
        '''
        Generate from a stream
        '''
        return cls()

    @classmethod
    def read_array(cls, stream, read_item):
        '''
        Read a sequence if items by invoking read_item(stream)
        and return the results as a list.
        '''
        count = cls.read_uint32(stream)
        return [read_item(stream) for _ in range(count)]

    @classmethod
    def read_list(cls, stream, read_item):
        '''
        Read a sequence if items by invoking read_item(stream)
        and return the results as a list.
        '''
        ret = list()
        while True:
            nextval = cls.read_uint32(stream)
            if nextval == 0:
                return ret
            if nextval != 1:
                raise ProtocolError("%s(%s) unexpected nextval=%d" % (cls.__name__, read_item, nextval))
            ret.append(read_item(stream))

    def write(self, stream):
        '''
        Write this to a stream
        '''
        # Nothing to do here

class _ONCRPCargres(ONCRPCwire):
    '''
    base class for ONCRPCargs and ONCRPCres
    '''
    def __init__(self, xid=None):
        self.xid = xid

    _DESC_ATTRS_2 = ['xid']

class ONCRPCargs(_ONCRPCargres):
    '''
    Generic arguments for ONC RPC
    '''
    @property
    def procname(self):
        ret = type(self).__name__
        if ret.endswith('args'):
            ret = ret[:-4]
        if ret.endswith('_'):
            ret = ret[:-1]
        return ret

class ONCRPCres(_ONCRPCargres):
    '''
    Note: ONCRPCres may itself be success or failure.
    ONCRPCresfail is always a failure at the RPC level-
    for example, the RPC timed out.
    '''
    def __init__(self, xid=None, status=None):
        super().__init__(xid=xid)
        self.status = status

    _DESC_ATTRS_1 = ['status']

    _DESC_ATTRS_FMT = {'status' : '%s',
                      }

    @property
    def procname(self):
        ret = type(self).__name__
        if ret.endswith('res'):
            return ret[:-3]
        return ret

    def __int__(self):
        return int(self.status)

    def __bool__(self):
        return bool(self.status)

class ONCRPCresfail(ONCRPCres):
    '''
    ONCRPCres that is always failure
    '''
    # No specialization here

class ONCRPCresfailTimeout(ONCRPCresfail):
    '''
    ONCRPCres that is a timeout
    '''
    def __init__(self, xid=None):
        super().__init__(xid=xid, status='RPCTimeout')

class NULLargs(ONCRPCargs):
    '''
    Generic NULL RPC args for any protocol
    '''
    # No specialization here

class NULLres(ONCRPCres):
    '''
    Generic NULL RPC res for any protocol
    The RPC has no status code of its own. We need a status
    to reflect errors at other levels, so we artificially
    use 0 for success.
    '''
    def __init__(self, **kwargs):
        kwargs.setdefault('status', 0)
        super().__init__(**kwargs)

class Credential(ONCRPCwire):
    '''
    Generic credential
    '''
    def __init__(self, flavor, data):
        self.flavor = flavor
        self.data = data

    _DESC_ATTRS_2 = ['flavor']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.write_enum(stream, self.flavor)
        self.write_bytes(stream, self.data)

class _RawTCPClient():
    '''
    Client using TCP to a specific port.
    '''
    def __init__(self, host, prog=None, vers=None, port=None, bindaddr=None, reserved=False):
        '''
        Binds to host:port.
        If reserved is set, use a reserved (< 1024) port.
        '''
        if not host:
            raise ValueError('host')
        self.host = host
        self.prog = prog if prog is not None else self._PROG
        self.vers = vers if vers is not None else self._VERS
        self.port = port if port is not None else self._PORT
        self._bindaddr = bindaddr if bindaddr is not None else ''
        self._reserved = reserved
        self.sock = None
        self._ever_tried_reconnect = False
        self.need_reconnect = True
        self._credlock = threading.Lock()
        self.cred = None
        self._verflock = threading.Lock()
        self.verf = None

    _PROG = None
    _VERS = None
    _PORT = 0

    _lastxid_lock = threading.Lock()
    _lastxid = 0 # incremented before first use, so the first one we use is 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def reconnect(self, logger=None):
        try:
            # Skip logging the initial connection attempt
            if self._ever_tried_reconnect:
                logger = LoggerState.logger_get(logger=logger)
                logger.debug("reconnect begin host=%s port=%s prog=%s", self.host, self.port, self.prog)
            if self.sock is not None:
                self.close()
                self.sock = None
            self.makesocket() # Assigns to self.sock
            self.bindsocket()
            self.connsocket()
            self.cred = None
            self.verf = None
            self.need_reconnect = False
            if self._ever_tried_reconnect:
                logger.debug("reconnect complete host=%s port=%s prog=%s", self.host, self.port, self.prog)
        finally:
            self._ever_tried_reconnect = True

    def makesocket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def connsocket(self):
        # Override this if you don't want/need a connection
        self.sock.connect((self.host, self.port))

    def bindsocket(self):
        if not self._reserved:
            self.sock.bind((self._bindaddr, 0)) # hostname, portnum
            return
        port = 1024
        while port > 1:
            port -= 1
            try:
                self.sock.bind((self._bindaddr, port))
                return
            except OSError as e:
                if e.errno == errno.EADDRINUSE:
                    continue
                raise
        raise PortUnavailableException("no reserved ports available")

    @classmethod
    def xid_used(cls, xid):
        with cls._lastxid_lock:
            cls._lastxid = max(cls._lastxid, xid)

    @classmethod
    def get_new_xid(cls):
        with cls._lastxid_lock:
            cls._lastxid += 1
            return cls._lastxid

    def make_call(self, proc, rpcargs_class, rpcargs, res_class, timeoutRel=None, timeoutAbs=None, callTries=None, xid=None, logger=None):
        '''
        timeoutRel: how long to wait for each try
        callTries: how many tries, total
        Example: timeoutRel=30.0,callTries=2 means that the RPC will
        be issued with a 30-second timeout. If that hits, then it is re-issued
        with the same xid. If that times out after 30 seconds, we've now tries
        2 times (callTries), so we give up and return a timeout error.
        '''
        callTries = callTries if callTries is not None else callTries_default
        if not issubclass(rpcargs_class, ONCRPCargs):
            raise TypeError("non-args type %s for rpcargs_class" % rpcargs_class.__name__)
        if not inspect.isclass(rpcargs_class):
            raise TypeError("expected a class for rpcargs_class")
        if not isinstance(rpcargs, rpcargs_class):
            raise TypeError("rpcargs expected %s, got %s" % (rpcargs_class.__name__, type(rpcargs).__name__))
        if not inspect.isclass(res_class):
            raise TypeError("expected a class for res_class")
        if not issubclass(res_class, ONCRPCres):
            raise TypeError("res_class expected ONCRPCres, got %s" % type(res_class).__name__)
        if callTries < 1:
            raise ValueError("illegal callTries %s" % callTries)
        if xid is None:
            xid = rpcargs.xid if rpcargs.xid is not None else self.get_new_xid()
        if not isinstance(xid, int):
            raise TypeError("xid expected int, got %s" % type(xid).__name__)

        if timeoutAbs is not None:
            if timeoutRel is not None:
                raise ValueError("may not specify both timeoutAbs and timeoutRel")
        else:
            timeoutRel = timeoutRel if timeoutRel is not None else timeoutRel_default

        tries = 0
        while tries < callTries:
            tries += 1
            if timeoutRel is not None:
                timeoutAbs = time.time() + timeoutRel
            try:
                res = self._make_call_internal(proc, rpcargs, res_class, xid, timeoutAbs, logger=logger)
                if not isinstance(res, ONCRPCres):
                    self.need_reconnect = True
                    raise TypeError("non-res type %s from _make_call_internal" % type(res).__name__)
                res.xid = xid
                return res
            except RuntimeError as e:
                self.need_reconnect = True
                if tries < callTries:
                    logger = LoggerState.logger_get(logger=logger)
                    logger.warning("make_call %s RuntimeError %s, will retry", rpcargs.procname, e)
                else:
                    logger.warning("make_call %s RuntimeError %s, will not retry", rpcargs.procname, e)
                    return ONCRPCresfail(xid=xid, status="RuntimeError %s"%e)
            except (ConnectionResetError, EOFError, ProtocolError, RPCTimeout) as e:
                if not isinstance(e, RPCTimeout):
                    # Do not reconnect on timeout so we can get xid matching.
                    self.need_reconnect = True
                if tries < callTries:
                    logger = LoggerState.logger_get(logger=logger)
                    logger.warning("make_call %s %s, will retry", rpcargs.procname, repr(e))
                else:
                    if isinstance(e, RPCTimeout):
                        return ONCRPCresfailTimeout(xid=xid)
                    logger.warning("make_call %s %s, will not retry", rpcargs.procname, repr(e))
                    return ONCRPCresfail(status='RPCFailed', xid=xid)
            except Exception as e:
                self.need_reconnect = True
                logger = LoggerState.logger_get(logger=logger)
                logger.warning("traceback:\n%s", traceback.format_exc())
                if tries < callTries:
                    logger.warning("make_call %s %s, will retry", rpcargs.procname, repr(e))
                else:
                    logger.warning("make_call %s %s, will not retry", rpcargs.procname, repr(e))
                    status_str = "Exception:%s" % repr(e)
                    return ONCRPCresfail(xid=xid, status=status_str)
        # This is really unreachable - above, we return the most recent failure
        self.need_reconnect = True
        return ONCRPCresfail(xid=xid, status="RPCTriesExceeded")

    def _start_call(self, stream, proc, xid):
        self.xid_used(xid)
        stream.write(struct.pack('!IiIIII',
                                 xid,
                                 CALL.value,
                                 RPCVERSION,
                                 self.prog,
                                 self.vers,
                                 proc))
        cred = self.mkcred()
        cred.write(stream)
        verf = self.mkverf()
        verf.write(stream)

    @staticmethod
    def make_auth_null():
        return bytes(0)

    @staticmethod
    def make_auth_unix():
        '''
        Return pickled AUTH_UNIX for the current user
        '''
        stream = io.BytesIO()
        hostname = socket.getfqdn() # Must use this and not socket.gethostname() so that reverse lookups map back
        ONCRPCwire.write_auth_unix(stream, int(time.time()), hostname, os.getuid(), os.getgid(), os.getgroups()) # pylint: disable=no-member,useless-suppression
        stream.seek(0, io.SEEK_SET)
        return stream.read()

    @staticmethod
    def make_auth_unix_euid():
        '''
        Return pickled AUTH_UNIX for the current effective user
        '''
        stream = io.BytesIO()
        hostname = socket.getfqdn() # Must use this and not socket.gethostname() so that reverse lookups map back
        ONCRPCwire.write_auth_unix(stream, int(time.time()), hostname, os.geteuid(), os.getegid(), os.getgroups()) # pylint: disable=no-member,useless-suppression
        stream.seek(0, io.SEEK_SET)
        return stream.read()

    def mkcred_null(self):
        '''
        AUTH_NULL
        '''
        with self._credlock:
            if self.cred is None:
                self.cred = Credential(AUTH_NULL, self.make_auth_null())
            return self.cred

    def mkverf_null(self):
        '''
        NULL auth
        '''
        with self._verflock:
            if self.verf is None:
                self.verf = Credential(AUTH_NULL, self.make_auth_null())
            return self.verf

    mkcred = mkcred_null
    mkverf = mkverf_null

    def mkcred_unix(self):
        '''
        AUTH_UNIX
        '''
        with self._credlock:
            try:
                if self.cred.flavor == AUTH_UNIX:
                    return self.cred
            except AttributeError:
                pass
            self.cred = Credential(AUTH_UNIX, self.make_auth_unix())
            return self.cred

    def mkcred_unix_euid(self):
        '''
        AUTH_UNIX using euid
        '''
        with self._credlock:
            try:
                if self.cred.flavor == AUTH_UNIX:
                    return self.cred
            except AttributeError:
                pass
            self.cred = Credential(AUTH_UNIX, self.make_auth_unix_euid())
            return self.cred

    def call_0(self, logger=None):
        '''
        Procedure 0 is always like this
        '''
        return self.make_call(0, "PROC0", None, None, None, logger=logger)

    def _recvwrapper(self, bufsize, timeoutAbs):
        timeoutRel = timeoutAbs - time.time()
        if timeoutRel <= 0.0:
            raise RPCTimeout()
        self.sock.settimeout(timeoutRel)
        try:
            buf = self.sock.recv(bufsize)
        except socket.timeout as e:
            self.need_reconnect = True
            raise RPCTimeout() from e
        except:
            self.need_reconnect = True
            raise
        finally:
            self.sock.settimeout(None)
        return buf

    def _recvfrag(self, recordbuf, timeoutAbs, logger=None):
        header = self._recvwrapper(4, timeoutAbs)
        if len(header) < 4:
            logger = LoggerState.logger_get(logger=logger)
            logger.debug("_recvfrag: len(header)=%s (short)", len(header))
            self.need_reconnect = True
            raise EOFError()
        x = struct.unpack('!I', header)[0]
        last = ((x & 0x80000000) != 0)
        n = int(x & 0x7fffffff)
        while n > 0:
            buf = self._recvwrapper(n, timeoutAbs)
            if not buf:
                logger = LoggerState.logger_get(logger=logger)
                logger.debug("_recvfrag: n=%s buf=%s", n, buf)
                self.need_reconnect = True
                raise EOFError()
            n = n - len(buf)
            recordbuf.write(buf)
        return last

    def _recvrecord(self, timeoutAbs, logger=None):
        recordbuf = io.BytesIO()
        last = 0
        while not last:
            last = self._recvfrag(recordbuf, timeoutAbs, logger=logger)
        recordbuf.seek(0, io.SEEK_SET)
        return recordbuf

    def _sendrecord(self, record_stream, timeoutAbs):
        '''
        Send one logical record. The input is record_stream,
        which is io.BytesIO with 4 bytes reserved at the beginning.
        '''
        # Compute and write the header, then extract
        # the entire stream contents as a single buffer.

        # set record_header to the length of the stream (including the reserved bytes)
        record_header = record_stream.seek(0, io.SEEK_END)
        assert record_header >= 4

        # adjust record_header to the length of the stream without
        # the reserved bytes, then mark that this is the only
        # record and update the stream with the new header.
        record_header -= 4 # length of record
        record_header |= 0x80000000 # no records follow

        record_stream.seek(0, io.SEEK_SET)
        record_stream.write(struct.pack('!I', record_header))
        record_stream.seek(0, io.SEEK_SET)

        # extract the entire stream contents (including header) as a single buffer
        record = record_stream.read()

        timeoutRel = timeoutAbs - time.time()
        if timeoutRel <= 0.0:
            raise RPCTimeout()
        self.sock.settimeout(timeoutRel)

        try:
            self.sock.send(record)
        except socket.timeout as e:
            self.need_reconnect = True
            raise RPCTimeout() from e
        except:
            self.need_reconnect = True
            raise
        finally:
            self.sock.settimeout(None)

    def _make_call_internal(self, proc, rpcargs, res_class, expect_xid, timeoutAbs, logger=None):
        '''
        Make a call and process the response. Return the
        response as either res_class or ONCRPCresfail.
        '''
        if self.need_reconnect:
            self.reconnect(logger=logger)
            if self.need_reconnect:
                return ONCRPCresfail(xid=expect_xid, status="reconnect failed")
        arg_stream = io.BytesIO()
        arg_stream.write(bytes(4)) # reserve space for record length
        self._start_call(arg_stream, proc, expect_xid)
        try:
            rpcargs.write(arg_stream)
        except Exception as e:
            logger = LoggerState.logger_get(logger=logger)
            logger.warning("cannot write args %s: %s", rpcargs, repr(e))
            raise
        self._sendrecord(arg_stream, timeoutAbs)
        reply_stream = self._recvrecord(timeoutAbs, logger=logger)
        xid, mtype, stat = struct.unpack('!Iii', reply_stream.read(12))
        if mtype != REPLY.value:
            self.need_reconnect = True
            return ONCRPCresfail(xid=xid, status="mtype %s is not REPLY" % mtype)
        if xid != expect_xid:
            # Should not happen - we assume single-threaded, and this is TCP
            self.need_reconnect = True
            return ONCRPCresfail(xid=expect_xid, status="wrong xid in reply: got %s expected %s" % (xid, expect_xid))
        if stat == MSG_ACCEPTED.value:
            res_class.discard_auth(reply_stream)
            astat = res_class.read_enum(reply_stream)
            if astat != SUCCESS.value:
                return ONCRPCresfail(xid=xid, status=accept_stat(astat))
        elif stat == MSG_DENIED.value:
            dstat = res_class.read_enum(reply_stream)
            if dstat == RPC_MISMATCH.value:
                reply_stream.seek(8, io.SEEK_CUR)
                return ONCRPCresfail(xid=xid, status=reject_stat(dstat))
            if dstat == AUTH_ERROR.value:
                astat = res_class.read_uint32(reply_stream)
                return ONCRPCresfail(xid=xid, status=Auth_stat(dstat))
            return ONCRPCresfail(xid=xid, status="MSG_DENIED with unexpected dstatus in reply (%s)" % dstat)
        else:
            self.need_reconnect = True
            return ONCRPCresfail(xid=xid, status="unexpected status in reply (%s)" % stat)
        # RPC succeeded at the ONC level
        ret = res_class.read(reply_stream)
        ret.xid = xid
        return ret

# Port mapper interface

# Program number, version and (fixed!) port number
# See RFC 1833 (Binding Protocols for ONC RPC Version 2)
PMAP_PROG = 100000
PMAP_VERS = 2
PMAP_PORT = 111

# Procedure numbers
PMAPPROC_NULL = 0                       # (void) -> void
PMAPPROC_SET = 1                        # (mapping) -> bool
PMAPPROC_UNSET = 2                      # (mapping) -> bool
PMAPPROC_GETPORT = 3                    # (mapping) -> unsigned int
PMAPPROC_DUMP = 4                       # (void) -> pmaplist
PMAPPROC_CALLIT = 5                     # (call_args) -> call_result

# A mapping is (prog, vers, prot, port) and prot is one of:
IPPROTO_TCP = 6

class GETPORT_args(ONCRPCargs):
    def __init__(self, prog=None, vers=None, prot=None, port=None):
        super().__init__()
        self.prog = int(prog)
        self.vers = int(vers)
        self.prot = int(prot)
        self.port = int(port)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        stream.write(struct.pack('!IIII', self.prog, self.vers, self.prot, self.port))

    _DESC_ATTRS_2 = ['prog', 'vers', 'prot', 'port']

class GETPORT_res(ONCRPCres):
    def __init__(self, port=None):
        super().__init__()
        self.port = port

    _DESC_ATTRS_2 = ['port']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        kwargs = {'port' : struct.unpack('!I', stream.read(4))[0]}
        return cls(**kwargs)

class TCPPortMapperClient(_RawTCPClient):
    _PROG = PMAP_PROG
    _VERS = PMAP_VERS
    _PORT = PMAP_PORT

    def __init__(self, *args, **kwargs):
        logger = LoggerState.logger_get(logger=kwargs.pop('logger', None))
        super().__init__(*args, **kwargs)
        try:
            self.reconnect(logger=logger)
        except Exception as e:
            logger.warning("traceback:\n%s", traceback.format_exc())
            raise RuntimeError("%s: cannot connect to host=%s port=%s for prog=%s vers=%s: %s" % (type(self).__name__, self.host, self.port, self.prog, self.vers, repr(e))) from e

    def GETPORT(self, rpcargs, **kwargs):
        return self.make_call(PMAPPROC_GETPORT, GETPORT_args, rpcargs, GETPORT_res, **kwargs)

class TCPClient(_RawTCPClient):
    def __init__(self, *args, **kwargs):
        logger = LoggerState.logger_get(logger=kwargs.pop('logger', None))
        super().__init__(*args, **kwargs)
        try:
            getport_args = GETPORT_args(prog=self.prog, vers=self.vers, prot=IPPROTO_TCP, port=0)
        except Exception as e:
            logger.warning("cannot construct GETPORT_args: %s from\n%s", repr(e), exc_stack_pformat())
            raise
        with TCPPortMapperClient(self.host, logger=logger) as port_mapper_client:
            res = port_mapper_client.GETPORT(getport_args) # pylint: disable=no-member # pylint is wrong
        if isinstance(res, ONCRPCresfail):
            raise RuntimeError("%s: program=%s vers=%s cannot contact portmapper: %s" % (type(self).__name__, self.prog, self.vers, res))
        if not isinstance(res, GETPORT_res):
            raise RuntimeError("%s: unexpected response %s" % (type(self).__name__, repr(res)))
        if res.port == 0:
            raise RuntimeError('TCPClient: program=%s vers=%s not registered with portmapper' % (self.prog, self.vers))
        self.port = res.port
        try:
            self.reconnect(logger=logger)
        except Exception as e:
            logger.warning("traceback:\n%s", traceback.format_exc())
            raise RuntimeError("%s: cannot connect to host=%s port=%s for prog=%s vers=%s: %s" % (type(self).__name__, self.host, self.port, self.prog, self.vers, repr(e))) from e

def exc_stacklines():
    '''
    Invoked from within an exception handler. Returns traceback.format_exc().splitlines()
    with the exception string removed from the end.
    '''
    exc_info = sys.exc_info()
    exception_class_names = [exc_info[0].__module__+'.'+getattr(exc_info[0], '__qualname__', '?'), exc_info[0].__name__]
    del exc_info
    lines = traceback.format_exc().splitlines()
    lastline = lines[-1]
    for exception_class_name in exception_class_names:
        if lastline.startswith(exception_class_name+': '):
            lines = lines[:-1]
            break
    return lines

def exc_stack_pformat():
    '''
    Invoked from within an exception handler. Returns a human
    readable string for the exception stack.
    '''
    return pprint.pformat(exc_stacklines())

def stack_pformat():
    '''
    Return the current stack in human-readable form
    '''
    return pprint.pformat(traceback.format_stack())
