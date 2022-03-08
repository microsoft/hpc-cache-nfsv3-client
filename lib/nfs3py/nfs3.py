#!/usr/bin/env python
#
# lib/nfs3py/nfs3.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
# Naming matches RFC 1813 as closely as possible to make it clear how the
# spec maps to the code. Structures are slightly different; to keep things
# simple, resok/resfail unions are collapsed out. To avoid conflicts,
# some attributes are prefixed with 'a'. For example, object -> aobject
# (conflicts with builtin object), name -> aname (conflicts with class name).
#
# For both collapsed resok/resfail and for unions in general, any arm
# that does not exist under the discriminator has undefined contents,
# though by convention it is set to None.
#
import functools
import os
import struct
import time

import nfs3py.rpc_rfc1057 as rpc_rfc1057
from nfs3py.rpc_rfc1057 import (NULLargs,
                                      NULLres,
                                      ONCRPCargs,
                                      ONCRPCres,
                                      ONCRPCwire,
                                      ProtocolError,
                                      TCPClient,
                                      annotated_enum,
                                     )

# From RFC 1833 (Binding Protocols for ONC RPC Version 2)
PMAP_PORT = 111
PORTMAPPER_PROG = 100000

NSEC_PER_SEC = 1000000000.0

# From RFC 1813 (NFS Version 3 Protocol Specification)
#
MOUNT3PROG = 100005
MOUNT3VERS = 3 # duh
MOUNTPROC3_NULL = 0
MOUNTPROC3_MNT = 1
MOUNTPROC3_DUMP = 2
MOUNTPROC3_UMNT = 3
MOUNTPROC3_UMNTALL = 4
MOUNTPROC3_EXPORT = 5
#
NFS3PROG = 100003
NFS3VERS = 3 # duh
NFSPROC3_NULL = 0
NFSPROC3_GETATTR = 1
NFSPROC3_SETATTR = 2
NFSPROC3_LOOKUP = 3
NFSPROC3_ACCESS = 4
NFSPROC3_READLINK = 5
NFSPROC3_READ = 6
NFSPROC3_WRITE = 7
NFSPROC3_CREATE = 8
NFSPROC3_MKDIR = 9
NFSPROC3_SYMLINK = 10
NFSPROC3_MKNOD = 11
NFSPROC3_REMOVE = 12
NFSPROC3_RMDIR = 13
NFSPROC3_RENAME = 14
NFSPROC3_LINK = 15
NFSPROC3_READDIR = 16
NFSPROC3_READDIRPLUS = 17
NFSPROC3_FSSTAT = 18
NFSPROC3_FSINFO = 19
NFSPROC3_PATHCONF = 20
NFSPROC3_COMMIT = 21
#
MNTPATHLEN = 1024
MNTNAMLEN = 255
FHSIZE3 = 64
#
NFS3_FHSIZE = 64
NFS3_COOKIEVERFSIZE = 8
NFS3_CREATEVERFSIZE = 8
NFS3_WRITEVERFSIZE = 8
#
ACCESS3_READ    = 0x0001 # pylint: disable=bad-whitespace
ACCESS3_LOOKUP  = 0x0002 # pylint: disable=bad-whitespace
ACCESS3_MODIFY  = 0x0004 # pylint: disable=bad-whitespace
ACCESS3_EXTEND  = 0x0008 # pylint: disable=bad-whitespace
ACCESS3_DELETE  = 0x0010 # pylint: disable=bad-whitespace
ACCESS3_EXECUTE = 0x0020
#
FSF3_LINK        = 0x0001 # pylint: disable=bad-whitespace
FSF3_SYMLINK     = 0x0002 # pylint: disable=bad-whitespace
FSF3_HOMOGENEOUS = 0x0008
FSF3_CANSETTIME  = 0x0010 # pylint: disable=bad-whitespace


class LoggerState(rpc_rfc1057.LoggerState):
    LOGGER_NAME_DEFAULT = 'nfs3'

    @classmethod
    def logger_set(cls, logger):
        with cls._lock:
            cls._logger = logger
            super().logger_set(logger)

class OkayOrFailRPCres(ONCRPCres):
    '''
    Layer smarts about resok vs resfail on ONCRPCres
    '''
    _DESC_ATTRS_2_OK = []
    _DESC_ATTRS_2_FAIL = []

    def _desc_attrs(self):
        da = super()._desc_attrs()
        if not (self._DESC_ATTRS_2_OK or self._DESC_ATTRS_2_FAIL):
            return da
        assert da[0] == 'status'
        if not self.status:
            return [da[0]] + self._DESC_ATTRS_2_OK + da[1:]
        return [da[0]] + self._DESC_ATTRS_2_FAIL + da[1:]

class NFS3RPCres(OkayOrFailRPCres):
    '''
    Layer smarts about resok vs resfail on ONCRPCres
    '''
    @classmethod
    def generate_from_status(cls, stream, xid=None):
        '''
        Generate from a stream populating the initial status
        '''
        status = nfsstat3(cls.read_int32(stream))
        return cls(status=status, xid=xid)

@functools.total_ordering
class vararray(ONCRPCwire):
    '''
    Wrapper around bytes. This class does not add
    functionality over raw bytes, but it provides
    a consistent API for subclasses to do so.
    '''
    def __init__(self, data=None):
        self._buf = None
        self.buf = data

    def __len__(self):
        return len(self._buf)

    _MAX_LOG_LEN = 8

    def __str__(self):
        if len(self._buf) > self._MAX_LOG_LEN:
            return "len=%d,0x%s..." % (len(self._buf), self._buf[:self._MAX_LOG_LEN].hex())
        return self._buf.hex()

    def __repr__(self):
        if len(self._buf) > self._MAX_LOG_LEN:
            return "%s(len=%d,0x%s...)" % (type(self).__name__, len(self._buf), self._buf[:self._MAX_LOG_LEN].hex())
        return "%s(0x%s)" % (type(self).__name__, self._buf.hex())

    def __hash__(self):
        return hash(self._buf)

    def __eq__(self, other):
        if other is None:
            return False
        if isinstance(other, (bytearray, bytes)):
            return self._buf == other
        if isinstance(other, vararray):
            return self._buf == other.buf
        if isinstance(other, str):
            return self._buf == os.fsencode(other)
        raise TypeError("illegal equality check between %s and %s" % (type(self).__name__, type(other).__name__))

    def __lt__(self, other):
        if other is None:
            return False
        if isinstance(other, (bytearray, bytes)):
            return self._buf < other
        if isinstance(other, vararray):
            return self._buf < other.buf
        raise TypeError("illegal compare between %s and %s" % (type(self).__name__, type(other).__name__))

    def __ne__(self, other):
        return not self.__eq__(other)

    def __bool__(self):
        return bool(self._buf)

    def __bytes__(self):
        return self._buf

    def to_string(self):
        return os.fsdecode(self._buf)

    @property
    def buf(self):
        return self._buf

    @buf.setter
    def buf(self, data):
        # Enforce: self._buf may be written at most once
        assert self._buf is None
        if data is None:
            self._buf = bytes()
        elif isinstance(data, bytearray):
            self._buf = bytes(data)
        elif isinstance(data, bytes):
            self._buf = data
        elif isinstance(data, str):
            self._buf = os.fsencode(data)
        elif isinstance(data, vararray):
            self._buf = bytes(data.buf)
        else:
            raise TypeError("unexpected data type %s" % type(data).__name__)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        count = cls.read_uint32(stream)
        data = cls.read_then_align(stream, count, 4)
        return cls(data=data)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        stream.write(struct.pack('!I', len(self._buf)))
        stream.write(self._buf)
        extra = len(self._buf) % 4
        if extra:
            stream.write(bytes(4-extra))

class fixedarray(vararray):
    def __init__(self, data=None):
        if data is None:
            data = bytes(self._FIXEDSIZE)
        else:
            assert len(data) == self._FIXEDSIZE
        super().__init__(data=data)

    _FIXEDSIZE = 0

    @classmethod
    def size(cls):
        return cls._FIXEDSIZE

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        return cls(data=cls.read_then_align(stream, cls._FIXEDSIZE, 4))

    def write(self, stream):
        '''
        Write this to a stream
        '''
        assert len(self._buf) == self._FIXEDSIZE
        super().write(stream)

