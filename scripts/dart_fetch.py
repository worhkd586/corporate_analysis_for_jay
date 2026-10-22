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
import ast
import datetime as dt
import math
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
    "pretaxIncome": (("IS", "CIS"), ["ifrs-full_ProfitLossBeforeTax"], r"^법인세(비용)?차감전"),
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
    # 사업보고서 데이터가 없는 해(상장 전 등)는 다음 해 보고서의 전기, 그다음 해 보고서의 전전기 금액으로 채운다
    def source(i):
        if raw[i]:
            return raw[i], "thstrm_amount"
        if i + 1 < len(raw) and raw[i + 1]:
            return raw[i + 1], "frmtrm_amount"
        if i + 2 < len(raw) and raw[i + 2]:
            return raw[i + 2], "bfefrmtrm_amount"
        return [], None
    for i in range(len(raw)):
        items, col = source(i)
        rows.append(extract(items, col) if items else {})
    series = lambda k: [eok(r.get(k)) for r in rows]
    f = {"unit": "억원", "fsDiv": "연결" if fs_div == "CFS" else "별도", "years": years,
         "revenue": series("revenue"), "operatingIncome": series("operatingIncome"),
         "netIncome": series("netIncome"), "ocf": series("ocf"),
         # 법차손(법인세비용차감전손실) / 자기자본 비율 점검용
         "pretaxIncome": series("pretaxIncome"), "equityHist": series("equity"),
         # 연도별 대차대조표 (추이 차트·이상 신호용)
         "totalAssetsHist": series("totalAssets"), "liabilitiesHist": series("totalLiabilities"),
         "cashHist": series("cash"), "shortDebtHist": series("shortDebt")}
    last = rows[-1]
    bs_src = f"{latest} 사업보고서"
    interim, interim_label = None, None
    # 대차대조표 항목은 사업보고서 이후의 가장 최근 분기보고서로 갱신 (상환능력은 최신 잔액이 중요)
    for code, label in REPORTS[1:]:
        items, _ = statements(corp, latest + 1, code)
        if items:
            last = {**last, **{k: v for k, v in extract(items).items()
                               if k in ("totalAssets", "totalLiabilities", "equity", "cash", "shortDebt", "totalDebt") and v is not None}}
            bs_src = f"{latest + 1} {label}"
            interim, interim_label = items, f"{latest + 1} {label.replace('보고서', '')}"
            break
    for k in ("totalAssets", "totalLiabilities", "equity", "cash", "shortDebt", "totalDebt"):
        f[k] = eok(last.get(k))
    f["interestExpense"] = eok(rows[-1].get("interestExpense"))
    f["balanceAsOf"] = bs_src
    f["statements"] = full_statements([source(i) for i in range(len(raw))], years, interim, interim_label)
    return f


# 연도마다 이름만 조금 다른 같은 계정을 한 줄로 합친다: "영업이익(손실)"="영업이익", "금융원가"="금융비용", "영업수익"="매출액"
ACCOUNT_ALIASES = [(r"^(수익\(매출액\)|영업수익|매출)$", "매출액"), (r"^금융원가$", "금융비용"), (r"^영업비용$", "매출원가"),
                   (r"^지분법(이익|손익|손실)$", "지분법손익"), (r"^기타(영업외)?(수익|이익)$", "기타수익"), (r"^기타(영업외)?(비용|손실)$", "기타비용")]


def canon_account(nm):
    base = re.sub(r"\s+", "", nm)
    base = re.sub(r"\((손실|이익|손익)\)$", "", base)          # 영업이익(손실) → 영업이익
    base = re.sub(r"^(당기순|분기순|반기순)", "당기순", base)
    for rx, to in ACCOUNT_ALIASES:
        if re.search(rx, base):
            return to
    return base


# 손익계산서는 DART 응답 순서가 표시 순서가 아니어서 일반적인 손익 흐름 순서로 다시 놓는다
IS_ORDER = [r"^(매출액|수익\(매출액\)|영업수익|매출)$", r"매출원가|영업비용", r"매출총", r"판매비", r"^영업이익|^영업손", r"기타수익|기타영업외수익",
            r"기타비용|기타영업외비용", r"금융수익", r"금융(비용|원가)", r"지분법", r"법인세(비용)?차감전", r"^법인세", r"계속영업", r"중단영업",
            r"^당기순", r"기타포괄", r"총포괄", r"지배기업|지배주주", r"비지배", r"주당"]


