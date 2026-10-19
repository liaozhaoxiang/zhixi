"""灌入一份真实公开数据集：Open-Meteo 历史天气档案（ECMWF ERA5 再分析）。

用途：演示「数据来源要真实、可点击」——这份四城市日度气象数据来自公开 API，
每条证据都会带上真实的来源链接，报告尾部给出处。不是合成数据。
"""
from __future__ import annotations

import io
import json
import time
import urllib.request

import pandas as pd

from app import datasets

CITIES = [
    ("北京", 39.9042, 116.4074),
    ("上海", 31.2304, 121.4737),
    ("广州", 23.1291, 113.2644),
    ("成都", 30.5728, 104.0668),
]
START, END = "2024-01-01", "2026-10-10"
SOURCE_NAME = "Open-Meteo 历史天气档案（ECMWF ERA5 再分析）"
SOURCE_URL = "https://open-meteo.com/en/docs/historical-weather-api"

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(city: str, lat: float, lon: float) -> pd.DataFrame:
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}&start_date={START}&end_date={END}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max"
        "&timezone=Asia%2FShanghai"
    )
    with opener.open(url, timeout=30) as r:
        j = json.loads(r.read().decode("utf-8"))
    d = j["daily"]
    return pd.DataFrame(
        {
            "日期": d["time"],
            "城市": city,
            "最高气温": d["temperature_2m_max"],
            "最低气温": d["temperature_2m_min"],
            "降水量": d["precipitation_sum"],
            "最大风速": d["wind_speed_10m_max"],
        }
    )


def main() -> None:
    frames = []
    for city, lat, lon in CITIES:
        df = fetch(city, lat, lon)
        print(f"{city}: {len(df)} 行 {df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}")
        frames.append(df)
        time.sleep(0.4)
    all_df = pd.concat(frames, ignore_index=True).dropna(subset=["最高气温"])
    buf = io.BytesIO()
    all_df.to_csv(buf, index=False, encoding="utf-8-sig")
    item = datasets.save_upload(
        f"四大城市日度气象_真实数据_{START}_{END}.csv",
        buf.getvalue(),
        source_url=SOURCE_URL,
        source_name=SOURCE_NAME,
    )
    # 灌完不抢激活位，让用户自己在数据源面板点
    datasets.set_active(None)
    print(f"已入库: {item['name']} | {item['rows']} 行 / {item['columns']} 列 | 表 {item['table']}")
    print(f"来源: {SOURCE_URL}")


if __name__ == "__main__":
    main()
