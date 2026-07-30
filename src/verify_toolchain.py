"""Valencia Energy Sim - toolchain doğrulama.
OpenStudio SDK + EnergyPlus (pyenergyplus) + analiz/GIS yığınının çalıştığını kanıtlar.
Çalıştır:  .venv/bin/python src/verify_toolchain.py
"""
import sys

import openstudio
print("OpenStudio SDK :", openstudio.openStudioVersion())

# 10x8 m taban, 3 m yükseklik. fromFloorPrint için taban SAAT YÖNÜNDE olmalı.
m = openstudio.model.Model()
pts = [openstudio.Point3d(0, 0, 0), openstudio.Point3d(0, 8, 0),
       openstudio.Point3d(10, 8, 0), openstudio.Point3d(10, 0, 0)]
sp = openstudio.model.Space.fromFloorPrint(pts, 3.0, m)
print("Space.fromFloorPrint:", "OK" if sp.is_initialized() else "FAIL")

ft = openstudio.energyplus.ForwardTranslator()
w = ft.translateModel(m)
print("ForwardTranslator .idf nesne sayisi:", len(w.objects()))

sys.path.insert(0, "/Applications/OpenStudio-3.11.0/EnergyPlus")
from pyenergyplus.api import EnergyPlusAPI
print("pyenergyplus API :", EnergyPlusAPI().api_version())

import pandas, numpy, scipy, geopandas, matplotlib
print("Analiz/GIS      : pandas", pandas.__version__, "| geopandas", geopandas.__version__,
      "| numpy", numpy.__version__, "| scipy", scipy.__version__)

print(">>> TOOLCHAIN OK <<<")
