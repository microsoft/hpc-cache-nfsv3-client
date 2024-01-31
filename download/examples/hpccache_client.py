#!/usr/bin/env python3
#
# bin/hpccache_client.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.
#
'''
Developer Notes:
================
An extension of NFS3 client to perform hpccache special ops
'''

import sys

from hpccache.nfs3py import nfs3
from hpccache.nfs3py import nfs3_util

class RefreshFile3Args(nfs3.READ3args):
    '''
    Class representing special arguments required for refreshing
    a file with READ RPC
    '''
    def __init__(self, handle):
        super().__init__(handle, offset=0, count=0)


class RefreshDirPlus3Args(nfs3.READDIRPLUS3args):
    '''
    Class representing special arguments required for refreshing
    a file with READDIRPLUS RPC
    '''
    def __init__(self, handle):
        super().__init__(handle, cookie=0xFFFFFFFFFFFFFFFF,
                         maxcount=0, dircount=0)


class HPCCacheClient:
    '''
    Class representing client to an HPC Cache
    '''
    def __init__(self, server_ip, export):
        self._server_ip = server_ip
        self._export = export
        self._mount3_client = None
        self._nfs3_client = None
        self._root = None


    def umount(self):
        '''
        "Unmounts" the cache
        '''
        if self._mount3_client:
            self._mount3_client.rpc_umntall()

        self._root = None


    def mount(self):
        '''
        "Mounts" the cache export and gets the root file handle
        '''
        self._mount3_client = nfs3_util.MOUNT3_Client(self._server_ip)

        null_res = self._mount3_client.rpc_null()
        if null_res.status != 0:
            raise ValueError(f"MOUNT NULL RPC - {null_res.status}")

        mount_args = nfs3.MOUNT3_MNTargs(adirpath=self._export)
        mount_res = self._mount3_client.rpc_mnt(mount_args)
        if mount_res.status != nfs3.mountstat3_MNT3_OK:
            raise ValueError(f"MOUNT - {mount_res.status}")

        self._nfs3_client = nfs3_util.NFS3_Client(self._server_ip)
        null_res = self._nfs3_client.rpc_null()
        if null_res.status != nfs3.nfsstat3_NFS3_OK:
            raise ValueError(f"CLIENT NULL RPC - {null_res.status}")

        self._root = mount_res.fhandle


    def lookup(self, dirhandle, entryname):
        '''
        Returns a handle for object under directory

        Args:
            dirhandle: Parent directory handle
            entryname: Object name to lookup under directory
        '''
        lookup_args = nfs3.LOOKUP3args(adir=dirhandle, aname=entryname)
        lookup_res = self._nfs3_client.rpc_lookup(lookup_args)
        if lookup_res.status != nfs3.nfsstat3_NFS3_OK:
            raise ValueError(f"LOOKUP - {lookup_res.status}")

        return lookup_res.get_handle()


    def path_lookup(self, path):
        '''
        Perform lookup and return handle for a path

        Args:
            path: Pathname for lookup
        '''

        # We only deal with absolute paths for now, must start with /
        if not path.startswith('/'):
            raise ValueError("Not absolute path")

        handle = self._root
        path_parts = path.split('/')
        for part in path_parts[1:]:
            handle = self.lookup(handle, part)

        return handle


    def getattr(self, handle):
        '''
        Returns attributes for an object

        Args:
            handle: Object handle
        '''
        getattr_args = nfs3.GETATTR3args(aobject=handle)
        getattr_res = self._nfs3_client.rpc_getattr(getattr_args)
        if getattr_res.status != nfs3.nfsstat3_NFS3_OK:
            raise ValueError("GETATTR - {getattr_res.status}")

        return getattr_res.obj_attributes


    def _refresh_dirplus(self, handle):
        '''
        Force refresh a directory object referred to by handle

        Args:
            handle: Directory object handle
        '''
        refresh_args = RefreshDirPlus3Args(handle)
        refresh_res = self._nfs3_client.rpc_readdirplus(refresh_args)
        if refresh_res.status != nfs3.nfsstat3_NFS3ERR_TOOSMALL:
            raise ValueError("REFRESH_DIRPLUS - {refresh_res.status}")


    def _refresh_file(self, handle):
        '''
        Force refresh a file object referred to by handle

        Args:
            handle: File object handle
        '''
        refresh_args = RefreshFile3Args(handle)
        refresh_res = self._nfs3_client.rpc_read(refresh_args)
        if refresh_res.status != nfs3.nfsstat3_NFS3_OK:
            raise ValueError("REFRESH_FILE - {refresh_res.status}")


    def refresh(self, handle):
        '''
        Force refresh an object referred to by handle

        Args:
            handle: Object handle
        '''
        attr = self.getattr(handle)

        if attr.ftype == nfs3.ftype3_NF3DIR:
            self._refresh_dirplus(handle)
        elif attr.ftype == nfs3.ftype3_NF3REG:
            self._refresh_file(handle)
        else:
            pass


if __name__ == "__main__":
    sys.exit(0)
