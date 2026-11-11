"""Bounded, fully logged offline gold tuning; never changes production weights."""
from __future__ import annotations
import argparse
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import numpy as np
import gold_score as gold
from gold_research import samples, corr, SEED


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def available_series(inputs):
    """Features are dated by proxy availability, never by their observation alone."""
    output = {}
    for source, key in [('real','g01_real_rate'), ('dollar','g02_dollar'),
                        ('stress','g05_stress'), ('etf','g03_etf'), ('cot','g04_comex')]:
        rows = sorted(inputs[source])
        for i, (day, value) in enumerate(rows):
            vals = {}
            if source in ('real','stress','cot'):
                vals[source+'_level'] = value
            windows = (4,12) if source=='etf' else (4,) if source=='cot' else (20,60)
            for n in windows:
                if i >= n:
                    vals[f'{source}_{n}'] = (math.log(value/rows[i-n][1])
                        if source in ('etf','dollar') else value-rows[i-n][1])
            for name, v in vals.items():
                output.setdefault(name, []).append((gold._proxy_available_day(key,day),v))
    prices = sorted(inputs['reference']['lbma_pm_usd'])
    logs = np.log([p for _,p in prices])
    returns = np.diff(logs)
    for i in range(120,len(prices)):
        day = gold._proxy_available_day('g08_momentum', prices[i][0])
        for n in (20,60,120):
            output.setdefault(f'mom_{n}',[]).append((day,float(logs[i]-logs[i-n])))
        for n in (5,20,60):
            output.setdefault(f'vol_{n}',[]).append((day,float(np.std(returns[i-n:i],ddof=1)*np.sqrt(252))))
        for decay in (.94,.97,.99):
            weights = decay**np.arange(i-1,-1,-1)
            vol = float(np.sqrt(np.sum(weights*returns[:i]**2)/weights.sum()*252))
            output.setdefault(f'ewma_{decay}',[]).append((day,vol))
        path = np.exp(logs[i-20:i+1])
        dd = float(-np.min(path/np.maximum.accumulate(path)-1))
        output.setdefault('dd_20',[]).append((day,max(dd,1e-6)))
    # Release-date collisions use the latest observation available that day.
    return {k:sorted(dict(v).items()) for k,v in output.items()}


MARKET = ['mom_20','mom_60','mom_120','vol_20','vol_60']
MACRO = ['real_level','real_20','real_60','dollar_20','dollar_60']
PRESSURE = ['stress_level','stress_20']
FLOWS = ['etf_4','etf_12','cot_level','cot_4']


def raw_samples(inputs, histories):
    prices = sorted(inputs['reference']['lbma_pm_usd'])
    days = [d for d,_ in prices]
    logs = np.log([p for _,p in prices])
    dates = {k:[d for d,_ in v] for k,v in histories.items()}
    weeks = {}
    for i, day in enumerate(days):
        weeks[datetime.fromisoformat(day).isocalendar()[:2]] = i
    output = []
    for i in weeks.values():
        day = days[i]
        values, releases = {}, {}
        for key, series in histories.items():
            pos = bisect_right(dates[key],day)-1
            if pos < 0:
                continue
            release = dates[key][pos]
            age = (datetime.fromisoformat(day)-datetime.fromisoformat(release)).days
            if age > (14 if key.startswith(('dollar','cot','etf')) else 7):
                continue
            values[key] = series[pos][1]
            releases[key] = release
        if not all(k in values for k in MARKET+MACRO+PRESSURE+['dd_20']):
            continue
        entry = i+1
        for target, h in [('return5',5),('return20',20),('return60',60),('vol20',20),('drawdown20',20)]:
            if entry+h >= len(days):
                continue
            path = np.exp(logs[entry:entry+h+1])
            actual = (float(logs[entry+h]-logs[entry]) if target.startswith('return') else
                      float(np.std(np.diff(logs[entry:entry+h+1]),ddof=1)*np.sqrt(252)) if target=='vol20' else
                      max(float(-np.min(path/np.maximum.accumulate(path)-1)),1e-6))
            output.append({'as_of':day,'entry_date':days[entry], 'end_date':days[entry+h],
                           'horizon':h,'target':target,'actual':actual,'features':values,
                           'feature_available_at':releases})
    return output


def configurations(groups):
    return [{'id':f'{name}/w{window}/a{alpha}/s{shrink}', 'group':name,'features':keys,
             'window':window,'alpha':alpha,'shrink':shrink}
            for name,keys in groups.items() for window in (52,104,208,0)
            for alpha in (.1,1,10,100) for shrink in (.25,1)]


