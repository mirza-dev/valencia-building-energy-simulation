# -*- coding: utf-8 -*-
"""
N-BİNA MAHALLE PIPELINE'I — Benicalap (temsilî model yöntemi)   [REFERANS KOD]
==============================================================================
Bu proje bina bina simülasyon YAPMAZ (AGENTS.md yöntemi + Sustainability 2021
±%9 emsali). Akış:

  1. Benicalap stoku yüklenir ve temizlenir (959 bina, 18 cluster).
  2. Her cluster için 1 TEMSİLÎ bina seçilir (alan/kat medyanına en yakın).
  3. Temsilî bina KENDİ gerçek konumunda modellenir (kendi komşuları →
     party wall + 50 m gölge) ve E+ ile koşulur → cluster kWh/m².
  4. Sonuç GIS'e geri bağlanır: her bina = cluster kWh/m² × KONUT ALANI
     (Tipo15 daire kütüğünden) → bina bazlı enerji + karbon → mahalle toplamı.
  5. Validasyon: bina bazlı `demanda_ca` sertifika kolonu + mahalle toplamı.

Dönem/tipoloji U-değerleri: TABULA España (IVE) tipoloji broşürü, "ESTADO
ORIGINAL" föyleri — https://episcope.eu (ES_TABULA_TypologyBrochure_IVE.pdf).
NOT: Javier'in tam veritabanı (`Valencia_2051.db`) gelirse SADECE aşağıdaki
TABULA_ES tablosu güncellenir (elimizdeki EU DB export'unda U-değeri YOK —
Pablo'nun 2025-11-06 maili: U'lar CS/UR/AR sonekleriyle tam veritabanında).

Kullanım:  .venv/bin/python src/reference/neighborhood_pipeline_claude.py
Süre: ~10-15 dk (18 E+ koşusu, sıralı).
"""

from datetime import datetime
from pathlib import Path
import re
import shutil
import sys

import geopandas as gpd
import pandas as pd

# Pilot pipeline'ın iki modülü AYNEN kullanılır (yeniden yazım yok):
#   mb  = 3D model kurucu (geometri, party wall, gölge, pencere, params)
#   sim = E+ koşusu + sonuç okuma + QA + karbon
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_builder_claude as mb          # noqa: E402
import run_simulation_claude as sim        # noqa: E402

# ============================================================================
# CONFIG
# ============================================================================
BOUNDARY_GPKG = mb.PROJECT / "data/gis/benicalap_boundary.gpkg"
TIPO15_CSV    = mb.PROJECT / "data/reference/Tipo15_soloV(in).csv"
OUT_DIR       = mb.OUT_DIR / "neighborhood"

