"""Keep tests out of the repository's real agent workspaces and generated KB views.

Usage at module level of a test file:
    from calibration.isolation import generated_dirs_fixture
    setUpModule, tearDownModule = generated_dirs_fixture()
"""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def unlink_links(root):
    """Remove every symlink and Windows junction under root, without following any of them.

    Register with addCleanup after the temporary directory's own cleanup (cleanups run last
    first). Python 3.14 on Windows cannot finish deleting a directory once a link in it has lost
    its target earlier in the same rmtree; removing links while targets still exist avoids that.
    """
    links = []
    for directory, dirnames, filenames in os.walk(root):     # Links found here are never entered.
        for name in list(dirnames):
            if _is_link(Path(directory) / name):
                links.append(Path(directory) / name)
                dirnames.remove(name)
        links += [Path(directory) / name for name in filenames if _is_link(Path(directory) / name)]
    for link in links:
        try:
            os.unlink(link)    # Directory links are removed as links on Windows too.
        except FileNotFoundError:
            pass


def generated_dirs_fixture():
    state = {}

    def setUp():
        from orchestrator import config
        from orchestrator.config import RunConfig
        temp = tempfile.TemporaryDirectory(prefix='celeste-test-')
        root = Path(temp.name)
        workspace, view = RunConfig.workspace_dir.fget, RunConfig.kb_dir.fget

        def moved(path, part):
            base = config.REPO_ROOT / part
            return root / part / path.relative_to(base) if path.is_relative_to(base) else path

        patchers = [patch.object(RunConfig, 'workspace_dir', property(lambda cfg: moved(workspace(cfg), 'workspaces'))),
                    patch.object(RunConfig, 'kb_dir', property(lambda cfg: moved(view(cfg), 'local/kb_views')))]
        for patcher in patchers:
            patcher.start()
        state.update(temp=temp, patchers=patchers)

    def tearDown():
        for patcher in reversed(state.pop('patchers', [])):
            patcher.stop()
        if temp := state.pop('temp', None):
            temp.cleanup()

    return setUp, tearDown