def ridge_predict(train, row, config, risk=False):
    selected = train[-config['window']:] if config['window'] else train
    keys = config['features']
    x = np.asarray([[r['features'][k] for k in keys] for r in selected])
    xx = np.asarray([row['features'][k] for k in keys])
    mu, sd = x.mean(axis=0), x.std(axis=0)
    sd = np.where(sd>1e-10, sd,1)
    z, zz = np.clip((x-mu)/sd,-4,4), np.clip((xx-mu)/sd,-4,4)
    y = np.asarray([r['actual'] for r in selected])
    log_target = risk and config.get('transform','log')=='log'
    if log_target:
        y = np.log(np.maximum(y,1e-6))
    ym, zm = y.mean(), z.mean(axis=0)
    z = z-zm
    beta = np.linalg.solve(z.T@z/len(y)+config['alpha']*np.eye(len(keys)), z.T@(y-ym)/len(y))
    pred = float(np.clip(ym+(zz-zm)@beta, ym-3*y.std(),ym+3*y.std()))
    return math.exp(pred) if log_target else max(pred,1e-6) if risk else pred


def walk(rows, configs, stronger_baselines=False):
    output = []
    risk = not rows[0]['target'].startswith('return')
    for i,row in enumerate(rows):
        train = [r for r in rows[:i] if r['end_date']<row['as_of']]
        if len(train)<52:
            continue
        pred = {'mean':float(np.mean([r['actual'] for r in train]))}
        if not risk:
            pred['zero'] = 0.
            anchor = 0.
            if stronger_baselines:
                for window in (52,104,208,0):
                    m = float(np.mean([r['actual'] for r in (train[-window:] if window else train)]))
                    for shrink in (.25,.5,1):
                        pred[f'drift/w{window}/s{shrink}'] = m*shrink
        else:
            anchor = row['features']['vol_20' if row['target']=='vol20' else 'dd_20']
            pred['persistence20'] = anchor
            if row['target']=='vol20':
                pred['persistence60'] = row['features']['vol_60']
                if stronger_baselines:
                    for weight in (.25,.5,.75):
                        pred[f'blend20_60/{weight}'] = math.sqrt(weight*anchor**2+(1-weight)*row['features']['vol_60']**2)
                    for decay in (.94,.97,.99):
                        pred[f'ewma/{decay}'] = row['features'][f'ewma_{decay}']
        # Config pairs with different shrink factors reuse the same fitted model.
        fitted = {}
        for config in configs:
            key = (config['group'],config['window'],config['alpha'],config.get('transform','log'))
            if key not in fitted:
                fitted[key] = ridge_predict(train,row,config,risk)
            pred[config['id']] = float(anchor+config['shrink']*(fitted[key]-anchor))
        mature = [r for r in output if r['end_date']<row['as_of']][-52:]
        chosen = 'persistence20' if risk else 'zero'
        if len(mature)>=26:
            chosen = min(pred, key=lambda k:np.mean([(r['actual']-r['predictions'][k])**2 for r in mature]))
        output.append({**row, 'train_n':len(train), 'train_label_end':train[-1]['end_date'],
                       'selection_n':len(mature),'selected':chosen,'predictions':{**pred,'nested':pred[chosen]}})
    return output


def nonoverlap_indices(rows):
    """Trading holidays mean a fixed 4-week stride need not separate 20-day labels."""
    phases = []
    for offset in range(math.ceil(rows[0]['horizon']/5)+1):
        indices, end = [], ''
        for i in range(offset,len(rows)):
            if rows[i]['entry_date'] >= end:
                indices.append(i)
                end = rows[i]['end_date']
        if indices:
            phases.append(indices)
    return phases


