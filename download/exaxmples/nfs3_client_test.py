#!/usr/bin/env python
#
# bin/nfs3_client_test.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''
Tests to exercise the HPC Cache NFS3 client. This is not intended
to be a thorough exercise of the server. It is intended
to verify the protocol implementation in nfs3.py and rpc_rfc1057.py.
'''
import argparse
import math
import os
import pprint
import sys
import time
import traceback
import uuid

import avere.nfs3py.nfs3 as nfs3
import avere.nfs3py.nfs3_util as nfs3_util
import avere.nfs3py.rpc_rfc1057 as rpc_rfc1057

exc_info_err = nfs3_util.exc_info_err

class TestFailure(Exception):
    '''
    A test has failed
    '''
    # no specialization

class LoggerState(nfs3_util.LoggerState):
    LOGGER_NAME_DEFAULT = 'nfs3_client_test'

    _LOG_FORMAT_BASE = '%(asctime)s %(levelname).3s %(filename)20s:%(lineno)04d '

    @classmethod
    def logger_set(cls, logger):
        with cls._lock:
            cls._logger = logger
            super().logger_set(logger)

def getframe(idx, include_lineno=True):
    '''
    Return a string of the form caller_name:linenumber.
    idx is the number of frames up the stack, so 1 = immediate caller.
    '''
    f = sys._getframe(idx+1) # pylint: disable=protected-access
    ret = f.f_code.co_name
    if include_lineno:
        ret += ':'
        ret += str(f.f_lineno)
    return ret

class Test():
    def __init__(self, logger, desc, path, mnt, cli):
        self.logger = logger
        self.desc = desc
        self.path = path
        self._pathfh = None
        self._rootfh = None
        self.testdir = None # Name in self.path of self.testfh
        self.testpath = None # Full path of self.testfh
        self.testfh = None # Subdir of self.path
        self.mnt = mnt
        self.cli = cli

    @property
    def pathfh(self):
        if self._pathfh is None:
            try:
                self._pathfh = nfs3_util.resolve_path(self.mnt, self.cli, self.path, logger=self.logger)
            except Exception as e:
                raise TestFailure("cannot resolve test path: %s" % exc_info_err()) from e
        return self._pathfh

    @property
    def rootfh(self):
        if self._rootfh is None:
            try:
                self._rootfh = nfs3_util.resolve_path(self.mnt, self.cli, '/', logger=self.logger)
            except Exception as e:
                raise TestFailure("cannot resolve root: %s" % exc_info_err()) from e
        return self._rootfh

    def fail(self):
        logger = self.logger
        logger.error("desc=%s failed from %s\n%s",
                     self.desc, getframe(1),
                     pprint.pformat(traceback.format_stack()))
        raise SystemExit(1)

    def good(self):
        logger = self.logger
        logger.info("GOOD %s %s", getframe(1), self.desc)

    def test_null(self):
        '''
        Exercise NULL RPCs (proc 0)
        '''
        logger = self.logger
        mnt = self.mnt
        cli = self.cli

        res = mnt.rpc_null(logger=self.logger)
        logger.info("mnt null res=%s", res)
        self.expect_success(res)

        res = cli.rpc_null(logger=self.logger)
        logger.info("cli null res=%s", res)
        self.expect_success(res)

        self.good()

    @staticmethod
    def expect_truthy(val):
        if not bool(val):
            raise TestFailure("expected but did not get truthy: %s" % repr(val))

    @staticmethod
    def expect_falsey(val):
        if bool(val):
            raise TestFailure("expected but did not get truthy: %s" % repr(val))

    @staticmethod
    def expect_success(res):
        '''
        expect res to be a valid success result
        '''
        if not isinstance(res, rpc_rfc1057.ONCRPCres):
            raise TestFailure("result %s (%s) is not ONCRPCres" % (type(res).__name__, repr(res)))
        if res:
            raise TestFailure("result %s is not success" % repr(res))

    @staticmethod
    def expect_fail(res):
        '''
        expect res to be a valid fail result
        '''
        if not isinstance(res, rpc_rfc1057.ONCRPCres):
            raise TestFailure("result %s (%s) is not ONCRPCres" % (type(res).__name__, repr(res)))
        if not res:
            raise TestFailure("result %s is not failure" % repr(res))

    @staticmethod
    def expect_isinstance(a, b):
        if not isinstance(a, b):
            raise TestFailure("expected %s (%s) to be an instance of %s" % (type(a).__name__, repr(a), type(b).__name__))

    @staticmethod
    def expect_not_None(val):
        if val is None:
            raise TestFailure("result is None")

    @staticmethod
    def expect_eq(a, b):
        # Force __eq__
        if not a == b:
            raise TestFailure("%s != %s" % (repr(a), repr(b)))

    @staticmethod
    def datestr(ts=None):
        '''
        Convert ts (float timestamp) to a human string
        '''
        ts = ts if ts is not None else time.time()
        tmp = time.strftime('%Y-%m-%d %H:%M:%S.', time.localtime(ts))
        ns = int(math.floor((ts - math.floor(ts)) * 1000000000.0))
        return tmp + "%09d" % ns

    @classmethod
    def uniqstr(cls, prefix=''):
        ret = prefix
        if ret:
            if not ret.endswith('.'):
                ret += '.'
        else:
            ret = ''
        ret += cls.datestr()
        ret += '.'
        ret += str(uuid.uuid4())
        return ret

    def test_resolve_root(self):
        '''
        Verify that resolve_path() DTRT with the root
        '''
        logger = self.logger
        mnt = self.mnt
        cli = self.cli

        mntargs = nfs3.MOUNT3_MNTargs(adirpath=os.path.sep)
        mntres = mnt.rpc_mnt(mntargs, logger=self.logger)
        logger.info("mnt args=%s res=%s", mntargs, mntres)
        self.expect_success(mntres)
        fhandle = mntres.fhandle
        self.expect_isinstance(fhandle, nfs3.fhandle3)

        fh = nfs3_util.resolve_path(mnt, cli, os.path.sep, logger=logger)
        self.expect_isinstance(fh, nfs3.nfs_fh3)

        self.expect_eq(fhandle, fh)

        # Cache for future use
        self._rootfh = fh

    def test_getattr_root(self):
        logger = self.logger
        cli = self.cli

        ga_args = nfs3.GETATTR3args(aobject=self.rootfh)
        ga_res = cli.rpc_getattr(ga_args, logger=self.logger)
        logger.info("getattr args %s", ga_args)
        logger.info("getattr res %s", ga_res)
        self.expect_success(ga_res)
        self.expect_eq(ga_res.obj_attributes.ftype, nfs3.ftype3_NF3DIR)

    def test_common(self):
        '''
        Run all tests that do not assume any special
        casing for raw vs util.
        '''
        self.test_mount()
        self.good()
        self.test_resolve_root()
        self.good()
        self.test_getattr_root()
        self.good()
        self.test_nfs3()
        self.good()

    def show_dump(self):
        '''
        Perform a dump and show the results.
        Returns the RPC result for dump.
        '''
        logger = self.logger
        mnt = self.mnt
        res = mnt.rpc_dump(logger=self.logger)
        logger.info("dump res %s", res)
        self.expect_success(res)
        if res.mounts:
            txt = "mount points:"
            for mount in res.mounts:
                txt += "\n%s:%s" % (mount.ml_hostname, mount.ml_directory)
            logger.info("%s", txt)
        else:
            logger.info("no mounts")
        return res

    def show_exports(self):
        '''
        List exports and show the results.
        Returns the RPC result.
        '''
        logger = self.logger
        mnt = self.mnt
        res = mnt.rpc_export(logger=self.logger)
        logger.info("export res %s", res)
        self.expect_success(res)
        if res.exports:
            txt = "exports:"
            for export in res.exports:
                txt += "\n%s %s" % (export.ex_dir, export.ex_groups)
            logger.info("%s", txt)
        else:
            logger.info("no exports")
        return res

    def test_mount(self):
        '''
        Test mount RPCs
        '''
        logger = self.logger
        mnt = self.mnt

        res = mnt.rpc_null(logger=self.logger)
        logger.info("null res %s", res)
        self.expect_success(res)

        # dump
        self.show_dump()
        self.good()

        # export
        exportres = self.show_exports()
        self.good()

        # mnt
        mnt_dirpath = '/'
        if exportres.exports:
            mnt_dirpath = exportres.exports[0].ex_dir
        logger.info("mnt_dirpath=%s", mnt_dirpath)
        mntargs = nfs3.MOUNT3_MNTargs(adirpath=mnt_dirpath)
        mntres = mnt.rpc_mnt(mntargs, logger=self.logger)
        logger.info("mnt args=%s res=%s", mntargs, mntres)
        self.expect_success(mntres)
        self.show_dump()
        # I'd like to verify that the mount shows up in the list,
        # but armada does not support that.
        self.good()

        # umnt
        umntargs = nfs3.MOUNT3_UMNTargs(adirpath=mnt_dirpath)
        umntres = mnt.rpc_umnt(umntargs, logger=self.logger)
        logger.info("umnt args=%s res=%s", umntargs, umntres)
        self.expect_success(umntres)
        self.show_dump()
        self.good()

        logger.info("mount all exports")
        for export in exportres.exports:
            mntargs = nfs3.MOUNT3_MNTargs(adirpath=export.ex_dir)
            mntres = mnt.rpc_mnt(mntargs, logger=self.logger)
            logger.info("mnt args=%s res=%s", mntargs, mntres)
            self.expect_success(mntres)
        logger.info("all exports mounted")
        self.show_dump()
        self.good()

        # umntall
        umntallres = mnt.rpc_umntall(logger=self.logger)
        logger.info("umntallres %s", umntallres)
        self.expect_success(umntallres)
        self.show_dump()
        self.good()

    def test_nfs3(self):
        '''
        Test all NFS3 operations
        '''
        logger = self.logger
        cli = self.cli

        # MKDIR3 directory for testing
        logger.info("%s rootfh %s", getframe(0), self.rootfh)
        self.testdir = self.uniqstr('prefix=nfs3')
        self.testpath = os.path.join(self.path, self.testdir)
        attr = nfs3.sattr3(mode=0o755, uid=os.getuid(), gid=os.getgid()) # pylint: disable=no-member,useless-suppression
        mkdir_args = nfs3.MKDIR3args(adir=self.pathfh, aname=self.testdir, attributes=attr)
        mkdir_res = cli.rpc_mkdir(mkdir_args, logger=self.logger)
        logger.info("mkdir args %s", mkdir_args)
        logger.info("mkdir res %s", mkdir_res)
        self.expect_success(mkdir_res)
        self.testfh = mkdir_res.get_handle()
        if self.testfh is None:
            largs = nfs3.LOOKUP3args(self.rootfh, self.testdir)
            lres = cli.rpc_lookup(largs, logger=logger)
            self.expect_success(lres)
            self.testfh = lres.get_handle()
        self.expect_truthy(self.testfh)
        self.good()

        # MKDIR3 - EXIST
        margs = nfs3.MKDIR3args(adir=self.pathfh, aname=self.testdir, attributes=attr)
        mres = mkdir_res = cli.rpc_mkdir(mkdir_args, logger=self.logger)
        logger.info("mkdir args %s", mkdir_args)
        logger.info("mkdir res %s", mkdir_res)
        self.expect_fail(mkdir_res)
        self.expect_eq(mkdir_res.status, nfs3.nfsstat3_NFS3ERR_EXIST)
        self.good()

        # GETATTR3 with junk fh
        gargs = nfs3.GETATTR3args(aobject=b'abcde')
        gres = cli.rpc_getattr(gargs, logger=logger)
        logger.info("getattr args %s", gargs)
        logger.info("getattr res  %s", gres)
        self.expect_fail(gres)
        if not isinstance(gres, nfs3.GETATTR3res):
            raise TestFailure("gres %s is not GETATTR3res" % type(gres).__name__)
        self.good()

        # CREATE3 a file to use for tests
        name = 'f1'
        cargs = nfs3.CREATE3args(self.testfh, name)
        cres = cli.rpc_create(cargs, logger=logger)
        logger.info("create args %s", cargs)
        logger.info("create res %s", cres)
        self.expect_success(cres)
        fh = cres.get_handle()
        if fh is None:
            largs = nfs3.LOOKUP3args(self.testfh, name)
            lres = cli.rpc_lookup(largs, logger=logger)
            self.expect_success(lres)
            fh = lres.aobject
        logger.info("f1 is %s", fh)
        self.good()

        # GETATTR3 succeeds
        gargs = nfs3.GETATTR3args(aobject=fh)
        gres = cli.rpc_getattr(gargs, logger=logger)
        logger.info("getattr args %s", gargs)
        logger.info("getattr res  %s", gres)
        self.expect_success(gres)
        self.good()

        # SETATTR3 succeeds
        attr = nfs3.sattr3(size=513)
        sargs = nfs3.SETATTR3args(aobject=fh, new_attributes=attr)
        sres = cli.rpc_setattr(sargs, logger=logger)
        logger.info("setattr args %s", sargs)
        logger.info("setattr res  %s", sres)
        self.expect_success(sres)
        gargs = nfs3.GETATTR3args(aobject=fh)
        gres = cli.rpc_getattr(gargs, logger=logger)
        logger.info("getattr args %s", gargs)
        logger.info("getattr res  %s", gres)
        self.expect_success(gres)
        self.expect_eq(gres.obj_attributes.size, 513)
        self.good()

        # SETATTR3 guard checks
        ctime_good = gres.obj_attributes.ctime
        ctime_bad = nfs3.nfstime3(seconds=ctime_good.seconds+1, nseconds=ctime_good.nseconds)
        sargs.guard = nfs3.sattrguard3(obj_ctime=ctime_bad)
        sargs.new_attributes.size.size = 515
        sres = cli.rpc_setattr(sargs, logger=logger)
        logger.info("setattr args %s", sargs)
        logger.info("setattr res  %s", sres)
        self.expect_fail(sres)
        self.expect_eq(sres.status, nfs3.nfsstat3_NFS3ERR_NOT_SYNC)
        sargs.guard = nfs3.sattrguard3(obj_ctime=ctime_good)
        sargs.new_attributes.size.size = 517
        sres = cli.rpc_setattr(sargs, logger=logger)
        logger.info("setattr args %s", sargs)
        logger.info("setattr res  %s", sres)
        self.expect_success(sres)
        gargs = nfs3.GETATTR3args(aobject=fh)
        gres = cli.rpc_getattr(gargs, logger=logger)
        logger.info("getattr args %s", gargs)
        logger.info("getattr res  %s", gres)
        self.expect_success(gres)
        self.expect_eq(gres.obj_attributes.size, 517)
        self.good()

        # LOOKUP3 succeeds
        largs = nfs3.LOOKUP3args(self.testfh, name)
        lres = cli.rpc_lookup(largs, logger=logger)
        logger.info("lookup args %s", largs)
        logger.info("lookup res  %s", lres)
        self.expect_success(lres)
        self.expect_eq(lres.aobject, fh)

        # LOOKUP3 fails with NOENT
        largs = nfs3.LOOKUP3args(self.testfh, 'f2')
        lres = cli.rpc_lookup(largs, logger=logger)
        logger.info("lookup args %s", largs)
        logger.info("lookup res  %s", lres)
        self.expect_fail(lres)
        self.expect_eq(lres.status, nfs3.nfsstat3_NFS3ERR_NOENT)
        self.good()

        # ACCESS3 with bad handle
        aargs = nfs3.ACCESS3args(aobject=b'abcde', access=0xf)
        ares = cli.rpc_access(aargs, logger=logger)
        logger.info("access args %s", aargs)
        logger.info("access res  %s", ares)
        self.expect_fail(ares)
        self.good()

        # ACCESS3 succeeds
        aargs = nfs3.ACCESS3args(aobject=fh, access=0xf)
        ares = cli.rpc_access(aargs, logger=logger)
        logger.info("access args %s", aargs)
        logger.info("access res  %s", ares)
        self.expect_success(ares)
        self.good()

        # SYMLINK3 succeeds
        linkdata = nfs3.nfspath3(data=str(uuid.uuid4()))
        sargs = nfs3.SYMLINK3args(dirfh=self.testfh, aname='symlink1', target=linkdata)
        sres = cli.rpc_symlink(sargs, logger=logger)
        logger.info("symlink args %s", sargs)
        logger.info("symlink res %s", sres)
        self.expect_success(sres)
        largs = nfs3.LOOKUP3args(self.testfh, 'symlink1')
        lres = cli.rpc_lookup(largs, logger=logger)
        logger.info("lookup args %s", largs)
        logger.info("lookup res  %s", lres)
        self.expect_success(lres)
        symlinkfh = lres.get_handle()
        if sres.get_handle():
            self.expect_eq(sres.get_handle(), symlinkfh)
        self.good()

        # READLINK3 succeeds
        rargs = nfs3.READLINK3args(symlink=symlinkfh)
        rres = cli.rpc_readlink(rargs, logger=logger)
        logger.info("readlink args %s", rargs)
        logger.info("readlink res  %s", rres)
        self.expect_success(rres)
        self.expect_eq(rres.data, linkdata)
        self.good()

        # READ3 - attempt to read more bytes than are in the file
        gargs = nfs3.GETATTR3args(aobject=fh)
        gres = cli.rpc_getattr(gargs, logger=logger)
        logger.info("getattr args %s", gargs)
        logger.info("getattr res  %s", gres)
        self.expect_success(gres)
        rargs = nfs3.READ3args(afile=fh, offset=0, count=gres.obj_attributes.size+5)
        rres = cli.rpc_read(rargs, logger=logger)
        logger.info("read args %s", rargs)
        logger.info("read res  %s", rres)
        self.expect_success(rres)
        self.expect_eq(rres.count, len(rres.data))

        # WRITE3 - write some data, then read it back
        data = os.urandom(1025)
        wargs = nfs3.WRITE3args(afile=fh, offset=0, count=len(data), data=data)
        wres = cli.rpc_write(wargs, logger=logger)
        logger.info("write args %s", wargs)
        logger.info("write res  %s", wres)
        self.expect_success(wres)
        rargs = nfs3.READ3args(afile=fh, offset=0, count=wargs.count)
        rres = cli.rpc_read(rargs, logger=logger)
        logger.info("read args %s", rargs)
        logger.info("read res  %s", rres)
        self.expect_success(rres)
        self.expect_eq(rres.count, len(rres.data))
        self.expect_eq(rres.data, data)
        self.good()

        # COMMIT3
        cargs = nfs3.COMMIT3args(afile=fh, offset=0, count=len(data))
        cres = cli.rpc_commit(cargs, logger=logger)
        logger.info("commit args %s", cargs)
        logger.info("commit res  %s", cres)
        self.expect_success(cres)
        self.good()

        # MKNOD3
        for nt in range(8):
            ft = nfs3.ftype3(nt)
            if ft in (nfs3.ftype3_NF3BLK, nfs3.ftype3_NF3CHR):
                ndata = nfs3.devicedata3(spec=nfs3.specdata3(2, 3))
            elif ft in (nfs3.ftype3_NF3SOCK, nfs3.ftype3_NF3FIFO):
                ndata = nfs3.sattr3()
            else:
                continue
            mname = "node%d" % nt
            what = nfs3.mknoddata3(ftype=ft, data=ndata)
            margs = nfs3.MKNOD3args(dirfh=self.testfh, aname=mname, what=what)
            mres = cli.rpc_mknod(margs, logger=logger)
            logger.info("mknod args %s", margs)
            logger.info("mknod res %s", mres)
            if not isinstance(mres, nfs3.MKNOD3res):
                raise TestFailure("mres %s is not MKNOD3res" % type(mres).__name__)
            if mres.status not in (nfs3.nfsstat3_NFS3_OK, nfs3.nfsstat3_NFS3ERR_ACCES, nfs3.nfsstat3_NFS3ERR_NOTSUPP):
                raise TestFailure("unexpected status %s" % repr(mres.status))
            logger.info("GOOD %s %s %s", getframe(0), self.desc, ft)
        self.good()

        # CREATE3 then REMOVE3 succeed
        name = 'f2'
        cargs = nfs3.CREATE3args(self.testfh, name)
        cres = cli.rpc_create(cargs, logger=logger)
        logger.info("create args %s", cargs)
        logger.info("create res %s", cres)
        self.expect_success(cres)
        rargs = nfs3.REMOVE3args(self.testfh, name)
        rres = cli.rpc_remove(rargs, logger=logger)
        logger.info("remove args %s", rargs)
        logger.info("remove res  %s", rres)
        self.expect_success(rres)
        self.good()

        # REMOVE3 fails
        rargs = nfs3.REMOVE3args(self.testfh, 'f3')
        rres = cli.rpc_remove(rargs, logger=logger)
        logger.info("remove args %s", rargs)
        logger.info("remove res  %s", rres)
        self.expect_fail(rres)
        self.expect_eq(rres.status, nfs3.nfsstat3_NFS3ERR_NOENT)
        self.good()

        # MKDIR3 then RMDIR3 succeeding and failing
        name = 'd3'
        margs = nfs3.MKDIR3args(adir=self.testfh, aname=name)
        mres = cli.rpc_mkdir(margs, logger=logger)
        logger.info("mkdir args %s", margs)
        logger.info("mkdir res  %s", mres)
        self.expect_success(mres)
        mres = cli.rpc_mkdir(margs, logger=logger)
        logger.info("mkdir args %s", margs)
        logger.info("mkdir res  %s", mres)
        self.expect_fail(mres)
        self.expect_eq(mres.status, nfs3.nfsstat3_NFS3ERR_EXIST)
        rargs = nfs3.RMDIR3args(self.testfh, name)
        rres = cli.rpc_rmdir(rargs, logger=logger)
        logger.info("rmdir args %s", rargs)
        logger.info("rmdir res  %s", rres)
        self.expect_success(rres)
        rres = cli.rpc_rmdir(rargs, logger=logger)
        logger.info("rmdir args %s", rargs)
        logger.info("rmdir res  %s", rres)
        self.expect_fail(rres)
        self.expect_eq(rres.status, nfs3.nfsstat3_NFS3ERR_NOENT)
        self.good()

        # READDIR3
        rargs = nfs3.READDIR3args(adir=self.testfh)
        rres = cli.rpc_readdir(rargs)
        logger.info("readdir args %s", rargs)
        logger.info("readdir res %s", rres)
        self.expect_success(rres)
        rargs = nfs3.READDIR3args(adir=b'abcde')
        rres = cli.rpc_readdir(rargs)
        logger.info("readdir args %s", rargs)
        logger.info("readdir res %s", rres)
        self.expect_fail(rres)

        # READDIRPLUS3
        rargs = nfs3.READDIRPLUS3args(adir=self.testfh)
        rres = cli.rpc_readdirplus(rargs)
        logger.info("readdirplus args %s", rargs)
        logger.info("readdirplus res %s", rres)
        self.expect_success(rres)
        rargs = nfs3.READDIRPLUS3args(adir=b'abcde')
        rres = cli.rpc_readdirplus(rargs)
        logger.info("readdirplus args %s", rargs)
        logger.info("readdirplus res %s", rres)
        self.expect_fail(rres)

        # RENAME3 succeeds and fails
        name1 = 'f2'
        name2 = 'f3'
        cargs = nfs3.CREATE3args(self.testfh, name1)
        cres = cli.rpc_create(cargs, logger=logger)
        logger.info("create args %s", cargs)
        logger.info("create res %s", cres)
        self.expect_success(cres)
        rargs = nfs3.RENAME3args(from_fh=self.testfh, from_name=name1, to_fh=self.testfh, to_name=name2)
        rres = cli.rpc_rename(rargs, logger=logger)
        logger.info("rename args %s", rargs)
        logger.info("rename res  %s", rres)
        self.expect_success(rres)
        rargs = nfs3.RENAME3args(from_fh=self.testfh, from_name=name1, to_fh=self.testfh, to_name=name2)
        rres = cli.rpc_rename(rargs, logger=logger)
        logger.info("rename args %s", rargs)
        logger.info("rename res  %s", rres)
        self.expect_fail(rres)
        self.expect_eq(rres.status, nfs3.nfsstat3_NFS3ERR_NOENT)
        self.good()

        # LINK3 succeeds
        linkargs = nfs3.LINK3args(afile=fh, dirfh=self.testfh, aname='f4')
        linkres = cli.rpc_link(linkargs, logger=logger)
        logger.info("link args %s", linkargs)
        logger.info("link res  %s", linkres)
        self.expect_success(linkres)
        fhs = list()
        for name in ['f1', 'f4']:
            largs = nfs3.LOOKUP3args(self.testfh, name)
            lres = cli.rpc_lookup(largs, logger=logger)
            logger.info("lookup args %s", largs)
            logger.info("lookup res  %s", lres)
            self.expect_success(lres)
            fhs.append(lres.get_handle())
        self.expect_eq(*fhs) # pylint: disable=no-value-for-parameter
        self.good()

        # LINK3 fails
        linkargs = nfs3.LINK3args(afile=fh, dirfh=self.testfh, aname='f4')
        linkres = cli.rpc_link(linkargs, logger=logger)
        logger.info("link args %s", linkargs)
        logger.info("link res  %s", linkres)
        self.expect_fail(linkres)
        self.expect_eq(linkres.status, nfs3.nfsstat3_NFS3ERR_EXIST)
        self.good()

        # FSSTAT succeeds and fails
        exportres = self.show_exports()
        efh = None
        for export in exportres.exports:
            mntargs = nfs3.MOUNT3_MNTargs(adirpath=export.ex_dir)
            mntres = self.mnt.rpc_mnt(mntargs, logger=logger)
            if not mntres:
                efh = nfs3.nfs_fh3(data=mntres.fhandle)
                break
        if not efh:
            efh = self.rootfh
        args = nfs3.FSSTAT3args(fsroot=efh)
        res = cli.rpc_fsstat(args, logger=logger)
        logger.info("fsstat args %s", args)
        logger.info("fsstat res  %s", res)
        self.expect_success(res)
        args = nfs3.FSSTAT3args(fsroot=b'abcde')
        res = cli.rpc_fsstat(args, logger=logger)
        logger.info("fsstat args %s", args)
        logger.info("fsstat res  %s", res)
        self.expect_fail(res)
        self.good()

        # FSINFO succeeds and fails
        args = nfs3.FSINFO3args(fsroot=efh)
        res = cli.rpc_fsinfo(args, logger=logger)
        logger.info("fsinfo args %s", args)
        logger.info("fsinfo res  %s", res)
        self.expect_success(res)
        args = nfs3.FSINFO3args(fsroot=b'abcde')
        res = cli.rpc_fsinfo(args, logger=logger)
        logger.info("fsinfo args %s", args)
        logger.info("fsinfo res  %s", res)
        self.expect_fail(res)
        self.good()

        # PATHCONF succeeds and fails
        args = nfs3.PATHCONF3args(aobject=efh)
        res = cli.rpc_pathconf(args, logger=logger)
        logger.info("pathconf args %s", args)
        logger.info("pathconf res  %s", res)
        self.expect_success(res)
        args = nfs3.PATHCONF3args(aobject=b'abcde')
        res = cli.rpc_pathconf(args, logger=logger)
        logger.info("pathconf args %s", args)
        logger.info("pathconf res  %s", res)
        self.expect_fail(res)
        self.good()

def fail():
    logger = LoggerState.logger_get()
    logger.error("failed")
    raise SystemExit(1)

def main():
    ap_parser = argparse.ArgumentParser()
    ap_parser.add_argument('-a', '--addr', type=str, default='',
                           help='address to test')
    ap_parser.add_argument('-p', '--path', type=str, default='',
                           help='root directory for write-like tests')
    ap_args = ap_parser.parse_args()
    logger = LoggerState.logger_get()

    addr = ap_args.addr
    if not addr:
        logger.error("no address provided")
        raise SystemExit(1)

    path = ap_args.path
    if not path:
        logger.error("no path provided")
        raise SystemExit(1)
    if not path.startswith(os.path.sep):
        logger.error("invalid path='%s' (does not start with '%s')", path, os.path.sep)
        raise SystemExit(1)
    if path.endswith(os.path.sep):
        logger.error("invalid path='%s' (ends with '%s')", path, os.path.sep)
        raise SystemExit(1)
    if path[1:]:
        for x in path[1:].split(os.path.sep):
            if not x:
                logger.error("invalid path='%s'", path)
                raise SystemExit(1)

    mnt = nfs3.MOUNT3_Client(addr, reserved=True)
    cli = nfs3.NFS3_Client(addr)
    t_raw = Test(logger, 'raw', path, mnt, cli)

    mnt = nfs3_util.MOUNT3_Client(addr, reserved=True)
    cli = nfs3_util.NFS3_Client(addr)
    t_util = Test(logger, 'util', path, mnt, cli)

    # First do NULL tests. This verifies basic mechanics of make_call().
    t_raw.test_null()
    t_util.test_null()

    # Full tests that do not assume
    # nfs3_util special behaviors.
    t_raw.test_common()
    t_util.test_common()

    raise SystemExit(0)

if __name__ == "__main__":
    main()
    raise SystemExit(1)
