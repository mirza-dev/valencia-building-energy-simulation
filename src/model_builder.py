from datetime import datetime 
import calendar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
import openstudio
import pandas as pd
from shapely.geometry import LineString 
from shapely.geometry.polygon import orient

from model_config import BuildConfig, OUTPUT_VARIABLES as CONFIG_OUTPUT_VARIABLES
from select_building import TARGET_BARRIO 

def _project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if(parent / "data" / "gis").exists():
            return parent
    raise FileNotFoundError("Project root couldn't found: This project should have a 'data/gis' folder in its root directory." 
                            "Is there this script inside the ~/valencia-energy-sim?")

PROJECT = _project_root()

DEFAULT_BUILD_CONFIG = BuildConfig.for_project(PROJECT)

BUILDING_GPKG = DEFAULT_BUILD_CONFIG.data.building_path
BUILDINGS_GPKG = BUILDING_GPKG
NEIGHBORS_SHP = DEFAULT_BUILD_CONFIG.data.neighbor_path
TEMPLATE_OSM = DEFAULT_BUILD_CONFIG.data.template_path
EPW_FILE = DEFAULT_BUILD_CONFIG.data.epw_path
OUT_DIR = DEFAULT_BUILD_CONFIG.data.output_root

FLOOR_H = DEFAULT_BUILD_CONFIG.geometry.floor_height_m
GROUND_UNCONDITIONED = DEFAULT_BUILD_CONFIG.geometry.ground_unconditioned
WWR_CARDINAL = DEFAULT_BUILD_CONFIG.openings.cardinal_wwr()
WINDOW_U = DEFAULT_BUILD_CONFIG.envelope.window_u
WINDOW_SHGC = DEFAULT_BUILD_CONFIG.envelope.window_g

WIN_W, WIN_H, WIN_SILL = 1.2, 1.2, 0.9
DOOR_W, DOOR_H, DOOR_SILL = 1.2, 2.1, 0.01
BALCONY_DOORS_PER_FLOOR = 2
BALCONY_DEPTH= 1.0
 
SHADE_BLIND_NAME= "Lamas Horizontales 25mm cada 20mm"
SHADE_SETPOINT= 250.0

CONTEXT_RADIUS = DEFAULT_BUILD_CONFIG.geometry.context_radius_m

DEFAULT_PARAMS = DEFAULT_BUILD_CONFIG.to_legacy_params()

PARTY_WALL_TOL = 0.3 

# Derived, never restated: this constant sat at a literal (50.0, 5000.0) while
# the gate in prepare_footprint() read the config, so raising the ceiling would
# have left a stale pair here for the next reader to believe.
FOOTPRINT_RANGE = (DEFAULT_BUILD_CONFIG.geometry.footprint_min_m2,
                   DEFAULT_BUILD_CONFIG.geometry.footprint_max_m2)


OUTPUT_VARIABLES = dict(CONFIG_OUTPUT_VARIABLES)


@dataclass
class BuildResult:
    """Complete result used by the workbench while legacy callers keep tuples."""

    osm: Any
    stats: dict[str, Any]
    config: BuildConfig
    warnings: list[str] = field(default_factory=list)
    geometry_actions: list[dict[str, Any]] = field(default_factory=list)

# ============================================================================
# 1) READING DATA + VERIFICATION PRE-FLIGHT 
# ============================================================================

