"""ŞEHİR ÖLÇEĞİ 3D MODEL ÜRETİMİ — Tüm Valencia (26.452 bina / 21 cluster).

PARÇA A'NIN GENELLEŞTİRİLMESİ (SADECE MODEL KURMA — SİMÜLASYON YOK):
Bu script pilotta doğrulanan model-kurma zincirini (load_neighbors →
find_party_walls → build_model → save_model) Valencia'nın TAMAMINA taşır.
EnergyPlus koşusu, QA-E+ çaprazı ve karbon katmanı BİLEREK dışarıda —
kullanıcı kararı (2026-07-08): önce model üretimi şehir ölçeğinde
genelleştirilip GÖZLE doğrulanır, simülasyon katmanı sonra eklenir.

YÖNTEM (AGENTS: temsilî model — bina bina DEĞİL):
  1. Şehir stoku yüklenir: 26.452 bina, cluster parse + kat imputasyonu +
     Tipo15 konut alanı join'i (Benicalap load_stock'unun sınır-filtresiz hali).
  2. Her cluster için medyan-tipik temsilî bina seçilir (21 cluster —
     Benicalap'taki 18 + BlocPluriP01, VivUniP07, EdiPluriP07).
  3. Her temsilî için dönem-uyumlu 3D OpenStudio modeli kurulur ve kaydedilir:
     out/city_models/<cluster>/model_python.osm + model_3d.png (4 açı render).

YENİDEN KULLANIM (bu dosya YENİ mantık içermez, sadece kapsamı genişletir):
  - model_builder_claude (mb): geometri zinciri + save_model — pilotla birebir.
  - neighborhood_pipeline_claude (nbp): TABULA_ES U-değerleri, period_params,
    ground_rule_from_tipo15, select_representatives — Benicalap'la birebir.
  - plot_model_claude (pm): surface_polys renk kodu — pilot renderıyla birebir.

ÇALIŞTIRMA:
  .venv/bin/python src/reference/city_model_builder_claude.py           # 21 model
  .venv/bin/python src/reference/city_model_builder_claude.py 4252702YJ2745A
                                    # tek bina: refparcela ver, modeli kur
Süre: ~3-4 dk (21 model + render). EnergyPlus GEREKMEZ.
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_builder_claude as mb              # noqa: E402
import neighborhood_pipeline_claude as nbp     # noqa: E402
import plot_model_claude as pm                 # noqa: E402

import matplotlib                              # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402
import numpy as np                             # noqa: E402

# ============================================================================
# CONFIG — sadece kapsam/çıktı; fizik ve eşikler mb/nbp'den gelir
# ============================================================================
OUT_DIR = mb.OUT_DIR / "city_models"

# ============================================================================
# 1) ŞEHİR STOKU — nbp.load_stock'un SINIR FİLTRESİZ hali
# ============================================================================

def load_stock_city() -> gpd.GeoDataFrame:
    """Tüm Valencia bina stoku. nbp.load_stock ile TEK fark: Benicalap sınır
    filtresi yok (26.452 binanın tamamı) + duplike refparcela işlenir.

    Temizlik kuralları (Benicalap'la aynı, hepsi bayraklı):
      - cluster deseni bozuksa bina DIŞLANIR (raporlanır).
      - altura_max<1 (şehirde 1.641 bina): cluster medyanı, `imputed_floors`.
      - Konut alanı: Tipo15 parsel toplamı (%99.3 kapsama); kapsanmayana
        cluster medyan oranı × taban alanı, `res_area_proxy`.
      - Duplike refparcela (7 satır): parsel konut alanı satırlara taban
        alanı oranında BÖLÜŞTÜRÜLÜR (çift sayma önlenir), `dup_refparcela`.
    """
    stock = gpd.read_file(mb.NEIGHBORS_SHP)
    print(f"[stok] Tüm Valencia: {len(stock)} bina.")

    # Cluster ayrıştır (aile + dönem) — nbp ile aynı regex
    parsed = stock["cluster"].str.extract(nbp.CLUSTER_RE)
    bad = parsed[0].isna()
    if bad.any():
        print(f"[stok] UYARI: {bad.sum()} binada tanınmayan cluster etiketi → dışlandı: "
              f"{sorted(stock.loc[bad, 'cluster'].unique())}")
        stock = stock[~bad].copy()
        parsed = parsed[~bad]
    stock["family"], stock["period"] = parsed[0], parsed[1]

    # Kat imputasyonu — nbp.load_stock ile aynı kural
    stock["altura_max"] = pd.to_numeric(stock["altura_max"], errors="coerce").fillna(0).astype(int)
    stock["imputed_floors"] = stock["altura_max"] < 1
    med_floors = (stock[stock["altura_max"] >= 1]
                  .groupby("cluster")["altura_max"].median())
    for cl, med in med_floors.items():
        m = (stock["cluster"] == cl) & stock["imputed_floors"]
        stock.loc[m, "altura_max"] = int(round(med))
    still = stock["imputed_floors"] & (stock["altura_max"] < 1)
    if still.any():
        fam_med = stock[stock["altura_max"] >= 1].groupby("family")["altura_max"].median()
        for fam, med in fam_med.items():
            stock.loc[still & (stock["family"] == fam), "altura_max"] = max(1, int(round(med)))
    print(f"[stok] kat imputasyonu: {int(stock['imputed_floors'].sum())} bina.")

    # Tipo15 konut alanı join'i — nbp ile aynı; ek: duplike parsel bölüştürme
    t15 = pd.read_csv(nbp.TIPO15_CSV, sep=";", encoding="latin-1",
                      usecols=["31_pc", "442_sup_Residencial"], dtype={"31_pc": str})
    res_by_parcel = t15.groupby("31_pc")["442_sup_Residencial"].sum()
    stock["res_area_m2"] = stock["refparcela"].map(res_by_parcel)
    stock["res_area_proxy"] = stock["res_area_m2"].isna()

    stock["dup_refparcela"] = stock["refparcela"].duplicated(keep=False)
    if stock["dup_refparcela"].any():
        # Aynı parsel referansını taşıyan satırlar Tipo15'ten AYNI alanı almıştı
        # → parsel alanı taban alanı oranında paylaştırılır (toplam korunur).
        for ref, grp in stock[stock["dup_refparcela"]].groupby("refparcela"):
            total = res_by_parcel.get(ref)
            if total is None or pd.isna(total):
                continue
            w = grp["Shape_Area"] / grp["Shape_Area"].sum()
            stock.loc[grp.index, "res_area_m2"] = total * w
        print(f"[stok] duplike refparcela: {int(stock['dup_refparcela'].sum())} satır — "
              f"konut alanı taban oranında bölüştürüldü.")

    # Proxy: cluster medyan (konut alanı / taban alanı) oranı — nbp ile aynı
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
# 2) TEMSİLÎ SEÇİMİ — nbp.select_representatives'in şehir-dayanıklı hali
# ============================================================================
# Şehir stoğunda 6 binada iç avlu (delikli poligon) var (BlocPluriP03/04/05);
# fromFloorPrint delik desteklemediği için clean_polygon bunlarda exception
# atar. nbp'nin köşe-sayma adımı apply içinde patlıyordu (Benicalap'ta hiç
# delikli aday yoktu → oradaki koşuda görünmedi). Burada güvenli sayaç:
# delikli/bozuk geometri -1 alır ve ADAYLIKTAN çıkar (stokta kalır — ölçekleme
# geometri modellemesi gerektirmez, sadece temsilî olamaz).

def _n_vertices_safe(geom) -> int:
    try:
        return len(mb.clean_polygon(geom).exterior.coords) - 1
    except Exception:
        return -1


def select_representatives_city(stock: gpd.GeoDataFrame) -> pd.DataFrame:
    """nbp.select_representatives ile aynı skor/pre-flight mantığı; tek fark
    köşe sayımı exception-güvenli ve modellenemeyen geometri aday dışı."""
    reps = []
    for cluster, sub in stock.groupby("cluster"):
        cand = sub[~sub["imputed_floors"]].copy()
        if cand.empty:
            cand = sub.copy()
        cand["n_vertices"] = cand.geometry.apply(_n_vertices_safe)
        dropped = int((cand["n_vertices"] < 0).sum())
        if dropped:
            print(f"[seçim] {cluster}: {dropped} aday modellenemez geometri "
                  f"(iç avlu vb.) → aday dışı.")
        cand = cand[cand["n_vertices"] > 0]
        if cand.empty:
            raise RuntimeError(f"{cluster}: modellenebilir aday kalmadı.")
        med_area = cand["Shape_Area"].median()
        med_fl = cand["altura_max"].median()
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
            except Exception as e:
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
# 3) TEK TEMSİLÎNİN MODELİNİ KUR — nbp.run_representative'in E+'SIZ dilimi
# ============================================================================

def build_representative(stock: gpd.GeoDataFrame, rep: pd.Series) -> dict:
    """Tek temsilî binanın 3D modelini kur ve kaydet (SİMÜLASYON YOK).
    nbp.run_representative ile params/geometri zinciri satır satır aynı;
    E+ yerine save_model + render gelir."""
    row = stock[stock["refparcela"] == rep["refparcela"]].iloc[0]
    ref, cluster = row["refparcela"], rep["cluster"]
    print(f"\n===== {cluster} → temsilî {ref} ({rep['n_buildings']} bina) =====")

    params = nbp.period_params(rep["family"], rep["period"])
    g_data = nbp.ground_rule_from_tipo15(ref)      # veri > aile varsayımı
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
    osm_path = mb.save_model(osm, run_dir)
    png_path = run_dir / "model_3d.png"
    render_model(osm, png_path, f"{cluster} — {ref} ({rep['n_buildings']} bina)")
    print(f"[kayıt] {osm_path.name} + {png_path.name} → {run_dir}")

    return {**rep.to_dict(),
            **{f"stat_{k}": v for k, v in stats.items()},
            **{f"param_{k}": v for k, v in params.items()},
            "osm_path": str(osm_path)}


# ============================================================================
# 4) RENDER — pilot renk kodu (pm.surface_polys) ile 4 açılı PNG
# ============================================================================

def render_model(model, png_path: Path, title: str):
    """plot_model_claude.main'in genelleştirilmiş hali: sabit pilot dosyası
    yerine verilen model nesnesini çizer (renk kodu pm.surface_polys'ten)."""
    polys = pm.surface_polys(model)
    bld_pts = np.array([(v.x(), v.y(), v.z())
                        for srf in model.getSurfaces() for v in srf.vertices()])
    mins, maxs = bld_pts.min(axis=0), bld_pts.max(axis=0)
    center, span = (mins + maxs) / 2, (maxs - mins).max() / 2
    span *= 1.35

    views = [("Güneybatıdan", 25, -120), ("Doğudan", 25, -30),
             ("Kuzeyden", 25, 60), ("Üstten (footprint)", 88, -90)]
    fig = plt.figure(figsize=(16, 12))
    for i, (vtitle, elev, azim) in enumerate(views, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        for pts, color, alpha in polys:
            ax.add_collection3d(Poly3DCollection([pts], facecolor=color, alpha=alpha,
                                                 edgecolor="#333333", linewidth=0.4))
        ax.set_xlim(center[0] - span, center[0] + span)
        ax.set_ylim(center[1] - span, center[1] + span)
        ax.set_zlim(0, 2 * span)
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(vtitle, fontsize=11)
        ax.set_xlabel("x (m)"); ax.set_ylabel("y (m → Kuzey)"); ax.set_zlabel("z (m)")
    fig.suptitle(f"{title}\n(mavi=dış, kırmızı=party wall, camgöbeği=pencere, "
                 f"koyu mavi=balkon kapısı, yeşil=iç, kahve=zemin, gri=komşu gölge)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(png_path, dpi=100)
    plt.close(fig)


# ============================================================================
# 5) MAIN — stok → 21 temsilî → 21 model (veya argümanla tek bina)
# ============================================================================

def main():
    mb.validate_input_files()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stock = load_stock_city()

    # Tek bina modu: refparcela argümanı verilirse sadece o bina modellenir
    # ("genelleştirilmiş Parça A" iddiasının kanıtı: HERHANGİ bir Valencia
    # binası tek komutla modellenebilir).
    if len(sys.argv) > 1:
        ref = sys.argv[1].strip()
        hit = stock[stock["refparcela"] == ref]
        if hit.empty:
            raise SystemExit(f"refparcela bulunamadı: {ref}")
        row = hit.iloc[0]
        rep = pd.Series({"cluster": row["cluster"], "family": row["family"],
                         "period": row["period"], "refparcela": ref,
                         "n_buildings": 1,
                         "rep_area_m2": round(float(row["Shape_Area"]), 1),
                         "cluster_med_area_m2": float("nan"),
                         "rep_floors": int(row["altura_max"]),
                         "cluster_med_floors": float("nan"), "rep_vertices": -1})
        build_representative(stock, rep)
        return

    reps = select_representatives_city(stock)
    reps.to_csv(OUT_DIR / "representatives.csv", index=False)

    results, failures = [], []
    for _, rep in reps.iterrows():
        try:
            results.append(build_representative(stock, rep))
        except Exception as e:              # bir cluster patlarsa diğerleri sürsün
            print(f"[HATA] {rep['cluster']}: {e}")
            failures.append({"cluster": rep["cluster"],
                             "refparcela": rep["refparcela"], "error": str(e)})

    df = pd.DataFrame(results)
    df.to_csv(OUT_DIR / "models_stats.csv", index=False)

    print("\n" + "=" * 70)
    print(f"ŞEHİR MODEL ÜRETİMİ BİTTİ: {len(results)}/{len(reps)} model kuruldu.")
    if failures:
        pd.DataFrame(failures).to_csv(OUT_DIR / "failures.csv", index=False)
        print(f"BAŞARISIZ: {len(failures)} cluster → failures.csv")
    cols = ["cluster", "n_buildings", "rep_floors", "stat_footprint_m2",
            "stat_window_area_m2", "stat_n_party_surfaces", "stat_n_shading_surfaces"]
    print(df[[c for c in cols if c in df.columns]].to_string(index=False))
    print(f"\nÇıktılar: {OUT_DIR}/<cluster>/model_python.osm + model_3d.png")
    print("Görsel kontrol: PNG'lere bak — pencereler dış cephede mi, party wall "
          "kırmızı/penceresiz mi, kat sayısı doğru mu?")


if __name__ == "__main__":
    main()