class vararray_limited(vararray):
    '''
    A variable length array with a maximum size
    '''
    _MAXSIZE = 0

    @classmethod
    def check_size(cls, buf):
        if len(buf) > cls._MAXSIZE:
            raise AssertionError("%s: %d exceeds maximum %d" % (cls.__name__, len(buf), cls._MAXSIZE))

    @property
    def buf(self):
        return self._buf

    @buf.setter
    def buf(self, data):
        if data is None:
            self._buf = bytes()
        elif isinstance(data, bytearray):
            self._buf = bytes(data)
        elif isinstance(data, bytes):
            self._buf = data
        elif isinstance(data, str):
            self.check_size(data)
            self._buf = os.fsencode(data)
        elif isinstance(data, vararray):
            self._buf = bytes(data.buf)
        else:
            raise TypeError("unexpected data type %s" % type(data).__name__)
        self.check_size(self._buf)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        count = cls.read_uint32(stream)
        if count > cls._MAXSIZE:
            raise ProtocolError("%s: count=%d exceeds limit=%d" % (cls.__name__, count, cls._MAXSIZE))
        data = cls.read_then_align(stream, count, 4)
        return cls(data=data)

class _vararray_str_base():
    '''
    vararray overloads shared by vararray_str and vararray_str_limited
    '''
    def __str__(self):
        return vararray.to_string(self)

    def __repr__(self):
        return "%s('%s')" % (type(self).__name__, vararray.to_string(self))

class vararray_str(_vararray_str_base, vararray):
    '''
    Variant of vararray that prints itself as a string rather than a big hex value
    '''
    # No further specialization

class vararray_str_limited(_vararray_str_base, vararray_limited):
    '''
    Variant of vararray_limited that prints itself as a string rather than a big hex value
    '''
    # No further specialization

class opaque(vararray):
    '''
    Generic variable-length array of bytes
    '''
    # No specialization here

class filename3(vararray_str):
    pass

class nfspath3(vararray_str):
    pass

# Yeah:
#      struct nfs_fh3 {
#         opaque       data<NFS3_FHSIZE>;
#      };
#
#      typedef opaque fhandle3<FHSIZE3>;
#
# Thanks for that.

@functools.total_ordering
class nfs_fh3(vararray_limited):
    '''
    This may be used with both HPC Cache and non-HPC Cache filehandles, so the default __str__())
    does not decode it as an HPC Cache filehandle. In the future, we may want to add
    a subclass that does so.
    '''
    _MAXSIZE = NFS3_FHSIZE
    _MAX_LOG_LEN = _MAXSIZE

    def __eq__(self, other):
        if isinstance(other, post_op_fh3):
            return other.__eq__(self)
        return super().__eq__(other)

    def __hash__(self):
        '''
        Must define this here because overloading __eq__()
        implicitly sets __hash__ to None.
        '''
        assert self._buf is not None
        return hash(self._buf)

    def __lt__(self, other):
        if isinstance(other, post_op_fh3):
            return other.__lt__(self)
        return super().__lt__(other)

    def get_handle(self):
        return self if self._buf else None


class fhandle3(nfs_fh3):
    '''
    FHSIZE3 and NFS3_FHSIZE are really the same,
    so fhandle3 and nfs_fh3 are really the same.
    These values are echoed to more closely
    mirror RFC 1813.
    '''
    _MAXSIZE = FHSIZE3
    _MAX_LOG_LEN = _MAXSIZE

class dirpath(vararray_str_limited):
    _MAXSIZE = MNTPATHLEN

class name(vararray_str_limited):
    _MAXSIZE = MNTNAMLEN

class cookieverf3(fixedarray):
    _FIXEDSIZE = NFS3_COOKIEVERFSIZE
    _MAX_LOG_LEN = _FIXEDSIZE

class createverf3(fixedarray):
    _FIXEDSIZE = NFS3_CREATEVERFSIZE
    _MAX_LOG_LEN = _FIXEDSIZE

class writeverf3(fixedarray):
    _FIXEDSIZE = NFS3_WRITEVERFSIZE
    _MAX_LOG_LEN = _FIXEDSIZE

class mountstat3(annotated_enum):
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {0 : 'MNT3_OK',
             1 : 'MNT3ERR_PERM',
             2 : 'MNT3ERR_NOENT',
             5 : 'MNT3ERR_IO',
             13 : 'MNT3ERR_ACCES',
             20 : 'MNT3ERR_NOTDIR',
             22 : 'MNT3ERR_INVAL',
             63 : 'MNT3ERR_NAMETOOLONG',
             10004 : 'MNT3ERR_NOTSUPP',
             10006 : 'MNT3ERR_SERVERFAULT',
            }
mountstat3_MNT3_OK = mountstat3(0)
mountstat3_MNT3ERR_PERM = mountstat3(1)
mountstat3_MNT3ERR_NOENT = mountstat3(2)
mountstat3_MNT3ERR_IO = mountstat3(5)
mountstat3_MNT3ERR_ACCES = mountstat3(13)
mountstat3_MNT3ERR_NOTDIR = mountstat3(20)
mountstat3_MNT3ERR_INVAL = mountstat3(22)
mountstat3_MNT3ERR_NAMETOOLONG = mountstat3(63)
mountstat3_MNT3ERR_NOTSUPP = mountstat3(10004)
mountstat3_MNT3ERR_SERVERFAULT = mountstat3(10006)

class nfsstat3(annotated_enum):
    _MISMATCH_CMP_ALLOW = True
    _XLAT = {0 : 'NFS3_OK',
             1 : 'NFS3ERR_PERM',
             2 : 'NFS3ERR_NOENT',
             5 : 'NFS3ERR_IO',
             6 : 'NFS3ERR_NXIO',
             13 : 'NFS3ERR_ACCES',
             17 : 'NFS3ERR_EXIST',
             18 : 'NFS3ERR_XDEV',
             19 : 'NFS3ERR_NODEV',
             20 : 'NFS3ERR_NOTDIR',
             21 : 'NFS3ERR_ISDIR',
             22 : 'NFS3ERR_INVAL',
             27 : 'NFS3ERR_FBIG',
             28 : 'NFS3ERR_NOSPC',
             30 : 'NFS3ERR_ROFS',
             31 : 'NFS3ERR_MLINK',
             63 : 'NFS3ERR_NAMETOOLONG',
             66 : 'NFS3ERR_NOTEMPTY',
             69 : 'NFS3ERR_DQUOT',
             70 : 'NFS3ERR_STALE',
             71 : 'NFS3ERR_REMOTE',
             10001 : 'NFS3ERR_BADHANDLE',
             10002 : 'NFS3ERR_NOT_SYNC',
             10003 : 'NFS3ERR_BAD_COOKIE',
             10004 : 'NFS3ERR_NOTSUPP',
             10005 : 'NFS3ERR_TOOSMALL',
             10006 : 'NFS3ERR_SERVERFAULT',
             10007 : 'NFS3ERR_BADTYPE',
             10008 : 'NFS3ERR_JUKEBOX',
            }
nfsstat3_NFS3_OK = nfsstat3(0)
nfsstat3_NFS3ERR_PERM = nfsstat3(1)
nfsstat3_NFS3ERR_NOENT = nfsstat3(2)
nfsstat3_NFS3ERR_IO = nfsstat3(5)
nfsstat3_NFS3ERR_NXIO = nfsstat3(6)
nfsstat3_NFS3ERR_ACCES = nfsstat3(13)
nfsstat3_NFS3ERR_EXIST = nfsstat3(17)
nfsstat3_NFS3ERR_XDEV = nfsstat3(18)
nfsstat3_NFS3ERR_NODEV = nfsstat3(19)
nfsstat3_NFS3ERR_NOTDIR = nfsstat3(20)
nfsstat3_NFS3ERR_ISDIR = nfsstat3(21)
nfsstat3_NFS3ERR_INVAL = nfsstat3(22)
nfsstat3_NFS3ERR_FBIG = nfsstat3(27)
nfsstat3_NFS3ERR_NOSPC = nfsstat3(28)
nfsstat3_NFS3ERR_ROFS = nfsstat3(30)
nfsstat3_NFS3ERR_MLINK = nfsstat3(31)
nfsstat3_NFS3ERR_NAMETOOLONG = nfsstat3(63)
nfsstat3_NFS3ERR_NOTEMPTY = nfsstat3(66)
nfsstat3_NFS3ERR_DQUOT = nfsstat3(69)
nfsstat3_NFS3ERR_STALE = nfsstat3(70)
nfsstat3_NFS3ERR_REMOTE = nfsstat3(71)
nfsstat3_NFS3ERR_BADHANDLE = nfsstat3(10001)
nfsstat3_NFS3ERR_NOT_SYNC = nfsstat3(10002)
nfsstat3_NFS3ERR_BAD_COOKIE = nfsstat3(10003)
nfsstat3_NFS3ERR_NOTSUPP = nfsstat3(10004)
nfsstat3_NFS3ERR_TOOSMALL = nfsstat3(10005)
nfsstat3_NFS3ERR_SERVERFAULT = nfsstat3(10006)
nfsstat3_NFS3ERR_BADTYPE = nfsstat3(10007)
nfsstat3_NFS3ERR_JUKEBOX = nfsstat3(10008)