def validate_input_files(config: BuildConfig | None = None):
    """Verify that the required input files exist."""
    cfg = config or DEFAULT_BUILD_CONFIG
    files= {
        "Building GeoPackage": cfg.data.building_path,
        "Neighbors Shapefile": cfg.data.neighbor_path,
        "OpenStudio template": cfg.data.template_path,
        "Epw weather file": cfg.data.epw_path,
    }
    missing = [f" - {ad}: {p}" for ad,p in files.items() if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing input files:\n" + "\n".join(missing))
    
def validate_building_row(row):
    """Check that the building row has the required fields."""

    ref = row.get("refparcela")
    if ref is None or str(ref).strip() == "":
        raise ValueError("Building row is missing 'refparcela' field or it is empty.")
    if row.geometry is None or row.geometry.is_empty:
        raise ValueError(f"{ref}: Building row has no geometry.")
    h = row.get("altura_max")
    if h is None or pd.isna(h):
        raise ValueError(f"{ref}: Building row has no 'altura_max' field or it is NaN.")
    if float(h) != int(float(h)) or not (1 <= int(float(h)) <= 100):
        raise ValueError(f"{ref}: Building row 'altura_max' field must be an integer between 1 and 100, got {h}.")

def load_buildings(gpkg_path: Path) -> gpd.GeoDataFrame:
    """Load buildings from a GeoPackage and validate them."""
    gdf = gpd.read_file(gpkg_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 25830:
        raise ValueError(f"It was expecting CRS EPSG28830 but has came {gdf.crs}.")
    for col in ("refparcela", "altura_max"):
        if col not in gdf.columns:
            raise ValueError(f"{gpkg_path.name}: Missing required column '{col}'."
                             f"Existing columns: {list(gdf.columns)[:12]}...")
    print(f"Loaded {len(gdf)} building from {gpkg_path.name}.")
    return gdf 

def clean_polygon(geom):
    """Clean and orient a polygon geometry."""
    if geom.geom_type == "MultiPolygon":
        if len(geom.geoms) != 1:
            raise ValueError(f"Not supported MultiPolygon with {len(geom.geoms)} parts.")
        geom = geom.geoms[0]
    if geom.geom_type != "Polygon":
        raise ValueError(f"Expected Polygon but got {geom.geom_type}.")
    if not geom.is_valid:
        raise ValueError("Geometric is not valid. - Requaring to 'Fix geometry' in QCIS.")
    if len(geom.interiors) > 0:
        raise ValueError(f"Polygon has {len(geom.interiors)} interior rings (holes), which is not supported.")
    return geom

def prepare_footprint(geom, config: BuildConfig | None = None):
    """Prepare the building footprint by cleaning and orienting it."""
    cfg = config or DEFAULT_BUILD_CONFIG
    g = cfg.geometry
    simp = geom.simplify(g.simplify_tolerance_m, preserve_topology=True)
    area_change = abs(simp.area - geom.area) / geom.area
    if area_change > g.max_area_delta_fraction:
        raise ValueError(
            f"Geometry simplification changed area by {area_change:.2%}, which is more than "
            f"{g.max_area_delta_fraction:.2%}.")
    footprint_range = (g.footprint_min_m2, g.footprint_max_m2)
    if not (footprint_range[0] <= simp.area <= footprint_range[1]):
        raise ValueError(f"Footprint area {simp.area:0.0f} m² is outside the expected range {footprint_range}.")
    cw = orient(simp, sign=-1.0)
    coords = list(cw.exterior.coords)[:-1]
    if len(coords) < 3:
        raise ValueError(f"Footprint has only {len(coords)} vertices, which is less than the minimum of 3.")
    return coords, cw.area

# ============================================================================
# 2) NEIGHBORS - DEDICTION OF PARTY WALL + COMMON READING FOR SHADOW MASS
# ============================================================================

def load_neighbors(geom, refparcela: str, neighbors_shp: Path,
                   config: BuildConfig | None = None) -> gpd.GeoDataFrame:
    """Load neighboring buildings within a specified radius."""
    cfg = config or DEFAULT_BUILD_CONFIG
    minx, miny, maxx, maxy = geom.bounds
    r = cfg.geometry.context_radius_m
    nb = gpd.read_file(neighbors_shp, bbox=(minx - r, miny - r, maxx + r, maxy + r ))
    keep = []
    for idx, row in nb.iterrows():
        if row["refparcela"] == refparcela or row.geometry is None:
            continue
        if row.geometry.equals(geom):
            print(f"Neighbor warning: {row['refparcela']} it was ignored!")
            continue
        if row.geometry.distance(geom) > r:
            continue
        keep.append(idx)
    out = nb.loc[keep]
    print(f"[neighbor] {len(out)} has readen from neighbor build ({r:.0f} m radius).")
    return out 

def find_party_walls(geom, refparcela: str, neighbors_shp: Path, neighbors=None,
                     config: BuildConfig | None = None):
    """BinanÄ±n komÅularÄ±yla paylaÅtÄ±ÄÄ± kenarlarÄ± bul."""
    cfg = config or DEFAULT_BUILD_CONFIG
    if neighbors is None:
        neighbors = load_neighbors(geom, refparcela, neighbors_shp, config=cfg)
    shared = []
    for _, r in neighbors.iterrows():
        inter = geom.exterior.intersection(r.geometry)
        if inter.length > cfg.geometry.min_shared_edge_m:
            shared.append(inter)
            print(f"[party] neighbor {r['refparcela']}: shared edge {inter.length:.2f} m")
    if not shared:
        print("[party] no shared edge - detached building.")
        return None
    out = shared[0]
    for s in shared[1:]:
        out = out.union(s)
    return out
    
# ============================================================================    
# 3) WINDOW RATIO — WWR BY ORIENTATION
# ============================================================================

def wwr_for_azimuth(az_deg: float, config: BuildConfig | None = None) -> float:
    """Rotate the WWR according to the compass angle of the front."""
    cardinals = (config or DEFAULT_BUILD_CONFIG).openings.cardinal_wwr()
    az = az_deg % 360.0
    lo = int(az// 90) * 90  #sub cardinal (0/90/180/270)
    hi = (lo + 90) % 360    #upper cardinal (cycle: 270->0)
    frac = (az-lo) / 90.0   #where is it between two cardinal? (0...1)
    return cardinals[lo] + (cardinals[hi] - cardinals[lo]) * frac

# ============================================================================
# 4) SETUP THE MODEL — FOOTPRINT → OPENSTUDIO 3D MODEL
# ============================================================================

def load_template(config: BuildConfig | None = None) -> openstudio.model.Model:
    template_path = (config or DEFAULT_BUILD_CONFIG).data.template_path
    tr = openstudio.osversion.VersionTranslator()
    opt = tr.loadModel(openstudio.toPath(str(template_path)))
    if opt.isNull():
        raise RuntimeError(f"Template could not dowloaded: {template_path}")
    return opt.get()

def _wall_plan_segment(surface, x0: float, y0: float) -> LineString:

    pts = list({(round(v.x() + x0, 3), round(v.y() + y0, 3)) for v in surface.vertices()})
    if len(pts) < 2:
        raise ValueError(f"Wall plan projection could not be generated: {surface.nameString()}")
    best = max(((a, b) for i, a in enumerate(pts) for b in pts[i + 1:]),
               key=lambda ab: (ab[0][0] - ab[1][0]) ** 2 + (ab[0][1] - ab[1][1]) ** 2)
    return LineString(best)

def _massless_construction(osm, name: str, u_value: float, surface_film_r: float):
    """Generate a single-layer 'massless' construction from the target U-value (for the LHS)."""
    r_layer = max(1.0 / u_value - surface_film_r, 0.05)
    mat = openstudio.model.MasslessOpaqueMaterial(osm,"Rough", r_layer)
    mat.setName(f"{name} material (R={r_layer:.3f})" )
    c = openstudio.model.Construction(osm)
    c.setName(name)
    c.insertLayer(0, mat)
    return c

def _layer_resistance(mat) -> float | None:
    """Thermal resistance of a layer of opaque material [m²K/W]"""
    m = mat.to_StandardOpaqueMaterial()
    if not m.isNull():
        m = m.get()
        return m.thickness() / m.thermalConductivity()
    m = mat.to_MasslessOpaqueMaterial()
    if not m.isNull():
        return m.get().thermalResistance()
    m = mat.to_AirGap()
    if not m.isNull():
        return m.get().thermalResistance()
    return None

def _construction_with_delta_u(osm, base, delta_u: float, surface_film_r: float, name: str):
    layers = base.layers()
    rs= [_layer_resistance(m) for m in layers]
    if any( r is None for r in rs):
        raise RuntimeError(f"'Unrecognized layer type in {base.nameString()} ΔU could not be applied.'")
    r_nofilm = sum(rs)
    u_old = 1.0 / (r_nofilm + surface_film_r)
    r_target_nofilm = 1.0 / (u_old + delta_u) - surface_film_r
    dr = r_nofilm - r_target_nofilm
    idx = max(range(len(rs)), key=lambda i: rs[i])
    r_new = rs[idx] - dr
    if r_new <= 0.01:
        raise RuntimeError(f"ΔU={delta_u} is so big: '{base.nameString()}' layer."
                           f"R={rs[idx]:.3f} -> {r_new:.3f} it was be (that not physical).")
    new_layers = openstudio.model.MaterialVector()
    for i, mat in enumerate(layers):
        clone = mat.clone(osm).to_Material().get()
        if i ==idx:
            std = clone.to_StandardOpaqueMaterial()
            if not std.isNull():
                std = std.get()
                std.setThermalConductivity(std.thickness() / r_new)
                std.setName(f"{std.nameString()} + TB(dU={delta_u:.2f})")
            else:
                ml = clone.to_MasslessOpaqueMaterial()
                (ml.get() if not ml.isNull() else clone.to_AirGap().get()).setThermalResistance(r_new)
        new_layers.append(clone)
    c = openstudio.model.Construction(osm)
    c.setName(name)
    c.setLayers(new_layers)
    return c 

def _clone_template_material(osm, name: str):
    for m in osm.getStandardOpaqueMaterials():
        if m.nameString() == name:
            return m.clone(osm).to_StandardOpaqueMaterial().get()
    for a in osm.getAirGaps():
        if a.nameString() == name:
            return a.clone(osm).to_AirGap().get()
    raise RuntimeError(f"There is no material in the template: {name}")

def _mat_r(m) -> float:
    r= _layer_resistance(m)
    if r is None:
        raise RuntimeError(f"Undefined layer type: {m.nameString()}")
    return r

def _assemble_calibrated(osm, name: str, target_u: float, film_r: float, fixed_layers: list,
                         calib, insert_at: int):
    r_fixed = sum(_mat_r(m) for m in fixed_layers)
    r_need = 1.0 /target_u - film_r - r_fixed
    if r_need <= 0.005:
        raise RuntimeError(f"'{name}': Target U={target_u} could not construct this layer set."
                           f"(R_need = {r_need:.3f} <= 0) - Regime threshold is incorrect.")
    calib.setThickness(r_need * calib.thermalConductivity())
    calib.setName(f"{calib.nameString()} (Calibre {calib.thickness()*1000:.0f}mm)")
    ordered = list(fixed_layers)
    ordered.insert(insert_at, calib)
    layers = openstudio.model.MaterialVector()
    for m in ordered:
        layers.append(m)
    c = openstudio.model.Construction(osm)
    c.setName(name)
    c.setLayers(layers)
    return c

def _build_layered_wall(osm, target_u: float, film_r: float = 0.17):
    mortero = _clone_template_material(osm, "Mortero de cemento referencia")
    yeso = _clone_template_material(osm,"Enlucido de yeso d < 1000_15mm")
    if target_u >= 1.5:
        calib = _clone_template_material(osm, "Ladrillo Perforado Referencia")
        return _assemble_calibrated(osm, f"Muro macizo ladrillo (U_base={target_u:.2f})", 
                                    target_u, film_r, [mortero, yeso], calib, insert_at=1)
    perforado = _clone_template_material(osm, "Ladrillo Perforado Referencia")
    camara = _clone_template_material(osm, "Camara de aire en paredes R 0.18")
    if target_u >= 1.0:
        calib = _clone_template_material(osm, "Ladrillo Hueco Referencia")
        return _assemble_calibrated(osm, f"Muro IVE ladrillo (U_base={target_u:.2f})", target_u, film_r, [mortero, perforado, camara, yeso], calib, insert_at=3)
    hueco = _clone_template_material(osm, "Ladrillo Doble Hueco Referencia")
    calib = _clone_template_material(osm, "Aislante Medianera Referencia B")
    return _assemble_calibrated(osm, f"Muro ladrillo aislado (U_base={target_u:.2f})", target_u, film_r, [mortero, perforado, camara, hueco, yeso], calib, insert_at=3)

def _build_layered_roof(osm, target_u: float, film_r: float = 0.14):
    mortero = _clone_template_material(
        osm, "Mortero de cemento o cal para albañileria y para revoco/enlucido 1600 < d < 1800_2cm")
    yeso = _clone_template_material(osm, "Enlucido de yeso d < 1000_15mm")
    if target_u >= 3.5:                           
        calib = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(
            osm, f"Cubierta forjado desnudo (U={target_u:.2f})", target_u, film_r,
            [mortero, yeso], calib, insert_at=1)
    arena   = _clone_template_material(osm, "Arena y grava [1700 < d < 2200]  6 cm")
    asfalto = _clone_template_material(osm, "Asfalto 10mm")
    if target_u >= 1.0:                               
        calib = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(
            osm, f"Cubierta plana ladrillo (U={target_u:.2f})", target_u, film_r,
            [arena, asfalto, mortero, yeso], calib, insert_at=3)
    fu = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
    calib = _clone_template_material(osm, "Aislante Medianera Referencia B")
    return _assemble_calibrated(
        osm, f"Cubierta plana aislada (U={target_u:.2f})", target_u, film_r,
        [arena, asfalto, mortero, fu, yeso], calib, insert_at=3)

def _build_ive_brick_wall(osm, target_u: float = 1.33, film_r: float = 0.17):
    return _build_layered_wall(osm, target_u, film_r)

def _add_context_shading(osm, neighbors, x0: float, y0: float,
                         config: BuildConfig | None = None) -> int:
    from shapely.ops import unary_union

    cfg = config or DEFAULT_BUILD_CONFIG

    group = openstudio.model.ShadingSurfaceGroup(osm)
    group.setName("Neighbor shadow masses")
    group.setShadingSurfaceType("Site")


    by_height = {}
    n_skip = 0
    for _, r in neighbors.iterrows():
        h_raw = r.get("altura_max")
        if h_raw is None or pd.isna(h_raw) or int(float(h_raw)) < 1:
            n_skip += 1
            continue
        n_lev = int(float(h_raw)) + (1 if cfg.geometry.neighbor_assume_ground else 0)
        by_height.setdefault(n_lev * cfg.geometry.floor_height_m, []).append(r.geometry)

    n_srf = 0 
    for h, geoms in by_height.items():
        merged = unary_union(geoms)
        parts = merged.geoms if merged.geom_type == "MultiPolygon" else [merged]
        for part in parts: 
            if part.geom_type != "Polygon":
                continue
            ring = list(part.simplify(0.3, preserve_topology=True).exterior.coords)
            for (axp, ayp), (bxp, byp) in zip(ring[:-1], ring[1:]):
                p = openstudio.Point3dVector()
                for x, y, z in ((axp, ayp,h), (axp, ayp, 0.0),
                              (bxp, byp, 0.0), (bxp, byp, h)):
                    p.append(openstudio.Point3d(x - x0, y - y0, z))
                ss = openstudio.model.ShadingSurface(p, osm)
                ss.setShadingSurfaceGroup(group)
                n_srf += 1
    print(f"[shadow] added {n_srf} shadow layer ({len(neighbors)} neighbor ->"
          f"{len(by_height)} elevation group; {n_skip} floor information is missing -> skipped).")
    return n_srf 

def _summer_schedule(osm, config: BuildConfig | None = None):
    """Jun-Sep = 1, other months = 0 (period when persiana control active.)"""
    cfg = config or DEFAULT_BUILD_CONFIG
    sched = openstudio.model.ScheduleRuleset(osm)
    sched.setName("June-Sep is persiana summer program")
    sched.defaultDaySchedule().addValue(openstudio.Time(0, 24, 0, 0), 0.0)
    rule = openstudio.model.ScheduleRule(sched)
    rule.setName("Summer months")
    rule.daySchedule().addValue(openstudio.Time(0, 24, 0, 0), 1.0)
    for day in ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"):
        getattr(rule, f"setApply{day}")(True)
    rule.setStartDate(openstudio.Date(openstudio.MonthOfYear(cfg.shading.summer_start_month), 1))
    end_day = calendar.monthrange(2024, cfg.shading.summer_end_month)[1]
    rule.setEndDate(openstudio.Date(openstudio.MonthOfYear(cfg.shading.summer_end_month), end_day))
    return sched

def _add_facade_openings(osm, srf, wwr: float, win_construction, shading_control,
                         config: BuildConfig | None = None):
    cfg = config or DEFAULT_BUILD_CONFIG
    opening = cfg.openings
    v = srf.vertices()
    if len(v) != 4:
        print(f"[warning] {srf.nameString()}: It is not four cornered, the openin is skipped.")
        return 0, 0, 0.0, 0.0
    ax, ay, z_lo = v[2].x(), v[2].y(), v[2].z()
    bx, by = v[3].x(), v[3].y()
    L = ((bx - ax) ** 2 + (by -ay) ** 2) ** 0.5
    ux, uy = (bx - ax) / L, (by - ay) / L 
    target = L * cfg.geometry.floor_height_m * wwr
    door_area = opening.door_width_m * opening.door_height_m
    n_door = opening.balcony_doors_per_facade_floor
    while n_door > 0 and target < n_door * door_area:
        n_door -= 1
    n_win = max(0, round((target - n_door * door_area) /
                         (opening.window_width_m * opening.window_height_m)))
    n_open = n_win + n_door 

    def make_subsurface(s_center, w, h, sill, kind):
        s1, s2 = s_center - w / 2, s_center + w / 2
        if s1 < 0.1 or s2 > L - 0.1:
            return None
        p = openstudio.Point3dVector()
        for s, z in ((s2, z_lo + sill + h), (s1, z_lo + sill + h),
                    (s1, z_lo + sill), (s2, z_lo + sill )):
            p.append(openstudio.Point3d(ax + ux * s, ay + uy * s, z))
        ss = openstudio.model.SubSurface(p, osm)
        ss.setSurface(srf)
        ss.setSubSurfaceType(kind)
        ss.setConstruction(win_construction)
        if shading_control is not None:
            ss.addShadingControl(shading_control)
        return ss
    
    door_slots = set()
    if n_door >= 1:
        door_slots.add(0)
    if n_door >= 2:
        door_slots.add(n_open - 1)
    
    n_w = n_d = 0
    area = 0.0
    for i in range(n_open):
        s_center = L * ( i + 0.5) / n_open
        if i in door_slots:
            ss = make_subsurface(
                s_center, opening.door_width_m, opening.door_height_m,
                opening.door_sill_m, "GlassDoor")
            if ss is not None:
                ss.addOverhang(opening.balcony_depth_m, 0.1)
                n_d += 1
                area += ss.grossArea()
        else: 
            ss = make_subsurface(
                s_center, opening.window_width_m, opening.window_height_m,
                opening.window_sill_m, "FixedWindow")
            if ss is not None:
                n_w += 1
                area += ss.grossArea()
    return n_w, n_d, area, target 

def build_model_with_config(row, party_geom, config: BuildConfig, neighbors=None) -> BuildResult:
    """Build one OpenStudio model from the complete, validated configuration."""

    P = config.to_legacy_params()
    validate_building_row(row)
    refparcela = row["refparcela"]
    ground_unc = config.geometry.ground_unconditioned
    n_res = int(row["altura_max"])
    n_total = n_res + (1 if ground_unc else 0)

    coords, area = prepare_footprint(clean_polygon(row.geometry), config=config)
    x0, y0 = coords[0]

    osm = load_template(config=config)
    building = osm.getBuilding()
    building.setName(str(refparcela))
    building.setNorthAxis(0.0)

    space_type_res = None
    space_type_ground = None
    for st in osm.getSpaceTypes():
        if st.nameString() == "Espacio Tipo Vivienda CTE":
            space_type_res = st
        elif st.nameString() == "Espacio Tipo No habitable 1ACH":
            space_type_ground = st
    if space_type_res is None:
        raise RuntimeError("'Espacio Vivienda CTRE' not exist in the template!")
    if ground_unc and space_type_ground is None:
        raise RuntimeError("'Espacio Vivienda CTRE' not exist in the template!"
                           "Ground buffer layer requires this type: (GROUND_UNCONDITIONED=True)")
    
    sch_heat = sch_cool = None
    for sch in osm.getScheduleRulesets():
        if sch.nameString() == "T Calefaccion vivienda CTE":
            sch_heat = sch 
        elif sch.nameString() == "T refrigeracion vivienda CTE":  
            sch_cool = sch  
    if sch_heat is None or sch_cool is None:
        raise RuntimeError("Heating/Cooling thermostat programs are not exist in the template!")
    
    thermostat = openstudio.model.ThermostatSetpointDualSetpoint(osm)
    thermostat.setName("Termostat Vivienda CTE (pipeline)")
    thermostat.setHeatingSetpointTemperatureSchedule(sch_heat)
    thermostat.setCoolingSetpointTemperatureSchedule(sch_cool)


    for i in range(n_total):
        z = config.geometry.floor_height_m * i
        pts = [openstudio.Point3d(x - x0, y - y0, z) for x, y in coords]
        opt_space = openstudio.model.Space.fromFloorPrint(
            pts, config.geometry.floor_height_m, osm)
        if opt_space.isNull():
            raise RuntimeError(f"{i}th floor could not be produced (Is the corner row clockwise?)")
        
    spaces = sorted(osm.getSpaces(),
                    key=lambda s: min(v.z() for srf in s.surfaces() for v in srf.vertices()))
    
    zone_of_space = {}
    residential_spaces = []
    for i, sp in enumerate(spaces):
        is_ground = ground_unc and i == 0
        label = "Ground (commercial or buffer zone)" if is_ground else f"{i}th floor"

        story = openstudio.model.BuildingStory(osm)
        story.setName(f"Story {i} - {label}")
        sp.setBuildingStory(story)
        sp.setName(f"Space {i} - {label}")
        
        zone = openstudio.model.ThermalZone(osm)
        zone.setName(f"Zone {i} - {label}")
        sp.setThermalZone(zone)
        zone_of_space[sp.nameString()] = zone 

        if is_ground:
            sp.setSpaceType(space_type_ground)
        else:
            sp.setSpaceType(space_type_res)
            residential_spaces.append(sp)
            ideal = openstudio.model.ZoneHVACIdealLoadsAirSystem(osm)
            ideal.setName(f"Ideal Loads {label}")
            ideal.addToThermalZone(zone)
            dsoa = space_type_res.designSpecificationOutdoorAir()
            if not dsoa.isNull():
                ideal.setDesignSpecificationOutdoorAirObject(dsoa.get())
            zone.setThermostatSetpointDualSetpoint(thermostat)

    for i in range(len(spaces) - 1, 0, -1):
        spaces[i].intersectSurfaces(spaces[i - 1])
        spaces[i].matchSurfaces(spaces[i - 1])
    
    party_buf = None
    if party_geom is not None:
        party_buf = party_geom.buffer(config.geometry.party_wall_tolerance_m)
    n_party = 0
    party_surfaces = []
    for srf in osm.getSurfaces():
        if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
            continue
        if party_buf is not None:
            seg= _wall_plan_segment(srf, x0, y0)
            if (seg.intersection(party_buf).length >
                    config.geometry.party_overlap_ratio * seg.length):
                srf.setOutsideBoundaryCondition("Adiabatic")
                party_surfaces.append(srf)
                n_party +=1

    def _get_construction(name: str):
        for c in osm.getConstructions():
            if c.nameString() == name:
                return c 
        raise RuntimeError(f"Could not find '{name}' in the template!")
    
    du = P["thermal_bridge_du"] or 0.0
    if P["wall_u"] is not None and P["massless"]:
         era_wall = _massless_construction(osm,f"Wall U={P['wall_u']:.2f}+TB{du:.2f} (LHS)", P["wall_u"] + du, 0.17)
    else: 
        base_wall = _build_layered_wall(osm, P["wall_u"] if P["wall_u"] is not None else 1.33)
        if du > 0:
            era_wall = _construction_with_delta_u(osm, base_wall, du, 0.17, f"{base_wall.nameString()} + TB(dU={du:.2f})")
        else:
            era_wall = base_wall
    if P["roof_u"] is not None:
        if P["massless"]:
            era_roof = _massless_construction(osm, f"Roof U={P['roof_u']:.2f} (LHS)", P["roof_u"], 0.14)
        else:
            era_roof = _build_layered_roof(osm, P["roof_u"])
    else:
        era_roof = _get_construction("Cubierta plana no aislada")        
    era_party = _get_construction("Medianera Referencia B")
    for srf in osm.getSurfaces():
        st, obc = srf.surfaceType(), srf.outsideBoundaryCondition()
        if st == "Wall" and obc == "Outdoors":
            srf.setConstruction(era_wall)
        elif st == "RoofCeiling" and obc == "Outdoors":
            srf.setConstruction(era_roof)
        elif st == "Wall" and obc == "Adiabatic":
            srf.setConstruction(era_party)
    
    glazing = openstudio.model.SimpleGlazing(osm)
    glazing.setName("IVE 1960-80 only glass aluminum")
    glazing.setUFactor(P["window_u"])
    glazing.setSolarHeatGainCoefficient(P["window_g"])
    win_construction = openstudio.model.Construction(osm)
    win_construction.insertLayer(0, glazing)


    shading_control = None 
    for blind in osm.getBlinds():
        if blind.nameString() == config.shading.blind_name:
            shading_control = openstudio.model.ShadingControl(blind) 
            shading_control.setName("Persiana check (write)")
            shading_control.setShadingType("ExteriorBlind")
            shading_control.setShadingControlType("OnIfHighSolarOnWindow")
            shading_control.setSetpoint(P["shade_setpoint"])
            shading_control.setSchedule(_summer_schedule(osm, config=config))
            break
    if shading_control is None:
        print(f"[Warning] there is no '{config.shading.blind_name}' in the template")

    residential_names = {sp.nameString() for sp in residential_spaces}
    n_windows = 0
    n_doors = 0
    window_area = 0.0
    facade_qa = {}
    for srf in osm.getSurfaces():
        if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
            continue
        sp = srf.space()
        if sp.isNull() or sp.get().nameString() not in residential_names:
            continue
        az = openstudio.radToDeg(srf.azimuth())
        ratio = wwr_for_azimuth(az, config=config)
        n_w, n_d, glass_area, target_area = _add_facade_openings(
            osm, srf, ratio, win_construction, shading_control, config=config)
        n_windows += n_w
        n_doors += n_d
        window_area += glass_area
        key = round(az, 1)
        agg = facade_qa.setdefault(key, {"azimut": key, "wwr_target": round(ratio, 3),
                                         "wall_m2": 0.0, "target_m2": 0.0,
                                         "glass_m2": 0.0, "window": 0, "door": 0})
        agg["wall_m2"] += srf.grossArea()
        agg["target_m2"] += target_area
        agg["glass_m2"] += glass_area
        agg["window"] += n_w
        agg["door"] += n_d
    for agg in facade_qa.values():
        agg["wwr_real"] = round(agg["glass_m2"] / agg["wall_m2"], 3) if agg["wall_m2"] else 0.0
        agg["lapse_pct"] = (round (100.0 * (agg["glass_m2"] - agg["target_m2"]) / agg["target_m2"], 1)
                            if agg["target_m2"] else 0.0)
        for k in ("wall_m2", "target_m2", "glass_m2"):
            agg[k] = round(agg[k], 1)

    if P["infiltration_ach"] is not None:
        # only the dwelling airtightness object ("... constante 0,2ACH Viv CTE");
        # buffer-space objects (No habitable 1ACH/3ACH) keep their template values
        for inf in osm.getSpaceInfiltrationDesignFlowRates():
            if "constante" in inf.nameString() and "Viv" in inf.nameString():
                inf.setAirChangesperHour(P["infiltration_ach"])
    

    n_shading = 0
    if P["context_shading"]:
        nb = neighbors
        if nb is None:
            nb = load_neighbors(
                clean_polygon(row.geometry), refparcela,
                config.data.neighbor_path, config=config)
        n_shading = _add_context_shading(osm, nb, x0, y0, config=config)
        osm.getShadowCalculation().setMaximumFiguresInShadowOverlapCalculations(
            config.shading.max_shadow_figures)

    epw = openstudio.EpwFile(openstudio.toPath(str(config.data.epw_path)))
    openstudio.model.WeatherFile.setWeatherFile(osm, epw)
    
    for var_name in config.operation.output_variables.values():
        var = openstudio.model.OutputVariable(var_name, osm)
        var.setReportingFrequency("RunPeriod")
    
    stats = {
        "refparcela": refparcela,
        "footprint_m2": round(area, 1),
        "n_floors_total": n_total,
        "n_floors_residential": n_res,
        "res_area_m2": round(area * n_res, 1),        
        "n_party_surfaces": n_party,
        "n_windows": n_windows,
        "n_balcony_doors": n_doors,
        "window_area_m2": round(window_area, 1),
        "n_shading_surfaces": n_shading,
        "wall_construction": era_wall.nameString(),
        "roof_construction": era_roof.nameString(),             
        "facade_qa": list(facade_qa.values()),
        "origin_x": x0,
        "origin_y": y0,
    }
    warnings = []
    for facade in stats["facade_qa"]:
        if abs(facade["lapse_pct"]) > config.qa.facade_wwr_warning_pct:
            warnings.append(
                f"Facade {facade['azimut']}° WWR deviation is {facade['lapse_pct']}%")
    return BuildResult(osm=osm, stats=stats, config=config, warnings=warnings)


def build_model(row, party_geom, params=None, neighbors=None):
    """Legacy API preserved for the teaching script and existing pipelines."""
    config = DEFAULT_BUILD_CONFIG.with_legacy_params(params)
    result = build_model_with_config(row, party_geom, config, neighbors=neighbors)
    return result.osm, result.stats


def save_model(osm, run_dir: Path) -> Path:
    
    run_dir.mkdir(parents=True, exist_ok=True)
    osm_path = run_dir / "model_python.osm"
    if not osm.save(openstudio.toPath(str(osm_path)), True):
        raise RuntimeError(f"Model could not saved: {osm_path}")
    return osm_path


# ============================================================================
# 4.6) REAL HVAC CONVERSION (consumption mode) — Part F1
# ============================================================================
# Converts a finished ideal-loads model into a real-system model so EnergyPlus
# reports CONSUMPTION instead of demand (Rai parity). Stock assumptions kept in
# one place until Javier confirms them (question #13):

HVAC_HEATING_EFFICIENCY = 0.85    # natural-gas burner, seasonal efficiency (Spanish stock)
HVAC_COOLING_COP = 2.5            # split DX unit, rated COP

# Valencia design days for autosizing (ASHRAE climatic design conditions,
# Valencia aeropuerto 082840). Needed because no .ddy file came with the EPW:
DESIGN_DAYS = {
    "heating": {"month": 1, "day": 21, "db": 2.6, "range": 0.0,
                "wb": 2.6, "day_type": "WinterDesignDay"},
    "cooling": {"month": 7, "day": 21, "db": 32.4, "range": 8.7,
                "wb": 22.8, "day_type": "SummerDesignDay"},
}


def add_real_hvac(osm, heating_efficiency=None, cooling_cop=None):
    """Swap ideal loads for a real HVAC system on an already-built model.

    Per conditioned zone: PTAC = Fan:OnOff + Coil:Heating:Fuel (natural gas,
    fixed burner efficiency) + Coil:Cooling:DX:SingleSpeed (fixed rated COP).
    Adds the two Valencia design days + zone sizing so E+ can autosize.
    Thermostats, schedules, DSOA and the envelope stay untouched.
    Returns a small dict for the run metadata."""

    eff = HVAC_HEATING_EFFICIENCY if heating_efficiency is None else float(heating_efficiency)
    cop = HVAC_COOLING_COP if cooling_cop is None else float(cooling_cop)

    zones = []
    for unit in list(osm.getZoneHVACIdealLoadsAirSystems()):
        opt = unit.thermalZone()
        if opt.is_initialized():
            zones.append(opt.get())
        unit.remove()
    if not zones:
        raise RuntimeError("add_real_hvac: no ideal-loads zones found - build the model first.")

    always_on = osm.alwaysOnDiscreteSchedule()
    for zone in zones:
        fan = openstudio.model.FanOnOff(osm, always_on)
        fan.setName(f"PTAC Fan {zone.nameString()}")
        heat = openstudio.model.CoilHeatingGas(osm, always_on)
        heat.setName(f"PTAC Gas Heat {zone.nameString()}")
        heat.setGasBurnerEfficiency(eff)
        cool = openstudio.model.CoilCoolingDXSingleSpeed(osm)
        cool.setName(f"PTAC DX Cool {zone.nameString()}")
        cool.setRatedCOP(cop)
        ptac = openstudio.model.ZoneHVACPackagedTerminalAirConditioner(
            osm, always_on, fan, heat, cool)
        ptac.setName(f"PTAC {zone.nameString()}")
        # The real Spanish stock (splits / radiators) supplies NO mechanical
        # outdoor air: ventilation = infiltration + window opening. The CTE
        # DSOA ventilation stays only in the normative ideal-loads demand run.
        ptac.setOutdoorAirFlowRateDuringCoolingOperation(0.0)
        ptac.setOutdoorAirFlowRateDuringHeatingOperation(0.0)
        ptac.setOutdoorAirFlowRateWhenNoCoolingorHeatingisNeeded(0.0)
        if not ptac.addToThermalZone(zone):
            raise RuntimeError(f"add_real_hvac: PTAC could not be added to {zone.nameString()}")

    for label, dd in DESIGN_DAYS.items():
        obj = openstudio.model.DesignDay(osm)
        obj.setName(f"Valencia {label} design day")
        obj.setMaximumDryBulbTemperature(dd["db"])
        obj.setDailyDryBulbTemperatureRange(dd["range"])
        obj.setHumidityConditionType("Wetbulb")
        obj.setWetBulbOrDewPointAtMaximumDryBulb(dd["wb"])
        obj.setMonth(dd["month"])
        obj.setDayOfMonth(dd["day"])
        obj.setDayType(dd["day_type"])

    sc = osm.getSimulationControl()
    sc.setDoZoneSizingCalculation(True)
    sc.setRunSimulationforSizingPeriods(False)
    sc.setRunSimulationforWeatherFileRunPeriods(True)

    return {"hvac_zones": len(zones), "heating_efficiency": eff, "cooling_cop": cop}


# ============================================================================
# 4.7) SCENARIO KNOBS — comfort setpoints + weather file (Part G)
# ============================================================================
# Post-build mutations (same additive pattern as add_real_hvac): the template
# CTE thermostat programs use sentinel values to switch the system OFF
# (heating -10 °C off-season, cooling 50 °C off-season/off-hours). A comfort
# offset must shift only REAL setpoints and leave the sentinels untouched:

COMFORT_HEAT_MIN_C = 5.0     # heating schedule values below this are "off" sentinels
COMFORT_COOL_MAX_C = 40.0    # cooling schedule values above this are "off" sentinels


def _shift_day_schedule(day_sch, delta: float, lo: float | None, hi: float | None) -> int:
    """Shift every in-band value of one ScheduleDay by delta. Returns count."""
    pairs = list(zip(day_sch.times(), day_sch.values()))
    day_sch.clearValues()
    shifted = 0
    for t, v in pairs:
        in_band = (lo is None or v > lo) and (hi is None or v < hi)
        day_sch.addValue(t, v + delta if in_band else v)
        shifted += int(in_band)
    return shifted


def _shift_ruleset(osm, sch, delta: float, lo: float | None, hi: float | None):
    """Clone a ScheduleRuleset and shift its RULE day profiles. Returns clone.

    The default day profile is deliberately NOT shifted: in the CTE template
    the rules cover the whole year and the default (23.5/23.5, zero deadband)
    only feeds design-day sizing — shifting it makes the heating setpoint
    cross the cooling one and EnergyPlus aborts the sizing run."""
    clone = sch.clone(osm).to_ScheduleRuleset().get()
    sign = "+" if delta >= 0 else ""
    clone.setName(f"{sch.nameString()} {sign}{delta}C (scenario)")
    n = 0
    for rule in clone.scheduleRules():
        n += _shift_day_schedule(rule.daySchedule(), delta, lo, hi)
    if not clone.isWinterDesignDayScheduleDefaulted():
        n += _shift_day_schedule(clone.winterDesignDaySchedule(), delta, lo, hi)
    if not clone.isSummerDesignDayScheduleDefaulted():
        n += _shift_day_schedule(clone.summerDesignDaySchedule(), delta, lo, hi)
    return clone, n


def apply_comfort_offsets(osm, heat_delta: float = 0.0, cool_delta: float = 0.0) -> dict:
    """Shift the pipeline thermostat setpoints by heat_delta / cool_delta (K).

    Positive heat_delta = warmer homes in winter (more heating);
    negative cool_delta = colder homes in summer (more cooling).
    No-op when both deltas are 0 (default runs stay byte-identical)."""
    heat_delta, cool_delta = float(heat_delta), float(cool_delta)
    info = {"heat_delta_c": heat_delta, "cool_delta_c": cool_delta}
    if heat_delta == 0.0 and cool_delta == 0.0:
        return info

    thermostats = [t for t in osm.getThermostatSetpointDualSetpoints()
                   if t.nameString() == "Termostat Vivienda CTE (pipeline)"]
    if not thermostats:
        raise RuntimeError("apply_comfort_offsets: pipeline thermostat not found "
                           "- build the model first.")
    thermostat = thermostats[0]                    # one shared object for all zones

    if heat_delta != 0.0:
        sch = thermostat.heatingSetpointTemperatureSchedule().get().to_ScheduleRuleset().get()
        clone, n = _shift_ruleset(osm, sch, heat_delta, lo=COMFORT_HEAT_MIN_C, hi=None)
        thermostat.setHeatingSetpointTemperatureSchedule(clone)
        info["heat_values_shifted"] = n
    if cool_delta != 0.0:
        sch = thermostat.coolingSetpointTemperatureSchedule().get().to_ScheduleRuleset().get()
        clone, n = _shift_ruleset(osm, sch, cool_delta, lo=None, hi=COMFORT_COOL_MAX_C)
        thermostat.setCoolingSetpointTemperatureSchedule(clone)
        info["cool_values_shifted"] = n
    return info


def set_weather_file(osm, epw_path) -> dict:
    """Replace the model's weather file (climate-scenario runs, e.g. a morphed
    2050 EPW). The default EPW is already set by build_model."""
    epw_path = Path(epw_path)
    if not epw_path.exists():
        raise FileNotFoundError(f"EPW file not found: {epw_path}")
    epw = openstudio.EpwFile(openstudio.toPath(str(epw_path)))
    if not openstudio.model.WeatherFile.setWeatherFile(osm, epw):
        raise RuntimeError(f"Weather file could not be set: {epw_path}")
    return {"epw_file": epw_path.name}


def main():

    t0 = datetime.now()
    validate_input_files()
    buildings = load_buildings(BUILDING_GPKG)

    for _, row in buildings.iterrows():
        ref = row["refparcela"]
        print(f"\n===== Building: {ref} =====")

        geom = clean_polygon(row.geometry)
        neighbors = load_neighbors(geom, ref, NEIGHBORS_SHP)  
        party = find_party_walls(geom, ref, NEIGHBORS_SHP, neighbors=neighbors)

        osm, stats = build_model(row, party, neighbors=neighbors)
        print(f"[model] floor {stats['footprint_m2']} m² × {stats['n_floors_total']} level "
              f"({stats['n_floors_residential']} vivienda) | party surface: {stats['n_party_surfaces']} "
              f"| {stats['n_windows']} windows + {stats['n_balcony_doors']} balcony doors = "
              f"{stats['window_area_m2']} m² glass | {stats['n_shading_surfaces']} shading surfaces")

        osm_path = save_model(osm, OUT_DIR / str(ref))
        print(f"[kayıt] {osm_path}")

    print("\n[not] This model build 3d surface (EnergyPlus not).")
    print("      The image control: Open with openstudio application or")
    print("      .venv/bin/python src/reference/plot_model_claude.py")
    print("      Simulation:  .venv/bin/python src/reference/run_simulation_claude.py")
    print(f"Time: {(datetime.now() - t0).total_seconds():.0f} sn")


if __name__ == "__main__":
    main()
