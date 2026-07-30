"""LHS (Latin Hypercube Sampling) belirsizlik çalışması — pilot bina (v3 tabanı).

NE YAPAR (Ashby Layer 4 = LHS'in koda dökülmüş hali):
  1. Değişken register'ındaki 10 belirsiz girdiyi LHS ile N kez örnekler
     (LHS = tabakalı örnekleme: her değişkenin aralığını N dilime böler, her
     dilimden tam 1 örnek alır → Monte Carlo'dan çok daha az koşuyla düzgün
     kapsama; kaynak: Procedure for LHS + LECCO BIP).
  2. 7 SİMÜLASYON değişkeni için pipeline'ı N kez koşar (model kurulumu
     model_builder_claude'dan, E+ koşusu/sonuç okuma run_simulation_claude'dan
     import edilir — kod tekrarı yok). Komşu gölgelemesi (v3)
     tüm koşularda AÇIK — mahalle dokusu belirsiz değil, GIS'ten kesin.
  3. 3 POST değişkeni (COP/SEER/emisyon faktörü) simülasyon istemez —
     karbon aynı LHS matrisinden analitik hesaplanır.
  4. Çıktılar: out/lhs/runs.csv + histogramlar + Spearman duyarlılık
     (tornado) grafikleri + konsol özeti.

DEĞİŞKEN REGISTER'I (Ashby Layer 2; tam tablo Obsidian deney notunda):
  Energy   : wall_u, roof_u, window_u, window_g, infiltration_ach,
             thermal_bridge_du (v3: TABULA battaniye eki — detay bilinmiyor)
  Society  : shade_setpoint (persiana kullanım alışkanlığı)
  Energy/Economics (post): cop, seer (1974 binasında sistem stoku bilinmiyor)
  Legislation/Environment (post): emission_factor (resmi CTE 0,331 vs 2024 şebekesi ~0,15)

ÇALIŞTIRMA:
  cd ~/valencia-energy-sim
  .venv/bin/python src/reference/lhs_study_claude.py          # N=50 (~6 dk)
  LHS_N=3 .venv/bin/python src/reference/lhs_study_claude.py  # hızlı duman testi
"""

import os
import shutil
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from scipy.stats import qmc, spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_builder_claude as mb      # 3D model kurulumu + paylaşılan CONFIG
import run_simulation_claude as sim    # E+ koşusu + sonuç okuma

# ============================================================================
# DEĞİŞKENLER — (min, maks); hepsi Uniform dağılım (v1 sadeleştirmesi;
# kaynaklar/anlamlar Obsidian register tablosunda)
# ============================================================================

SIM_VARS = {                      # pipeline params'ına giden 7 değişken
    "wall_u": (1.2, 2.0),         # W/m²K — IVE 1960-80 dönem belirsizliği (nominal 1,43)
    "roof_u": (1.4, 2.3),         # W/m²K — düz çatı, yalıtımsız aralık (nominal ~1,9)
    "window_u": (4.5, 5.7),       # W/m²K — tek cam + çerçeve durumu
    "window_g": (0.70, 0.85),     # SHGC — cam kirliliği/tip belirsizliği
    "infiltration_ach": (0.1, 0.5),  # 1/h — 1974 doğrama sızdırmazlığı (nominal 0,2)
    "shade_setpoint": (150.0, 400.0),  # W/m² — persiana kapatma alışkanlığı
    "thermal_bridge_du": (0.0, 0.2),   # W/m²K — v3 TABULA battaniye eki (nominal 0,10;
                                       # 1974 detayları bilinmiyor: 0=ihmal, 0,2=kötü detay)
}
POST_VARS = {                     # simülasyon istemeyen 3 karbon değişkeni
    "cop": (1.0, 3.0),            # ısıtma sistemi verimi (direnç ↔ ısı pompası)
    "seer": (1.8, 3.5),           # soğutma verimi (eski ↔ yeni split)
    "emission_factor": (0.15, 0.331),  # kgCO₂/kWh — 2024 şebekesi ↔ resmi CTE
}
ALL_VARS = {**SIM_VARS, **POST_VARS}

