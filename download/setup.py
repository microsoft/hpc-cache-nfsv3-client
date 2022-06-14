#!/usr/bin/env python
#
# setup.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.
#
'''
setuptools script to generate an installable module for Avere-nfs3py
'''

import logging
import os
import re
import sys

try:
    import setuptools
except ImportError:
    # When bootstrapping via generate_artifacts.py, setuptools is not found
    # but we do not care.
    pass

NAME = 'Avere-nfs3py'

BINFILES = (os.path.join('bin', 'nfs3_client_test.py'),
            os.path.join('bin', 'nfs3_fh_from_path.py'),
            os.path.join('bin', 'nfs3_path_from_fh.py'),
           )

# Files from lib/ to include in the .tgz artifact
LIBFILES = (os.path.join('avere', 'nfs3py', 'nfs3.py'),
            os.path.join('avere', 'nfs3py', 'nfs3_util.py'),
            os.path.join('avere', 'nfs3py', 'rpc_rfc1057.py'),
           )

DESC_SHORT = 'Avere NFS3 Python client'
DESC_LONG = "The %s is compliant with RFC 1813" % DESC_SHORT

requirements = []

_RE_SEP = re.compile('\\' + os.path.sep)

def _libfile_to_module(x):
    '''
    Given a libfile like 'a/b.py', convert it to
    a Python module name like 'a.b'.
    '''
    if x.endswith('.py'):
        x = x[:-3]
    return '.'.join(x.split(os.path.sep))

def main(*args):
    logging.basicConfig(level=logging.WARNING)

    try:
        os.chdir(os.path.abspath(os.path.split(__file__)[0]))
    except Exception:
        pass

    py_modules = [_libfile_to_module(x) for x in LIBFILES]

    setuptools.setup(author='Microsoft Corporation',
                     classifiers=['Development Status :: 5 - Production/Stable',
                                  'Environment :: Console',
                                  'Operating System :: POSIX :: Linux',
                                  'Programming Language :: Python :: 3',
                                 ],
                     description=DESC_SHORT,
                     install_requires=requirements,
                     keywords='Avere Microsoft',
                     long_description=DESC_LONG,
                     name=NAME,
                     # Leave out the specification so that we can use
                     # pip with a "wrong" Python when building armada. Sigh.
                     #python_requires='~=3.7',
                     version='NFS3PY_VERSION',
                     zip_safe=False,
                     scripts=BINFILES,
                     py_modules=py_modules,
                    )
    raise SystemExit(0)

if __name__ == '__main__':
    main(*sys.argv[1:])
    raise SystemExit(1)
