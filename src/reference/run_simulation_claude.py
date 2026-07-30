"""EnergyPlus simülasyon koşucusu (runner) — Claude'un açıklamalı referans kodu.

NE YAPAR (SİMÜLASYON ZİNCİRİ — 3D model kurulumu BURADA DEĞİL):
  model_builder_claude (mb) ile kurulan OpenStudio modeli →
  EnergyPlus yıllık simülasyonu (pyenergyplus API) →
  eplusout.sql'den ısıtma/soğutma talebi (kWh/m²) →
  QA/DOĞRULAMA katmanı (err taraması Severe=0 şartı, E+ ↔ GIS çapraz kontrol,
  makullük bandı, qa_report.txt + run_metadata.json) →
  KARBON AYAK İZİ (senaryo bazlı: talep→tüketim→kgCO₂/m²·yıl) → results.csv

  3D MODEL KURULUMU AYRI DOSYADA: model_builder_claude.py — bu dosya onu import
  eder; geometri fonksiyonları ve paylaşılan CONFIG (yollar, OUTPUT_VARIABLES,
  DEFAULT_PARAMS) mb.* üzerinden gelir. NEDEN İKİYE BÖLÜNDÜ: modelleme ile
  simülasyon ayrı sorumluluklar; önce model kurulup GÖZLE doğrulanır (builder),
  sonra simüle edilir (bu dosya) — kullanıcı isteği, 2026-07-06.

MANUEL SÜREÇLE EŞLEŞME (kalan adımlar — öncekiler builder'da):
  Run Simulation                → run_energyplus()          (pyenergyplus API)
  Sonuç okuma (Results tab)     → read_results()            (pandas + eplusout.sql)

ÇALIŞTIRMA (modeli mb ile kurar, E+ koşar, QA + karbon + results.csv üretir):
  cd ~/valencia-energy-sim
  .venv/bin/python src/reference/run_simulation_claude.py
"""

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import openstudio
import pandas as pd

import model_builder_claude as mb     # geometri + paylaşılan CONFIG (yollar, params)

# ============================================================================
# CONFIG — SADECE simülasyon/QA/karbon sabitleri. Geometri sabitleri ve yollar
# builder'dadır (mb.BUILDINGS_GPKG, mb.EPW_FILE, mb.OUT_DIR, mb.OUTPUT_VARIABLES...).
# ============================================================================
EPLUS_DIR = "/Applications/OpenStudio-3.11.0/EnergyPlus"  # OpenStudio'ya gömülü E+ 25.2.0

# QA eşikleri (E+ ↔ GIS çapraz kontrol toleransları):
QA_AREA_TOL = 0.005      # şartlandırılmış alan: model vs E+ ≤ %0,5
QA_GLAZING_TOL = 0.02    # cam alanı: model vs E+ ≤ %2
QA_UNMET_HOURS_MAX = 50  # ideal loads'ta unmet hours ~0 beklenir; 50 h üstü şüpheli
QA_RESULT_BAND = (1.0, 150.0)   # kWh/m²·yıl makullük bandı (Akdeniz konut)

# Kadastro sertifika kolonları (karşılaştırma için; tanımlar Javier'de doğrulanacak):
COL_HEAT_DEMAND = "demanda_ca"   # muhtemelen ısıtma talebi kWh/m²
COL_COOL_DEMAND = "demanda__1"   # muhtemelen soğutma talebi kWh/m²

# --- KARBON KATMANI SABİTLERİ ------------------------------------------------
# Zincir: TALEP (simülasyon, kWh/m²) → TÜKETİM (sistem verimi: talep÷COP) →
#         CO₂ (tüketim × emisyon faktörü, kgCO₂/kWh).
# KAPSAM: sadece mekân ısıtma+soğutma (simüle edilen talep). Sıcak su (DHW),
# aydınlatma ve cihazlar HARİÇ — rapora böyle yazılır. Embodied karbon (LCA)
# bu katmana girmez.
#
# Emisyon faktörleri — İspanya RESMİ bina sertifikasyon değerleri:
# IDAE/MITECO, "Factores de emisión de CO₂ y coeficientes de paso a energía
# primaria ... en el sector de edificios en España" (2016, CTE/RITE tanınmış
# doküman). CERMA da bunları kullanır → kadastro `calificaci` harfleriyle
# tutarlı zemin. NOT: gerçek 2024 şebekesi daha temiz (~0,15-0,20) — bu fark
# ileride LHS belirsizlik değişkeni adayı.
EMISSION_FACTORS = {           # kgCO₂ / kWh (nihai enerji)
    "electricity": 0.331,      # elektrik, yarımada İspanya
    "natural_gas": 0.252,      # doğalgaz (senaryolarda kullanılmıyor; varyant için)
}

