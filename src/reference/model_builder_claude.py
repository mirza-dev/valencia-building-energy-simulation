"""Valencia bina 3D MODEL kurucu (builder) — Claude'un açıklamalı referans kodu.

NE YAPAR (SADECE GEOMETRİ — EnergyPlus YOK):
  GeoPackage'daki bina footprint'i → OpenStudio 3D modeli (SketchUp'sız!):
  katlar, termal zonlar, dönem-uyumlu construction'lar, party wall (adyabatik),
  ayrık pencere/balkon düzeni, persiana kontrolü, komşu gölge kütleleri →
  kaydedilmiş .osm dosyası.

  Bu dosya bir binanın 3D modelini kurar ve DİSKE YAZAR; EnergyPlus'ı ÇALIŞTIRMAZ.
  Modeli görsel denetlemek için: OpenStudio Application ile aç VEYA
  `plot_model_claude.py` ile matplotlib render'ı al. Simülasyon (E+ koşusu,
  sonuç, QA, karbon) AYRI dosyada: `run_simulation_claude.py`.

  NEDEN İKİYE BÖLÜNDÜ: modelleme (geometri) ile simülasyon (E+) ayrı sorumluluklar;
  önce sağlam bir 3D model kurup GÖZLE doğrulamak, sonra simüle etmek daha temiz
  ve öğretici (kullanıcı isteği, 2026-07-06). build_model() → (model, stats)
  döndürür; simülasyon dosyası bu modeli alıp koşturur.

TASARIM İLKESİ: Bu iş N binada tekrarlanacak. O yüzden her adım FONKSİYON;
`main()` sadece bir döngü. v1'de döngü 1 bina içeriyor (hedef: 4252702YJ2745A),
ama aynı kod bir mahalle GeoDataFrame'i verilince de çalışacak şekilde yazıldı.

MANUEL SÜREÇLE EŞLEŞME (SketchUp/OpenStudio Application adımları → buradaki karşılık):
  Template .osm açma            → load_template()          (VersionTranslator)
  Create Spaces from Diagram    → build_model() içinde Space.fromFloorPrint
  Space Type atama              → sp.setSpaceType(...)
  Thermal Zone atama            → sp.setThermalZone(...)
  Surface Matching              → intersectSurfaces + matchSurfaces
  Pencere çizme                 → SubSurface + addOverhang (ayrık açıklıklar)
  Weather file ekleme           → WeatherFile.setWeatherFile(...)
  Modeli kaydet                 → save_model()             (.osm → disk)
  (Run Simulation + Sonuç okuma → run_simulation_claude.py)

VERİ KAYNAKLARI / VARSAYIMLAR (künyeden — 05_Experiments/2026-07-03 Bina Künyesi):
  - Kat sayısı: kadastro `altura_max` = 5 konut katı; Street View teyitli.
  - Zemin kat: Tipo15'e göre zemin katta daire YOK (ticari/giriş) →
    modelde ŞARTLANDIRILMAMIŞ tampon bölge ("Espacio Tipo No habitable 1ACH").
    Toplam 6 seviye × 3 m = 18 m (EU DB yükseklik 17,5 m ile ~%3 içinde).
  - NW cephe (20,04 m): party wall — ikiz komşu 4252701YJ2745A ile bitişik.
    Modelde ADYABATİK (ısı geçişi yok), pencere yok. Otomatik tespit edilir.
  - Pencereler: Matias'ın WWR sözlüğü (N %12 / E %18 / S %25 / W %18);
    çapraz cepheler için kardinal değerler arasında doğrusal interpolasyon
    (45°'de tam olarak künye §7.1'deki "iki komşu yönün ortalaması" kuralı).
  - Cam: IVE/TABULA 1960-80 dönemi — alüminyum sürme, TEK cam:
    U = 5,7 W/m²K, g(SHGC) = 0,82 (künye §7.0.1).
  - Opak kabuk: template kütüphanesinden DÖNEM-UYUMLU yalıtımsız construction'lar
    ('Muro sin aislante', 'Cubierta plana no aislada', 'Medianera Referencia B',
    'Solera sin aislante') — space type default'u olan yalıtımlı rehabilitasyon
    seti KULLANILMAZ (kod içinde açıklama var).
  - Havalandırma: CTE 'Ventilacion mecanica 4 l/s·kişi' (template DSOA'sı) ideal
    loads'a açıkça bağlanır; +0,2 ACH sabit sızıntı + yaz gecesi 4 ACH doğal
    soğutma space type'tan otomatik gelir.
  - v2 eki: persiana (ExteriorBlind, OnIfHighSolarOnWindow, sadece Haz-Eyl) +
    ayrık pencere/balkon kapısı düzeni + balkon overhang gölgeleri.
  - v3 ekleri (Gauthier raporunun eksik listesinin kapanışı):
    (1) KOMŞU GÖLGELEMESİ: 50 m çevredeki binalar GIS'ten gölge kütlesi olarak
        eklenir (context_shading param).
    (2) TERMAL KÖPRÜ: TABULA battaniye eki ΔU_tb=0,10 W/m²K dış duvar efektif
        U'suna eklenir; persiana kutusu (cajón) etkisi de bu kanalda temsil
        edilir (thermal_bridge_du param — LHS değişkeni).

ÇALIŞTIRMA (sadece model kurar, E+ yok):
  cd ~/valencia-energy-sim
  .venv/bin/python src/reference/model_builder_claude.py
"""

from datetime import datetime
from pathlib import Path

import geopandas as gpd
import openstudio
import pandas as pd
from shapely.geometry import LineString
from shapely.geometry.polygon import orient

# ============================================================================
# CONFIG — model kurulumu için tüm sabitler/yollar tek yerde. N binaya geçerken
# sadece BUILDINGS_GPKG'yi mahalle dosyasıyla değiştirmek yetecek.
# (E+ koşusu / QA / karbon sabitleri run_simulation_claude.py'dedir.)
# ============================================================================

