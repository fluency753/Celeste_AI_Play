"""Host import of the sandbox rules also installed in the agent image."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_source = Path(__file__).resolve().parents[1] / 'agent-container' / 'program_sandbox.py'
_spec = spec_from_file_location('_celeste_program_sandbox', _source)
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
PATH = _module.PATH
SHELL = _module.SHELL
TIMEOUT = _module.TIMEOUT
bwrap_args = _module.bwrap_args
