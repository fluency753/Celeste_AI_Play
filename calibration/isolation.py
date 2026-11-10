"""Keep tests out of the repository's real agent workspaces and generated KB views.

Usage at module level of a test file:
    from calibration.isolation import generated_dirs_fixture
    setUpModule, tearDownModule = generated_dirs_fixture()
"""
from pathlib import Path
import tempfile
from unittest.mock import patch


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
