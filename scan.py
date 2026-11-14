"""Publish Zhuoyao Top3 candidates, using Eastmoney spot and Tencent daily bars."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
from time import sleep
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TENCENT = 'https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get'
EASTMONEY = 'https://push2.eastmoney.com/api/qt/clist/get'
PREFIXES = ('600', '601', '603', '605', '000', '001', '002', '003')


def request(url, params):
    for attempt in range(3):
        try:
            req = Request(url + '?' + urlencode(params), headers={'User-Agent': 'Mozilla/5.0'})
            with urlopen(req, timeout=15) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            sleep(attempt + 1)


def daily(symbol):
    result = request(TENCENT, {'param': f'{symbol},day,,,260,qfq'})
    data = (result.get('data') or {}).get(symbol) or {}
    rows = data.get('qfqday') or data.get('day')
    if not rows:
        raise ValueError(f'{symbol}: no Tencent daily bars')
    return rows


def gold_cross(rows):
    rows = [r for r in rows if float(r[5]) > 0]
    if len(rows) < 11:
        return False
    smoothed = previous = current = None
    for i in range(9, len(rows)):
        window = rows[i-9:i+1]
        high = max(float(r[3]) for r in window)
        low = min(float(r[4]) for r in window)
        if high <= low:
            previous = None
            current = None
            continue
        position = (float(rows[i][2]) - low) / (high - low)
        gap = 400 * position - 180
        smoothed = gap if smoothed is None else .2 * gap + .8 * smoothed
        previous, current = current, smoothed
    return previous is not None and current is not None and previous <= 0 < current


def spot(day):
    records = {}
    page = 1
    while True:
        payload = request(EASTMONEY, {
            'pn': page, 'pz': 100, 'po': 1, 'fid': 'f12', 'np': 1, 'fltt': 2,
            'fs': 'm:0+t:6,m:1+t:2', 'fields': 'f2,f5,f8,f12,f14,f124',
        })
        data = payload.get('data') or {}
        batch = data.get('diff') or []
        if not batch:
            raise ValueError(f'Eastmoney returned empty page {page}')
        for row in batch:
            records[row['f12']] = row
        if len(records) >= int(data['total']):
            break
        page += 1
        if page > 60:
            raise ValueError('Incomplete Eastmoney stock list')
    if len(records) < 2500:
        raise ValueError('Incomplete Eastmoney stock list')
    current_quotes = 0
    for row in records.values():
        code, name = row['f12'], row['f14']
        if not code.startswith(PREFIXES) or 'ST' in name.upper() or '退' in name:
            continue
        try:
            close, volume, turnover = (float(row[key]) for key in ('f2', 'f5', 'f8'))
            quote_day = datetime.fromtimestamp(int(row['f124']), ZoneInfo('Asia/Shanghai')).date().isoformat()
        except (TypeError, ValueError):
            continue
        if quote_day != day or close <= 0:
            continue
        current_quotes += 1
        if 300000 <= volume < 800000 and 5 <= turnover < 15:
            cap = close * volume * 10000 / turnover
            if cap < 7.5e9:
                yield dict(code=code,name=name,turnover=turnover,volume_lots=volume,float_cap=cap)

    if current_quotes < 2000:
        raise ValueError(f'Only {current_quotes} fresh main-board quotes for {day}')

def symbol(code):
    return ('sh' if code.startswith('6') else 'sz') + code


def scan(day, stage, index):
    if stage == 'close':
        def check(row):
            bars = daily(symbol(row['code']))
            if bars[-1][0] != day:
                raise ValueError(f"{row['code']}: daily bar not published for {day}")
            return row if gold_cross(bars) else None

        try:
            snapshot = list(spot(day))
        except (OSError, ValueError) as error:
            print(f'Eastmoney snapshot unavailable: {error}. Using TDX and Tencent daily data.')
            from backfill import historical_candidates
            results = historical_candidates(day)
        else:
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = [row for row in workers.map(check, snapshot) if row is not None]
        return dict(date=day, stage=stage, candidates=sorted(results, key=lambda r:r['code']))

    previous = index[-2][0]
    watchlist = json.loads(Path(f'reports/{previous}-close.json').read_text())
    if watchlist.get('date') != previous:
        raise ValueError('Previous session watchlist date mismatch')
    ranked = []
    for row in watchlist['candidates']:
        bars = daily(symbol(row['code']))
        if bars[-1][0] != day:
            raise ValueError(f"{row['code']}: opening bar not verified for {day}")
        if bars[-2][0] != previous:
            raise ValueError(f"{row['code']}: previous bar date mismatch")
        gap = float(bars[-1][1]) / float(bars[-2][2]) - 1
        if -.02 <= gap < -.01 or 0 <= gap < .03:
            ranked.append(dict(row, opening_gap=gap))
    ranked.sort(key=lambda r:(r['opening_gap'] >= 0, r['opening_gap'], r['code']))
    return dict(date=day, signal_date=previous, stage=stage, candidates=ranked[:3])


def render_markdown(result):
    opening = result['stage'] == 'open'
    title = '开盘 Top3' if opening else '盘后初筛'
    rows = result['candidates']
    lines = [f"# 主力捉妖 · {result['date']} {title}", ""]
    if opening:
        lines += [f"信号日 {result['signal_date']}。按开盘缺口排序，最多取三只。", ""]
    else:
        lines += ["盘后候选按代码排列，下一交易日开盘确认排名。", ""]
    lines += [f"候选数 **{len(rows)}**。", ""]
    if rows:
        columns = ["排名", "代码", "名称", "换手率", "成交量（手）", "估算流通市值（亿元）"]
        if opening:
            columns.append("开盘缺口")
        lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
        for rank, row in enumerate(rows, 1):
            name = row["name"].replace("|", "&#124;").replace("\n", " ")
            cells = [str(rank) if opening else "—", row["code"], name,
                     f"{row['turnover']:.2f}%", f"{row['volume_lots']:,.0f}", f"{row['float_cap'] / 1e8:.2f}"]
            if opening:
                cells.append(f"{row['opening_gap']:+.2%}")
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("本次扫描无符合条件的候选。")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['close', 'open'])
    parser.add_argument('--date')
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    today = now.date().isoformat()
    day = args.date or today
    if args.stage == 'open' and day != today:
        parser.error('Opening scan requires the current session')
    if not args.date and now.weekday() >= 5:
        return
    index = [bar for bar in daily('sh000300') if bar[0] <= day]
    if index[-1][0] != day:
        raise SystemExit(f'{day}: no current index bar (market closed or data delayed)')
    result = scan(day, args.stage, index)
    path = Path(f'reports/{day}-{args.stage}.json')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    markdown = render_markdown(result)
    path.with_suffix('.md').write_text(markdown, encoding='utf-8')
    print(markdown, end='')


if __name__ == '__main__':
    main()
