#!/usr/bin/env python
#
# lib/hpccache/nfs3py/nfs3_util.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''
Various utility / helper operations useful for nfs3 clients
'''
import os
import pprint
import sys
import time

import hpccache.nfs3py.nfs3 as nfs3

class LoggerState(nfs3.LoggerState):
    LOGGER_NAME_DEFAULT = 'hpccache.nfs3_util'

    @classmethod
    def logger_set(cls, logger):
        with cls._lock:
            cls._logger = logger
            super().logger_set(logger)

# These parameters apply specifically to NFS3 calls. They do not
# apply to other calls that are necessary for the health check,
# such as mount and portmapper.
#
# callTries is the maximum number of times to attempt each call.
# timeoutRel is how long to give an individual try before timing-out.
# A call is considered retryable if it failed at the transport level
# or if it jukeboxed.
nfs3_callTries_default = 5
nfs3_timeoutRel_default = 30.0

class _LatencyTrackerOp():
    'Track min/max/mean times for a single operation'
    def __init__(self, opname, stat_class):
        self.stat_class = stat_class
        self.opname = opname
        self.opcount = 0
        self.opcount_success = 0
        self.errors = list()
        self.latency_min = None
        self.latency_max = None
        self.latency_min_status = None
        self.latency_max_status = None
        self.total_time = 0.0

    def __repr__(self):
        return "<%s,%s,%s>" % (type(self).__name__, hex(id(self)), str(self))

    def __str__(self):
        if self.opcount > 0:
            mean = self.total_time / float(self.opcount)
            return "%s min=%f,%s max=%f,%s count=%u mean=%f" \
                % (self.opname,
                   self.latency_min, self.latency_min_status,
                   self.latency_max, self.latency_max_status,
                   self.opcount, mean)
        return "%s no ops" % self.opname

    def _error(self, status):
        '''
        Return status in error dict form {'string':'x', 'value':-1}
        '''
        if not self.stat_class.valid(status):
            err = {'string' : str(status),
                   'value' : -1,
                  }
            return err
        int_status = int(status)
        err = {'string' : self.stat_class.string_from_value(status),
               'value' : int_status,
              }
        return err

    def append_error(self, status):
        self.errors.append(self._error(status))

    def complete(self, start, end, status, logger=None):
        '''
        An operation has completed.
        start: time.time() when the operation began
        end: time.time when the operation ended
        status: either None or ONCRPCres for the completed operation
        '''
        logger = LoggerState.logger_get(logger=logger)
        self.opcount += 1
        if end >= start:
            latency = end - start
        else:
            latency = 0.0
        self.total_time += latency
        if not self.stat_class.valid(status):
            # latency_min and latency_max only include wire responses
            self.append_error(status)
            return
        int_status = None
        try:
            int_status = int(status)
        except Exception:
            logger.warning("cannot convert %s '%s' to int: %s",
                           type(status).__name__, status, exc_info_err())
            self.append_error(status)
            return
        if int_status == 0:
            self.opcount_success += 1
        else:
            self.append_error(status)
        if (self.latency_min is None) or (latency < self.latency_min):
            self.latency_min = latency
            self.latency_min_status = int_status
        if (self.latency_max is None) or (latency > self.latency_max):
            self.latency_max = latency
            self.latency_max_status = int_status

    def report(self):
        ret = {'opname' : self.opname,
               'opcount' : self.opcount,
               'successful_opcount' : self.opcount_success,
               'total_time' : float(self.total_time),
               'error_list' : self.errors,
              }
        if self.latency_min is None:
            ret['latency_min'] = 0.0
            ret['latency_min_status'] = {'string' : '',
                                         'value' : -1,
                                        }
        else:
            ret['latency_min'] = float(self.latency_min)
            ret['latency_min_status'] = self._error(self.latency_min_status)
        if self.latency_max is None:
            ret['latency_max'] = 0.0
            ret['latency_max_status'] = {'string' : '',
                                         'value' : -1,
                                        }
        else:
            ret['latency_max'] = float(self.latency_max)
            ret['latency_max_status'] = self._error(self.latency_max_status)
        return ret

class LatencyTracker():
    'Wrapper to track latencies (using _LatencyTrackerOp) for zero or more operations'
    def __init__(self, stat_class):
        self._ops_all = dict()
        self._ops_success = dict()
        self.stat_class = stat_class
        self.success = True

    def __repr__(self):
        return "<%s,%s,%s>" % (type(self).__name__, hex(id(self)), repr(self._ops_all))

    def __str__(self):
        return str(self._ops_all)

    @property
    def ops(self):
        'Accessor for the dictionary of operations'
        return self._ops_all

    def complete(self, opname, start, end, status, logger=None):
        '''
        An operation has completed.
        opname: string identifying the operation type
        start: time.time() when the operation began
        end: time.time when the operation ended
        status: either None or ONCRPCres for the completed operation
        '''
        logger = LoggerState.logger_get(logger=logger)
        dlist = [self._ops_all]
        int_status = None
        if isinstance(status, str):
            self.success = False
        else:
            try:
                int_status = int(status)
            except Exception:
                # _LatencyTrackerOp.complete() will log this
                int_status = None
        if int_status == 0:
            dlist.append(self._ops_success)
        else:
            self.success = False
        for ops_dict in dlist:
            try:
                op = ops_dict[opname]
            except KeyError:
                op = _LatencyTrackerOp(opname, self.stat_class)
                ops_dict[opname] = op
            op.complete(start, end, status, logger=logger)

    def dump(self, logger=None):
        'log each operation in this tracker'
        logger = LoggerState.logger_get(logger=logger)
        logger.info("%s", pprint.pformat(self.report()))

    _REPORT_KEYS_SUCC = ('opname',
                         'successful_opcount',
                         'total_time',
                         'latency_min',
                         'latency_max',
                        )
    _REPORT_KEYS_ALL = ('opcount',
                        'error_list',
                       )

    def report(self):
        ret = {'success' : bool(self.success),
               'latency' : dict()
              }
        for opname, op_all in self._ops_all.items():
            try:
                op_succ = self._ops_success[opname]
            except KeyError:
                op_succ = _LatencyTrackerOp(opname, self.stat_class)
            report_succ = op_succ.report()
            report_all = op_all.report()
            ret['latency'][opname] = {k : report_all[k] for k in self._REPORT_KEYS_ALL}
            ret['latency'][opname].update({k : report_succ[k] for k in self._REPORT_KEYS_SUCC})
        return ret

class MOUNT3_Client(nfs3.MOUNT3_Client):
    'Wrapper around a raw nfs3.MOUNT3_Client that provides additional convenience methods'
    def find_root_in_export_list(self, logger=None):
        'Return a bool indicating whether or not the root is in the export list'
        logger = LoggerState.logger_get(logger=logger)
        res = self.rpc_export()
        if res.status != nfs3.mountstat3_MNT3_OK:
            logger.error("cannot list exports: %s", res.status)
            return False
        ret = False
        for exportHpcCache in res.exports:
            logger.debug("export %s", exportHpcCache)
            dirpath = exportHpcCache.ex_dir.to_string()
            if dirpath == "/":
                # keep going for debug, logging all of the exports
                ret = True
        return ret

    @staticmethod
    def get_rootfh_using_mnt(mnt, logger=None):
        '''
        Return the root filehandle as fhandle3. This is the result of mount "/".
        '''
        logger = LoggerState.logger_get(logger=logger)
        if not isinstance(mnt, nfs3.MOUNT3_Client):
            raise TypeError("mnt expected nfs3.MOUNT3_Client, got %s" % type(mnt))
        mount_args = nfs3.MOUNT3_MNTargs(adirpath="/")
        res = mnt.rpc_mnt(mount_args)
        logger.debug("get_rootfh status=%s", res.status)
        if res.status != nfs3.mountstat3_MNT3_OK:
            logger.error("cannot mount /: %s", res.status)
            return None
        return res.fhandle

    def get_rootfh(self, logger=None):
        'Return the root filehandle as fhandle3. This is the result of mount "/".'
        return self.get_rootfh_using_mnt(self, logger=logger)

class NFS3_Client(nfs3.NFS3_Client):
    'Wrapper around a raw nfs3.NFS3_Client that provides additional convenience methods'
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.latencies = LatencyTracker(nfs3.nfsstat3)

    def make_call(self, proc, rpcargs_class, rpcargs, res_class, **kwargs): # pylint: disable=arguments-differ
        '''
        make_call is wrapped here to override the default timeoutRel and callTries
        and to provide retries for NFS3ERR_JUKEBOX. When the try limit is reached,
        the status of the last attempt is returned
        '''
        logger = LoggerState.logger_get(logger=kwargs.get('logger', None))
        callTries = kwargs.pop('callTries', nfs3_callTries_default)
        if not isinstance(callTries, int):
            raise TypeError("callTries excpected type int, got %s" % type(callTries).__name__)
        if callTries < 1:
            raise ValueError("illegal callTries %s" % callTries)
        in_xid = kwargs.get('xid', None)
        cur_xid = in_xid if in_xid is not None else self.get_new_xid()
        next_xid = cur_xid
        procname = rpcargs.procname

        kwargs['callTries'] = 1
        kwargs['logger'] = logger

        try_count = 0
        while try_count < callTries:
            try_count = try_count + 1
            kwargs['xid'] = cur_xid
            t0 = time.time()
            res = nfs3.NFS3_Client.make_call(self, proc, rpcargs_class, rpcargs, res_class, **kwargs)
            t1 = time.time()
            self.latencies.complete(procname, t0, t1, res.status, logger=logger)
            # If res.status is not nfsstat3 then this is an error.
            # The error is not at the NFS level, so we may retry with the same xid.
            if isinstance(res.status, nfs3.nfsstat3):
                if res.status == nfs3.nfsstat3_NFS3ERR_JUKEBOX:
                    if in_xid is not None:
                        # The caller asked for this xid explicitly, so do not retry.
                        return res
                    next_xid = self.get_new_xid()
                else:
                    # The call completed with an nfsstat3 result that is not JUKEBOX,
                    # so do not retry it here.
                    return res
            if try_count < callTries:
                logger.warning("%s call %s status=%s xid=%s/%s, will retry (%s/%s/%s)",
                               type(self).__name__, procname, res.status, cur_xid, res.xid, next_xid, try_count, callTries)
                cur_xid = next_xid
                time.sleep(0.25)
        # return the result of the most recent attempt
        if res.status is None:
            retstatus = "Unexpected_NoneStatus"
            logger.warning("%s call %s status is none, mapping to %s",
                           type(self).__name__, procname, retstatus)
            res.status = retstatus
        logger.warning("%s call %s status=%s xid=%s/%s, will not retry (%s/%s)",
                       type(self).__name__, procname, res.status, cur_xid, res.xid, try_count, callTries)
        return res

    def readdir_entire_dir(self, dirfh):
        '''
        Iteratively perform READDIR3 and return a tuple of (bool, complete_entry_list).
        The bool indicates success/failure.
        '''
        cookie = 0
        cookieverf = nfs3.cookieverf3(None)
        entries = []
        while True:
            rd_args = nfs3.READDIR3args(adir=dirfh, cookie=cookie, cookieverf=cookieverf)
            rd_res = self.rpc_readdir(rd_args)
            if rd_res.status != nfs3.nfsstat3_NFS3_OK:
                logger = LoggerState.logger_get(logger=logger)
                logger.error("readdir status=%s", rd_res.status)
                return False, []
            entry_list = rd_res.reply.entries
            entries.extend(entry_list)
            if rd_res.reply.eof:
                return True, entries
            last_entry = entry_list[len(entry_list)-1]
            cookie = last_entry.cookie
            cookieverf = rd_res.cookieverf

    def readdirplus_entire_dir(self, dirfh, logger=None):
        '''
        Iteratively perform READDIRPLUS3 and return a tuple of (bool, complete_entry_list).
        The bool indicates success/failure.
        '''
        cookie = 0
        cookieverf = nfs3.cookieverf3(None)
        entries = []
        while True:
            rdp_args = nfs3.READDIRPLUS3args(adir=dirfh, cookie=cookie, cookieverf=cookieverf)
            rdp_res = self.rpc_readdirplus(rdp_args)
            if rdp_res.status != nfs3.nfsstat3_NFS3_OK:
                logger = LoggerState.logger_get(logger=logger)
                logger.error("readdirplus status=%s", rdp_res.status)
                return False, []
            entry_list = rdp_res.reply.entries
            entries.extend(entry_list)
            if rdp_res.reply.eof:
                return True, entries
            last_entry = entry_list[len(entry_list)-1]
            cookie = last_entry.cookie
            cookieverf = rdp_res.cookieverf

    def entry_handle(self, dirfh, entry):
        '''
        Given entry as entry3 or entryplus3 from dirfh, return
        the handle for the entry. This may require
        further communication with the server.
        '''
        childfh = entry.get_handle()
        if childfh:
            return childfh
        args = nfs3.LOOKUP3args(adir=dirfh, aname=entry.get_name())
        res = self.rpc_lookup(args)
        return res.get_handle()

    def lookup_create(self, tag, parentdirfh, aname, prevfh=None, prevfh_str=None, do_create=False, logger=None):
        '''
        Lookup name in dir. If it does not exist, create it.
        If it exists, and prevfh is not None, expect the fh to match prevfh.
        Return (status, fh), where status=True is success; False is failure.
        If status=False, this operation logs the error details.
        tag: string for logging to identify what this FH is logically
        parentdirfh: parent dir
        aname: name of child
        prevfh: None or handle value to expect
        prevfhstr: log this for prevfh
        do_create: If the child does not exist and do_create, then mkdir the child
        '''
        logger = LoggerState.logger_get(logger=logger)
        prevfh_str = prevfh_str if prevfh_str is not None else str(prevfh)
        lookup_args = nfs3.LOOKUP3args(adir=parentdirfh, aname=aname)
        lookup_res = self.rpc_lookup(lookup_args)
        logger.debug("%s lookup args=%s res=%s", tag, lookup_args, lookup_res)
        handle = None
        if lookup_res.status == nfs3.nfsstat3_NFS3_OK:
            handle = lookup_res.get_handle()
            if (prevfh is not None) and (handle != prevfh):
                logger.error("%s %s handle changed from %s to %s", tag, aname, prevfh_str, handle)
                return False, None
            return True, handle
        if lookup_res.status == nfs3.nfsstat3_NFS3ERR_NOENT:
            if do_create:
                logger.info("%s directory %s does not exist- create it", tag, aname)
                dir_attrs = nfs3.sattr3(mode=0o777, uid=0, gid=0)
                mkdir_args = nfs3.MKDIR3args(adir=parentdirfh, aname=aname, attributes=dir_attrs)
                logger.debug("%s issue mkdir %s", tag, mkdir_args)
                mkdir_res = self.rpc_mkdir(mkdir_args)
                logger.debug("%s mkdir res=%s", tag, mkdir_res)
                if mkdir_res.status != nfs3.nfsstat3_NFS3_OK:
                    logger.error("%s mkdir failed %s", tag, mkdir_res)
                    return False, None
                if not mkdir_res.obj.handle_follows:
                    logger.debug("%s mkdir succeeded but no handle in response %s", tag, mkdir_res)
                    lookup_args = nfs3.LOOKUP3args(adir=parentdirfh, aname=aname)
                    lookup_res = self.rpc_lookup(lookup_args)
                    logger.debug("%s lookup args=%s res=%s", tag, lookup_args, lookup_res)
                    if lookup_res.status == nfs3.nfsstat3_NFS3_OK:
                        return True, lookup_res.get_handle()
                    logger.error("%s mkdir %s succeeded but did not return a handle, and the subsequent lookup failed with status %s",
                                 tag, aname, lookup_res.status)
                    return False, None
                return True, mkdir_res.obj.handle
            logger.info("%s directory %s does not exist", tag, aname)
            return False, None
        logger.error("%s lookup args=%s res.status=%s", tag, lookup_args, lookup_res.status)
        return False, None

    # Remove the entire subtree named remove_name in parent_dir_fh
    def remove_subtree(self, parent_dir_path, parent_dir_fh, remove_name, logger=None):
        '''
        Perform the logical equivalent of rm -r parent_dir_path/remove_name
        parent_dir_path: logical path to parent_dir_fh
        parent_dir_fh: dir in which to perform the remove
        remove_name: this name (and all children if this is a directory) is removed in parent_dir_fh
        '''
        logger = LoggerState.logger_get(logger=logger)
        ret = True
        if not isinstance(parent_dir_path, str):
            raise TypeError("remove_subtree requires str for parent_dir_path")
        if not isinstance(parent_dir_fh, nfs3.nfs_fh3):
            raise TypeError("remove_subtree requires nfs_fh3 for parent_dir_fh")
        if not isinstance(remove_name, str):
            raise TypeError("remove_subtree requires str for remove_name")
        remove_args = nfs3.REMOVE3args(dirfh=parent_dir_fh, aname=remove_name)
        remove_res = self.rpc_remove(remove_args)
        if remove_res.status == nfs3.nfsstat3_NFS3_OK:
            logger.debug("remove_subtree removed %s/%s", parent_dir_path, remove_name)
            return True
        if remove_res.status == nfs3.nfsstat3_NFS3ERR_NOENT:
            return True
        if remove_res.status != nfs3.nfsstat3_NFS3ERR_ISDIR:
            logger.warning("remove_subtree cannot remove %s/%s status=%s args=%s res=%s", parent_dir_path, remove_name, remove_res.status, remove_args, remove_res)
            return False
        lookup_args = nfs3.LOOKUP3args(adir=parent_dir_fh, aname=remove_name)
        lookup_res = self.rpc_lookup(lookup_args)
        if lookup_res.status == nfs3.nfsstat3_NFS3ERR_NOENT:
            # Lost a race. Consider this success - if there is a problem, then
            # either the final rmdir fails with NOTEMPTY some intermediate step does.
            return True
        if lookup_res.status != nfs3.nfsstat3_NFS3_OK:
            logger.debug("remove_subtree lookup %s/%s failed status=%s args=%s res=%s", parent_dir_path, remove_name, lookup_res.status, lookup_args, lookup_res)
            return False
        remove_fh = lookup_res.get_handle()
        rdp_ret, entries = self.readdirplus_entire_dir(remove_fh)
        if rdp_ret:
            for entry in entries:
                if entry.name in ('.', '..', '.snapshot'):
                    continue
                if self.remove_subtree("%s/%s" % (parent_dir_path, remove_name), remove_fh, entry.name):
                    logger.debug("remove_subtree removed %s/%s/%s", parent_dir_path, remove_name, entry.name)
                else:
                    # errors already logged
                    ret = False
        else:
            # Consider this success - if there is a problem, then
            # either the final rmdir fails with NOTEMPTY some intermediate step does.
            logger.warning("remove_subtree could not read dir path=%s/%s fh=%s", parent_dir_path, remove_name, remove_fh)
        rmdir_args = nfs3.RMDIR3args(dirfh=parent_dir_fh, aname=remove_name)
        rmdir_res = self.rpc_rmdir(rmdir_args)
        if rmdir_res.status == nfs3.nfsstat3_NFS3_OK:
            logger.debug("remove_subtree removed %s/%s", parent_dir_path, remove_name)
        else:
            logger.warning("remove_subtree parent_dir_fh=%s cannot rmdir %s/%s status=%s args=%s res=%s", parent_dir_fh, parent_dir_path, remove_name, rmdir_res.status, rmdir_args, rmdir_res)
            ret = False
        return ret

def resolve_path(mnt, cli, path, logger=None):
    '''
    Given a logical path starting from the root, use mnt and cli to resolve the path to a filehandle
    mnt: MOUNT3_Client
    cli: NFS3_Client
    returns a filehandle (nfs_fh3) or raises an exception
    '''
    return resolve_path_from(mnt, cli, None, path, logger=logger)

def resolve_path_from(mnt, cli, fh, path, logger=None):
    '''
    Given a logical path, use mnt and cli to resolve the path to a filehandle.
    mnt: MOUNT3_Client
    cli: NFS3_Client
    returns a filehandle (nfs_fh3) or raises an exception
    '''
    logger = LoggerState.logger_get(logger=logger)
    if not path:
        raise ValueError("path")
    if fh is None:
        if not path.startswith(os.path.sep):
            raise ValueError("cannot resolve relative path without filehandle")
        fh = MOUNT3_Client.get_rootfh_using_mnt(mnt, logger=logger)
        if fh is None:
            raise RuntimeError("cannot get root filehandle")
        if path == os.path.sep:
            return nfs3.nfs_fh3(data=fh)
        resolved = os.path.sep
        path_remain = path[len(resolved):]
    else:
        if path.startswith(os.path.sep):
            raise ValueError("attempt to resolve absolute path starting with a filehandle")
        resolved = ''
        path_remain = path
    comps = path_remain.split(os.path.sep)
    for comp in comps:
        if not comp:
            raise ValueError("invalid path")
        lookup_args = nfs3.LOOKUP3args(adir=fh, aname=comp)
        lookup_res = cli.rpc_lookup(lookup_args)
        if lookup_res.status != nfs3.nfsstat3_NFS3_OK:
            logger.debug("lookup_args=%s", lookup_args)
            logger.debug("lookup_res=%s", lookup_res)
            logger.error("resolve_path lookup %s in fh=%s path=%s status=%s", comp, fh, resolved, lookup_res.status)
            raise RuntimeError("cannot resolve path")
        fh = lookup_res.get_handle()
        if not resolved.endswith(os.path.sep):
            resolved += os.path.sep
        resolved += comp
        logger.debug("resolve_path resolved=%s fh=%s", resolved, fh)
    return fh

def exc_info_err(*args):
    '''
    Invoked from within an exception handler. Returns a human
    representation of the error.
    '''
    b = ' '.join(args)
    exc_info = sys.exc_info()
    err = getattr(exc_info[0], '__name__', '').split('.')[-1]
    if str(exc_info[1]):
        if err:
            err += " "
        err += str(exc_info[1])
    if b:
        return b + ': ' + err
    return err
