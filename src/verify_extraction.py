"""QGIS export doğrulaması: benicalap_bina_secilen.gpkg

Kullanıcı QGIS'te tek binayı export ettikten sonra çalıştırılır.
Kontroller:
 1. Dosya var ve okunuyor
 2. Tam 1 kayıt
 3. Geometri Polygon ve exterior koordinatları okunabiliyor
    (Gauthier'in GIS_3.py akışının beklediği yapı)
 4. Gerekli kolonlar dolu: altura_max, ano_constr, cluster, Shape_Area
 5. CRS = EPSG:25830

Çalıştır:  .venv/bin/python src/verify_extraction.py
"""

import sys
from pathlib import Path

import geopandas as gpd

GPKG = Path(__file__).resolve().parent.parent / "data" / "gis" / "benicalap_bina_secilen.gpkg"

REQUIRED_COLS = ["refparcela", "altura_max", "ano_constr", "cluster",
                 "Shape_Area", "nombre", "pob_total"]


def fail(msg: str) -> None:
    print(f"HATA: {msg}")
    sys.exit(1)


def main() -> None:
    if not GPKG.exists():
        fail(f"Dosya yok: {GPKG}\n  → QGIS rehberi Adım 6'yı tamamla "
             "(Export → Save Selected Features As).")

    f = gpd.read_file(GPKG)
    print(f"Dosya           : {GPKG.name}")
    print(f"Kayıt sayısı    : {len(f)}")
    if len(f) != 1:
        fail("Tam 1 bina bekleniyordu. Export'ta 'Save only selected features' "
             "işaretli miydi? Seçim tek bina mıydı?")

    b = f.iloc[0]

    # CRS
    print(f"CRS             : {f.crs}")
    if f.crs is None or f.crs.to_epsg() != 25830:
        fail("CRS EPSG:25830 değil — export'u CRS değiştirmeden tekrarla.")

    # Kolonlar
    missing = [c for c in REQUIRED_COLS if c not in f.columns]
    if missing:
        fail(f"Eksik kolonlar: {missing}")
    empty = [c for c in REQUIRED_COLS if b[c] is None]
    if empty:
        fail(f"Boş değerli kolonlar: {empty}")

    # Geometri — GIS_3.py'nin yaptığı gibi exterior koordinatları çıkar
    geom = b.geometry
    print(f"Geometri tipi   : {geom.geom_type}")
    if geom.geom_type == "MultiPolygon":
        # GeoPackage export'u tek parçalı Polygon'ları da MultiPolygon'a sarabilir
        # (standart OGR davranışı) — tek parçaysa sorun yok, iç Polygon'u kullan.
        if len(geom.geoms) != 1:
            fail(f"MultiPolygon {len(geom.geoms)} parçalı — tek parçalı bina seçilmeliydi.")
        geom = geom.geoms[0]
        print("                  (MultiPolygon, 1 parça — Polygon olarak kullanılıyor)")
    elif geom.geom_type != "Polygon":
        fail(f"Polygon/MultiPolygon(1 parça) bekleniyordu, {geom.geom_type} geldi.")
    coords = list(geom.exterior.coords)
    print(f"Köşe sayısı     : {len(coords) - 1}")

    # Özet
    print("\n=== SEÇİLEN BİNA ===")
    print(f"refparcela  : {b['refparcela']}")
    print(f"Mahalle     : {b['nombre']}")
    print(f"TABULA      : {b['cluster']}  ({b.get('tipologia_', '?')})")
    print(f"Yapım yılı  : {int(b['ano_constr'])}  ({b.get('ano_cons_1', '?')})")
    print(f"Kat sayısı  : {int(b['altura_max'])}")
    print(f"Taban alanı : {b['Shape_Area']:.0f} m2")
    print(f"Konut       : {b.get('numero_viv', '?')} ({b.get('num_vivend', '?')} adet)")
    print(f"Nüfus       : {int(b['pob_total'])}")

    # GIS_3.py uyum simülasyonu: origin'e ötelenmiş koordinat listesi
    x0, y0 = coords[0]
    X = [x - x0 for x, _ in coords]
    Y = [y - y0 for _, y in coords]
    print(f"\nGIS_3.py uyumu  : origin'e ötelenmiş {len(X)} koordinat üretildi, "
          f"n_floors={int(b['altura_max'])}")

    print("\n>>> EXTRACTION OK <<<")
    print("Sonraki adım: bu gpkg ile OpenStudio model üretimi (Gauthier akışı).")


if __name__ == "__main__":
    main()
