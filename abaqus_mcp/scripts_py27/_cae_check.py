# Py2.7 -- runs inside `abaqus cae noGUI`. Verifies the CAE/meshing license
# is reachable by building a trivial part and meshing it.
from abaqus import mdb
from abaqusConstants import THREE_D, DEFORMABLE_BODY, STANDALONE
import part
import mesh

m = mdb.Model(name='chk')
s = m.ConstrainedSketch(name='s', sheetSize=1.0)
s.rectangle(point1=(0.0, 0.0), point2=(1.0, 1.0))
p = m.Part(name='p', dimensionality=THREE_D, type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=s, depth=1.0)
p.seedPart(size=0.5)
p.generateMesh()
with open('cae_check_result.txt', 'w') as f:
    f.write('CAE_OK cells=%d elems=%d nodes=%d\n'
            % (len(p.cells), len(p.elements), len(p.nodes)))
