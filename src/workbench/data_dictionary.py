"""Curated GIS semantics, coverage evidence, and read-only workflow contracts.

The registry is deliberately explicit: code can prove where a field is read, but
it cannot infer a scientifically honest human meaning from a column name.  The
coverage layer is snapshot-keyed so immutable datasets are analysed only once
per Workbench process.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
import numpy as np
import pandas as pd

from workbench import db


PROJECT = Path(__file__).resolve().parents[2]
TIPO15_PATH = PROJECT / "data/reference/Tipo15_soloV(in).csv"
REGISTRY_VERSION = "2026-07-22.1"
WORKFLOWS = ("builder", "simulation", "neighborhood", "city", "lhs")

Effect = Literal[
    "physical_model", "stock_method", "scaling", "validation_only",
    "reporting_only", "not_used",
]


def _field(
    meaning_en: str,
    meaning_tr: str,
    *,
    effect: Effect,
    workflows: tuple[str, ...] = (),
    source: str = "Valencia city cadastral GIS supplied by Rai",
    status: str = "confirmed",
    validity: str = "present",
    scope_en: str | None = None,
    scope_tr: str | None = None,
) -> dict[str, Any]:
    return {
        "meaning": {"en": meaning_en, "tr": meaning_tr},
        "effect": effect,
        "workflows": list(workflows),
        "source": source,
        "definition_status": status,
        "validity": validity,
        "scope": {"en": scope_en, "tr": scope_tr} if scope_en or scope_tr else None,
    }


FIELD_REGISTRY: dict[str, dict[str, Any]] = {
    "geometry": _field(
        "Building footprint geometry used to create the model footprint and context.",
        "Model footprint'i ve bağlamı oluşturmak için kullanılan bina geometrisi.",
        effect="physical_model", workflows=("builder", "neighborhood", "city"), validity="geometry",
    ),
    "refparcela": _field(
        "Cadastral parcel reference used as building identity and the Tipo15 join key.",
        "Bina kimliği ve Tipo15 join anahtarı olarak kullanılan kadastro parsel referansı.",
        effect="stock_method", workflows=("builder", "neighborhood", "city"), validity="nonblank",
    ),
    "altura_max": _field(
        "Floor count used to construct the building height and storey geometry.",
        "Bina yüksekliği ve kat geometrisini oluşturmak için kullanılan kat sayısı.",
        effect="physical_model", workflows=("builder", "neighborhood", "city"), validity="floors",
    ),
    "cluster": _field(
        "Assigned TABULA family + period code. It selects envelope U-values, glazing g-value, and the family ground-floor default; it does not set infiltration, schedules, setpoints, or HVAC.",
        "Atanmış TABULA aile + dönem kodu. Kabuk U-değerlerini, cam g-değerini ve aile zemin-kat varsayımını seçer; infiltrasyon, program, setpoint veya HVAC belirlemez.",
        effect="physical_model", workflows=("builder", "neighborhood", "city"), validity="cluster",
    ),
    "Shape_Area": _field(
        "Raw footprint area in m². It affects representative selection, residential-area proxy scaling, and duplicate-parcel apportioning; it is not residential floor area.",
        "Ham footprint alanı (m²). Temsilci seçimini, konut-alanı proxy ölçeklemesini ve tekrarlı parsel paylaştırmasını etkiler; konut kat alanı değildir.",
        effect="stock_method", workflows=("neighborhood", "city"), validity="positive",
    ),
    "nombre": _field(
        "Municipal district name used to select and aggregate district scope.",
        "İlçe kapsamını seçmek ve toplamak için kullanılan belediye ilçe adı.",
        effect="stock_method", workflows=("neighborhood", "city"), validity="nonblank",
    ),
    "coddistrit": _field(
        "Municipal district code retained for reporting; district aggregation is name-keyed.",
        "Raporlama için korunan belediye ilçe kodu; ilçe toplaması isim anahtarlıdır.",
        effect="reporting_only", workflows=("city",), validity="nonblank",
    ),
    "demanda_ca": _field(
        "External-source heating demand (kWh/m²). Its calculation method and floor-area basis are not documented in the supplied dataset.",
        "Dış kaynaklı ısıtma talebi (kWh/m²). Hesap yöntemi ve alan tabanı sağlanan veri setinde belgelenmemiştir.",
        effect="validation_only", workflows=("simulation", "neighborhood", "city", "lhs"),
        status="partially_confirmed", validity="positive",
        source="External source; heating meaning confirmed by Rai, calculation not produced by Rai/Javier",
    ),
    "demanda__1": _field(
        "Unconfirmed reference field. Internal evidence suggests post-intervention heating demand; it must not be presented as cooling demand.",
        "Teyit edilmemiş referans alanı. İç kanıt müdahale-sonrası ısıtma talebine işaret eder; soğutma talebi olarak sunulmamalıdır.",
        effect="validation_only", workflows=("simulation", "neighborhood", "city", "lhs"),
        status="unconfirmed", validity="positive",
    ),
    "ConsumE": _field(
        "Building-level energy-consumption intensity from Rai's reference city calculation (kWh/m²); the author-reported reference level is 47 kWh/m².",
        "Rai'nin referans şehir hesabından bina-bazlı enerji tüketim yoğunluğu (kWh/m²); yazarın bildirdiği referans düzey 47 kWh/m²'dir.",
        effect="validation_only", workflows=("city",), validity="positive",
        source="Rai reference city calculation",
        scope_en="Rai confirms that DHW is included and ground floors are excluded from the residential basis; he recalls using heat pumps for all systems. Compare with like-for-like consumption, not heating/cooling demand.",
        scope_tr="Rai, DHW'nin dahil ve zemin katların konut alanı tabanı dışında olduğunu teyit eder; tüm sistemlerde ısı pompası kullandığını hatırlamaktadır. Isıtma/soğutma talebiyle değil, eş kapsamlı tüketimle karşılaştırılmalıdır.",
    ),
    "ConsumETot": _field(
        "Total-consumption companion output from the same Rai calculation. The supplied shapefile values are all zero, so the field is unusable here and no unit is inferred.",
        "Aynı Rai hesabının toplam-tüketim eşlikçi çıktısı. Sağlanan shapefile değerlerinin tümü sıfırdır; bu nedenle alan burada kullanılamaz ve birim tahmin edilmez.",
        effect="validation_only", workflows=("city",), validity="positive",
        source="Rai reference city calculation", status="source_confirmed_values_unusable",
    ),
    "tipologia_": _field(
        "Cadastral building-type label used as a cross-check for the pre-assigned cluster family.",
        "Önceden atanmış cluster ailesini çapraz kontrol etmekte kullanılan kadastro bina tipi etiketi.",
        effect="reporting_only", workflows=("neighborhood", "city"), validity="nonblank",
    ),
    "ano_constr": _field(
        "Construction year carried for reporting and cluster-period cross-checking; the current pipeline reads the assigned cluster directly.",
        "Raporlama ve cluster dönemi çapraz kontrolü için taşınan yapım yılı; mevcut pipeline atanmış cluster'ı doğrudan okur.",
        effect="reporting_only", workflows=("neighborhood", "city"), validity="year",
    ),
    "uso_princi": _field(
        "Primary-use label retained for reporting and cross-checking. The current stock loader does not actively filter on this field.",
        "Raporlama ve çapraz kontrol için korunan ana kullanım etiketi. Mevcut stok yükleyici bu alanla aktif filtre yapmaz.",
        effect="reporting_only", workflows=("neighborhood", "city"), validity="nonblank",
    ),
    "pob_total": _field(
        "Population total used in external population-scaled validation, not in the energy model.",
        "Enerji modelinde değil, dış nüfus-oranlı validasyonda kullanılan toplam nüfus.",
        effect="validation_only", workflows=("neighborhood", "city"), validity="nonnegative",
    ),
    "pob_0_14": _field("Population aged 0–14; retained for reporting and future behaviour studies.", "0–14 yaş nüfusu; raporlama ve gelecekteki davranış çalışmaları için korunur.", effect="reporting_only", workflows=("neighborhood", "city"), validity="nonnegative"),
    "pob_15_65": _field("Population aged 15–65; retained for reporting and future behaviour studies.", "15–65 yaş nüfusu; raporlama ve gelecekteki davranış çalışmaları için korunur.", effect="reporting_only", workflows=("neighborhood", "city"), validity="nonnegative"),
    "pob_66_mas": _field("Population aged 66+; retained for reporting and future behaviour studies.", "66+ yaş nüfusu; raporlama ve gelecekteki davranış çalışmaları için korunur.", effect="reporting_only", workflows=("neighborhood", "city"), validity="nonnegative"),
    "num_vivend": _field("Dwelling count used in household-level reporting checks.", "Hane-seviyesi raporlama kontrollerinde kullanılan daire sayısı.", effect="reporting_only", workflows=("neighborhood", "city"), validity="nonnegative"),
    "numero_viv": _field("Cadastral dwelling-count/class field retained for reporting; supplied values may be empty.", "Raporlama için korunan kadastro daire-sayısı/sınıfı alanı; sağlanan değerler boş olabilir.", effect="reporting_only", workflows=("neighborhood", "city"), validity="nonblank"),
    "calificaci": _field("Pre-intervention energy-certificate letter grade.", "Müdahale-öncesi enerji sertifikası harf notu.", effect="reporting_only", workflows=("neighborhood", "city"), validity="grade"),
    "califica_1": _field("Post-intervention energy-certificate letter grade.", "Müdahale-sonrası enerji sertifikası harf notu.", effect="reporting_only", workflows=("neighborhood", "city"), validity="grade"),
    "coste_inte": _field("Intervention cost intensity (€/m²) used in the separate cost-effectiveness analysis.", "Ayrı maliyet-etkinlik analizinde kullanılan müdahale maliyet yoğunluğu (€/m²).", effect="reporting_only", workflows=("neighborhood", "city"), validity="positive"),
    "coste_in_1": _field("Total intervention cost per certificate dwelling.", "Sertifika dairesi başına toplam müdahale maliyeti.", effect="reporting_only", workflows=("neighborhood", "city"), validity="positive"),
    "31_pc": _field(
        "Parcel reference joining the Tipo15 dwelling ledger to GIS refparcela.",
        "Tipo15 daire kütüğünü GIS refparcela alanına bağlayan parsel referansı.",
        effect="stock_method", workflows=("neighborhood", "city"), validity="nonblank",
        source="Tipo15 dwelling ledger supplied by Rai",
    ),
    "442_sup_Residencial": _field(
        "Dwelling residential area (m²), summed by parcel to scale kWh/m² results to annual stock totals.",
        "kWh/m² sonuçlarını yıllık stok toplamına ölçeklemek için parsel bazında toplanan daire konut alanı (m²).",
        effect="scaling", workflows=("neighborhood", "city"), validity="positive",
        source="Tipo15 dwelling ledger supplied by Rai",
    ),
    "252_planta": _field(
        "Dwelling floor label used to determine whether a representative parcel has a residential ground floor.",
        "Temsilci parselde konut zemin katı olup olmadığını belirlemekte kullanılan daire kat etiketi.",
        effect="physical_model", workflows=("neighborhood", "city"), validity="nonblank",
        source="Tipo15 dwelling ledger supplied by Rai",
    ),
}


REPORTING_FIELDS = {
    "coorx", "coory", "the_geom_L", "the_geom_A", "Shape_Leng", "referencia",
    "codigo_ine", "nombre_mun", "codigo_pro", "zona_clima", "altura_m_1", "ano_cons_1",
}
for _name in REPORTING_FIELDS:
    FIELD_REGISTRY.setdefault(_name, _field(
        "Source attribute retained for traceability and reporting; it does not enter the current energy calculation.",
        "İzlenebilirlik ve raporlama için korunan kaynak alanı; mevcut enerji hesabına girmez.",
        effect="reporting_only", workflows=("neighborhood", "city"),
    ))


VALIDITY_TEXT = {
    "geometry": {
        "en": "Non-empty, valid Polygon or MultiPolygon geometry",
        "tr": "Boş olmayan, geçerli Polygon veya MultiPolygon geometri",
    },
    "nonblank": {"en": "Non-null, non-blank value", "tr": "Null olmayan, boş olmayan değer"},
    "floors": {"en": "Finite integer from 1 to 100", "tr": "1–100 arası sonlu tam sayı"},
    "cluster": {"en": "Recognised VivUni/EdiPluri/BlocPluri P01–P07 code", "tr": "Tanınan VivUni/EdiPluri/BlocPluri P01–P07 kodu"},
    "positive": {"en": "Finite value greater than zero", "tr": "Sıfırdan büyük sonlu değer"},
    "nonnegative": {"en": "Finite value greater than or equal to zero", "tr": "Sıfır veya daha büyük sonlu değer"},
    "year": {"en": "Finite year from 1000 to 2100", "tr": "1000–2100 arası sonlu yıl"},
    "grade": {"en": "Certificate grade A–G", "tr": "A–G sertifika notu"},
    "present": {"en": "Present value; no scientific rule is defined", "tr": "Mevcut değer; bilimsel geçerlilik kuralı tanımlı değil"},
}


def definition_for(field_name: str) -> dict[str, Any]:
    known = FIELD_REGISTRY.get(field_name)
    if known:
        return known
    return _field(
        "No meaning is registered for this field. It is not used by the current pipeline contract.",
        "Bu alan için kayıtlı bir anlam yoktur. Mevcut pipeline sözleşmesi tarafından kullanılmaz.",
        effect="not_used", status="unclassified", validity="present",
        source="Selected dataset",
    )


def _present_mask(series: pd.Series) -> pd.Series:
    present = series.notna()
    if pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(series.dtype):
        present &= series.astype("string").str.strip().fillna("").ne("")
    return present


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _usable_mask(series: pd.Series, rule: str) -> pd.Series:
    present = _present_mask(series)
    if rule == "geometry":
        usable = present.copy()
        usable &= ~series.is_empty
        usable &= series.is_valid
        usable &= series.geom_type.isin({"Polygon", "MultiPolygon"})
        return usable.fillna(False)
    if rule == "nonblank" or rule == "present":
        return present.fillna(False)
    if rule == "grade":
        return present & series.astype("string").str.strip().str.upper().isin(set("ABCDEFG"))
    if rule == "cluster":
        return present & series.astype("string").str.strip().str.match(
            r"^(VivUni|EdiPluri|BlocPluri)P0[1-7]$", na=False,
        )
    values = _numeric(series)
    finite = pd.Series(np.isfinite(values), index=series.index) & present
    if rule == "positive":
        return finite & values.gt(0)
    if rule == "nonnegative":
        return finite & values.ge(0)
    if rule == "floors":
        return finite & values.between(1, 100) & values.mod(1).eq(0)
    if rule == "year":
        return finite & values.between(1000, 2100)
    return present.fillna(False)


def _inferred_type(series: pd.Series) -> str:
    if isinstance(series.dtype, gpd.array.GeometryDtype):
        return "geometry"
    if pd.api.types.is_bool_dtype(series.dtype):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series.dtype):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return "date"
    return "text"


def _stats(field_name: str, series: pd.Series) -> dict[str, Any]:
    definition = definition_for(field_name)
    present = _present_mask(series)
    usable = _usable_mask(series, definition["validity"])
    total = int(len(series))
    present_count = int(present.sum())
    usable_count = int(usable.sum())
    values = _numeric(series) if _inferred_type(series) == "numeric" else None
    zero_count = int((values.eq(0) & present).sum()) if values is not None else 0
    evidence: dict[str, Any] = {}
    if field_name == "ConsumE" and usable_count:
        evidence = {
            "dataset_median": float(values[usable].median()),
            "author_reference": 47.0,
            "unit": "kWh/m²",
        }
    return {
        "field": field_name,
        "inferred_type": _inferred_type(series),
        "row_count": total,
        "present_count": present_count,
        "present_pct": round(100 * present_count / total, 3) if total else 0.0,
        "usable_count": usable_count,
        "usable_pct": round(100 * usable_count / total, 3) if total else 0.0,
        "missing_count": total - present_count,
        "invalid_count": present_count - usable_count,
        "zero_count": zero_count,
        "coverage_basis": "dataset_rows",
        "validity_rule": VALIDITY_TEXT[definition["validity"]],
        "evidence": evidence,
        **{key: value for key, value in definition.items() if key != "validity"},
    }


@lru_cache(maxsize=24)
def _analyse_gis(path_string: str, snapshot_hash: str) -> dict[str, Any]:  # noqa: ARG001
    frame = gpd.read_file(Path(path_string))
    fields = [_stats(str(name), frame[name]) for name in frame.columns]
    return {"rows": int(len(frame)), "columns": fields}


def _merge_column_stats(target: dict[str, Any], item: dict[str, Any]) -> None:
    target["row_count"] += item["row_count"]
    for key in ("present_count", "usable_count", "missing_count", "invalid_count", "zero_count"):
        target[key] += item[key]


@lru_cache(maxsize=8)
def _analyse_companion(path_string: str, snapshot_hash: str) -> dict[str, Any]:  # noqa: ARG001
    path = Path(path_string)
    aggregates: dict[str, dict[str, Any]] = {}
    rows = 0
    for chunk in pd.read_csv(path, sep=";", encoding="latin-1", chunksize=50_000, low_memory=False):
        rows += len(chunk)
        for name in chunk.columns:
            item = _stats(str(name), chunk[name])
            if name not in aggregates:
                aggregates[name] = item
            else:
                _merge_column_stats(aggregates[name], item)
    for item in aggregates.values():
        total = item["row_count"]
        item["present_pct"] = round(100 * item["present_count"] / total, 3) if total else 0.0
        item["usable_pct"] = round(100 * item["usable_count"] / total, 3) if total else 0.0
    return {"rows": int(rows), "columns": list(aggregates.values())}


@lru_cache(maxsize=16)
def _companion_join_coverage(gis_path: str, gis_hash: str, companion_hash: str) -> dict[str, Any]:  # noqa: ARG001
    frame = gpd.read_file(gis_path, columns=["refparcela"])
    if "refparcela" not in frame.columns:
        return {"available": False, "reason": "refparcela_not_available"}
    area_refs: set[str] = set()
    record_refs: set[str] = set()
    for chunk in pd.read_csv(
        TIPO15_PATH, sep=";", encoding="latin-1",
        usecols=["31_pc", "442_sup_Residencial"], chunksize=75_000,
        dtype={"31_pc": "string"}, low_memory=False,
    ):
        refs = chunk["31_pc"].astype("string").str.strip()
        record_refs.update(refs.dropna().loc[refs.dropna().ne("")].astype(str))
        area = pd.to_numeric(chunk["442_sup_Residencial"], errors="coerce")
        valid = refs.notna() & refs.ne("") & np.isfinite(area) & area.gt(0)
        area_refs.update(refs[valid].astype(str))
    gis_refs = frame["refparcela"].astype("string").str.strip()
    total = int(len(frame))
    area_joined = int(gis_refs.isin(area_refs).sum())
    ground_joined = int(gis_refs.isin(record_refs).sum())
    return {
        "available": True,
        "coverage_basis": "gis_building_rows_joined_by_refparcela_to_tipo15_31_pc",
        "building_rows": total,
        "residential_area": {
            "joined_count": area_joined,
            "joined_pct": round(100 * area_joined / total, 3) if total else 0.0,
        },
        "ground_rule": {
            "joined_count": ground_joined,
            "joined_pct": round(100 * ground_joined / total, 3) if total else 0.0,
        },
    }


def dataset_dictionary(dataset_id: str) -> dict[str, Any]:
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    if dataset["kind"] not in {"gis", "companion"}:
        raise ValueError("Data dictionaries are available for GIS and companion datasets")
    snapshot_hash = str(dataset.get("snapshot_hash") or dataset.get("sha256") or "")
    if dataset["kind"] == "gis":
        analysis = _analyse_gis(str(dataset["path"]), snapshot_hash)
    else:
        analysis = _analyse_companion(str(dataset["path"]), snapshot_hash)
    notes = db.dataset_field_notes(dataset_id)
    columns = []
    for item in analysis["columns"]:
        note = notes.get(item["field"], {})
        columns.append(item | {
            "user_note": note.get("note", ""),
            "user_semantic_label": note.get("semantic_label", ""),
            "note_updated_at": note.get("updated_at"),
        })
    companion_links: list[dict[str, Any]] = []
    companion = db.get_dataset("tipo15-ledger")
    if dataset["kind"] == "gis" and companion and TIPO15_PATH.exists():
        metadata_columns = set(dataset.get("metadata", {}).get("columns", []))
        join = (
            _companion_join_coverage(
                str(dataset["path"]), snapshot_hash,
                str(companion.get("snapshot_hash") or companion.get("sha256") or ""),
            )
            if "refparcela" in metadata_columns
            else {"available": False, "reason": "refparcela_not_available"}
        )
        companion_links.append({
            "dataset_id": companion["id"], "name": companion["name"],
            "source": "Tipo15 dwelling ledger", "join": join,
        })
    return {
        "schema_version": 1,
        "registry_version": REGISTRY_VERSION,
        "dataset": {
            "id": dataset["id"], "name": dataset["name"], "kind": dataset["kind"],
            "snapshot_hash": snapshot_hash, "rows": analysis["rows"],
        },
        "columns": columns,
        "companion_links": companion_links,
    }


def save_field_note(dataset_id: str, field_name: str, note: str, semantic_label: str) -> dict[str, Any]:
    dataset = db.get_dataset(dataset_id)
    if dataset is None:
        raise KeyError(dataset_id)
    columns = set(dataset.get("metadata", {}).get("columns", []))
    if field_name not in columns:
        raise ValueError(f"Field not found in dataset metadata: {field_name}")
    return db.upsert_dataset_field_note(dataset_id, field_name, note, semantic_label)


def _source_dataset(setting: str) -> tuple[str, dict[str, Any] | None]:
    settings = db.project_settings()
    dataset_id = settings.get(setting)
    return str(dataset_id or ""), db.get_dataset(dataset_id) if dataset_id else None


def _input(key: str, label: str, value: str, effect: str, detail: str, source: str) -> dict[str, str]:
    return {"key": key, "label": label, "value": value, "effect": effect, "detail": detail, "source": source}


def workflow_input_contract(workflow: str) -> dict[str, Any]:
    if workflow not in WORKFLOWS:
        raise ValueError(f"Unknown workflow: {workflow}")
    building_id, building = _source_dataset("building_dataset_id")
    neighbor_id, neighbor = _source_dataset("neighbor_dataset_id")
    template_id, template = _source_dataset("template_dataset_id")
    weather_id, weather = _source_dataset("weather_dataset_id")
    names = {
        "building": building["name"] if building else "Not selected",
        "neighbor": neighbor["name"] if neighbor else "Not selected",
        "template": template["name"] if template else "Not selected",
        "weather": weather["name"] if weather else "Not selected",
    }
    mapping = (building or {}).get("metadata", {}).get("field_mapping", {})
    fields = {
        "reference": str(mapping.get("refparcela") or "refparcela"),
        "floors": str(mapping.get("altura_max") or "altura_max"),
        "cluster": str(mapping.get("cluster") or "cluster"),
    }
    contracts: dict[str, dict[str, Any]] = {
        "builder": {
            "summary": "The Builder reads the active project GIS and creates one automatic, inspectable model. Field mapping is already configurable through Data Health normalization.",
            "inputs": [
                _input("geometry", "Footprint geometry", "geometry", "physical_model", "Creates the floor print and context geometry.", names["building"]),
                _input("reference", "Building identity", fields["reference"], "stock_method", "Selects the requested building and joins context.", names["building"]),
                _input("floors", "Floor count", fields["floors"], "physical_model", "Determines storeys and building height.", names["building"]),
                _input("cluster", "TABULA cluster", fields["cluster"], "physical_model", "Suggests automatic envelope parameters; it does not set schedules, infiltration, setpoints, or HVAC.", names["building"]),
                _input("neighbors", "Context GIS", names["neighbor"], "physical_model", "Provides party-wall and shadow context.", neighbor_id),
                _input("template", "OpenStudio template", names["template"], "physical_model", "Supplies PlantillaOS objects and automatic defaults.", template_id),
                _input("weather", "Weather", names["weather"], "physical_model", "Attached to the generated model and snapshotted at commit.", weather_id),
            ],
            "locked": False,
            "status": "Existing field mapping is configurable; scientific overrides belong in Model Editor.",
        },
        "simulation": {
            "summary": "Simulation never rebuilds the model from GIS. It runs the byte-identical immutable OSM and weather snapshot inherited from the selected parent run.",
            "inputs": [
                _input("parent_model", "Parent OSM", "Selected immutable model run", "physical_model", "Byte-identical parent artifact; GIS fields are not re-read.", "Parent run input manifest"),
                _input("parent_weather", "Parent weather", "Inherited EPW snapshot", "physical_model", "No silent fallback to the active project weather.", "Parent run input manifest"),
            ],
            "locked": True,
            "status": "Immutable parent contract — select a model on the Simulation page.",
        },
        "neighborhood": {
            "summary": "Part C uses the verified domain stock policy, with a versioned project default and an immutable reviewed copy for every run.",
            "inputs": [
                _input("stock", "Stock GIS", "DatosRai_ciudadValencia.shp", "stock_method", "Boundary, district, or building scope is selected from the verified city stock.", "Part C domain path"),
                _input("reference", "Identity", "refparcela", "stock_method", "Building identity and Tipo15 join key.", "Stock GIS"),
                _input("floors", "Floor count", "altura_max", "physical_model", "Invalid values: cluster median → family median → 1.", "Stock GIS"),
                _input("cluster", "TABULA cluster", "cluster", "physical_model", "Invalid labels are excluded and reported.", "Stock GIS"),
                _input("footprint", "Footprint area", "Shape_Area", "stock_method", "Representative score, area proxy, and parcel apportioning.", "Stock GIS"),
                _input("ground", "Ground-floor rule", "Tipo15-derived → family fallback", "physical_model", "Tipo15 data overrides the family assumption when parcel records exist.", "Tipo15 252_planta"),
                _input("residential_area", "Residential area", "Tipo15 → cluster-ratio proxy", "scaling", "Scales representative kWh/m² to annual stock totals.", "Tipo15 442_sup_Residencial"),
            ],
            "locked": False,
            "status": "Project default policy — configurable; every run resolves an immutable copy.",
        },
        "city": {
            "summary": "Part D uses the same verified stock preparation as Part C, then aggregates all Valencia buildings by cluster and district.",
            "inputs": [
                _input("stock", "Stock GIS", "DatosRai_ciudadValencia.shp", "stock_method", "Full Valencia residential stock; 26,452 separate EnergyPlus runs are not performed.", "Part D domain path"),
                _input("scope", "District scope", "nombre", "stock_method", "District aggregation is keyed by name; coddistrit is retained for reporting.", "Stock GIS"),
                _input("floors", "Floor count", "altura_max", "physical_model", "Invalid values: cluster median → family median → 1.", "Stock GIS"),
                _input("cluster", "TABULA cluster", "cluster", "physical_model", "Selects the 21 representative typology-period groups.", "Stock GIS"),
                _input("footprint", "Footprint area", "Shape_Area", "stock_method", "Representative score, area proxy, and duplicate-parcel apportioning.", "Stock GIS"),
                _input("ground", "Ground-floor rule", "Tipo15-derived → family fallback", "physical_model", "Resolved per representative parcel.", "Tipo15 252_planta"),
                _input("residential_area", "Residential area", "Tipo15 → cluster-ratio proxy", "scaling", "Area basis for city totals.", "Tipo15 442_sup_Residencial"),
            ],
            "locked": False,
            "status": "Project default policy — configurable; every run resolves an immutable copy.",
        },
        "lhs": {
            "summary": "The current LHS capability is the verified pilot uncertainty protocol, not an arbitrary stock-policy study.",
            "inputs": [
                _input("protocol", "Protocol", "N=50 · seed=42", "stock_method", "Latin Hypercube sampling with the verified variable ranges.", "LHS golden contract"),
                _input("model", "Model", "Verified pilot automatic model", "physical_model", "Uses the PlantillaOS-based pilot chain.", "LHS input snapshots"),
                _input("ranges", "Variables", "7 simulation + 3 post-processing", "physical_model", "Ranges remain capability-gated and immutable per run.", "src/lhs_study.py"),
            ],
            "locked": True,
            "status": "Verified pilot protocol — locked.",
        },
    }
    passive = []
    for name, definition in FIELD_REGISTRY.items():
        if definition["effect"] in {"validation_only", "reporting_only", "not_used"}:
            passive.append({
                "field": name,
                "effect": definition["effect"],
                "meaning": definition["meaning"],
                "definition_status": definition["definition_status"],
            })
    contract = contracts[workflow]
    return {
        "schema_version": 1,
        "phase": 1,
        "workflow": workflow,
        "project_dataset_ids": {
            "building": building_id, "neighbor": neighbor_id,
            "template": template_id, "weather": weather_id,
        },
        "passive_fields": sorted(passive, key=lambda item: item["field"].lower()),
        "editor_link": "/runs",
        **contract,
    }


def companion_bootstrap_metadata() -> dict[str, Any]:
    columns = list(pd.read_csv(TIPO15_PATH, sep=";", encoding="latin-1", nrows=0).columns)
    rows = 0
    for chunk in pd.read_csv(
        TIPO15_PATH, sep=";", encoding="latin-1", usecols=["31_pc"], chunksize=100_000,
    ):
        rows += len(chunk)
    return {
        "managed": False,
        "source_role": "companion_dwelling_ledger",
        "rows": rows,
        "columns": columns,
        "delimiter": ";",
        "encoding": "latin-1",
    }
