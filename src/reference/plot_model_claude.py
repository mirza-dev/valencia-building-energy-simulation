"""3D model görsel doğrulama — üretilen OSM'i matplotlib ile çizer.

NE İÇİN: Python'un kurduğu geometriyi GÖZLE denetlemek (SketchUp'sız kontrol):
  - Footprint paralelkenar mı, 6 seviye var mı?
  - Pencereler SADECE konut katlarında (1-5) ve dış cephelerde mi?
  - NW party wall KIRMIZI (adyabatik) ve PENCERESİZ mi?
  - Zemin kat penceresiz tampon mu?

Renk kodu (OpenStudio Application'ın 'Render by Boundary Condition' mantığı):
  mavi   = dış duvar/çatı (Outdoors)      kırmızı = adyabatik (party wall)
  yeşil  = katlar arası iç yüzey (Surface) kahve   = zemine oturan (Ground)
  camgöbeği = pencere (SubSurface)

ÇALIŞTIRMA:
  .venv/bin/python src/reference/plot_model_claude.py
Çıktı: out/<refparcela>/model_3d.png (4 açıdan görünüm)
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")                      # ekransız (headless) çizim
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import openstudio

PROJECT = Path(__file__).resolve().parents[2]
OSM_PATH = PROJECT / "out/4252702YJ2745A/model_python.osm"
PNG_PATH = OSM_PATH.parent / "model_3d.png"

OBC_COLOR = {
    "Outdoors": ("#4a90d9", 0.45),         # mavi, yarı saydam
    "Adiabatic": ("#d64545", 0.85),        # kırmızı, belirgin (party wall!)
    "Surface": ("#5cb85c", 0.15),          # yeşil, çok saydam (iç yüzeyler)
    "Ground": ("#8b6f47", 0.6),            # kahverengi
}


def surface_polys(model):
    """Model yüzeylerini (çokgen köşe listesi, renk, saydamlık) olarak topla."""
    polys = []
    for srf in model.getSurfaces():
        pts = [(v.x(), v.y(), v.z()) for v in srf.vertices()]
        color, alpha = OBC_COLOR.get(srf.outsideBoundaryCondition(), ("#999999", 0.3))
        polys.append((pts, color, alpha))
        # Pencereler (SubSurface) üstüne camgöbeği; balkon kapıları koyu mavi
        for ss in srf.subSurfaces():
            wpts = [(v.x(), v.y(), v.z()) for v in ss.vertices()]
            color = "#0077cc" if ss.subSurfaceType() == "GlassDoor" else "#00d0d0"
            polys.append((wpts, color, 0.95))
    # Gölge yüzeyleri iki türlü (grup tipinden ayırt edilir):
    #   Space grubu = balkon çıkmaları (addOverhang) → koyu gri plakalar
    #   Site grubu  = v3 komşu gölge kütleleri → çok saydam açık gri (binayı örtmesin)
    # (fromFloorPrint space'leri origin=0 olduğundan iki çerçeve de model
    # koordinatlarıyla çakışık — vertex'ler mutlak kabul edilebilir.)
    for sh in model.getShadingSurfaces():
        spts = [(v.x(), v.y(), v.z()) for v in sh.vertices()]
        grp = sh.shadingSurfaceGroup()
        is_context = (not grp.isNull()) and grp.get().shadingSurfaceType() == "Site"
        if is_context:
            polys.append((spts, "#aaaaaa", 0.10))
        else:
            polys.append((spts, "#666666", 0.9))
    return polys


def main():
    tr = openstudio.osversion.VersionTranslator()
    model = tr.loadModel(openstudio.toPath(str(OSM_PATH))).get()
    polys = surface_polys(model)

    # Eksen sınırları: SADECE bina yüzeylerinden (Surface) hesaplanır — komşu
    # gölge kütleleri 50 m çevreyi kapsar, onlara göre ölçeklersek bina minicik
    # kalır. Komşular kadraj dışına taşar ve kenarlarda kırpılır (istenen bu).
    bld_pts = np.array([(v.x(), v.y(), v.z())
                        for srf in model.getSurfaces() for v in srf.vertices()])
    mins, maxs = bld_pts.min(axis=0), bld_pts.max(axis=0)
    center, span = (mins + maxs) / 2, (maxs - mins).max() / 2
    span *= 1.35                                       # komşulardan bir şerit görünsün

    # 4 açı: her cepheyi ayrı gör (azim = bakış yönü; model +Y = Kuzey)
    views = [
        ("Güneyden (SE + SW cepheler — pencereli olmalı)", 25, -120),
        ("Doğudan (NE + SE cepheler — pencereli olmalı)", 25, -30),
        ("Kuzeyden (NW PARTY WALL — kırmızı + penceresiz olmalı)", 25, 60),
        ("Üstten (paralelkenar footprint)", 88, -90),
    ]

    fig = plt.figure(figsize=(16, 12))
    for i, (title, elev, azim) in enumerate(views, 1):
        ax = fig.add_subplot(2, 2, i, projection="3d")
        for pts, color, alpha in polys:
            ax.add_collection3d(Poly3DCollection([pts], facecolor=color, alpha=alpha,
                                                 edgecolor="#333333", linewidth=0.4))
        ax.set_xlim(center[0] - span, center[0] + span)
        ax.set_ylim(center[1] - span, center[1] + span)
        ax.set_zlim(0, 2 * span)
        ax.view_init(elev=elev, azim=azim)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("x (m)"); ax.set_ylabel("y (m → Kuzey)"); ax.set_zlabel("z (m)")

    fig.suptitle("4252702YJ2745A — Python'un kurduğu 3D model (mavi=dış, kırmızı=party wall, "
                 "camgöbeği=pencere, yeşil=iç, kahve=zemin, açık gri=komşu gölge kütleleri)",
                 fontsize=13)
    fig.tight_layout()
    fig.savefig(PNG_PATH, dpi=110)
    print(f"Kaydedildi: {PNG_PATH}")


if __name__ == "__main__":
    main()
