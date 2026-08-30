# Py2.7 / abaqus cae noGUI -- generate a simple L-bracket STEP file to use as a
# CAD-import test fixture. Usage: abaqus cae noGUI=_make_test_step.py -- out.step
import sys
from abaqus import mdb
from abaqusConstants import THREE_D, DEFORMABLE_BODY

argv = sys.argv
out = argv[-1] if argv and argv[-1].lower().endswith('.step') else 'bracket.step'

m = mdb.Model(name='gen')
s = m.ConstrainedSketch(name='s', sheetSize=200.0)
# L-shaped profile in the XY plane.
s.Line(point1=(0.0, 0.0), point2=(60.0, 0.0))
s.Line(point1=(60.0, 0.0), point2=(60.0, 20.0))
s.Line(point1=(60.0, 20.0), point2=(20.0, 20.0))
s.Line(point1=(20.0, 20.0), point2=(20.0, 60.0))
s.Line(point1=(20.0, 60.0), point2=(0.0, 60.0))
s.Line(point1=(0.0, 60.0), point2=(0.0, 0.0))
p = m.Part(name='BRACKET', dimensionality=THREE_D, type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=s, depth=10.0)

mdb.saveAs(pathName='gen_model')
# Export STEP.
session_ok = True
try:
    import step  # noqa
except Exception:
    pass
p.writeStepFile(fileName=out)
with open('make_step_result.txt', 'w') as f:
    f.write('STEP_OK %s cells=%d faces=%d\n' % (out, len(p.cells), len(p.faces)))
