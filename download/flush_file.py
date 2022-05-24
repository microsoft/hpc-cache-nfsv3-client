#!/usr/bin/env python3
#
# bin/flush_file.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''
Flush a dirty file to backend core filer
'''

'''
Usage:
======

export JUNCTION <export mounted by client, namespace_path>
export CACHE_IP <IP addr used by clients to mount HPC Cache>

Usage options:
python3 ./flush_file.py --help

Asynchronously flush a number of files listed in a file:
 cat flushlist | python3 ./flush_file.py ${JUNCTION} ${CACHE_IP}

Synchronously flush a number of files listed in a file:
 cat flushlist | python3 ./flush_file.py ${JUNCTION} ${CACHE_IP} --sync

Developer Notes:
================

We can flush a file using its full path by issuing an NFS COMMIT op with
special offset and count magic fields in the call request.

1) Synchronously flush a file, causes the calling thread to wait until
    the file at provided path is flushed to the backend core filer or
    an error is encountered:

        offset=0x1234ABCDDEADDEAD count=0xABADBEEF
        Returns: 0 (NFS3_OK) on success, other error codes on failure

2) Asynchronously flush a file, causes the client thread to request a
    large file to be flushed without blocking the caller.

        offset=0x1234ABCDDEADDEAD count=0xADEADBE6
        Returns: 10002 (nfsstat3_NFS3ERR_NOT_SYNC) if async flush started
                 0 (NFS3_OK) if file is already flushed

3) Asynchronous flush status, allows a caller to check the current flush
    status of a file that is/was being flushed asynchronously.

        offset=0x1234ABCDDEADDEAD count=0xADEADBE5
        Returns: 0 (NFS3_OK) if the file has been successfully flushed.
                 66 (NFS3ERR_NOTEMPTY) if the flush failed and the
                        attributes remain dirty.

Notes:
* Commit requests are always forwarded to the HPC Cache address that has the dirty
    attributes or file, irrespective of which HPC Cache address it arrives on from client.
* Error NOT_SYNC is when it's still flushing (or started flushing),
    NOTEMPTY is when no outstanding flushing is going but attrs remain dirty.
    Seeing the returned mtime is also a good hint if NOTEMPTY is due to
    recent writes, etc. The point is these commit related flushes aren't
    fully error-recoverable-state like cfs-cleaner's errorQ is where in cases
    like ACCESS or NOSPC errs we'll keep failing till the issue is fixed
    (in the mass often).
* If the NFS3ERR_NOTEMPTY is encountered due to new write activity or due
    to temporary backend core filer issues, they may be worked around by
    adding additional retries instead of breaking out. Alternatively,
    grep for ERROR and create a list of files that failed to flush and
    run flush_file.py to try and flush them again.