# Sistem senaryoları — 1974 binası, gerçek sistem verisi YOK; iki uç senaryo:
# (Doğalgaz kombisi varyantı: ısıtma tüketimi = talep ÷ 0,85 (kazan verimi),
#  CO₂ = tüketim × 0.252 → istenirse sözlüğe 3. senaryo olarak eklenebilir.)
SYSTEM_SCENARIOS = {
    "S1 elektrikli direnç + eski split": {
        "heating_cop": 1.0,    # portatif elektrikli radyatör (Valencia'da yaygın)
        "cooling_seer": 2.0,   # eski split klima
        "fuel": "electricity",
    },
    "S2 reversible split (ısı pompası)": {
        "heating_cop": 2.5,    # modern split, SCOP
        "cooling_seer": 2.5,
        "fuel": "electricity",
    },
}


# ============================================================================
# 5) ENERGYPLUS KOŞUSU
# ============================================================================

def run_energyplus(osm, run_dir: Path) -> Path:
    """Modeli IDF'e çevir ve EnergyPlus'ı çalıştır. Dönüş: eplusout.csv yolu.

    Gauthier'in akışından İKİ fark:
    1. ExpandObjects GEREKMİYOR: OpenStudio ideal loads'u doğrudan
       ZoneHVAC:IdealLoadsAirSystem IDF objesi olarak yazar (HVACTemplate değil).
    2. E+ ayrı kurulum değil: OpenStudio 3.11.0'a gömülü 25.2.0 kullanılıyor.
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    # OSM'i sakla (OpenStudio Application ile açıp bakılabilir)
    osm.save(openstudio.toPath(str(run_dir / "model_python.osm")), True)

    # ForwardTranslator: OpenStudio modeli → EnergyPlus IDF
    ft = openstudio.energyplus.ForwardTranslator()
    workspace = ft.translateModel(osm)
    idf_path = run_dir / "model.idf"
    workspace.save(openstudio.toPath(str(idf_path)), True)

    if not Path(EPLUS_DIR).exists():
        raise FileNotFoundError(f"EnergyPlus klasörü yok: {EPLUS_DIR}")

    # pyenergyplus, E+ kurulum klasöründen import edilir (pip paketi değil!)
    if EPLUS_DIR not in sys.path:
        sys.path.insert(0, EPLUS_DIR)
    from pyenergyplus.api import EnergyPlusAPI

    api = EnergyPlusAPI()
    state = api.state_manager.new_state()             # her koşu taze state (N-bina için şart)
    exit_code = api.runtime.run_energyplus(state, [
        "--weather", str(mb.EPW_FILE),
        "--output-directory", str(run_dir),
        str(idf_path),
    ])
    # NOT: '--readvars' (CSV üretici) KULLANMIYORUZ — OpenStudio'ya gömülü E+
    # dağıtımında ReadVarsESO aracı yok (denendi, 'Could not find ReadVarsESO').
    # Gerek de yok: E+ her koşuda eplusout.sql (SQLite veritabanı) üretiyor;
    # sonuçları oradan pandas ile okuyacağız — daha sağlam ve daha öğretici.
    api.state_manager.delete_state(state)
    if exit_code != 0:
        raise RuntimeError(f"EnergyPlus hata verdi (exit={exit_code}) — {run_dir}/eplusout.err dosyasına bak.")
    return run_dir / "eplusout.sql"


# ============================================================================
# 6) SONUÇ OKUMA — E+ çıktısı → pandas → kWh/m²
# ============================================================================

def read_results(sql_path: Path, res_area_m2: float) -> dict:
    """eplusout.sql'den (E+'ın SQLite çıktı veritabanı) yıllık ısıtma/soğutma
    toplamını oku, kWh/m²'ye çevir.

    E+ SQL şeması: ReportDataDictionary = 'hangi değişken, hangi zon, hangi
    frekans' kataloğu; ReportData = değerlerin kendisi. İkisini JOIN'leyip
    'Run Period' (yıllık toplam) frekanslı satırları çekiyoruz — bunlar bizim
    mb.build_model()'de tanımladığımız iki OutputVariable'ın yıllık değerleri.
    Filtre TAM AD eşleşmesi (mb.OUTPUT_VARIABLES) — 'contains(Heating)' gibi gevşek
    filtre, ileride eklenecek başka bir değişkeni de toplayıp sonucu sessizce
    şişirebilirdi (çift sayma koruması).

    Değerler Joule; kWh = J / 3.6e6. m² = KONUT alanı (zemin ticari kat hariç —
    kadastro sertifika değerleri de konut alanına göre).
    """
    import sqlite3
    con = sqlite3.connect(sql_path)
    df = pd.read_sql("""
        SELECT d.Name AS degisken, d.KeyValue AS zon, r.Value AS joule
        FROM ReportData r
        JOIN ReportDataDictionary d
          ON r.ReportDataDictionaryIndex = d.ReportDataDictionaryIndex
        WHERE d.ReportingFrequency = 'Run Period'
          AND d.Name IN (?, ?)
    """, con, params=(mb.OUTPUT_VARIABLES["heating"], mb.OUTPUT_VARIABLES["cooling"]))
    con.close()
    if df.empty:
        raise RuntimeError(f"SQL'de yıllık (Run Period) veri yok: {sql_path}")
    heat_j = df.loc[df["degisken"] == mb.OUTPUT_VARIABLES["heating"], "joule"].sum()
    cool_j = df.loc[df["degisken"] == mb.OUTPUT_VARIABLES["cooling"], "joule"].sum()
    heat_kwh = float(heat_j) / 3.6e6
    cool_kwh = float(cool_j) / 3.6e6
    return {
        "heating_kwh": round(heat_kwh, 1),
        "cooling_kwh": round(cool_kwh, 1),
        "heating_kwh_m2": round(heat_kwh / res_area_m2, 2),
        "cooling_kwh_m2": round(cool_kwh / res_area_m2, 2),
    }


# ============================================================================
# 6.5) QA / DOĞRULAMA KATMANI — "hatasız" iddiasının kanıt mekanizması
# ============================================================================
# Akademik ilke: model çıktısına güvenmeden önce (1) E+ hata dosyası temiz mi,
# (2) E+'ın gördüğü bina bizim GIS'ten kurduğumuz binayla aynı mı (çapraz
# kontrol), (3) sonuçlar fiziksel olarak makul mü — üçü de otomatik denetlenir
# ve bina başına qa_report.txt + run_metadata.json olarak arşivlenir
# (tekrarlanabilirlik: hangi girdiyle, hangi sürümle, hangi parametreyle).

def scan_err_file(run_dir: Path) -> dict:
    """eplusout.err'i tara: Severe/Fatal varsa KOŞUYU GEÇERSİZ SAY (exception).
    E+ exit code 0 dönse bile Severe üretebilir — exit code'a güvenmek yetmez.
    Dönüş: {'warnings': n, 'severes': n} (Warning sayısı rapora yazılır).
    """
    err_path = run_dir / "eplusout.err"
    if not err_path.exists():
        raise RuntimeError(f"eplusout.err yok: {run_dir} — koşu hiç başlamamış olabilir.")
    text = err_path.read_text(errors="replace")
    n_severe = text.count("** Severe  **")
    n_fatal = text.count("**  Fatal  **")
    # DİKKAT: err'de birden çok özet satırı var (Warmup/Sizing/final) — GERÇEK
    # toplam SON satırda ('Completed Successfully-- N Warning; ...'). İlk
    # eşleşme Warmup'ın 0'ını alırdı (yaşandı — QA'nın kendi debug dersi).
    matches = re.findall(r"(\d+)\s+Warning;\s+(\d+)\s+Severe", text)
    n_warning = int(matches[-1][0]) if matches else text.count("** Warning **")
    if n_severe or n_fatal:
        raise RuntimeError(f"E+ {n_severe} Severe / {n_fatal} Fatal hata üretti — "
                           f"sonuçlar geçersiz. Bak: {err_path}")
    return {"warnings": n_warning, "severes": n_severe}


def _tabular_value(con, report: str, table: str, row: str, column: str):
    """E+ SQL TabularDataWithStrings'ten tek değer çek (bulunamazsa None)."""
    cur = con.execute(
        """SELECT Value FROM TabularDataWithStrings
           WHERE ReportName=? AND TableName=? AND RowName=? AND ColumnName=?""",
        (report, table, row, column))
    r = cur.fetchone()
    try:
        return float(r[0]) if r else None
    except (TypeError, ValueError):
        return None


