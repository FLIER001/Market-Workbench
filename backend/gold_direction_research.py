"""Exploratory directional gold priors; probabilities, abstention and honest baselines."""
from __future__ import annotations
import argparse
from bisect import bisect_right
from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path
import json
import numpy as np
import gold_score as gold
from gold_research import rank, SEED
from gold_tuning import available_series, raw_samples, dump, nonoverlap_indices

ROOT=Path(__file__).parents[1]
GROUPS={
    'short_price':['mom_5','mom_20','rsi14','z20'],
    'trend':['mom_20','mom_60','mom_120','ma60_risk','ma120_risk'],
    'rates_fx':['real_20','real_60','dollar_20','dollar_60'],
    'price_macro':['mom_20','mom_60','mom_120','real_20','real_60','dollar_20','dollar_60'],
    'volatility':['vol_20','vol_60','vol_ratio'],
    'reversal':['rsi14','z20','dd_20','vol_ratio'],
}
SINGLE=['mom_5','mom_20','mom_60','mom_120','rsi14','z20','ma60_risk','ma120_risk',
        'real_20','real_60','dollar_20','dollar_60','vol_ratio']
FLOWS=['etf_4','etf_12','cot_level','cot_4']


def feature_series(inputs):
    series=available_series(inputs)
    prices=sorted(inputs['reference']['lbma_pm_usd'])
    logs=np.log([v for _,v in prices]); ret=np.diff(logs)
    for i in range(120,len(prices)):
        day=gold._proxy_available_day('g08_momentum',prices[i][0])
        vol20=float(np.std(ret[i-20:i],ddof=1)); vol60=float(np.std(ret[i-60:i],ddof=1))
        gains=np.maximum(ret[i-14:i],0).mean(); losses=np.maximum(-ret[i-14:i],0).mean()
        values={'mom_5':float(logs[i]-logs[i-5]),
                'rsi14':float(gains/(gains+losses)) if gains+losses>0 else .5,
                'z20':float((logs[i]-np.mean(logs[i-19:i+1]))/max(np.std(logs[i-19:i+1]),1e-8)),
                'vol_ratio':vol20/max(vol60,1e-8)}
        for n in (20,60,120):
            values[f'ma{n}_risk']=float((logs[i]-np.mean(logs[i-n+1:i+1]))/max(vol20*np.sqrt(n),1e-8))
        for n in (1,3,5,10,20,40,50,60,70,80,100,120):
            values[f'mom_risk_{n}']=float((logs[i]-logs[i-n])/max(vol20*np.sqrt(n),1e-8))
        for n in (5,7):
            gains=np.maximum(ret[i-n:i],0).mean(); losses=np.maximum(-ret[i-n:i],0).mean()
            values[f'rsi{n}']=float(gains/(gains+losses)) if gains+losses>0 else .5
        for key,value in values.items():
            series.setdefault(key,[]).append((day,value))
    return series


def configs(groups=GROUPS, singles=SINGLE):
    result=[]
    for group,keys in groups.items():
        for window in (52,104,156,0):
            for alpha in (.1,1,10):
                for shrink in (.5,1):
                    result.append({'id':f'logit/{group}/w{window}/a{alpha}/s{shrink}',
                                   'method':'logit','group':group,'features':keys,'window':window,
                                   'alpha':alpha,'shrink':shrink})
    for key in singles:
        for window in (52,104,156,0):
            for strength in (10,30):
                result.append({'id':f'bins/{key}/w{window}/prior{strength}', 'method':'bins',
                               'group':key,'features':[key],'window':window,'strength':strength})
    return result


def sigmoid(z):
    return 1/(1+np.exp(-np.clip(z,-30,30)))


def fit_probability(train, features, config, explain=False):
    train=train[-config['window']:] if config['window'] else train
    keys=config['features']
    y=np.asarray([r['actual']>0 for r in train],dtype=float)
    base=float((y.sum()+1)/(len(y)+2))
    if config['method'] in ('fixed','ensemble'):
        terms=config.get('terms') or [(config['lookback'],config['sign'],1.)]
        probabilities=[]
        for lookback,sign,weight in terms:
            signal=features[f'mom_risk_{lookback}']*sign*math.sqrt(config['horizon']/lookback)
            probabilities.append(weight*.5*(1+math.erf(np.clip(signal,-8,8)/math.sqrt(2))))
        return float(sum(probabilities)/sum(t[2] for t in terms))
    x=np.asarray([[r['features'][k] for k in keys] for r in train])
    xx=np.asarray([features[k] for k in keys])
    if config['method']=='bins':
        boundaries=np.quantile(x[:,0],[1/3,2/3])
        bins=np.searchsorted(boundaries,x[:,0],side='right')
        current=int(np.searchsorted(boundaries,xx[0],side='right'))
        selected=y[bins==current]; strength=config['strength']
        probability=float((selected.sum()+strength*base)/(len(selected)+strength))
        return (probability,{'boundaries':boundaries.tolist(),'bin':current,'bin_n':len(selected),'train_base':base}) if explain else probability
    mu,sd=x.mean(axis=0),x.std(axis=0); sd=np.where(sd>1e-9,sd,1)
    z=np.column_stack([np.ones(len(x)),np.clip((x-mu)/sd,-4,4)])
    zz=np.r_[1,np.clip((xx-mu)/sd,-4,4)]
    penalty=np.diag([0]+[config['alpha']]*len(keys))
    beta=np.zeros(z.shape[1]); beta[0]=math.log(base/(1-base))
    for _ in range(20):
        p=sigmoid(z@beta); weight=np.maximum(p*(1-p),1e-6)
        grad=z.T@(p-y)/len(y)+penalty@beta
        hessian=(z.T*weight)@z/len(y)+penalty+np.eye(z.shape[1])*1e-8
        step=np.linalg.solve(hessian,grad)
        beta-=step
        if np.max(np.abs(step))<1e-7: break
    probability=float(sigmoid(zz@beta))
    if explain:
        return probability,{'training_mean':mu.tolist(),'training_sd':sd.tolist(),'standardized_coefficients':beta.tolist(),
                             'features':keys,'train_base':base,'train_n':len(train)}
    return probability


