#!/usr/bin/env python3
"""DART OpenAPI에서 메자닌 분석에 필요한 데이터만 뽑아 대시보드용 JSON으로 저장한다.

사용법:
    DART_API_KEY=... python3 scripts/dart_fetch.py "기업명 또는 종목코드" [...]

출력:
    mezzanine-dashboard/data/<corp_code>.json   기업별 데이터 (대시보드 db의 companies/<corp_code>)
    mezzanine-dashboard/data/index.json         검색용 이름 목록 (대시보드 db의 index/names)

표준 출력은 기업당 한 줄 요약만 남긴다.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

API = "https://opendart.fss.or.kr/api/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "mezzanine-dashboard", "data")
CACHE = os.path.join(ROOT, ".cache")
KEY = os.environ.get("DART_API_KEY", "").strip()
EOK = 1e8  # 원 → 억원
MARKET = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "기타"}
# 정기보고서 코드: 사업보고서, 3분기, 반기, 1분기
REPORTS = [("11011", "사업보고서"), ("11014", "3분기보고서"), ("11012", "반기보고서"), ("11013", "1분기보고서")]


def get(path, **params):
    params["crtfc_key"] = KEY
    url = API + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def get_json(path, **params):
    data = json.loads(get(path, **params))
    # 013 = 조회된 데이터 없음
    if data.get("status") not in ("000", "013"):
        raise RuntimeError(f"{path}: {data.get('status')} {data.get('message')}")
    return data.get("list", []) if data.get("status") == "000" else []


def corp_list():
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "corpCode.xml")
    fresh = os.path.exists(path) and (dt.datetime.now().timestamp() - os.path.getmtime(path) < 7 * 86400)
    if not fresh:
        with zipfile.ZipFile(io.BytesIO(get("corpCode.xml"))) as z:
            with open(path, "wb") as f:
                f.write(z.read(z.namelist()[0]))
    rows = []
    for el in ET.parse(path).getroot().iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:  # 상장사만
            rows.append({"corp_code": el.findtext("corp_code"), "name": el.findtext("corp_name").strip(), "stock_code": stock})
    return rows


def find_corp(query, rows):
    q = query.strip().replace(" ", "")
    for key in ("stock_code", "name"):
        hit = [r for r in rows if r[key].replace(" ", "") == q]
        if hit:
            return hit[0], []
    part = [r for r in rows if q in r["name"].replace(" ", "")]
    if len(part) == 1:
        return part[0], []
    return None, part[:5]


def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def eok(v):
    return None if v is None else round(v / EOK, 1)


# 계정 매칭: account_id 우선, 없으면 계정명 정규식
ACCOUNTS = {
    "revenue": (("IS", "CIS"), ["ifrs-full_Revenue"], r"^(매출액|수익\(매출액\)|영업수익|매출)$"),
    "operatingIncome": (("IS", "CIS"), ["dart_OperatingIncomeLoss"], r"^영업이익(\(손실\))?$|^영업손익$"),
    "netIncome": (("IS", "CIS"), ["ifrs-full_ProfitLoss"], r"^당기순이익(\(손실\))?$|^당기순손익$"),
    "ocf": (("CF",), ["ifrs-full_CashFlowsFromUsedInOperatingActivities"], r"^영업활동.*현금흐름"),
    "interestExpense": (("IS", "CIS"), ["ifrs-full_InterestExpense", "dart_InterestExpense"], r"^이자비용$"),
    "financeCosts": (("IS", "CIS"), ["ifrs-full_FinanceCosts"], r"^금융(비용|원가)$"),
    "totalAssets": (("BS",), ["ifrs-full_Assets"], r"^자산총계$"),
    "totalLiabilities": (("BS",), ["ifrs-full_Liabilities"], r"^부채총계$"),
    "equity": (("BS",), ["ifrs-full_Equity"], r"^자본총계$"),
    "cash": (("BS",), ["ifrs-full_CashAndCashEquivalents"], r"^현금및현금성자산$"),
}
SHORT_DEBT = r"^(단기차입금|유동성장기(차입금|부채)|유동성사채|(유동성)?전환사채|(유동성)?신주인수권부사채|(유동성)?교환사채|단기사채)$"
LONG_DEBT = r"^(장기차입금|사채|비유동성?(전환사채|신주인수권부사채|교환사채)|장기사채)$"


def pick(items, sj, ids, pattern, col):
    for it in items:
        if it.get("sj_div") in sj and it.get("account_id") in ids:
            v = num(it.get(col))
            if v is not None:
                return v
    rx = re.compile(pattern)
    for it in items:
        if it.get("sj_div") in sj and rx.search((it.get("account_nm") or "").replace(" ", "")):
            v = num(it.get(col))
            if v is not None:
                return v
    return None


def sum_bs(items, pattern, col):
    rx = re.compile(pattern)
    vals = [num(it.get(col)) for it in items if it.get("sj_div") == "BS" and rx.search((it.get("account_nm") or "").replace(" ", ""))]
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def statements(corp, year, reprt):
    for fs in ("CFS", "OFS"):  # 연결 우선, 없으면 별도
        items = get_json("fnlttSinglAcntAll.json", corp_code=corp, bsns_year=year, reprt_code=reprt, fs_div=fs)
        if items:
            return items, fs
    return [], None


def extract(items, col="thstrm_amount"):
    out = {k: pick(items, *spec, col) for k, spec in ACCOUNTS.items()}
    out["shortDebt"] = sum_bs(items, SHORT_DEBT, col)
    long_debt = sum_bs(items, LONG_DEBT, col)
    out["totalDebt"] = None if out["shortDebt"] is None and long_debt is None else (out["shortDebt"] or 0) + (long_debt or 0)
    if out["interestExpense"] is None:
        out["interestExpense"] = out.pop("financeCosts")
    else:
        out.pop("financeCosts")
    return out


def financials(corp):
    this_year = dt.date.today().year
    years, rows, fs_div = [], [], None
    # 최근 사업보고서 연도 찾기 → 그 연도부터 5개년
    for y in range(this_year - 1, this_year - 3, -1):
        latest_items, fs = statements(corp, y, "11011")
        if latest_items:
            latest, fs_div = y, fs
            break
    else:
        return None
    for y in range(latest - 4, latest + 1):
        items = latest_items if y == latest else statements(corp, y, "11011")[0]
        years.append(str(y))
        rows.append(extract(items) if items else {})
    series = lambda k: [eok(r.get(k)) for r in rows]
    f = {"unit": "억원", "fsDiv": "연결" if fs_div == "CFS" else "별도", "years": years,
         "revenue": series("revenue"), "operatingIncome": series("operatingIncome"),
         "netIncome": series("netIncome"), "ocf": series("ocf")}
    last = rows[-1]
    bs_src = f"{latest} 사업보고서"
    # 대차대조표 항목은 사업보고서 이후의 가장 최근 분기보고서로 갱신 (상환능력은 최신 잔액이 중요)
    for code, label in REPORTS[1:]:
        items, _ = statements(corp, latest + 1, code)
        if items:
            last = {**last, **{k: v for k, v in extract(items).items()
                               if k in ("totalAssets", "totalLiabilities", "equity", "cash", "shortDebt", "totalDebt") and v is not None}}
            bs_src = f"{latest + 1} {label}"
            break
    for k in ("totalAssets", "totalLiabilities", "equity", "cash", "shortDebt", "totalDebt"):
        f[k] = eok(last.get(k))
    f["interestExpense"] = eok(rows[-1].get("interestExpense"))
    f["balanceAsOf"] = bs_src
    return f


def mezzanine(corp):
    end = dt.date.today()
    bgn = end.replace(year=end.year - 4)
    rng = {"bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d")}
    out = []
    for path, typ, px in (("cvbdIsDecsn.json", "CB", "cv_prc"), ("bdwtIsDecsn.json", "BW", "ex_prc"), ("exbdIsDecsn.json", "EB", "ex_prc")):
        for it in get_json(path, corp_code=corp, **rng):
            out.append({
                "type": typ,
                "round": (it.get("bd_tm") or "").strip() + "회차" if it.get("bd_tm") else "",
                "amount": eok(num(it.get("bd_fta"))),
                "conversionPrice": num(it.get(px)),
                "refixFloor": num(it.get("act_mktprc_fl_cvprc_lwtrsprc")),
                "coupon": num(it.get("bd_intr_ex")),
                "ytm": num(it.get("bd_intr_sf")),
                "maturity": (it.get("bd_mtd") or "")[:10] or None,
                "putDate": None,
                "decided": it.get("bddd") or it.get("rcept_no", "")[:8],
                "rceptNo": it.get("rcept_no"),
            })
    out.sort(key=lambda x: x.get("decided") or "", reverse=True)
    return out


def shareholders(corp, year):
    for y in (year, year - 1):
        for code, _ in REPORTS:
            rows = get_json("hyslrSttus.json", corp_code=corp, bsns_year=y, reprt_code=code)
            if rows:
                common = [r for r in rows if "보통" in (r.get("stock_knd") or "")] or rows
                total = [r for r in common if (r.get("nm") or "").strip() == "계"]
                if total:
                    pct = num(total[0].get("trmend_posesn_stock_qota_rt"))
                else:
                    pct = sum(num(r.get("trmend_posesn_stock_qota_rt")) or 0 for r in common)
                top = next((r for r in common if (r.get("nm") or "").strip() != "계"), {})
                return {"majorShareholderPct": pct, "majorShareholder": (top.get("nm") or "").strip() or None}
    return {}


def shares(corp, year):
    for y in (year, year - 1):
        for code, _ in REPORTS:
            rows = get_json("stockTotqySttus.json", corp_code=corp, bsns_year=y, reprt_code=code)
            for r in rows:
                if (r.get("se") or "").strip() in ("보통주", "합계"):
                    return num(r.get("istc_totqy"))
    return None


def build(c):
    info = json.loads(get("company.json", corp_code=c["corp_code"]))
    fin = financials(c["corp_code"])
    year = int(fin["years"][-1]) + 1 if fin else dt.date.today().year
    mz = mezzanine(c["corp_code"])
    sh = shareholders(c["corp_code"], year)
    return {
        "source": "DART",
        "fetchedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "company": {"name": info.get("corp_name") or c["name"], "ticker": c["stock_code"], "corpCode": c["corp_code"],
                    "market": MARKET.get(info.get("corp_cls"), info.get("corp_cls")), "ceo": info.get("ceo_nm"),
                    "indutyCode": info.get("induty_code"), "homepage": info.get("hm_url")},
        "financials": fin,
        "sharesOutstanding": shares(c["corp_code"], year),
        "mezzanine": {"issued": mz, **sh},
    }


def main(queries):
    if not KEY:
        sys.exit("DART_API_KEY 환경 변수가 없습니다.")
    os.makedirs(OUT, exist_ok=True)
    rows = corp_list()
    idx_path = os.path.join(OUT, "index.json")
    index = json.load(open(idx_path)) if os.path.exists(idx_path) else {"entries": []}
    for q in queries:
        c, cands = find_corp(q, rows)
        if not c:
            print(f"[{q}] 찾지 못함" + (": 후보 " + ", ".join(f"{x['name']}({x['stock_code']})" for x in cands) if cands else ""))
            continue
        data = build(c)
        with open(os.path.join(OUT, c["corp_code"] + ".json"), "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        index["entries"] = [e for e in index["entries"] if e["corpCode"] != c["corp_code"]]
        index["entries"].append({"name": data["company"]["name"], "ticker": c["stock_code"], "corpCode": c["corp_code"], "fetchedAt": data["fetchedAt"]})
        f = data["financials"] or {}
        filled = sum(v is not None for k in ("revenue", "operatingIncome", "netIncome", "ocf") for v in f.get(k, []))
        print(f"[{q}] {data['company']['name']} {c['corp_code']} | 재무 {f.get('fsDiv','-')} {f.get('years',['-'])[0]}~{f.get('years',['-'])[-1]} 채움 {filled}/20 "
              f"| BS {f.get('balanceAsOf','-')} | 메자닌 {len(data['mezzanine']['issued'])}건 | 최대주주 {data['mezzanine'].get('majorShareholderPct')}%")
    index["entries"].sort(key=lambda e: e["name"])
    with open(idx_path, "w") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1:] or sys.exit(__doc__))