def full_statements(sources, years, interim, interim_label):
    """재무상태표·손익계산서·현금흐름표 전체 계정 (억원). 대시보드의 '재무제표 상세'용.

    sources: 연도별 (보고서 항목, 금액 열). 같은 이름의 계정은 연도가 달라도 한 줄로 합친다
    (보고서마다 account_id가 바뀌는 경우가 있어서). 한 보고서 안에 같은 이름이 또 나오면 #2, #3으로 구분한다.
    interim: 최근 분기·반기 보고서 항목 (손익은 누적 금액).
    """
    cols = list(years) + ([interim_label] if interim else [])
    latest = next((items for items, _ in reversed(sources) if items), [])
    is_sj = ("IS",) if any(it.get("sj_div") == "IS" for it in latest) else ("CIS",)
    out = {"columns": cols}

    def keyed(items, sjs):
        seen = {}
        for it in items:
            if it.get("sj_div") not in sjs:
                continue
            nm = (it.get("account_nm") or "").strip()
            base = canon_account(nm)
            seen[base] = seen.get(base, 0) + 1
            yield (base if seen[base] == 1 else f"{base}#{seen[base]}"), nm, it

    for key, sjs in (("BS", ("BS",)), ("IS", is_sj), ("CF", ("CF",))):
        rows = {}
        def put(items, col, ci, cum=False):
            for k, nm, it in keyed(items, sjs):
                amt = (it.get("thstrm_add_amount") or it.get(col)) if cum else it.get(col)
                r = rows.setdefault(k, {"n": nm, "v": [None] * len(cols)})
                if r["v"][ci] is None:
                    r["v"][ci] = eok(num(amt))
        for ci, (items, col) in enumerate(sources):
            if items:
                put(items, col, ci)
        if interim:
            put(interim, "thstrm_amount", len(cols) - 1, cum=key == "IS")
        pos = {k: i for i, (k, _, _) in enumerate(keyed(latest, sjs))}
        def rank(k):
            p = pos.get(k, 10000)
            if key != "IS":
                return (0, p)   # 재무상태표·현금흐름표는 DART 순서(합계 → 소계 → 세부)를 그대로 쓴다
            name = rows[k]["n"].replace(" ", "")
            g = next((i for i, rx in enumerate(IS_ORDER) if re.search(rx, name)), len(IS_ORDER))
            return (g, p)
        out[key] = [rows[k] for k in sorted(rows, key=rank) if any(v is not None for v in rows[k]["v"])]
    return out


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
                # 시가하락에 따른 최저 조정가액 (EB 공시에는 없음)
                "refixFloor": num(it.get("act_mktprcfl_cvprc_lwtrsprc")),
                "convStart": kdate(it.get("cvrqpd_bgd") or it.get("exrqpd_bgd") or it.get("expd_bgd")),
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


def naver_sise(code, start, end, timeframe):
    """네이버 증권 시세 [날짜, 시가, 고가, 저가, 종가, 거래량, ...] (DART에는 주가가 없다)."""
    url = "https://api.finance.naver.com/siseJson.naver?" + urllib.parse.urlencode({
        "symbol": code, "requestType": 1, "startTime": start.strftime("%Y%m%d"), "endTime": end.strftime("%Y%m%d"), "timeframe": timeframe})
    with urllib.request.urlopen(url, timeout=30) as r:
        rows = ast.literal_eval(r.read().decode("utf-8", "replace").strip())
    return [x for x in rows[1:] if isinstance(x, list) and len(x) >= 6 and x[4]]


def price(code, shares):
    """현재가, 52주 고저, 20일 평균 거래대금(억원), 연환산 변동성(%), 24개월 월말 종가."""
    end = dt.date.today()
    try:
        daily = naver_sise(code, end - dt.timedelta(days=380), end, "day")
        monthly = naver_sise(code, end - dt.timedelta(days=760), end, "month")
    except Exception as e:  # 시세가 없어도 DART 데이터는 저장한다
        print(f"  시세 조회 실패: {e}")
        return None
    if not daily:
        return None
    year = daily[-250:]
    closes = [x[4] for x in year]
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a and b]
    mean = sum(rets) / len(rets) if rets else 0
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100 if len(rets) > 1 else None
    last20 = daily[-20:]
    cur = daily[-1][4]
    d = str(daily[-1][0])
    return {
        "source": "naver",
        "asOf": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
        "current": cur,
        "high52": max(x[2] for x in year),
        "low52": min(x[3] for x in year),
        "avgDailyValue": round(sum(x[4] * x[5] for x in last20) / len(last20) / EOK, 1),
        "volatility": round(vol, 1) if vol is not None else None,
        "marketCap": round(cur * shares / EOK) if shares else None,
        "series": [{"m": f"{str(x[0])[:4]}-{str(x[0])[4:6]}", "p": x[4]} for x in monthly[-24:]],
    }


