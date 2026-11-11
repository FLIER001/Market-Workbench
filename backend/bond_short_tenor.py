"""Short-maturity (not short-horizon) bond research. Never a production signal.

OPENBLAS_NUM_THREADS=1 backend/.venv/bin/python backend/bond_short_tenor.py
Uses the frozen 20/60/120-day protocol and the existing purged ridge engine.
"""
from __future__ import annotations
import bisect
import hashlib
import json
from datetime import date
from pathlib import Path
import numpy as np
from bond_research import load,series
from bond_tuning import (PARTITIONS,labels_for,mask_partition,rolling_mean,
                         ridge_predictions,trend_prior,summary,bootstrap_improvement)

ROOT=Path('research/output/bonds_short_tenor_2026-10-21')
SOURCE=Path('research/output/bonds_tuning')
PROTOCOL=Path('research/bonds_short_tenor_protocol_2026-10-21.md')


def delta(values,k,is_yield=True):
    out=np.full(len(values),np.nan)
    out[k:]=(values[k:]-values[:-k])*100 if is_yield else (values[k:]/values[:-k]-1)*10000
    return out


def asof_quotes(dates,mapping,max_age=7):
    """Only previous real observations; save age, do not invent same-day quotes."""
    keys=sorted(mapping);values=np.full(len(dates),np.nan);ages=values.copy();observed=[]
    for i,d in enumerate(dates):
        k=bisect.bisect_right(keys,str(d))-1
        if k<0:observed.append(None);continue
        source_date=keys[k];age=(date.fromisoformat(str(d))-date.fromisoformat(source_date)).days
        if age<=max_age:
            values[i]=mapping[source_date];ages[i]=age;observed.append(source_date)
        else:observed.append(None)
    return values,ages,observed


