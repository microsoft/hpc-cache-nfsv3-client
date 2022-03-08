#!/usr/bin/env python3
#
# run_pylint.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''
Recursively scan for .py files starting in the current directory.
Read pylint_exclude.txt for files to exclude.
Assemble a command line and run pylint.

If no args are specified, look for all .py files in this subtree.
If the file pylint.exclude.txt exists, exclude those filenames
matching regexps in that file.

If filenames are specified on the command-line, that list of filenames
is used and no others. No exclusions are considered.

If no filenames are specified on the command-line, then files matching
any regexp provided to --exclude are excluded.

If any --exclude_from arguments are provided, additional exclusions
are loaded from those files, and the default exclusion file is
not considered.

Perform additional checks, such as ensuring that all .py files contain
proper copyright notices and headers.
'''

import argparse
import logging
import os
import platform
import pprint
import re
import stat
import subprocess
import sys

COPYRIGHT_INTERNAL = \
'''
# Copyright (c) Microsoft Corporation. All rights reserved.
#
'''.lstrip()
COPYRIGHT_INTERNAL_LINES = COPYRIGHT_INTERNAL.splitlines()

LOGGER_LEVEL = logging.INFO
LOGGER_NAME = 'run_pylint'

EXCLUDE_FROM_DEFAULT = 'pylint_exclude.txt'
PYLINTRC_DEFAULT = 'pylintrc'

def file_list_get():
    'Return a list of .py files starting with the current directory'
    files = list()
    list_files_in_dir('.', files)
    # Produce a deterministically-ordered and human-friendly result
    files.sort()
    return files

def list_files_in_dir(path, files):
    'Scan path for .py files and append them to files. Recursively check subdirs.'
    # Written to work with both Python2 and Python3
    for name in os.listdir(path):
        if name in ('.', '..'):
            continue
        nextpath = os.path.join(path, name) if path != '.' else name
        st = os.stat(nextpath)
        if stat.S_ISDIR(st.st_mode):
            list_files_in_dir(nextpath, files)
            continue
        if name.endswith('.py'):
            files.append(nextpath)

def load_exclude_file(path, exclude_regexps_set):
    'Load a list of regexps from path and add them to exclude_regexps'
    with open(path, 'r') as f:
        for line in f.readlines():
            line = line.strip()
            if (not line) or line.startswith('#'):
                continue
            exclude_regexps_set.add(line)

def is_excluded(filename, exclude_filenames):
    'Return whether filename matches a regexp in exclude_filenames'
    for reg in exclude_filenames:
        if reg.search(filename):
            return True
    return False

def logger_create(logger_level=LOGGER_LEVEL, logger_name=LOGGER_NAME):
    log_format = "%(asctime)s %(message)s"
    logging.basicConfig(format=log_format)
    logger = logging.getLogger(name=logger_name)
    logger.setLevel(logger_level)
    return logger

def find_pylint():
    'Search $PATH for pylint'
    env_path = os.environ.get('PATH', '')
    for path in env_path.split(':'):
        pylint = os.path.join(path, 'pylint')
        if os.path.isfile(pylint):
            return pylint
    return None

def main():
    logger = logger_create()
    pylint_default = find_pylint()
    parser = argparse.ArgumentParser()
    parser.add_argument("filenames", type=str, nargs='*', help="explicit list of filenames")
    parser.add_argument("--exclude", type=str, nargs='*', help="exclude files matching this regexp")
    parser.add_argument("--exclude_from", type=str, nargs='*', help="exclude files matching regexps in this file")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="number of concurrent processes")
    parser.add_argument("--rcfile", type=str, default=PYLINTRC_DEFAULT, help="pylintrc file")
    parser.add_argument("--pylint", type=str, default=pylint_default, help="pylint executable (default %s)" % pylint_default)
    pargs = parser.parse_args()
    try:
        main_with_logger(pargs, logger)
    except KeyboardInterrupt:
        logger.error("KeyboardInterrupt")
        sys.exit(1)

def main_with_logger(pargs, logger):
    pyfiles = file_list_get()
    if pargs.filenames:
        # Do not apply exclusions when given files on the command-line
        filenames = pargs.filenames
        excluded_count = 0
    else:
        # Figure out what we will exclude
        exclude_regexp_set = set()
        if pargs.exclude:
            exclude_regexp_set.update(pargs.exclude)
        if pargs.exclude_from:
            for exclude_file in pargs.exclude_from:
                logger.debug("load exclusions from %s", exclude_file)
                load_exclude_file(exclude_file, exclude_regexp_set)
        elif os.path.isfile(EXCLUDE_FROM_DEFAULT):
            load_exclude_file(EXCLUDE_FROM_DEFAULT, exclude_regexp_set)
        if exclude_regexp_set:
            logger.debug("exclude regexps:\n%s", pprint.pformat(exclude_regexp_set))
        exclude_regexps = [re.compile(x) for x in exclude_regexp_set if x]
        candidate_filenames = pyfiles
        # Perform exclusions
        filenames = [x for x in candidate_filenames if not is_excluded(x, exclude_regexps)]
        excluded_count = len(candidate_filenames) - len(filenames)
    if not filenames:
        logger.error("no files")
        sys.exit(1)

    if platform.system() == 'Windows':
        pythonpath_elem = ['.', 'lib'] + sys.path
        pythonpath = ';'.join(pythonpath_elem)
        logger.info("pythonpath: %s", pythonpath)
    else:
        pythonpath_elem = [os.path.abspath('.'), os.path.abspath('./lib')]
        pythonpath = ':'.join(pythonpath_elem)

    env = dict(os.environ)
    env['PYTHONPATH'] = pythonpath

    if pargs.pylint:
        pylint_bin = pargs.pylint
    else:
        logger.error("cannot find pylint")
        sys.exit(1)

    pylintrc = pargs.rcfile

    pylint_jobs = pargs.jobs

    info = "filecount=%d" % len(filenames)
    if excluded_count:
        info += " excluded=%d" % excluded_count
    logger.info("%s", info)
    del info
    logger.debug("pylint_bin: %s", pylint_bin)
    logger.debug("pylintrc: %s", pylintrc)
    logger.debug("pythonpath: %s", pythonpath)

    exit_status = 0

    cmd = [pylint_bin,
           '-j', pylint_jobs,
           '--rcfile', pylintrc,
           '--reports=n',
           '--score=n',
           #'--load-plugins', 'clfsload_pylint_plugin',
          ]
    cmd.extend(filenames)
    cmd = [str(x) for x in cmd]
    logger.debug("cmd: %s", cmd)
    ret = subprocess.call(cmd, env=env)
    if ret:
        logger.error("pylint error(s)")
        exit_status = 1
    else:
        logger.info("pylint good")

    if check_py_headers(logger, pyfiles):
        exit_status = 1

    sys.exit(exit_status)

def check_py_headers(logger, pyfiles):
    '''
    Verify expected headers on Python files
    Return nonzero if there are any problems
    '''
    ret = sum([check_py_file(logger, filename) for filename in pyfiles])
    logger.info("headers good on %d/%d files", len(pyfiles)-ret, len(pyfiles))
    return ret

def check_py_file(logger, filename):
    '''
    Verify the expected header on a Python file.
    Return 1 on error, 0 on success.
    '''
    state = 0
    fn_re = re.compile(r'^# ([a-zA-Z0-9_\/]+)\.py$')
    lineno = 0
    with open(filename, 'r') as f:
        expect_fn = filename
        if platform.system() == 'Windows':
            expect_fn = re.sub(r'\\', r'/', expect_fn)
        copyright_lines = COPYRIGHT_INTERNAL_LINES
        while True:
            a = f.readline()
            lineno += 1
            if not a:
                break
            if not a.startswith('#'):
                break
            a = a.rstrip()
            if (state == 0) and a.startswith('#!'):
                state = 1
                continue
            if (state < 2) and (a == '#'):
                state = 2
                continue
            if (state == 3) and (a == '#'):
                state = 4
                continue
            if state >= 4:
                csl = state - 4
                if a == copyright_lines[csl]:
                    if csl >= (len(copyright_lines) - 1):
                        return 0
                    state += 1
                    continue
                if state > 4:
                    logger.error("%s:%d: mismatched copyright line", filename, lineno)
                else:
                    logger.error("%s:%d: expected first copyright line", filename, lineno)
                return 1
            if (state == 5) and (a == '#'):
                return 0
            if state == 2:
                m = fn_re.search(a)
                if (not m) or (not m.group(1)):
                    break
                got_fn = m.group(1) + '.py'
                if got_fn != expect_fn:
                    logger.error("%s: header '%s' != '%s'", filename, got_fn, expect_fn)
                    return 1
                state = 3
                continue
            break
        logger.error("%s:%d: malformed header (state=%d)", filename, lineno, state)
        return 1

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("KeyboardInterrupt")
    sys.exit(1)
