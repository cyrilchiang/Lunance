"""
一次性歷史補洞（在 GitHub Actions 雲端跑）
============================================
用 TDCC portal 逐檔查詢，補 backfill_list.json 清單裡股票的「近 3 個月」每週集保，
合併進 data.json（與每日 OpenData 累積的結構相容）。

特性：
  - 可續跑：只補 data.json 裡還缺(null)的 (股票, 日期)。
  - 定期存檔：每 COMMIT_EVERY 檔就 git commit/push 一次，被中斷也保住進度。
  - portal 權杖一次性 → 每查一筆先 GET 拿新權杖再 POST。
  - 放慢請求、容錯，降低被封 IP 機率。
"""
import os
import re
import json
import time
import random
import datetime
import subprocess
from io import StringIO

import requests
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(HERE, "data.json")
LIST_JSON = os.path.join(HERE, "backfill_list.json")
PORTAL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Referer": PORTAL,
}
MONTHS_BACK = 3
COMMIT_EVERY = 10          # 每幾檔存檔一次
SLEEP = (2.5, 4.0)         # 每筆查詢間隔（秒）

GROUPS = [(1, 2, 3), (4, 5, 6, 7, 8), (9,), (10,), (11,), (12, 13), (14,), (15,)]


def get_token_and_dates(session):
    r = session.get(PORTAL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text
    m = re.search(r'name="SYNCHRONIZER_TOKEN"\s+[^>]*value="([^"]+)"', html)
    token = m.group(1) if m else None
    dates = sorted(set(re.findall(r'<option[^>]*value="(\d{8})"', html)), reverse=True)
    return token, dates


def query(session, date, stock):
    token, _ = get_token_and_dates(session)
    if not token:
        raise RuntimeError("no token")
    payload = {
        "SYNCHRONIZER_TOKEN": token, "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
        "method": "submit", "firDate": date, "scaDate": date,
        "sqlMethod": "StockNo", "stockNo": stock, "stockName": "",
    }
    r = session.post(PORTAL, data=payload, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse(html):
    """回傳 24 值 [8 比例, 8 張數, 8 人數]，查無資料回 None。"""
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return None
    target = None
    for t in tables:
        if t.shape[1] >= 5 and t.shape[0] >= 15:
            c0 = t.iloc[:, 0].astype(str).str.strip()
            if c0.str.fullmatch(r"\d+").sum() >= 15:
                target = t
                break
    if target is None:
        return None

    def num(x):
        try:
            return float(str(x).replace(",", "").strip())
        except Exception:
            return None

    shares, people = {}, {}
    for _, row in target.iterrows():
        s = str(row.iloc[0]).strip()
        if not s.isdigit():
            continue
        n = int(s)
        people[n] = num(row.iloc[2])
        shares[n] = num(row.iloc[3])
    if shares.get(17) in (None, 0):
        return None
    total = shares[17]
    out = []
    for g in GROUPS:  # 比例
        sh = sum((shares.get(n) or 0) for n in g)
        out.append(round(sh / total * 100, 3))
    for g in GROUPS:  # 張數
        out.append(int(sum((shares.get(n) or 0) for n in g) / 1000))
    for g in GROUPS:  # 人數
        out.append(int(sum((people.get(n) or 0) for n in g)))
    return out


def git_commit(msg):
    try:
        subprocess.run(["git", "add", "data.json"], cwd=HERE, check=True)
        r = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=HERE)
        if r.returncode != 0:  # 有變動
            subprocess.run(["git", "commit", "-m", msg], cwd=HERE, check=True)
            subprocess.run(["git", "push"], cwd=HERE, check=True)
            print("  >> committed:", msg, flush=True)
    except Exception as e:
        print("  git commit failed:", e, flush=True)


def main():
    data = json.load(open(DATA_JSON, encoding="utf-8"))
    watch = json.load(open(LIST_JSON, encoding="utf-8"))

    session = requests.Session()
    _, portal_dates = get_token_and_dates(session)
    cutoff = datetime.date.today() - datetime.timedelta(days=MONTHS_BACK * 31)
    target_dates = [d for d in portal_dates
                    if datetime.datetime.strptime(d, "%Y%m%d").date() >= cutoff]
    target_iso = [str(datetime.datetime.strptime(d, "%Y%m%d").date()) for d in target_dates]

    # 重建全域 dates（含既有 + 目標），排序
    all_dates = sorted(set(data["dates"]) | set(target_iso))
    di = {d: i for i, d in enumerate(all_dates)}

    # 把每檔 v 重新對齊到 all_dates（缺的補 None）
    NULL = [None] * 24
    for code, st in data["stocks"].items():
        oldv = st["v"]
        newv = [list(NULL) for _ in all_dates]
        for j, d in enumerate(data["dates"]):
            if j < len(oldv):
                newv[di[d]] = oldv[j]
        st["v"] = newv
    data["dates"] = all_dates

    iso_to_portal = {str(datetime.datetime.strptime(d, "%Y%m%d").date()): d for d in target_dates}

    done = 0
    for ci, code in enumerate(watch, 1):
        st = data["stocks"].get(code)
        if st is None:
            st = data["stocks"].setdefault(code, {"n": "", "v": [list(NULL) for _ in all_dates]})
        # 找這檔還缺的目標日期
        missing = [iso for iso in target_iso
                   if st["v"][di[iso]] is None or st["v"][di[iso]][7] is None]
        if not missing:
            continue
        print(f"[{ci}/{len(watch)}] {code} 補 {len(missing)} 週", flush=True)
        for iso in missing:
            pdate = iso_to_portal[iso]
            try:
                vals = parse(query(session, pdate, code))
                if vals:
                    st["v"][di[iso]] = vals
            except Exception as e:
                print(f"   {iso} 失敗 {type(e).__name__}", flush=True)
            time.sleep(random.uniform(*SLEEP))
        done += 1
        if done % COMMIT_EVERY == 0:
            json.dump(data, open(DATA_JSON, "w", encoding="utf-8"),
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            git_commit(f"backfill progress {ci}/{len(watch)}")

    json.dump(data, open(DATA_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    git_commit("backfill done")
    print("補洞完成", flush=True)


if __name__ == "__main__":
    main()
