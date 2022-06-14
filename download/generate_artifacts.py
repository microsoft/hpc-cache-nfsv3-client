#!/usr/bin/env python
#
# generate_artifacts.py
#
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.
#
'''
This script knows how to generate artifacts for the Avere-nfs3py repo.
This may execute inside or outside a build pipeline.
'''

import argparse
import json
import logging
import os
import platform
import pprint
import re
import shlex
import shutil
import subprocess
import sys
import traceback

class Averenfs3pyArtifacts():
    '''
    Manage artifacts generation for the Avere-nfs3py repo
    '''
    def __init__(self, root, working_path, output_path, version=None, logger=None):
        self.root = root
        self.working_path = working_path
        self.working_path_build = os.path.join(self.working_path, 'build')
        self.output_path = output_path
        self.venv_path = os.path.join(self.working_path, 'venv_build')
        self.version = version or self.NFS3PY_VERSION_DEFAULT
        self.logger = logger or self.logger_create()
        self.python_exe = None
        self._linux_distro = False # no result cached
        self._linux_distro_exc = None
        self.pip_artifact = None # path as str
        self._exext = '.exe' if sys.executable.endswith('.exe') else ''
        self._bindir_name = 'Scripts' if platform.system() == 'Windows' else 'bin'

        if not re.search(r'^[0-9]+\.[0-9]+\.[0-9]+$', self.version):
            raise self.InstallError("invalid version specification '%s'" % self.version)

    NFS3PY_VERSION_DEFAULT = '1.0.0'

    LOGGER_NAME = 'generate_artifacts'
    LOGGER_LEVEL_DEFAULT = logging.DEBUG
    LOG_FORMAT = "%(asctime)s %(levelname).3s %(message)s"

    # Set PYLINT_VERSION to the empty string to always use the latest,
    # or to a version spec for something specific.
    # Here we pin 2.3.1 to avoid false negatives in 2.4.
    PYLINT_VERSION = '==2.3.1'

    # Updates to apply to the environment when executing
    # install commands.
    ENVIRON_EXTRA = {'PYTHONDONTWRITEBYTECODE' : '1',
                     'PYTHONUNBUFFERED' : '1',
                    }

    _PYTHON_MAJOR_PREFER = 3
    _PYTHON_MINOR_PREFER = 7

    def pip_exe(self, path=None):
        '''
        Path to pip for the virtualenv
        '''
        path = path or self.venv_path
        return shlex.quote(os.path.join(path, self._bindir_name, 'pip'+self._exext))

    class InstallError(Exception):
        '''
        Error occurred during installation
        '''
        # no specialization here

    class CannotDetermineLinuxDistroError(InstallError):
        '''
        During installation, cannot determine the Linux distro
        '''
        # no specialization here

    @property
    def linux_distro(self):
        '''
        Determine and return the Linux distro.
        Returns None if the distro cannot be determined
        '''
        if self._linux_distro is False: # is False check for uncached, vs None for cached unknown
            if platform.system() != 'Linux':
                self._linux_distro_exc = "system is '%s'" % platform.system()
                self._linux_distro = None
            else:
                try:
                    self._linux_distro = self._linux_distro_flavour()
                except self.CannotDetermineLinuxDistroError as e:
                    self._linux_distro_exc = e
                    self._linux_distro = None
        return self._linux_distro

    @classmethod
    def _linux_distro_flavour(cls):
        '''
        Return a best guess at the flavour of the Linux distro.
        Raises CannotDetermineLinuxDistroError if we are unable
        to figure it out.
        '''
        filename = '/etc/os-release'
        best_guess_val = None
        best_guess_pref = None
        key_prefs = {k : v for v, k in enumerate(('ID_LIKE',
                                                  'ID',
                                                  'NAME'))}
        try:
            with open(filename) as f:
                for line in f:
                    try:
                        line = line.strip()
                        if line.startswith('#'):
                            continue
                        tup = line.split('=', 1)
                        if len(tup) != 2:
                            continue
                        key, val = tup
                        if not (key and val):
                            continue
                        try:
                            pref = key_prefs[key]
                        except KeyError:
                            continue
                        if (best_guess_pref is None) or (pref < best_guess_pref):
                            best_guess_val = val
                            best_guess_pref = pref
                    except Exception:
                        continue
        except FileNotFoundError as e:
            raise cls.CannotDetermineLinuxDistroError("%s not found" % filename) from e
        if best_guess_val is None:
            raise cls.CannotDetermineLinuxDistroError("cannot determine Linux distro from %s" % filename)
        return best_guess_val.lower()

    def _install_cmd(self, cmd, add_path=None, add_pythonpath=None, timeout=1200):
        '''
        Run the given command. Raises InstallError on error.
        '''
        logger = self.logger
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        env = dict(os.environ)
        env.update(self.ENVIRON_EXTRA)
        if add_pythonpath:
            pp = env.get('PYTHONPATH', '')
            pp = pp.split(':') if pp else list()
            pp = add_pythonpath + pp
            env['PYTHONPATH'] = ':'.join(pp)
        if add_path:
            pp = env.get('PATH', '')
            pp = pp.split(':') if pp else list()
            pp = add_path + pp
            env['PATH'] = ':'.join(pp)
        cmdv = ' '.join(cmd)
        logger.info("run %s", cmdv)
        try:
            subprocess.run(cmd, stdin=subprocess.DEVNULL, check=True, env=env, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            ev = self._exc_describe(e)
            logger.error("cannot execute install command %s: %s", cmd, ev)
            raise self.InstallError("command '%s' failed: %s" % (cmdv, ev)) from e
        logger.debug("'%s' successful", cmdv)

    @staticmethod
    def _exc_describe(exc):
        '''
        Convert BaseException exc to a human-readable string
        '''
        err = type(exc).__name__
        es = str(exc).rstrip()
        if es:
            err += ' '
            err += es
        return err

    def _python_preferred_path(self):
        '''
        Return the path to the preferred Python version,
        or None if it is not found.
        '''
        # Are we it?
        if (sys.version_info.major == self._PYTHON_MAJOR_PREFER) and (sys.version_info.minor == self._PYTHON_MINOR_PREFER):
            return sys.executable
        if platform.system() == 'Windows':
            return sys.executable
        # Is it in our path?
        for interp in ("python%s.%s" % (self._PYTHON_MAJOR_PREFER, self._PYTHON_MINOR_PREFER),
                       "python%s" % self._PYTHON_MAJOR_PREFER,
                       "python"):
            for pathelem in os.environ.get('PATH', '').split(':'):
                interp_exe = os.path.abspath(os.path.join(pathelem, interp))
                cmd = (interp_exe,
                       '-c',
                       'import json; import sys; print(json.dumps({"major":sys.version_info.major, "minor":sys.version_info.minor, "executable":sys.executable})); sys.exit(0);',
                      )
                try:
                    p = subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
                    if p.returncode:
                        continue
                    output = str(p.stdout, encoding='ascii').strip()
                    d = json.loads(output)
                    if (d['major'] == self._PYTHON_MAJOR_PREFER) and (d['minor'] == self._PYTHON_MINOR_PREFER):
                        return d['executable']
                except Exception:
                    continue
        return None

    def _python_install_debian(self):
        '''
        Install Python on a debian-like distro.
        Raises InstallError on error.
        Returns abspath to the Python executable.
        '''
        logger = self.logger
        logger.info("install Python for debian")
        self._install_cmd("sudo apt-get update")
        python_exe = self._python_preferred_path()
        pv = "python%s.%s" % (self._PYTHON_MAJOR_PREFER, self._PYTHON_MINOR_PREFER)
        if python_exe:
            logger.info("found %s already installed for %s", python_exe, pv)
        else:
            self._install_cmd("sudo apt-get --assume-yes install %s" % pv)
            python_exe = self._python_preferred_path()
            if not python_exe:
                raise self.InstallError("%s not found after install" % pv)
        python_exe = os.path.abspath(python_exe)
        logger.debug("Python is %s", python_exe)
        #self._install_cmd("sudo apt-get --assume-yes install %s-dev" % pv)
        self._install_cmd("sudo apt-get --assume-yes install python%s-venv" % self._PYTHON_MAJOR_PREFER)
        return python_exe

    @classmethod
    def logger_create(cls, log_level=LOGGER_LEVEL_DEFAULT, name=LOGGER_NAME):
        '''
        Initialize logging and create and return a logger.
        '''
        log_format = cls.LOG_FORMAT
        logging.basicConfig(format=log_format, stream=sys.stdout)
        logger = logging.getLogger(name=name)
        logger.setLevel(log_level)
        return logger

    def run_pylint(self):
        '''
        Generate a virtualenv and use it to run pylint
        '''
        self.chdir_verbose(self.root)
        path = os.path.join(self.working_path, 'venv_pylint')
        self.setup_venv(path, install_pylint=True)
        pylint_exe = shlex.quote(os.path.join(path, self._bindir_name, 'pylint'+self._exext))
        self._install_cmd("%s run_pylint.py --pylint %s" % (self.python_exe, pylint_exe),
                          add_path=[os.path.join(path, self._bindir_name)])

    def dump_directory(self, path):
        '''
        Cheesy recursive walk. If this causes problems, just
        rewrite it to avoid the recursion. The recursive walk
        orders things nicely so I left it that way.
        '''
        logger = self.logger
        for ent in os.scandir(path):
            logger.debug("'%s' %s", ent.path, ent.stat())
            if ent.is_dir():
                self.dump_directory(ent.path)

    def chdir_verbose(self, path):
        '''
        Change working directory to path and print debugging info
        '''
        logger = self.logger
        os.chdir(path)
        logger.debug("wd is now %s", os.getcwd())

    def generate_artifacts(self):
        '''
        Generate artifacts and place them in output_path
        '''
        self.setup_output_path()
        self.setup_working_path()
        self.install_python()
        self.setup_venv(self.venv_path)
        self.generate_pip_artifact()
        self.test_pip_artifact()

    def generate_pip_artifact(self):
        '''
        Generate and save the installable package
        '''
        self.chdir_verbose(self.working_path_build)
        setup_py = os.path.join(self.working_path_build, 'setup.py')

        # Rewrite setup_py with the correct version for the package
        with open(setup_py, 'r') as f:
            setup_orig = f.read()
        setup_new = re.sub(r'NFS3PY_VERSION', self.version, setup_orig)
        with open(setup_py, 'w') as f:
            f.write(setup_new)

        # Run setup.py to generate the package
        self._install_cmd("%s -B %s sdist" % (self.python_exe, shlex.quote(setup_py)))
        nfs3py_tgz = "Avere-nfs3py-%s.tar.gz" % self.version
        src = os.path.join(self.working_path_build, 'dist', nfs3py_tgz)
        dst = os.path.join(self.output_path, nfs3py_tgz)
        shutil.copyfile(src, dst)
        self.pip_artifact = dst

    def test_pip_artifact(self):
        self.chdir_verbose(self.working_path_build)
        venv = os.path.join(self.working_path, 'venv_test')
        self.setup_venv(venv)
        self._install_cmd("%s install %s" % (self.pip_exe(venv), shlex.quote(self.pip_artifact)))

    def _setup_empty_path(self, path):
        '''
        Do what it takes to make path be an existing but empty directory
        '''
        logger = self.logger
        if os.path.isdir(path):
            logger.info("remove path %s", path)
            shutil.rmtree(path)
        elif os.path.exists(path):
            logger.info("remove %s", path)
            os.unlink(path)
        if os.path.exists(path):
            logger.error("%s still exists after remove", path)
            raise SystemExit(1)
        os.mkdir(path, mode=0o755)
        logger.debug("created %s", path)

    def setup_output_path(self):
        '''
        Create the empty output path
        '''
        self._setup_empty_path(self.output_path)

    def _srcdst(self, name):
        '''
        Return a tuple of src, dst where those are
        relative path name mapped from self.root into
        self.working_path_build
        '''
        return (os.path.join(self.root, name), os.path.join(self.working_path_build, name))

    def setup_working_path(self):
        '''
        Create and populate the working path
        '''
        logger = self.logger
        self._setup_empty_path(self.working_path)
        self._setup_empty_path(self.working_path_build)
        for src, dst in (self._srcdst('bin'),
                         (os.path.join(self.root, 'lib', 'avere'), os.path.join(self.working_path_build, 'avere')),
                        ):
            logger.info("copy %s to %s", src, dst)
            shutil.copytree(src, dst, symlinks=True)
        for src, dst in (self._srcdst('setup.py'),
                        ):
            logger.info("copy %s to %s", src, dst)
            shutil.copyfile(src, dst)

    def install_python(self):
        logger = self.logger
        if platform.system() == 'Windows':
            python_exe = shlex.quote(sys.executable)
            logger.info("skip install_python on '%s'; use '%s'", platform.system(), python_exe)
            self.python_exe = python_exe
            return
        linux_distro = self.linux_distro
        if not linux_distro:
            assert self._linux_distro_exc is not None
            raise self._linux_distro_exc
        if linux_distro in ('debian', 'ubuntu'):
            logger.info("perform debian install for %s", linux_distro)
            self.python_exe = self._python_install_debian()
        else:
            raise self.InstallError("unsupported Linux distro '%s'" % linux_distro)

    def setup_venv(self, path, install_pylint=False):
        '''
        Create a virtual environment used for installing and linting
        '''
        logger = self.logger
        self._setup_empty_path(path)
        logger.info("Create %s", path)
        self._install_cmd("%s -m venv %s" % (self.python_exe, shlex.quote(path)))
        pip_exe = self.pip_exe(path=path)
        exe_name = os.path.split(sys.executable)[1]
        self.python_exe = shlex.quote(os.path.join(path, self._bindir_name, exe_name))
        self._install_cmd("%s -m pip install --upgrade pip" % self.python_exe)
        self._install_cmd("%s --no-color install --upgrade setuptools" % pip_exe)
        self._install_cmd("%s --version" % pip_exe) # for the logs
        self._install_cmd("%s freeze --all" % pip_exe) # for the logs

        if install_pylint:
            pylint = 'pylint' + self.PYLINT_VERSION
            logger.info("install %s", pylint)
            self._install_cmd("%s install --no-color %s" % (pip_exe, pylint))
            self._install_cmd("%s freeze --all" % pip_exe) # for the logs

    @classmethod
    def main(cls, *args):
        '''
        Entry point from the command-line
        '''
        logger = cls.logger_create(logging.DEBUG)
        try:
            root = os.path.abspath(os.path.split(__file__)[0])
        except Exception:
            root = None
        ap_parser = argparse.ArgumentParser()
        ap_parser.add_argument("working_path", type=str,
                               help="path in which to build - this path is removed and recreated")
        ap_parser.add_argument("output_path", type=str,
                               help="path of directory in which outputs are placed - this path is removed and recreated")
        ap_parser.add_argument("--root", type=str, default=root,
                               help="root path for the input repo")
        ap_parser.add_argument("--version", type=str, default=cls.NFS3PY_VERSION_DEFAULT,
                               help="version to insert in setup.py")
        ap_args = ap_parser.parse_args(args=args)
        logger.info("%s version_info %s", __file__, sys.version_info)
        logger.info("%s platform.system '%s'", __file__, platform.system())
        root = ap_args.root
        try:
            repo_artifacts = cls(root, ap_args.working_path, ap_args.output_path, version=ap_args.version, logger=logger)
            repo_artifacts.generate_artifacts()
            logger.info("all artifacts generated")
            repo_artifacts.run_pylint()
            logger.info("pylint complete")
            raise SystemExit(0)
        except Exception as e:
            # Catch and explicitly format the exception to avoid
            # having the output mangled by the pipeline.
            logger.error("error from:\n%s", pprint.pformat(traceback.format_exc().splitlines()))
            logger.error("%s", repr(e))
        raise SystemExit(1)

if __name__ == '__main__':
    Averenfs3pyArtifacts.main(*sys.argv[1:])
    raise SystemExit(1)
