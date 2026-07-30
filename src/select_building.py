"""Benicalap'tan QGIS görevi için aday bina seçimi.

Javier'in ilk görevi: Valencia şehir datasetinden bir bina seçip QGIS ile
verilerini extract etmek. Bu script, hedef kriterlere uyan aday binaları
listeler; kullanıcı QGIS'te bunlardan birini (varsayılan: 1. aday) seçer.

Kriterler:
- cluster == 'BlocPluriP04'  (1960-1979 blok apartman; şehrin en kritik tipolojisi)
- nombre  == 'BENICALAP'     (ekibin pilot bölgesi)
- Tek Polygon (MultiPolygon parçalı değil)
- Basit geometri: az köşe + dikdörtgene yakınlık (Gauthier akışı ve ileride
  pencere otomasyonu için dörtgene yakın taban tercih edilir)
- Orta boy: Shape_Area 200-600 m2, altura_max 4-6 kat
- ano_constr dolu (dönem doğrulanabilir olsun)

Çalıştır:  .venv/bin/python src/select_building.py
"""

from pathlib import Path

import geopandas as gpd

DATA = Path(__file__).resolve().parent.parent / "data" / "gis"
SHP = DATA / "DatosRai_ciudadValencia.shp"

TARGET_CLUSTER = "BlocPluriP04"
TARGET_BARRIO = "BENICALAP"


def rectangularity(geom) -> float:
    """Poligonun minimum döndürülmüş dikdörtgenine oranı (1.0 = tam dikdörtgen)."""
    mrr = geom.minimum_rotated_rectangle
    return geom.area / mrr.area if mrr.area > 0 else 0.0


def main() -> None:
    f = gpd.read_file(SHP)
    print(f"Toplam bina: {len(f)}  |  CRS: {f.crs}")

    cand = f[(f["cluster"] == TARGET_CLUSTER) & (f["nombre"] == TARGET_BARRIO)].copy()
    print(f"{TARGET_BARRIO} içindeki {TARGET_CLUSTER}: {len(cand)}")

    # Tek Polygon + geometri sadeliği
    cand = cand[cand.geometry.geom_type == "Polygon"].copy()
    cand["n_corners"] = cand.geometry.apply(lambda g: len(g.exterior.coords) - 1)
    cand["rect"] = cand.geometry.apply(rectangularity)

    # Orta boy + yıl dolu
    cand = cand[
        cand["Shape_Area"].between(200, 600)
        & cand["altura_max"].between(4, 6)
        & cand["ano_constr"].notna()
    ]
    print(f"Filtre sonrası (Polygon, 200-600 m2, 4-6 kat, yıl dolu): {len(cand)}")

    # En temiz geometriler önce: az köşe, yüksek dikdörtgenlik
    cand = cand.sort_values(["n_corners", "rect"], ascending=[True, False])

    cols = ["refparcela", "ano_constr", "altura_max", "Shape_Area",
            "n_corners", "rect", "numero_viv", "num_vivend", "pob_total",
            "coorx", "coory"]
    top = cand.head(5)

    print("\n=== ADAY BİNALAR (ilk 5) ===")
    for rank, (_, r) in enumerate(top.iterrows(), 1):
        print(f"\n#{rank}  refparcela: {r['refparcela']}")
        print(f"    Yıl: {int(r['ano_constr'])} | Kat: {int(r['altura_max'])} "
              f"| Taban: {r['Shape_Area']:.0f} m2")
        print(f"    Köşe: {r['n_corners']} | Dikdörtgenlik: {r['rect']:.2f}")
        print(f"    Konut: {r['numero_viv']} ({r['num_vivend']} adet) "
              f"| Nüfus: {int(r['pob_total'])}")
        print(f"    Merkez (EPSG:25830): {r['coorx']:.1f}, {r['coory']:.1f}")

    if len(top) > 0:
        best = top.iloc[0]
        print("\n=== ÖNERİLEN HEDEF BİNA ===")
        print(f"refparcela = {best['refparcela']}")
        print("QGIS'te seçim ifadesi:")
        print(f"  \"refparcela\" = '{best['refparcela']}'")


if __name__ == "__main__":
    main()
