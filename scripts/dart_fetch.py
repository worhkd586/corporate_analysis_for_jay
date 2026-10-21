#!/usr/bin/env python3
"""DART OpenAPI에서 메자닌 분석에 필요한 데이터만 뽑아 대시보드용 JSON으로 저장한다.

사용법:
    DART_API_KEY=... python3 scripts/dart_fetch.py [--request-id ID] "기업명 또는 종목코드" [...]
    DART_API_KEY=... python3 scripts/dart_fetch.py --queue DIR
    DART_API_KEY=... python3 scripts/dart_fetch.py --refresh-corp-codes   (상장사 목록 갱신)
        DIR: ArtifactData list(out_dir=DIR)로 저장한 requests/, results/ 문서.
             results가 없는 requests만 최대 10건 처리한다.

출력:
    mezzanine-dashboard/data/<corp_code>.json   기업별 데이터
    .cache/upload/writes.json                   대시보드 db에 올릴 batch 목록
        companies/<corp_code>          기업 데이터
        lookup/n_<hex>, t_<종목코드>   검색어 → corp_code (대시보드의 norm()과 같은 규칙)
        results/<request id>           대시보드가 기다리는 완료 신호 (--request-id 지정 시)

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


CORP_CODES = os.path.join(ROOT, "scripts", "corp_codes.json")


def corp_list(refresh=False):
    """상장사 목록 (corp_code, 이름, 종목코드).

    DART corpCode.xml은 받는 데 4분 가까이 걸려서, 상장사만 추린 scripts/corp_codes.json을
    레포에 두고 쓴다. 파일이 없거나 refresh=True면 새로 받아 그 파일을 갱신한다.
    """
    if not refresh and os.path.exists(CORP_CODES):
        return json.load(open(CORP_CODES))
    with zipfile.ZipFile(io.BytesIO(get("corpCode.xml"))) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    rows = []
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:  # 상장사만
            rows.append({"corp_code": el.findtext("corp_code"), "name": el.findtext("corp_name").strip(), "stock_code": stock})
    rows.sort(key=lambda r: r["stock_code"])
    with open(CORP_CODES, "w") as fh:
        json.dump(rows, fh, ensure_ascii=False, separators=(",", ":"))
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
    raw = []
    for y in range(latest - 4, latest + 1):
        raw.append(latest_items if y == latest else statements(corp, y, "11011")[0])
        years.append(str(y))
    # 사업보고서가 없는 해(상장 전 등)는 다음 해 보고서의 전기(frmtrm) 금액으로 채운다
    for i, items in enumerate(raw):
        if items:
            rows.append(extract(items))
        elif i + 1 < len(raw) and raw[i + 1]:
            rows.append(extract(raw[i + 1], "frmtrm_amount"))
        else:
            rows.append({})
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


def kdate(s):
    """'2030년 10월 02일', '2030-10-02', '20301002' → '2030-10-02' (일이 없으면 '2030-10')."""
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})(?:\D{0,3}(\d{1,2}))?", s or "")
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}" + (f"-{int(d):02d}" if d else "")


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
                "maturity": kdate(it.get("bd_mtd")),
                "putDate": None,
                "decided": kdate(it.get("bddd") or (it.get("rcept_no") or "")[:8]),
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


def norm(s):
    # 대시보드 index.html의 norm()과 같아야 한다
    return re.sub(r"\s|\(주\)|㈜|주식회사", "", s or "").lower()


def lookup_ids(text):
    ids = ["n_" + norm(text).encode().hex()[:190]]
    if re.fullmatch(r"\d{6}", text.strip()):
        ids.append("t_" + text.strip())
    return ids


def load_docs(folder):
    docs = {}
    if not os.path.isdir(folder):
        return docs
    for name in os.listdir(folder):
        if name.endswith(".json"):
            body = json.load(open(os.path.join(folder, name)))
            if isinstance(body.get("data"), dict):  # {id, data, version} 형태로 저장된 경우
                body = body["data"]
            docs[name[:-5]] = body
    return docs


def pending_requests(folder):
    reqs = load_docs(os.path.join(folder, "requests"))
    done = load_docs(os.path.join(folder, "results"))
    todo = [(rid, r.get("query", "")) for rid, r in reqs.items() if rid not in done and r.get("query")]
    return sorted(todo, key=lambda x: reqs[x[0]].get("at", ""))[:10]


def main(argv):
    if argv[:1] == ["--refresh-corp-codes"]:
        print(f"상장사 {len(corp_list(refresh=True))}곳 → {CORP_CODES}")
        return
    if argv[:1] == ["--queue"]:
        todo = pending_requests(argv[1])
        if not todo:
            print("대기 중인 요청 없음")
            return
        jobs = todo
    elif argv[:1] == ["--request-id"]:
        jobs = [(argv[1], q) for q in argv[2:]]
    else:
        jobs = [(None, q) for q in argv]
    if not KEY:
        sys.exit("DART_API_KEY 환경 변수가 없습니다.")
    os.makedirs(OUT, exist_ok=True)
    up = os.path.join(CACHE, "upload")
    os.makedirs(up, exist_ok=True)
    writes = []

    def add(collection, doc_id, body):
        path = os.path.join(up, f"{collection}_{doc_id}.json")
        with open(path, "w") as fh:
            json.dump(body, fh, ensure_ascii=False, separators=(",", ":"))
        writes.append({"op": "set", "collection": collection, "doc_id": doc_id, "file_path": path})

    rows = corp_list()
    if not all(find_corp(q, rows)[0] for _, q in jobs):
        rows = corp_list(refresh=True)  # 최근 상장사일 수 있으니 목록을 새로 받아 다시 찾는다
    now = lambda: dt.datetime.now().isoformat(timespec="seconds")
    for req, q in jobs:
        c, cands = find_corp(q, rows)
        if not c:
            msg = f"[{q}] DART 상장사 목록에서 찾지 못함" + (": 후보 " + ", ".join(f"{x['name']}({x['stock_code']})" for x in cands) if cands else "")
            print(msg)
            if req:
                add("results", req, {"status": "error", "message": msg, "candidates": cands, "at": now()})
            continue
        try:
            data = build(c)
        except Exception as e:  # 한 기업이 실패해도 나머지 요청은 처리
            print(f"[{q}] 수집 실패: {e}")
            if req:
                add("results", req, {"status": "error", "message": f"DART 수집 실패: {e}", "at": now()})
            continue
        with open(os.path.join(OUT, c["corp_code"] + ".json"), "w") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        add("companies", c["corp_code"], data)
        for lid in dict.fromkeys(lookup_ids(q) + lookup_ids(data["company"]["name"]) + lookup_ids(c["stock_code"])):
            add("lookup", lid, {"corpCode": c["corp_code"], "name": data["company"]["name"]})
        if req:
            add("results", req, {"status": "done", "corpCode": c["corp_code"], "name": data["company"]["name"], "at": now()})
        f = data["financials"] or {}
        filled = sum(v is not None for k in ("revenue", "operatingIncome", "netIncome", "ocf") for v in f.get(k, []))
        print(f"[{q}] {data['company']['name']} {c['corp_code']} | 재무 {f.get('fsDiv','-')} {f.get('years',['-'])[0]}~{f.get('years',['-'])[-1]} 채움 {filled}/20 "
              f"| BS {f.get('balanceAsOf','-')} | 메자닌 {len(data['mezzanine']['issued'])}건 | 최대주주 {data['mezzanine'].get('majorShareholderPct')}%")
    with open(os.path.join(up, "writes.json"), "w") as fh:
        json.dump(writes, fh, ensure_ascii=False, indent=1)
    print(f"업로드 목록: {os.path.join(up, 'writes.json')} ({len(writes)}건)")


if __name__ == "__main__":
    main(sys.argv[1:] or sys.exit(__doc__))