def crosscheck_energyplus(sql_path: Path, stats: dict) -> list:
    """E+ ↔ GIS çapraz kontrolü: E+'ın tabular raporundaki bina, bizim GIS'ten
    kurduğumuz binayla aynı mı? Model kurulum hatalarını (yanlış alan, kayıp
    pencere, eksik zon) SONUÇLARA bakmadan yakalar.
    Dönüş: kontrol listesi [{kontrol, model, eplus, tolerans, gecti}, ...].
    """
    import sqlite3
    con = sqlite3.connect(sql_path)
    checks = []

    def add(name, model_val, eplus_val, tol_rel):
        if eplus_val is None:
            checks.append({"kontrol": name, "model": model_val, "eplus": None,
                           "tolerans": tol_rel, "gecti": False})
            return
        ok = abs(eplus_val - model_val) <= tol_rel * max(abs(model_val), 1e-9)
        checks.append({"kontrol": name, "model": round(model_val, 2),
                       "eplus": round(eplus_val, 2), "tolerans": tol_rel, "gecti": ok})

    # 1) Şartlandırılmış taban alanı: konut alanı (GIS taban × konut katı) ↔ E+
    add("sartlandirilmis_alan_m2", stats["res_area_m2"],
        _tabular_value(con, "AnnualBuildingUtilityPerformanceSummary",
                       "Building Area", "Net Conditioned Building Area", "Area"),
        QA_AREA_TOL)
    # 2) Cam alanı: yerleştirdiğimiz açıklık toplamı ↔ E+ fenestration özeti
    add("cam_alani_m2", stats["window_area_m2"],
        _tabular_value(con, "EnvelopeSummary", "Exterior Fenestration",
                       "Total or Average", "Area of Multiplied Openings"),
        QA_GLAZING_TOL)
    # 3) Zon sayısı: kat sayısıyla birebir olmalı
    n_zones = con.execute("SELECT COUNT(*) FROM Zones").fetchone()[0]
    checks.append({"kontrol": "zon_sayisi", "model": stats["n_floors_total"],
                   "eplus": n_zones, "tolerans": 0,
                   "gecti": n_zones == stats["n_floors_total"]})
    # 4) Unmet hours: ideal loads sınırsız kapasite → ~0 olmalı; değilse
    #    termostat/program kurulumunda sorun var demektir.
    unmet = 0.0
    for col in ("During Occupied Heating", "During Occupied Cooling"):
        v = _tabular_value(con, "SystemSummary", "Time Setpoint Not Met", "Facility", col)
        unmet += v or 0.0
    checks.append({"kontrol": "unmet_saat", "model": 0.0, "eplus": round(unmet, 1),
                   "tolerans": QA_UNMET_HOURS_MAX, "gecti": unmet <= QA_UNMET_HOURS_MAX})
    con.close()
    return checks


