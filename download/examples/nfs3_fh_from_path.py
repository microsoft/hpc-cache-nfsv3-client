#!/usr/bin/env python
#
# bin/nfs3_fh_from_path.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''
Convert a path to a filehandle
'''
import argparse
import sys

import hpccache.nfs3py.nfs3_util as nfs3_util

class LoggerState(nfs3_util.LoggerState):
    LOGGER_NAME_DEFAULT = __file__

def main(*args):
    ap_parser = argparse.ArgumentParser()
    ap_parser.add_argument('addr', type=str,
                           help='target address')
    ap_parser.add_argument('path', type=str,
                           help='path to map')
    ap_args = ap_parser.parse_args(args=args)
    logger = LoggerState.logger_get()
    LoggerState.logger_set(logger) # push it up the chain
    mnt = nfs3_util.MOUNT3_Client(ap_args.addr, reserved=True, logger=logger)
    cli = nfs3_util.NFS3_Client(ap_args.addr, logger=logger)
    print(nfs3_util.resolve_path(mnt, cli, ap_args.path, logger=logger))
    raise SystemExit(0)

if __name__ == "__main__":
    main(*sys.argv[1:])
    raise SystemExit(1)