def walk(rows,candidates):
    output=[]
    for i,row in enumerate(rows):
        train=[r for r in rows[:i] if r['end_date']<row['as_of']]
        if len(train)<52: continue
        def prior(n=0):
            selected=train[-n:] if n else train
            return (sum(r['actual']>0 for r in selected)+1)/(len(selected)+2)
        base=prior()
        predictions={'base':base,'prior52':prior(52),'prior104':prior(104),'half':.5}
        fitted={}
        for c in candidates:
            key=(c['method'],c['group'],c['window'],c.get('alpha'),c.get('strength'),c.get('sign'),c.get('anchor'))
            if key not in fitted: fitted[key]=fit_probability(train,row['features'],c)
            anchor=.5 if c.get('anchor')=='half' else base
            predictions[c['id']]=float(anchor+c.get('shrink',1)*(fitted[key]-anchor))
        mature=[r for r in output if r['end_date']<row['as_of']][-104:]
        chosen='base'
        if len(mature)>=26:
            chosen=min(predictions,key=lambda k:np.mean([(float(r['actual']>0)-r['probabilities'][k])**2 for r in mature]))
        output.append({**row,'train_n':len(train),'train_label_end':train[-1]['end_date'],
                       'selected':chosen,'probabilities':{**predictions,'nested':predictions[chosen]}})
    return output


def auc(y,p):
    positives=int(y.sum());negatives=len(y)-positives
    if not positives or not negatives: return None
    return float((rank(p)[y>0].sum()-positives*(positives-1)/2)/(positives*negatives))


def balanced(y,p):
    if not np.any(y>0) or not np.any(y==0): return None
    return float((np.mean(p[y>0]>=.5)+np.mean(p[y==0]<.5))/2)


