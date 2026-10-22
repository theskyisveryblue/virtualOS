"""Import the controller implemented in the existing worker shell file.

The shell file is the canonical, trusted repository program. Mutable queue
state is data and is never evaluated as code.
"""
from pathlib import Path

_worker = Path(__file__).with_name('cloud-disk-archive.sh')
_program = _worker.read_text()
_begin = "# virtualos-agent-control:begin\n: <<'VIRTUALOS_AGENT_CONTROL_PY'\n"
_end = "\nVIRTUALOS_AGENT_CONTROL_PY\n# virtualos-agent-control:end\n"
if _program.count(_begin) != 1 or _program.count(_end) != 1:
    raise ImportError('worker controller source markers must be unique')
_source = _program.split(_begin, 1)[1].split(_end, 1)[0]
exec(compile(_source, str(_worker), 'exec'), globals())