def load_panel(root=ROOT,source=SOURCE):
    data=np.load(source/'data_panel.npz');dates=data['dates'];paths=[source/'data_panel.npz']
    maps={tenor:{} for tenor in ('3月','6月','1年','10年')}
    files=sorted((source/'inputs').glob('curve_*.json'))+sorted((root/'inputs').glob('curve_*.json'))
    for p in files:
        paths.append(p);r=load(p)
        for tenor in maps:maps[tenor].update(series(r['yields'].get(tenor)))
    yields={name:np.array([maps[tenor].get(str(d),np.nan) for d in dates])
            for name,tenor in (('gov3M','3月'),('gov6M','6月'),('gov1Y','1年'),('gov10Y','10年'))}
    quality={'dates':[str(dates[0]),str(dates[-1])],'n':len(dates),'targets':{},'funding':{},'omitted':[]}
    original_one=data['yield10Y']-data['curve'][:,1]
    quality['overlap_1Y_max_diff_pct']=float(np.nanmax(abs(yields['gov1Y']-original_one)))
    quality['overlap_10Y_max_diff_pct']=float(np.nanmax(abs(yields['gov10Y']-data['yield10Y'])))
    if quality['overlap_1Y_max_diff_pct']>1e-6 or quality['overlap_10Y_max_diff_pct']>1e-6:
        raise ValueError('Same-source overlap revised; inspect and freeze vintage before fitting')
    def points(path):
        paths.append(path);return series(load(path)['series'])
    def array(path):
        m=points(path);return np.array([m.get(str(d),np.nan) for d in dates])
    index=array(source/'inputs/index_short_财富.json')
    ytm=array(source/'inputs/index_short_平均市值法到期收益率.json')
    duration_path=root/'inputs/duration_short.json'
    duration=array(duration_path) if duration_path.exists() else None
    if duration is None:quality['omitted'].append('short-index duration download unavailable; no replacement')
    funding={};asof_records={}
    for tenor in ('隔夜','1周','3月'):
        path=root/'inputs'/f'shibor_{tenor}.json'
        if not path.exists():quality['omitted'].append(f'Shibor {tenor} download unavailable');continue
        m=points(path);values,ages,observed=asof_quotes(dates,m)
        funding[tenor]=(values,ages)
        quality['funding'][tenor]={'source':'Shibor unsecured quote, not DR007 or executable financing',
            'start':min(m),'end':max(m),'same_day_missing':sum(str(d) not in m for d in dates),
            'asof_missing':int((~np.isfinite(values)).sum()),'aged_quotes':int(np.sum(ages>0)),
            'max_age_days':float(np.nanmax(ages))}
        asof_records[tenor]=[{'date':str(d),'quote_date':obs,'age_days':None if not np.isfinite(age) else int(age)}
                            for d,obs,age in zip(dates,observed,ages)]
    for name,values in {**yields,'short_index':index}.items():
        quality['targets'][name]={'missing':int((~np.isfinite(values)).sum()),
                                 'min':float(np.nanmin(values)),'max':float(np.nanmax(values))}
    (root/'data_quality.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
    (root/'funding_asof_dates.json').write_text(json.dumps(asof_records,ensure_ascii=False))
    np.savez_compressed(root/'data_panel.npz',dates=dates,**yields,short_index=index,index_ytm=ytm)
    return dates,yields,index,ytm,duration,funding,paths,quality


def features_for(yields,funding,own,is_yield,ytm,duration):
    three,six,one,ten=[yields[k] for k in ('gov3M','gov6M','gov1Y','gov10Y')]
    curve=np.column_stack([one,six-three,one-six,ten-one])
    momentum=np.column_stack([delta(yields[k],lag) for k in ('gov3M','gov6M','gov1Y') for lag in (5,20,60)])
    if not is_yield:
        index_columns=[ytm]+([] if duration is None else [duration])
        curve=np.column_stack([curve,*index_columns])
        momentum=np.column_stack([momentum,*[delta(own,k,False) for k in (5,20,60)]])
    combined=np.column_stack([curve,momentum])
    groups={'curve':curve,'momentum':momentum,'combined':combined,
            'nonlinear':np.column_stack([combined,one*(one-three),delta(one,20)*(one-three)])}
    if '隔夜' in funding and '3月' in funding:
        on,on_age=funding['隔夜'];term,term_age=funding['3月']
        fund=np.column_stack([on,term,term-on,one-term,delta(on,5),delta(on,20),
                              delta(term,5),delta(term,20),on_age,term_age])
        if '1周' in funding:
            week,age=funding['1周'];fund=np.column_stack([fund,week,week-on,age])
        groups['funding']=fund
        groups['funding_combined']=np.column_stack([combined,fund])
    return groups


def candidates(groups,y,own,h,is_yield,ytm=None):
    base=rolling_mean(y,h,756)
    ps={'zero':np.zeros(len(y)),'mean_756':base,'mean_252':rolling_mean(y,h,252)}
    for window in (20,60,120,252):
        trend=delta(own,window,is_yield)*h/window
        for weight in (.25,.5,1.):
            for sign,name in ((1,'trend'),(-1,'reversal')):
                ps[f'{name}_{window}_{weight}']=trend_prior(base,trend,weight,sign)
    for group,x in groups.items():
        for window in (252,756,1260):
            for alpha in (10.,100.,1000.):
                p=ridge_predictions(x,y,h,window,alpha)
                for shrink in (.25,.5,1.):
                    ps[f'ridge_{group}_{window}_{alpha}_{shrink}']=(1-shrink)*base+shrink*p
    if ytm is not None:
        ps['carry']=ytm*100*h/252;ps['carry_half']=.5*(base+ps['carry'])
    return ps


def select(dates,y,ps,h):
    base=ps['mean_756'];rows=[];excluded=[]
    masks={part:mask_partition(dates,h,part)&np.isfinite(y)&np.isfinite(base)
           for part in ('validation1','validation2')}
    for name,p in ps.items():
        stats={part:summary(y,p,base,mask) for part,mask in masks.items()}
        if any(stats[part]['n']!=int(mask.sum()) for part,mask in masks.items()):
            excluded.append(name);continue
        rows.append({'model':name,'selection_loss_ratio':1-float(np.mean([v['r2_mean'] for v in stats.values()])),**stats})
    ranked=sorted(rows,key=lambda r:(r['selection_loss_ratio'],r['model']))
    members=[r['model'] for r in ranked if r['model'].startswith('ridge_')][:5]
    ps['ensemble_top5']=np.mean([ps[name] for name in members],axis=0)
    stats={part:summary(y,ps['ensemble_top5'],base,mask) for part,mask in masks.items()}
    rows.append({'model':'ensemble_top5','selection_loss_ratio':1-float(np.mean([v['r2_mean'] for v in stats.values()])),**stats})
    selected=min(rows,key=lambda r:(r['selection_loss_ratio'],r['model']))['model']
    return selected,rows,excluded,members


def economics(y,p,base,mask,h):
    rows=[]
    for phase in range(h):
        origins=np.flatnonzero(mask&np.isfinite(y)&np.isfinite(p)&np.isfinite(base)&(np.arange(len(y))%h==phase))
        for model,pred in (('buy_hold',np.ones(len(y))),('mean',base),('selected',p)):
            w=(pred[origins]>0).astype(float);turnover=np.abs(np.diff(np.r_[0.,w,0.]))
            for cost in (0.,.5,1.,2.,5.):
                ret=(w*y[origins]-cost*turnover[:-1])/10000
                if len(ret):ret[-1]-=cost*turnover[-1]/10000
                nav=np.r_[1.,np.cumprod(1+ret)]
                rows.append({'model':model,'phase':phase,'cost_bp':cost,'n_periods':len(origins),
                    'return_pct':float((nav[-1]-1)*100),'turnover':float(turnover.sum()),
                    'max_drawdown_pct':float(np.min(nav/np.maximum.accumulate(nav)-1)*100)})
    return rows


def run(root=ROOT,source=SOURCE):
    root.mkdir(parents=True,exist_ok=True)
    dates,yields,index,ytm,duration,funding,paths,quality=load_panel(root,source)
    protocol=PROTOCOL.read_bytes();(root/'protocol.md').write_bytes(protocol)
    manifest={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths+[PROTOCOL,Path(__file__)]}
    (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    choices=[];rankings=[];jobs=[]
    targets={k:yields[k] for k in ('gov3M','gov6M','gov1Y')};targets['short_index']=index
    for target,own in targets.items():
        is_yield=target!='short_index';groups=features_for(yields,funding,own,is_yield,ytm,duration)
        for h in (20,60,120):
            key=f'{target}_{h}';print('fit',key,flush=True)
            y=labels_for(own,h,is_yield);ps=candidates(groups,y,own,h,is_yield,None if is_yield else ytm)
            selected,rows,excluded,members=select(dates,y,ps,h)
            choice={'task':key,'target':target,'horizon':h,'selected':selected,'candidate_count':len(rows),
                    'excluded_missing_validation':excluded,'ensemble_members':members}
            choices.append(choice);rankings.extend([{'task':key,**r} for r in rows])
            np.savez_compressed(root/f'predictions_{key}.npz',actual=y,**ps)
            jobs.append((choice,y,ps))
    (root/'selection_frozen.json').write_text(json.dumps({'choices':choices,'manifest':manifest,
        'warning':'Previously viewed historical outcomes; all new results are exploratory'},indent=2))
    (root/'validation_rankings.json').write_text(json.dumps(rankings,indent=2))
    print('SELECTION FROZEN; EVALUATING',flush=True)
    results=[]
    for choice,y,ps in jobs:
        h=choice['horizon'];s=choice['selected'];p=ps[s];base=ps['mean_756']
        r={**choice,'partitions':{},'years':{},'bootstrap':{},'families':{}}
        for part in PARTITIONS:
            mask=mask_partition(dates,h,part)
            r['partitions'][part]={name:summary(y,ps[name],base,mask) for name in dict.fromkeys(('zero','mean_756',s))}
        for part in ('audit','extension_used'):
            mask=mask_partition(dates,h,part)&np.isfinite(y)&np.isfinite(p)&np.isfinite(base)
            r['bootstrap'][part]=bootstrap_improvement(y[mask],p[mask],base[mask],h)
            r[f'{part}_nonoverlap_r2']=[summary(y,p,base,mask&(np.arange(len(y))%h==phase))['r2_mean'] for phase in range(h)]
            if choice['target']=='short_index':r[f'economics_{part}']=economics(y,p,base,mask,h)
        for year in range(2020,2027):
            mask=mask_partition(dates,h,'audit' if year<2023 else 'extension_used')&np.char.startswith(dates,str(year))
            r['years'][str(year)]=summary(y,p,base,mask)
        rows=[row for row in rankings if row['task']==choice['task']]
        for group in ('curve','momentum','combined','nonlinear','funding','funding_combined'):
            names=[row for row in rows if row['model'].startswith(f'ridge_{group}_') and row['model'].removeprefix('ridge_').rsplit('_',3)[0]==group]
            if not names:continue
            best=min(names,key=lambda z:z['selection_loss_ratio']);pred=ps[best['model']]
            r['families'][group]={'selected':best['model'],'validation_loss_ratio':best['selection_loss_ratio'],
                **{part:summary(y,pred,base,mask_partition(dates,h,part)) for part in ('audit','extension_used')}}
        if s.startswith('ridge_'):
            group=s.removeprefix('ridge_').rsplit('_',3)[0]
            neighbors=[pred for name,pred in ps.items() if name.startswith('ridge_') and name.removeprefix('ridge_').rsplit('_',3)[0]==group]
            vals=[summary(y,pred,base,mask_partition(dates,h,'audit'))['r2_mean'] for pred in neighbors]
            r['neighborhood']={'n':len(vals),'positive_fraction':float(np.mean(np.array(vals)>0)),
                               'median_r2':float(np.median(vals))}
        a=r['partitions']['audit'][s];e=r['partitions']['extension_used'][s]
        r['candidate_prior']=a['r2_mean']>0 and e['r2_mean']>0 and sum((r['years'][str(yr)]['r2_mean'] or 0)>0 for yr in (2020,2021,2022))>=2
        results.append(r)
    pv=np.array([max(z['p_one_sided'] for part in r['bootstrap'].values() for z in part.values()) for r in results])
    order=np.argsort(pv);q=np.minimum.accumulate((pv[order]*len(pv)/np.arange(1,len(pv)+1))[::-1])[::-1]
    for idx,v in zip(order,q):results[idx]['bh_q']=float(min(1,v))
    for r in results:r['strong_evidence']=r['candidate_prior'] and r['bh_q']<.05 and all(z['ci95'][0]>0 for part in r['bootstrap'].values() for z in part.values())
    report={'status':'short_maturity_exploratory','data_quality':quality,
            'total_comparisons':sum(c['candidate_count'] for c in choices),'results':results}
    (root/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for r in results:
        print(r['task'],r['selected'],round(r['partitions']['audit'][r['selected']]['r2_mean'],4),
            round(r['partitions']['extension_used'][r['selected']]['r2_mean'],4),r['candidate_prior'],r['strong_evidence'],flush=True)
    return report

if __name__=='__main__':run()