N_RUNS = int(os.environ.get("LHS_N", "50"))
SEED = 42
LHS_DIR = mb.OUT_DIR / "lhs"

# Karşılaştırma çizgileri (grafiklere işlenir)
BASELINE = {"heating": 16.70, "cooling": 18.64, "co2_s1": 8.61, "co2_s2": 4.68}  # v3
CADASTRE = {"heating": 27.97, "cooling": 6.63}   # demanda_ca / demanda__1 (tanım şartlı*)


def sample_matrix() -> pd.DataFrame:
    """9 boyutlu LHS örneklemi üret ve gerçek aralıklara ölçekle."""
    names = list(ALL_VARS)
    sampler = qmc.LatinHypercube(d=len(names), seed=SEED)
    unit = sampler.random(n=N_RUNS)                      # [0,1) hiperküpü
    lows = [ALL_VARS[k][0] for k in names]
    highs = [ALL_VARS[k][1] for k in names]
    scaled = qmc.scale(unit, lows, highs)                # gerçek aralıklara
    return pd.DataFrame(scaled, columns=names)


def main():
    samples = sample_matrix()
    print(f"[lhs] {N_RUNS} koşu × {len(ALL_VARS)} değişken (seed={SEED})")

    # Bina + komşular + party wall BİR kez hazırlanır (döngüde değişmez) —
    # v3: komşular gölge kütleleri için de lazım; 50 koşuda 50 kez shapefile
    # okumamak için tek okuma yapılıp build_model'e geçirilir.
    buildings = mb.load_buildings(mb.BUILDINGS_GPKG)
    row = buildings.iloc[0]
    geom = mb.clean_polygon(row.geometry)
    neighbors = mb.load_neighbors(geom, row["refparcela"], mb.NEIGHBORS_SHP)
    party = mb.find_party_walls(geom, row["refparcela"], mb.NEIGHBORS_SHP, neighbors=neighbors)

    if LHS_DIR.exists():
        shutil.rmtree(LHS_DIR)
    LHS_DIR.mkdir(parents=True)

    records = []
    for i, s in samples.iterrows():
        params = {k: float(s[k]) for k in SIM_VARS}
        # 2026-07-10: LHS bilinçli olarak MASSLESS duvar/çatı kullanır (U etkisini
        # izole etme sadeleştirmesi + donmuş N=50 çalışmasıyla tutarlılık).
        # Deterministik koşular (pilot/N-bina/şehir) artık gerçek katmanlı kurulur.
        params["massless"] = True
        osm, stats = mb.build_model(row, party, params, neighbors=neighbors)
        run_dir = LHS_DIR / f"run_{i:02d}"
        sql = sim.run_energyplus(osm, run_dir)
        res = sim.read_results(sql, stats["res_area_m2"])

        # Karbon: post değişkenler aynı LHS satırından (senaryo yerine sürekli aralık)
        cons = res["heating_kwh_m2"] / s["cop"] + res["cooling_kwh_m2"] / s["seer"]
        co2_kg_m2 = cons * s["emission_factor"]
        rec = {**{k: round(float(s[k]), 4) for k in ALL_VARS},
               "heating_kwh_m2": res["heating_kwh_m2"],
               "cooling_kwh_m2": res["cooling_kwh_m2"],
               "consumption_kwh_m2": round(cons, 2),
               "co2_kg_m2": round(co2_kg_m2, 2),
               "co2_t_bina": round(co2_kg_m2 * stats["res_area_m2"] / 1000.0, 1)}
        records.append(rec)
        print(f"[lhs] koşu {i+1}/{N_RUNS}: ısıtma {res['heating_kwh_m2']:.1f} | "
              f"soğutma {res['cooling_kwh_m2']:.1f} | CO₂ {co2_kg_m2:.1f} kg/m²")
        shutil.rmtree(run_dir, ignore_errors=True)       # disk tasarrufu (sql'ler büyük)

    df = pd.DataFrame(records)
    df.to_csv(LHS_DIR / "runs.csv", index=False)

    # --- Histogramlar ----------------------------------------------------------
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    panels = [
        ("heating_kwh_m2", "Isıtma talebi (kWh/m²·yıl)",
         [("v3 baseline", BASELINE["heating"], "tab:blue"), ("kadastro*", CADASTRE["heating"], "tab:red")]),
        ("cooling_kwh_m2", "Soğutma talebi (kWh/m²·yıl)",
         [("v3 baseline", BASELINE["cooling"], "tab:blue"), ("kadastro*", CADASTRE["cooling"], "tab:red")]),
        ("co2_kg_m2", "Karbon (kgCO₂/m²·yıl)",
         [("S1 direnç", BASELINE["co2_s1"], "tab:orange"), ("S2 ısı pompası", BASELINE["co2_s2"], "tab:green")]),
    ]
    for ax, (col, title, lines) in zip(axes, panels):
        ax.hist(df[col], bins=12, color="#8ab4d8", edgecolor="#345")
        for label, x, c in lines:
            ax.axvline(x, color=c, linestyle="--", linewidth=1.6, label=label)
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=8)
    fig.suptitle(f"LHS belirsizlik dağılımları — pilot bina, N={N_RUNS} (*kadastro tanımı doğrulanmadı)")
    fig.tight_layout()
    fig.savefig(LHS_DIR / "histograms.png", dpi=110)

    # --- Spearman duyarlılık (tornado) ------------------------------------------
    # Sıra korelasyonu: |rho| yüksek = o değişken çıktıyı güçlü sürüklüyor.
    outputs = [("heating_kwh_m2", list(SIM_VARS)),
               ("cooling_kwh_m2", list(SIM_VARS)),
               ("co2_kg_m2", list(ALL_VARS))]
    fig, axes = plt.subplots(1, 3, figsize=(17, 5))
    sens_summary = {}
    for ax, (col, varlist) in zip(axes, outputs):
        rhos = {v: spearmanr(df[v], df[col]).statistic for v in varlist}
        ordered = sorted(rhos.items(), key=lambda kv: abs(kv[1]))
        names = [k for k, _ in ordered]
        vals = [v for _, v in ordered]
        ax.barh(names, vals, color=["#d64545" if v > 0 else "#4a90d9" for v in vals])
        ax.axvline(0, color="#333", linewidth=0.8)
        ax.set_title(f"{col} — Spearman ρ", fontsize=11)
        ax.set_xlim(-1, 1)
        sens_summary[col] = sorted(rhos.items(), key=lambda kv: -abs(kv[1]))[:3]
    fig.suptitle("Duyarlılık: hangi belirsiz girdi hangi çıktıyı sürüklüyor? (kırmızı=artırır, mavi=azaltır)")
    fig.tight_layout()
    fig.savefig(LHS_DIR / "tornado.png", dpi=110)

    # --- Konsol özeti ------------------------------------------------------------
    print("\n===== LHS ÖZETİ =====")
    for col in ("heating_kwh_m2", "cooling_kwh_m2", "co2_kg_m2", "co2_t_bina"):
        q = df[col].quantile
        print(f"{col:20s} ort {df[col].mean():7.2f} | medyan {q(0.5):7.2f} | "
              f"P5 {q(0.05):7.2f} | P95 {q(0.95):7.2f}")
    print("\nEn güçlü 3 sürükleyici (|Spearman ρ|):")
    for col, top in sens_summary.items():
        pretty = ", ".join(f"{k} ({v:+.2f})" for k, v in top)
        print(f"  {col}: {pretty}")
    print(f"\nÇıktılar: {LHS_DIR}/runs.csv · histograms.png · tornado.png")


if __name__ == "__main__":
    main()