def _project_root() -> Path:
    """Proje kökünü bul: bu dosyadan YUKARI çıkarak 'data/gis' klasörünü ara.
    Neden sabit parents[N] değil? Referans dosya src/reference/ altında (kökten
    2 seviye derin), kullanıcının transkripsiyon dosyası src/ altında (1 seviye)
    — sabit indeks dosya taşınınca SESSİZCE yanlış kökü gösterir ve data yolları
    kırılır (Codex bulgusu P1, 2026-07-06). Marker araması iki konumda da aynı
    kodla çalışır; kök bulunamazsa net hata verir.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "data" / "gis").exists():
            return parent
    raise FileNotFoundError(
        "Proje kökü bulunamadı: bu dosyanın üst klasörlerinde 'data/gis' yok. "
        "Script ~/valencia-energy-sim altında mı duruyor?")


PROJECT = _project_root()                              # ~/valencia-energy-sim

BUILDINGS_GPKG = PROJECT / "data/gis/benicalap_bina_choosen.gpkg"   # v1: 1 bina
                 # (2026-07-07: kullanıcı dosyayı 'secilen'→'choosen' olarak yeniden adlandırdı)
NEIGHBORS_SHP  = PROJECT / "data/gis/DatosRai_ciudadValencia.shp"   # party wall tespiti için komşular
TEMPLATE_OSM   = PROJECT / "data/templates/PlantillaOS_v2.osm"      # Javier'in template'i
EPW_FILE       = PROJECT / "data/weather/ESP_Valencia.082840_IWEC.epw"
OUT_DIR        = PROJECT / "out"                                     # bina başına alt klasör açılır

FLOOR_H = 3.0            # kat yüksekliği (m) — 6 seviye × 3,0 = 18 m ≈ EU DB 17,5 m
GROUND_UNCONDITIONED = True   # zemin kat ticari/şartlandırılmamış mı? (Tipo15: zeminde daire yok)
                              # N-bina döngüsünde bina başına Tipo15'ten türetilebilir.

# Matias'ın WWR (pencere/duvar oranı) sözlüğü — kardinal yönler için:
WWR_CARDINAL = {0: 0.12, 90: 0.18, 180: 0.25, 270: 0.18}   # N, E, S, W

# IVE/TABULA 1960-80 pencere camı (künye §7.0.1):
WINDOW_U = 5.7        # W/m²K (tek cam + alüminyum çerçeve, ısı köprüsü kesintisiz)
WINDOW_SHGC = 0.82    # g⊥ 0,80–0,85 aralığının ortası

# --- v2: GERÇEK CEPHE DÜZENİ (Street View sayımı, künye §7.2) -----------------
# Bant pencere yerine AYRIK açıklıklar: kat başına cephede 2 BALKON KAPISI
# (uçlarda) + WWR alanını tamamlayan sayıda PENCERE (arada, eşit aralıklı).
# Gerçek sayım (SE): 5 pencere + 2 balkon kapısı/kat → kural bunu ~1 açıklık
# toleransla yeniden üretir ve N-binaya genellenir.
WIN_W, WIN_H, WIN_SILL = 1.2, 1.2, 0.9      # standart pencere (künye §7.0.2) + parapet
DOOR_W, DOOR_H, DOOR_SILL = 1.2, 2.1, 0.01  # balkon kapısı (1,2×2,1)
BALCONY_DOORS_PER_FLOOR = 2                 # cephe başına (Street View: uçlarda balkonlar)
BALCONY_DEPTH = 1.0                         # balkon çıkması (m) → üst balkon alttakini gölgeler

# --- v2: PERSIANA/GÖLGELEME (Street View'da tenteler görülüyor) ----------------
# Template'in blind malzemesiyle ExteriorBlind; "güneş pencerede eşiği aşarsa
# kapat" kontrolü, SADECE yaz aylarında (Haz-Eyl) aktif. Kullanım alışkanlığı
# belirsiz → setpoint LHS belirsizlik değişkeni.
SHADE_BLIND_NAME = "Lamas Horizontales 25mm cada 20mm"   # template kütüphanesinden
SHADE_SETPOINT = 250.0   # W/m² — pencereye düşen güneş bu eşiği aşınca panjur kapanır

# --- v3: KOMŞU GÖLGELEMESİ (context shading) -----------------------------------
# Gauthier raporunun eksik listesindeki 'shading' kalemi: yoğun şehir dokusunda
# karşı bina ve komşular alt katların güneşini keser — özellikle kış güneşi
# alçakken ısıtmayı, yazın da cephe güneşlenmesini etkiler. Komşu footprint'leri
# GIS'ten okunur ve gölge KÜTLESİ (ShadingSurfaceGroup) olarak eklenir; termal
# zon değildirler (içi simüle edilmez, sadece gölge düşürürler).
CONTEXT_RADIUS = 50.0    # m — bu yarıçap içindeki komşular gölge kütlesi olur
# Komşu yüksekliği: altura_max KONUT katlarını sayar; zemin ticari kat mahalle
# stokunda yaygın (Tipo15 deseni) → komşulara STOK GENELİ kural uygulanır:
# seviye = altura_max + 1 (modül sabiti GROUND_UNCONDITIONED). Modellenen binanın
# kendi zemin kuralı ise params['ground_unconditioned'] ile bina başına seçilir
# (N-bina: VivUni'de False). Yaklaşık bir tahmindir; belirsizliği rapora yazılır.

# --- v3: TERMAL KÖPRÜ (thermal bridge) ------------------------------------------
# Gauthier listesindeki 'thermal bridge' + künyedeki persiana kutusu (cajón de
# persiana — 1974'te yalıtımsız): kolon/kiriş/lento/kutu köprüleri tek tek
# çizmek yerine TABULA hesap yönteminin battaniye eki kullanılır:
#   U_duvar_efektif = U_duvar + ΔU_tb  (TABULA varsayılanı ΔU_tb = 0,10 W/m²K)
# Sadece DIŞ DUVARLARA uygulanır (köprüler ağırlıkla duvar detaylarında);
# çatıya uygulanmaz. LHS'te belirsizlik değişkeni (0,0–0,2).

# --- LHS PARAMETRİZASYONU ------------------------------------------------------
# build_model(params=...) ile üzerine yazılabilen belirsiz girdiler.
# None = baseline davranış (template dönem construction'ları / varsayılan değer).
# LHS çalışması (lhs_study_claude.py) bu sözlüğü örnekleyerek N koşu yapar.
DEFAULT_PARAMS = {
    "wall_u": None,            # W/m²K — verilirse massless eşdeğer duvar üretilir
    "roof_u": None,            # W/m²K — verilirse massless eşdeğer çatı üretilir
    "window_u": WINDOW_U,      # W/m²K
    "window_g": WINDOW_SHGC,   # SHGC
    "infiltration_ach": None,  # 1/h — verilirse template'in sabit 0,2 ACH'ı değişir
    "shade_setpoint": SHADE_SETPOINT,  # W/m² — persiana kapanma eşiği
    "context_shading": True,   # v3: komşu binalar gölge kütlesi olarak eklensin mi
    "thermal_bridge_du": 0.10, # v3: W/m²K — TABULA battaniye termal köprü eki (dış duvar)
    # 2026-07-10: wall_u/roof_u verildiğinde duvar/çatı NASIL kurulsun?
    #   False (default) → GERÇEK KATMANLI, dönem-bilinçli rejim (_build_layered_wall/
    #                     _build_layered_roof): gerçek malzeme + ısıl kütle + tam U.
    #   True            → eski massless tek-katman (SADECE LHS: U etkisini izole
    #                     etmek için bilinçli sadeleştirme; donmuş çalışma korunur).
    "massless": False,
    # N-bina: zemin kat ticari-tampon mu? Çok katlı bloklarda True (Tipo15 deseni),
    # müstakil/sıra evlerde (VivUni) False — orada zemin de konuttur. Komşu GÖLGE
    # yükseklikleri bu param'dan ETKİLENMEZ (stok geneli varsayımı, aşağıda).
    "ground_unconditioned": GROUND_UNCONDITIONED,
}

PARTY_WALL_TOL = 0.3  # m — duvar ile komşu sınırı eşleştirme toleransı
                      # (kadastro mikro-kırıkları 0,1 m; simplify sapması < 0,3 m)

FOOTPRINT_RANGE = (50.0, 5000.0)  # m² — bina tabanı makul aralık (pre-flight)

# E+ çıktı değişkenleri — TEK kaynak burada tanımlanır: build_model bunları modele
# OutputVariable olarak ekler; run_simulation_claude.read_results SADECE bunları
# okur (tam ad eşleşmesi; 'contains' filtresi ileride eklenecek başka bir
# değişkenle çift saymaya yol açabilirdi). İki dosya da mb.OUTPUT_VARIABLES'ı kullanır.
OUTPUT_VARIABLES = {
    "heating": "Zone Ideal Loads Supply Air Total Heating Energy",
    "cooling": "Zone Ideal Loads Supply Air Total Cooling Energy",
}


# ============================================================================
# 1) VERİ OKUMA + PRE-FLIGHT DOĞRULAMA
# ============================================================================
# "Hatasız" pipeline'ın ilk kuralı: bozuk girdiyle E+'a kadar gidip anlaşılmaz
# bir hatayla düşmek yerine, İLK ADIMDA net Türkçe mesajla durmak.

def validate_input_files():
    """Tüm girdi dosyalarının varlığını başta kontrol et (fail-fast)."""
    files = {
        "Bina GeoPackage": BUILDINGS_GPKG,
        "Komşu shapefile (party wall + gölgeleme)": NEIGHBORS_SHP,
        "OpenStudio template": TEMPLATE_OSM,
        "EPW hava durumu dosyası": EPW_FILE,
    }
    missing = [f"  - {ad}: {p}" for ad, p in files.items() if not p.exists()]
    if missing:
        raise FileNotFoundError("Girdi dosyaları eksik:\n" + "\n".join(missing))


def validate_building_row(row):
    """Bir bina satırının modellenebilir olduğunu doğrula (net hata mesajlı).
    N-bina döngüsünde bozuk kayıtları erken yakalamanın temeli.
    """
    ref = row.get("refparcela")
    if ref is None or str(ref).strip() == "":
        raise ValueError("Bina kaydında 'refparcela' boş — kimliksiz bina modellenmez.")
    if row.geometry is None or row.geometry.is_empty:
        raise ValueError(f"{ref}: geometri boş.")
    h = row.get("altura_max")
    if h is None or pd.isna(h):
        raise ValueError(f"{ref}: 'altura_max' (kat sayısı) eksik.")
    if float(h) != int(float(h)) or not (1 <= int(float(h)) <= 30):
        raise ValueError(f"{ref}: 'altura_max'={h} geçersiz (1–30 arası tam sayı beklenir).")


def load_buildings(gpkg_path: Path) -> gpd.GeoDataFrame:
    """Simüle edilecek binaları oku (v1: tek binalık gpkg; N-bina: mahalle dosyası)."""
    gdf = gpd.read_file(gpkg_path)
    if gdf.crs is None or gdf.crs.to_epsg() != 25830:
        raise ValueError(f"CRS EPSG:25830 bekleniyordu, {gdf.crs} geldi.") 
    for col in ("refparcela", "altura_max"):
        if col not in gdf.columns:
            raise ValueError(f"{gpkg_path.name}: zorunlu kolon '{col}' yok. "
                             f"Mevcut kolonlar: {list(gdf.columns)[:12]}...")
    print(f"[veri] {gpkg_path.name}: {len(gdf)} bina yüklendi.")
    return gdf


def clean_polygon(geom):
    """Geometriyi tek, geçerli, deliksiz Polygon'a indir.
    - GeoPackage bazen MultiPolygon'a sarar → tek parçaysa aç.
    - Delikli poligon (iç avlu) fromFloorPrint'e verilemez → net hata
      (avlu desteği N-bina fazının işi; sessizce yanlış model kurmaktansa dur).
    - is_valid değilse (kendini kesen kadastro çizimi) net hata.
    """
    if geom.geom_type == "MultiPolygon":
        if len(geom.geoms) != 1:
            raise ValueError(f"Çok parçalı MultiPolygon ({len(geom.geoms)} parça) desteklenmiyor.")
        geom = geom.geoms[0]
    if geom.geom_type != "Polygon":
        raise ValueError(f"Polygon bekleniyordu, {geom.geom_type} geldi.")
    if not geom.is_valid:
        raise ValueError("Geometri geçersiz (self-intersection olabilir) — QGIS'te 'Fix geometries' gerekli.")
    if len(geom.interiors) > 0:
        raise ValueError(f"Poligonda {len(geom.interiors)} delik (iç avlu?) var — "
                         "fromFloorPrint delik desteklemez; avlu desteği N-bina fazında ele alınacak.")
    return geom


def prepare_footprint(geom):
    """Footprint'i modele hazırla:
    1. simplify(0.3): kadastro mikro-kırıklarını (~0,1 m) temizle →
       8 köşeli 'sahte' poligon gerçek 4 köşeli paralelkenara döner (künye §6).
    2. orient(sign=-1): köşeleri SAAT YÖNÜNE çevir — OpenStudio fromFloorPrint
       taban normalinin AŞAĞI bakmasını ister, yoksa boş döner (test edildi!).
    Dönüş: (köşe listesi [(x,y),...] — kapanış noktası TEKRARSIZ, alan m²)
    """
    simp = geom.simplify(0.3, preserve_topology=True)
    area_change = abs(simp.area - geom.area) / geom.area
    if area_change > 0.01:
        raise ValueError(f"simplify alanı %{area_change*100:.1f} değiştirdi — tolerans düşürülmeli.")
    if not (FOOTPRINT_RANGE[0] <= simp.area <= FOOTPRINT_RANGE[1]):
        raise ValueError(f"Taban alanı {simp.area:.0f} m² makul aralık dışında {FOOTPRINT_RANGE} — "
                         "veri hatası olabilir (birim/CRS kontrol et).")
    cw = orient(simp, sign=-1.0)                      # -1 = saat yönü (clockwise)
    coords = list(cw.exterior.coords)[:-1]            # son nokta = ilk nokta → at
    if len(coords) < 3:
        raise ValueError(f"simplify sonrası {len(coords)} köşe kaldı — poligon dejenere.")
    return coords, cw.area


# ============================================================================
# 2) KOMŞULAR — party wall tespiti + gölge kütleleri için ortak okuma
# ============================================================================

def load_neighbors(geom, refparcela: str, neighbors_shp: Path) -> gpd.GeoDataFrame:
    """Binanın CONTEXT_RADIUS yarıçapı içindeki komşuları TEK SEFERDE oku.
    Aynı komşu kümesi iki işte kullanılır: party wall tespiti + gölge kütleleri.
    Sadece bbox penceresi okunur — 26.452 binalık şehir dosyasının tamamını
    yüklemeye gerek yok, N binada da hızlı kalır. Ama bbox sadece hızlı ÖN
    filtredir: köşeleri yarıçapın √2 katına (50→~71 m) uzanır, o yüzden ardından
    GERÇEK mesafe filtresi uygulanır (Codex bulgusu P3: pilotta 3 bina
    50,4–72,8 m'den bbox köşesiyle sızıyordu — raporda 'within 50 m' yazıyor).
    Kendisi ve duplike geometriler (aynı bina farklı kayıtla) da elenir.
    """
    minx, miny, maxx, maxy = geom.bounds
    r = CONTEXT_RADIUS
    nb = gpd.read_file(neighbors_shp, bbox=(minx - r, miny - r, maxx + r, maxy + r))
    keep = []
    for idx, row in nb.iterrows():
        if row["refparcela"] == refparcela or row.geometry is None:
            continue
        if row.geometry.equals(geom):
            # Duplike kayıt: kendi kopyasıyla kesişim tüm çevreyi party wall
            # yapar, gölgede de binayı kendi kabuğuyla sarardı.
            print(f"[komşu] uyarı: {row['refparcela']} bizim geometrinin duplikesi — atlandı.")
            continue
        if row.geometry.distance(geom) > CONTEXT_RADIUS:
            continue                     # bbox köşesi — gerçek yarıçapın dışında
        keep.append(idx)
    out = nb.loc[keep]
    print(f"[komşu] {len(out)} komşu bina okundu ({CONTEXT_RADIUS:.0f} m yarıçap).")
    return out


def find_party_walls(geom, refparcela: str, neighbors_shp: Path, neighbors=None):
    """Binanın komşularıyla paylaştığı kenarları bul.
    Yöntem (künye §6'da elle kanıtlananın fonksiyon hali):
      bina DIŞ HALKASI (exterior ring) ∩ komşu poligonu → paylaşılan kenar.
      1 m'den uzun kesişimler 'party wall' sayılır.
    Neden poligon∩poligon değil? İki poligon hafif ÜST ÜSTE binerse (kadastro
    çizim hatası) kesişim çizgi değil ALAN olur ve çevre uzunluğu sahte bir
    'paylaşılan kenar' üretirdi (Codex bulgusu P2). Dış halka ∩ komşu ise her
    iki durumda da BİZİM duvarın komşuya değen segmentini verir — bitişik
    pilotta iki yöntem birebir aynı sonucu verdi (20,04 m, geometri eşit).
    `neighbors` verilirse (main tek okuma yapar) shapefile tekrar okunmaz;
    verilmezse içeride load_neighbors çağrılır (eski çağrı imzası korunur —
    LHS scripti bu imzayla çalışıyor).
    Dönüş: paylaşılan kenarların birleşimi (shapely geometri) veya None.
    """
    if neighbors is None:
        neighbors = load_neighbors(geom, refparcela, neighbors_shp)
    shared = []
    for _, r in neighbors.iterrows():
        inter = geom.exterior.intersection(r.geometry)
        if inter.length > 1.0:
            shared.append(inter)
            print(f"[party] komşu {r['refparcela']}: paylaşılan kenar {inter.length:.2f} m")
    if not shared:
        print("[party] paylaşılan kenar yok — bina müstakil (detached).")
        return None
    out = shared[0]
    for s in shared[1:]:
        out = out.union(s)
    return out


# ============================================================================
# 3) PENCERE ORANI — yöne göre WWR
# ============================================================================

def wwr_for_azimuth(az_deg: float) -> float:
    """Cephenin pusula açısına göre WWR döndür.
    Kardinal yönler (0/90/180/270) arasında DOĞRUSAL interpolasyon:
    tam 45°'de bu, künye §7.1'deki 'iki komşu ana yönün ortalaması' kuralına eşittir.
    Örnek (bizim bina): NE 49,4° → %15,3 · SE 139,4° → %21,8 · SW 229,4° → %21,2.
    """
    az = az_deg % 360.0
    lo = int(az // 90) * 90            # alt kardinal (0/90/180/270)
    hi = (lo + 90) % 360               # üst kardinal (döngüsel: 270→0)
    frac = (az - lo) / 90.0            # iki kardinal arasında nerede? (0..1)
    return WWR_CARDINAL[lo] + (WWR_CARDINAL[hi] - WWR_CARDINAL[lo]) * frac


# ============================================================================
# 4) MODEL KURULUMU — footprint → OpenStudio 3D modeli
# ============================================================================

def load_template() -> openstudio.model.Model:
    """Template'i TAZE kopya olarak yükle (her bina kendi modelini alır).
    VersionTranslator eski sürüm .osm'leri de güncel şemaya çevirir.
    """
    tr = openstudio.osversion.VersionTranslator()
    opt = tr.loadModel(openstudio.toPath(str(TEMPLATE_OSM)))
    if opt.isNull():
        raise RuntimeError(f"Template yüklenemedi: {TEMPLATE_OSM}")
    return opt.get()


def _wall_plan_segment(surface, x0: float, y0: float) -> LineString:
    """Dikey duvar yüzeyinin plan (kuşbakışı) izdüşümünü LineString olarak ver.
    Duvarın 4 köşesi plandaki 2 noktaya iner; koordinatlar gerçek (EPSG:25830)
    çerçeveye geri ötelenir (model origin kaydırması + x0/y0 ile geri alınır).
    Sayısal gürültü yuvarlamada 2'den fazla nokta bırakabilir → duvarın gerçek
    iki ucu = EN UZAK nokta çifti (rastgele ilk ikisi değil).
    """
    pts = list({(round(v.x() + x0, 3), round(v.y() + y0, 3)) for v in surface.vertices()})
    if len(pts) < 2:
        raise ValueError(f"Duvar plan izdüşümü çıkarılamadı: {surface.nameString()}")
    best = max(((a, b) for i, a in enumerate(pts) for b in pts[i + 1:]),
               key=lambda ab: (ab[0][0] - ab[1][0]) ** 2 + (ab[0][1] - ab[1][1]) ** 2)
    return LineString(best)


def _massless_construction(osm, name: str, u_value: float, surface_film_r: float):
    """Hedef U-değerinden tek katmanlı 'kütlesiz' construction üret (LHS için).
    R_katman = 1/U − yüzey film dirençleri. Isıl kütle ihmal edilir — belirsizlik
    çalışmasında U'nun ETKİSİNİ izole etmek için bilinçli sadeleştirme.
    """
    r_layer = max(1.0 / u_value - surface_film_r, 0.05)
    mat = openstudio.model.MasslessOpaqueMaterial(osm, "Rough", r_layer)
    mat.setName(f"{name} malzeme (R={r_layer:.3f})")
    c = openstudio.model.Construction(osm)
    c.setName(name)
    c.insertLayer(0, mat)
    return c


def _layer_resistance(mat) -> float | None:
    """Bir opak malzeme katmanının ısıl direnci [m²K/W] (tip bazında)."""
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
    return None                                       # bilinmeyen tip (ör. cam) — dokunma


def _construction_with_delta_u(osm, base, delta_u: float, surface_film_r: float, name: str):
    """Mevcut MASİF construction'dan U'su ΔU kadar YÜKSEK yeni construction üret
    (v3 termal köprü — TABULA battaniye eki: U_efektif = U + ΔU_tb).

    Isıl kütleyi korumak için katman dizilimi aynen kopyalanır; sadece EN DİRENÇLİ
    katmanın iletkenliği artırılır (kalınlık/yoğunluk değişmez → kütle aynı).
    Malzemeler klonlanır — template kütüphanesindeki orijinal 'Muro sin aislante'
    başka yerlerde kullanılıyor olabilir, ASLA yerinde değiştirilmez.
    """
    layers = base.layers()
    rs = [_layer_resistance(m) for m in layers]
    if any(r is None for r in rs):
        raise RuntimeError(f"'{base.nameString()}' içinde tanınmayan katman tipi — ΔU uygulanamadı.")
    r_nofilm = sum(rs)
    u_old = 1.0 / (r_nofilm + surface_film_r)
    r_target_nofilm = 1.0 / (u_old + delta_u) - surface_film_r
    dr = r_nofilm - r_target_nofilm                   # bu kadar direnç eksiltilecek
    idx = max(range(len(rs)), key=lambda i: rs[i])    # en dirençli katman
    r_new = rs[idx] - dr
    if r_new <= 0.01:
        raise RuntimeError(f"ΔU={delta_u} çok büyük: '{base.nameString()}' katmanı "
                           f"R={rs[idx]:.3f} → {r_new:.3f} olurdu (fiziksel değil).")
    new_layers = openstudio.model.MaterialVector()
    for i, mat in enumerate(layers):
        clone = mat.clone(osm).to_Material().get()
        if i == idx:
            std = clone.to_StandardOpaqueMaterial()
            if not std.isNull():
                std = std.get()
                std.setThermalConductivity(std.thickness() / r_new)
                std.setName(f"{std.nameString()} +TB(dU={delta_u:.2f})")
            else:
                ml = clone.to_MasslessOpaqueMaterial()
                (ml.get() if not ml.isNull() else clone.to_AirGap().get()).setThermalResistance(r_new)
        new_layers.append(clone)
    c = openstudio.model.Construction(osm)
    c.setName(name)
    c.setLayers(new_layers)
    return c


def _clone_template_material(osm, name: str):
    """Template'teki adlı malzemeyi KLONLA (StandardOpaque veya AirGap).
    Orijinaller başka construction'larda kullanılıyor — ASLA yerinde
    değiştirilmez (debug #10)."""
    for m in osm.getStandardOpaqueMaterials():
        if m.nameString() == name:
            return m.clone(osm).to_StandardOpaqueMaterial().get()
    for a in osm.getAirGaps():
        if a.nameString() == name:
            return a.clone(osm).to_AirGap().get()
    raise RuntimeError(f"Template'te malzeme yok: {name}")


def _mat_r(m) -> float:
    """Klonlanmış bir katmanın ısıl direnci [m²K/W] (tip bazında — AirGap'te
    thermalConductivity çağrılmaz, OpenStudio hata loglar)."""
    r = _layer_resistance(m)
    if r is None:
        raise RuntimeError(f"Tanınmayan katman tipi: {m.nameString()}")
    return r


def _assemble_calibrated(osm, name: str, target_u: float, film_r: float,
                         fixed_layers: list, calib, insert_at: int):
    """Ortak kalibrasyon çekirdeği: sabit katmanlar + 1 kalibrasyon katmanı.

    Kalibrasyon katmanının KALINLIĞI, assembly film dahil tam target_u verecek
    şekilde ayarlanır: t = (1/U − film − R_sabit) · k. Rejim seçimi doğru
    yapıldıysa R > 0 garantidir; değilse fail-fast (sessiz yanlış fizik olmaz).
    """
    r_fixed = sum(_mat_r(m) for m in fixed_layers)
    r_need = 1.0 / target_u - film_r - r_fixed
    if r_need <= 0.005:
        raise RuntimeError(f"'{name}': hedef U={target_u} bu katman setiyle kurulamaz "
                           f"(R_gereken={r_need:.3f} ≤ 0) — rejim eşiği yanlış.")
    calib.setThickness(r_need * calib.thermalConductivity())
    calib.setName(f"{calib.nameString()} (kalibre {calib.thickness()*1000:.0f}mm)")
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
    """HERHANGİ bir tipoloji/dönem için GERÇEK katmanlı dış duvar (dıştan içe).

    Dönem-bilinçli 3 rejim (TABULA_ES U aralıklarına göre; fizibilite hesabı
    2026-07-10): eski dönemin yüksek U'su hava boşluklu duvarla KURULAMAZ
    (R negatif çıkar) — o dönemin gerçeği de zaten dolu duvardır:
      U ≥ 1.5      → DOLU tuğla:      mortero + perforado(kalibre) + yeso
      1.0 ≤ U <1.5 → BOŞLUKLU (IVE):  mortero + perforado 115 + cámara R0.18
                                       + hueco(kalibre) + yeso   [= pilot seti]
      U < 1.0      → YALITIMLI:       boşluklu set + aislante k=0.035(kalibre),
                                       hueco sabit 70mm (doble hueco)
    Kalibrasyon katmanının kalınlığı film dahil TAM target_u verir. Termal
    köprü (ΔU) burada DEĞİL — üstte _construction_with_delta_u ile eklenir.
    """
    mortero = _clone_template_material(osm, "Mortero de cemento referencia")
    yeso    = _clone_template_material(osm, "Enlucido de yeso d < 1000_15mm")
    if target_u >= 1.5:                                # DOLU tuğla duvar
        calib = _clone_template_material(osm, "Ladrillo Perforado Referencia")
        return _assemble_calibrated(
            osm, f"Muro macizo ladrillo (U_base={target_u:.2f})", target_u, film_r,
            [mortero, yeso], calib, insert_at=1)
    perforado = _clone_template_material(osm, "Ladrillo Perforado Referencia")
    camara    = _clone_template_material(osm, "Camara de aire en paredes R 0.18")
    if target_u >= 1.0:                                # BOŞLUKLU (pilot IVE seti)
        calib = _clone_template_material(osm, "Ladrillo Hueco Referencia")
        return _assemble_calibrated(
            osm, f"Muro IVE ladrillo (U_base={target_u:.2f})", target_u, film_r,
            [mortero, perforado, camara, yeso], calib, insert_at=3)
    # YALITIMLI: hueco sabit (70mm doble), yalıtım kalibre edilir
    hueco = _clone_template_material(osm, "Ladrillo Doble Hueco Referencia")
    calib = _clone_template_material(osm, "Aislante Medianera Referencia B")
    return _assemble_calibrated(
        osm, f"Muro ladrillo aislado (U_base={target_u:.2f})", target_u, film_r,
        [mortero, perforado, camara, hueco, yeso], calib, insert_at=3)


def _build_layered_roof(osm, target_u: float, film_r: float = 0.14):
    """HERHANGİ bir tipoloji/dönem için GERÇEK katmanlı düz çatı (dıştan içe).

    3 rejim (fizibilite hesabı 2026-07-10; U=4.17 tam setle kurulamaz):
      U ≥ 3.5      → ÇIPLAK döşeme:  mortero + FU hormigón(kalibre) + yeso
      1.0 ≤ U <3.5 → TAM SET:        arena+asfalto+mortero+FU(kalibre)+yeso
                                      [= template 'Cubierta plana no aislada' seti]
      U < 1.0      → YALITIMLI:      tam set + aislante(kalibre), FU sabit 300mm
    """
    mortero = _clone_template_material(
        osm, "Mortero de cemento o cal para albañileria y para revoco/enlucido 1600 < d < 1800_2cm")
    yeso = _clone_template_material(osm, "Enlucido de yeso d < 1000_15mm")
    if target_u >= 3.5:                                # ÇIPLAK döşeme (çok eski çatı)
        calib = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(
            osm, f"Cubierta forjado desnudo (U={target_u:.2f})", target_u, film_r,
            [mortero, yeso], calib, insert_at=1)
    arena   = _clone_template_material(osm, "Arena y grava [1700 < d < 2200]  6 cm")
    asfalto = _clone_template_material(osm, "Asfalto 10mm")
    if target_u >= 1.0:                                # TAM SET (template çatısı)
        calib = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
        return _assemble_calibrated(
            osm, f"Cubierta plana ladrillo (U={target_u:.2f})", target_u, film_r,
            [arena, asfalto, mortero, yeso], calib, insert_at=3)
    # YALITIMLI: FU sabit 300mm, yalıtım kalibre
    fu = _clone_template_material(osm, "FU Entrevigado de hormigon aligerado -Canto 300 mm")
    calib = _clone_template_material(osm, "Aislante Medianera Referencia B")
    return _assemble_calibrated(
        osm, f"Cubierta plana aislada (U={target_u:.2f})", target_u, film_r,
        [arena, asfalto, mortero, fu, yeso], calib, insert_at=3)


def _build_ive_brick_wall(osm, target_u: float = 1.33, film_r: float = 0.17):
    """BlocPluriP04 (1961-80) IVE dış duvarı — pilot default'u.

    Javier onayı (2026-07-09, soru #10): template'in betonarme 'Muro sin aislante'
    (U≈3.06) YERİNE IVE broşürü çözümü. Artık genel _build_layered_wall'a delege
    eder (target 1.33 → boşluklu rejim = birebir aynı pilot duvarı)."""
    return _build_layered_wall(osm, target_u, film_r)


def _add_context_shading(osm, neighbors, x0: float, y0: float) -> int:
    """Komşu binaları gölge kütlesi olarak ekle (v3).

    Komşular önce YÜKSEKLİK GRUBUNA göre birleştirilir (unary_union/dissolve):
    blok içinde birbirine bitişik binaların ORTAK duvarları gölge üretmez ama
    E+ gölge örtüşme sayısını patlatır ('Too many figures' uyarısı ilk denemede
    yaşandı — ham 89 duvarla limit 60.000'de bile aşılıyordu). Aynı yükseklikteki
    bitişik kütleler tek blok kabuğuna iner → daha az yüzey, aynı gölge silueti.

    Sonra her blok kabuğunun dış halkası duvar duvar dikey dörtgene çevrilir ve
    'Site' tipli ShadingSurfaceGroup'a konur (northAxis=0 → model çerçevesiyle
    aynı). Çatı yüzeyi GEREKMEZ: gölge silueti dikey duvarlarla tam tanımlanır.
    Yükseklik: seviye = altura_max (+1 zemin ticari kat — bizim binayla tutarlı
    varsayım, CONFIG'de açıklandı) × FLOOR_H.
    Dönüş: eklenen gölge yüzeyi sayısı.
    """
    from shapely.ops import unary_union

    group = openstudio.model.ShadingSurfaceGroup(osm)
    group.setName("Komsu golge kutleleri (context)")
    group.setShadingSurfaceType("Site")

    # Yükseklik grubu → geometri listesi
    by_height = {}
    n_skip = 0
    for _, r in neighbors.iterrows():
        h_raw = r.get("altura_max")
        if h_raw is None or pd.isna(h_raw) or int(float(h_raw)) < 1:
            n_skip += 1
            continue
        n_lev = int(float(h_raw)) + (1 if GROUND_UNCONDITIONED else 0)
        by_height.setdefault(n_lev * FLOOR_H, []).append(r.geometry)

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
                for x, y, z in ((axp, ayp, h), (axp, ayp, 0.0),
                                (bxp, byp, 0.0), (bxp, byp, h)):
                    p.append(openstudio.Point3d(x - x0, y - y0, z))
                ss = openstudio.model.ShadingSurface(p, osm)
                ss.setShadingSurfaceGroup(group)
                n_srf += 1
    print(f"[gölge] {n_srf} gölge yüzeyi eklendi ({len(neighbors)} komşu → "
          f"{len(by_height)} yükseklik grubu; {n_skip} kat bilgisi eksik → atlandı).")
    return n_srf


def _summer_schedule(osm):
    """Haz-Eyl = 1, diğer aylar = 0 (persiana kontrolünün aktif olduğu dönem)."""
    sched = openstudio.model.ScheduleRuleset(osm)
    sched.setName("Persiana yaz programi (Haz-Eyl)")
    sched.defaultDaySchedule().addValue(openstudio.Time(0, 24, 0, 0), 0.0)
    rule = openstudio.model.ScheduleRule(sched)
    rule.setName("Yaz aylari")
    rule.daySchedule().addValue(openstudio.Time(0, 24, 0, 0), 1.0)
    for day in ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"):
        getattr(rule, f"setApply{day}")(True)
    rule.setStartDate(openstudio.Date(openstudio.MonthOfYear(6), 1))
    rule.setEndDate(openstudio.Date(openstudio.MonthOfYear(9), 30))
    return sched


def _add_facade_openings(osm, srf, wwr: float, win_construction, shading_control):
    """Bir dış duvara GERÇEK cephe düzenini yerleştir (v2 — bant pencere değil):
    uçlarda 2 balkon kapısı + arada WWR alanını tamamlayan sayıda pencere.

    Köşe düzeni: fromFloorPrint duvarları [B_üst, A_üst, A_alt, B_alt] sıralı
    (dıştan bakışta saat yönü tersi). Alt kenar A=vertices[2] → B=vertices[3].
    SubSurface köşeleri de aynı yönelimle [s2_üst, s1_üst, s1_alt, s2_alt] verilir.
    Dönüş: (pencere adedi, balkon kapısı adedi, toplam cam alanı m², hedef alan m²).
    """
    v = srf.vertices()
    if len(v) != 4:
        print(f"[uyarı] {srf.nameString()}: It is not four cornered, the openin is skipped.")
        return 0, 0, 0.0, 0.0
    ax, ay, z_lo = v[2].x(), v[2].y(), v[2].z()
    bx, by = v[3].x(), v[3].y()
    L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
    ux, uy = (bx - ax) / L, (by - ay) / L          # alt kenar boyunca birim vektör

    # Açıklık sayıları: hedef cam alanı = duvar alanı × WWR.
    # Kısa/az-WWR cephe kuralı: hedef 2 balkon kapısından bile küçükse kapı
    # sayısını 2→1→0 düşür — yoksa gerçekleşen alan hedefi sessizce aşar
    # (N-bina dar cephelerinde sistematik fazla-cam hatası olurdu).
    target = L * FLOOR_H * wwr
    door_area = DOOR_W * DOOR_H
    n_door = BALCONY_DOORS_PER_FLOOR
    while n_door > 0 and target < n_door * door_area:
        n_door -= 1
    n_win = max(0, round((target - n_door * door_area) / (WIN_W * WIN_H)))
    n_open = n_win + n_door


    def make_subsurface(s_center, w, h, sill, kind):
        s1, s2 = s_center - w / 2, s_center + w / 2
        if s1 < 0.1 or s2 > L - 0.1:
            return None                              # duvar kenarına taşma koruması
        p = openstudio.Point3dVector()
        for s, z in ((s2, z_lo + sill + h), (s1, z_lo + sill + h),
                     (s1, z_lo + sill), (s2, z_lo + sill)):
            p.append(openstudio.Point3d(ax + ux * s, ay + uy * s, z))
        ss = openstudio.model.SubSurface(p, osm)
        ss.setSurface(srf)
        ss.setSubSurfaceType(kind)
        ss.setConstruction(win_construction)
        if shading_control is not None:
            ss.addShadingControl(shading_control)
        return ss

    # Kapı konumları: 2 kapı → iki uç; 1 kapı → ilk uç; 0 kapı → hepsi pencere.
    door_slots = set()
    if n_door >= 1:
        door_slots.add(0)
    if n_door >= 2:
        door_slots.add(n_open - 1)

    n_w = n_d = 0
    area = 0.0
    for i in range(n_open):
        s_center = L * (i + 0.5) / n_open            # eşit aralıklı yerleşim
        if i in door_slots:                          # uçlar: balkon kapıları
            ss = make_subsurface(s_center, DOOR_W, DOOR_H, DOOR_SILL, "GlassDoor")
            if ss is not None:
                ss.addOverhang(BALCONY_DEPTH, 0.1)   # balkon çıkması → gölgeleme
                n_d += 1
                area += ss.grossArea()
        else:                                        # aralar: pencereler
            ss = make_subsurface(s_center, WIN_W, WIN_H, WIN_SILL, "FixedWindow")
            if ss is not None:
                n_w += 1
                area += ss.grossArea()
    return n_w, n_d, area, target


def build_model(row, party_geom, params=None, neighbors=None):
    """Tek bir bina için OpenStudio modelini kur. Dönüş: (model, istatistik sözlüğü).

    `params`: DEFAULT_PARAMS'ı üzerine yazan belirsizlik sözlüğü (LHS koşuları
    için); None ise baseline model kurulur.
    `neighbors`: load_neighbors çıktısı (gölge kütleleri için). None ise ve
    context_shading açıksa içeride yüklenir — eski 3-argümanlı çağrılar
    (LHS scripti) değişiklik gerektirmeden çalışır.

    Adımlar (yorumları takip et — her blok manuel sürecin bir adımı):
    """
    P = {**DEFAULT_PARAMS, **(params or {})}
    unknown = set(P) - set(DEFAULT_PARAMS)
    if unknown:
        raise ValueError(f"Bilinmeyen param anahtarları: {unknown} — yazım hatası olabilir "
                         f"(geçerli: {sorted(DEFAULT_PARAMS)})")
    validate_building_row(row)
    refparcela = row["refparcela"]
    ground_unc = bool(P["ground_unconditioned"])      # bina başına zemin kuralı (N-bina)
    n_res = int(row["altura_max"])                    # konut katı sayısı (kadastro)
    n_total = n_res + (1 if ground_unc else 0)        # + zemin ticari kat

    coords, area = prepare_footprint(clean_polygon(row.geometry))
    x0, y0 = coords[0]                                # origin kaydırması: OpenStudio
                                                      # koordinatları küçük ister;
                                                      # ilk köşeyi (0,0) yapıyoruz.

    osm = load_template()
    building = osm.getBuilding()
    building.setName(str(refparcela))
    building.setNorthAxis(0.0)                        # model +Y = gerçek Kuzey (EPSG:25830)

    # --- Template kütüphanesinden ihtiyaçları bul ------------------------------
    # (adlar template'te İspanyolca; probe ile doğrulandı)
    space_type_res = None      # konut katları için
    space_type_ground = None   # şartlandırılmamış zemin için
    for st in osm.getSpaceTypes():
        if st.nameString() == "Espacio Tipo Vivienda CTE":
            space_type_res = st
        elif st.nameString() == "Espacio Tipo No habitable 1ACH":
            space_type_ground = st
    if space_type_res is None:
        raise RuntimeError("Template'te 'Espacio Tipo Vivienda CTE' yok!")
    if ground_unc and space_type_ground is None:
        # Sessiz geçilirse zemin kat TİPSİZ kalır → 1 ACH tampon sızıntısı
        # kaybolur ve model fiziği fark edilmeden bozulur (Codex bulgusu P2).
        raise RuntimeError("Template'te 'Espacio Tipo No habitable 1ACH' yok! "
                           "Zemin tampon katı (ground_unconditioned=True) bu tipi gerektirir.")

    sch_heat = sch_cool = None
    for sch in osm.getScheduleRulesets():
        if sch.nameString() == "T Calefaccion vivienda CTE":
            sch_heat = sch
        elif sch.nameString() == "T refrigeracion vivienda CTE":
            sch_cool = sch
    if sch_heat is None or sch_cool is None:
        raise RuntimeError("Template'te CTE ısıtma/soğutma termostat programları yok!")

    # Termostat: CTE konut sıcaklık programlarıyla çift ayar noktalı termostat.
    thermostat = openstudio.model.ThermostatSetpointDualSetpoint(osm)
    thermostat.setName("Termostat Vivienda CTE (pipeline)")
    thermostat.setHeatingSetpointTemperatureSchedule(sch_heat)
    thermostat.setCoolingSetpointTemperatureSchedule(sch_cool)

    # --- Geometri: her kat için footprint'ten space üret ----------------------
    # (manuel karşılığı: SketchUp'ta kat kat çizip 'Create Spaces from Diagram')
    for i in range(n_total):
        z = FLOOR_H * i
        pts = [openstudio.Point3d(x - x0, y - y0, z) for x, y in coords]
        opt_space = openstudio.model.Space.fromFloorPrint(pts, FLOOR_H, osm)
        if opt_space.isNull():
            raise RuntimeError(f"Kat {i} space üretilemedi (köşe sırası saat yönünde mi?)")

    # Space'leri kat sırasına diz (z'ye göre) — getSpaces() sırası garanti değil.
    spaces = sorted(osm.getSpaces(),
                    key=lambda s: min(v.z() for srf in s.surfaces() for v in srf.vertices()))

    # --- Kat/zon/tip atamaları -------------------------------------------------
    zone_of_space = {}
    residential_spaces = []
    for i, sp in enumerate(spaces):
        is_ground = ground_unc and i == 0
        label = "Zemin (ticari-tampon)" if is_ground else f"Kat {i}"

        story = openstudio.model.BuildingStory(osm)
        story.setName(f"Story {i} - {label}")
        sp.setBuildingStory(story)
        sp.setName(f"Space {i} - {label}")

        zone = openstudio.model.ThermalZone(osm)
        zone.setName(f"Zone {i} - {label}")
        sp.setThermalZone(zone)
        zone_of_space[sp.nameString()] = zone

        if is_ground:
            # Zemin: yaşanmayan alan tipi (1 ACH sızıntı), ISITMA/SOĞUTMA YOK →
            # serbest salınımlı tampon bölge. Termostat ve ideal loads atanmaz.
            # (Tipin varlığı yukarıda fail-fast ile garanti edildi.)
            sp.setSpaceType(space_type_ground)
        else:
            sp.setSpaceType(space_type_res)
            residential_spaces.append(sp)
            # Ideal Air Loads: DOĞRUDAN ZoneHVACIdealLoadsAirSystem objesi kur.
            # NEDEN böyle? Gauthier'in kullandığı zone.setUseIdealAirLoads(True)
            # bayrağı IDF'e 'HVACTemplate:...' yazar ve E+ öncesi ExpandObjects
            # çalıştırmayı ZORUNLU kılar (Gauthier o yüzden çalıştırıyordu).
            # Açık obje kurunca IDF'e gerçek ZoneHVAC:IdealLoadsAirSystem yazılır
            # → ExpandObjects tamamen gereksizleşir. (İlk denemede E+ 'HVACTemplate
            # objects found' hatası verdi; bu satırlar o hatanın çözümü.)
            ideal = openstudio.model.ZoneHVACIdealLoadsAirSystem(osm)
            ideal.setName(f"Ideal Loads {label}")
            ideal.addToThermalZone(zone)
            # CTE havalandırması: template'in space type'ı 'Ventilacion mecanica
            # 4l/s/persona' tanımlıyor ama ideal loads'a otomatik BAĞLANMIYOR —
            # bağlamazsak kışın dış hava yükü 0 olur ve ısıtma gerçek dışı düşük
            # çıkar (ilk koşuda 3 kWh/m² çıkmasının ana sebeplerinden biri).
            dsoa = space_type_res.designSpecificationOutdoorAir()
            if not dsoa.isNull():
                ideal.setDesignSpecificationOutdoorAirObject(dsoa.get())
            zone.setThermostatSetpointDualSetpoint(thermostat)

    # --- Surface Matching ------------------------------------------------------
    # Üst üste duran katların taban/tavanlarını eşleştir: bu yüzeyler artık dış
    # yüzey değil, katlar arası iç yüzey olur (manuel 'Surface Matching' aracı).
    for i in range(len(spaces) - 1, 0, -1):
        spaces[i].intersectSurfaces(spaces[i - 1])
        spaces[i].matchSurfaces(spaces[i - 1])

    # --- Party wall'ları adyabatik yap ----------------------------------------
    # Komşuyla paylaşılan kenara oturan TÜM duvar yüzeyleri (her katta) bulunur.
    # Adyabatik = ısı geçişi yok: iki tarafta da benzer sıcaklıkta yaşam alanı
    # varsayımı (ikiz bina, künye §6).
    party_buf = None
    if party_geom is not None:
        party_buf = party_geom.buffer(PARTY_WALL_TOL)
    n_party = 0
    party_surfaces = []
    for srf in osm.getSurfaces():
        if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
            continue
        # Eşleşme kriteri (2026-07-08 şehir genellemesi düzeltmesi): eski
        # `.within(party_buf)` duvarın TAMAMININ tampon içinde olmasını istiyordu.
        # Pilotta party wall cephenin tamamıydı → sorun görünmedi; şehir stoğunda
        # KISMİ paylaşımlar var (örn. 18 m'lik ortak sınır 20+ m'lik duvarın
        # parçası) ve within sessizce kaçırıyordu (3 temsilîde tespit edilen
        # party wall modele hiç işlenmemişti). Yeni kriter: duvar plan izinin
        # tampon içindeki örtüşme uzunluğu duvarın yarısından fazlaysa adyabatik.
        # Tam-paylaşımlı duvarda örtüşme = tam boy → pilot davranışı DEĞİŞMEZ
        # (regresyonla kanıtlandı). Bilinen sınır: %50 altı kısmi paylaşım hâlâ
        # dış duvar kalır (doğru çözüm yüzey bölme — kapsam dışı, belgelendi).
        if party_buf is not None:
            seg = _wall_plan_segment(srf, x0, y0)
            if seg.intersection(party_buf).length > 0.5 * seg.length:
                srf.setOutsideBoundaryCondition("Adiabatic")
                party_surfaces.append(srf)
                n_party += 1

    # --- DÖNEM CONSTRUCTION'LARI (1974, yalıtımsız) -----------------------------
    # Template'in space type default'u 'Aislamiento interior...' (İÇTEN YALITIMLI
    # cephe + ahşap eğimli kiremit çatı) — bu bir REHABİLİTASYON seti, 1974
    # yalıtımsız düz çatılı binamız için yanlış (ilk koşuda ısıtmanın 3 kWh/m²
    # çıkmasının diğer ana sebebi). Template kütüphanesinde dönem-uyumlu
    # yalıtımsız construction'lar hazır — onları AÇIKÇA atıyoruz:
    #   dış duvar  → IVE tuğla duvarı (_build_ive_brick_wall; Javier soru #10 onayı:
    #                 delikli tuğla+hava boşluğu+iç kabuk, base U=1,33 → +TB efektif 1,43)
    #   düz çatı   → 'Cubierta plana no aislada' (yalıtımsız düz çatı — Street View teyitli düz çatı)
    #   party wall → 'Medianera Referencia B'    (medianera = bitişik bina duvarı; adyabatikte
    #                                             sadece ısıl kütle rolü var)
    # Zemine oturan döşeme ('Solera sin aislante') ve katlar arası döşemeler
    # ('Suelo/Techo sin aislante') zaten default set'ten yalıtımsız geliyor ✓.
    # NOT: Adyabatik yüzeyler default set'lerden construction ALAMAZ (OpenStudio
    # default'ları Adiabatic'i kapsamıyor) — açık atama bu yüzden de şart, yoksa
    # E+ 'missing construction_name' hatası verir (ilk denemede yaşandı).
    def _get_construction(name: str):
        for c in osm.getConstructions():
            if c.nameString() == name:
                return c
        raise RuntimeError(f"Template'te '{name}' construction'ı bulunamadı!")

    # LHS modu: wall_u/roof_u örneklenmişse template construction'ı yerine
    # hedef U'dan üretilen massless eşdeğer kullanılır (film dirençleri:
    # duvar 0,13+0,04=0,17; çatı 0,10+0,04=0,14 m²K/W).
    # v3 termal köprü: her iki yolda da duvarın EFEKTİF U'suna ΔU_tb eklenir
    # (TABULA battaniye eki; masif yolda kütle korunarak — bkz. yardımcı fonksiyon).
    du = P["thermal_bridge_du"] or 0.0
    if P["wall_u"] is not None and P["massless"]:      # LHS yolu (eski davranış, birebir)
        era_wall = _massless_construction(
            osm, f"Duvar U={P['wall_u']:.2f}+TB{du:.2f} (LHS)", P["wall_u"] + du, 0.17)
    else:
        # 2026-07-10: HERHANGİ bir bina için gerçek katmanlı duvar. wall_u
        # verildiyse (N-bina/şehir: cluster'ın TABULA U'su) o hedefe, verilmediyse
        # pilot default'una (IVE 1.33) kurulur; TB üstüne eklenir (çift sayma yok).
        base_wall = _build_layered_wall(osm, P["wall_u"] if P["wall_u"] is not None else 1.33)
        if du > 0:
            era_wall = _construction_with_delta_u(
                osm, base_wall, du, 0.17,
                f"{base_wall.nameString()} +TB(dU={du:.2f})")
        else:
            era_wall = base_wall
    if P["roof_u"] is not None:
        if P["massless"]:                              # LHS yolu (eski davranış)
            era_roof = _massless_construction(osm, f"Cati U={P['roof_u']:.2f} (LHS)",
                                              P["roof_u"], 0.14)
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

    # --- Pencereler (v2: GERÇEK cephe düzeni) -----------------------------------
    # Sadece KONUT katlarının DIŞ duvarlarına. Bant (setWindowToWallRatio) yerine
    # AYRIK açıklıklar: uçlarda 2 balkon kapısı (üstlerinde balkon çıkması =
    # gölgeleme) + arada WWR alanını tamamlayan pencereler — Street View sayımı
    # (künye §7.2: SE'de 5 pencere + 2 balkon kapısı/kat) böyle yeniden üretilir.
    # Cam: IVE tek cam alüminyum — SimpleGlazing (U ve g, params'tan → LHS).
    glazing = openstudio.model.SimpleGlazing(osm)
    glazing.setName("IVE 1960-80 tek cam aluminyum")
    glazing.setUFactor(P["window_u"])
    glazing.setSolarHeatGainCoefficient(P["window_g"])
    win_construction = openstudio.model.Construction(osm)
    win_construction.setName("Pencere IVE 1960-80")
    win_construction.insertLayer(0, glazing)

    # Persiana kontrolü (v2): template'in blind malzemesi + "güneş eşiği aşarsa
    # kapat" + sadece yaz programı. Setpoint params'tan (kullanım alışkanlığı
    # belirsiz → LHS değişkeni).
    shading_control = None
    for blind in osm.getBlinds():
        if blind.nameString() == SHADE_BLIND_NAME:
            shading_control = openstudio.model.ShadingControl(blind)
            shading_control.setName("Persiana kontrolu (yaz)")
            shading_control.setShadingType("ExteriorBlind")
            shading_control.setShadingControlType("OnIfHighSolarOnWindow")
            shading_control.setSetpoint(P["shade_setpoint"])
            shading_control.setSchedule(_summer_schedule(osm))
            break
    if shading_control is None:
        print(f"[uyarı] template'te '{SHADE_BLIND_NAME}' yok — persiana kontrolsüz devam")

    residential_names = {sp.nameString() for sp in residential_spaces}
    n_windows = 0
    n_doors = 0
    window_area = 0.0
    facade_qa = {}      # azimut → hedef/gerçekleşen WWR toplulaması (QA raporu için)
    for srf in osm.getSurfaces():
        if srf.surfaceType() != "Wall" or srf.outsideBoundaryCondition() != "Outdoors":
            continue
        sp = srf.space()
        if sp.isNull() or sp.get().nameString() not in residential_names:
            continue
        az = openstudio.radToDeg(srf.azimuth())       # duvarın dışa bakan pusula açısı
        ratio = wwr_for_azimuth(az)
        # DİKKAT (debug #6): dönüş değişkeni 'area' ADLANDIRILAMAZ — yukarıdaki
        # footprint 'area'sını gölgeleyip ezer (yaşandı: taban 562→18 m² olup
        # kWh/m² 31 kat şişmişti). Ayrı isim kullan:
        n_w, n_d, glass_area, target_area = _add_facade_openings(
            osm, srf, ratio, win_construction, shading_control)
        n_windows += n_w
        n_doors += n_d
        window_area += glass_area
        key = round(az, 1)
        agg = facade_qa.setdefault(key, {"azimut": key, "wwr_hedef": round(ratio, 3),
                                         "duvar_m2": 0.0, "hedef_m2": 0.0,
                                         "cam_m2": 0.0, "pencere": 0, "kapi": 0})
        agg["duvar_m2"] += srf.grossArea()
        agg["hedef_m2"] += target_area
        agg["cam_m2"] += glass_area
        agg["pencere"] += n_w
        agg["kapi"] += n_d
    # Gerçekleşen WWR + hedeften sapma (%) — açıklıklar 1,44/2,52 m²'lik adımlarla
    # yerleştiği için küçük sapma NORMALDİR; QA raporu %15 üstünü işaretler.
    for agg in facade_qa.values():
        agg["wwr_gercek"] = round(agg["cam_m2"] / agg["duvar_m2"], 3) if agg["duvar_m2"] else 0.0
        agg["sapma_pct"] = (round(100.0 * (agg["cam_m2"] - agg["hedef_m2"]) / agg["hedef_m2"], 1)
                            if agg["hedef_m2"] else 0.0)
        for k in ("duvar_m2", "hedef_m2", "cam_m2"):
            agg[k] = round(agg[k], 1)

    # --- Sızıntı (infiltration) parametresi — LHS -------------------------------
    # Template space type'ı sabit 0,2 ACH sızıntı içeriyor; belirsizlik koşusunda
    # bu değer örneklenir (taze model kopyası olduğu için değişiklik güvenli).
    if P["infiltration_ach"] is not None:
        for inf in osm.getSpaceInfiltrationDesignFlowRates():
            if "constante" in inf.nameString():
                inf.setAirChangesperHour(P["infiltration_ach"])

    # --- v3: Komşu gölge kütleleri ----------------------------------------------
    n_shading = 0
    if P["context_shading"]:
        nb = neighbors
        if nb is None:
            nb = load_neighbors(clean_polygon(row.geometry), refparcela, NEIGHBORS_SHP)
        n_shading = _add_context_shading(osm, nb, x0, y0)
        # E+ gölge örtüşme limiti default 15.000 figür — komşu kütleler + 130
        # açıklık + 30 overhang kombinasyonu bunu aşıyor ('DeterminePolygonOverlap:
        # Too many figures' uyarısı ilk v3 koşusunda yakalandı). Limit yükseltilir;
        # DOĞRULANDI: 15k/60k limit ve 89/73 yüzey varyantlarında yıllık sonuçlar
        # BİREBİR aynı (16,70/18,64) → uyarı kalsa bile sonuca etkisi ihmal
        # düzeyinde (deney notunda belgeli). Yine de mümkün olan en yüksek
        # kapsama için 200.000 kullanılır.
        osm.getShadowCalculation().setMaximumFiguresInShadowOverlapCalculations(200000)

    # --- Hava durumu + çıktı değişkenleri --------------------------------------
    epw = openstudio.EpwFile(openstudio.toPath(str(EPW_FILE)))
    openstudio.model.WeatherFile.setWeatherFile(osm, epw)

    # Yıllık toplam ısıtma/soğutma enerjisi (ideal loads) — RunPeriod frekansı
    # → zon başına TEK yıllık değer. Adlar OUTPUT_VARIABLES sabitinden: read_results
    # da AYNI sabiti kullanır (tanım-okuma tutarlılığı tek yerden garanti).
    for var_name in OUTPUT_VARIABLES.values():
        var = openstudio.model.OutputVariable(var_name, osm)
        var.setReportingFrequency("RunPeriod")

    stats = {
        "refparcela": refparcela,
        "footprint_m2": round(area, 1),
        "n_floors_total": n_total,
        "n_floors_residential": n_res,
        "res_area_m2": round(area * n_res, 1),        # konut alanı = taban × konut katı
        "n_party_surfaces": n_party,
        "n_windows": n_windows,
        "n_balcony_doors": n_doors,
        "window_area_m2": round(window_area, 1),
        "n_shading_surfaces": n_shading,              # v3: komşu gölge yüzeyleri
        # 2026-07-10: hangi duvar/çatı construction'ı kullanıldı (rejim + U izlenebilir)
        "wall_construction": era_wall.nameString(),
        "roof_construction": era_roof.nameString(),
        "facade_qa": list(facade_qa.values()),        # QA raporuna gider (CSV'ye girmez)
    }
    return osm, stats


# ============================================================================
# 5) MODEL KAYDETME + ANA DÖNGÜ — N bina (v1: 1 bina) — E+ YOK!
# ============================================================================

def save_model(osm, run_dir: Path) -> Path:
    """Modeli .osm dosyası olarak kaydet (bina başına alt klasör).
    Kaydedilen dosya OpenStudio Application ile açılıp gözle denetlenebilir
    veya plot_model_claude.py ile PNG render alınabilir. Simülasyon dosyası
    (run_simulation_claude.py) koşu çıktılarını AYNI klasöre yazar — model +
    sonuçlar bina başına tek yerde durur.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    osm_path = run_dir / "model_python.osm"
    if not osm.save(openstudio.toPath(str(osm_path)), True):
        # save() bool döner — kontrol edilmezse yazılamayan dosya (disk dolu,
        # izin sorunu) 'kaydedildi' sanılırdı (Codex bulgusu P3).
        raise RuntimeError(f"Model kaydedilemedi: {osm_path}")
    return osm_path


def main():
    """Her bina için: GIS oku → 3D model kur → .osm kaydet. SİMÜLASYON YOK —
    bu dosyanın işi modeli kurmak ve görsel denetime hazır etmek."""
    t0 = datetime.now()
    validate_input_files()                            # fail-fast: eksik dosya varsa hiç başlama
    buildings = load_buildings(BUILDINGS_GPKG)

    for _, row in buildings.iterrows():
        ref = row["refparcela"]
        print(f"\n===== BİNA: {ref} =====")

        geom = clean_polygon(row.geometry)
        neighbors = load_neighbors(geom, ref, NEIGHBORS_SHP)   # tek okuma: party + gölge
        party = find_party_walls(geom, ref, NEIGHBORS_SHP, neighbors=neighbors)

        osm, stats = build_model(row, party, neighbors=neighbors)
        print(f"[model] taban {stats['footprint_m2']} m² × {stats['n_floors_total']} seviye "
              f"({stats['n_floors_residential']} konut) | party yüzey: {stats['n_party_surfaces']} "
              f"| {stats['n_windows']} pencere + {stats['n_balcony_doors']} balkon kapısı = "
              f"{stats['window_area_m2']} m² cam | {stats['n_shading_surfaces']} komşu gölge yüzeyi")

        osm_path = save_model(osm, OUT_DIR / str(ref))
        print(f"[kayıt] {osm_path}")

    print("\n[not] Bu script SADECE 3D modeli kurar (EnergyPlus YOK).")
    print("      Görsel kontrol: OpenStudio Application ile .osm'i aç VEYA")
    print("      .venv/bin/python src/reference/plot_model_claude.py")
    print("      Simülasyon:  .venv/bin/python src/reference/run_simulation_claude.py")
    print(f"Süre: {(datetime.now() - t0).total_seconds():.0f} sn")


if __name__ == "__main__":
    main()