def summarize(rows, baseline, bootstrap=True):
    if not rows:
        return {}
    names = list(rows[0]['predictions'])
    y = np.asarray([r['actual'] for r in rows])
    p = np.asarray([[r['predictions'][k] for k in names] for r in rows])
    loss = (y[:,None]-p)**2
    bl = loss[:,names.index(baseline)]
    meanloss = loss[:,names.index('mean')].mean()
    denom = bl.mean()
    gains = (bl[:,None]-loss)/max(denom,1e-20)
    r2 = gains.mean(axis=0)
    simultaneous = None
    if bootstrap and len(rows)>=26:
        block = math.ceil(rows[0]['horizon']/5)+1
        rng = np.random.default_rng(SEED)
        indices = np.asarray([np.concatenate([(s+np.arange(block))%len(rows) for s in
                    rng.integers(0,len(rows),math.ceil(len(rows)/block))])[:len(rows)] for _ in range(400)])
        centered = gains-gains.mean(axis=0)
        noise = centered[indices].mean(axis=1)
        simultaneous = float(np.quantile(np.max(np.abs(noise),axis=1),.95))
    output = {}
    nonoverlap = nonoverlap_indices(rows)
    for j,k in enumerate(names):
        folds = np.array_split(np.arange(len(rows)),3)
        fold_gains = [float(1-loss[idx,j].mean()/max(bl[idx].mean(),1e-20)) for idx in folds]
        phases = [float(1-loss[idx,j].mean()/max(bl[idx].mean(),1e-20)) for idx in nonoverlap]
        output[k] = {'n':len(rows),'mse':float(loss[:,j].mean()),'r2_vs_primary':float(r2[j]),
                     'r2_vs_mean':float(1-loss[:,j].mean()/max(meanloss,1e-20)),
                     'rank_ic':corr(p[:,j],y),'fold_r2':fold_gains,
                     'positive_folds':sum(v>0 for v in fold_gains),
                     'nonoverlap_phase_r2_min':min(phases),
                     'nonoverlap_positive_share':float(np.mean(np.asarray(phases)>0)),
                     'nonoverlap_phase_n':[len(idx) for idx in nonoverlap],
                     'simultaneous_ci95': [float(r2[j]-simultaneous),float(r2[j]+simultaneous)] if simultaneous is not None else None}
        if rows[0]['target'].startswith('return'):
            output[k]['direction_accuracy'] = float(np.mean((p[:,j]>0)==(y>0))) if k!='zero' else None
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--round2',action='store_true',help='Adaptive robustness round with stronger simple benchmarks')
    args = parser.parse_args()
    inputs = json.loads(Path(args.inputs).read_text())
    out = Path(args.output);out.mkdir(parents=True,exist_ok=True)
    scorekeys = list(gold._META)
    groups = {'score8':{'expert':['expert'],'equal':['equal'],'rates_fx':['rates_fx'],
                        'momentum':['momentum'],'all_scores':scorekeys},
              'macro':{'market':MARKET,'rates_fx':MACRO,'macro_market':MARKET+MACRO,
                       'pressure_macro_market':MARKET+MACRO+PRESSURE},
              'flows':{'market':MARKET,'macro_market':MARKET+MACRO,
                       'flows':FLOWS,'all':MARKET+MACRO+PRESSURE+FLOWS}}
    configs = {k:configurations(v) for k,v in groups.items()}
    if args.round2:
        groups = {'macro':{**groups['macro'], 'vol_only':['vol_20','vol_60'],
                           'har_vol':['vol_5','vol_20','vol_60']}}
        configs = {'macro':configurations(groups['macro'])}
        configs['macro'] += [{**c,'id':c['id']+'/level','transform':'level'} for c in configs['macro']]
    manifest = {'status':'exploratory_reconstructed','independent_oos':False,
                'created_at':datetime.now(timezone.utc).isoformat(),'seed':SEED,
                'source_hash':gold._hash_gold(inputs),'code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'model_code_hash':hashlib.sha256(Path(gold.__file__).read_bytes()).hexdigest(),
                'protocol_hash':hashlib.sha256((Path(__file__).parents[1]/'research/gold_tuning_protocol_2026-10-20.md').read_bytes()).hexdigest(),
                'configurations':configs,'round':2 if args.round2 else 1,'never_changes_production':True}
    dump(out/'manifest.json',manifest)
    (out/'research_code.py').write_bytes(Path(__file__).read_bytes())
    (out/'gold_score_snapshot.py').write_bytes(Path(gold.__file__).read_bytes())
    dump(out/'inputs.json',inputs)
    raw = raw_samples(inputs,available_series(inputs))
    last_day = max(d for d,_ in inputs['reference']['lbma_pm_usd'])
    score = gold.calculate_gold(inputs,[],datetime.fromisoformat(last_day).replace(tzinfo=timezone.utc))
    score_rows = samples(inputs,score['_research_histories'])
    for r in score_rows:
        r['target']='return'+str(r['horizon'])
        r['features'].update(r['indicator_scores'])
    results, predictions = {}, {}
    for panel in groups:
        panel_rows = score_rows if panel=='score8' else [r for r in raw if panel!='flows' or all(k in r['features'] for k in FLOWS)]
        results[panel],predictions[panel] = {},{}
        targets = ['return5','return20','return60'] if panel=='score8' else ['return5','return20','return60','vol20','drawdown20']
        for target in targets:
            rows = sorted([r for r in panel_rows if r['target']==target],key=lambda r:r['as_of'])
            active_configs = [c for c in configs[panel] if not target.startswith('return') or c.get('transform')!='level']
            forecasts = walk(rows,active_configs,args.round2)
            predictions[panel][target] = forecasts
            baseline = 'zero' if target.startswith('return') else 'persistence20'
            full = summarize(forecasts,baseline)
            tail = summarize(forecasts[-26:],baseline,False)
            results[panel][target] = {'primary_baseline':baseline,'start':forecasts[0]['as_of'] if forecasts else None,
                                      'end':forecasts[-1]['as_of'] if forecasts else None,'full':full,'tail26':tail}
            dump(out/f'forecasts_{panel}_{target}.json',forecasts)
            best = max(full,key=lambda k:full[k]['r2_vs_primary']) if full else None
            print(panel,target,'n',len(forecasts),'best',best,full.get(best,{}).get('r2_vs_primary'),
                  'nested',full.get('nested',{}).get('r2_vs_primary'),flush=True)
    # Same prediction dates across panels; models retain their own mature training history.
    common = {}
    for target in ('return5','return20','return60'):
        dates = set.intersection(*[set(r['as_of'] for r in predictions[p][target]) for p in groups])
        common[target] = {p:summarize([r for r in predictions[p][target] if r['as_of'] in dates],'zero',False) for p in groups}
    dump(out/'results.json',{'manifest':manifest,'panels':results,'common_dates':common,
                            'limits':['latest revisions; proxy release dates','all historical periods previously observable',
                                      'risk skill is not return alpha','simultaneous intervals are approximate within each panel and target']})
    print('Saved',out.resolve(),flush=True)


if __name__=='__main__':
    main()