def naver_json(path):
    with urllib.request.urlopen("https://m.stock.naver.com/api/" + path, timeout=30) as r:
        return json.loads(r.read())


def nnum(s):
    """'1,234', '35.52배', '-3,758원', 'N/A' → 숫자 또는 None."""
    m = re.search(r"-?[\d,]*\.?\d+", str(s or "").replace(" ", ""))
    return float(m.group().replace(",", "")) if m and "N/A" not in str(s) else None


def peer_metrics(code, mcap=None):
    """네이버 종목 지표(PER·PBR)와 연간 실적(최근 확정 연도 영업이익률·매출성장률)."""
    integ = naver_json(f"stock/{code}/integration")
    ti = {x.get("code"): x.get("value") for x in integ.get("totalInfos", [])}
    fin = naver_json(f"stock/{code}/finance/annual").get("financeInfo") or {}
    keys = [t["key"] for t in fin.get("trTitleList", []) if t.get("isConsensus") == "N"]
    rows = {r.get("title"): r.get("columns") or {} for r in fin.get("rowList", [])}
    val = lambda title, k: nnum((rows.get(title, {}).get(k) or {}).get("value")) if k else None
    k1 = keys[-1] if keys else None
    k0 = keys[-2] if len(keys) > 1 else None
    rev1, rev0 = val("매출액", k1), val("매출액", k0)
    return {"name": integ.get("stockName"), "code": code, "marketCap": mcap,
            "per": nnum(ti.get("per")), "pbr": nnum(ti.get("pbr")),
            "opMargin": val("영업이익률", k1),
            "revenueGrowth": round((rev1 / rev0 - 1) * 100, 1) if rev0 and rev0 > 0 and rev1 is not None else None,
            "netIncome": val("당기순이익", k1), "fy": k1}


def peers(code, n=5):
    """네이버 동일 업종에서 시가총액이 가장 비슷한 n곳 (규모가 비슷해야 비교 의미가 있다)."""
    try:
        ind = naver_json(f"stock/{code}/integration").get("industryCode")
        if not ind:
            return None
        stocks, page, name = [], 1, None
        while page <= 3:
            d = naver_json(f"stocks/industry/{ind}?page={page}&pageSize=100")
            name = name or (d.get("groupInfo") or {}).get("name")
            stocks += d.get("stocks") or []
            if len(stocks) >= (d.get("totalCount") or 0):
                break
            page += 1
        cap = lambda x: nnum(x.get("marketValue"))
        me = next((x for x in stocks if x.get("itemCode") == code), None)
        base = cap(me) if me else None
        cands = [x for x in stocks if x.get("itemCode") != code and (cap(x) or 0) > 0 and x.get("stockEndType") == "stock"]
        if base:
            cands.sort(key=lambda x: abs(math.log(cap(x) / base)))
        else:
            cands.sort(key=lambda x: -cap(x))
        out = []
        for x in cands[:n]:
            try:
                out.append(peer_metrics(x["itemCode"], cap(x)))
            except Exception as e:
                print(f"  경쟁사 {x.get('stockName')} 지표 실패: {e}")
        return {"source": "naver", "industryCode": ind, "industryName": name, "list": out}
    except Exception as e:
        print(f"  경쟁사 조회 실패: {e}")
        return None


KIS = "https://www.kisrating.com"


