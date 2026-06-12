"""
線上版資料產生器（給 GitHub Actions 每週自動執行）
==================================================
抓 TDCC 集保 OpenData 最新一週 → 計算 24 欄統計 → 累積寫入 data.json。
index.html 會在瀏覽器端讀取 data.json 顯示。

每跑一次：若抓到的日期尚未存在於 data.json，就加進去（累積歷史）。
"""
import os
import json
import time
from io import StringIO

import requests
import pandas as pd

TDCC_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(HERE, "data.json")
NAMES_JSON = os.path.join(HERE, "stock_names.json")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/124.0"}

COLS = [
    '10張以下持股比例', '10至50張持股比例', '50至100張持股比例', '100至200張持股比例',
    '200至400張持股比例', '400至800張持股比例', '800至1千張持股比例', '超過1千張持股比例',
    '10張以下持有張數', '10至50張持有張數', '50至100張持有張數', '100至200張持有張數',
    '200至400張持有張數', '400至800張持有張數', '800至1千張持有張數', '超過1千張持有張數',
    '10張以下持有人數', '10至50張持有人數', '50至100張持有人數', '100至200張持有人數',
    '200至400張持有人數', '400至800張持有人數', '800至1千張持有人數', '超過1千張持有人數',
]
LEVELS = ['10張以下', '10至50張', '50至100張', '100至200張',
          '200至400張', '400至800張', '800至1千張', '超過1千張']


def fetch_latest():
    for i in range(4):
        try:
            r = requests.get(TDCC_URL, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.exceptions.RequestException as e:
            print("fetch retry", i + 1, type(e).__name__)
            time.sleep(15)
    raise RuntimeError("無法連到 TDCC OpenData")


def compute(csv_text):
    df = pd.read_csv(StringIO(csv_text), dtype={'證券代號': str})
    date = str(pd.to_datetime(str(df['資料日期'].iloc[0])).date())
    df = df.rename(columns={'證券代號': 'stock_id'})
    df['stock_id'] = df['stock_id'].astype(str).str.strip()
    piv = df.pivot(index='stock_id', columns='持股分級')
    shares, people = piv['股數'], piv['人數']
    total = shares.loc[:, 17]

    def grp_ratio(*lv):
        return (sum(shares.loc[:, n] for n in lv) / total * 100).round(3)

    def grp_lots(*lv):
        return (sum(shares.loc[:, n] for n in lv) / 1000).round(0)

    def grp_ppl(*lv):
        return sum(people.loc[:, n] for n in lv)

    groups = [(1, 2, 3), (4, 5, 6, 7, 8), (9,), (10,), (11,), (12, 13), (14,), (15,)]
    out = {}
    for g in groups:
        out['r' + str(g[0])] = grp_ratio(*g)
    cols = {}
    # 依 COLS 順序組裝每檔的 24 值
    ratio_series = [grp_ratio(*g) for g in groups]
    lots_series = [grp_lots(*g) for g in groups]
    ppl_series = [grp_ppl(*g) for g in groups]
    result = {}
    for sid in shares.index:
        sid = str(sid)
        vals = []
        try:
            for s in ratio_series:
                vals.append(None if pd.isna(s[sid]) else float(round(s[sid], 3)))
            for s in lots_series:
                vals.append(None if pd.isna(s[sid]) else int(s[sid]))
            for s in ppl_series:
                vals.append(None if pd.isna(s[sid]) else int(s[sid]))
            result[sid] = vals
        except Exception:
            continue
    return date, result


def main():
    names = {}
    if os.path.isfile(NAMES_JSON):
        names = json.load(open(NAMES_JSON, encoding="utf-8"))

    data = {"dates": [], "levels": LEVELS, "stocks": {}}
    if os.path.isfile(DATA_JSON):
        data = json.load(open(DATA_JSON, encoding="utf-8"))
        data.setdefault("levels", LEVELS)

    csv_text = fetch_latest()
    date, result = compute(csv_text)

    if date in data["dates"]:
        print("日期", date, "已存在，無需更新")
        return

    di = len(data["dates"])
    data["dates"].append(date)
    for sid, vals in result.items():
        st = data["stocks"].setdefault(sid, {"n": names.get(sid, ""), "v": []})
        # 補齊前面缺的日期
        while len(st["v"]) < di:
            st["v"].append([None] * 24)
        st["v"].append(vals)
    # 沒出現在本次的股票也補 None
    for sid, st in data["stocks"].items():
        while len(st["v"]) < di + 1:
            st["v"].append([None] * 24)

    json.dump(data, open(DATA_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(',', ':'))
    print("已更新 data.json：新增日期", date, "｜總日期數", len(data["dates"]),
          "｜股票數", len(data["stocks"]))


if __name__ == "__main__":
    main()