class ftype3(annotated_enum):
    _SUCCESSVAL = None
    _XLAT = {0 : 'invalid',
             1 : 'NF3REG',
             2 : 'NF3DIR',
             3 : 'NF3BLK',
             4 : 'NF3CHR',
             5 : 'NF3LNK',
             6 : 'NF3SOCK',
             7 : 'NF3FIFO',
            }
ftype3_invalid = ftype3(0) # 0 is not part of RFC 1813
ftype3_NF3REG = ftype3(1)
ftype3_NF3DIR = ftype3(2)
ftype3_NF3BLK = ftype3(3)
ftype3_NF3CHR = ftype3(4)
ftype3_NF3LNK = ftype3(5)
ftype3_NF3SOCK = ftype3(6)
ftype3_NF3FIFO = ftype3(7)

class stable_how(annotated_enum):
    _SUCCESSVAL = None
    _XLAT = {0 : 'UNSTABLE',
             1 : 'DATA_SYNC',
             2 : 'FILE_SYNC',
            }
stable_how_UNSTABLE = stable_how(0)
stable_how_DATA_SYNC = stable_how(1)
stable_how_FILE_SYNC = stable_how(2)

class createmode3(annotated_enum):
    _SUCCESSVAL = None
    _XLAT = {0 : 'UNCHECKED',
             1 : 'GUARDED',
             2 : 'EXCLUSIVE',
            }
createmode3_UNCHECKED = createmode3(0)
createmode3_GUARDED = createmode3(1)
createmode3_EXCLUSIVE = createmode3(2)

class time_how(annotated_enum):
    _SUCCESSVAL = None
    _XLAT = {0 : 'DONT_CHANGE',
             1 : 'SET_TO_SERVER_TIME',
             2 : 'SET_TO_CLIENT_TIME',
            }
time_how_DONT_CHANGE = time_how(0)
time_how_SET_TO_SERVER_TIME = time_how(1)
time_how_SET_TO_CLIENT_TIME = time_how(2)

class mountbody(ONCRPCwire):
    def __init__(self, ml_hostname=None, ml_directory=None):
        self.ml_hostname = name(data=ml_hostname)
        self.ml_directory = dirpath(data=ml_directory)

    _DESC_ATTRS_2 = ['ml_hostname', 'ml_directory']

    def matches(self, hostname, path):
        return (os.fsencode(hostname) == self.ml_hostname) and (os.fsencode(path) == self.ml_directory)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ml_hostname = name.read(stream)
        ml_directory = dirpath.read(stream)
        return cls(ml_hostname=ml_hostname, ml_directory=ml_directory)

class exportnode(ONCRPCwire):
    def __init__(self, ex_dir=None, ex_groups=None):
        self.ex_dir = dirpath(data=ex_dir)
        self.ex_groups = ex_groups or list()

    _DESC_ATTRS_2 = ['ex_dir', 'ex_groups']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.ex_dir = dirpath.read(stream)
        ret.ex_groups = ret.read_list(stream, name.read)
        return ret

class specdata3(ONCRPCwire):
    def __init__(self, specdata1=0, specdata2=0):
        self.specdata1 = specdata1
        self.specdata2 = specdata2

    _DESC_ATTRS_2 = ['specdata1', 'specdata2']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        specdata1, specdata2 = struct.unpack('!II', cls.read_exact(stream, 8))
        return cls(specdata1=specdata1, specdata2=specdata2)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        stream.write(struct.pack('!II', self.specdata1, self.specdata2))

@functools.total_ordering
class nfstime3(ONCRPCwire):
    def __init__(self, seconds=0, nseconds=0, isdate=True):
        self.seconds = int(seconds)
        self.nseconds = int(nseconds)
        self._isdate = isdate # bool; not part of the wire represendation

    def __repr__(self):
        if self._isdate:
            return self._asdatestr()
        return super().__repr__()

    def __str__(self):
        if self._isdate:
            return self._asdatestr()
        return super().__str__()

    def _asdatestr(self):
        '''
        Helper to generate a string of the form (seconds=nnnn,nseconds=nnnn,date.nseconds)
        '''
        dtstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.seconds))
        ht = "%s.%09d" % (dtstr, self.nseconds)
        return "(seconds=%s,nseconds=%s,%s)" % (self.seconds, self.nseconds, ht)

    @property
    def isdate(self):
        '''
        Property accessor so the underlying attribute
        may be named '_isdate' to make it more obvious
        that it is not part of the wire protocol.
        '''
        return self._isdate

    @isdate.setter
    def isdate(self, value):
        '''
        Property accessor so the underlying attribute
        may be named '_isdate' to make it more obvious
        that it is not part of the wire protocol.
        '''
        self._isdate = bool(value)

    _DESC_ATTRS_2 = ['seconds', 'nseconds', 'isdate']

    def _cmp(self, other):
        if isinstance(other, (int, float, nfstime3)):
            a = float(self)
            b = float(other)
            if a < b:
                return -1
            if a > b:
                return 1
            return 0
        raise TypeError("%s compare vs %s is not supported" % (type(self).__name__, type(other).__name__))

    def __hash__(self):
        return hash(self.seconds+self.nseconds)

    def __eq__(self, other):
        try:
            return self._cmp(other) == 0
        except TypeError:
            return False

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        return self._cmp(other) < 0

    def __le__(self, other):
        return self._cmp(other) <= 0

    def __gt__(self, other):
        return self._cmp(other) > 0

    def __ge__(self, other):
        return self._cmp(other) >= 0

    def __int__(self):
        return int(self.seconds)

    def __float__(self):
        return float(self.seconds) + (float(self.nseconds) / NSEC_PER_SEC)

    def time(self):
        return float(self.seconds) + (float(self.nseconds) / NSEC_PER_SEC)

    def time_before(self, t):
        st = self.time()
        if t > st:
            return t - st
        return 0.0

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        seconds, nseconds = struct.unpack('!II', cls.read_exact(stream, 8))
        return cls(seconds=seconds, nseconds=nseconds)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        stream.write(struct.pack('!II', self.seconds, self.nseconds))

class fattr3(ONCRPCwire):
    def __init__(self):
        self.ftype = ftype3_invalid # ftype3
        self.mode = 0 # mode3
        self.nlink = 0 # uint32
        self.uid = 0 # uid3 (uint32)
        self.gid = 0 # gid3 (uint32)
        self.size = 0 # size3 (uint64)
        self.used = 0 # size3 (uint64)
        self.rdev = specdata3()
        self.fsid = 0 # uint64
        self.fileid = 0 # fileid3 (uint64)
        self.atime = nfstime3()
        self.mtime = nfstime3()
        self.ctime = nfstime3()

    _DESC_ATTRS_2 = ['ftype',
                     'mode',
                     'nlink',
                     'uid',
                     'gid',
                     'size',
                     'used',
                     'rdev',
                     'fsid',
                     'fileid',
                     'atime',
                     'mtime',
                     'ctime',
                    ]

    _DESC_ATTRS_FMT = {'ftype' : '%s',
                       'mode' : '0o%o',
                      }

    _RFMT = '!iIIIIQQIIQQIIIIII'
    _RSZ = struct.calcsize(_RFMT)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        # This is done often enough to make a custom unpacker worth it
        ret = cls()
        (ftype_val,
         ret.mode,
         ret.nlink,
         ret.uid,
         ret.gid,
         ret.size,
         ret.used,
         ret.rdev.specdata1,
         ret.rdev.specdata2,
         ret.fsid,
         ret.fileid,
         ret.atime.seconds,
         ret.atime.nseconds,
         ret.mtime.seconds,
         ret.mtime.nseconds,
         ret.ctime.seconds,
         ret.ctime.nseconds) = struct.unpack(cls._RFMT, cls.read_exact(stream, cls._RSZ))
        ret.ftype = ftype3(ftype_val)
        return ret

@functools.total_ordering
class post_op_fh3(ONCRPCwire):
    def __init__(self, handle_follows=False):
        self.handle_follows = handle_follows
        self.handle = None # nfs_fh3

    def _desc_attrs(self):
        if self.handle_follows:
            return ['handle_follows', 'handle'] + super()._desc_attrs()
        return ['handle_follows'] + super()._desc_attrs()

    def __hash__(self):
        return hash(self.handle)

    def __eq__(self, other):
        if isinstance(other, post_op_fh3):
            if (not self.handle_follows) and (not other.handle_follows):
                return True
            if (not self.handle_follows) or (not other.handle_follows):
                return False
            return self.handle == other.handle
        if isinstance(other, nfs_fh3):
            if not self.handle_follows:
                return False
            return self.handle == other
        raise TypeError("%s cannot compare with type %s" % (type(self).__name__, type(other).__name__))

    def __lt__(self, other):
        if isinstance(other, post_op_fh3):
            if (not self.handle_follows) and (not other.handle_follows):
                return False
            if not self.handle_follows:
                return True
            if not other.handle_follows:
                return False
            return self.handle < other.handle
        if isinstance(other, nfs_fh3):
            if not self.handle_follows:
                return False
            return self.handle < other
        raise TypeError("%s cannot compare with type %s" % (type(self).__name__, type(other).__name__))

    def get_handle(self):
        return self.handle.get_handle() if self.handle_follows else None

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls(handle_follows=cls.read_bool(stream))
        if ret.handle_follows:
            ret.handle = nfs_fh3.read(stream)
        return ret