def check_plausibility(res: dict) -> list:
    """Sonuç makullük bandı: Akdeniz konutunda kWh/m²·yıl 1–150 dışıysa model
    kurgusunda büyük hata vardır (birim karışıklığı, alan hatası...)."""
    lo, hi = QA_RESULT_BAND
    out = []
    for k in ("heating_kwh_m2", "cooling_kwh_m2"):
        v = res[k]
        out.append({"kontrol": f"makul_band_{k}", "model": v,
                    "eplus": f"[{lo}-{hi}]", "tolerans": "-", "gecti": lo <= v <= hi})
    return out


def _sha1(path: Path) -> str:
    """Girdi dosyası parmak izi (tekrarlanabilirlik: SONUÇ hangi veriyle üretildi)."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _eplus_version(run_dir: Path) -> str:
    """E+ sürümünü err dosyasının ilk satırından oku ('Program Version,...')."""
    try:
        first = (run_dir / "eplusout.err").read_text(errors="replace").splitlines()[0]
        m = re.search(r"Version ([\d.]+)", first)
        return m.group(1) if m else first.strip()
    except Exception:
        return "?"


def write_run_metadata(run_dir: Path, stats: dict, res: dict, params: dict | None):
    """run_metadata.json: bu koşu HANGİ girdiler + sürümler + parametrelerle
    üretildi — akademik tekrarlanabilirliğin asgari kaydı."""
    meta = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "openstudio": openstudio.openStudioVersion(),
        "energyplus": _eplus_version(run_dir),
        "paketler": {"geopandas": gpd.__version__, "pandas": pd.__version__},
        "girdiler_sha1": {
            "buildings_gpkg": _sha1(mb.BUILDINGS_GPKG),
            "template_osm": _sha1(mb.TEMPLATE_OSM),
            "epw": _sha1(mb.EPW_FILE),
            "neighbors_shp": _sha1(mb.NEIGHBORS_SHP),
        },
        "params": {k: v for k, v in (params or mb.DEFAULT_PARAMS).items()},
        "stats": {k: v for k, v in stats.items() if k != "facade_qa"},
        "sonuclar": res,
    }
    with open(run_dir / "run_metadata.json", "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def write_qa_report(run_dir: Path, stats: dict, res: dict,
                    err_stats: dict, checks: list) -> bool:
    """İnsan-okur QA raporu (qa_report.txt). Dönüş: tüm kontroller geçti mi?"""
    lines = [f"QA RAPORU — {stats['refparcela']}",
             f"({datetime.now().isoformat(timespec='seconds')})", ""]
    lines.append(f"E+ hata dosyası: {err_stats['warnings']} Warning, "
                 f"{err_stats['severes']} Severe (Severe=0 şart, sağlandı)")
    lines.append("")
    lines.append("Çapraz kontroller (model ↔ EnergyPlus):")
    all_ok = True
    for c in checks:
        mark = "OK " if c["gecti"] else "FAIL"
        all_ok &= c["gecti"]
        lines.append(f"  [{mark}] {c['kontrol']:28s} model={c['model']} "
                     f"eplus={c['eplus']} tol={c['tolerans']}")
    lines.append("")
    lines.append("Cephe açıklıkları (hedef vs gerçekleşen WWR; sapma %15 üstü işaretlenir):")
    for f in stats.get("facade_qa", []):
        flag = "  <-- KONTROL ET" if abs(f["sapma_pct"]) > 15.0 else ""
        lines.append(f"  azimut {f['azimut']:6.1f}°: hedef {f['wwr_hedef']:.3f} → "
                     f"gerçekleşen {f['wwr_gercek']:.3f} "
                     f"({f['cam_m2']} / {f['duvar_m2']} m²; {f['pencere']} pencere + "
                     f"{f['kapi']} kapı; sapma {f['sapma_pct']:+.1f}%){flag}")
    lines.append("")
    lines.append(f"Sonuçlar: ısıtma {res['heating_kwh_m2']} / soğutma "
                 f"{res['cooling_kwh_m2']} kWh/m²·yıl")
    lines.append(f"GENEL: {'TÜM KONTROLLER GEÇTİ' if all_ok else 'EN AZ BİR KONTROL BAŞARISIZ'}")
    (run_dir / "qa_report.txt").write_text("\n".join(lines) + "\n")
    return all_ok


# ============================================================================
# 7) KARBON AYAK İZİ — talep → tüketim → CO₂
# ============================================================================

def carbon_footprint(res: dict, res_area_m2: float) -> dict:
    """Simülasyon TALEBİNİ senaryo bazında karbona çevir.

    Her senaryo için üç adım (fizik değil, aritmetik — model tekrar KOŞMAZ):
      1. Tüketim  = ısıtma talebi ÷ COP  +  soğutma talebi ÷ SEER   [kWh/m²·yıl]
         (COP/SEER: sistemin 1 kWh elektrikle kaç kWh ısı/soğuk taşıdığı;
          direnç ısıtıcıda COP=1, ısı pompasında >1 → daha az tüketim)
      2. CO₂     = tüketim × emisyon faktörü                        [kgCO₂/m²·yıl]
      3. Bina toplamı = CO₂ × konut alanı ÷ 1000                    [tCO₂/yıl]

    N-bina notu: fonksiyon saf (sadece sözlük alır) → döngüde bina başına
    çağrılır; mahalle toplamı = Σ(kgCO₂/m² × alan). LHS bağlantısı: COP/SEER
    ve emisyon faktörü ileride belirsizlik değişkeni olacak (Ashby Layer 4).
    """
    out = {}
    for i, (name, sc) in enumerate(SYSTEM_SCENARIOS.items(), start=1):
        ef = EMISSION_FACTORS[sc["fuel"]]
        cons_kwh_m2 = (res["heating_kwh_m2"] / sc["heating_cop"]
                       + res["cooling_kwh_m2"] / sc["cooling_seer"])
        co2_kg_m2 = cons_kwh_m2 * ef
        co2_t_bina = co2_kg_m2 * res_area_m2 / 1000.0
        prefix = f"s{i}"
        out[f"{prefix}_senaryo"] = name
        out[f"{prefix}_tuketim_kwh_m2"] = round(cons_kwh_m2, 2)
        out[f"{prefix}_co2_kg_m2"] = round(co2_kg_m2, 2)
        out[f"{prefix}_co2_t_yil"] = round(co2_t_bina, 1)
    return out


# ============================================================================
# 8) ANA DÖNGÜ — N bina (v1: 1 bina)
# ============================================================================

def main():
    t0 = datetime.now()
    mb.validate_input_files()                            # fail-fast: eksik dosya varsa hiç başlama
    buildings = mb.load_buildings(mb.BUILDINGS_GPKG)

    rows = []
    for _, row in buildings.iterrows():
        ref = row["refparcela"]
        print(f"\n===== BİNA: {ref} =====")

        geom = mb.clean_polygon(row.geometry)
        neighbors = mb.load_neighbors(geom, ref, mb.NEIGHBORS_SHP)   # tek okuma: party + gölge
        party = mb.find_party_walls(geom, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)

        osm, stats = mb.build_model(row, party, neighbors=neighbors)
        print(f"[model] taban {stats['footprint_m2']} m² × {stats['n_floors_total']} seviye "
              f"({stats['n_floors_residential']} konut) | party yüzey: {stats['n_party_surfaces']} "
              f"| {stats['n_windows']} pencere + {stats['n_balcony_doors']} balkon kapısı = "
              f"{stats['window_area_m2']} m² cam | {stats['n_shading_surfaces']} komşu gölge yüzeyi")

        run_dir = mb.OUT_DIR / str(ref)
        if run_dir.exists():
            shutil.rmtree(run_dir)                    # eski koşuyu temizle
        sql_path = run_energyplus(osm, run_dir)

        res = read_results(sql_path, stats["res_area_m2"])
        print(f"[sonuç] ısıtma {res['heating_kwh_m2']} kWh/m² | soğutma {res['cooling_kwh_m2']} kWh/m²")

        # --- QA katmanı: hata taraması + çapraz kontroller + arşiv ------------
        err_stats = scan_err_file(run_dir)            # Severe/Fatal varsa exception
        checks = crosscheck_energyplus(sql_path, stats) + check_plausibility(res)
        qa_ok = write_qa_report(run_dir, stats, res, err_stats, checks)
        write_run_metadata(run_dir, stats, res, params=None)
        print(f"[QA] {'TÜM KONTROLLER GEÇTİ ✓' if qa_ok else 'KONTROL BAŞARISIZ — qa_report.txt!'}"
              f" ({err_stats['warnings']} E+ warning) → {run_dir / 'qa_report.txt'}")

        carbon = carbon_footprint(res, stats["res_area_m2"])
        for i in range(1, len(SYSTEM_SCENARIOS) + 1):
            print(f"[karbon] {carbon[f's{i}_senaryo']}: "
                  f"tüketim {carbon[f's{i}_tuketim_kwh_m2']} kWh/m² → "
                  f"{carbon[f's{i}_co2_kg_m2']} kgCO₂/m²·yıl → bina toplamı "
                  f"{carbon[f's{i}_co2_t_yil']} tCO₂/yıl")

        # Kadastro sertifika değerleriyle karşılaştırma (tanımlar Javier'de doğrulanacak*)
        rec = {k: v for k, v in stats.items() if k != "facade_qa"}   # liste CSV'ye girmez
        rec.update({"eplus_warnings": err_stats["warnings"], "qa_all_pass": qa_ok})
        rec.update({**res, **carbon})
        if COL_HEAT_DEMAND in row.index and pd.notna(row[COL_HEAT_DEMAND]):
            rec["cadastre_heat_kwh_m2"] = float(row[COL_HEAT_DEMAND])
            rec["heat_ratio_model/cadastre"] = round(res["heating_kwh_m2"] / float(row[COL_HEAT_DEMAND]), 2)
        if COL_COOL_DEMAND in row.index and pd.notna(row[COL_COOL_DEMAND]):
            rec["cadastre_cool_kwh_m2"] = float(row[COL_COOL_DEMAND])
        rows.append(rec)

    results = pd.DataFrame(rows)
    mb.OUT_DIR.mkdir(exist_ok=True)
    out_csv = mb.OUT_DIR / "results.csv"
    results.to_csv(out_csv, index=False)

    print("\n===== ÖZET =====")
    print(results.to_string(index=False))
    print(f"\nSonuç dosyası: {out_csv}")
    print(f"Süre: {(datetime.now() - t0).total_seconds():.0f} sn")


if __name__ == "__main__":
    main()
