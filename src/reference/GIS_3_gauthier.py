#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jul 17 15:54:01 2025

@author: gauthierferry
"""

import openstudio
import geopandas as gpd
import sys
import os
import shutil
import subprocess
from datetime import datetime

###Shapefile
f = gpd.read_file("/Users/gauthierferry/Documents/7. UPV/3. Rai/GIS_file/DANA_area.shp") #Path to change

#Number of floors
Floors = f['altura_max']
n_floors = int(Floors[0])
n_floors_max = int(max(Floors))

#Points of the Polygon
geom = f.geometry.iloc[0]
L_coordo = list(geom.exterior.coords)
x_rep, y_rep = L_coordo[0]
X = []
Y = []
for e in L_coordo:
    x, y = e
    x_new = x - x_rep
    y_new = y - y_rep
    X.append(x_new)
    Y.append(y_new)

###Create OS model
plantilla_path = openstudio.toPath("/Users/gauthierferry/Documents/7. UPV/PlantillaOS_v2.osm") #Path to change
translator = openstudio.osversion.VersionTranslator()
osm = translator.loadModel(plantilla_path).get()

###Library definition

#SpaceType
    
#BuildingStory & ThermalZone
for i in range(n_floors_max):
    floor_name = "Building Story " + str(i+1)
    th_zone_name = "Thermal Zone " + str(i+1)
    openstudio.model.BuildingStory(osm).setName(floor_name)
    openstudio.model.ThermalZone(osm).setName(th_zone_name)

#Storage into variables
for space_t in osm.getSpaceTypes():
    if space_t.nameString() == "Espacio Tipo Vivienda CTE":
        space_t_Esp_Viv_CTE = space_t
        break

#Schedules
for sch in osm.getScheduleRulesets():
    if sch.nameString() == "T refrigeracion vivienda CTE":
        sch_T_refrig_vivCTE = sch
    elif sch.nameString() == "T Calefaccion vivienda CTE":
        sch_T_calef_vivCTE = sch
        break
    
#ThermostatSetpoint.DualSetpoint
thermostat = openstudio.model.ThermostatSetpointDualSetpoint(osm)
thermostat.setName("Thermostat Setpoint Dual Viv CTE")
thermostat.setCoolingSetpointTemperatureSchedule(sch_T_refrig_vivCTE)
thermostat.setHeatingSetpointTemperatureSchedule(sch_T_calef_vivCTE)

for th in osm.getThermostatSetpointDualSetpoints():
    if th.nameString() == "Thermostat Setpoint Dual Viv CTE":
        thermostat_vivCTE = th
        break
    
###Geometry
for i in range(n_floors):
    P = []
    z = 3*i
    for i in range (len(X)-1):
        point = openstudio.openstudioutilitiesgeometry.Point3d(X[i], Y[i], z)
        P.append(point)
    openstudio.model.Space.fromFloorPrint(P, 3., osm)
    
spaces = osm.getSpaces()
th_zones = osm.getThermalZones()
b_stories = osm.getBuildingStorys()
    
###Assign Spacetype, BuildingStory, DefaultConstructionSet
for i, sp in enumerate(spaces): 
    sp.setSpaceType(space_t_Esp_Viv_CTE)
    sp.setBuildingStory(b_stories[i])
    #sp.setDefaultConstructionSet(default_cs_refzb)
    sp.setThermalZone(th_zones[i])
    
###Intersect & Match 
for i in range(len(spaces)-1):
    i = len(spaces) - i - 1
    spaces[i].intersectSurfaces(spaces[i-1])
    spaces[i].matchSurfaces(spaces[i-1])    
    
###Create heating & cooling device
for sp in spaces:
    th_z = sp.thermalZone()
    if th_z.is_initialized():
        th_z = th_z.get()
        th_z.setUseIdealAirLoads(True)
        th_z.setThermostatSetpointDualSetpoint(thermostat_vivCTE)
        
###Implement weather file
epw_path = openstudio.toPath("/Users/gauthierferry/Documents/7. UPV/ESP_Valencia.082840_IWEC.epw") #Path to change
wf = openstudio.EpwFile(epw_path)
openstudio.model.WeatherFile.setWeatherFile(osm, wf)

###Set output variables
out_var = openstudio.model.OutputVariable("Zone mean Air Temperature", osm)
out_var.setReportingFrequency("Hourly")

out_var = openstudio.model.OutputVariable("Zone  Thermostat heating Setpoint Temperature", osm)
out_var.setReportingFrequency("Hourly")

out_var = openstudio.model.OutputVariable("Zone Thermostat Cooling Setpoint Temperature", osm)
out_var.setReportingFrequency("Hourly")


osm.save("model_python.osm", True)


###Analysis on EnergyPlus

#Forward translate osm file to idf
ft = openstudio.energyplus.ForwardTranslator()
w = ft.translateModel(osm)
w.save(openstudio.path('model.idf'), True)

#Import modules
sys.path.insert(0, "/Applications/EnergyPlus-25-1-0/")
from pyenergyplus.api import EnergyPlusAPI
import pyenergyplus
pyenergyplus.api.EnergyPlusAPI.api_version()

# Path definition (all of the paths to change)
IDF_FILE = '/Users/gauthierferry/Documents/7. UPV/model.idf'
WEATHER_FILE = '/Users/gauthierferry/Documents/7. UPV/3. Rai/3. Weather files/ESP_Valencia.082840_IWEC.epw'
OUTPUT_DIR = '/Users/gauthierferry/Documents/7. UPV/EnergyPlus_results'
energyplus_dir = '/Applications/EnergyPlus-25-1-0'

# Create output directory if it doesn't exist
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Create a temporary working directory
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
working_dir = f'/Users/gauthierferry/Documents/7. UPV/temp_run_{timestamp}' #To change
os.makedirs(working_dir, exist_ok=True)

# Copy IDF file
idf_temp_path = os.path.join(working_dir, 'in.idf')
shutil.copy(IDF_FILE, idf_temp_path)

# === Call ExpandObjects ===
expand_exe = os.path.join(energyplus_dir, 'ExpandObjects')
subprocess.run([expand_exe], cwd=working_dir)

expanded_idf = os.path.join(working_dir, 'expanded.idf')
if not os.path.exists(expanded_idf):
    raise FileNotFoundError("ExpandObjects a échoué : fichier 'expanded.idf' introuvable.")


# === RUN ENERGYPLUS ===

# Initialize API
api = EnergyPlusAPI()
state = api.state_manager.new_state()

# Run EnergyPlus
exit_code = api.runtime.run_energyplus(state, [
    '--weather', WEATHER_FILE,
    '--output-directory', OUTPUT_DIR,
    '--readvars',
    expanded_idf
])

# === PRINT RESULT ===
if exit_code == 0:
    print("Simulation completed successfully!")
else:
    print("Simulation failed with exit code {exit_code}")
    
    


















