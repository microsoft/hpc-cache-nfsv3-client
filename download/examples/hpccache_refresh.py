#!/usr/bin/env python3
#
# bin/refresh_file.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.
#
'''
Refresh (Force check attributes) for file/dir in cache and drop modified
data from cache

Usage:
======
export JUNCTION <export mounted by client, namespace_path>
export CACHE_IP <IP addr used by clients to mount HPC Cache>
export OBJECT_LIST <Path to file with list of objects to be refreshed>

Usage options:
python3 ./refresh_file.py --help

Refresh a number of files listed in a file:
 python3 ./refresh_file.py ${JUNCTION} ${CACHE_IP} $(OBJECT_LIST}
'''

import argparse
import logging
import sys
import time

from hpccache_client import HPCCacheClient

def process_object_list(server, export, objlist):
    '''
    Read list of object paths one by one and for refresh the object
    '''
    failed = 0
    start = time.monotonic()
    logging.info("SETUP")
    hpccache = HPCCacheClient(server, export)

    try:
        hpccache.mount()
    except ValueError as err:
        logging.critical("FAILED: %s", format(err))
        return -1

    with open(objlist) as file:
        for line in file:
            path = line.rstrip()
            logging.info("REFRESH: %s", path)
            try:
                handle = hpccache.path_lookup(path)
                hpccache.refresh(handle)
            except ValueError as err:
                logging.critical("FAILED: %s - %s", path, format(err))
                failed += 1

    logging.info("CLEANUP")
    hpccache.umount()
    end = time.monotonic()

    logging.info ("Elapsed time: %s", str.format("{0:.6f}", (end - start)))
    if failed:
        logging.error("Refresh failed for objects: %s", failed)

    return failed


def parse_args():
    '''
    Parse Arguments
    '''
    parser = argparse.ArgumentParser(description='Object refresh Utility')
    parser.add_argument('export', type=str, help='Export, e.g. /1_1_1_0')
    parser.add_argument('server', type=str, help='Server IP addr')
    parser.add_argument('list', type=str, 
                        help='File with list of objects to be refreshed')
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(format='%(asctime)s: %(message)s', level=logging.INFO)

    args = parse_args()
    if process_object_list(args.server, args.export, args.list):
        sys.exit(-1)