def directional_metrics(rows, name):
    y=np.asarray([float(r['actual']>0) for r in rows]); p=np.asarray([r['probabilities'][name] for r in rows])
    baselines={k:np.asarray([r['probabilities'][k] for r in rows]) for k in ('base','prior52','prior104')}
    loss=(p-y)**2; brier=float(np.mean(loss))
    primary=np.mean((baselines['base']-y)**2)
    folds=np.array_split(np.arange(len(rows)),3)
    def gain(idx,k='base'):
        return float(1-np.mean(loss[idx])/np.mean((baselines[k][idx]-y[idx])**2))
    nonoverlap=nonoverlap_indices(rows)
    signals={}
    for edge in (.03,.05,.10):
        up=(p>=.55)&(p-baselines['base']>=edge)
        down=(p<=.45)&(p-baselines['base']<=-edge)
        selected=up|down
        signals[str(edge)]={'n':int(selected.sum()),'coverage':float(selected.mean()),
            'up_n':int(up.sum()),'up_precision':float(y[up].mean()) if np.any(up) else None,
            'down_n':int(down.sum()),'down_precision':float((1-y[down]).mean()) if np.any(down) else None,
            'accuracy':float(np.mean((p[selected]>=.5)==y[selected])) if np.any(selected) else None,
            'always_up_accuracy_same_dates':float(y[selected].mean()) if np.any(selected) else None,
            'prior_accuracy_same_dates':float(np.mean((baselines['base'][selected]>=.5)==y[selected])) if np.any(selected) else None}
    return {'n':len(y),'brier':brier,'brier_skill':float(1-brier/primary),
        'skill_vs_prior52':gain(np.arange(len(y)),'prior52'),'skill_vs_prior104':gain(np.arange(len(y)),'prior104'),
        'auc':auc(y,p),'balanced_accuracy':balanced(y,p),'accuracy':float(np.mean((p>=.5)==y)),
        'always_up_accuracy':float(y.mean()),'fold_brier_skill':[gain(idx) for idx in folds],
        'fold_auc':[auc(y[idx],p[idx]) for idx in folds],
        'fold_balanced_accuracy':[balanced(y[idx],p[idx]) for idx in folds],
        'tail26_brier_skill':gain(np.arange(max(0,len(y)-26),len(y))),
        'tail52_brier_skill':gain(np.arange(max(0,len(y)-52),len(y))),
        'tail52_auc':auc(y[-52:],p[-52:]),'tail52_balanced_accuracy':balanced(y[-52:],p[-52:]),
        'nonoverlap_skill':[gain(idx) for idx in nonoverlap],
        'nonoverlap_n':[len(idx) for idx in nonoverlap], 'signals':signals,
        'yearly':{year:{'n':len(idx),'skill':gain(idx),'auc':auc(y[idx],p[idx]),'balanced_accuracy':balanced(y[idx],p[idx])}
            for year in sorted({r['as_of'][:4] for r in rows})
            for idx in [np.asarray([i for i,r in enumerate(rows) if r['as_of'].startswith(year)])]}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--fast',action='store_true',help='Adaptive round: fast windows and fixed trend/reversal rules')
    parser.add_argument('--neighborhood',action='store_true',help='Adaptive round: conservative trend neighborhood')
    args=parser.parse_args(); inputs=json.loads(Path(args.inputs).read_text())
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    panels={'long':configs(),'flows':configs({'flows':FLOWS,'flows_price':FLOWS+GROUPS['short_price']},[])}
    if args.fast:
        groups={**GROUPS,'fast_price':['mom_risk_3','mom_risk_10','mom_risk_20','rsi5','vol_ratio']}
        adaptive=[]
        for group,keys in groups.items():
            for window in (13,26,52):
                for alpha in (.01,.1,1):
                    for shrink in (.5,1):
                        adaptive.append({'id':f'logit/{group}/w{window}/a{alpha}/s{shrink}',
                            'method':'logit','group':group,'features':keys,'window':window,'alpha':alpha,'shrink':shrink})
        for n in (3,5,10,20,60,120):
            for sign in (-1,1):
                for shrink in (.25,.5,1):
                    for anchor in ('base','half'):
                        adaptive.append({'id':f'fixed/n{n}/sign{sign}/s{shrink}/{anchor}',
                            'method':'fixed','group':f'mom{n}','features':[f'mom_risk_{n}'],'window':0,
                            'lookback':n,'sign':sign,'shrink':shrink,'anchor':anchor})
        panels={'long':adaptive}
    if args.neighborhood:
        families={f'n{n}':[(n,1,1)] for n in (40,50,60,70,80,100,120)}
        families.update({'pool50_60_70':[(50,1,1),(60,1,1),(70,1,1)],
                         'pool40_60_80':[(40,1,1),(60,1,1),(80,1,1)],
                         'pool20_60_120':[(20,1,1),(60,1,1),(120,1,1)],
                         'trend75_reversal25':[(60,1,.75),(5,-1,.25)],
                         'trend90_reversal10':[(60,1,.9),(5,-1,.1)],
                         'trend50_reversal50':[(60,1,.5),(5,-1,.5)]})
        panels={'long':[{'id':f'prior/{name}/s{shrink}','method':'ensemble','group':name,
                         'features':[f'mom_risk_{t[0]}' for t in terms],'terms':terms,
                         'window':0,'shrink':shrink,'anchor':'half'}
                        for name,terms in families.items() for shrink in (.15,.25,.35,.5)]}
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'status':'adaptive_exploratory',
        'primary_horizon':20,'independent_oos':False,'candidate_configs':panels,'source_hash':gold._hash_gold(inputs),
        'protocol_hash':hashlib.sha256((ROOT/('research/gold_direction_round3_2026-10-20.md' if args.neighborhood else 'research/gold_direction_round2_2026-10-20.md' if args.fast else 'research/gold_direction_protocol_2026-10-20.md')).read_bytes()).hexdigest(),
        'code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'seed':SEED}
    dump(out/'manifest.json',manifest);dump(out/'inputs.json',inputs)
    (out/'research_code.py').write_bytes(Path(__file__).read_bytes())
    all_rows=raw_samples(inputs,feature_series(inputs)); results={}
    for panel,candidates in panels.items():
        results[panel]={}
        for h in (5,20,60):
            for c in candidates: c['horizon']=h
            rows=[r for r in all_rows if r['target']==f'return{h}' and (panel!='flows' or all(k in r['features'] for k in FLOWS))]
            forecasts=walk(rows,candidates)
            metrics={k:directional_metrics(forecasts,k) for k in forecasts[0]['probabilities']}
            results[panel][str(h)]={'start':forecasts[0]['as_of'],'end':forecasts[-1]['as_of'],'metrics':metrics}
            dump(out/f'forecasts_{panel}_{h}.json',forecasts)
            dump(out/'results.json',{'manifest':manifest,'panels':results})
            best=sorted(metrics,key=lambda k:metrics[k]['brier_skill'],reverse=True)[:3]
            print(panel,h,'n',len(forecasts),[(k,round(metrics[k]['brier_skill'],4),round(metrics[k]['balanced_accuracy'],4),round(metrics[k]['auc'],4)) for k in best],flush=True)
    print('Saved',out.resolve(),flush=True)


if __name__=='__main__': main()
