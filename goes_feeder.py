#!/usr/bin/env python3
"""
NMAS GOES FEEDER
Descarga productos oficiales NOAA GOES de acceso público:
- ABI-L2-ACMF: máscara/probabilidad de nube.
- ABI-L2-RRQPEF: tasa de lluvia satelital.

Publica un JSON pequeño para que Google Apps Script no tenga que interpretar
NetCDF/HDF5. No requiere cuenta de AWS ni API key.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
import numpy as np
import requests
import xarray as xr
from botocore import UNSIGNED
from botocore.config import Config
from pyproj import CRS, Transformer


PRODUCT_CLOUD = "ABI-L2-ACMF"
PRODUCT_RAIN = "ABI-L2-RRQPEF"
SATELLITES = {
    "G19": {"bucket": "noaa-goes19", "longitude_split": -106.0},
    "G18": {"bucket": "noaa-goes18", "longitude_split": -106.0},
}
MAX_LOOKBACK_HOURS = 5
REQUEST_TIMEOUT_SECONDS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def finite_number(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback


def s3_client():
    return boto3.client("s3", config=Config(signature_version=UNSIGNED))


def latest_key(client, bucket: str, product: str, now: datetime) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for offset in range(MAX_LOOKBACK_HOURS + 1):
        hour = now - timedelta(hours=offset)
        prefix = f"{product}/{hour:%Y}/{hour:%j}/{hour:%H}/"
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            response = client.list_objects_v2(**kwargs)
            candidates.extend(
                item for item in response.get("Contents", [])
                if str(item.get("Key", "")).endswith(".nc")
            )
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
        if candidates:
            break

    if not candidates:
        return None

    return max(candidates, key=lambda item: item.get("LastModified", datetime.min.replace(tzinfo=timezone.utc)))


def download_object(client, bucket: str, key: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(bucket, key, str(target))


def first_existing(ds: xr.Dataset, names: Iterable[str]) -> str | None:
    for name in names:
        if name in ds.variables:
            return name
    return None


@dataclass
class GoesSampler:
    satellite: str
    product: str
    path: Path
    ds: xr.Dataset
    x: np.ndarray
    y: np.ndarray
    height: float
    transformer: Transformer
    observed_at: datetime
    data_name: str
    dqf_name: str | None

    @classmethod
    def open(cls, satellite: str, product: str, path: Path) -> "GoesSampler":
        ds = xr.open_dataset(path, engine="h5netcdf", mask_and_scale=True, decode_times=False)

        if "goes_imager_projection" not in ds.variables:
            raise RuntimeError(f"{satellite} {product}: falta goes_imager_projection")

        projection = ds["goes_imager_projection"].attrs
        height = finite_number(projection.get("perspective_point_height"))
        lon0 = finite_number(projection.get("longitude_of_projection_origin"))
        sweep = str(projection.get("sweep_angle_axis", "x"))
        semi_major = finite_number(projection.get("semi_major_axis"), 6378137.0)
        semi_minor = finite_number(projection.get("semi_minor_axis"), 6356752.31414)

        geos = CRS.from_proj4(
            f"+proj=geos +h={height} +lon_0={lon0} +sweep={sweep} "
            f"+a={semi_major} +b={semi_minor} +units=m +no_defs"
        )
        transformer = Transformer.from_crs("EPSG:4326", geos, always_xy=True)

        if product == PRODUCT_CLOUD:
            data_name = first_existing(ds, (
                "Cloud_Probabilities",
                "cloud_probability",
                "cloud_probabilities",
                "ACM",
                "BCM",
            ))
        else:
            data_name = first_existing(ds, (
                "RRQPE",
                "rainfall_rate",
                "Rainfall_Rate",
                "rain_rate",
            ))

        if not data_name:
            raise RuntimeError(
                f"{satellite} {product}: no se encontró variable científica. "
                f"Variables: {', '.join(ds.variables.keys())}"
            )

        dqf_name = first_existing(ds, ("DQF", "dqf", "Data_Quality_Flag"))
        observed_at = (
            parse_time(ds.attrs.get("time_coverage_end"))
            or parse_time(ds.attrs.get("date_created"))
            or utc_now()
        )

        return cls(
            satellite=satellite,
            product=product,
            path=path,
            ds=ds,
            x=np.asarray(ds["x"].values, dtype=float),
            y=np.asarray(ds["y"].values, dtype=float),
            height=height,
            transformer=transformer,
            observed_at=observed_at,
            data_name=data_name,
            dqf_name=dqf_name,
        )

    def close(self) -> None:
        self.ds.close()

    def _indices(self, lon: float, lat: float) -> tuple[int, int]:
        x_m, y_m = self.transformer.transform(lon, lat)
        if not (math.isfinite(x_m) and math.isfinite(y_m)):
            raise ValueError("Punto fuera del disco visible")
        x_scan = x_m / self.height
        y_scan = y_m / self.height
        ix = int(np.abs(self.x - x_scan).argmin())
        iy = int(np.abs(self.y - y_scan).argmin())
        return ix, iy

    def _window(self, lon: float, lat: float, radius: int) -> tuple[np.ndarray, np.ndarray | None]:
        ix, iy = self._indices(lon, lat)
        x0, x1 = max(0, ix - radius), min(len(self.x), ix + radius + 1)
        y0, y1 = max(0, iy - radius), min(len(self.y), iy + radius + 1)

        data = self.ds[self.data_name].isel(x=slice(x0, x1), y=slice(y0, y1))
        values = np.asarray(data.values, dtype=float)

        quality = None
        if self.dqf_name:
            dqf = self.ds[self.dqf_name]
            if "x" in dqf.dims and "y" in dqf.dims:
                quality = np.asarray(
                    dqf.isel(x=slice(x0, x1), y=slice(y0, y1)).values,
                    dtype=float,
                )
        return values, quality

    @staticmethod
    def _valid(values: np.ndarray, quality: np.ndarray | None) -> np.ndarray:
        mask = np.isfinite(values)
        if quality is not None and quality.shape == values.shape:
            good = np.isfinite(quality) & (quality == 0)
            if np.any(mask & good):
                mask &= good
        return values[mask]

    def sample_cloud(self, lon: float, lat: float, radius: int) -> dict[str, Any]:
        values, quality = self._window(lon, lat, radius)
        valid = self._valid(values, quality)
        if valid.size == 0:
            raise ValueError("Sin píxeles válidos de nube")

        name = self.data_name
        if name in {"Cloud_Probabilities", "cloud_probability", "cloud_probabilities"}:
            normalized = np.clip(valid, 0.0, 1.0)
            cloud_pct = float(np.mean(normalized) * 100.0)
            cloud_probability_pct = cloud_pct
        elif name == "ACM":
            # 0 despejado, 1 probablemente despejado, 2 probablemente nublado, 3 nublado.
            weights = np.select(
                [valid <= 0, valid == 1, valid == 2, valid >= 3],
                [0.0, 0.25, 0.75, 1.0],
                default=np.nan,
            )
            weights = weights[np.isfinite(weights)]
            if weights.size == 0:
                raise ValueError("ACM sin clasificaciones válidas")
            cloud_pct = float(np.mean(weights) * 100.0)
            cloud_probability_pct = cloud_pct
        else:
            # BCM: 0 despejado, 1 nublado.
            normalized = np.clip(valid, 0.0, 1.0)
            cloud_pct = float(np.mean(normalized) * 100.0)
            cloud_probability_pct = cloud_pct

        if cloud_pct <= 15:
            cloud_class = "CLEAR"
        elif cloud_pct <= 40:
            cloud_class = "MOSTLY_CLEAR"
        elif cloud_pct <= 75:
            cloud_class = "PARTLY_CLOUDY"
        else:
            cloud_class = "CLOUDY"

        return {
            "cloud_pct": round(cloud_pct, 1),
            "cloud_probability_pct": round(cloud_probability_pct, 1),
            "cloud_class": cloud_class,
            "cloud_valid_pixels": int(valid.size),
            "cloud_variable": name,
        }

    def sample_rain(self, lon: float, lat: float, radius: int) -> dict[str, Any]:
        values, quality = self._window(lon, lat, radius)
        valid = self._valid(values, quality)
        valid = valid[(valid >= 0.0) & (valid <= 100.0)]
        if valid.size == 0:
            raise ValueError("Sin píxeles válidos de lluvia")

        positive = valid[valid >= 0.01]
        pixel_pct = 100.0 * positive.size / valid.size
        p75_all = float(np.percentile(valid, 75))
        positive_mean = float(np.mean(positive)) if positive.size else 0.0
        peak = float(np.max(valid))

        # Intensidad conservadora del área: no basta un único píxel aislado.
        if pixel_pct >= 22:
            area_rate = max(p75_all, positive_mean * min(1.0, pixel_pct / 55.0))
        elif pixel_pct >= 11 and peak >= 0.40:
            area_rate = min(peak, max(0.05, positive_mean * 0.35))
        else:
            area_rate = 0.0

        return {
            "rain_rate_mm_h": round(area_rate, 3),
            "rain_rate_p75_mm_h": round(p75_all, 3),
            "rain_rate_positive_mean_mm_h": round(positive_mean, 3),
            "rain_rate_peak_mm_h": round(peak, 3),
            "rain_pixel_pct": round(pixel_pct, 1),
            "rain_valid_pixels": int(valid.size),
            "rain_variable": self.data_name,
        }


def load_previous(repo: str) -> dict[str, Any]:
    if not repo:
        return {}
    url = f"https://raw.githubusercontent.com/{repo}/data/satellite.json?ts={int(utc_now().timestamp())}"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code != 200:
            return {}
        payload = response.json()
        return payload.get("locations", {}) if isinstance(payload, dict) else {}
    except Exception:
        return {}


def preferred_satellite(lon: float) -> str:
    # Punto medio aproximado entre GOES-West y GOES-East.
    return "G18" if lon <= -106.0 else "G19"


def choose_sampler(
    samplers: dict[str, dict[str, GoesSampler]],
    product: str,
    preferred: str,
) -> GoesSampler | None:
    other = "G19" if preferred == "G18" else "G18"
    for sat in (preferred, other):
        sampler = samplers.get(sat, {}).get(product)
        if sampler:
            return sampler
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locations", default="locations.json")
    parser.add_argument("--output", default="satellite.json")
    args = parser.parse_args()

    now = utc_now()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    previous = load_previous(repo)

    config = json.loads(Path(args.locations).read_text(encoding="utf-8"))
    points = config.get("locations", [])
    if not points:
        raise RuntimeError("locations.json no contiene ubicaciones")

    client = s3_client()
    samplers: dict[str, dict[str, GoesSampler]] = {"G18": {}, "G19": {}}
    product_meta: dict[str, Any] = {}
    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="nmas-goes-") as tmp:
        tmp_path = Path(tmp)

        for sat, sat_cfg in SATELLITES.items():
            bucket = sat_cfg["bucket"]
            for product in (PRODUCT_CLOUD, PRODUCT_RAIN):
                try:
                    item = latest_key(client, bucket, product, now)
                    if not item:
                        raise RuntimeError("sin archivos recientes")
                    key = str(item["Key"])
                    local = tmp_path / f"{sat}_{product}.nc"
                    download_object(client, bucket, key, local)
                    sampler = GoesSampler.open(sat, product, local)
                    samplers[sat][product] = sampler
                    product_meta[f"{sat}_{product}"] = {
                        "bucket": bucket,
                        "key": key,
                        "observed_at": iso_z(sampler.observed_at),
                        "age_minutes": round(max(0.0, (now - sampler.observed_at).total_seconds() / 60.0), 1),
                        "variable": sampler.data_name,
                    }
                except Exception as exc:
                    errors.append(f"{sat} {product}: {exc}")

        locations_out: dict[str, Any] = {}

        for point in points:
            loc_id = str(point["id"])
            lat = float(point["lat"])
            lon = float(point["lon"])
            radius = int(point.get("window_radius_pixels", 1))
            preferred = preferred_satellite(lon)

            cloud_sampler = choose_sampler(samplers, PRODUCT_CLOUD, preferred)
            rain_sampler = choose_sampler(samplers, PRODUCT_RAIN, preferred)

            row: dict[str, Any] = {
                "id": loc_id,
                "name": point.get("name", loc_id),
                "lat": lat,
                "lon": lon,
                "kind": point.get("kind", "location"),
                "status": "OK",
            }

            observed_times: list[datetime] = []

            try:
                if not cloud_sampler:
                    raise RuntimeError("sin producto de nube")
                row.update(cloud_sampler.sample_cloud(lon, lat, radius))
                row["cloud_satellite"] = cloud_sampler.satellite
                observed_times.append(cloud_sampler.observed_at)
            except Exception as exc:
                row["status"] = "PARTIAL"
                row["cloud_error"] = str(exc)

            try:
                if not rain_sampler:
                    raise RuntimeError("sin producto RRQPE")
                row.update(rain_sampler.sample_rain(lon, lat, radius))
                row["rain_satellite"] = rain_sampler.satellite
                observed_times.append(rain_sampler.observed_at)
            except Exception as exc:
                row["status"] = "PARTIAL" if row.get("cloud_pct") is not None else "ERROR"
                row["rain_error"] = str(exc)
                row.update({
                    "rain_rate_mm_h": 0.0,
                    "rain_rate_p75_mm_h": 0.0,
                    "rain_rate_positive_mean_mm_h": 0.0,
                    "rain_rate_peak_mm_h": 0.0,
                    "rain_pixel_pct": 0.0,
                })

            if observed_times:
                observed_at = min(observed_times)
                age_minutes = max(0.0, (now - observed_at).total_seconds() / 60.0)
                row["observed_at"] = iso_z(observed_at)
                row["age_minutes"] = round(age_minutes, 1)
                row["confidence"] = int(max(35, min(98, 98 - age_minutes * 1.3)))
            else:
                row["observed_at"] = None
                row["age_minutes"] = 999.0
                row["confidence"] = 0

            old = previous.get(loc_id, {}) if isinstance(previous, dict) else {}
            row["cloud_delta_pct"] = round(
                finite_number(row.get("cloud_pct")) - finite_number(old.get("cloud_pct")),
                1,
            )
            row["rain_delta_mm_h"] = round(
                finite_number(row.get("rain_rate_mm_h")) - finite_number(old.get("rain_rate_mm_h")),
                3,
            )

            cloud_delta = row["cloud_delta_pct"]
            rain_delta = row["rain_delta_mm_h"]
            if rain_delta >= 0.05:
                row["trend"] = "RAIN_INCREASING"
            elif rain_delta <= -0.05:
                row["trend"] = "RAIN_DECREASING"
            elif cloud_delta >= 8:
                row["trend"] = "CLOUDS_INCREASING"
            elif cloud_delta <= -8:
                row["trend"] = "CLOUDS_DECREASING"
            else:
                row["trend"] = "STABLE"

            locations_out[loc_id] = row

        for sat_group in samplers.values():
            for sampler in sat_group.values():
                sampler.close()

    valid_count = sum(
        1 for row in locations_out.values()
        if row.get("status") in {"OK", "PARTIAL"} and row.get("cloud_pct") is not None
    )

    output = {
        "schema": "nmas-goes-v1",
        "generated_at": iso_z(now),
        "recommended_max_age_minutes": 35,
        "location_count": len(locations_out),
        "valid_location_count": valid_count,
        "products": product_meta,
        "errors": errors,
        "locations": locations_out,
    }

    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    if valid_count == 0:
        raise RuntimeError("No se obtuvo ninguna ubicación satelital válida")

    print(
        f"OK: {valid_count}/{len(locations_out)} ubicaciones; "
        f"{len(product_meta)} productos; {len(errors)} avisos."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