class _entry3base(ONCRPCwire):
    '''
    base class for entry3 and entryplus3
    '''
    def __init__(self):
        self.fileid = 0 # fileid3 (uint64)
        self.name = None # filename3, string
        self.cookie = 0 # cookie3

    @classmethod
    def find_name_in_list(cls, entries, name_to_find):
        for entry in entries:
            if not isinstance(entry, cls):
                raise TypeError("%s entry type %s is not %s" % (cls.__name__, type(entry).__name__, cls.__name__))
            if entry.name == name_to_find:
                return entry
        return None

    def get_name(self):
        return self.name

class entry3(_entry3base):
    _DESC_ATTRS_2 = ['fileid', 'name', 'cookie']

    @staticmethod
    def get_handle():
        '''
        No handle in entry3. Provide get_handle() to be isomorphic wrt entryplus3
        '''
        return None

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.fileid, count = struct.unpack('!QI', cls.read_exact(stream, 12))
        ret.name = filename3(data=cls.read_then_align(stream, count, 4))
        ret.cookie = cls.read_uint64(stream)
        return ret

class entryplus3(_entry3base):
    def __init__(self):
        super().__init__()
        self.name_attributes = None # post_op_attr
        self.name_handle = None # post_op_fh3

    _DESC_ATTRS_2 = ['fileid', 'name', 'cookie', 'name_attributes', 'name_handle']

    def get_handle(self):
        return self.name_handle.get_handle()

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.fileid, count = struct.unpack('!QI', cls.read_exact(stream, 12))
        ret.name = filename3(data=cls.read_then_align(stream, count, 4))
        ret.cookie, attributes_follow = struct.unpack('!Qi', cls.read_exact(stream, 12))
        ret.name_attributes = post_op_attr(attributes_follow=attributes_follow)
        if attributes_follow:
            ret.name_attributes.attributes = fattr3.read(stream)
        ret.name_handle = post_op_fh3.read(stream)
        return ret

class _dirlistbase(ONCRPCwire):
    '''
    base class for dirlist3 and dirlistplus3
    '''
    def __init__(self):
        self.entries = list()
        self.eof = True

    _ENTRY_CLASS = None

    def __len__(self):
        return len(self.entries)

    def __repr__(self):
        return type(self).__name__ + '(' + ','.join(['{' + ','.join([entry.desc_str()]) + '}' for entry in self.entries]) + ')'

    def __str__(self):
        '''
        Define explicitly to avoid superclass definitions
        '''
        return self.__repr__()

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.entries = cls.read_list(stream, cls._ENTRY_CLASS.read)
        ret.eof = cls.read_bool(stream)
        return ret

class dirlist3(_dirlistbase):
    '''
    list of entry3
    '''
    _ENTRY_CLASS = entry3

class dirlistplus3(_dirlistbase):
    '''
    list of entryplus3
    '''
    _ENTRY_CLASS = entryplus3

class wcc_attr(ONCRPCwire):
    def __init__(self, size=0):
        self.size = size # size3 (uint64)
        self.mtime = nfstime3()
        self.ctime = nfstime3()

    _DESC_ATTRS_2 = ['size', 'mtime', 'ctime']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        (ret.size,
         ret.mtime.seconds,
         ret.mtime.nseconds,
         ret.ctime.seconds,
         ret.ctime.nseconds) = struct.unpack('!QIIII', cls.read_exact(stream, 24))
        return ret

class _op_attr_base(ONCRPCwire):
    '''
    base class for pre_op_attr and post_op_attr
    '''
    def __init__(self, attributes_follow=False):
        self.attributes_follow = bool(attributes_follow)
        self.attributes = None # wcc_attr

    _ATTRIBUTE_CLASS = None

    def _desc_attrs(self):
        if self.attributes_follow:
            return ['attributes_follow', 'attributes'] + super()._desc_attrs()
        return ['attributes_follow'] + super()._desc_attrs()

    def get_attributes(self):
        if self.attributes_follow:
            return self.attributes
        return None

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        attributes_follow = cls.read_bool(stream)
        ret = cls(attributes_follow=attributes_follow)
        if attributes_follow:
            ret.attributes = cls._ATTRIBUTE_CLASS.read(stream)
        return ret

class pre_op_attr(_op_attr_base):
    '''
    attributes_follow bool
    attributes wcc_attr
    '''
    _ATTRIBUTE_CLASS = wcc_attr

class post_op_attr(_op_attr_base):
    '''
    attributes_follow bool
    attributes fattr3
    '''
    _ATTRIBUTE_CLASS = fattr3

class diropargs3(ONCRPCwire):
    def __init__(self, adir=None, aname=None):
        self.adir = nfs_fh3(data=adir) if adir is not None else nfs_fh3()
        self.name = filename3(data=aname) # filename3

    _DESC_ATTRS_2 = ['adir', 'name']

    def get_name(self):
        return self.name

    def get_handle(self):
        return self.adir.get_handle()

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        adir = nfs_fh3.read(stream)
        aname = filename3.read(stream)
        return cls(adir=adir, aname=aname)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.adir.write(stream)
        self.name.write(stream)

class wcc_data(ONCRPCwire):
    '''
    before pre_op_attr
    after post_op_attr
    '''
    def __init__(self):
        self.before = None # pre_op_attr
        self.after = None # post_op_attr

    _DESC_ATTRS_2 = ['before', 'after']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.before = pre_op_attr.read(stream)
        ret.after = post_op_attr.read(stream)
        return ret