* If flush-commit-sync/async comes in and already is running it'll return
    NOT_SYNC; obviously if one comes and the attrs are clean, say it was file
    was flushed via cleaner earlier (or wasn't dirty in the first place)
    it'll return early with success.
'''

import argparse
import logging
import queue
import threading
import sys
import time

import avere.nfs3py.nfs3 as nfs3
import avere.nfs3py.nfs3_util as nfs3_util

RECHECK_SECONDS=0.25

DEFAULT_NUMTHREADS=4
DEFAULT_PER_FILE_TIMEOUT=300

MAGIC_OFFSET=0x1234ABCDDEADDEAD
SYNC_FLUSH_COUNT=0xABADBEEF
ASYNC_FLUSH_COUNT=0xADEADBE6
FLUSH_STATUS_COUNT=0xADEADBE5

error_count = 0

class FileFlushClient:
    def __init__(self, server_ip, export, output_lock=None):
        self.server_ip = server_ip
        self.export = export
        self.output_lock = output_lock
        self.mount3_client = None
        self.nfs3_client = None
        self.root = None
        self.init_nfs3_client(self.server_ip, self.export)
        # If we get here, we successfully created a client and mounted fs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.mount3_client is not None:
            logging.info("Cleanup: Unmounting the file system")
            self.mount3_client.rpc_umntall()
        self.root = None

    def incr_err_count(self):
        global error_count
        if self.output_lock:
            with self.output_lock:
                error_count += 1
        else:
            error_count += 1

    def init_nfs3_client(self, server_ip, export):
        self.mount3_client = nfs3_util.MOUNT3_Client(server_ip)
        null_res = self.mount3_client.rpc_null()
        mount_args = nfs3.MOUNT3_MNTargs(adirpath=export)
        mount_res = self.mount3_client.rpc_mnt(mount_args)
        if mount_res.status != nfs3.mountstat3_MNT3_OK:
            raise ValueError("Failed mount")
        self.nfs3_client = nfs3_util.NFS3_Client(server_ip)
        null_res = self.nfs3_client.rpc_null()
        if null_res.status != nfs3.nfsstat3_NFS3_OK:
            raise ValueError("Failed creating nfs3 client")
        self.root =  mount_res.fhandle

    def lookup(self, dirhandle, entryname):
        end_time = time.monotonic() + DEFAULT_PER_FILE_TIMEOUT
        while (True):
            if time.monotonic() > end_time:
                logging.info (f"ERROR: Lookup timeout {entryname}")
                return None
            lookup_args = nfs3.LOOKUP3args(adir=dirhandle, aname=entryname)
            lookup_res = self.nfs3_client.rpc_lookup(lookup_args)
            if lookup_res.status != nfs3.nfsstat3_NFS3_OK:
                logging.info(f"ERROR: Failed to lookup {entryname}")
                return None
            return lookup_res.get_handle()

    def get_handle_for_path(self, filepath):
        # We only deal with absolute paths for now, must start with /
        if not filepath.startswith('/'):
            logging.info (f"ERROR: Absolute path needed for: {filepath}")
            return None
        path_parts = filepath.split('/')
        handle = self.root
        for part in path_parts[1:]:
            subhandle = self.lookup(handle, part)
            if subhandle is None:
                logging.info (f"ERROR: failed lookup {filepath}")
                return None
            handle = subhandle
        return handle

    def sync_commit(self, testfile_handle, timeout=120.0):
        commit_args = nfs3.COMMIT3args(afile=testfile_handle,
                                       offset=MAGIC_OFFSET,
                                       count=SYNC_FLUSH_COUNT)
        commit_res = self.nfs3_client.rpc_commit(commit_args,
                                                 timeoutRel=timeout)
        return commit_res

    def async_commit(self, testfile_handle):
        commit_args = nfs3.COMMIT3args(afile=testfile_handle,
                                       offset=MAGIC_OFFSET,
                                       count=ASYNC_FLUSH_COUNT)
        commit_res = self.nfs3_client.rpc_commit(commit_args,
                                                 timeoutRel=120.0)
        return commit_res.status

    def check_commit_status(self, testfile_handle):
        commit_args = nfs3.COMMIT3args(afile=testfile_handle,
                                       offset=MAGIC_OFFSET,
                                       count=FLUSH_STATUS_COUNT)
        commit_res = self.nfs3_client.rpc_commit(commit_args, timeoutRel=120.0)
        return commit_res

    def commit_and_wait(self, testfile_handle, timeout=0):
        commit_status = self.async_commit(testfile_handle)
        if not (commit_status == nfs3.nfsstat3_NFS3ERR_NOT_SYNC
                or commit_status == nfs3.nfsstat3_NFS3_OK):
            logging.info (f"ERROR: Async commit returned {commit_status}")
            self.incr_err_count()
            return
        return self.wait_for_flush_completion(testfile_handle, timeout)

    def wait_for_flush_completion(self, testfile_handle, timeout=0):
        endTime = None
        if timeout != 0:
            endtime = time.monotonic() + timeout

        while(True):
            if timeout != 0:
                if time.monotonic() > endtime:
                    logging.info (f"ERROR: Timed out flushing handle={testfile_handle}")
                    break
            commit_res = self.check_commit_status(testfile_handle)
            commit_status = commit_res.status
            if (commit_status == nfs3.nfsstat3_NFS3_OK):
                logging.info (f"Flush completed for handle={testfile_handle}")
                return commit_res
            if (commit_status == nfs3.nfsstat3_NFS3ERR_NOT_SYNC):
                logging.debug (f"Flush in-progress for handle={testfile_handle}")
                time.sleep(RECHECK_SECONDS)
                continue
            if (commit_status == nfs3.nfsstat3_NFS3ERR_NOTEMPTY):
                logging.info (f"ERROR: No flush in progress,"
                              f" but attrs dirty for handle={testfile_handle}")
                self.incr_err_count()
                break
            else:
                logging.info (f"ERROR: {commit_status} for handle={testfile_handle}")
                self.incr_err_count()
                break
        return None

    def commit_file_and_wait(self, filepath, thread_id=0, timeout=0):
        handle = self.get_handle_for_path(filepath)
        if handle is None:
            self.incr_err_count()
            logging.info (f"Thread={thread_id:02d} ERROR failed lookup {filepath}")
            return
        start = time.monotonic()
        logging.info (f"Thread={thread_id:02d} Flushing {filepath} handle={handle}")
        commit_res = self.commit_and_wait(handle, timeout)
        if commit_res is None:
            # error count is incremented by commit_and_wait() call, if needed
            logging.info (f"Thread={thread_id:02d} ERROR flushing {filepath}"
                          f" in {time.monotonic() - start:.6f}sec")
        else:
            logging.info (f"Thread={thread_id:02d} Flushed {filepath}"
                          f" in {time.monotonic() - start:.6f}sec"
                          f" mtime={commit_res.file_wcc.after.attributes.mtime}")

    def commit_synchronously(self, filepath, thread_id=0, timeout=0):
        handle = self.get_handle_for_path(filepath)
        if handle is None:
            self.incr_err_count()
            logging.info (f"Thread={thread_id:02d} ERROR failed lookup {filepath}")
            return
        start = time.monotonic()
        logging.info (f"Thread={thread_id:02d} Flushing {filepath} handle={handle}")
        commit_res = self.sync_commit (handle, timeout)
        commit_status = commit_res.status
        if not (commit_status == nfs3.nfsstat3_NFS3_OK):
            self.incr_err_count()
            logging.info (f"Thread={thread_id:02d} ERROR flushing {filepath}"
                          f" in {time.monotonic() - start:.6f}sec")
        else:
            logging.info (f"Thread={thread_id:02d} Flushed {filepath}"
                          f" in {time.monotonic() - start:.6f}sec"
                          f" mtime={commit_res.file_wcc.after.attributes.mtime}")

def parse_args():
    parser = argparse.ArgumentParser(description='File Flush Utility')
    parser.add_argument('export', type=str, help='Export, e.g. /1_1_1_0')
    parser.add_argument('server', type=str, help='Server IP addr')
    parser.add_argument('--threads', type=int, default=DEFAULT_NUMTHREADS,
                        help="Number of concurrent flush threads")
    parser.add_argument('--timeout', type=int, default=DEFAULT_PER_FILE_TIMEOUT,
                        help="Per file flush timeout (sec)")
    parser.add_argument('--sync', action='store_true',
                        help="Flush each file synchronously")
    parser.add_argument('--verbose', action='store_true',
                        help="Log every flush-in-progress check")

    return parser.parse_args()

def worker(args, thread_id, q, output_lock):
    with FileFlushClient(args.server, args.export, output_lock) as flush_client:
        while True:
            filepath = q.get()
            if args.sync:
                flush_client.commit_synchronously(filepath,
                                                  thread_id=thread_id,
                                                  timeout=args.timeout)
            else:
                flush_client.commit_file_and_wait(filepath,
                                                  thread_id=thread_id,
                                                  timeout=args.timeout)
            q.task_done()

def process_files_parallel(args):
    global error_count
    q = queue.Queue()
    output_lock = threading.Lock()
    # Kick off the workers
    start = time.monotonic()
    for i in range(args.threads):
        threading.Thread(target=worker, args=(args, i, q, output_lock), daemon=True).start()
    for line in sys.stdin:
        filepath = line.strip()
        q.put(filepath)
    q.join()
    logging.info (f"Total elapsed time {time.monotonic() - start:.06f}")
    if error_count != 0:
        logging.info (f"{error_count} ERRORS encountered."
                      f" grep ERROR in output for details.")

if __name__ == "__main__":
    args = parse_args()
    loglevel=logging.INFO
    if args.verbose:
        loglevel=logging.DEBUG
    logging.basicConfig(format='%(asctime)s: %(message)s', level=loglevel)
    process_files_parallel(args)
    if error_count != 0:
        sys.exit(-1)