def kis_post(path, data):
    req = urllib.request.Request(KIS + path, data=urllib.parse.urlencode(data).encode(), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def credit_rating(name):
    """한국신용평가(KIS) 공개 등급. 등급이 없으면 None (대시보드에서 공란)."""
    q = norm(name)
    kw = re.sub(r"\s|\(주\)|㈜|주식회사", "", name or "")  # KIS 검색은 대소문자를 구분한다 (SK하이닉스)
    try:
        d = json.loads(kis_post("/ratingsSearch/corp_presearch.json",
                                {"mainSearchType": 1, "keyword": kw, "searchKeyword": kw, "searchYn": "M", "searchType": 1}))
        cands = d.get("dataList1") or []
        hit = {"kiscd": d.get("kiscd"), "upcheOpt": d.get("upcheOpt")} if d.get("isRedirect") else \
            next((c for c in cands if norm(c.get("upcheOpt")) == q), None)
        if not hit or not hit.get("kiscd"):
            return None
        page = kis_post("/ratingsSearch/corp_overview.do", {"kiscd": hit["kiscd"]})
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
        # 예: "회사채(선순위) 등급 BBB Outlook/Watchlist 부정적 평가일 2026.06.29", "기업어음 등급 A1 Watchlist 평가일 2026.06.12"
        ratings = [{"type": re.sub(r"^평가의견\s+\S+\s+", "", t.strip()), "grade": g, "outlook": o or None, "date": dt_.replace(".", "-")}
                   for t, g, o, dt_ in re.findall(r"([가-힣A-Za-z()·/ ]{2,20}?)\s*등급\s+(\S+)\s+(?:Outlook/)?Watchlist\s*(\S*?)\s*평가일\s+([\d.]{10})", text)]
        if not ratings:
            return None
        return {"source": "한국신용평가", "kiscd": hit["kiscd"], "name": hit.get("upcheOpt"), "ratings": ratings}
    except Exception as e:
        print(f"  신용등급 조회 실패: {e}")
        return None


def disclosures(corp):
    """최근 2년 공시 목록 (정성평가 근거). 제목·접수일·접수번호만 남긴다."""
    end = dt.date.today()
    bgn = end - dt.timedelta(days=730)
    out = []
    for page in range(1, 4):
        data = json.loads(get("list.json", corp_code=corp, bgn_de=bgn.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
                              page_no=page, page_count=100))
        if data.get("status") != "000":
            break
        for it in data.get("list", []):
            out.append({"d": it.get("rcept_dt"), "t": re.sub(r"\s+", " ", it.get("report_nm") or "").strip(),
                        "n": it.get("rcept_no"), "f": it.get("flr_nm")})
        if page >= int(data.get("total_page") or 1):
            break
    return out


def audit(corp, year):
    """회계감사인·감사의견 (사업보고서). 필드는 DART가 준 그대로 짧게 잘라 둔다."""
    for y in (year, year - 1):
        rows = get_json("accnutAdtorNmNdAdtOpinion.json", corp_code=corp, bsns_year=y, reprt_code="11011")
        if rows:
            skip = {"rcept_no", "corp_cls", "corp_code", "corp_name"}
            return {"year": y, "rows": [{k: str(v)[:120] for k, v in r.items() if k not in skip} for r in rows[:4]]}
    return None


def build(c):
    info = json.loads(get("company.json", corp_code=c["corp_code"]))
    fin = financials(c["corp_code"])
    year = int(fin["years"][-1]) + 1 if fin else dt.date.today().year
    mz = mezzanine(c["corp_code"])
    n_shares = shares(c["corp_code"], year)
    sh = shareholders(c["corp_code"], year)
    return {
        "source": "DART",
        "fetchedAt": dt.datetime.now().isoformat(timespec="seconds"),
        "company": {"name": info.get("corp_name") or c["name"], "ticker": c["stock_code"], "corpCode": c["corp_code"],
                    "market": MARKET.get(info.get("corp_cls"), info.get("corp_cls")), "ceo": info.get("ceo_nm"),
                    "indutyCode": info.get("induty_code"), "homepage": info.get("hm_url")},
        "financials": fin,
        "sharesOutstanding": n_shares,
        "price": price(c["stock_code"], n_shares),
        "peers": peers(c["stock_code"]),
        "credit": credit_rating(info.get("corp_name") or c["name"]),
        "mezzanine": {"issued": mz, **sh},
        "audit": audit(c["corp_code"], year - 1),
        "disclosures": disclosures(c["corp_code"]),
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
              f"| BS {f.get('balanceAsOf','-')} | 메자닌 {len(data['mezzanine']['issued'])}건 | 최대주주 {data['mezzanine'].get('majorShareholderPct')}% "
              f"| 공시 {len(data['disclosures'])}건 | 감사 {'있음' if data['audit'] else '없음'} "
              f"| 주가 {(data['price'] or {}).get('current', '없음')} ({(data['price'] or {}).get('asOf', '-')}) "
              f"| 경쟁사 {len((data['peers'] or {}).get('list', []))}곳 "
              f"| 신용등급 {', '.join(r['type'] + ' ' + r['grade'] for r in (data['credit'] or {}).get('ratings', [])) or '없음'}")
    with open(os.path.join(up, "writes.json"), "w") as fh:
        json.dump(writes, fh, ensure_ascii=False, indent=1)
    print(f"업로드 목록: {os.path.join(up, 'writes.json')} ({len(writes)}건)")


if __name__ == "__main__":
    main(sys.argv[1:] or sys.exit(__doc__))