class set_mode3(ONCRPCwire):
    def __init__(self, mode=None):
        self.set_it = (mode is not None)
        self.mode = mode

    _DESC_ATTRS_FMT = {'mode' : '0o%o',
                      }

    def _desc_attrs(self):
        if self.set_it:
            return ['set_it', 'mode'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it:
            stream.write(struct.pack('!iI', 1, self.mode))
        else:
            stream.write(bytes(4))

class set_uid3(ONCRPCwire):
    def __init__(self, uid=None):
        self.set_it = (uid is not None)
        self.uid = uid

    def _desc_attrs(self):
        if self.set_it:
            return ['set_it', 'uid'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it:
            stream.write(struct.pack('!iI', 1, self.uid))
        else:
            stream.write(bytes(4))

class set_gid3(ONCRPCwire):
    def __init__(self, gid=None):
        self.set_it = (gid is not None)
        self.gid = gid

    def _desc_attrs(self):
        if self.set_it:
            return ['set_it', 'gid'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it:
            stream.write(struct.pack('!iI', 1, self.gid))
        else:
            stream.write(bytes(4))

class set_size3(ONCRPCwire):
    def __init__(self, size=None):
        self.set_it = (size is not None)
        self.size = size # size3

    def _desc_attrs(self):
        if self.set_it:
            return ['set_it', 'size'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it:
            stream.write(struct.pack('!iQ', 1, self.size))
        else:
            stream.write(bytes(4))

class set_atime(ONCRPCwire):
    def __init__(self, set_it=time_how_DONT_CHANGE, atime=None):
        self.set_it = set_it # time_how
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            if atime is None:
                raise ValueError("set_it_how=%s(SET_TO_CLIENT_TIME) but atime is None" % self.set_it)
            self.atime = atime
        else:
            self.atime = None

    def _desc_attrs(self):
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            return ['set_it', 'atime'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            stream.write(struct.pack('!iII', self.set_it, self.atime.seconds, self.atime.nseconds))
        else:
            self.set_it.write(stream)

class set_mtime(ONCRPCwire):
    def __init__(self, set_it=time_how_DONT_CHANGE, mtime=None):
        self.set_it = set_it # time_how
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            if mtime is None:
                raise ValueError("set_it_how=%s(SET_TO_CLIENT_TIME) but mtime is None" % self.set_it)
            self.mtime = mtime
        else:
            self.mtime = None

    def _desc_attrs(self):
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            return ['set_it', 'mtime'] + super()._desc_attrs()
        return ['set_it'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.set_it == time_how_SET_TO_CLIENT_TIME:
            stream.write(struct.pack('!iII', self.set_it, self.mtime.seconds, self.mtime.nseconds))
        else:
            self.set_it.write(stream)

class sattr3(ONCRPCwire):
    def __init__(self, mode=None, uid=None, gid=None, size=None):
        self.mode = set_mode3(mode=mode)
        self.uid = set_uid3(uid=uid)
        self.gid = set_gid3(gid=gid)
        self.size = set_size3(size=size)
        self.atime = set_atime()
        self.mtime = set_mtime()

    _DESC_ATTRS_2 = ['mode', 'uid', 'gid', 'size', 'atime', 'mtime']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.mode.write(stream)
        self.uid.write(stream)
        self.gid.write(stream)
        self.size.write(stream)
        self.atime.write(stream)
        self.mtime.write(stream)

class sattrguard3(ONCRPCwire):
    def __init__(self, obj_ctime=None):
        if obj_ctime is not None:
            if not isinstance(obj_ctime, nfstime3):
                raise TypeError("%s expected nfstime3 for obj_ctime but got %s" % (type(self).__name__, type(obj_ctime).__name__))
            self.check = True
            self.obj_ctime = obj_ctime
        else:
            self.check = False
            self.obj_ctime = None

    def _desc_attrs(self):
        if self.check:
            return ['check', 'obj_ctime'] + super()._desc_attrs()
        return ['check'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        if self.check:
            stream.write(struct.pack('!iII', 1, self.obj_ctime.seconds, self.obj_ctime.nseconds))
        else:
            stream.write(bytes(4))

class createhow3(ONCRPCwire):
    def __init__(self, mode=createmode3_UNCHECKED, obj_attributes=None, verf=None):
        self._mode = createmode3(int(mode))
        if (self.mode == createmode3_UNCHECKED) or (self.mode == createmode3_GUARDED):
            self.obj_attributes = obj_attributes if obj_attributes is not None else sattr3()
            self.verf = None
        elif self.mode == createmode3_EXCLUSIVE:
            self.obj_attributes = None
            self.verf = createverf3(data=verf) if verf is not None else createverf3()
        else:
            raise ValueError("createhow3 bad mode=%s" % mode)

    _DESC_ATTRS_FMT = {'mode' : '%s',
                      }

    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, mode):
        self._mode = createmode3(int(mode))

    def _desc_attrs(self):
        if self.mode in [createmode3_UNCHECKED, createmode3_GUARDED]:
            return ['mode', 'obj_attributes'] + super()._desc_attrs()
        if self.mode == createmode3_EXCLUSIVE:
            return ['mode', 'verf'] + super()._desc_attrs()
        return ['mode'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.mode.write(stream)
        if self.mode in [createmode3_UNCHECKED, createmode3_GUARDED]:
            self.obj_attributes.write(stream)
        elif self.mode == createmode3_EXCLUSIVE:
            self.verf.write(stream)
        else:
            raise ValueError("invalid mode=%s" % repr(self.mode))

class symlinkdata3(ONCRPCwire):
    def __init__(self, symlink_attributes=None, symlink_data=None):
        if isinstance(symlink_attributes, sattr3):
            self.symlink_attributes = symlink_attributes
        elif symlink_attributes is None:
            self.symlink_attributes = sattr3()
        else:
            raise TypeError("%s expects sattr3 for symlink_attributes, got %s" % (type(self).__name__, type(symlink_attributes).__name__))

        self.symlink_data = nfspath3(symlink_data)

    _DESC_ATTRS_2 = ['symlink_attributes', 'symlink_data']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.symlink_attributes.write(stream)
        self.symlink_data.write(stream)

class devicedata3(ONCRPCwire):
    def __init__(self, dev_attributes=None, spec=None):
        if isinstance(dev_attributes, sattr3):
            self.dev_attributes = dev_attributes
        elif dev_attributes is None:
            self.dev_attributes = sattr3()
        else:
            raise TypeError("%s expects sattr3 for dev_attributes, got %s" % (type(self).__name__, type(dev_attributes).__name__))

        if isinstance(spec, specdata3):
            self.spec = spec
        elif spec is None:
            self.spec = specdata3()
        else:
            raise TypeError("%s expects specdata3 for spec, got %s" % (type(self).__name__, type(spec).__name__))

    _DESC_ATTRS_2 = ['dev_attributes', 'spec']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.dev_attributes.write(stream)
        self.spec.write(stream)

class mknoddata3(ONCRPCwire):
    # data becomes device for CHR/BLK and pipe_attributes for SOCK/FIFO
    def __init__(self, ftype=None, data=None):
        if isinstance(ftype, ftype3):
            self.ftype = ftype
        else:
            raise TypeError("%s expects ftype3 for ftype, got %s" % (type(self).__name__, type(ftype).__name__))

        self.device = None
        self.pipe_attributes = None

        if (self.ftype == ftype3_NF3CHR) or (self.ftype == ftype3_NF3BLK):
            if isinstance(data, devicedata3):
                self.device = data
            else:
                raise TypeError("%s.ftype=%s requires devicedata3, got %s" % (type(self).__name__, self.ftype, type(data).__name__))
        elif (self.ftype == ftype3_NF3SOCK) or (self.ftype == ftype3_NF3FIFO):
            if isinstance(data, sattr3):
                self.pipe_attributes = data
            else:
                raise TypeError("%s.ftype=%s requires sattr3, got %s" % (type(self).__name__, self.ftype, type(data).__name__))
        else:
            if data is not None:
                raise TypeError("%s.ftype=%s expects None for data, got %s" % (type(self).__name__, self.ftype, type(data).__name__))

    def _desc_attrs(self):
        if self.ftype in (ftype3_NF3CHR, ftype3_NF3BLK):
            return ['ftype', 'device'] + super()._desc_attrs()
        if self.ftype in (ftype3_NF3SOCK, ftype3_NF3FIFO):
            return ['ftype', 'pipe_attributes'] + super()._desc_attrs()
        return ['ftype'] + super()._desc_attrs()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.ftype.write(stream)
        if self.ftype in (ftype3_NF3CHR, ftype3_NF3BLK):
            self.device.write(stream)
        elif self.ftype in (ftype3_NF3SOCK, ftype3_NF3FIFO):
            self.pipe_attributes.write(stream)

class NFS3_NULLres(NULLres):
    '''
    The NULL RPC has no status of its own. To simplify caller
    code, we say here that a successful call has status nfsstat3_NFS3_OK.
    '''
    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        return cls(status=nfsstat3_NFS3_OK)

class GETATTR3args(ONCRPCargs):
    def __init__(self, aobject=None, xid=None):
        super().__init__(xid=xid)
        self.aobject = nfs_fh3(data=aobject) if aobject is not None else nfs_fh3()

    _DESC_ATTRS_2 = ['aobject']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.aobject.write(stream)

class GETATTR3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_attributes = None # fattr3

    _DESC_ATTRS_2_OK = ['obj_attributes']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj_attributes = fattr3.read(stream)
        return ret

class SETATTR3args(ONCRPCargs):
    def __init__(self, aobject=None, new_attributes=None, guard=None, xid=None):
        super().__init__(xid=xid)
        self.aobject = aobject if aobject is not None else nfs_fh3()

        if isinstance(new_attributes, sattr3):
            self.new_attributes = new_attributes
        elif new_attributes is None:
            self.new_attributes = sattr3()
        else:
            raise TypeError("%s expected sattr3 for new_attributes, not %s" % (type(self).__name__, type(new_attributes).__name__))

        if isinstance(guard, sattr3):
            self.guard = guard
        elif guard is None:
            self.guard = sattrguard3()
        else:
            raise TypeError("%s expected sattr3 for new_attributes, not %s" % (type(self).__name__, type(new_attributes).__name__))

    _DESC_ATTRS_2 = ['aobject', 'new_attributes', 'guard']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.aobject.write(stream)
        self.new_attributes.write(stream)
        self.guard.write(stream)

class SETATTR3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_wcc = None # wcc_data

    _DESC_ATTRS_2 = ['obj_wcc']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        ret.obj_wcc = wcc_data.read(stream)
        return ret

class LOOKUP3args(ONCRPCargs):
    def __init__(self, adir, aname, xid=None):
        super().__init__(xid=xid)
        self.what = diropargs3(adir=adir, aname=aname)

    _DESC_ATTRS_2 = ['what']

    def get_name(self):
        return self.what.get_name()

    def get_handle(self):
        return self.what.get_handle()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.what.write(stream)

class LOOKUP3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.aobject = None # nfs_fh3
        self.obj_attributes = None # post_op_attr
        self.dir_attributes = None # post_op_attr

    _DESC_ATTRS_2_OK = ['aobject', 'obj_attributes', 'dir_attributes']
    _DESC_ATTRS_2_FAIL = ['dir_attributes']

    def get_handle(self):
        return self.aobject.get_handle()

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.aobject = nfs_fh3.read(stream)
            ret.obj_attributes = post_op_attr.read(stream)
            ret.dir_attributes = post_op_attr.read(stream)
        else:
            ret.dir_attributes = post_op_attr.read(stream)
        return ret

class ACCESS3args(ONCRPCargs):
    def __init__(self, aobject=None, access=0, xid=None):
        super().__init__(xid=xid)
        self.aobject = nfs_fh3(data=aobject) if aobject is not None else nfs_fh3()
        self.access = access # uint32

    _DESC_ATTRS_2 = ['aobject', 'access']

    _DESC_ATTRS_FMT = {'access' : '0x%x',
                      }

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.aobject.write(stream)
        self.write_uint32(stream, self.access)

class ACCESS3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_attributes = None # post_op_attr
        self.access = None # uint32

    _DESC_ATTRS_2_OK = ['obj_attributes', 'access']
    _DESC_ATTRS_2_FAIL = ['obj_attributes']

    _DESC_ATTRS_FMT = {'access' : '0x%x',
                      }

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj_attributes = post_op_attr.read(stream)
            ret.access = ret.read_uint32(stream)
        else:
            ret.obj_attributes = post_op_attr.read(stream)
        return ret

class READLINK3args(ONCRPCargs):
    def __init__(self, symlink=None, xid=None):
        super().__init__(xid=xid)
        self.symlink = symlink if symlink is not None else nfs_fh3()

    _DESC_ATTRS_2 = ['symlink']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.symlink.write(stream)

class READLINK3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.symlink_attributes = None # post_op_attr
        self.data = None # nfspath3

    _DESC_ATTRS_2_OK = ['symlink_attributes', 'data']
    _DESC_ATTRS_2_FAIL = ['symlink_attributes']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.symlink_attributes = post_op_attr.read(stream)
            ret.data = nfspath3.read(stream)
        else:
            ret.symlink_attributes = post_op_attr.read(stream)
        return ret

class READ3args(ONCRPCargs):
    def __init__(self, afile=None, offset=0, count=0, xid=None):
        super().__init__(xid=xid)
        self.afile = afile if afile is not None else nfs_fh3()
        self.offset = offset
        self.count = count

    _DESC_ATTRS_2 = ['afile', 'offset', 'count']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.afile.write(stream)
        stream.write(struct.pack('!QI', self.offset, self.count))

class READ3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.file_attributes = None # post_op_attr
        self.count = None # count3
        self.eof = None # bool
        self.data = None # opaque

    _DESC_ATTRS_2_OK = ['file_attributes', 'count', 'eof']
    _DESC_ATTRS_2_FAIL = ['file_attributes']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.file_attributes = post_op_attr.read(stream)
            ret.count, eof_val, oc = struct.unpack('!IiI', cls.read_exact(stream, 12))
            ret.eof = bool(eof_val)
            data_val = cls.read_exact(stream, oc)
            ret.data = opaque(data_val)
        else:
            ret.file_attributes = post_op_attr.read(stream)
        return ret

class WRITE3args(ONCRPCargs):
    def __init__(self, afile=None, offset=0, count=0, stable=stable_how_FILE_SYNC, data=None, xid=None):
        super().__init__(xid=xid)
        self.afile = afile if afile is not None else nfs_fh3()
        self.offset = offset # offset3 (uint64)
        self.count = count # count3 (uint32)
        if isinstance(stable, stable_how):
            self.stable = stable
        else:
            self.stable = stable_how(stable)
        self._data = opaque(data=data)

    _DESC_ATTRS_2 = ['afile', 'offset', 'count', 'stable', 'data']

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, data):
        self._data = opaque(data=data)

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.afile.write(stream)
        stream.write(struct.pack('!QIiI', self.offset, self.count, self.stable, len(self._data)))
        self.write_then_align(stream, self._data.buf, 4)

class WRITE3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.file_wcc = None # wcc_data
        self.count = None # count3
        self.committed = None # stable_how
        self.verf = None # writeverf3

    _DESC_ATTRS_2_OK = ['file_wcc', 'count', 'committed', 'verf']
    _DESC_ATTRS_2_FAIL = ['file_wcc']

    _RFMT = "!Ii%ds" % NFS3_WRITEVERFSIZE
    _RSZ = struct.calcsize(_RFMT)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.file_wcc = wcc_data.read(stream)
            ret.count, committed_val, verf_val = struct.unpack(cls._RFMT, cls.read_exact(stream, cls._RSZ))
            ret.committed = stable_how(committed_val)
            ret.verf = writeverf3(data=verf_val)
        else:
            ret.file_wcc = wcc_data.read(stream)
        return ret

class CREATE3args(ONCRPCargs):
    def __init__(self, dirfh, aname, how=None, xid=None):
        super().__init__(xid=xid)
        self.where = diropargs3(adir=dirfh, aname=aname)
        if how is None:
            self.how = createhow3()
        elif isinstance(how, createhow3):
            self.how = how
        else:
            raise TypeError("%s expected createhow3 for how, not %s" % (type(self).__name__, type(how).__name__))

    _DESC_ATTRS_2 = ['where', 'how']

    def get_name(self):
        return self.where.get_name()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.where.write(stream)
        self.how.write(stream)

class _create3res(NFS3RPCres):
    '''
    Many create-like operations are isomorphic.
    This provides a common base class to avoid cut-and-paste.
    '''
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj = None # post_op_fh3
        self.obj_attributes = None # post_op_attr
        self.dir_wcc = None # wcc_data

    _DESC_ATTRS_2_OK = ['obj', 'obj_attributes', 'dir_wcc']
    _DESC_ATTRS_2_FAIL = ['dir_wcc']

    def get_handle(self):
        return self.obj.get_handle()

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj = post_op_fh3.read(stream)
            ret.obj_attributes = post_op_attr.read(stream)
            ret.dir_wcc = wcc_data.read(stream)
        else:
            ret.dir_wcc = wcc_data.read(stream)
        return ret

class CREATE3res(_create3res):
    pass

class MKDIR3args(ONCRPCargs):
    def __init__(self, adir=None, aname=None, attributes=None, xid=None):
        super().__init__(xid=xid)
        self.where = diropargs3(adir=adir, aname=aname)
        if attributes is None:
            self.attributes = sattr3()
            self.attributes.mtime = set_mtime(set_it=time_how_SET_TO_SERVER_TIME)
        elif isinstance(attributes, sattr3):
            self.attributes = attributes
        else:
            raise TypeError("%s expected sattr3 for attributes, not %s" % (type(self).__name__, type(attributes).__name__))

    _DESC_ATTRS_2 = ['where', 'attributes']

    def get_name(self):
        return self.where.get_name()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.where.write(stream)
        self.attributes.write(stream)

class MKDIR3res(_create3res):
    pass

class SYMLINK3args(ONCRPCargs):
    # For convenience, target can be symlinkdata3 or just a string.
    # If it is just a string, default sattr3 is used for attributes.
    def __init__(self, dirfh=None, aname=None, target=None, xid=None):
        super().__init__(xid=xid)
        self.where = diropargs3(adir=dirfh, aname=aname)
        if target is None:
            self.symlink = symlinkdata3()
        elif isinstance(target, symlinkdata3):
            self.symlink = target
        else:
            self.symlink = symlinkdata3(symlink_data=nfspath3(data=target))

    _DESC_ATTRS_2 = ['where', 'symlink']

    def get_name(self):
        return self.where.get_name()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.where.write(stream)
        self.symlink.write(stream)

class SYMLINK3res(_create3res):
    pass

class MKNOD3args(ONCRPCargs):
    def __init__(self, dirfh=None, aname=None, what=None, xid=None):
        super().__init__(xid=xid)
        self.where = diropargs3(adir=dirfh, aname=aname)
        if isinstance(what, mknoddata3):
            self.what = what
        else:
            raise TypeError("%s.what unsupported type %s" % (type(self).__name__, type(what).__name__))

    _DESC_ATTRS_2 = ['where', 'what']

    def get_name(self):
        return self.where.get_name()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.where.write(stream)
        self.what.write(stream)

class MKNOD3res(_create3res):
    pass

class _remove3args(ONCRPCargs):
    '''
    Code shared between isomorphic remove-like operations
    '''
    def __init__(self, dirfh, aname, xid=None):
        super().__init__(xid=xid)
        self.aobject = diropargs3(adir=dirfh, aname=aname)

    _DESC_ATTRS_2 = ['aobject']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.aobject.write(stream)

class _remove3res(NFS3RPCres):
    '''
    Code shared between isomorphic remove-like operations
    '''
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.dir_wcc = None # wcc_data

    _DESC_ATTRS_2 = ['dir_wcc']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        ret.dir_wcc = wcc_data.read(stream)
        return ret

class REMOVE3args(_remove3args):
    pass

class REMOVE3res(_remove3res):
    pass

class RMDIR3args(_remove3args):
    pass

class RMDIR3res(_remove3res):
    pass

class RENAME3args(ONCRPCargs):
    def __init__(self, from_fh=None, from_name=None, to_fh=None, to_name=None, xid=None):
        super().__init__(xid=xid)
        self.afrom = diropargs3(adir=from_fh, aname=from_name)
        self.ato = diropargs3(adir=to_fh, aname=to_name)

    _DESC_ATTRS_2 = ['afrom', 'ato']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.afrom.write(stream)
        self.ato.write(stream)

class RENAME3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.fromdir_wcc = None # wcc_data
        self.todir_wcc = None # wcc_data

    _DESC_ATTRS_2 = ['fromdir_wcc', 'todir_wcc']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        ret.fromdir_wcc = wcc_data.read(stream)
        ret.todir_wcc = wcc_data.read(stream)
        return ret

class LINK3args(ONCRPCargs):
    def __init__(self, afile=None, dirfh=None, aname=None, xid=None):
        super().__init__(xid=xid)
        self.afile = afile if afile is not None else nfs_fh3()
        self.link = diropargs3(adir=dirfh, aname=aname)

    _DESC_ATTRS_2 = ['afile', 'link']

    def get_name(self):
        return self.link.get_name()

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.afile.write(stream)
        self.link.write(stream)

class LINK3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.file_attributes = None # post_op_attr
        self.linkdir_wcc = None # wcc_data

    _DESC_ATTRS_2 = ['file_attributes', 'linkdir_wcc']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        ret.file_attributes = post_op_attr.read(stream)
        ret.linkdir_wcc = wcc_data.read(stream)
        return ret

class READDIR3args(ONCRPCargs):
    def __init__(self, adir=None, cookie=0, cookieverf=None, count=16*1024, xid=None):
        super().__init__(xid=xid)
        self.adir = nfs_fh3(data=adir) if adir is not None else nfs_fh3()
        self.cookie = cookie # cookie3 (uint64)
        if cookieverf is not None:
            self.cookieverf = cookieverf
        else:
            self.cookieverf = cookieverf3(None)
        self.count = count # count3

    _DESC_ATTRS_2 = ['adir', 'cookie', 'cookieverf', 'count']

    _WFMT = "!Q%dsI" % NFS3_COOKIEVERFSIZE

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.adir.write(stream)
        stream.write(struct.pack(self._WFMT, self.cookie, self.cookieverf.buf, self.count))

class _readdir3res(NFS3RPCres):
    '''
    base class for READDIR3res and READDIRPLUS3res
    '''
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.dir_attributes = None # post_op_attr
        self.cookieverf = None # cookieverf3
        self.reply = None # _ENTRY_CLASS (dirlist3/dirlistplus3)

    _ENTRY_CLASS = None
    _DESC_ATTRS_2_OK = ['dir_attributes', 'cookieverf', 'reply']
    _DESC_ATTRS_2_FAIL = ['dir_attributes']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.dir_attributes = post_op_attr.read(stream)
            ret.cookieverf = cookieverf3.read(stream)
            ret.reply = cls._ENTRY_CLASS.read(stream)
        else:
            ret.dir_attributes = post_op_attr.read(stream)
        return ret

class READDIR3res(_readdir3res):
    _ENTRY_CLASS = dirlist3

class READDIRPLUS3args(ONCRPCargs):
    def __init__(self, adir=None, cookie=0, cookieverf=None, dircount=16*1024, maxcount=16*1024, xid=None):
        super().__init__(xid=xid)
        self.adir = nfs_fh3(data=adir) if adir is not None else nfs_fh3()
        self.cookie = cookie # cookie3 (uint64)
        self.cookieverf = cookieverf3(data=cookieverf)
        self.dircount = dircount # count3
        self.maxcount = maxcount # count3

    _DESC_ATTRS_2 = ['adir', 'cookie', 'cookieverf', 'dircount', 'maxcount']

    _WFMT = "!Q%dsII" % NFS3_COOKIEVERFSIZE

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.adir.write(stream)
        stream.write(struct.pack(self._WFMT, self.cookie, self.cookieverf.buf, self.dircount, self.maxcount))

class READDIRPLUS3res(_readdir3res):
    _ENTRY_CLASS = dirlistplus3

class FSSTAT3args(ONCRPCargs):
    def __init__(self, fsroot, xid=None):
        super().__init__(xid=xid)
        self.fsroot = nfs_fh3(data=fsroot) if fsroot is not None else nfs_fh3()

    _DESC_ATTRS_2 = ['fsroot']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.fsroot.write(stream)

class FSSTAT3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_attributes = None # post_op_attr
        self.tbytes = None # size3 (uint64)
        self.fbytes = None # size3 (uint64)
        self.abytes = None # size3 (uint64)
        self.tfiles = None # size3 (uint64)
        self.ffiles = None # size3 (uint64)
        self.afiles = None # size3 (uint64)
        self.invarsec = None # uint32

    _DESC_ATTRS_2_OK = ['obj_attributes', 'tbytes', 'fbytes', 'abytes', 'tfiles', 'ffiles', 'afiles', 'invarsec']
    _DESC_ATTRS_2_FAIL = ['obj_attributes']

    _RFMT = '!QQQQQQI'
    _RSZ = struct.calcsize(_RFMT)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj_attributes = post_op_attr.read(stream)
            (ret.tbytes,
             ret.fbytes,
             ret.abytes,
             ret.tfiles,
             ret.ffiles,
             ret.afiles,
             ret.invarsec) = struct.unpack(cls._RFMT, cls.read_exact(stream, cls._RSZ))
        else:
            ret.obj_attributes = post_op_attr.read(stream)
        return ret

class FSINFO3args(ONCRPCargs):
    def __init__(self, fsroot, xid=None):
        super().__init__(xid=xid)
        self.fsroot = nfs_fh3(data=fsroot) if fsroot is not None else nfs_fh3()

    _DESC_ATTRS_2 = ['fsroot']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.fsroot.write(stream)

class FSINFO3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_attributes = None # post_op_attr
        self.rtmax = None # uint32
        self.rtpref = None # uint32
        self.rtmult = None # uint32
        self.wtmax = None # uint32
        self.wtpref = None # uint32
        self.wtmult = None # uint32
        self.dtpref = None # uint32
        self.maxfilesize = None # size3 (uint64)
        self.time_delta = nfstime3()
        self.properties = None # uint32

    _DESC_ATTRS_2_OK = ['obj_attributes', 'rtmax', 'rtpref', 'rtmult', 'wtmax', 'wtpref', 'wtmult', 'dtpref', 'maxfilesize', 'time_delta', 'properties']
    _DESC_ATTRS_2_FAIL = ['obj_attributes']

    _DESC_ATTRS_FMT = {'properties' : '0x%08x',
                      }

    _RFMT = '!IIIIIIIQIII'
    _RSZ = struct.calcsize(_RFMT)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj_attributes = post_op_attr.read(stream)
            (ret.rtmax,
             ret.rtpref,
             ret.rtmult,
             ret.wtmax,
             ret.wtpref,
             ret.wtmult,
             ret.dtpref,
             ret.maxfilesize,
             ret.time_delta.seconds,
             ret.time_delta.nseconds,
             ret.properties) = struct.unpack(cls._RFMT, cls.read_exact(stream, cls._RSZ))
        else:
            ret.obj_attributes = post_op_attr.read(stream)
        return ret

class PATHCONF3args(ONCRPCargs):
    def __init__(self, aobject, xid=None):
        super().__init__(xid=xid)
        self.aobject = nfs_fh3(data=aobject) if aobject is not None else nfs_fh3()

    _DESC_ATTRS_2 = ['aobject']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.aobject.write(stream)

class PATHCONF3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.obj_attributes = None # post_op_attr
        self.linkmax = None # uint32
        self.name_max = None # uint32
        self.no_trunc = None # bool (int32)
        self.chown_restricted = None # bool (int32)
        self.case_insensitive = None # bool (int32)
        self.case_preserving = None # bool (int32)

    _DESC_ATTRS_2_OK = ['obj_attributes', 'linkmax', 'name_max', 'no_trunc', 'chown_restricted', 'case_insensitive', 'case_preserving']
    _DESC_ATTRS_2_FAIL = ['obj_attributes']

    _RFMT = '!IIiiii'
    _RSZ = struct.calcsize(_RFMT)

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.obj_attributes = post_op_attr.read(stream)
            (ret.linkmax,
             ret.name_max,
             ret.no_trunc,
             ret.chown_restricted,
             ret.case_insensitive,
             ret.case_preserving) = struct.unpack(cls._RFMT, cls.read_exact(stream, cls._RSZ))
        else:
            ret.obj_attributes = post_op_attr.read(stream)
        return ret

class COMMIT3args(ONCRPCargs):
    def __init__(self, afile=None, offset=0, count=0, xid=None):
        super().__init__(xid=xid)
        self.afile = afile if afile is not None else nfs_fh3()
        self.offset = offset # offset3 (uint64)
        self.count = count # count3 (uint32)

    _DESC_ATTRS_2 = ['afile', 'offset', 'count']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.afile.write(stream)
        stream.write(struct.pack('!QI', self.offset, self.count))

class COMMIT3res(NFS3RPCres):
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.file_wcc = None # wcc_data
        self.verf = None # writeverf3

    _DESC_ATTRS_2_OK = ['file_wcc', 'verf']
    _DESC_ATTRS_2_FAIL = ['file_wcc']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls.generate_from_status(stream)
        if ret.status == nfsstat3_NFS3_OK:
            ret.file_wcc = wcc_data.read(stream)
            ret.verf = writeverf3.read(stream)
        else:
            ret.file_wcc = wcc_data.read(stream)
        return ret

class MOUNT3_DUMPres(ONCRPCres):
    '''
    No real status for this RPC. Synthesize it from mountstat3_MNT3_OK.
    This result is really logically mountlist.
    '''
    def __init__(self, status, **kwargs):
        super().__init__(status=status, **kwargs)
        self.mounts = list() # [mountbody, ...]

    _DESC_ATTRS_2 = ['mounts']

    def __len__(self):
        return len(self.mounts)

    def __repr__(self):
        return type(self).__name__ + '(' + ','.join(['{' + ','.join([mount.desc_str()]) + '}' for mount in self.mounts]) + ')'

    def __str__(self):
        '''
        Define explicitly to avoid superclass definitions
        '''
        return self.__repr__()

    def contains(self, hostname, path):
        '''
        Returns whether a matching mount exists in the list
        '''
        he = os.fsencode(hostname)
        pe = os.fsencode(path)
        for mount in self.mounts:
            if (he == mount.ml_hostname) and (pe == mount.ml_directory):
                return True
        return False

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls(status=mountstat3_MNT3_OK)
        ret.mounts = cls.read_list(stream, mountbody.read)
        return ret

class MOUNT3_MNTargs(ONCRPCargs):
    def __init__(self, adirpath=None, xid=None):
        super().__init__(xid=xid)
        self.adirpath = dirpath(data=adirpath) if adirpath is not None else dirpath()

    _DESC_ATTRS_2 = ['adirpath']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.adirpath.write(stream)

class MOUNT3_MNTres(OkayOrFailRPCres):
    def __init__(self, status=None, **kwargs):
        super().__init__(status=status, **kwargs)
        self.fhandle = None # fhandle3
        self.auth_flavors = None # list of int

    _DESC_ATTRS_2_OK = ['fhandle', 'auth_flavors']
    _DESC_ATTRS_2_FAIL = []

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        status_val = cls.read_enum(stream)
        ret = cls(status=mountstat3(status_val))
        if ret.status == mountstat3_MNT3_OK:
            ret.fhandle = fhandle3.read(stream)
            ret.auth_flavors = ret.read_array(stream, cls.read_int32)
        return ret

class MOUNT3_UMNTargs(ONCRPCargs):
    def __init__(self, adirpath=None, xid=None):
        super().__init__(xid=xid)
        self.adirpath = dirpath(data=adirpath) if adirpath is not None else dirpath()

    _DESC_ATTRS_2 = ['adirpath']

    def write(self, stream):
        '''
        Write this to a stream
        '''
        self.adirpath.write(stream)

class MOUNT3_UMNTres(NULLres):
    pass

class MOUNT3_UMNTALLres(NULLres):
    pass

class MOUNT3_EXPORTres(OkayOrFailRPCres):
    '''
    The RPC has no status code of its own. We need a status
    to reflect errors at other levels, so we artificially
    use mountstat3_MNT3_OK for success.
    '''
    def __init__(self, status=None, **kwargs):
        status = status if status is not None else mountstat3_MNT3_OK
        super().__init__(status=status, **kwargs)
        self.exports = None # list(exportnode)

    @staticmethod
    def _desc_attrs():
        '''
        Return a list of attribute names interesting for desc()
        '''
        return ['exports']

    @classmethod
    def read(cls, stream):
        '''
        Generate from a stream
        '''
        ret = cls()
        ret.exports = cls.read_list(stream, exportnode.read)
        return ret

class MOUNT3_Client(TCPClient):
    _PROG = MOUNT3PROG
    _VERS = MOUNT3VERS

    mkcred = TCPClient.mkcred_unix

    def rpc_null(self, **kwargs):
        return self.make_call(MOUNTPROC3_NULL, NULLargs, NULLargs(), NULLres, **kwargs)

    def rpc_dump(self, **kwargs):
        return self.make_call(MOUNTPROC3_DUMP, NULLargs, NULLargs(), MOUNT3_DUMPres, **kwargs)

    def rpc_mnt(self, rpcargs, **kwargs):
        return self.make_call(MOUNTPROC3_MNT, MOUNT3_MNTargs, rpcargs, MOUNT3_MNTres, **kwargs)

    def rpc_umnt(self, rpcargs, **kwargs):
        return self.make_call(MOUNTPROC3_UMNT, MOUNT3_UMNTargs, rpcargs, MOUNT3_UMNTres, **kwargs)

    def rpc_umntall(self, **kwargs):
        return self.make_call(MOUNTPROC3_UMNTALL, NULLargs, NULLargs(), MOUNT3_UMNTALLres, **kwargs)

    def rpc_export(self, **kwargs):
        return self.make_call(MOUNTPROC3_EXPORT, NULLargs, NULLargs(), MOUNT3_EXPORTres, **kwargs)

class NFS3_Client(TCPClient):
    _PROG = NFS3PROG
    _VERS = NFS3VERS

    mkcred = TCPClient.mkcred_unix

    def rpc_null(self, **kwargs):
        return self.make_call(NFSPROC3_NULL, NULLargs, NULLargs(), NFS3_NULLres, **kwargs)

    def rpc_getattr(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_GETATTR, GETATTR3args, rpcargs, GETATTR3res, **kwargs)

    def rpc_setattr(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_SETATTR, SETATTR3args, rpcargs, SETATTR3res, **kwargs)

    def rpc_lookup(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_LOOKUP, LOOKUP3args, rpcargs, LOOKUP3res, **kwargs)

    def rpc_access(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_ACCESS, ACCESS3args, rpcargs, ACCESS3res, **kwargs)

    def rpc_readlink(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_READLINK, READLINK3args, rpcargs, READLINK3res, **kwargs)

    def rpc_read(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_READ, READ3args, rpcargs, READ3res, **kwargs)

    def rpc_write(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_WRITE, WRITE3args, rpcargs, WRITE3res, **kwargs)

    def rpc_create(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_CREATE, CREATE3args, rpcargs, CREATE3res, **kwargs)

    def rpc_mkdir(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_MKDIR, MKDIR3args, rpcargs, MKDIR3res, **kwargs)

    def rpc_symlink(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_SYMLINK, SYMLINK3args, rpcargs, SYMLINK3res, **kwargs)

    def rpc_mknod(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_MKNOD, MKNOD3args, rpcargs, MKNOD3res, **kwargs)

    def rpc_remove(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_REMOVE, REMOVE3args, rpcargs, REMOVE3res, **kwargs)

    def rpc_rmdir(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_RMDIR, RMDIR3args, rpcargs, RMDIR3res, **kwargs)

    def rpc_rename(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_RENAME, RENAME3args, rpcargs, RENAME3res, **kwargs)

    def rpc_link(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_LINK, LINK3args, rpcargs, LINK3res, **kwargs)

    def rpc_readdir(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_READDIR, READDIR3args, rpcargs, READDIR3res, **kwargs)

    def rpc_readdirplus(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_READDIRPLUS, READDIRPLUS3args, rpcargs, READDIRPLUS3res, **kwargs)

    def rpc_fsstat(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_FSSTAT, FSSTAT3args, rpcargs, FSSTAT3res, **kwargs)

    def rpc_fsinfo(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_FSINFO, FSINFO3args, rpcargs, FSINFO3res, **kwargs)

    def rpc_pathconf(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_PATHCONF, PATHCONF3args, rpcargs, PATHCONF3res, **kwargs)

    def rpc_commit(self, rpcargs, **kwargs):
        return self.make_call(NFSPROC3_COMMIT, COMMIT3args, rpcargs, COMMIT3res, **kwargs)
