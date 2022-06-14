#!/usr/bin/env python
#
# bin/nfs3_path_from_fh.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.
#
'''
Convert a filehandle to a path
'''
import argparse
import os
import sys

import hpccache.nfs3py.nfs3 as nfs3
import hpccache.nfs3py.nfs3_util as nfs3_util

exc_info_err = nfs3_util.exc_info_err

class LoggerState(nfs3_util.LoggerState):
    LOGGER_NAME_DEFAULT = __file__

class ResolveState():
    def __init__(self, addr, fh, logger=None):
        self.logger = LoggerState.logger_get(logger=logger)
        self.addr = addr
        self.fh = fh
        self.seen = {fh}
        self.path = ''
        self.done = False
        self.success = False
        self.mnt = nfs3_util.MOUNT3_Client(self.addr, reserved=True)
        self.cli = nfs3_util.NFS3_Client(self.addr)
        self.exports = None
        self._load_exports()

    def _load_exports(self):
        '''
        Build self.exports as a dict that maps filehandles to exportHpcCache
        '''
        logger = self.logger
        self.exports = dict()
        exportres = self.mnt.rpc_export(logger=logger)
        if exportres:
            logger.warning("cannot fetch exports from %s result=%s", self.addr, exportres)
        else:
            for export in exportres.exports:
                mntargs = nfs3.MOUNT3_MNTargs(adirpath=export.ex_dir)
                mntres = self.mnt.rpc_mnt(mntargs, logger=logger)
                if mntres:
                    logger.warning("cannot resolve export=%s args=%s res=%s", export, mntargs, mntres)
                    continue
                self.exports[nfs3.nfs_fh3(data=mntres.fhandle)] = export

    def resolve(self):
        while not self.done:
            self._resolve_one()

    def _resolve_one(self):
        '''
        Resolve the next parent
        '''
        logger = self.logger
        exportHpcCache = self.exports.get(self.fh, None)
        if exportHpcCache is not None:
            self.path = exportHpcCache.ex_dir.to_string() + self.path
            self.done = True
            self.success = True
            return
        lookupargs = nfs3.LOOKUP3args(adir=self.fh, aname='..')
        lookupres = self.cli.rpc_lookup(lookupargs, logger=logger)
        if lookupres:
            logger.warning("cannot lookup args=%s res=%s", lookupargs, lookupres)
            self.done = True
            return
        pfh = lookupres.get_handle()
        if not pfh:
            logger.warning("no handle in lookup result args=%s res=%s", lookupargs, lookupres)
            self.done = True
            return
        if pfh in self.seen:
            logger.warning("encountered %s more than once", pfh)
            self.done = True
            return
        self.seen.add(pfh)
        success, entries = self.cli.readdirplus_entire_dir(pfh, logger=logger)
        if not success:
            logger.warning("cannot scan directory %s", pfh)
            self.done = True
            return
        for entry in entries:
            efh = self.cli.entry_handle(pfh, entry)
            if not efh:
                logger.warning("no handle for directory=%s entry=%s", pfh, entry)
                continue
            if efh == self.fh:
                if self.path:
                    self.path = os.path.sep + entry.get_name().to_string() + os.path.sep + self.path
                else:
                    self.path = entry.get_name().to_string()
                self.fh = pfh
                return
        logger.warning("did not find %s in %s", self.fh, pfh)
        self.done = True

def main(*args):
    ap_parser = argparse.ArgumentParser()
    ap_parser.add_argument('addr', type=str,
                           help='target address')
    ap_parser.add_argument('filehandle', type=str,
                           help='filehandle to reverse map')
    ap_args = ap_parser.parse_args(args=args)
    logger = LoggerState.logger_get()
    LoggerState.logger_set(logger) # push it up the chain

    addr = ap_args.addr

    try:
        fh_data = bytes.fromhex(ap_args.filehandle)
    except ValueError:
        logger.error("cannot parse filehandle '%s': %s", ap_args.filehandle, exc_info_err())
        raise SystemExit(1)
    fh = nfs3.nfs_fh3(data=fh_data)

    resolver = ResolveState(addr, fh, logger=logger)
    resolver.resolve()
    if resolver.success:
        print(resolver.path)
        raise SystemExit(0)
    logger.error("resolve did not succeed")
    if resolver.path:
        print("...%s" % resolver.path)
    raise SystemExit(1)

if __name__ == "__main__":
    main(*sys.argv[1:])
    raise SystemExit(1)
