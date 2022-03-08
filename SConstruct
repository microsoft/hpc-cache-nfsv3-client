#
# SConstruct
#
'''
SConstruct for hpc-cache-nfs3py
'''
import errno
import os
import platform
import shutil
import stat

import SCons.Tool.install

########################################
# Utilities

MODE_WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH

def InstallAsReadOnlyAction(target, source, env):
    '''
    Like InstallAs, but makes the target read-only
    '''
    for tgt, src in zip(target, source):
        try:
            os.makedirs(str(tgt.dir))
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        t = str(tgt)
        shutil.copy2(str(src), t)
        st = os.stat(t)
        m = stat.S_IMODE(st.st_mode)
        if m & MODE_WRITE:
            m &= ~MODE_WRITE
            os.chmod(t, m)
    return 0

########################################
# env setup

env = Environment(ARMADAHOME='usr/home/armada',
                  EXPORTROOT='export',
                  EXPORTUSR='$EXPORTROOT/$ARMADAHOME',
                 )
env['BUILDERS']['InstallAsReadOnly'] = env.Builder(action=env.Action(InstallAsReadOnlyAction), emitter=SCons.Tool.install.add_targets_to_INSTALLED_FILES)

########################################
# Core of the build

exportfiles = ('bin/nfs3_client_test.py',
               'bin/nfs3_fh_from_path.py',
               'bin/nfs3_path_from_fh.py',
               'lib/nfs3py/nfs3.py',
               'lib/nfs3py/nfs3_util.py',
               'lib/nfs3py/rpc_rfc1057.py',
              )

tarfile_name = 'hpc-cache-nfs3py.tar.gz'
tarfile_path = '$EXPORTROOT/' + tarfile_name

exportnodes = [env.InstallAsReadOnly("$EXPORTUSR/"+relpath, relpath) for relpath in exportfiles]
all = list(exportnodes)

tarcmd = "cd $EXPORTROOT && tar czf %s %s" % (tarfile_name, ' '.join(['$ARMADAHOME/'+f for f in exportfiles]))
all.append(env.Command(tarfile_path, exportnodes, tarcmd))

if platform.system() == 'Linux':
    md5cmd = "md5sum $SOURCE | awk '{ print $1; }' > $TARGET"
else:
    md5cmd = "/sbin/md5 -q $SOURCE > $TARGET"
all.append(env.Command(tarfile_path+'.md5', tarfile_path, md5cmd))

env.Alias('all', all)
env.Clean('all', '$EXPORTROOT')
env.Default('all')