# --- TABULA España (IVE) — ESTADO ORIGINAL U-değerleri [W/m²K] ---------------
# Kaynak: ES_TABULA_TypologyBrochure_IVE.pdf (episcope.eu), föy başına Fachada /
# Cubierta / Huecos. Çatıda föyde 'Cubierta plana' varsa o alındı (Benicalap
# bloklarında düz çatı hakim; modelin çatısı da düz), yoksa föyün tek çatısı.
# Tipoloji eşlemesi (tipologia_ kolonuyla doğrulandı):
#   BlocPluri = 'Plurifamiliar >= 3 plantas' → TABULA 'Bloque en altura'
#   EdiPluri  = 'Plurifamiliar < 3 plantas'  → TABULA 'Edificio plurifamiliar'
#   VivUni    = 'Unifamiliar'                → TABULA 'Vivienda unifamiliar
#               ADOSADA' (Benicalap dokusu sıra evi; 'aislada' değil — varsayım,
#               deney notunda gerekçeli).
# Dönem eşlemesi (şehir ano_cons_1 ↔ TABULA föyleri; sınırlar ±1-4 yıl kayık,
# kabul edilen yaklaşıklık): P01<1900 · P02 1901-40↔1901-36 · P03 1941-60↔
# 1937-59 · P04 1961-80↔1960-79 · P05 1981-2007↔1980-2006 · P06 2008-20 ve
# P07 2021+ ↔ 'Posterior a 2006' (en yakın föy).
# window_g: tek cam (U≥4) → 0.82 (künye §7.0.1); çift cam → 0.75 (tipik berrak
# çift cam; TABULA föyleri g vermiyor).
TABULA_ES = {
    # family        period  wall_u  roof_u  window_u
    ("VivUni",    "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.00),
    ("VivUni",    "P02"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni",    "P03"): dict(wall_u=2.56, roof_u=4.17, window_u=4.59),
    ("VivUni",    "P04"): dict(wall_u=1.33, roof_u=1.67, window_u=5.70),
    ("VivUni",    "P05"): dict(wall_u=0.72, roof_u=1.92, window_u=3.04),
    ("VivUni",    "P06"): dict(wall_u=0.47, roof_u=0.48, window_u=2.92),
    ("EdiPluri",  "P01"): dict(wall_u=2.56, roof_u=1.60, window_u=5.35),
    ("EdiPluri",  "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("EdiPluri",  "P03"): dict(wall_u=2.94, roof_u=1.67, window_u=5.70),
    ("EdiPluri",  "P04"): dict(wall_u=1.64, roof_u=1.61, window_u=5.70),
    ("EdiPluri",  "P05"): dict(wall_u=0.62, roof_u=0.56, window_u=3.37),
    ("EdiPluri",  "P06"): dict(wall_u=0.52, roof_u=0.45, window_u=3.54),
    ("BlocPluri", "P01"): dict(wall_u=2.56, roof_u=4.17, window_u=5.35),
    ("BlocPluri", "P02"): dict(wall_u=2.56, roof_u=3.08, window_u=5.35),
    ("BlocPluri", "P03"): dict(wall_u=2.27, roof_u=1.37, window_u=4.72),
    ("BlocPluri", "P04"): dict(wall_u=1.33, roof_u=1.92, window_u=5.70),
    ("BlocPluri", "P05"): dict(wall_u=0.58, roof_u=0.60, window_u=3.37),
    ("BlocPluri", "P06"): dict(wall_u=0.48, roof_u=0.47, window_u=3.29),
}
PERIOD_FALLBACK = {"P07": "P06"}   # P07 (2021+) föyü yok → en yakın dönem

# Zemin kat kuralı (aile bazında varsayılan; temsilî binada Tipo15 planta-0
# kaydı varsa ONA göre düzeltilir — veri > varsayım):
GROUND_UNCONDITIONED_BY_FAMILY = {"VivUni": False, "EdiPluri": True, "BlocPluri": True}

CLUSTER_RE = re.compile(r"^(VivUni|EdiPluri|BlocPluri)(P\d\d)$")


# ============================================================================
# 1) STOK HAZIRLAMA — yükle, temizle, konut alanını bağla
# ============================================================================

def load_stock() -> gpd.GeoDataFrame:
    """Benicalap bina stoku: sınır filtresi + kat imputasyonu + Tipo15 join.

    Temizlik kuralları (hepsi bayraklı — sonuç dosyasında görünür):
      - cluster deseni bozuksa bina DIŞLANIR (raporlanır).
      - altura_max<1 (51 bina): cluster'ın Benicalap-içi sıfır-dışı MEDYANI
        yazılır, `imputed_floors=True` işaretlenir. (Kat=0 kadastro eksiği;
        binanın 0 katı olamaz.)
      - Konut alanı: Tipo15 daire kütüğü `442_sup_Residencial` parsel toplamı
        (kapsama %98.9). Kapsanmayan binaya cluster'ının medyan
        (konut alanı / taban alanı) oranı × kendi taban alanı yazılır,
        `res_area_proxy=True` işaretlenir.
    """
    city = gpd.read_file(mb.NEIGHBORS_SHP)
    bnd = gpd.read_file(BOUNDARY_GPKG).to_crs(city.crs)
    stock = city[city.geometry.centroid.within(bnd.union_all())].copy()
    print(f"[stok] Benicalap: {len(stock)} bina (şehir {len(city)}).")

    # Cluster ayrıştır (aile + dönem)
    parsed = stock["cluster"].str.extract(CLUSTER_RE)
    bad = parsed[0].isna()
    if bad.any():
        print(f"[stok] UYARI: {bad.sum()} binada tanınmayan cluster etiketi → dışlandı: "
              f"{sorted(stock.loc[bad, 'cluster'].unique())}")
        stock = stock[~bad].copy()
        parsed = parsed[~bad]
    stock["family"], stock["period"] = parsed[0], parsed[1]

    # Kat imputasyonu
    stock["altura_max"] = pd.to_numeric(stock["altura_max"], errors="coerce").fillna(0).astype(int)
    stock["imputed_floors"] = stock["altura_max"] < 1
    med_floors = (stock[stock["altura_max"] >= 1]
                  .groupby("cluster")["altura_max"].median())
    for cl, med in med_floors.items():
        m = (stock["cluster"] == cl) & stock["imputed_floors"]
        stock.loc[m, "altura_max"] = int(round(med))
    still = stock["imputed_floors"] & (stock["altura_max"] < 1)
    if still.any():   # cluster'ın tamamı 0 katsa: aile medyanı, o da yoksa 1
        fam_med = stock[stock["altura_max"] >= 1].groupby("family")["altura_max"].median()
        for fam, med in fam_med.items():
            stock.loc[still & (stock["family"] == fam), "altura_max"] = max(1, int(round(med)))
    print(f"[stok] kat imputasyonu: {int(stock['imputed_floors'].sum())} bina "
          f"(cluster medyanı yazıldı).")

    # Tipo15 konut alanı join'i
    t15 = pd.read_csv(TIPO15_CSV, sep=";", encoding="latin-1",
                      usecols=["31_pc", "442_sup_Residencial"], dtype={"31_pc": str})
    res_by_parcel = t15.groupby("31_pc")["442_sup_Residencial"].sum()
    stock["res_area_m2"] = stock["refparcela"].map(res_by_parcel)
    stock["res_area_proxy"] = stock["res_area_m2"].isna()

    # Proxy: cluster medyan (konut alanı / taban alanı) oranı
    known = stock[~stock["res_area_proxy"]]
    ratio_by_cluster = (known["res_area_m2"] / known["Shape_Area"]).groupby(known["cluster"]).median()
    ratio_global = float((known["res_area_m2"] / known["Shape_Area"]).median())
    for idx in stock.index[stock["res_area_proxy"]]:
        r = ratio_by_cluster.get(stock.at[idx, "cluster"], ratio_global)
        stock.at[idx, "res_area_m2"] = float(stock.at[idx, "Shape_Area"]) * float(r)
    print(f"[stok] Tipo15 konut alanı: {(~stock['res_area_proxy']).sum()} bina kütükten, "
          f"{int(stock['res_area_proxy'].sum())} bina oran-proxy | "
          f"toplam {stock['res_area_m2'].sum():,.0f} m².")
    return stock


# ============================================================================
# 2) DÖNEM PARAMETRELERİ — cluster → build_model params sözlüğü
# ============================================================================

def period_params(family: str, period: str) -> dict:
    """TABULA_ES tablosundan build_model params'ı üret.

    - wall_u/roof_u: builder bunları MASSLESS eşdeğere çevirir (ısıl kütle
      ihmal edilir — LHS notundaki bilinen sınırlama; TÜM cluster'lara aynı
      yöntem uygulanır ki karşılaştırma içsel tutarlı olsun. Pilot çalışma
      (params=None, masif template) ayrı çalışmadır, 16.67/18.67 çapası durur).
    - Termal köprü ΔU=0.10 builder'da wall_u'ya OTOMATİK eklenir (default) —
      burada tabloya ekleme YAPMA (çift sayma olur).
    - window_g: tek cam 0.82, çift cam 0.75 (CONFIG notu).
    """
    key = (family, PERIOD_FALLBACK.get(period, period))
    if key not in TABULA_ES:
        raise KeyError(f"TABULA_ES'te yok: {key} — tablo eksik mi?")
    u = TABULA_ES[key]
    return {
        "wall_u": u["wall_u"],
        "roof_u": u["roof_u"],
        "window_u": u["window_u"],
        "window_g": 0.82 if u["window_u"] >= 4.0 else 0.75,
        "ground_unconditioned": GROUND_UNCONDITIONED_BY_FAMILY[family],
    }


def ground_rule_from_tipo15(refparcela: str) -> bool | None:
    """Temsilî bina için zemin kuralını VERİDEN türet: Tipo15'te bu parselde
    planta-0 (zemin) konut kaydı VARSA zemin konuttur (ground_unconditioned=
    False). Kayıt hiç yoksa None döner (aile varsayılanı kullanılır)."""
    t15 = pd.read_csv(TIPO15_CSV, sep=";", encoding="latin-1",
                      usecols=["31_pc", "252_planta"], dtype=str)
    rows = t15[t15["31_pc"] == refparcela]
    if rows.empty:
        return None
    plantas = rows["252_planta"].fillna("").str.strip().str.upper()
    has_ground_dwelling = plantas.isin({"0", "00", "BJ", "B", "BX"}).any()
    return not has_ground_dwelling


# ============================================================================
# 3) TEMSİLÎ BİNA SEÇİMİ — cluster başına 1 bina
# ============================================================================

def select_representatives(stock: gpd.GeoDataFrame) -> pd.DataFrame:
    """Her cluster için medyan-tipik binayı seç.

    Skor (küçük = iyi): |alan−medyan|/medyan + |kat−medyan|/medyan
                        + 0.01 × köşe sayısı (basit geometri hafif tercih).
    Şartlar: kat imputesiz (gerçek veri) + geometri pre-flight'ı geçer
    (clean_polygon → prepare_footprint → alan bandı). Aday skor sırasıyla
    denenir; ilk geçen temsilî olur (select_building.py mantığının genellemesi).
    """
    reps = []
    for cluster, sub in stock.groupby("cluster"):
        cand = sub[~sub["imputed_floors"]].copy()
        if cand.empty:                      # cluster'ın tamamı imputeliyse mecburen hepsi
            cand = sub.copy()
        med_area = cand["Shape_Area"].median()
        med_fl = cand["altura_max"].median()
        cand["n_vertices"] = cand.geometry.apply(
            lambda g: len(mb.clean_polygon(g).exterior.coords) - 1)
        cand["score"] = (abs(cand["Shape_Area"] - med_area) / max(med_area, 1)
                         + abs(cand["altura_max"] - med_fl) / max(med_fl, 1)
                         + 0.01 * cand["n_vertices"])
        chosen, reason = None, None
        for _, row in cand.sort_values("score").iterrows():
            try:
                mb.validate_building_row(row)
                mb.prepare_footprint(mb.clean_polygon(row.geometry))
                chosen, reason = row, "pre-flight OK"
                break
            except Exception as e:          # geçemeyen aday atlanır, sıradaki denenir
                reason = f"aday atlandı: {e}"
        if chosen is None:
            raise RuntimeError(f"{cluster}: hiçbir aday pre-flight'ı geçemedi ({reason})")
        reps.append({
            "cluster": cluster, "family": chosen["family"], "period": chosen["period"],
            "refparcela": chosen["refparcela"], "n_buildings": len(sub),
            "rep_area_m2": round(float(chosen["Shape_Area"]), 1),
            "cluster_med_area_m2": round(float(med_area), 1),
            "rep_floors": int(chosen["altura_max"]), "cluster_med_floors": float(med_fl),
            "rep_vertices": int(chosen["n_vertices"]),
        })
    df = pd.DataFrame(reps).sort_values("n_buildings", ascending=False)
    print(f"[seçim] {len(df)} cluster için temsilî bina seçildi.")
    return df


# ============================================================================
# 4) BATCH KOŞU — temsilî binaları pilotla AYNI zincirden geçir
# ============================================================================

def run_representative(stock: gpd.GeoDataFrame, rep: pd.Series) -> dict:
    """Tek temsilî binayı koş: build → E+ → oku → QA → karbon.
    run_simulation_claude.main() ile satır satır aynı zincir — tek fark
    dönem params'ı ve çıktı klasörü (out/neighborhood/<cluster>/)."""
    row = stock[stock["refparcela"] == rep["refparcela"]].iloc[0]
    ref, cluster = row["refparcela"], rep["cluster"]
    print(f"\n===== {cluster} → temsilî {ref} ({rep['n_buildings']} bina) =====")

    params = period_params(rep["family"], rep["period"])
    g_data = ground_rule_from_tipo15(ref)          # veri > aile varsayımı
    if g_data is not None and g_data != params["ground_unconditioned"]:
        print(f"[zemin] Tipo15 kaydına göre düzeltildi: ground_unconditioned={g_data}")
        params["ground_unconditioned"] = g_data
    print(f"[params] duvar U={params['wall_u']} çatı U={params['roof_u']} "
          f"pencere U={params['window_u']}/g={params['window_g']} "
          f"zemin-tampon={params['ground_unconditioned']}")

    geom = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geom, ref, mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geom, ref, mb.NEIGHBORS_SHP, neighbors=neighbors)

    osm, stats = mb.build_model(row, party, params=params, neighbors=neighbors)
    print(f"[model] taban {stats['footprint_m2']} m² × {stats['n_floors_total']} seviye | "
          f"party: {stats['n_party_surfaces']} | cam {stats['window_area_m2']} m² | "
          f"gölge {stats['n_shading_surfaces']}")

    run_dir = OUT_DIR / cluster
    if run_dir.exists():
        shutil.rmtree(run_dir)
    sql_path = sim.run_energyplus(osm, run_dir)

    res = sim.read_results(sql_path, stats["res_area_m2"])
    err_stats = sim.scan_err_file(run_dir)         # Severe/Fatal → exception
    checks = sim.crosscheck_energyplus(sql_path, stats) + sim.check_plausibility(res)
    qa_ok = sim.write_qa_report(run_dir, stats, res, err_stats, checks)
    sim.write_run_metadata(run_dir, stats, res, params=params)
    carbon = sim.carbon_footprint(res, stats["res_area_m2"])
    print(f"[sonuç] ısıtma {res['heating_kwh_m2']} / soğutma {res['cooling_kwh_m2']} kWh/m² "
          f"| QA {'✓' if qa_ok else 'FAIL — qa_report.txt!'}")

    return {**rep.to_dict(),
            "heating_kwh_m2": res["heating_kwh_m2"],
            "cooling_kwh_m2": res["cooling_kwh_m2"],
            "qa_all_pass": qa_ok, "eplus_warnings": err_stats["warnings"],
            "s1_co2_kg_m2": carbon["s1_co2_kg_m2"], "s2_co2_kg_m2": carbon["s2_co2_kg_m2"],
            **{f"param_{k}": v for k, v in params.items()}}


# ============================================================================
# 5) ÖLÇEKLEME — cluster kWh/m² → 959 binaya GIS join
# ============================================================================

def scale_to_stock(stock: gpd.GeoDataFrame, cluster_results: pd.DataFrame) -> gpd.GeoDataFrame:
    """Yöntemin kalbi (kasıtlı granularity mismatch): tipoloji seviyesinde
    simüle et, bina seviyesinde uygula. Her bina kendi cluster'ının kWh/m²
    değerini KENDİ konut alanıyla çarpar; karbon aynı alanla ölçeklenir."""
    per_cluster = cluster_results.set_index("cluster")
    out = stock.copy()
    for col in ("heating_kwh_m2", "cooling_kwh_m2", "s1_co2_kg_m2", "s2_co2_kg_m2"):
        out[col] = out["cluster"].map(per_cluster[col])
    out["heating_kwh"] = out["heating_kwh_m2"] * out["res_area_m2"]
    out["cooling_kwh"] = out["cooling_kwh_m2"] * out["res_area_m2"]
    out["s1_co2_t"] = out["s1_co2_kg_m2"] * out["res_area_m2"] / 1000.0
    out["s2_co2_t"] = out["s2_co2_kg_m2"] * out["res_area_m2"] / 1000.0
    return out


# ============================================================================
# 6) VALİDASYON — sertifika kolonu + mahalle toplamı
# ============================================================================

def validate(buildings: gpd.GeoDataFrame, cluster_results: pd.DataFrame) -> str:
    """İki seviye:
    (a) bina bazlı: model ısıtma kWh/m² ↔ `demanda_ca` (SARTLI — kolon tanımı
        hâlâ Javier'de; pilot bulgusu: 27.97 model dağılımının dışındaydı).
    (b) mahalle: toplam talep + tipoloji kırılımı (makale beklentisi: EdiPluri/
        BlocPluri baskın; Sustainability 2021'de P04 çok-aileli ~%59'du).
    """
    L = ["", "=" * 70, "VALİDASYON", "=" * 70]

    ok = buildings[COL_CERT].notna() & (buildings[COL_CERT] > 0)
    L.append(f"\n(a) Bina bazlı — model ↔ sertifika `demanda_ca` ({int(ok.sum())} bina):")
    grp = buildings[ok].groupby("cluster").agg(
        n=("cluster", "size"),
        model=("heating_kwh_m2", "first"),
        cert_med=(COL_CERT, "median"))
    grp["oran_model/cert"] = (grp["model"] / grp["cert_med"]).round(2)
    L.append(grp.round(2).to_string())
    w_model = (buildings.loc[ok, "heating_kwh_m2"] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    w_cert = (buildings.loc[ok, COL_CERT] * buildings.loc[ok, "res_area_m2"]).sum() \
        / buildings.loc[ok, "res_area_m2"].sum()
    L.append(f"\nAlan-ağırlıklı ısıtma: model {w_model:.2f} vs sertifika {w_cert:.2f} kWh/m² "
             f"(oran {w_model / w_cert:.2f}) — tanım farkı şerhiyle oku (Javier sorusu 3).")

    L.append(f"\n(b) Mahalle toplamları ({len(buildings)} bina, "
             f"{buildings['res_area_m2'].sum():,.0f} m² konut alanı):")
    tot_h = buildings["heating_kwh"].sum() / 1e6
    tot_c = buildings["cooling_kwh"].sum() / 1e6
    L.append(f"  Isıtma talebi : {tot_h:8.2f} GWh/yıl")
    L.append(f"  Soğutma talebi: {tot_c:8.2f} GWh/yıl")
    L.append(f"  Karbon S1 (direnç+split): {buildings['s1_co2_t'].sum():8.0f} tCO₂/yıl")
    L.append(f"  Karbon S2 (ısı pompası) : {buildings['s2_co2_t'].sum():8.0f} tCO₂/yıl")

    L.append("\n  Tipoloji ailesi kırılımı (ısıtma+soğutma talebi payı):")
    tot = (buildings["heating_kwh"] + buildings["cooling_kwh"]).sum()
    fam = (buildings.groupby("family")
           .apply(lambda d: (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100,
                  include_groups=False).round(1))
    for f_, v in fam.sort_values(ascending=False).items():
        L.append(f"    {f_:10s}: %{v}")
    top = (buildings.groupby("cluster")
           .apply(lambda d: (d["heating_kwh"] + d["cooling_kwh"]).sum() / tot * 100,
                  include_groups=False).round(1).sort_values(ascending=False))
    L.append(f"  En büyük cluster payı: {top.index[0]} %{top.iloc[0]} "
             f"(Sustainability 2021 emsali: P04 çok-aileli ~%59 idi)")
    return "\n".join(L)


COL_CERT = "demanda_ca"   # sertifika ısıtma talebi kolonu (tanım Javier'de — SARTLI)


# ============================================================================
# 7) MAIN
# ============================================================================

def main():
    t0 = datetime.now()
    mb.validate_input_files()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stock = load_stock()
    reps = select_representatives(stock)
    reps.to_csv(OUT_DIR / "representatives.csv", index=False)

    results = []
    for _, rep in reps.iterrows():
        results.append(run_representative(stock, rep))
    cluster_results = pd.DataFrame(results)
    cluster_results.to_csv(OUT_DIR / "clusters_results.csv", index=False)

    buildings = scale_to_stock(stock, cluster_results)
    keep = ["refparcela", "cluster", "family", "period", "altura_max", "imputed_floors",
            "res_area_m2", "res_area_proxy", "heating_kwh_m2", "cooling_kwh_m2",
            "heating_kwh", "cooling_kwh", "s1_co2_kg_m2", "s2_co2_kg_m2",
            "s1_co2_t", "s2_co2_t", COL_CERT, "geometry"]
    buildings[keep].to_file(OUT_DIR / "results_buildings.gpkg", driver="GPKG")

    report = validate(buildings, cluster_results)
    print(report)
    summary = (f"N-BİNA MAHALLE SONUCU — Benicalap ({datetime.now():%Y-%m-%d %H:%M})\n"
               f"{len(stock)} bina, {len(reps)} cluster/temsilî simülasyon\n"
               + cluster_results[["cluster", "n_buildings", "heating_kwh_m2",
                                  "cooling_kwh_m2", "qa_all_pass"]].to_string(index=False)
               + "\n" + report + "\n")
    (OUT_DIR / "summary.txt").write_text(summary)

    print(f"\nÇıktılar: {OUT_DIR}/(representatives|clusters_results).csv, "
          f"results_buildings.gpkg (QGIS'te aç), summary.txt")
    print(f"Süre: {(datetime.now() - t0).total_seconds() / 60:.1f} dk")


if __name__ == "__main__":
    main()
