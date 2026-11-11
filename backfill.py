import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scan import TENCENT, daily, gold_cross, request, symbol
from tdx_daily import ROOT, package


def read_session(day):
    package(day)
    with (ROOT / (day + '.csv')).open(encoding='utf-8', newline='') as f:
        return {r['code']:r for r in csv.DictReader(f)}


def rank(watchlist, signal_day, entry_day):
    ranked = []
    for row in watchlist:
        bars = {r[0]:r for r in daily(symbol(row['code']))}
        if entry_day not in bars:
            raise ValueError(f"{row['code']}: missing entry bar")
        entry, prior = bars[entry_day], bars[signal_day]
        gap = float(entry[1])/float(prior[2])-1
        if -.02 <= gap < -.01 or 0 <= gap < .03:
            ranked.append(dict(row,opening_gap=gap,entry_open=float(entry[1]),
                               entry_close=float(entry[2]),
                               entry_day_return=float(entry[2])/float(entry[1])-1))
    return sorted(ranked,key=lambda r:(r['opening_gap']>=0,r['opening_gap'],r['code']))[:3]


def historical_candidates(day):
    market=read_session(day)

    def screen(item):
        code,row=item
        if not code.startswith(('600','601','603','605','000','001','002','003')) or 'ST' in row['name'].upper() or '退' in row['name']:
            return None
        volume=float(row['volume'])
        if not (300000 <= volume < 800000 or 30000000 <= volume < 80000000):
            return None
        sym=symbol(code)
        response=request(TENCENT,{'param':f'{sym},day,,,25,'})
        rows=((response.get('data') or {}).get(sym) or {}).get('day') or []
        raw=next((r for r in rows if r[0]==day),None)
        if raw is None or len(raw)<8:
            raise ValueError(f'{code}: missing raw bar for {day}')
        volume,turnover,close=float(raw[5]),float(raw[7]),float(raw[2])
        if not (300000<=volume<800000 and 5<=turnover<15):
            return None
        cap=close*volume*10000/turnover
        if cap>=7.5e9 or not gold_cross([r for r in daily(sym) if r[0]<=day]):
            return None
        return dict(code=code,name=row['name'],turnover=turnover,volume_lots=volume,float_cap=cap)

    with ThreadPoolExecutor(max_workers=10) as workers:
        return sorted((r for r in workers.map(screen,market.items()) if r),key=lambda r:r['code'])


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('signal_day')
    parser.add_argument('entry_day')
    args=parser.parse_args()
    day,entry_day=args.signal_day,args.entry_day
    reports=Path('reports')
    candidates=historical_candidates(day)
    result=dict(date=day,stage='close',candidates=candidates)
    opened=dict(date=entry_day,signal_date=day,stage='open',candidates=rank(candidates,day,entry_day))
    prior=[]
    if day=='2026-11-09':
        prior=rank([dict(code='000151',name='中成股份'),dict(code='603212',name='赛伍技术')],'2026-11-06',day)
        for row in prior:
            bars={r[0]:r for r in daily(symbol(row['code']))}
            row['exit_close']=float(bars[entry_day][2])
            row['gross_return']=row['exit_close']/row['entry_open']-1
    for stage in [result,opened]+([dict(date=day,signal_date='2026-11-06',stage='open',candidates=prior)] if day=='2026-11-09' else []):
        content=json.dumps(stage,ensure_ascii=False,indent=2)+'\n'
        (reports/f"{stage['date']}-{stage['stage']}.json").write_text(content,encoding='utf-8')
        print(content)


if __name__=='__main__':
    main()
