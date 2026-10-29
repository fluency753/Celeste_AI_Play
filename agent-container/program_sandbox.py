"""Shared zero-network program sandbox, installed unchanged in the agent image."""

PATH = '/opt/py/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
SHELL = '/bin/sh'
# Codex native Shell matches Claude's Bash tool (pipefail, source, [[ ... ]]).
BASH = '/bin/bash'
TIMEOUT = '/usr/bin/timeout'


def bwrap_args(env=None, *, cwd='/workspace', network_isolated=False):
    args = ['/usr/bin/bwrap', '--ro-bind', '/', '/', '--bind', '/workspace', '/workspace', '--bind', '/tmp', '/tmp',
            '--tmpfs', '/home', '--dev', '/dev', '--proc', '/proc',
            *([] if network_isolated else ['--unshare-net']),
            '--unshare-pid', '--unshare-ipc', '--unshare-uts',
            '--die-with-parent', '--new-session', '--clearenv',
            '--setenv', 'PATH', PATH, '--setenv', 'HOME', '/tmp',
            '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'LC_ALL', 'C.UTF-8',
            '--chdir', cwd]
    for key, value in (env or {}).items():
        args += ['--setenv', key, str(value)]
    return args
