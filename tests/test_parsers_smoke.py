"""Smoke test: run the parsers against the real validation-cube outputs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.parsers.sta import parse_sta
from abaqus_mcp.parsers.msg import parse_msg
from abaqus_mcp.parsers.dat import parse_dat

# Checked-in outputs from a real solved job, so this test works on a fresh
# clone. (They used to be read out of the gitignored runs/ dir.)
run = Path(__file__).resolve().parent / "fixtures" / "validate_cube"

sta = parse_sta(run / "cube_static.sta")
print("STA status:", sta.status.value)
print("STA increments:", len(sta.increments))
if sta.last_increment:
    li = sta.last_increment
    print("  last inc: step=%d inc=%d total_time=%.3f" % (li.step, li.inc, li.total_time))
print("STA final_line:", sta.final_line)

msg = parse_msg(run / "cube_static.msg")
print("\nMSG completed:", msg.completed)
print("MSG error/warning counts:", msg.num_error_messages, "/", msg.num_warning_messages)
print("MSG diagnostics:", len(msg.diagnostics))
for d in msg.diagnostics:
    print("  [%s/%s] node=%s dof=%s: %s" % (d.kind, d.category, d.node, d.dof, d.text[:70]))

dat = parse_dat(run / "cube_static.dat")
print("\nDAT diagnostics:", len(dat.diagnostics))
for d in dat.errors:
    print("  ERROR [%s]: %s" % (d.category, d.text[:70]))

assert sta.status.value == "completed", "expected completed"
assert msg.completed is True
print("\nOK: parsers agree the validation job completed successfully.")
